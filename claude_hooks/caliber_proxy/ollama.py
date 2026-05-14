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

from claude_hooks._chat_retry import (
    ProxyRetryConfig,
    compute_backoff,
    counters,
    is_retryable_empty_response,
    is_retryable_status,
)

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


def default_upstream_backend() -> str:
    """Configured upstream backend dialect.

    - ``ollama`` (default, v1.4 and earlier behaviour): the upstream
      speaks Ollama's native ``/api/chat`` and the proxy translates
      between OpenAI ChatCompletion inbound and Ollama outbound.
    - ``openai_compat`` (v1.5+): the upstream is already OpenAI-shape
      (llamafile, LM-Studio, vLLM, etc.). Skip translation — POST the
      OpenAI payload directly to ``<upstream>/v1/chat/completions``
      and return the response verbatim. Retry budget + empty-content
      detection still apply.
    """
    val = os.environ.get(
        "CALIBER_GROUNDING_UPSTREAM_BACKEND", "ollama",
    ).strip().lower()
    if val not in ("ollama", "openai_compat"):
        log.warning(
            "CALIBER_GROUNDING_UPSTREAM_BACKEND=%r is not recognised; "
            "valid values are 'ollama' or 'openai_compat'. Falling back "
            "to 'ollama'.", val,
        )
        return "ollama"
    return val


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

# Tool-call fields known to break specific upstreams when echoed back
# in a request. Provider-evidence-based; expand only when a real
# upstream returns a 400 on the echoed field. Default behaviour is
# pass-through (see :func:`_translate_request_message` rationale).
#
# - ``function.index`` — emitted by deepseek/qwen3.5 cloud builds in
#   their *responses* (see ``get_advice/chat_client.py:_from_ollama``);
#   echoing it back in a *request* causes upstream JSON validators to
#   400 with "Value looks like object, but can't find closing '}'
#   symbol". The field is response-only.
_TOOL_CALL_DENY_OUTBOUND_FUNCTION: frozenset[str] = frozenset({"index"})

# tool_call-level (i.e. ``tool_calls[i].<field>``, not nested under
# ``function``). Empty for now — OpenAI-standard keys (``id``,
# ``type``, ``index``) are documented and accepted by Ollama 0.5+.
_TOOL_CALL_DENY_OUTBOUND_TOPLEVEL: frozenset[str] = frozenset()


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
       a JSON string while Ollama wants an object; only that field is
       transformed. **All other fields on the tool_call AND on its
       nested ``function`` are passed through verbatim**, except for
       a small denylist of fields known to break specific upstreams
       when echoed back (see :data:`_TOOL_CALL_DENY_OUTBOUND`).

       Why generic passthrough: providers attach required-on-echo
       metadata under various keys (Gemini's ``thought_signature`` on
       the function part, OpenAI's ``id``, future provenance fields).
       An explicit allowlist of {name, arguments} broke Gemini-cloud
       with 400 ``"Function call is missing a thought_signature in
       functionCall parts"``. Default-pass + targeted strip only
       what we have evidence of breakage for.
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
            fn = dict(tc.get("function") or {})
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args) if args else {}
                except (ValueError, TypeError):
                    args = {}
            elif args is None:
                args = {}
            fn["arguments"] = args
            # Strip only known-bad nested function fields (see denylist).
            for k in _TOOL_CALL_DENY_OUTBOUND_FUNCTION:
                fn.pop(k, None)
            new_tc = {k: v for k, v in tc.items() if k != "function"}
            for k in _TOOL_CALL_DENY_OUTBOUND_TOPLEVEL:
                new_tc.pop(k, None)
            new_tc["function"] = fn
            translated_tcs.append(new_tc)
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
        fn = dict(tc.get("function") or {})
        args = fn.get("arguments", {})
        if isinstance(args, dict):
            args = json.dumps(args, ensure_ascii=False)
        elif args is None:
            args = ""
        fn["arguments"] = args
        # Pass through any provider-specific function-level metadata
        # (e.g. Gemini's ``thought_signature``) so caliber/the agent
        # loop can echo it back next turn — required by some
        # upstreams (Gemini 400s without it).
        new_tc: dict[str, Any] = {k: v for k, v in tc.items() if k != "function"}
        new_tc["function"] = fn
        # Always synthesise an OpenAI-mandatory ``id`` if upstream didn't
        # provide one (Ollama-native often doesn't); preserve when present.
        new_tc.setdefault("id", f"call_{base_id}_{i}")
        new_tc.setdefault("type", "function")
        translated_tcs.append(new_tc)

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

    Raises :class:`UpstreamError` on non-2xx responses (after retries)
    so callers don't silently produce empty replies.

    **Cloud resilience** (2026-05-09): rides upstream Ollama 5xx /
    transient 4xx / 200-empty-content blips with the same retry
    budget the consultants engine uses (15 attempts / ~15 min by
    default). Tunable via ``CALIBER_GROUNDING_RETRY_*`` env vars.
    See ``claude_hooks/_chat_retry.py`` for the policy module and
    ``docs/PLAN-caliber-proxy-cloud-resilience.md`` for design notes.
    Per-process flap counters expose via ``server.py``'s ``/health``.
    """
    base = _base_url(upstream)
    backend = default_upstream_backend()
    if backend == "openai_compat":
        # v1.5+: upstream is already OpenAI-shape (llamafile, vLLM,
        # LM-Studio). Skip request/response translation; pass the
        # payload through verbatim (with ``stream:false`` enforced).
        url = base + "/v1/chat/completions"
        ollama_payload = dict(payload)
        ollama_payload["stream"] = False
    else:
        url = base + "/api/chat"
        ollama_payload = _to_ollama_request(payload)
    client = _get_client()
    log.debug("caliber POST %s (backend=%s)", url, backend)

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

    cfg = ProxyRetryConfig()
    cnt = counters()

    # Two parallel retry budgets:
    #  - status_attempt: counts toward ``cfg.max_attempts``, advances on
    #    every retryable HTTP failure. Backoff uses this index.
    #  - empty_attempt: separate small budget for 200-empty soft fails;
    #    doesn't count against the main budget but its own cap (~5)
    #    avoids spinning on a model that legitimately produced empty.
    status_attempt = 0
    empty_attempt = 0
    last_error: Optional[UpstreamError] = None

    while True:
        try:
            resp = client.post(
                url,
                json=ollama_payload,
                headers={"Content-Type": "application/json"},
            )
        except (httpx.RequestError, httpx.HTTPError) as e:
            # Network-level failure (connection reset, DNS, etc.) —
            # treat as 5xx-equivalent. Retryable.
            if cfg.disabled() or status_attempt >= cfg.max_attempts:
                cnt.upstream_retry_exhausted_total += 1
                raise UpstreamError(
                    599, f"network error: {type(e).__name__}: {e}"[:500]
                ) from e
            cnt.upstream_5xx_total += 1
            delay = compute_backoff(
                status_attempt, cfg.base_delay_s, cfg.max_delay_s,
            )
            log.warning(
                "ollama POST: network %s on attempt %d/%d, retrying in %.1fs",
                type(e).__name__, status_attempt + 1, cfg.max_attempts + 1,
                delay,
            )
            time.sleep(delay)
            status_attempt += 1
            continue

        if resp.status_code >= 400:
            try:
                body_obj = resp.json()
                body_text = json.dumps(body_obj)[:500]
            except json.JSONDecodeError:
                body_obj = resp.text[:500]
                body_text = resp.text[:500]

            retryable = is_retryable_status(resp.status_code, body_text)
            if (not retryable
                    or cfg.disabled()
                    or status_attempt >= cfg.max_attempts):
                if retryable:
                    cnt.upstream_retry_exhausted_total += 1
                last_error = UpstreamError(resp.status_code, body_obj)
                raise last_error

            # Track which class of flap this was.
            if resp.status_code in (500, 502, 503, 504, 408, 429):
                cnt.upstream_5xx_total += 1
            else:
                cnt.upstream_retryable_4xx_total += 1

            delay = compute_backoff(
                status_attempt, cfg.base_delay_s, cfg.max_delay_s,
            )
            log.warning(
                "ollama POST: HTTP %d on attempt %d/%d, retrying in %.1fs "
                "(body: %s)",
                resp.status_code, status_attempt + 1, cfg.max_attempts + 1,
                delay, body_text,
            )
            time.sleep(delay)
            status_attempt += 1
            continue

        # 2xx path. Parse JSON, then check for the 200-empty soft fail.
        try:
            ollama_resp = resp.json()
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"upstream returned non-JSON ({resp.status_code}): "
                f"{resp.text[:200]}"
            ) from e

        # openai_compat upstream returns OpenAI shape directly — no
        # translation needed. ``ollama`` upstream needs the
        # ``message{content,tool_calls}`` -> ``choices[].message``
        # transform applied by ``_to_openai_response``.
        if backend == "openai_compat":
            translated = ollama_resp
        else:
            translated = _to_openai_response(ollama_resp)

        if cfg.retry_on_empty and is_retryable_empty_response(translated):
            if empty_attempt < cfg.empty_max:
                cnt.upstream_empty_total += 1
                delay = compute_backoff(
                    empty_attempt, cfg.base_delay_s, cfg.max_delay_s,
                )
                log.warning(
                    "ollama POST: 200 OK with empty content on empty-attempt "
                    "%d/%d, retrying in %.1fs (model=%s)",
                    empty_attempt + 1, cfg.empty_max,
                    delay, ollama_payload.get("model", "?"),
                )
                time.sleep(delay)
                empty_attempt += 1
                continue
            # Exhausted empty-retry budget; ship the empty response so
            # the agent loop can decide what to do (force_first retry
            # with corrective user-msg, etc.). Don't raise — empty is
            # a wire-valid 200, not an HTTP error.
            log.warning(
                "ollama POST: empty-content retries exhausted (%d), "
                "returning empty response", cfg.empty_max,
            )

        if status_attempt > 0 or empty_attempt > 0:
            log.info(
                "ollama POST: succeeded after %d HTTP retries + %d empty "
                "retries (model=%s)",
                status_attempt, empty_attempt,
                ollama_payload.get("model", "?"),
            )
            cnt.upstream_retry_succeeded_total += 1

        return translated


def close() -> None:
    """Close the pooled client. Called from server shutdown."""
    global _CLIENT
    if _CLIENT is not None:
        try:
            _CLIENT.close()
        except Exception:
            pass
        _CLIENT = None
