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
# After this many tool rounds we strip ``tools`` / ``tool_choice`` from
# the payload so the model is forced to finalise its answer instead of
# looping on tool calls. Some templates (gemma4-98e) otherwise keep
# emitting partial JSON fragments as content and never commit to a
# complete response. Set to 0 to keep tools available for every round.
DEFAULT_FORCE_ANSWER_AFTER = 5
# Cap how many tool_calls we will execute from a single assistant turn.
# Models with a looping failure mode (gemma4-98e has been observed
# emitting 800+ calls in one response) otherwise bloat context into
# uselessness. The remaining calls are silently dropped — the model
# sees the first N results and can request more next round.
DEFAULT_MAX_TOOL_CALLS_PER_TURN = 8


def _max_iterations() -> int:
    try:
        return int(os.environ.get("CALIBER_GROUNDING_MAX_ITER",
                                  str(DEFAULT_MAX_ITERATIONS)))
    except ValueError:
        return DEFAULT_MAX_ITERATIONS


def _think_setting() -> Any:
    """Map ``CALIBER_GROUNDING_THINK`` to the ``think`` field Ollama
    accepts on /api/chat (and ``reasoning_effort`` on /v1 OpenAI-compat).

    Accepts: false (default), true, low, medium, high.

    Gemma4 has a well-known overthinking failure mode — left unconstrained
    it burns the whole context on `"Wait, let me re-read..."` loops and
    never produces the final answer. Caliber's task is structured
    output, not puzzle-solving, so we default to ``false`` (no thinking).
    """
    v = os.environ.get("CALIBER_GROUNDING_THINK", "medium").strip().lower()
    if v in ("", "false", "0", "no", "off"):
        return False
    if v in ("true", "1", "yes", "on"):
        return True
    if v in ("low", "medium", "high"):
        return v
    return False


def _model_override() -> Optional[str]:
    """If set, every upstream request uses this model regardless of the
    one the client sent. Caliber filters OpenAI model names through a
    hard allowlist (gpt-4*, o3-*) — this lets us accept any placeholder
    name from the client and always route to our real Ollama tag.
    """
    v = os.environ.get("CALIBER_GROUNDING_MODEL_OVERRIDE")
    return v.strip() if v and v.strip() else None


def _max_tool_calls_per_turn() -> int:
    try:
        return int(os.environ.get(
            "CALIBER_GROUNDING_MAX_TOOL_CALLS_PER_TURN",
            str(DEFAULT_MAX_TOOL_CALLS_PER_TURN),
        ))
    except ValueError:
        return DEFAULT_MAX_TOOL_CALLS_PER_TURN


def _force_answer_after() -> int:
    try:
        return int(os.environ.get("CALIBER_GROUNDING_FORCE_ANSWER_AFTER",
                                  str(DEFAULT_FORCE_ANSWER_AFTER)))
    except ValueError:
        return DEFAULT_FORCE_ANSWER_AFTER


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


def _execute_tool_calls(tool_calls: list[dict], cwd: str,
                         seen: dict[str, str]) -> list[dict]:
    """Run each tool and return the resulting ``role: tool`` messages.

    Duplicate (name+args) calls within a single agent loop return a
    short stub pointing the model back at the prior result. Models
    (notably gemma4-98e) will otherwise loop on the same tool call
    dozens of times, exploding context and wall time.
    """
    results = []
    for tc in tool_calls:
        tc_id = tc.get("id") or ""
        fn = tc.get("function") or {}
        name = fn.get("name") or ""
        args_str = fn.get("arguments") or "{}"
        # Tool calls can come as dicts OR stringified JSON — handle both.
        if isinstance(args_str, dict):
            args_str = json.dumps(args_str)
        key = f"{name}|{args_str}"
        if key in seen:
            output = (
                f"(duplicate: you already called {name}({args_str[:80]}). "
                "Use that prior result and continue. Do not repeat this call.)"
            )
            log.info("tool %s(%s) -> DEDUP stub", name, args_str[:80])
        else:
            t0 = time.monotonic()
            output = tools.execute(name, args_str, cwd)
            dt_ms = int((time.monotonic() - t0) * 1000)
            log.info(
                "tool %s(%s) -> %d chars in %d ms",
                name, args_str[:80], len(output), dt_ms,
            )
            seen[key] = output
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
    force_after = _force_answer_after() if tools_available else 0
    override = _model_override()
    if override:
        payload = dict(payload)
        payload["model"] = override
    # Constrain thinking budget. Ollama accepts both the native ``think``
    # field and the OpenAI-style ``reasoning_effort`` — set both so we
    # work against either endpoint path.
    think = _think_setting()
    if think is False:
        payload["think"] = False
        payload["reasoning_effort"] = "none"
    elif think is True:
        payload["think"] = True
    else:
        # "low" | "medium" | "high"
        payload["think"] = think
        payload["reasoning_effort"] = think
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
    tools_stripped = False
    seen_calls: dict[str, str] = {}
    for i in range(max_iterations):
        # After N tool rounds, strip the tool list so the model stops
        # looping on tool calls and commits to the final prose+JSON
        # answer. Caliber's parser expects: STATUS lines, EXPLAIN:, JSON.
        # Some templates (gemma4-98e) otherwise emit empty ```  fences.
        if (tools_available and force_after > 0 and i >= force_after
                and not tools_stripped):
            log.info(
                "force-answer: stripping tools at iter %d (after=%d)",
                i, force_after,
            )
            payload.pop("tools", None)
            payload.pop("tool_choice", None)
            tools_stripped = True
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
        # Guard against runaway tool_call bursts. gemma4-98e has been
        # observed emitting hundreds of IDENTICAL calls in one response.
        # First collapse by unique (name, arguments), then cap. This
        # preserves distinct calls when the model legitimately asks
        # for several at once.
        original_len = len(tool_calls)
        uniq: list[dict] = []
        seen_sigs: set[str] = set()
        for tc in tool_calls:
            fn = tc.get("function") or {}
            args = fn.get("arguments") or ""
            if isinstance(args, dict):
                args = json.dumps(args, sort_keys=True)
            sig = f"{fn.get('name','')}|{args}"
            if sig in seen_sigs:
                continue
            seen_sigs.add(sig)
            uniq.append(tc)
        cap = _max_tool_calls_per_turn()
        if len(uniq) > cap:
            uniq = uniq[:cap]
        if len(uniq) != original_len:
            log.info(
                "tool_calls %d -> %d unique (cap=%d) on iter %d",
                original_len, len(uniq), cap, i,
            )
        tool_calls = uniq
        # Normalise the assistant message: when tool_calls are present,
        # drop any ``content`` — models sometimes emit template
        # fragments or partial JSON alongside the structured call, and
        # re-sending that garbage on the next turn derails generation.
        clean_msg = dict(msg)
        clean_msg["content"] = None
        clean_msg["tool_calls"] = tool_calls  # already capped above
        # Gemma4 docs: "In multi-turn conversations, the historical model
        # output should only include the final response. Thoughts from
        # previous model turns must not be added before the next user
        # turn begins." Strip thinking/reasoning fields before echoing.
        for k in ("thinking", "reasoning", "reasoning_content"):
            clean_msg.pop(k, None)
        payload["messages"] = list(payload["messages"]) + [clean_msg]
        payload["messages"].extend(
            _execute_tool_calls(tool_calls, cwd, seen_calls)
        )
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

    def _write_sse(self, result: dict) -> None:
        """Synthesize an OpenAI-style SSE stream from a completed non-
        streaming result. Our agent loop is inherently non-streaming
        (we need to inspect tool_calls between iterations), so when
        the caller asks for ``stream: true`` we simulate SSE by
        chunking the final content and emitting standard
        chat.completion.chunk events. Caliber's stream parser only
        reads ``choices[0].delta.content`` so that's what we deliver.
        """
        try:
            choice = (result.get("choices") or [{}])[0]
            msg = choice.get("message") or {}
            content = msg.get("content") or ""
            role = msg.get("role") or "assistant"
            finish = choice.get("finish_reason") or "stop"
            tool_calls = msg.get("tool_calls")
            usage = result.get("usage")
            model = result.get("model") or "caliber-grounding-proxy"
            created = result.get("created") or int(time.time())
            cid = result.get("id") or f"chatcmpl-{created}"

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()

            def emit(delta: dict, finish_reason=None) -> None:
                chunk = {
                    "id": cid,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [{
                        "index": 0,
                        "delta": delta,
                        "finish_reason": finish_reason,
                    }],
                }
                self.wfile.write(b"data: ")
                self.wfile.write(json.dumps(chunk).encode("utf-8"))
                self.wfile.write(b"\n\n")
                self.wfile.flush()

            # Role header
            emit({"role": role})
            # Body: either tool_calls, or text chunks
            if tool_calls:
                emit({"tool_calls": tool_calls})
            if content:
                # Chunk the content so streaming clients see progress;
                # 1 KB per chunk keeps bookkeeping cheap.
                step = 1024
                for i in range(0, len(content), step):
                    emit({"content": content[i:i + step]})
            # Final usage + finish_reason
            final_chunk = {
                "id": cid,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": {},
                             "finish_reason": finish}],
            }
            if usage is not None:
                final_chunk["usage"] = usage
            self.wfile.write(b"data: ")
            self.wfile.write(json.dumps(final_chunk).encode("utf-8"))
            self.wfile.write(b"\n\ndata: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler convention
        if self.path == "/health":
            self._write_json(200, {"ok": True, "service": "caliber-grounding-proxy"})
            return
        if self.path == "/v1/models":
            # Advertise one caliber-allowlisted name so its model-recovery
            # UI has something to pick, plus the real Ollama tag we route
            # to. The override (if set) wins internally anyway.
            models = [
                {"id": "gpt-4o", "object": "model"},
                {"id": "gpt-4o-mini", "object": "model"},
            ]
            ov = _model_override()
            if ov:
                models.append({"id": ov, "object": "model"})
            self._write_json(200, {"object": "list", "data": models})
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
        stream_requested = bool(payload.get("stream"))
        # Optional dump of the full agent-loop result for debugging
        # truncation / JSON parse failures downstream. Set
        # CALIBER_GROUNDING_RESULT_DUMP_DIR to capture one file per request.
        dump_dir = os.environ.get("CALIBER_GROUNDING_RESULT_DUMP_DIR")
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
        if dump_dir:
            try:
                os.makedirs(dump_dir, exist_ok=True)
                fname = os.path.join(
                    dump_dir, f"result-{int(time.time() * 1000)}.json",
                )
                with open(fname, "w", encoding="utf-8") as f:
                    json.dump({"stream": stream_requested, "result": result},
                              f, indent=2, ensure_ascii=False)
            except OSError:
                pass
        if stream_requested:
            self._write_sse(result)
        else:
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
