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
import re
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Optional
from claude_hooks import ollama_slots

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

# Bodies of a relay's 502/503/504 that mean the relay could not reach
# the cloud at all — a dead WAN line, not an overloaded model. eleven2go
# answered 2026-09-23's outages with ``dial tcp: lookup ollama.com: no
# such host``.
OUTAGE_BODY_MARKERS = (
    "no such host", "dial tcp", "connection refused",
    "network is unreachable", "no route to host", "i/o timeout",
    "connection reset", "temporary failure in name resolution",
    "name or service not known", "server misbehaving",
)
OUTAGE_MAX_DELAY_S = 60.0


def is_outage(exc: BaseException, body: str = "") -> bool:
    """A failure to reach the model at all: the connection could not be
    made, was refused or reset, or a relay says it could not reach the
    cloud. A timeout *reading* a response is not one — that is a slow
    model, and the ordinary retry budget applies."""
    if isinstance(exc, urllib.error.HTTPError):
        return (exc.code in (502, 503, 504)
                and any(m in (body or "").lower() for m in OUTAGE_BODY_MARKERS))
    if isinstance(exc, urllib.error.URLError):
        return True  # raised before a response: connect / DNS phase
    return isinstance(exc, (ConnectionError, BrokenPipeError))


#: Fault classes a failed call is tagged with (``ChatFault.fault_class``).
#: The class decides what a caller does next, so it is named rather than
#: left for the caller to re-derive from a status code and a body:
#: ``transport`` (network, 5xx, a dead stream) is retried until the run's
#: deadline; ``refusal`` (429, admission) is backed off; ``model_missing``
#: moves to the next model in a role's chain without spending a retry;
#: ``context_overflow`` asks for compaction; ``bad_request`` is ours to fix;
#: ``tool_call_unparsable`` (plan D4) is the model's own call the server
#: could not read — never retried here, since resending the same request
#: asks for the same call; the agent loop re-asks with the cause.
TRANSPORT = "transport"
REFUSAL = "refusal"
MODEL_MISSING = "model_missing"
CONTEXT_OVERFLOW = "context_overflow"
BAD_REQUEST = "bad_request"
TOOL_CALL_UNPARSABLE = "tool_call_unparsable"


class ChatFault(RuntimeError):
    """A chat call that failed for good, with the reason named.

    A ``RuntimeError`` so every existing ``except RuntimeError`` keeps
    catching it; the message is unchanged from before the class existed.
    ``rejection`` is what the server said about a rejected tool call
    (``reason`` / ``key`` / ``tool``), ``partial`` the content streamed
    before a mid-stream failure.
    """

    def __init__(self, message: str, *, fault_class: str,
                 status: Optional[int] = None, body: str = "",
                 rejection: Optional[dict] = None, partial: str = ""):
        super().__init__(message)
        self.fault_class = fault_class
        self.status = status
        self.body = body
        self.rejection = rejection
        self.partial = partial


#: A tool call the server's parser could not read (Cerebriline
#: ``error-classification.ts:249``): Ollama's XML tool parsers, generic
#: "failed to parse tool call" wording, and llama.cpp's streaming parser
#: (``Invalid diff: now finding less tool calls``), which ends the stream.
_TOOL_CALL_UNPARSABLE_RX = (
    re.compile(r"\bXML syntax error\b[\s\S]*</?(?:function|tool_call|"
               r"parameter|invoke)\b", re.I),
    re.compile(r"\b(?:failed to|could not|cannot|unable to)\s+parse\b"
               r"[\s\S]{0,80}\b(?:tool[_\s-]?call|function[_\s-]?call|"
               r"tool arguments|function arguments)\b", re.I),
    re.compile(r"\b(?:invalid|malformed)\s+(?:tool[_\s-]?call|"
               r"function[_\s-]?call|tool arguments|function arguments)\b",
               re.I),
    re.compile(r"\bInvalid diff:\s*now finding less tool calls\b", re.I),
)
#: opencoti's error ``type`` for a call it rejected (bug-3601); newer
#: builds add ``reason`` (``swallowed_key``), ``key`` and ``tool``.
TOOL_CALL_REJECTED_TYPE = "tool_call_rejected"


def tool_call_rejection(body: Any, _depth: int = 0) -> Optional[dict]:
    """The ``{reason, key?, tool?}`` an engine attached to a rejected call,
    found in a body, a nested error, or a JSON-encoded string."""
    if body is None or _depth > 6:
        return None
    if isinstance(body, str):
        text = body.strip()
        if not text.startswith(("{", "[")):
            return None
        try:
            body = json.loads(text)
        except ValueError:
            return None
        return tool_call_rejection(body, _depth + 1)
    if isinstance(body, list):
        for item in body:
            found = tool_call_rejection(item, _depth + 1)
            if found:
                return found
        return None
    if not isinstance(body, dict):
        return None
    reason = body.get("reason")
    if (body.get("type") == TOOL_CALL_REJECTED_TYPE
            and isinstance(reason, str) and reason.strip()):
        out = {"reason": reason.strip()}
        for k in ("key", "tool"):
            v = body.get(k)
            if isinstance(v, str) and v.strip():
                out[k] = v.strip()
        return out
    for k in ("error", "data", "cause", "message"):
        found = tool_call_rejection(body.get(k), _depth + 1)
        if found:
            return found
    return None


def is_tool_call_unparsable(body: Any) -> bool:
    text = body if isinstance(body, str) else json.dumps(body, default=str)
    return (f'"{TOOL_CALL_REJECTED_TYPE}"' in (text or "")
            or any(rx.search(text or "") for rx in _TOOL_CALL_UNPARSABLE_RX))


_MODEL_MISSING_RX = re.compile(
    r"model\s+['\"]?[^'\"]*['\"]?\s+not found|not found, try pulling|"
    r"no such model|model not found|pull model manifest", re.I)
_OVERFLOW_RX = re.compile(
    # "exceeds the available context size" is llama.cpp / Ollama runner's
    # wording when the loaded window is below the declared one.
    r"context[ _]length|context[ _]window|"
    r"exceeds? the (?:maximum |available )?context|"
    r"too many tokens|prompt is too long|context_window_exceeded", re.I)


def _raise_stream_error(obj: dict, partial: str) -> None:
    """A stream's error event: a tool call the server could not read is
    :data:`TOOL_CALL_UNPARSABLE` (not retried); anything else is a dead
    stream, ``OSError``, retried as transport."""
    err = obj.get("error")
    if is_tool_call_unparsable(obj):
        raise ChatFault(f"stream error from server: {str(err)[:300]}",
                        fault_class=TOOL_CALL_UNPARSABLE,
                        status=obj.get("status") if isinstance(
                            obj.get("status"), int) else None,
                        body=json.dumps(obj, default=str)[:4000],
                        rejection=tool_call_rejection(obj), partial=partial)
    raise OSError(f"stream error from server: {str(err)[:300]}")


def classify_http_fault(status: int, body: str) -> str:
    # Ahead of the status gate: opencoti rejects a call with a 500, and
    # what the model emitted is the fault whatever the status says.
    if is_tool_call_unparsable(body):
        return TOOL_CALL_UNPARSABLE
    if status == 404 or (400 <= status < 500 and _MODEL_MISSING_RX.search(body or "")):
        return MODEL_MISSING
    if status == 429:
        return REFUSAL
    if status in RETRYABLE_STATUS or status >= 500:
        return TRANSPORT
    if _OVERFLOW_RX.search(body or ""):
        return CONTEXT_OVERFLOW
    return BAD_REQUEST


#: A refusal that names the provider's real output maximum. Ollama Cloud
#: / Z.ai, Anthropic, OpenAI spellings (Cerebriline ``gateway.ts:125-135``).
OUTPUT_CEILING_PATTERNS = (
    re.compile(r"maximum output tokens\s*\((\d+)\)", re.I),
    re.compile(r"max_tokens:\s*\d+\s*>\s*(\d+)", re.I),
    re.compile(r"supports at most\s*(\d+)\s*completion tokens", re.I),
)

#: Learned per (server, model) for the life of the process: a ceiling the
#: provider stated is a fact about the provider, not about one request.
_learned_ceilings: dict[tuple[str, str], int] = {}
_ceilings_lock = threading.Lock()


def learned_output_ceiling(base_url: str, model: str) -> Optional[int]:
    with _ceilings_lock:
        return _learned_ceilings.get((base_url, model))


def _learn_ceiling(base_url: str, model: str, body: str) -> Optional[int]:
    for rx in OUTPUT_CEILING_PATTERNS:
        m = rx.search(body or "")
        if m:
            n = int(m.group(1))
            if n > 0:
                with _ceilings_lock:
                    _learned_ceilings[(base_url, model)] = n
                return n
    return None


#: A refusal that is about the requested output format.
_FORMAT_REFUSED_RX = re.compile(
    r"\bformat\b|response_format|json_schema|structured output|grammar",
    re.I)

#: ``Retry-After`` is honoured up to this many seconds per wait; a longer
#: ask is answered with this, and the next attempt asks again.
RETRY_AFTER_MAX_S = 30.0


def retry_after_s(headers) -> Optional[float]:
    """Seconds a 429/503 asks us to wait: integer seconds or an
    HTTP-date (a past date is 0). ``None`` when absent or unparseable."""
    if headers is None:
        return None
    try:
        raw = headers.get("Retry-After")
    except Exception:  # noqa: BLE001
        return None
    if raw is None:
        return None
    raw = str(raw).strip()
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        return None
    if when is None:
        return None
    import datetime as _dt
    now = _dt.datetime.now(when.tzinfo or _dt.timezone.utc)
    return max(0.0, (when - now).total_seconds())


#: An answer with no content, no tool call and no reasoning that still
#: says it stopped normally is a dropped reply, not a verdict. Asked
#: again this many times, a short pause apart (Cerebriline
#: ``retry-empty-response.ts``, commit 9d7cf0b6f).
EMPTY_RETRIES = 2
EMPTY_RETRY_DELAY_S = 0.25


#: Plan D15 (Cerebriline ``EMPTY_TURN_REMINDER``): added once to the
#: request an empty reply is re-asked with. The resend was identical, and
#: a request with a fixed seed tends to answer the same nothing again.
EMPTY_TURN_REMINDER = (
    "[SYSTEM MESSAGE] Your last reply was empty: nothing arrived — no "
    "answer, no tool call. Take the smallest useful next step instead: make "
    "one tool call, or write one short paragraph. If the work you were "
    "planning does not fit in one reply, do the part that fits and continue "
    "in the next turn.")


def _with_empty_turn_reminder(body: dict) -> bool:
    """Append :data:`EMPTY_TURN_REMINDER` to a chat body once; whether
    the body changed."""
    msgs = body.get("messages")
    if not isinstance(msgs, list):
        return False
    if msgs and isinstance(msgs[-1], dict) \
            and msgs[-1].get("content") == EMPTY_TURN_REMINDER:
        return False
    body["messages"] = list(msgs) + [{"role": "user",
                                      "content": EMPTY_TURN_REMINDER}]
    return True


def _is_empty_reply(data: dict) -> bool:
    msg = data.get("message") or {}
    if (msg.get("content") or "").strip() or msg.get("tool_calls"):
        return False
    if any((msg.get(k) or "").strip() for k in _STREAMED_REASONING_KEYS
           if isinstance(msg.get(k), str)):
        return False
    return (data.get("done_reason") or "stop") == "stop"


def _timings(data: dict, *, wall_s: float, queue_s: float,
             ttft_s: Optional[float], attempts: int, empty_retries: int,
             headers=None) -> dict:
    """Per-call timings for the run record (plan A5). Durations Ollama
    reports in nanoseconds become milliseconds; prefill and decode rates
    are computed from evaluated tokens only."""
    out: dict = {"wall_ms": round(wall_s * 1000), "queue_ms": round(queue_s * 1000),
                 "attempts": attempts}
    if ttft_s is not None:
        out["ttft_ms"] = round(ttft_s * 1000)
    if empty_retries:
        out["empty_retries"] = empty_retries
    cached = data.get("cached_prompt_tokens")
    if isinstance(cached, int) and cached >= 0:
        out["cached_prompt_tokens"] = cached
    tb = data.get("think_budget_tokens")
    if isinstance(tb, int) and not isinstance(tb, bool) and tb >= 0:
        # xOllama and think-budget Ollama builds report what they enforced.
        out["think_budget_tokens"] = tb
        out["think_budget_source"] = "read_back"
    for src, dst in (("load_duration", "load_ms"),
                     ("prompt_eval_duration", "prompt_eval_ms"),
                     ("eval_duration", "eval_ms"),
                     ("total_duration", "total_ms")):
        v = data.get(src)
        if isinstance(v, (int, float)) and v >= 0:
            out[dst] = round(v / 1e6)
    pe, pd = data.get("prompt_eval_count"), data.get("prompt_eval_duration")
    if isinstance(pe, int) and pe > 0 and isinstance(pd, (int, float)) and pd > 0:
        out["prefill_tok_s"] = round(pe / (pd / 1e9), 1)
    ev, ed = data.get("eval_count"), data.get("eval_duration")
    if isinstance(ev, int) and ev > 0 and isinstance(ed, (int, float)) and ed > 0:
        out["decode_tok_s"] = round(ev / (ed / 1e9), 1)
    if headers is not None:
        try:
            win = headers.get("X-Context-Window")
        except Exception:  # noqa: BLE001
            win = None
        if win:
            try:
                out["granted_window"] = int(win)
            except ValueError:
                pass
    return out


#: Thinking levels, mildest first (plan B3).
THINK_LEVELS = ("minimal", "low", "medium", "high", "max")

#: Share of ``min(num_predict, num_ctx)`` a level may think in: Ollama's
#: ``thinkBudgetFraction`` (``api/types.go``), which the fork also applies
#: client-side for llama.cpp. ``xhigh`` is an alias of ``max``.
THINK_FRACTIONS = {"max": 4 / 5, "high": 1 / 2, "medium": 1 / 4,
                   "low": 1 / 8, "minimal": 1 / 16}


def normalize_think(value: Any) -> Any:
    """``think`` as the council states it: ``False``, ``True`` (the
    model's own budget; also ``"auto"`` / ``"on"``), a level from
    :data:`THINK_LEVELS` (``"xhigh"`` → ``"max"``) or a token count.
    Anything else is returned unchanged for the server to judge."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return value if value > 0 else False
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("auto", "on", "true"):
            return True
        if v in ("off", "none", "false"):
            return False
        if v == "xhigh":
            return "max"
        if v.isdigit():
            return normalize_think(int(v))
        return v
    return value


def think_budget_tokens(think: Any, num_predict: Optional[int],
                        num_ctx: Optional[int] = None) -> Optional[int]:
    """Thinking tokens a level or count allows, or ``None`` (no budget:
    ``True``, ``False``, or nothing to take a share of). The share is of
    ``min(num_predict, num_ctx)``, of whichever is known, floored."""
    t = normalize_think(think)
    if isinstance(t, int) and not isinstance(t, bool):
        return t
    frac = THINK_FRACTIONS.get(t) if isinstance(t, str) else None
    if frac is None:
        return None
    bases = [n for n in (num_predict, num_ctx) if isinstance(n, int) and n > 0]
    if not bases:
        return None
    n = int(min(bases) * frac)
    return n if n > 0 else None


def _sent_think_budget(body: dict) -> Optional[int]:
    """The thinking budget a request body stated, if any."""
    v = body.get("reasoning_budget_tokens")
    if isinstance(v, int) and not isinstance(v, bool):
        return v
    v = (body.get("options") or {}).get("think_budget")
    if isinstance(v, int) and not isinstance(v, bool):
        return v
    return None


class _Attempts:
    """``range(max_retries + 1)`` whose current index can be held, so an
    outage wait re-sends without spending a retry."""

    def __init__(self, max_retries: int):
        self.max_retries = max_retries
        self._held = False

    def hold(self) -> None:
        self._held = True

    def __iter__(self):
        n = 0
        while n <= self.max_retries:
            yield n
            if self._held:
                self._held = False
            else:
                n += 1


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


#: Label a mid-conversation system message carries once it is sent as a
#: user turn (:func:`single_system_messages`).
SYSTEM_NOTE_PREFIX = "[system note]\n"


def single_system_messages(messages: list[dict]) -> list[dict]:
    """Messages a llama.cpp chat template accepts: one ``system`` turn,
    first, and no two ``user`` turns in a row.

    Ollama renders several system messages and system turns mid-history;
    llama.cpp's Jinja templates (opencoti, llamafile) raise "System
    message must be at the beginning" on either. The council sends both:
    grounding, structure map, anchors and the role prompt as leading
    system turns, and compaction markers later on. So the leading run is
    joined into one system turn, in order, and a later system turn is
    sent as a user note, merged into an adjacent user turn so roles still
    alternate. Content is never dropped."""
    out: list[dict] = []
    i = 0
    lead: list[str] = []
    while i < len(messages) and messages[i].get("role") == "system":
        c = messages[i].get("content")
        if isinstance(c, str) and c:
            lead.append(c)
        i += 1
    if lead:
        out.append({"role": "system", "content": "\n\n".join(lead)})
    for m in messages[i:]:
        if m.get("role") == "system":
            m = {"role": "user",
                 "content": SYSTEM_NOTE_PREFIX + str(m.get("content") or "")}
        if (m.get("role") == "user" and out and out[-1].get("role") == "user"
                and isinstance(out[-1].get("content"), str)
                and isinstance(m.get("content"), str)):
            out[-1] = {**out[-1],
                       "content": out[-1]["content"] + "\n\n" + m["content"]}
            continue
        out.append(m)
    return out


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


def _context_length_from_show(data: dict) -> Optional[int]:
    """Pull the context window out of an ``/api/show`` response.

    Ollama namespaces it by architecture — ``gemma4.context_length``,
    ``qwen3.context_length``, … — so the key cannot be hardcoded; match
    on the suffix instead. Returns ``None`` when the endpoint does not
    advertise one, which several cloud builds do not.

    ``None`` is deliberately not "assume 4096". A wrong small guess
    would trim prompts that fit perfectly well, and the failure mode
    this whole change exists to kill is exactly that class: acting on a
    number nobody verified.
    """
    info = data.get("model_info")
    if not isinstance(info, dict):
        return None
    for key, value in info.items():
        if not key.endswith(".context_length"):
            continue
        try:
            n = int(value)
        except (TypeError, ValueError):
            continue
        if n > 0:
            return n
    return None


#: Where a model's thinking-budget message can be declared. Ollama
#: Message fields a streamed response carries reasoning under. Each chunk
#: holds a delta, so these are concatenated rather than overwritten.
_STREAMED_REASONING_KEYS = ("thinking", "reasoning", "reasoning_content")

#: surfaces a Modelfile's parameters under several shapes depending on
#: build, so all of them are checked rather than guessed between.
_BUDGET_MESSAGE_KEYS = (
    "thinking_budget_message", "think_budget_message",
    "reasoning_budget_message",
)


def _budget_message_from_show(data: dict) -> Optional[str]:
    """The model's own "you have run out of thinking budget" message.

    A model that hits its thinking cap does not stop reasoning — it stops
    mid-sentence. Ollama appends the message the Modelfile declares, so
    finding that message at the *end* of a think is direct evidence the
    budget, rather than the model, ended it.

    Returns ``None`` when the model declares none, which is most of them.
    ``None`` means "no marker to look for", not "no cap": the
    proximity ratio in :mod:`claude_hooks.capped_thinking` still applies.
    """
    for container in (data.get("parameters"), data.get("model_info"),
                      data.get("details")):
        if isinstance(container, dict):
            for key in _BUDGET_MESSAGE_KEYS:
                value = container.get(key)
                if isinstance(value, str) and value.strip():
                    return value
        elif isinstance(container, str):
            # ``parameters`` arrives as a flat text blob on some builds:
            # one ``key value`` pair per line, values often quoted.
            for line in container.splitlines():
                parts = line.strip().split(None, 1)
                if len(parts) != 2:
                    continue
                key, value = parts[0], parts[1].strip().strip('"')
                if key in _BUDGET_MESSAGE_KEYS and value:
                    return value
    return None


class ChatClient:
    def __init__(self, base_url: str, *, timeout_s: float = DEFAULT_TIMEOUT_S,
                 max_retries: int = DEFAULT_MAX_RETRIES,
                 retry_base_delay_s: float = DEFAULT_RETRY_BASE_DELAY_S,
                 retry_max_delay_s: float = DEFAULT_RETRY_MAX_DELAY_S,
                 outage_wait_s: float = 0.0,
                 api_key: Optional[str] = None):
        self.base_url = base_url.rstrip("/")
        # How long one call keeps waiting out a network outage
        # (``is_outage``) before the ordinary retry budget applies again.
        # Off by default: an interactive caller should hear about a dead
        # line, not hang on it. Benchmarks turn it on — a lost verdict
        # costs more than a late one.
        self.outage_wait_s = float(outage_wait_s or 0.0)
        # ``/v1`` (OpenAI compat) or ``/api`` pasted onto the root: both
        # are stripped, because every path below is built from the root.
        for suffix in ("/v1", "/api"):
            if self.base_url.endswith(suffix):
                self.base_url = self.base_url[: -len(suffix)]
        # Sent as a bearer token only when one is configured (ollama.com
        # direct, a keyed xOllama). A local server needs none. Ollama Cloud
        # itself (https://ollama.com, no local Ollama) falls back to the
        # host's key (``ollama.api_key_file``, else OLLAMA_API_KEY).
        if not api_key:
            from claude_hooks import ollama_upstream
            api_key = ollama_upstream.api_key(self.base_url)
        self.api_key = api_key or None
        # Set when this client serves a configured endpoint
        # (``@name/model``): the model the server actually knows.
        self.served_model: Optional[str] = None
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
        # Inference-time accounting (separates real LLM work from
        # retry sleeps + failed-attempt timeouts). Only the SUCCESSFUL
        # attempt's duration is added to ``total_inference_s``; the
        # 10-minute timeouts that fired before the successful retry
        # are explicitly excluded. ``last_inference_s`` is the
        # duration of the most recent successful chat() / stream call.
        #
        # Callers (notably the coder benchmark) reset these per-trial
        # via ``reset_inference_timer()`` and read
        # ``total_inference_s`` at trial end to get a
        # retry-decontaminated cost signal.
        self.last_inference_s: float = 0.0
        self.total_inference_s: float = 0.0
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
        # Per-model context window in tokens, learned from the SAME
        # ``/api/show`` response as the think probe (``model_info``
        # carries ``<arch>.context_length``). ``None`` = unknown, which
        # callers must treat as "do not guess" rather than as a small
        # default — inventing a limit is how a fitting prompt gets
        # needlessly trimmed.
        self._context_length: dict[str, Optional[int]] = {}
        # Same lifecycle as ``_context_length``: populated by the one
        # ``/api/show`` probe, ``None`` for a model that declares no
        # thinking-budget message.
        self._budget_message: dict[str, Optional[str]] = {}
        # The whole ``/api/show`` answer per model, kept so the capability
        # record (:meth:`caps`) is built without a second round trip.
        self._show: dict[str, dict] = {}
        # Per-call timings of the last successful call (plan A5); also
        # attached to the returned response as ``timings``.
        self.last_timings: dict = {}
        # Windows granted to requests that stated one (plan B2); a booking
        # never shrinks, so the first grant is kept.
        self._granted: dict[str, int] = {}
        # Read-back thinking budgets per (model, think, limits) (plan B3).
        self._think_budgets: dict[tuple, Optional[int]] = {}

    def _headers(self, **extra) -> dict:
        h = {"Content-Type": "application/json", **extra}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def caps(self, model: str, *, refresh: bool = False):
        """The :class:`claude_hooks.provider_caps.ProviderCaps` record for
        ``model`` on this server: window, output max, thinking values,
        cloud marker, Modelfile temperature, xOllama features."""
        from claude_hooks import provider_caps
        model = self._real(model)
        if model not in self._show and model not in self._probed_think:
            self._probe_supports_think(model)
        auth = ({"Authorization": f"Bearer {self.api_key}"}
                if self.api_key else None)
        return provider_caps.ollama_caps(
            self.base_url, model, show=self._show.get(model),
            headers=auth, refresh=refresh)

    def learned_output_ceiling(self, model: str) -> Optional[int]:
        """An output maximum this server's refusal named for ``model``."""
        return learned_output_ceiling(self.base_url, self._real(model))

    def think_budget(self, model: str, think: Any,
                     num_predict: Optional[int],
                     num_ctx: Optional[int] = None) -> Optional[int]:
        """The thinking budget the server will enforce for ``think`` at
        these limits, read back from ``POST /api/show`` (xOllama and
        think-budget builds answer ``think_budget_tokens``; stock Ollama
        does not, which is ``None``). Asked once per (model, think,
        limits). Never asked of a cloud model: no token budget exists
        there (plan B3)."""
        model = self._real(model)
        key = (model, str(think), num_predict, num_ctx)
        if key in self._think_budgets:
            return self._think_budgets[key]
        from claude_hooks import provider_caps
        caps = provider_caps.cached(self.base_url, model)
        if caps is not None and caps.cloud:
            self._think_budgets[key] = None
            return None
        opts = {k: v for k, v in (("num_predict", num_predict),
                                  ("num_ctx", num_ctx)) if v}
        req = urllib.request.Request(
            f"{self.base_url}/api/show", method="POST",
            data=json.dumps({"model": model, "think": normalize_think(think),
                             "options": opts}).encode(),
            headers=self._headers())
        value: Optional[int] = None
        try:
            with urllib.request.urlopen(req, timeout=10.0) as resp:
                data = json.loads(resp.read())
            v = data.get("think_budget_tokens") if isinstance(data, dict) else None
            if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
                value = v
        except (urllib.error.URLError, OSError, ValueError):
            return None  # not cached: ask again next time
        self._think_budgets[key] = value
        return value

    def loaded_context(self, model: str) -> Optional[int]:
        """The window ``model`` is loaded with now (``/api/ps``)."""
        from claude_hooks import provider_caps
        auth = ({"Authorization": f"Bearer {self.api_key}"}
                if self.api_key else None)
        return provider_caps.loaded_context(self.base_url, self._real(model),
                                            headers=auth)

    def _accepted_level(self, model: str, level: str) -> Any:
        """``level`` if the service accepts it for ``model``, else bare
        ``True`` (plan B3). Only an already-probed capability record is
        consulted, so building a request never costs a round trip; with
        none, the level is sent as is and a refusal is retried as
        ``True``."""
        from claude_hooks import provider_caps
        caps = provider_caps.cached(self.base_url, model)
        values = tuple(str(v).lower() for v in (caps.thinking_values if caps else ()))
        if not values or level in values:
            return level
        return True

    def _note_grant(self, payload: dict, timings: dict) -> None:
        """Remember ``X-Context-Window`` only for a request that stated a
        window (``options.num_ctx`` / ``num_ctx``). Without one, an
        opencoti request is a per-request allocation and the header is
        that request's settlement (83 tokens on a 21-token call), not a
        window later calls can budget against (plan B2)."""
        win = (timings or {}).get("granted_window")
        stated = (payload.get("num_ctx")
                  or (payload.get("options") or {}).get("num_ctx"))
        if win and stated:
            self._granted.setdefault(self._real(payload.get("model") or ""),
                                     int(win))

    def granted_window(self, model: str) -> Optional[int]:
        """The window the engine granted a request that stated one, or
        ``None`` (:meth:`_note_grant`)."""
        return self._granted.get(self._real(model))

    def _wait_out_outage(self, e: BaseException, body: str,
                         attempts: "_Attempts", state: dict, *,
                         what: str,
                         cancel_check: Optional[Callable[[], bool]] = None,
                         on_wait: Optional[Callable[[dict], None]] = None,
                         ) -> bool:
        """Sleep through a network outage and hold the attempt index.
        Returns False (use the ordinary retry path) when the budget is
        off or spent, or the failure is not an outage."""
        if self.outage_wait_s <= 0 or not is_outage(e, body):
            return False
        now = time.monotonic()
        t0 = state.setdefault("t0", now)
        if now - t0 >= self.outage_wait_s:
            if not state.get("spent"):
                state["spent"] = True
                log.error("%s: outage budget of %.0fs spent; ordinary "
                          "retries from here", what, self.outage_wait_s)
            return False
        n = state["n"] = state.get("n", 0) + 1
        delay = min(OUTAGE_MAX_DELAY_S, self.retry_base_delay_s * (2 ** (n - 1)),
                    max(0.0, self.outage_wait_s - (now - t0)))
        log.warning("%s: network outage (%s); waiting %.0fs, %.0f of %.0fs "
                    "outage budget used", what, str(e)[:160] or body[:160],
                    delay, now - t0, self.outage_wait_s)
        if on_wait is not None:
            for _t in ollama_slots.infra_notices(
                    now - t0, state.setdefault("noticed", set())):
                try:
                    on_wait({"what": "outage", "waited_s": round(now - t0),
                             "error": (str(e) or body)[:200]})
                except Exception:  # pragma: no cover
                    log.exception("on_wait raised; ignored")
        end = time.monotonic() + delay
        while True:
            left = end - time.monotonic()
            if left <= 0:
                break
            if cancel_check is not None and cancel_check():
                from consultants.engine.stall import CancelledByOrchestrator
                raise CancelledByOrchestrator("cancelled during an outage wait")
            time.sleep(min(1.0, left))
        attempts.hold()
        return True

    def reset_inference_timer(self) -> None:
        """Reset the per-trial inference-time accumulators.

        Call before a benchmark trial / consultant session so the
        ``total_inference_s`` accumulator reflects only the work
        done within that scope. ``last_inference_s`` is also
        reset so a downstream reader that picks up the field
        between calls won't see a stale value from a prior run.
        """
        self.last_inference_s = 0.0
        self.total_inference_s = 0.0

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
            headers=self._headers(),
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
        # The same response carries the context window under
        # ``model_info["<arch>.context_length"]`` (e.g.
        # ``gemma4.context_length: 262144``). Harvest it here rather
        # than in a second round-trip: the probe is already cached per
        # model, and a separate call would double the /api/show traffic
        # for information that arrived in the first one.
        if isinstance(data, dict):
            self._show[model] = data
        self._context_length[model] = _context_length_from_show(data)
        # Same response, same reasoning: the thinking-budget message is
        # declared in the Modelfile and arrives here, so harvest it on
        # the probe we were already making.
        self._budget_message[model] = _budget_message_from_show(data)

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

    def context_length(self, model: str) -> Optional[int]:
        """Context window for ``model`` in tokens, or ``None``.

        Reads the value ``/api/show`` advertises, probing once per model
        and caching for the client's lifetime. ``None`` means the
        endpoint did not say — callers must treat that as "unknown", not
        as a small default, because inventing a limit trims prompts that
        would have fit.

        The window is not the same thing as the *output* budget: on the
        `:cloud` models a 49k prompt against a 262k window still came
        back truncated, so this is a necessary input to a budget
        decision, never sufficient on its own.
        """
        model = self._real(model)
        if model not in self._context_length:
            # Populates both caches; the think verdict is a side effect
            # we already wanted.
            self._probe_supports_think(model)
        return self._context_length.get(model)

    def thinking_budget_message(self, model: str) -> Optional[str]:
        """The model's own out-of-thinking-budget message, or ``None``.

        Used to tell a think that ended because the model was finished
        from one that ended because the budget was. There is no guessing
        at the wording: either the model declares it via ``/api/show`` or
        there is no marker to find, and the detector falls back to the
        proximity ratio alone.
        """
        model = self._real(model)
        if model not in self._budget_message:
            self._probe_supports_think(model)
        return self._budget_message.get(model)

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
        return self._call(payload, stream=False)

    # ---- streaming variant ---------------------------------------- #

    def chat_streamed(self, payload: dict,
                      *,
                      on_token: Optional[Callable[[str], None]] = None,
                      cancel_check: Optional[Callable[[], bool]] = None,
                      on_admitted: Optional[Callable[[], None]] = None,
                      on_wait: Optional[Callable[[dict], None]] = None,
                      ) -> dict:
        """Streaming counterpart to ``chat()``.

        ``on_wait`` (plan D9) gets one dict per infrastructure wait — a
        queue for a connection slot or a network outage — at 10 min and
        again at 2 h of it; informational, nothing else changes.

        ``on_admitted`` is called once per attempt when the call holds
        its connection slot (``claude_hooks.ollama_slots``) and is about
        to send — a stall watchdog restarts its clock there.

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
        return self._call(payload, stream=True, on_token=on_token,
                          cancel_check=cancel_check, on_admitted=on_admitted,
                          on_wait=on_wait)

    #: Path of the chat route under the server root.
    _chat_path = "/api/chat"
    #: Ask ``/api/show`` whether the model thinks before sending ``think``.
    _probe_think_before_call = True

    def _decode(self, data: dict) -> dict:
        """A non-streamed reply in Ollama's shape (identity here; the
        OpenAI-compatible subclass translates)."""
        return data

    def _real(self, model: str) -> str:
        """The served model behind a configured endpoint reference
        (``@name`` / ``@name/model``); any other name unchanged."""
        if self.served_model and model and model.startswith("@"):
            return self.served_model
        return model

    def _with_served_model(self, payload: dict) -> dict:
        model = (payload or {}).get("model") or ""
        real = self._real(model)
        if real != model:
            payload = {**payload, "model": real}
        return payload

    def _prepare(self, payload: dict, *, strip_think: bool,
                 stream: bool) -> dict:
        body = self._to_ollama(payload, strip_think=strip_think)
        # A ceiling this server's refusal already named is applied before
        # sending, so only the first request to a model pays the refusal.
        ceiling = learned_output_ceiling(self.base_url, body.get("model") or "")
        opts = body.get("options")
        if (ceiling and isinstance(opts, dict)
                and isinstance(opts.get("num_predict"), int)
                and opts["num_predict"] > ceiling):
            opts["num_predict"] = ceiling
        if stream:
            body["stream"] = True       # the one bit that actually changes
        return body

    def _call(self, payload: dict, *, stream: bool,
              on_token: Optional[Callable[[str], None]] = None,
              cancel_check: Optional[Callable[[], bool]] = None,
              on_admitted: Optional[Callable[[], None]] = None,
              on_wait: Optional[Callable[[dict], None]] = None) -> dict:
        """The one retry loop behind :meth:`chat` and :meth:`chat_streamed`.

        Beyond the retry budget it handles, without spending a retry:
        a model that rejects ``think`` (strip it, remember the model); a
        refusal that names the provider's output maximum (retry once at
        that value, remember it per server and model); and an empty
        reply (asked again up to :data:`EMPTY_RETRIES` times). A 429/503
        carrying ``Retry-After`` waits what it asks, up to
        :data:`RETRY_AFTER_MAX_S`. A call that fails for good raises
        :class:`ChatFault` with its fault class named.
        """
        if stream:
            # Lazy import to avoid coupling get_advice -> consultants.
            from consultants.engine.stall import CancelledByOrchestrator
            cancelled_exc: tuple = (CancelledByOrchestrator,)
        else:
            cancelled_exc = ()
        what = "ollama chat_streamed" if stream else "ollama chat"

        # Decide whether to forward ``think``. Three layers, cheapest
        # first: (1) prior reactive fallback already marked this
        # model unsupported -> strip; (2) proactive ``/api/show``
        # probe says ``thinking`` is not in capabilities -> strip;
        # (3) probe says yes or is unknown -> send and rely on the
        # reactive 400 path as a safety net.
        payload = self._with_served_model(payload)
        model_tag = (payload or {}).get("model") or ""
        strip_think = model_tag in self._unsupported_think
        if (self._probe_think_before_call
                and not strip_think
                and "think" in (payload or {})
                and model_tag
                and model_tag not in self._probed_think):
            probe = self._probe_supports_think(model_tag)
            if probe is False:
                strip_think = True

        body = self._prepare(payload, strip_think=strip_think, stream=stream)
        url = f"{self.base_url}{self._chat_path}"
        encoded = json.dumps(body).encode()

        last_exc: Optional[Exception] = None
        attempts, outage = _Attempts(self.max_retries), {}
        ceiling_retried = False
        format_dropped = False
        empty_retries = 0
        sent = 0
        for attempt in attempts:
            sent += 1
            extra = {"Accept": "application/x-ndjson"} if stream else {}
            req = urllib.request.Request(
                url, data=encoded, method="POST",
                headers=self._headers(**extra),
            )
            clock: dict = {}
            try:
                # One connection slot per attempt, released during the
                # backoff below: a sleeping retry holds no connection.
                queued_at = time.monotonic()
                try:
                    admitted = (ollama_slots.slot(self.base_url, model_tag,
                                                  cancel_check=cancel_check,
                                                  on_wait=on_wait)
                                if stream else
                                ollama_slots.slot(self.base_url, model_tag))
                    admitted.__enter__()
                except ollama_slots.Cancelled as e:
                    raise cancelled_exc[0](
                        f"cancelled while queued for a {e} slot") from e
                try:
                    queue_s = time.monotonic() - queued_at
                    # The stall clock starts when the call is admitted:
                    # time queued for a slot is not a silent model.
                    if on_admitted is not None:
                        on_admitted()
                    attempt_start = time.monotonic()
                    with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                        resp_headers = getattr(resp, "headers", None)
                        if stream:
                            data = self._consume_ndjson(
                                resp, on_token=on_token,
                                cancel_check=cancel_check, clock=clock,
                            )
                        else:
                            data = self._decode(json.loads(resp.read()))
                finally:
                    admitted.__exit__(None, None, None)
                wall_s = time.monotonic() - attempt_start
                if empty_retries < EMPTY_RETRIES and _is_empty_reply(data):
                    empty_retries += 1
                    log.warning(
                        "%s: %s answered with nothing (no content, tool "
                        "call or reasoning); asking again (%d/%d)",
                        what, model_tag, empty_retries, EMPTY_RETRIES)
                    if _with_empty_turn_reminder(body):
                        encoded = json.dumps(body).encode()
                    time.sleep(EMPTY_RETRY_DELAY_S)
                    attempts.hold()
                    continue
                # Inference-time accounting: ONLY the successful
                # attempt's duration counts. Prior failed attempts
                # (timeouts, retryable 5xx, network resets) and the
                # exponential-backoff sleeps between them are excluded
                # from total_inference_s. The bench reads this field
                # at trial end to produce a retry-decontaminated cost
                # signal.
                self.last_inference_s = wall_s
                self.total_inference_s += wall_s
                if attempt > 0:
                    log.info(
                        "%s: succeeded on retry %d/%d (inference %.1fs)",
                        what, attempt, self.max_retries, self.last_inference_s,
                    )
                out = self._from_ollama(data)
                first = clock.get("first")
                self.last_timings = _timings(
                    data, wall_s=wall_s, queue_s=queue_s,
                    ttft_s=(first - attempt_start) if first else None,
                    attempts=sent, empty_retries=empty_retries,
                    headers=resp_headers)
                # The realized sampler (plan A5/B4), whichever shape the
                # body has: Ollama ``options`` or OpenAI top level.
                src = body.get("options") if isinstance(body.get("options"), dict) else body
                sampler = {k: src[k] for k in ("seed", "temperature")
                           if k in src}
                if sampler:
                    self.last_timings["sampler"] = sampler
                if "think_budget_tokens" not in self.last_timings:
                    sent_tb = _sent_think_budget(body)
                    if sent_tb is not None:
                        self.last_timings["think_budget_tokens"] = sent_tb
                        self.last_timings["think_budget_source"] = "sent"
                if data.get("reasoning_loop"):
                    # Plan D1: the stream was cut on a reasoning loop.
                    # The record keeps the quantity the guard fired on.
                    out["reasoning_loop"] = data["reasoning_loop"]
                    self.last_timings["reasoning_loop"] = data["reasoning_loop"]
                out["timings"] = self.last_timings
                self._note_grant(payload, self.last_timings)
                return out
            except cancelled_exc:
                # Cooperative abort — propagate without retrying. The
                # orchestrator owns the retry decision.
                raise
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
                if (think_rejected and body.get("think") not in (True, False)
                        and payload.get("think") is not True):
                    # A level (or budget) the model refused is not proof it
                    # cannot think: ask once with a bare ``think: true``
                    # before dropping the field (plan B3, report 01 #12).
                    log.warning("%s: %s refused think=%r (HTTP %d); retrying "
                                "with think=true: %s", what, model_tag,
                                body.get("think"), e.code, err_body[:200])
                    payload = {**payload, "think": True}
                    body = self._prepare(payload, strip_think=strip_think,
                                         stream=stream)
                    encoded = json.dumps(body).encode()
                    attempts.hold()
                    continue
                if think_rejected:
                    log.warning(
                        "%s: model %s rejected 'think' field "
                        "(HTTP %d). Stripping and retrying once. "
                        "Future calls to this model will skip the "
                        "field. Body: %s",
                        what, model_tag, e.code, err_body,
                    )
                    self._unsupported_think.add(model_tag)
                    body = self._prepare(payload, strip_think=True,
                                         stream=stream)
                    encoded = json.dumps(body).encode()
                    continue
                # Structured output the server refuses (a backend without
                # it, or a schema its grammar compiler cannot parse):
                # asked once more without the format, never failed.
                if (e.code in (400, 422, 500) and payload.get("format")
                        and not format_dropped
                        and _FORMAT_REFUSED_RX.search(err_body or "")):
                    format_dropped = True
                    log.warning("%s: %s refused the output format (HTTP %d); "
                                "asking once more without it: %s",
                                what, model_tag, e.code, err_body[:200])
                    payload = {k: v for k, v in payload.items() if k != "format"}
                    body = self._prepare(payload, strip_think=strip_think,
                                         stream=stream)
                    encoded = json.dumps(body).encode()
                    attempts.hold()
                    continue
                # A refusal naming the provider's real output maximum:
                # retry once at that value and remember it.
                if e.code in (400, 413, 422) and not ceiling_retried:
                    asked = (body.get("options") or {}).get("num_predict")
                    ceiling = _learn_ceiling(self.base_url, model_tag, err_body)
                    if ceiling and isinstance(asked, int) and asked > ceiling:
                        ceiling_retried = True
                        log.warning(
                            "%s: %s accepts at most %d output tokens (asked "
                            "%d); retrying once at the ceiling, which is now "
                            "remembered", what, model_tag, ceiling, asked)
                        body["options"]["num_predict"] = ceiling
                        encoded = json.dumps(body).encode()
                        attempts.hold()
                        continue

                fault = classify_http_fault(e.code, err_body)
                retryable = fault not in (MODEL_MISSING, TOOL_CALL_UNPARSABLE) and (
                    e.code in RETRYABLE_STATUS
                    or (400 <= e.code < 500
                        and any(s in err_body
                                for s in RETRYABLE_4XX_BODY_SUBSTRINGS))
                )
                if (fault != TOOL_CALL_UNPARSABLE
                        and self._wait_out_outage(e, err_body, attempts, outage,
                                                  what=what,
                                                  cancel_check=cancel_check,
                                                  on_wait=on_wait)):
                    continue
                if retryable and attempt < self.max_retries:
                    delay = min(
                        self.retry_base_delay_s * (2 ** attempt),
                        self.retry_max_delay_s,
                    )
                    asked_wait = (retry_after_s(getattr(e, "headers", None))
                                  if e.code in (429, 503) else None)
                    if asked_wait is not None:
                        delay = min(asked_wait, RETRY_AFTER_MAX_S)
                    log.warning(
                        "%s: HTTP %d on attempt %d/%d, "
                        "retrying in %.1fs%s (body: %s)",
                        what, e.code, attempt + 1, self.max_retries + 1, delay,
                        " as Retry-After asked" if asked_wait is not None else "",
                        err_body,
                    )
                    time.sleep(delay)
                    continue
                log.error("%s: HTTP %d (giving up, %s) body=%s",
                          what, e.code, fault, err_body)
                raise ChatFault(
                    f"{what} HTTP {e.code}: {err_body}",
                    fault_class=fault, status=e.code, body=err_body,
                    rejection=(tool_call_rejection(err_body)
                               if fault == TOOL_CALL_UNPARSABLE else None),
                ) from e
            except (urllib.error.URLError, OSError) as e:
                last_exc = e
                if self._wait_out_outage(e, "", attempts, outage,
                                         what=what, cancel_check=cancel_check,
                                         on_wait=on_wait):
                    continue
                if attempt < self.max_retries:
                    delay = min(
                        self.retry_base_delay_s * (2 ** attempt),
                        self.retry_max_delay_s,
                    )
                    log.warning(
                        "%s: %s on attempt %d/%d, retrying in %.1fs",
                        what, e, attempt + 1, self.max_retries + 1, delay,
                    )
                    time.sleep(delay)
                    continue
                log.error("%s: %s (giving up)", what, e)
                raise
        # Shouldn't reach here — the loop either returns or raises.
        if last_exc is not None:
            raise last_exc
        raise RuntimeError(f"{what}: no attempts made")

    def _consume_ndjson(self, resp,
                        *,
                        on_token: Optional[Callable[[str], None]],
                        cancel_check: Optional[Callable[[], bool]],
                        clock: Optional[dict] = None,
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
        from claude_hooks import reasoning_loop_guard as rlg

        content_parts: list[str] = []
        tool_calls: list[dict] = []
        last_msg_extra: dict = {}
        final_fields: dict = {}
        role = "assistant"
        guard = rlg.ReasoningLoopGuard()
        verdict = None

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
            if obj.get("error"):
                # A mid-stream failure: Ollama ends the stream with one
                # ``{"error": ...}`` line (a tool parser's error among
                # them), which used to be read past as a short reply.
                _raise_stream_error(obj, "".join(content_parts))
            msg = obj.get("message") or {}
            if msg:
                if "role" in msg:
                    role = msg["role"] or role
                delta = msg.get("content") or ""
                if delta:
                    content_parts.append(delta)
                tcs = msg.get("tool_calls")
                if tcs:
                    tool_calls.extend(tcs)
                # Reasoning streams in deltas exactly like content, so it
                # is accumulated the same way; other extra fields keep
                # their last value.
                reasoned = False
                for k, v in msg.items():
                    if k in ("role", "content", "tool_calls"):
                        continue
                    if k in _STREAMED_REASONING_KEYS and isinstance(v, str):
                        if v:
                            last_msg_extra[k] = last_msg_extra.get(k, "") + v
                            reasoned = True
                            verdict = guard.push(v)
                    else:
                        last_msg_extra[k] = v
                # ``on_token`` is the caller's liveness signal (the stall
                # monitor's only one). A reasoning model can think for
                # minutes before its first content token, and a tool-call
                # turn may carry no content at all — both are a working
                # model, not a silent one.
                if (clock is not None and "first" not in clock
                        and (delta or tcs or reasoned)):
                    clock["first"] = time.monotonic()
                if on_token is not None and (delta or tcs or reasoned):
                    try:
                        on_token(delta)
                    except Exception:  # pragma: no cover
                        log.exception("on_token raised; ignored")
            # Final record fields land on done=true.
            if obj.get("done"):
                for k in ("prompt_eval_count", "eval_count",
                         "total_duration", "load_duration",
                         "prompt_eval_duration", "eval_duration",
                         "done_reason",
                         # xOllama / think-budget builds: the enforced
                         # budget, and the prompt tokens served from cache.
                         "think_budget_tokens", "think_budget",
                         "cached_prompt_tokens"):
                    if k in obj:
                        final_fields[k] = obj[k]
            # A reasoning loop (plan D1): stop reading. Leaving the
            # ``with urlopen`` block closes the connection, which is what
            # stops the provider generating — and billing.
            if verdict is not None:
                break

        merged_message: dict = {"role": role,
                                "content": "".join(content_parts)}
        if tool_calls:
            merged_message["tool_calls"] = tool_calls
        merged_message.update(last_msg_extra)

        result = {"message": merged_message}
        result.update(final_fields)
        if verdict is not None:
            result["done"] = True
            result["done_reason"] = rlg.DONE_REASON
            result["reasoning_loop"] = verdict.public_dict()
        return result

    def _to_ollama(self, payload: dict, *, strip_think: bool = False) -> dict:
        body: dict = {
            "model": payload.get("model"),
            "messages": _ollama_messages(payload.get("messages") or []),
            "stream": False,
        }
        opts = dict(payload.get("options") or {})
        # The model's sampling template (config ``model_sampling``);
        # anything the caller set explicitly wins over it.
        from claude_hooks.model_sampling import sampling_for
        opts = {**sampling_for(body["model"] or ""), **opts}
        # An explicit None means "send nothing for this field": the way
        # a caller asks for the provider default despite a template.
        opts = {k: v for k, v in opts.items() if v is not None}
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
            think = normalize_think(payload["think"])
            if isinstance(think, int) and not isinstance(think, bool):
                # A token count: thinking on, the count as the budget
                # think-budget builds and xOllama enforce.
                body["think"] = True
                opts = dict(body.get("options") or {})
                opts.setdefault("think_budget", think)
                body["options"] = opts
            elif isinstance(think, str):
                body["think"] = self._accepted_level(body.get("model") or "",
                                                     think)
            else:
                body["think"] = think
        # Structured output: ``"json"`` or a JSON schema, passed through.
        if payload.get("format"):
            body["format"] = payload["format"]
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
        # Ollama's terminal signal is ``done_reason``: "stop" for a
        # natural end, "length" when generation was CUT because it hit
        # the output limit. This used to be hardcoded::
        #
        #     finish_reason = "tool_calls" if tool_calls else "stop"
        #
        # which destroyed the only evidence that an answer was
        # truncated. A cut completion became indistinguishable from a
        # finished one, so a half-written answer was stored, marked
        # ``status: completed`` with ``error: null``, and handed to the
        # user as final. That is exactly what happened to
        # csl-2026-08-12-0831-905a, whose xhigh adversarial audit ended
        # mid-token (``...count the **presence** of $\\text{``) after
        # 43 minutes and 2.8M prompt tokens.
        #
        # ``caliber_proxy/ollama.py`` has always done this correctly;
        # this translator — the one every consultants role uses — did
        # not. Two translators, one right.
        done_reason = data.get("done_reason") or "stop"
        # A tool-call turn keeps reporting "tool_calls" because the
        # agent loop branches on exactly that value to decide whether to
        # keep looping. The truncation signal is NOT folded into it —
        # it is carried alongside in ``done_reason`` so a detector can
        # act on a truncated tool-call turn (whose arguments may well be
        # half-written) without changing loop control flow.
        finish_reason = "tool_calls" if tool_calls else done_reason
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
                # Verbatim, always, even when finish_reason had to say
                # "tool_calls" for the loop's benefit.
                "done_reason": done_reason,
            }],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
            "done_reason": done_reason,
        }


# --------------------------------------------------------------------- #
# OpenAI-compatible llama.cpp-family servers (opencoti, llama.cpp)
# --------------------------------------------------------------------- #

#: Ollama option name -> llama.cpp wire name. Unset fields are never sent,
#: so a server that ignores a field sees the same body as before.
_LLAMACPP_OPTION_NAMES = {
    "temperature": "temperature", "top_p": "top_p", "top_k": "top_k",
    "min_p": "min_p", "typical_p": "typical_p",
    "repeat_penalty": "repeat_penalty", "repeat_last_n": "repeat_last_n",
    "presence_penalty": "presence_penalty",
    "frequency_penalty": "frequency_penalty", "seed": "seed",
    "stop": "stop", "num_keep": "n_keep",
    "mirostat": "mirostat", "mirostat_tau": "mirostat_tau",
    "mirostat_eta": "mirostat_eta",
    "think_budget_message": "reasoning_budget_message",
}


class OpenAICompatChatClient(ChatClient):
    """``/v1/chat/completions`` on opencoti or a plain llama.cpp server.

    Shares :class:`ChatClient`'s retry loop (Retry-After, empty-reply
    retry, learned ceiling, fault classes, timings): only the request
    body, the reply and the SSE stream are translated, into and out of
    the Ollama shape that loop already understands.

    ``think: false`` becomes ``reasoning_budget_tokens: 0`` on opencoti,
    the engine's own "no thinking" (it discards ``reasoning_effort``
    levels). Levels map to token budgets in Milestone B3; until then a
    level leaves the engine's default in force.
    """

    _chat_path = "/v1/chat/completions"
    # ``think`` is translated, never stripped, so there is nothing to ask.
    _probe_think_before_call = False

    def __init__(self, base_url: str, *, backend: str = "opencoti",
                 served_model: Optional[str] = None, **kwargs):
        base_url = base_url.rstrip("/")
        if base_url.endswith("/v1"):
            base_url = base_url[: -len("/v1")]
        super().__init__(base_url, **kwargs)
        self.backend = backend
        self.served_model = served_model

    # ---- capabilities ------------------------------------------------- #

    def caps(self, model: str, *, refresh: bool = False):
        from claude_hooks import provider_caps
        auth = ({"Authorization": f"Bearer {self.api_key}"}
                if self.api_key else None)
        return provider_caps.openai_compat_caps(
            self.base_url, self._real(model), headers=auth, refresh=refresh)

    def _probe_supports_think(self, model: str) -> Optional[bool]:
        """No ``/api/show`` here: the window comes from ``/props``. The
        think verdict stays unknown, so ``think`` is never stripped on a
        guess."""
        if model in self._probed_think:
            return self._probed_think[model]
        caps = self.caps(model)
        self._context_length[model] = caps.session_ctx_max or caps.declared_num_ctx
        self._budget_message[model] = None
        self._probed_think[model] = None
        return None

    # ---- request ------------------------------------------------------ #

    def _prepare(self, payload: dict, *, strip_think: bool,
                 stream: bool) -> dict:
        model = payload.get("model") or ""
        body: dict = {"model": model,
                      "messages": single_system_messages(
                          list(payload.get("messages") or [])),
                      "stream": bool(stream)}
        from claude_hooks.model_sampling import sampling_for
        opts = {**sampling_for(model), **dict(payload.get("options") or {})}
        opts = {k: v for k, v in opts.items() if v is not None}
        n_predict = opts.get("num_predict")
        if isinstance(n_predict, int) and n_predict > 0:
            ceiling = learned_output_ceiling(self.base_url, model)
            body["max_tokens"] = min(n_predict, ceiling) if ceiling else n_predict
        for src, dst in _LLAMACPP_OPTION_NAMES.items():
            if src in opts and opts[src] not in ("", []):
                body[dst] = opts[src]
        if payload.get("tools"):
            body["tools"] = payload["tools"]
        if "tool_choice" in payload:
            body["tool_choice"] = payload["tool_choice"]
        think = normalize_think(payload.get("think"))
        if self.backend == "opencoti" and not strip_think:
            if think is False:
                body["reasoning_budget_tokens"] = 0
            else:
                # The engine discards ``reasoning_effort``; a level is
                # turned into tokens with Ollama's fractions (plan B3).
                tb = think_budget_tokens(
                    think, body.get("max_tokens"),
                    self._context_length.get(model))
                if tb is not None:
                    body["reasoning_budget_tokens"] = tb
        fmt = payload.get("format")
        if fmt == "json":
            body["response_format"] = {"type": "json_object"}
        elif isinstance(fmt, dict):
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "result", "schema": fmt}}
        if stream:
            body["stream_options"] = {"include_usage": True}
        return body

    # ---- reply -------------------------------------------------------- #

    @staticmethod
    def _ollama_shape(message: dict, finish_reason: Optional[str],
                      usage: Optional[dict], timings: Optional[dict]) -> dict:
        out: dict = {
            "message": message,
            "done": True,
            "done_reason": (finish_reason
                            if finish_reason in ("length", "reasoning_loop")
                            else "stop"),
        }
        usage = usage or {}
        if usage.get("prompt_tokens") is not None:
            out["prompt_eval_count"] = int(usage["prompt_tokens"])
        if usage.get("completion_tokens") is not None:
            out["eval_count"] = int(usage["completion_tokens"])
        t = timings or {}
        for src, dst in (("prompt_ms", "prompt_eval_duration"),
                         ("predicted_ms", "eval_duration")):
            if isinstance(t.get(src), (int, float)):
                out[dst] = float(t[src]) * 1e6
        if isinstance(t.get("cache_n"), int):
            out["cached_prompt_tokens"] = t["cache_n"]
        if "prompt_eval_count" not in out and isinstance(t.get("prompt_n"), int):
            out["prompt_eval_count"] = t["prompt_n"] + int(t.get("cache_n") or 0)
        if "eval_count" not in out and isinstance(t.get("predicted_n"), int):
            out["eval_count"] = t["predicted_n"]
        return out

    def _decode(self, data: dict) -> dict:
        choices = data.get("choices") or [{}]
        ch = choices[0] or {}
        msg = dict(ch.get("message") or {})
        msg.setdefault("role", "assistant")
        if msg.get("content") is None:
            msg["content"] = ""
        return self._ollama_shape(msg, ch.get("finish_reason"),
                                  data.get("usage"), data.get("timings"))

    def _consume_ndjson(self, resp, *,
                        on_token: Optional[Callable[[str], None]],
                        cancel_check: Optional[Callable[[], bool]],
                        clock: Optional[dict] = None) -> dict:
        """Read an OpenAI SSE stream (``data: {...}`` lines, ``[DONE]``).
        Tool-call deltas are merged by ``index``; their argument
        fragments are concatenated. A comment line (``: ping``) is the
        engine's keepalive and counts as liveness."""
        from consultants.engine.stall import CancelledByOrchestrator
        from claude_hooks import reasoning_loop_guard as rlg

        content: list[str] = []
        reasoning: list[str] = []
        calls: dict[int, dict] = {}
        finish: Optional[str] = None
        usage: Optional[dict] = None
        timings: Optional[dict] = None
        guard = rlg.ReasoningLoopGuard()
        verdict = None
        for raw in resp:
            if verdict is not None:
                # Plan D1: stop reading; closing the connection stops
                # the server generating.
                break
            if cancel_check is not None and cancel_check():
                raise CancelledByOrchestrator()
            line = (raw.decode("utf-8", errors="replace")
                    if isinstance(raw, bytes) else raw).strip()
            if not line:
                continue
            if line.startswith(":"):
                if on_token is not None:
                    try:
                        on_token("")
                    except Exception:  # pragma: no cover
                        log.exception("on_token raised; ignored")
                continue
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                log.warning("openai stream: skipping malformed line: %r",
                            data[:80])
                continue
            if isinstance(obj.get("error"), dict) or obj.get("error"):
                _raise_stream_error(obj, "".join(content))
            if obj.get("usage"):
                usage = obj["usage"]
            if obj.get("timings"):
                timings = obj["timings"]
            for ch in obj.get("choices") or []:
                delta = ch.get("delta") or {}
                piece = delta.get("content") or ""
                think = delta.get("reasoning_content") or delta.get("reasoning") or ""
                tcs = delta.get("tool_calls") or []
                if piece:
                    content.append(piece)
                if think:
                    reasoning.append(think)
                    verdict = guard.push(think)
                for tc in tcs:
                    i = int(tc.get("index", len(calls)))
                    slot = calls.setdefault(i, {"id": None, "type": "function",
                                                "function": {"name": "",
                                                             "arguments": ""}})
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["function"]["name"] += fn["name"]
                    if fn.get("arguments"):
                        slot["function"]["arguments"] += fn["arguments"]
                if ch.get("finish_reason"):
                    finish = ch["finish_reason"]
                if piece or think or tcs:
                    if clock is not None and "first" not in clock:
                        clock["first"] = time.monotonic()
                    if on_token is not None:
                        try:
                            on_token(piece)
                        except Exception:  # pragma: no cover
                            log.exception("on_token raised; ignored")
        msg: dict = {"role": "assistant", "content": "".join(content)}
        if reasoning:
            msg["reasoning_content"] = "".join(reasoning)
        if calls:
            msg["tool_calls"] = [
                {**c, "id": c["id"] or f"tc_{i}"} for i, c in sorted(calls.items())]
        if verdict is not None:
            out = self._ollama_shape(msg, rlg.DONE_REASON, usage, timings)
            out["reasoning_loop"] = verdict.public_dict()
            return out
        return self._ollama_shape(msg, finish, usage, timings)


def parse_endpoint_ref(model_ref: str) -> Optional[tuple[str, Optional[str]]]:
    """``@name`` -> ``(name, None)``; ``@name/model`` -> ``(name, model)``;
    anything else -> ``None``. ``@`` never occurs in an Ollama tag."""
    if not model_ref or not model_ref.startswith("@"):
        return None
    name, _, model = model_ref[1:].partition("/")
    return (name, model or None) if name else None


def served_model_of(base_url: str, *, api_key: Optional[str] = None,
                    timeout: float = 10.0) -> Optional[str]:
    """The first model id an OpenAI-compatible server lists, for an
    endpoint reference that names none."""
    from claude_hooks import provider_caps
    root = base_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[: -len("/v1")]
    hdrs = {"Authorization": f"Bearer {api_key}"} if api_key else None
    try:
        status, body = provider_caps._request(f"{root}/v1/models",
                                              headers=hdrs, timeout=timeout)
    except (urllib.error.URLError, OSError):
        return None
    if status != 200 or not isinstance(body, dict):
        return None
    for item in body.get("data") or []:
        if isinstance(item, dict) and item.get("id"):
            return str(item["id"])
    return None


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
            "messages": single_system_messages(
                list(payload.get("messages") or [])),
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
    *,
    endpoints: Optional[dict] = None,
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
        # A local llamafile has no WAN line to wait out.
        kwargs.pop("outage_wait_s", None)
        return LlamafileAgentChatClient(label, **kwargs)
    ref = parse_endpoint_ref(model_ref)
    if ref is not None:
        name, model = ref
        ep = (endpoints or {}).get(name)
        if ep is None:
            raise ValueError(
                f"model {model_ref!r} names endpoint {name!r}, which is not "
                f"configured (known: {', '.join(sorted(endpoints or {})) or 'none'})")
        url = _ep_field(ep, "url")
        backend = (_ep_field(ep, "backend") or "opencoti").lower()
        api_key = endpoint_api_key(ep)
        served = model or _ep_field(ep, "model")
        if backend in ("ollama", "xollama"):
            client = ChatClient(url, api_key=api_key, **kwargs)
            client.served_model = served
            return client
        client = OpenAICompatChatClient(url, backend=backend,
                                        api_key=api_key, **kwargs)
        client.served_model = served or served_model_of(url, api_key=api_key) \
            or name
        return client
    return ChatClient(ollama_base_url, **kwargs)


def route_key(model_ref: str) -> str:
    """Which client serves ``model_ref``: one per ``llamafile://`` label,
    one per endpoint reference, one shared Ollama client for the rest."""
    if model_ref and model_ref.startswith("llamafile://"):
        return model_ref
    if parse_endpoint_ref(model_ref) is not None:
        return model_ref
    return "ollama"


class ModelRoutingChatClient:
    """One role's client when its models live on more than one backend.

    Fan-out lanes reuse their role's client and only change ``model`` in
    the payload, so a role whose extra models include an endpoint
    reference or a ``llamafile://`` label needs each call routed by the
    payload's model. Built only for such roles: a role whose models all
    share a backend keeps the plain client it always had.

    Per-call readouts (``last_usage``, ``last_timings``,
    ``last_inference_s``) come from the client that served the last
    call; ``total_inference_s`` is the sum over all of them.
    """

    def __init__(self, primary: str, factory: Callable[[str], Any]):
        self._factory = factory
        self._clients: dict[str, Any] = {}
        self._primary = primary
        self._last = self._for(primary)

    def _for(self, model: Optional[str]):
        key = route_key(model or self._primary)
        client = self._clients.get(key)
        if client is None:
            client = self._factory(model or self._primary)
            self._clients[key] = client
        return client

    def chat(self, payload: dict, *args, **kwargs) -> dict:
        self._last = self._for((payload or {}).get("model"))
        return self._last.chat(payload, *args, **kwargs)

    def chat_streamed(self, payload: dict, *args, **kwargs) -> dict:
        self._last = self._for((payload or {}).get("model"))
        return self._last.chat_streamed(payload, *args, **kwargs)

    def context_length(self, model: str):
        return self._for(model).context_length(model)

    def thinking_budget_message(self, model: str):
        return self._for(model).thinking_budget_message(model)

    def caps(self, model: str, **kw):
        return self._for(model).caps(model, **kw)

    def learned_output_ceiling(self, model: str):
        return self._for(model).learned_output_ceiling(model)

    def reset_inference_timer(self) -> None:
        for c in self._clients.values():
            if hasattr(c, "reset_inference_timer"):
                c.reset_inference_timer()

    @property
    def total_inference_s(self) -> float:
        return sum(getattr(c, "total_inference_s", 0.0)
                   for c in self._clients.values())

    def __getattr__(self, name: str):
        return getattr(self._last, name)


def make_role_chat_client(models: list[str], ollama_base_url: str, *,
                          endpoints: Optional[dict] = None, **kwargs):
    """The client for a role whose primary model is ``models[0]`` and
    whose fan-out lanes may use the rest: a plain client when they all
    share a backend, a :class:`ModelRoutingChatClient` otherwise."""
    models = [m for m in models if m] or [""]
    def factory(m: str):
        return make_agent_chat_client(m, ollama_base_url,
                                      endpoints=endpoints, **kwargs)
    if len({route_key(m) for m in models}) <= 1:
        return factory(models[0])
    return ModelRoutingChatClient(models[0], factory)


def endpoint_api_key(ep) -> Optional[str]:
    """An endpoint's key: its ``key_file``, else a legacy inline
    ``api_key``; ``None`` lets an ollama.com endpoint fall back to the
    host's Ollama Cloud key (``ollama_upstream.api_key``)."""
    path = _ep_field(ep, "key_file")
    if path:
        from claude_hooks import ollama_upstream
        key = ollama_upstream.read_key_file(str(path))
        if key:
            return key
        log.warning("endpoint key_file %s is unreadable or empty", path)
    return _ep_field(ep, "api_key") or None


def _ep_field(ep, key: str):
    if isinstance(ep, dict):
        return ep.get(key)
    return getattr(ep, key, None)
