"""Tiny Ollama ``/api/chat`` wrapper for the advisor.

Speaks Ollama's native chat shape (not OpenAI compat) so we get
``prompt_eval_count`` / ``eval_count`` reliably and can pin ``num_ctx``
via the ``options`` block. Translates the response back into the
OpenAI-style ``choices[*].message`` shape the agent loop runner
expects.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Callable, Optional

log = logging.getLogger("claude_hooks.get_advice.chat_client")

DEFAULT_TIMEOUT_S = 600.0
# Ollama Cloud is *very* unreliable on /api/chat — empirically about
# 20% of calls return 5xx, and a non-trivial fraction return 400 with
# upstream parser errors like ``"Value looks like object, but can't
# find closing '}' symbol"`` even when the request body is verifiably
# valid JSON (the same body retried wins seconds later). Treat 408,
# 429, 4xx-with-known-transient-bodies, and all 5xx as retryable.
#
# Budget sizing — empirical Ollama Cloud flap windows on
# 2026-05-07 (synthesizer 500'd for >2 min on csl-2026-05-07-2158-7c75
# and burned the prior 8-attempt / 136 s budget). A /consultants
# session can take 5-15 min total, so paying up to ~15 min of retry
# budget is a good trade vs. failing the consultation outright.
#
# Sequence with these defaults (15 attempts, base 1.5 s, cap 90 s):
#   1.5  3  6  12  24  48  90 ... 90  (10 runs at the cap)
#   = 1.5 + 3 + 6 + 12 + 24 + 48 + 90*9 = 904.5 s  ≈ 15.1 min
DEFAULT_MAX_RETRIES = 15
DEFAULT_RETRY_BASE_DELAY_S = 1.5
DEFAULT_RETRY_MAX_DELAY_S = 90.0
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}
# 4xx response bodies that look like transient cloud parser/validator
# flaps rather than genuine "you sent bad data" errors. Substring
# match against the response body. Conservative — anything not on
# this list fails fast.
RETRYABLE_4XX_BODY_SUBSTRINGS = (
    "Value looks like object",       # cloud JSON validator hiccup
    "but can't find closing",        # same
    "unexpected end",                # truncated stream from upstream
    "Bad Gateway",                   # 4xx body wrapping a 502 upstream
)

# Substrings in a 4xx body that mean "this model doesn't accept the
# ``think`` / ``reasoning_effort`` field." Hit on a non-reasoning model
# (older qwen, llama2, gemma2 etc.) when the consultants config asks
# for a reasoning level. We strip the field, retry once, and remember
# the model so subsequent calls skip the field too. Keep substrings
# specific so we don't accidentally swallow legitimate validation
# errors.
THINK_UNSUPPORTED_4XX_BODY_SUBSTRINGS = (
    '"think"',                       # explicit JSON key in error
    "'think'",
    "reasoning_effort",
    "does not support thinking",
    "thinking is not supported",
    "unknown field",
    "unrecognized field",
)


def _ollama_messages(messages: list[dict]) -> list[dict]:
    """Translate OpenAI-shape messages into Ollama-native shape.

    The one transform that matters: OpenAI tool_calls carry
    ``function.arguments`` as a JSON-encoded **string**, while
    Ollama's /api/chat expects an **object**. Sending the string
    form makes Ollama Cloud return 400 with
    ``"Value looks like object, but can't find closing '}' symbol"``
    when it tries to read the message back from the conversation
    history on iteration 1+. Parse and re-emit as a dict.
    """
    out: list[dict] = []
    for m in messages:
        tcs = m.get("tool_calls")
        if not tcs:
            out.append(m)
            continue
        new_tcs = []
        for tc in tcs:
            fn = dict(tc.get("function") or {})
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    fn["arguments"] = json.loads(args)
                except json.JSONDecodeError:
                    # Empty / malformed — fall back to {}.
                    fn["arguments"] = {}
            elif args is None:
                fn["arguments"] = {}
            tc2 = dict(tc)
            tc2["function"] = fn
            new_tcs.append(tc2)
        m2 = dict(m)
        m2["tool_calls"] = new_tcs
        out.append(m2)
    return out


class ChatClient:
    def __init__(self, base_url: str, *, timeout_s: float = DEFAULT_TIMEOUT_S,
                 max_retries: int = DEFAULT_MAX_RETRIES,
                 retry_base_delay_s: float = DEFAULT_RETRY_BASE_DELAY_S,
                 retry_max_delay_s: float = DEFAULT_RETRY_MAX_DELAY_S):
        self.base_url = base_url.rstrip("/")
        if self.base_url.endswith("/v1"):
            self.base_url = self.base_url[: -len("/v1")]
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.retry_base_delay_s = retry_base_delay_s
        self.retry_max_delay_s = retry_max_delay_s
        # Cumulative usage across calls in this client's lifetime.
        # Caller resets per-session as needed.
        self.last_usage: dict[str, int] = {
            "prompt_eval_count": 0,
            "eval_count": 0,
        }
        # Per-instance memo of model tags that 400'd on the ``think``
        # field. We strip ``think`` / ``reasoning_effort`` from
        # subsequent calls to that model so non-reasoning models
        # (older qwen, llama2, gemma2 …) don't pay the round-trip
        # cost of repeatedly being told no. Populated proactively
        # via ``_probe_supports_think`` (one ``/api/show`` call per
        # model) and reactively from 400 responses.
        self._unsupported_think: set[str] = set()
        # Models we already probed via /api/show — short-circuits the
        # second call. ``None`` value = probe is unknown / failed and
        # we should fall back to the reactive (400-based) path.
        self._probed_think: dict[str, Optional[bool]] = {}

    def _probe_supports_think(self, model: str) -> Optional[bool]:
        """Ask ``/api/show`` whether ``model`` advertises the
        ``thinking`` capability. Returns ``True`` / ``False`` / ``None``
        (unknown — endpoint failed or didn't return capabilities).
        Cached for the life of the ChatClient so each model is
        probed at most once per role.
        """
        if model in self._probed_think:
            return self._probed_think[model]
        url = f"{self.base_url}/api/show"
        encoded = json.dumps({"model": model}).encode()
        req = urllib.request.Request(
            url, data=encoded, method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10.0) as resp:
                data = json.loads(resp.read())
        except (urllib.error.URLError, urllib.error.HTTPError, OSError,
                json.JSONDecodeError) as e:
            log.info(
                "ollama show: probe failed for %s (%s) — relying on "
                "reactive 400 fallback for think support",
                model, e,
            )
            self._probed_think[model] = None
            return None
        # /api/show returns ``{"capabilities": ["completion", "tools",
        # "thinking", "vision", ...]}`` on supported builds. Some
        # cloud builds omit the field entirely; treat that as unknown.
        caps = data.get("capabilities")
        if not isinstance(caps, list):
            self._probed_think[model] = None
            return None
        supports = "thinking" in caps
        self._probed_think[model] = supports
        if not supports:
            self._unsupported_think.add(model)
            log.info(
                "ollama show: model %s does not advertise 'thinking' "
                "capability (caps=%s); will skip 'think' field",
                model, caps,
            )
        return supports

    def chat(self, payload: dict) -> dict:
        """POST /api/chat with the supplied (OpenAI-shape) payload.
        Translates the Ollama response back into OpenAI shape so the
        agent-loop runner can consume it unchanged. Retries on
        retryable 5xx with exponential backoff.

        Graceful ``think`` degrade: if the model returned 400 on a
        prior call with a body that names ``think`` /
        ``reasoning_effort`` / "unknown field", strip those fields up
        front so we don't waste a round-trip. If the field is sent
        anyway and we get a fresh 400 of that shape, strip + retry
        immediately (no retry-budget cost) and remember the model.
        """
        # Decide whether to forward ``think``. Three layers, cheapest
        # first: (1) prior reactive fallback already marked this
        # model unsupported -> strip; (2) proactive ``/api/show``
        # probe says ``thinking`` is not in capabilities -> strip;
        # (3) probe says yes or is unknown -> send and rely on the
        # reactive 400 path as a safety net.
        model_tag = (payload or {}).get("model") or ""
        strip_think = model_tag in self._unsupported_think
        if (not strip_think
                and "think" in (payload or {})
                and model_tag
                and model_tag not in self._probed_think):
            probe = self._probe_supports_think(model_tag)
            if probe is False:
                strip_think = True

        body = self._to_ollama(payload, strip_think=strip_think)
        url = f"{self.base_url}/api/chat"
        encoded = json.dumps(body).encode()

        last_exc: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            req = urllib.request.Request(
                url,
                data=encoded,
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                    data = json.loads(resp.read())
                if attempt > 0:
                    log.info(
                        "ollama chat: succeeded on retry %d/%d",
                        attempt, self.max_retries,
                    )
                return self._from_ollama(data)
            except urllib.error.HTTPError as e:
                last_exc = e
                # Read the body once — it carries the actual error
                # detail from Ollama / cloud upstream (e.g. "context
                # length exceeded", "model not loaded"). The default
                # urllib repr only says "HTTP Error N: Reason".
                try:
                    err_body = e.read().decode(errors="replace")[:500]
                except Exception:
                    err_body = "<unreadable>"
                # First-class: model rejected ``think``. Strip it,
                # remember the model, retry once outside the normal
                # retry budget so a stale config doesn't burn 9 round
                # trips. Only triggers when we DID send think on this
                # call.
                think_rejected = (
                    e.code in (400, 422)
                    and "think" in body
                    and any(s in err_body
                            for s in THINK_UNSUPPORTED_4XX_BODY_SUBSTRINGS)
                )
                if think_rejected:
                    log.warning(
                        "ollama chat: model %s rejected 'think' field "
                        "(HTTP %d). Stripping and retrying once. "
                        "Future calls to this model will skip the "
                        "field. Body: %s",
                        model_tag, e.code, err_body,
                    )
                    self._unsupported_think.add(model_tag)
                    body = self._to_ollama(payload, strip_think=True)
                    encoded = json.dumps(body).encode()
                    # Don't count this against retry budget; loop
                    # back into the same attempt index.
                    continue

                retryable = (
                    e.code in RETRYABLE_STATUS
                    or (400 <= e.code < 500
                        and any(s in err_body
                                for s in RETRYABLE_4XX_BODY_SUBSTRINGS))
                )
                if retryable and attempt < self.max_retries:
                    delay = min(
                        self.retry_base_delay_s * (2 ** attempt),
                        self.retry_max_delay_s,
                    )
                    log.warning(
                        "ollama chat: HTTP %d on attempt %d/%d, "
                        "retrying in %.1fs (body: %s)",
                        e.code, attempt + 1, self.max_retries + 1, delay,
                        err_body,
                    )
                    time.sleep(delay)
                    continue
                log.error("ollama chat: HTTP %d (giving up) body=%s",
                          e.code, err_body)
                raise RuntimeError(
                    f"ollama chat HTTP {e.code}: {err_body}"
                ) from e
            except (urllib.error.URLError, OSError) as e:
                last_exc = e
                if attempt < self.max_retries:
                    delay = min(
                        self.retry_base_delay_s * (2 ** attempt),
                        self.retry_max_delay_s,
                    )
                    log.warning(
                        "ollama chat: %s on attempt %d/%d, retrying in %.1fs",
                        e, attempt + 1, self.max_retries + 1, delay,
                    )
                    time.sleep(delay)
                    continue
                log.error("ollama chat: %s (giving up)", e)
                raise
        # Shouldn't reach here — the loop either returns or raises.
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("ollama chat: no attempts made")

    # ---- streaming variant ---------------------------------------- #

    def chat_streamed(self, payload: dict,
                      *,
                      on_token: Optional[Callable[[str], None]] = None,
                      cancel_check: Optional[Callable[[], bool]] = None,
                      ) -> dict:
        """Streaming counterpart to ``chat()``.

        POSTs to ``/api/chat`` with ``stream=true``, reads the NDJSON
        response line by line, calls ``on_token(text_delta)`` for each
        non-empty content chunk, and on ``done=true`` returns the same
        OpenAI-shape dict ``chat()`` would have returned for the same
        payload — so the agent-loop runner and tracing layers stay
        unchanged.

        ``cancel_check``: callable returning True when the caller
        wants the call aborted (e.g. ``StallController.is_cancelled``).
        Checked between NDJSON lines so cancellation is cooperative
        rather than rude (the orchestrator just stops waiting for
        further tokens). When cancelled mid-stream this method raises
        ``CancelledByOrchestrator`` — the caller catches it and
        retries with a fresh controller.

        Retry policy matches :py:meth:`chat`: transient 5xx /
        retryable 4xx-bodies / URL errors restart the streaming
        request from scratch (any partial output discarded) with
        exponential backoff up to ``max_retries`` attempts. Mid-
        stream stalls are NOT this method's responsibility — that's
        the orchestrator's job, observing token cadence through
        ``on_token``.
        """
        # Lazy import to avoid coupling get_advice -> consultants.
        from consultants.engine.stall import CancelledByOrchestrator

        # Same think-stripping path as chat().
        model_tag = (payload or {}).get("model") or ""
        strip_think = model_tag in self._unsupported_think
        if (not strip_think
                and "think" in (payload or {})
                and model_tag
                and model_tag not in self._probed_think):
            probe = self._probe_supports_think(model_tag)
            if probe is False:
                strip_think = True

        body = self._to_ollama(payload, strip_think=strip_think)
        body["stream"] = True       # the one bit that actually changes
        url = f"{self.base_url}/api/chat"
        encoded = json.dumps(body).encode()

        last_exc: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            req = urllib.request.Request(
                url, data=encoded, method="POST",
                headers={"Content-Type": "application/json",
                         "Accept": "application/x-ndjson"},
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                    final = self._consume_ndjson(
                        resp, on_token=on_token,
                        cancel_check=cancel_check,
                    )
                if attempt > 0:
                    log.info(
                        "ollama chat_streamed: succeeded on retry %d/%d",
                        attempt, self.max_retries,
                    )
                return self._from_ollama(final)
            except CancelledByOrchestrator:
                # Cooperative abort — propagate without retrying. The
                # orchestrator owns the retry decision.
                raise
            except urllib.error.HTTPError as e:
                last_exc = e
                try:
                    err_body = e.read().decode(errors="replace")[:500]
                except Exception:
                    err_body = "<unreadable>"
                think_rejected = (
                    e.code in (400, 422)
                    and "think" in body
                    and any(s in err_body
                            for s in THINK_UNSUPPORTED_4XX_BODY_SUBSTRINGS)
                )
                if think_rejected:
                    log.warning(
                        "ollama chat_streamed: model %s rejected 'think' "
                        "field (HTTP %d). Stripping and retrying once.",
                        model_tag, e.code,
                    )
                    self._unsupported_think.add(model_tag)
                    body = self._to_ollama(payload, strip_think=True)
                    body["stream"] = True
                    encoded = json.dumps(body).encode()
                    continue
                retryable = (
                    e.code in RETRYABLE_STATUS
                    or (400 <= e.code < 500
                        and any(s in err_body
                                for s in RETRYABLE_4XX_BODY_SUBSTRINGS))
                )
                if retryable and attempt < self.max_retries:
                    delay = min(
                        self.retry_base_delay_s * (2 ** attempt),
                        self.retry_max_delay_s,
                    )
                    log.warning(
                        "ollama chat_streamed: HTTP %d on attempt %d/%d, "
                        "retrying in %.1fs (body: %s)",
                        e.code, attempt + 1, self.max_retries + 1, delay,
                        err_body,
                    )
                    time.sleep(delay)
                    continue
                log.error(
                    "ollama chat_streamed: HTTP %d (giving up) body=%s",
                    e.code, err_body,
                )
                raise RuntimeError(
                    f"ollama chat_streamed HTTP {e.code}: {err_body}"
                ) from e
            except (urllib.error.URLError, OSError) as e:
                last_exc = e
                if attempt < self.max_retries:
                    delay = min(
                        self.retry_base_delay_s * (2 ** attempt),
                        self.retry_max_delay_s,
                    )
                    log.warning(
                        "ollama chat_streamed: %s on attempt %d/%d, "
                        "retrying in %.1fs",
                        e, attempt + 1, self.max_retries + 1, delay,
                    )
                    time.sleep(delay)
                    continue
                log.error("ollama chat_streamed: %s (giving up)", e)
                raise
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("ollama chat_streamed: no attempts made")

    def _consume_ndjson(self, resp,
                        *,
                        on_token: Optional[Callable[[str], None]],
                        cancel_check: Optional[Callable[[], bool]],
                        ) -> dict:
        """Read Ollama NDJSON stream off ``resp`` and assemble the
        final response dict.

        Aggregates ``message.content`` deltas into a single string,
        captures any ``tool_calls`` that arrive (typically only on
        the final ``done=true`` line, but some models emit them
        progressively), and returns the merged record with the
        ``done=true`` fields (prompt_eval_count, eval_count) intact.

        Raises :class:`CancelledByOrchestrator` if the cancel_check
        returns True between lines.
        """
        from consultants.engine.stall import CancelledByOrchestrator

        content_parts: list[str] = []
        tool_calls: list[dict] = []
        last_msg_extra: dict = {}
        final_fields: dict = {}
        role = "assistant"

        for raw_line in resp:
            if cancel_check is not None and cancel_check():
                raise CancelledByOrchestrator()
            if not raw_line:
                continue
            line = (raw_line.decode("utf-8", errors="replace")
                    if isinstance(raw_line, bytes) else raw_line)
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                log.warning("ollama stream: skipping malformed line: %r",
                            line[:80])
                continue
            msg = obj.get("message") or {}
            if msg:
                if "role" in msg:
                    role = msg["role"] or role
                delta = msg.get("content") or ""
                if delta:
                    content_parts.append(delta)
                    if on_token is not None:
                        try:
                            on_token(delta)
                        except Exception:  # pragma: no cover
                            log.exception("on_token raised; ignored")
                tcs = msg.get("tool_calls")
                if tcs:
                    tool_calls.extend(tcs)
                # Carry forward any extra fields the model emitted
                # (e.g. thinking content).
                for k, v in msg.items():
                    if k not in ("role", "content", "tool_calls"):
                        last_msg_extra[k] = v
            # Final record fields land on done=true.
            if obj.get("done"):
                for k in ("prompt_eval_count", "eval_count",
                         "total_duration", "load_duration",
                         "prompt_eval_duration", "eval_duration",
                         "done_reason"):
                    if k in obj:
                        final_fields[k] = obj[k]

        merged_message: dict = {"role": role,
                                "content": "".join(content_parts)}
        if tool_calls:
            merged_message["tool_calls"] = tool_calls
        merged_message.update(last_msg_extra)

        result = {"message": merged_message}
        result.update(final_fields)
        return result

    def _to_ollama(self, payload: dict, *, strip_think: bool = False) -> dict:
        body: dict = {
            "model": payload.get("model"),
            "messages": _ollama_messages(payload.get("messages") or []),
            "stream": False,
        }
        opts = dict(payload.get("options") or {})
        if opts:
            body["options"] = opts
        if "tools" in payload and payload["tools"]:
            body["tools"] = payload["tools"]
        if "tool_choice" in payload:
            body["tool_choice"] = payload["tool_choice"]
        # think is accepted natively by Ollama. Strip when the model
        # is known to reject it (graceful degrade for non-reasoning
        # models). Accepts bool or "low" | "medium" | "high".
        if "think" in payload and not strip_think:
            body["think"] = payload["think"]
        return body

    def _from_ollama(self, data: dict) -> dict:
        msg = dict(data.get("message") or {})
        # Ollama may return tool_calls with arguments as dict; runner
        # is fine with both, but normalize to JSON strings to match
        # the OpenAI wire shape. Also strip non-standard fields some
        # Ollama Cloud models emit (notably ``function.index`` from
        # qwen3.5/deepseek cloud builds — that field belongs at the
        # tool_call level in OpenAI spec, not nested in ``function``,
        # and echoing it back on the next turn causes the upstream
        # JSON validator to 400 with "Value looks like object, but
        # can't find closing '}' symbol"). Whitelist the function
        # subobject to {name, arguments} to be safe.
        tool_calls = msg.get("tool_calls") or []
        if tool_calls:
            normalized = []
            for tc in tool_calls:
                raw_fn = tc.get("function") or {}
                clean_fn: dict = {}
                if "name" in raw_fn:
                    clean_fn["name"] = raw_fn["name"]
                args = raw_fn.get("arguments")
                if isinstance(args, dict):
                    clean_fn["arguments"] = json.dumps(args)
                elif args is not None:
                    clean_fn["arguments"] = args
                tc2: dict = {}
                # Whitelist tool_call-level fields too.
                if tc.get("id"):
                    tc2["id"] = tc["id"]
                else:
                    tc2["id"] = f"tc_{len(normalized)}"
                tc2["type"] = tc.get("type") or "function"
                # ``index`` at the tool_call level IS standard (used
                # in streaming deltas); keep it if present.
                if "index" in tc:
                    tc2["index"] = tc["index"]
                tc2["function"] = clean_fn
                normalized.append(tc2)
            msg["tool_calls"] = normalized
        finish_reason = "tool_calls" if tool_calls else "stop"
        # Track per-call usage so the caller can read it without
        # re-walking the response.
        prompt_tokens = int(data.get("prompt_eval_count") or 0)
        completion_tokens = int(data.get("eval_count") or 0)
        self.last_usage = {
            "prompt_eval_count": prompt_tokens,
            "eval_count": completion_tokens,
        }
        return {
            "choices": [{
                "message": msg,
                "finish_reason": finish_reason,
            }],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }


# --------------------------------------------------------------------- #
# v1.5+: llamafile-backed agent ChatClient
# --------------------------------------------------------------------- #

class LlamafileAgentChatClient:
    """``chat(payload) -> dict`` for llamafile-backed agent loops.

    Same interface as :class:`ChatClient` but speaks OpenAI
    ``/v1/chat/completions`` directly on the daemon-resolved port. No
    translation layer because llamafile is already OpenAI-shape
    inbound; the response is also OpenAI-shape so the agent-loop
    runner consumes it unchanged.

    Cold-start: each request resolves the port via
    ``daemon_client.chat_model_ensure(label)``; the port is cached for
    ``port_cache_ttl`` seconds so steady traffic skips the RPC.
    On connection refusal mid-call (the daemon may have idle-reaped
    between the last ensure and this POST), the cache is invalidated,
    we re-ensure once, and retry.
    """

    def __init__(
        self, label: str, *,
        host: str = "127.0.0.1",
        port_cache_ttl: float = 60.0,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_base_delay_s: float = DEFAULT_RETRY_BASE_DELAY_S,
        retry_max_delay_s: float = DEFAULT_RETRY_MAX_DELAY_S,
    ):
        if not label:
            raise ValueError("LlamafileAgentChatClient requires a label")
        self.label = label
        self.host = host
        self.port_cache_ttl = port_cache_ttl
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.retry_base_delay_s = retry_base_delay_s
        self.retry_max_delay_s = retry_max_delay_s
        self.last_usage: dict[str, int] = {
            "prompt_eval_count": 0,
            "eval_count": 0,
        }
        self._cached_port: Optional[int] = None
        self._cached_at: float = 0.0

    # --- daemon-ensure / port resolution ---

    def _resolve_port(self, *, force_refresh: bool = False) -> int:
        now = time.monotonic()
        if (not force_refresh
                and self._cached_port is not None
                and (now - self._cached_at) < self.port_cache_ttl):
            return self._cached_port
        import importlib
        dc = importlib.import_module("claude_hooks.daemon_client")
        try:
            resp = dc.chat_model_ensure(self.label, timeout=120.0)
        except Exception as e:
            raise RuntimeError(
                f"daemon RPC chat_model_ensure({self.label!r}) failed: {e}"
            ) from e
        if resp is None:
            raise RuntimeError(
                f"daemon unreachable; cannot resolve "
                f"llamafile://{self.label}"
            )
        if not resp.get("ready") or "port" not in resp:
            raise RuntimeError(
                f"daemon refused to bring up llamafile://{self.label}: "
                f"{resp.get('reason', 'unknown')}"
            )
        self._cached_port = int(resp["port"])
        self._cached_at = now
        return self._cached_port

    # --- chat ---

    def chat(self, payload: dict) -> dict:
        """POST OpenAI-shape payload to /v1/chat/completions on the
        resolved port. Retries on 5xx with exponential backoff; the
        retry budget matches :class:`ChatClient` so cross-backend
        timing stays predictable.
        """
        body = self._to_openai(payload)
        encoded = json.dumps(body).encode()

        port = self._resolve_port()
        url = f"http://{self.host}:{port}/v1/chat/completions"

        last_exc: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            req = urllib.request.Request(
                url, data=encoded, method="POST",
                headers={"Content-Type": "application/json",
                         "Connection": "close"},
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                    data = json.loads(resp.read())
                if attempt > 0:
                    log.info(
                        "llamafile chat: succeeded on retry %d/%d",
                        attempt, self.max_retries,
                    )
                return self._from_openai(data)
            except urllib.error.HTTPError as e:
                last_exc = e
                try:
                    err_body = e.read().decode(errors="replace")[:500]
                except Exception:
                    err_body = "<unreadable>"
                if e.code in RETRYABLE_STATUS and attempt < self.max_retries:
                    delay = min(
                        self.retry_base_delay_s * (2 ** attempt),
                        self.retry_max_delay_s,
                    )
                    log.warning(
                        "llamafile chat: HTTP %d on attempt %d/%d, "
                        "retrying in %.1fs (body: %s)",
                        e.code, attempt + 1, self.max_retries + 1, delay,
                        err_body,
                    )
                    time.sleep(delay)
                    continue
                log.error("llamafile chat: HTTP %d (giving up) body=%s",
                          e.code, err_body)
                raise RuntimeError(
                    f"llamafile chat HTTP {e.code}: {err_body}"
                ) from e
            except (urllib.error.URLError, OSError) as e:
                last_exc = e
                # First refusal: invalidate cache, re-ensure, retry
                # this attempt without consuming the retry budget.
                if attempt == 0:
                    log.info(
                        "llamafile chat: connection failure on first "
                        "attempt (%s); invalidating port cache + "
                        "re-ensuring", e,
                    )
                    try:
                        port = self._resolve_port(force_refresh=True)
                        url = f"http://{self.host}:{port}/v1/chat/completions"
                    except Exception as re_err:
                        # Fall through to backoff with the original exc.
                        log.warning("llamafile re-ensure failed: %s", re_err)
                if attempt < self.max_retries:
                    delay = min(
                        self.retry_base_delay_s * (2 ** attempt),
                        self.retry_max_delay_s,
                    )
                    log.warning(
                        "llamafile chat: %s on attempt %d/%d, retrying in %.1fs",
                        e, attempt + 1, self.max_retries + 1, delay,
                    )
                    time.sleep(delay)
                    continue
                log.error("llamafile chat: %s (giving up)", e)
                raise
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("llamafile chat: no attempts made")

    def _to_openai(self, payload: dict) -> dict:
        """Pass OpenAI-shape payload through largely unchanged.

        llamafile / llama.cpp server natively accepts the OpenAI
        ``messages`` + ``tools`` + ``tool_choice`` shape. The only
        normalization needed is converting Ollama's
        ``options.num_predict`` -> ``max_tokens`` so configs that
        target both backends interchangeably work.
        """
        body: dict = {
            "model": payload.get("model") or self.label,
            "messages": list(payload.get("messages") or []),
            "stream": False,
        }
        opts = dict(payload.get("options") or {})
        if "num_predict" in opts:
            body["max_tokens"] = int(opts["num_predict"])
        if "temperature" in opts:
            body["temperature"] = opts["temperature"]
        if "tools" in payload and payload["tools"]:
            body["tools"] = payload["tools"]
        if "tool_choice" in payload:
            body["tool_choice"] = payload["tool_choice"]
        # llama.cpp 0.10.1 doesn't honor a top-level ``think``; reasoning
        # is governed by the GGUF + sampling params. We accept the field
        # so the runner doesn't have to special-case backend, then
        # silently drop it.
        return body

    def _from_openai(self, data: dict) -> dict:
        """Pass OpenAI-shape response through, recording usage.

        The agent-loop runner expects ``choices[0].message`` +
        optional ``choices[0].finish_reason`` + ``usage`` — all of
        which llamafile emits natively. The only transform: copy
        ``usage.prompt_tokens`` / ``completion_tokens`` into
        ``self.last_usage`` under Ollama's field names so the get-advice
        / consultants accounting code (which reads
        ``prompt_eval_count`` / ``eval_count``) works without a
        per-backend branch.
        """
        usage = data.get("usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        self.last_usage = {
            "prompt_eval_count": prompt_tokens,
            "eval_count": completion_tokens,
        }
        # llamafile returns the OpenAI shape verbatim — just return it.
        return data


# --------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------- #

def make_agent_chat_client(
    model_ref: str,
    ollama_base_url: str,
    **kwargs,
):
    """Pick an agent-loop-capable ChatClient for ``model_ref``.

    - ``llamafile://<label>`` -> :class:`LlamafileAgentChatClient`
      (daemon-ensured, OpenAI ``/v1/chat/completions``).
    - Anything else -> existing :class:`ChatClient` (Ollama native
      ``/api/chat``).

    Both expose the same ``chat(payload) -> dict`` + ``last_usage``
    interface so the agent loop and tracing layers are unchanged.
    """
    if model_ref and model_ref.startswith("llamafile://"):
        label = model_ref[len("llamafile://"):]
        return LlamafileAgentChatClient(label, **kwargs)
    return ChatClient(ollama_base_url, **kwargs)
