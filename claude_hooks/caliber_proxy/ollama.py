"""Thin client that POSTs to Ollama's native ``/api/chat`` endpoint
with on-the-fly translation between OpenAI ChatCompletion shape (what
caliber sends and what our agent loop / SSE writer expect) and Ollama's
native chat shape.

Why not /v1/chat/completions: Ollama's OpenAI-compat endpoint maps a
fixed list of OpenAI fields onto its internal options block and silently
drops everything else. In particular ``options.num_ctx`` in the request
body is ignored — the model loads at the Modelfile's baked default
(256k for gemma4-98e), so ``CALIBER_GROUNDING_NUM_CTX`` had no effect.
``/api/chat`` honours the full options block, plus native fields like
``think`` and ``keep_alive``, and is the right surface for a proxy that
needs to inject Ollama-specific runtime parameters per request.

The translators stay self-contained; the public surface
(:func:`chat_completions`, :class:`UpstreamError`, :func:`close`) and
its OpenAI-shaped return value are unchanged so callers and tests don't
need to know the underlying call moved.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Optional

log = logging.getLogger("claude_hooks.caliber_proxy.ollama")

try:
    import httpx
except ImportError as e:  # pragma: no cover - guarded at install time
    raise ImportError(
        "caliber-grounding-proxy requires httpx. Install with:\n"
        "    pip install 'httpx[http2]>=0.27'"
    ) from e


def default_upstream() -> str:
    """Configured upstream Ollama base. Accepts either the bare host
    (``http://192.168.178.2:11433``) or the legacy ``.../v1`` form;
    :func:`_base_url` strips the suffix either way.
    """
    return os.environ.get(
        "CALIBER_GROUNDING_UPSTREAM",
        "http://192.168.178.2:11433",
    )


def _base_url(upstream: Optional[str] = None) -> str:
    """Strip a trailing ``/v1`` from the upstream so we can hit the
    native ``/api/*`` endpoints. Backwards-compatible with configs that
    set the URL to ``http://host:port/v1``.
    """
    u = (upstream or default_upstream()).rstrip("/")
    if u.endswith("/v1"):
        u = u[: -len("/v1")]
    return u


def default_timeout() -> float:
    try:
        return float(os.environ.get("CALIBER_GROUNDING_HTTP_TIMEOUT", "600"))
    except ValueError:
        return 600.0


_CLIENT: Optional[httpx.Client] = None


def _get_client() -> httpx.Client:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = httpx.Client(
            timeout=httpx.Timeout(default_timeout(), connect=10.0),
            limits=httpx.Limits(
                max_keepalive_connections=4, max_connections=8,
            ),
            trust_env=False,
        )
    return _CLIENT


class UpstreamError(RuntimeError):
    """Upstream returned a non-2xx response. Carries the status and
    body so the proxy can relay a faithful error to the client instead
    of masking it as a success with empty choices."""

    def __init__(self, status: int, body: Any) -> None:
        super().__init__(f"upstream returned {status}")
        self.status = status
        self.body = body


# --- Request translation: OpenAI -> Ollama /api/chat ---------------- #

# OpenAI sampling fields that map cleanly to Ollama options.<same-or-aliased>.
_SAMPLING_FIELD_MAP: list[tuple[str, str]] = [
    ("temperature", "temperature"),
    ("top_p", "top_p"),
    ("top_k", "top_k"),
    ("seed", "seed"),
    ("stop", "stop"),
    ("presence_penalty", "presence_penalty"),
    ("frequency_penalty", "frequency_penalty"),
]


def _translate_request_message(msg: dict) -> dict:
    """Adjust an OpenAI-shaped chat message for ``/api/chat``.

    Two role-specific tweaks; everything else passes through verbatim:

    1. ``assistant`` with ``tool_calls`` — OpenAI carries arguments as
       a JSON string and adds ``id`` / ``type`` per call. Ollama wants
       arguments as an object and ignores the surrounding metadata, so
       we parse and strip.
    2. ``tool`` — OpenAI uses ``tool_call_id`` to correlate the result
       with its triggering call. Ollama tracks correlation by message
       ordering, so the id is dropped. We forward ``name`` as
       ``tool_name`` (Ollama 0.5+ accepts it as a hint).
    """
    role = msg.get("role")
    if role == "assistant" and msg.get("tool_calls"):
        kept = {k: v for k, v in msg.items() if k != "tool_calls"}
        translated_tcs: list[dict] = []
        for tc in msg["tool_calls"] or []:
            fn = tc.get("function") or {}
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args) if args else {}
                except (ValueError, TypeError):
                    args = {}
            elif args is None:
                args = {}
            translated_tcs.append({
                "function": {
                    "name": fn.get("name", ""),
                    "arguments": args,
                },
            })
        kept["tool_calls"] = translated_tcs
        return kept
    if role == "tool":
        out: dict[str, Any] = {
            "role": "tool",
            "content": msg.get("content", ""),
        }
        if "name" in msg and msg["name"]:
            out["tool_name"] = msg["name"]
        return out
    return msg


def _to_ollama_request(payload: dict) -> dict:
    """Translate an OpenAI ChatCompletion request body into the shape
    Ollama's ``/api/chat`` expects.

    Field mapping:

    - ``model``, ``messages``, ``tools`` — passed through (messages get
      role-specific fixups via :func:`_translate_request_message`).
    - ``max_completion_tokens`` / ``max_tokens`` -> ``options.num_predict``
    - sampling knobs (temperature, top_p, top_k, seed, stop,
      presence_penalty, frequency_penalty) -> ``options.<same>``
    - ``response_format.type`` ``json``/``json_object`` -> ``format=json``
    - native top-level fields (``think``, ``keep_alive``) pass through
    - ``stream`` is forced ``false`` — the agent loop needs to inspect
      tool_calls between iterations; the public proxy layer reconstructs
      SSE for clients that asked for streaming.
    - any pre-set ``options`` block (e.g. ``options.num_ctx`` injected
      by ``run_agent_loop``) is preserved and merged with the mapped
      fields above (existing keys win — mapping uses ``setdefault``).
    - ``tool_choice`` is dropped: Ollama has no equivalent.
    """
    out: dict[str, Any] = {
        "model": payload.get("model"),
        "stream": False,
    }

    msgs = payload.get("messages") or []
    out["messages"] = [_translate_request_message(m) for m in msgs]

    if payload.get("tools"):
        out["tools"] = payload["tools"]

    options: dict[str, Any] = dict(payload.get("options") or {})
    if payload.get("max_completion_tokens") is not None:
        options.setdefault("num_predict", payload["max_completion_tokens"])
    elif payload.get("max_tokens") is not None:
        options.setdefault("num_predict", payload["max_tokens"])
    for src, dst in _SAMPLING_FIELD_MAP:
        if payload.get(src) is not None:
            options.setdefault(dst, payload[src])
    if options:
        out["options"] = options

    if "think" in payload:
        out["think"] = payload["think"]
    if "keep_alive" in payload:
        out["keep_alive"] = payload["keep_alive"]

    rf = payload.get("response_format")
    if isinstance(rf, dict) and rf.get("type") in ("json", "json_object"):
        out["format"] = "json"

    return out


# --- Response translation: Ollama /api/chat -> OpenAI --------------- #


def _to_openai_response(ollama_resp: dict) -> dict:
    """Reshape Ollama's ``/api/chat`` reply into an OpenAI ChatCompletion.

    Important shape differences:

    - Ollama returns a single ``message``; OpenAI wraps in ``choices[0]``.
    - Ollama tool_calls have only ``function.{name,arguments}`` with
      arguments as an object. OpenAI requires ``id`` (so subsequent tool
      messages can correlate via ``tool_call_id``), ``type=function``,
      and arguments as a JSON string. We synthesise an id per call.
    - Ollama's terminal signal is ``done_reason`` (``stop``/``length``).
      We map to OpenAI ``finish_reason``: ``tool_calls`` if any tool
      calls are present, otherwise the done_reason verbatim.
    - Token counts: ``prompt_eval_count`` -> ``prompt_tokens``,
      ``eval_count`` -> ``completion_tokens``.
    """
    msg = ollama_resp.get("message") or {}
    raw_tcs = msg.get("tool_calls") or []

    base_id = int(time.time() * 1000)
    translated_tcs: list[dict] = []
    for i, tc in enumerate(raw_tcs):
        fn = tc.get("function") or {}
        args = fn.get("arguments", {})
        if isinstance(args, dict):
            args = json.dumps(args, ensure_ascii=False)
        elif args is None:
            args = ""
        translated_tcs.append({
            "id": f"call_{base_id}_{i}",
            "type": "function",
            "function": {"name": fn.get("name", ""), "arguments": args},
        })

    done_reason = ollama_resp.get("done_reason") or "stop"
    finish_reason = "tool_calls" if translated_tcs else done_reason

    out_msg: dict[str, Any] = {
        "role": msg.get("role", "assistant"),
        "content": msg.get("content"),
    }
    if translated_tcs:
        out_msg["tool_calls"] = translated_tcs
    for k in ("thinking", "reasoning", "reasoning_content"):
        v = msg.get(k)
        if v:
            out_msg[k] = v

    prompt_tokens = ollama_resp.get("prompt_eval_count") or 0
    completion_tokens = ollama_resp.get("eval_count") or 0

    return {
        "id": f"chatcmpl-{base_id}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": ollama_resp.get("model", ""),
        "choices": [{
            "index": 0,
            "message": out_msg,
            "finish_reason": finish_reason,
        }],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


# --- Public entry point --------------------------------------------- #


def chat_completions(payload: dict[str, Any],
                     upstream: Optional[str] = None,
                     ) -> dict[str, Any]:
    """POST ``payload`` (OpenAI ChatCompletion shape) to Ollama's native
    ``/api/chat`` and return an OpenAI-shaped reply. Streaming is left
    to the caller — internally we always pass ``stream: false`` so the
    agent loop can inspect tool_calls between iterations and the public
    proxy layer rebuilds SSE for clients that want it.

    Raises :class:`UpstreamError` on non-2xx responses so callers don't
    silently produce empty replies.
    """
    base = _base_url(upstream)
    url = base + "/api/chat"
    client = _get_client()
    log.debug("ollama POST %s", url)

    ollama_payload = _to_ollama_request(payload)

    dump_dir = os.environ.get("CALIBER_GROUNDING_DUMP_DIR")
    if dump_dir:
        try:
            os.makedirs(dump_dir, exist_ok=True)
            ts = int(time.time() * 1000)
            with open(os.path.join(dump_dir, f"req-{ts}.json"),
                      "w", encoding="utf-8") as f:
                json.dump(
                    {"openai": payload, "ollama": ollama_payload},
                    f, indent=2, ensure_ascii=False,
                )
        except OSError:
            pass

    resp = client.post(
        url,
        json=ollama_payload,
        headers={"Content-Type": "application/json"},
    )
    if resp.status_code >= 400:
        try:
            body = resp.json()
        except json.JSONDecodeError:
            body = resp.text[:500]
        raise UpstreamError(resp.status_code, body)
    try:
        ollama_resp = resp.json()
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"upstream returned non-JSON ({resp.status_code}): "
            f"{resp.text[:200]}"
        ) from e

    return _to_openai_response(ollama_resp)


def close() -> None:
    """Close the pooled client. Called from server shutdown."""
    global _CLIENT
    if _CLIENT is not None:
        try:
            _CLIENT.close()
        except Exception:
            pass
        _CLIENT = None
