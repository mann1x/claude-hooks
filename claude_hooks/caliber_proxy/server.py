"""OpenAI-compatible HTTP server that augments Ollama with project
grounding. Runs the tool-call agent loop locally (read_file, grep, glob,
list_files) so gemma4-98e (or any tool-capable Ollama model) can
cite real `path:line` references in caliber's init / regenerate output.

Architecture:

::

    caliber ──POST /v1/chat/completions──► this proxy
                                              │
                                              ├─ prepend grounding system + anchors
                                              ├─ inject tool specs
                                              ▼
                                           Ollama
                                              │
                                              ▼
                              if finish_reason == "tool_calls":
                                  execute tools locally, loop
                              else:
                                  mirror back to caliber

The proxy binds to ``127.0.0.1:38090`` by default (local-only; another
host can't see the project files anyway). Change via
``CALIBER_GROUNDING_HOST`` / ``CALIBER_GROUNDING_PORT``.

Exit codes:
    0 on clean SIGTERM / SIGINT; non-zero on bind / config failure.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

from claude_hooks.caliber_proxy import ollama, prompt, tools

log = logging.getLogger("claude_hooks.caliber_proxy.server")

# Max number of tool-call rounds before we give up and let the model
# produce whatever it has. User-configured at 35 (decision #3).
DEFAULT_MAX_ITERATIONS = 35


def _max_iterations() -> int:
    try:
        return int(os.environ.get("CALIBER_GROUNDING_MAX_ITER",
                                  str(DEFAULT_MAX_ITERATIONS)))
    except ValueError:
        return DEFAULT_MAX_ITERATIONS


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _tools_enabled() -> bool:
    """When False, the proxy does NOT inject tool specs. Useful for models
    whose tool-use template is weak on multi-turn (e.g. custom Gemma
    variants without proper role-tool template support). Pre-stuffing
    becomes the only grounding mechanism in that mode.
    """
    return _env_flag("CALIBER_GROUNDING_TOOLS", default=True)


def _extended_sources_enabled() -> bool:
    """When True, pre-stuffing expands to include a curated subset of
    project source files (capped at 200 KB). Recommended when
    ``CALIBER_GROUNDING_TOOLS=0`` so the model still has deep context."""
    return _env_flag("CALIBER_GROUNDING_EXTENDED", default=False)


def _cwd_for_request() -> str:
    """The proxy grounds against the directory it was launched in. If
    caliber is invoked from /srv/.../claude-hooks, the proxy must have
    been started in that same directory (via caliber-smart or manually).
    We *don't* accept a cwd override from the request body — that's a
    trivial prompt-injection vector to pivot into another project.
    """
    return os.environ.get("CALIBER_GROUNDING_CWD", os.getcwd())


# -- Agent loop ------------------------------------------------------- #
def _merge_tools(existing: Optional[list[dict]]) -> list[dict]:
    """Combine caliber's tool list (usually none) with ours. Caller tools
    win by name on collision — we stay out of the way if caliber ever
    starts sending its own."""
    our_specs = tools.openai_tool_specs()
    if not existing:
        return our_specs
    existing_names = {
        t.get("function", {}).get("name") for t in existing
        if isinstance(t, dict)
    }
    return list(existing) + [
        s for s in our_specs
        if s["function"]["name"] not in existing_names
    ]


def _inject_grounding(messages: list[dict], cwd: str,
                      tools_available: bool) -> list[dict]:
    """Prepend grounding system messages. We place them at the very front
    so they precede caliber's own system prompt — Ollama treats multiple
    system messages as additive, and the addendum is a hard constraint
    we want evaluated before caliber's task instructions.

    When the proxy is running without tool injection
    (``CALIBER_GROUNDING_TOOLS=0``), the addendum switches to a no-tools
    variant that tells the model all grounding is pre-loaded, and
    extended source pre-stuffing is forced on so there's more material
    for the model to ground against.
    """
    # Force extended sources whenever tools are off — the model has no
    # other way to reach source code beyond what we pre-stuff.
    extended = _extended_sources_enabled() or not tools_available
    grounding = prompt.build_grounding_messages(
        cwd,
        extended_sources=extended,
        tools_available=tools_available,
    )
    return grounding + list(messages)


def _execute_tool_calls(tool_calls: list[dict], cwd: str) -> list[dict]:
    """Run each tool and return the resulting ``role: tool`` messages."""
    results = []
    for tc in tool_calls:
        tc_id = tc.get("id") or ""
        fn = tc.get("function") or {}
        name = fn.get("name") or ""
        args_str = fn.get("arguments") or "{}"
        # Tool calls can come as dicts OR stringified JSON — handle both.
        if isinstance(args_str, dict):
            args_str = json.dumps(args_str)
        t0 = time.monotonic()
        output = tools.execute(name, args_str, cwd)
        dt_ms = int((time.monotonic() - t0) * 1000)
        log.info(
            "tool %s(%s) -> %d chars in %d ms",
            name, args_str[:80], len(output), dt_ms,
        )
        results.append({
            "role": "tool",
            "tool_call_id": tc_id,
            "name": name,
            "content": output,
        })
    return results


def run_agent_loop(payload: dict, cwd: str,
                   max_iterations: Optional[int] = None) -> dict:
    """Drive the tool-use loop until the model stops calling tools or the
    iteration cap is hit. Returns the final Ollama chat-completion JSON.
    """
    if max_iterations is None:
        max_iterations = _max_iterations()

    tools_available = _tools_enabled()
    messages = _inject_grounding(
        payload.get("messages") or [], cwd, tools_available=tools_available,
    )
    payload = dict(payload)
    payload["messages"] = messages
    if tools_available:
        payload["tools"] = _merge_tools(payload.get("tools"))
    else:
        # No tool injection — pre-stuffing is the only grounding. Strip
        # any caliber-supplied tool list too so the model doesn't try to
        # call something the proxy can't service.
        payload.pop("tools", None)
        payload.pop("tool_choice", None)
    # Force non-streaming inside the loop so we can inspect tool_calls
    # cleanly. We'll decide whether to stream the final turn back to
    # the caller separately.
    payload.pop("stream", None)

    final: dict[str, Any] = {}
    for i in range(max_iterations):
        final = ollama.chat_completions(payload)
        choices = final.get("choices") or []
        if not choices:
            log.warning("ollama returned empty choices on iter %d", i)
            break
        choice = choices[0]
        msg = choice.get("message") or {}
        tool_calls = msg.get("tool_calls") or []
        finish_reason = choice.get("finish_reason")
        log.debug(
            "iter %d: finish=%s, tool_calls=%d, content_len=%d",
            i, finish_reason, len(tool_calls),
            len((msg.get("content") or "")),
        )
        if finish_reason != "tool_calls" or not tool_calls:
            break
        # Normalise the assistant message: when tool_calls are present,
        # drop any ``content`` — models sometimes emit template
        # fragments or partial JSON alongside the structured call, and
        # re-sending that garbage on the next turn derails generation.
        clean_msg = dict(msg)
        clean_msg["content"] = None
        payload["messages"] = list(payload["messages"]) + [clean_msg]
        payload["messages"].extend(_execute_tool_calls(tool_calls, cwd))
    else:
        log.warning("agent loop hit max_iterations=%d", max_iterations)
    return final


# -- HTTP server ------------------------------------------------------ #
class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A002
        log.debug("%s - - %s", self.address_string(), fmt % args)

    def _write_json(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler convention
        if self.path == "/health":
            self._write_json(200, {"ok": True, "service": "caliber-grounding-proxy"})
            return
        if self.path == "/v1/models":
            self._write_json(200, {
                "object": "list",
                "data": [{"id": "caliber-grounding-proxy", "object": "model"}],
            })
            return
        self._write_json(404, {"error": {"message": "not found"}})

    def do_POST(self):  # noqa: N802
        if self.path not in ("/v1/chat/completions", "/chat/completions"):
            self._write_json(404, {"error": {"message": "not found"}})
            return
        clen = int(self.headers.get("Content-Length") or 0)
        try:
            body = self.rfile.read(clen) if clen > 0 else b""
            payload = json.loads(body) if body else {}
        except (ValueError, OSError) as e:
            self._write_json(400, {"error": {"message": f"bad request: {e}"}})
            return

        cwd = _cwd_for_request()
        try:
            result = run_agent_loop(payload, cwd)
        except ollama.UpstreamError as e:
            # Relay upstream failures faithfully — otherwise caliber
            # gets a 200-with-empty-choices and silently produces
            # "Model produced no output" instead of a debuggable error.
            log.warning("upstream error %d: %s", e.status,
                        str(e.body)[:200])
            self._write_json(e.status,
                             e.body if isinstance(e.body, dict)
                             else {"error": {"message": str(e.body)}})
            return
        except Exception as e:
            log.exception("agent loop failed")
            self._write_json(502, {"error": {
                "type": "proxy_error",
                "message": f"grounding proxy failed: {e}",
            }})
            return
        self._write_json(200, result)


def build_server(host: Optional[str] = None,
                 port: Optional[int] = None,
                 ) -> ThreadingHTTPServer:
    host = host or os.environ.get("CALIBER_GROUNDING_HOST", "127.0.0.1")
    port = port or int(os.environ.get("CALIBER_GROUNDING_PORT", "38090"))
    server = ThreadingHTTPServer((host, port), _Handler)
    server.daemon_threads = True
    return server


def run() -> int:
    logging.basicConfig(
        level=os.environ.get("CALIBER_GROUNDING_LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )
    try:
        server = build_server()
    except OSError as e:
        print(f"caliber-grounding-proxy: bind failed: {e}", file=sys.stderr)
        return 1
    host, port = server.server_address
    print(
        f"caliber-grounding-proxy listening on http://{host}:{port}/v1 -> "
        f"{ollama.default_upstream()}",
        file=sys.stderr,
    )
    print(
        f"  cwd: {_cwd_for_request()}",
        file=sys.stderr,
    )
    print(
        f"  point caliber at it via: OPENAI_BASE_URL=http://{host}:{port}/v1 "
        f"OPENAI_API_KEY=ollama",
        file=sys.stderr,
    )
    serve_thread = threading.Thread(target=server.serve_forever, daemon=True)
    serve_thread.start()
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    try:
        while not stop.is_set():
            stop.wait(timeout=1.0)
    finally:
        try:
            server.shutdown()
        except Exception:
            pass
        try:
            server.server_close()
        except Exception:
            pass
        ollama.close()
    return 0
