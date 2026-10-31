"""Token accounting that measures instead of assuming.

Ported from the Cline fork at ``/shared/dev/cline``
(``sdk/packages/shared/src/llms/tokens.ts``), whose constants and bounds
were derived from live measurement rather than taste. The rationale is
kept because the numbers are worthless without it — someone who reads
``2.7`` with no explanation will eventually "simplify" it to 4.

**Two ratios, not one.** A single chars-per-token figure for a whole
request is a weighted average over two populations that do not tokenize
alike. A serialized request is mostly JSON, code and tool output —
punctuation, escapes, identifiers — measured on this workload at 3.8 to
4.4 chars/token. *Reasoning* is prose the model wrote for itself, and
runs near 2.7. Averaging them works exactly as long as the mix holds
still, and it does not: in a live transcript, reasoning moved between
32% and 61% of the content, every turn.

Each departure from the average becomes error, and the error is not
symmetric in consequence. A reasoning-heavy request is **undercounted**,
which is the direction that lets a request be built too large. So the
two are counted apart.

**Measured beats conservative.** The starting ratio deliberately
over-counts (3 rather than the conventional 4) so a trigger fires before
the provider rejects the request. But over-counting is only safe in one
direction: it makes the compaction trigger fire early (safe) and it
*shrinks* the remaining-context term that caps output (not safe). That
term is a difference of two large numbers, so it amplifies whatever
error the approximation carries — in the fork's measurements a 0.14%
estimate error became an 18.4% error in the cap. Safety margin belongs
in the thresholds that state it explicitly, not in the approximation
every caller reads. Hence calibration: the first real
``prompt_eval_count`` replaces the guess.

**Why the bounds are wide.** An observed ratio outside
``[1.2, 16]`` says the *measurement* went wrong, not the content — a
truncated request, a provider counting something other than the prompt.
The ceiling has to clear what a large-vocabulary tokenizer does to
serialized JSON: measured on Gemma-4 (262k vocab), 645,803 characters
for 78,138 prompt tokens, a ratio of **8.26**. An earlier ceiling of 8
discarded that observation and every later one, so the ratio stayed
frozen while the estimate it fed ran 1.7x high.

**Calibration is per (session, model).** The fork keys none of this — its sessions
run one model at a time, so a single global ratio describes whatever is
loaded. The council does not: x-tier fanout runs N models concurrently
in one process, and gemma4 (262k vocab, measured at 8.26 chars/token on
serialized JSON) shares nothing useful with a 32k-vocab model. A blended
ratio would describe neither and would move every time the lane mix
changed. So every entry point takes a ``model`` and the state is a dict
keyed by it.

Within one model a second key matters: the *session* — one lane's tool
loop, one single-shot call and its continuations. The last counted
request is an anchor for the next estimate of **that** conversation;
read by a sibling lane on the same model it is a count for a different
prompt, and the fork measured exactly that contamination
(``a8d4ae171``, ``4adbd6182``). So a caller that names a ``session``
gets its own anchor and its own ratio, and the model-level ratio — fed
by every session of that model — is only the cold-start prior a new
session reads until it has measured something itself. A caller that
names no session gets the model-level state, as before.

**Three guards on an observation** (the fork's ``observeRequestTokens``):

* a count larger than the context window is dropped, count and ratio
  both — measured 166,848 "prompt tokens" against a 131,072 window
  (``2403f40e8``);
* a ratio under 1.2 chars/token drops the count too, not only the
  ratio — a token per character is not a prompt, it is a provider
  counting something else (138,549 tokens for 138,262 characters,
  ``c61cb3600``); a ratio over 16 still keeps the count;
* when reasoning leaves less than 20% of the tokens to the rest of the
  request (``RESIDUAL_SIGNAL_SHARE``), the anchor is kept but the ratio
  is not updated: the remainder is too small a signal to divide by.

State is module-global and lock-guarded. The TypeScript original needs
``Symbol.for`` to survive a bundler that produces several copies of the
module; Python's import system interns modules by name, so plain module
scope is the same guarantee. Note the consequence for **hooks**: they
are short-lived processes, so each one starts uncalibrated and runs on
the conservative default. Only the long-lived consultants engine
accumulates measurements.
"""

from __future__ import annotations

import json
import logging
import math
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Optional

log = logging.getLogger("claude_hooks.token_calib")

#: Starting ratio for everything that is not reasoning. Over-counts on
#: purpose; replaced by measurement at the first real response.
CHARS_PER_TOKEN = 3.0

#: Starting ratio for reasoning text, which is denser prose.
THINKING_CHARS_PER_TOKEN = 2.7

#: A ratio outside this range is a broken measurement, not dense
#: content. See the module docstring for why the ceiling is 16.
MIN_OBSERVED_CHARS_PER_TOKEN = 1.2
MAX_OBSERVED_CHARS_PER_TOKEN = 16.0

#: Weight of the newest observation once a baseline exists. The first
#: observation is taken whole — the default it replaces is a guess, not
#: a measurement.
OBSERVATION_WEIGHT = 0.3

#: When reasoning leaves less than this share of a request's tokens to
#: everything else, the remainder is too small to calibrate on: the
#: anchor is kept, the ratio is not moved (fork ``RESIDUAL_SIGNAL_SHARE``).
RESIDUAL_SIGNAL_SHARE = 0.2

#: A turn counts as "mostly reasoning" — and may calibrate the reasoning
#: ratio from its output — only above this share of what it produced
#: (fork ``MOSTLY_REASONING_SHARE``, ``gateway.ts:52``). At 0.6 a turn
#: that was 40% tool-call JSON still taught the reasoning ratio.
MOSTLY_REASONING_SHARE = 0.8

#: Sessions are cheap and short-lived (one per tool loop, one per
#: single-shot call), and nothing is obliged to release them; the
#: oldest are dropped beyond this many so a long-lived engine does not
#: accumulate one record per call it ever made.
MAX_SESSIONS = 512

#: Reasoning-part type names. The runtime calls it ``reasoning``; Ollama
#: and the stored transcript call it ``thinking``. A filter that knew
#: only one would silently do nothing on the other — not a visible
#: failure, just an estimate quietly back to counting what is never sent.
REASONING_KEYS = ("thinking", "reasoning", "reasoning_content",
                  "redacted_thinking")

_lock = threading.RLock()


@dataclass
class _Calibration:
    chars_per_token: Optional[float] = None
    thinking_chars_per_token: Optional[float] = None
    request_tokens: Optional[int] = None
    #: The anchor's own measurement: the characters (and the reasoning
    #: share of them) of the request ``request_tokens`` was counted for.
    request_chars: Optional[int] = None
    request_reasoning_chars: int = 0


#: One calibration per model. ``""`` is the entry for a caller that
#: does not name one, which keeps the API usable without making the
#: unkeyed case silently share a bucket with a real model.
_states: dict[str, _Calibration] = {}


#: One calibration per (session, model), LRU-bounded at MAX_SESSIONS.
_sessions: "OrderedDict[tuple[str, str], _Calibration]" = OrderedDict()


def _for(model: Optional[str]) -> _Calibration:
    key = model or ""
    with _lock:
        state = _states.get(key)
        if state is None:
            state = _Calibration()
            _states[key] = state
        return state


def _session_for(model: Optional[str],
                 session: Optional[str]) -> _Calibration:
    """The session's own state, or the model-level one without a session."""
    if not session:
        return _for(model)
    key = (str(session), model or "")
    with _lock:
        state = _sessions.get(key)
        if state is None:
            state = _Calibration()
            _sessions[key] = state
            while len(_sessions) > MAX_SESSIONS:
                _sessions.popitem(last=False)
        else:
            _sessions.move_to_end(key)
        return state


def release_session(session: str) -> None:
    """Forget a finished session's anchor and ratios, for every model.

    Optional — the LRU bound is the backstop — but a caller that knows
    its conversation is over should say so.
    """
    with _lock:
        for key in [k for k in _sessions if k[0] == session]:
            _sessions.pop(key, None)


# --------------------------------------------------------------------- #
# Output-cap attribution
# --------------------------------------------------------------------- #

#: Which term set the output cap on a request.
#:
#: ``requested`` is the caller's own limit (our ``num_predict``),
#: ``model-max-output`` the model's declared ceiling, ``default`` a
#: synthesized fallback — none of which compaction can move.
#: ``remaining-context`` and ``context-overflow`` are the window: the
#: room left after the prompt, which is exactly what compaction makes.
OUTPUT_CAP_SOURCES = (
    "requested", "default", "model-max-output",
    "remaining-context", "context-overflow", "uncapped",
)

#: The sources that compaction can actually do something about.
WINDOW_BOUND_SOURCES = ("remaining-context", "context-overflow")


@dataclass(frozen=True)
class OutputCapReport:
    """What limited a reply, recorded for whoever has to react to it.

    A turn cut off at its output cap looks identical either way — same
    ``done_reason``, same half-written message — but the two causes want
    opposite responses. A **window-bound** cap is compaction's to fix. A
    cap that came from the request or the model is not: shrinking the
    transcript cannot raise it, so compacting there spends the
    transcript to change nothing, and the retry faces the same ceiling
    with less of the work it was doing.
    """
    source: str
    max_tokens: Optional[int] = None

    @property
    def window_bound(self) -> bool:
        return self.source in WINDOW_BOUND_SOURCES

    def public_dict(self) -> dict:
        return {"source": self.source, "max_tokens": self.max_tokens,
                "window_bound": self.window_bound}


@dataclass(frozen=True)
class ContextOverflowReport:
    """The budget arithmetic having already failed for a request that
    was about to go out.

    Every other trigger is a projection — a character count, a ratio, a
    share held back against the ratio being wrong. This one is not, which
    is what makes it worth having *alongside* the estimate-driven trigger
    rather than instead of it. A window whose remaining room will not
    hold a minimum reply is full, whether or not any ratio agreed.
    """
    context_window: int
    estimated_input_tokens: int
    remaining_context: int
    min_output_tokens: int

    def public_dict(self) -> dict:
        return {
            "context_window": self.context_window,
            "estimated_input_tokens": self.estimated_input_tokens,
            "remaining_context": self.remaining_context,
            "min_output_tokens": self.min_output_tokens,
        }


# --------------------------------------------------------------------- #
# Ratios
# --------------------------------------------------------------------- #

def chars_per_token(model: str = "", session: Optional[str] = None) -> float:
    """The session's measured ratio, else the model's, else the default."""
    if session:
        own = _session_for(model, session).chars_per_token
        if own:
            return own
    return _for(model).chars_per_token or CHARS_PER_TOKEN


def thinking_chars_per_token(model: str = "",
                             session: Optional[str] = None) -> float:
    if session:
        own = _session_for(model, session).thinking_chars_per_token
        if own:
            return own
    return _for(model).thinking_chars_per_token or THINKING_CHARS_PER_TOKEN


def estimate_tokens(chars: int, model: str = "",
                    session: Optional[str] = None) -> int:
    if chars <= 0:
        return 0
    return max(1, int(chars / chars_per_token(model, session)) + 1)


def estimate_thinking_tokens(chars: int, model: str = "",
                             session: Optional[str] = None) -> int:
    if chars <= 0:
        return 0
    return max(1, int(chars / thinking_chars_per_token(model, session)) + 1)


def _blend(current: Optional[float], ratio: float) -> float:
    if current is None:
        return ratio
    return current * (1 - OBSERVATION_WEIGHT) + ratio * OBSERVATION_WEIGHT


def observe_thinking_tokens(chars: int, tokens: int,
                            model: str = "",
                            session: Optional[str] = None) -> None:
    """Calibrate the reasoning ratio from a turn's own output.

    The evidence is already in the response: a completed turn carries
    the provider's output-token count alongside the characters it
    produced. Only turns that are *mostly* reasoning should be passed —
    the caller decides, since it holds the message — because a turn that
    is mostly a tool call would teach this ratio about JSON.
    """
    if chars <= 0 or tokens <= 0:
        return
    ratio = chars / tokens
    if not (MIN_OBSERVED_CHARS_PER_TOKEN <= ratio
            <= MAX_OBSERVED_CHARS_PER_TOKEN):
        return
    with _lock:
        for state in _targets(model, session):
            state.thinking_chars_per_token = _blend(
                state.thinking_chars_per_token, ratio)


def _targets(model: Optional[str],
             session: Optional[str]) -> list[_Calibration]:
    """Where a ratio observation lands: the session, and the model-level
    prior every new session of that model starts from."""
    if not session:
        return [_for(model)]
    return [_session_for(model, session), _for(model)]


def observe_request_tokens(chars: int, tokens: int,
                           reasoning_chars: int = 0,
                           model: str = "", *,
                           session: Optional[str] = None,
                           context_window: Optional[int] = None) -> None:
    """Record that a request of ``chars`` cost ``tokens`` input tokens.

    The fork's ``observeRequestTokens``, step for step:

    1. a count larger than ``context_window`` is dropped — count and
       ratio — because no prompt that fits a window costs more than it;
    2. with ``reasoning_chars`` known, the ratio describes the *rest* of
       the request: reasoning is charged at its own rate first, so the
       two halves of the estimate never account for the same characters;
    3. when that leaves under ``RESIDUAL_SIGNAL_SHARE`` of the tokens to
       the rest, the count is kept as the anchor and the ratio is not
       moved;
    4. a ratio under the floor drops the count as well: a request that
       cost a token per character was not measured, something else was;
    5. otherwise the count becomes the anchor, and a ratio over the
       ceiling is refused while the count stays — ``tokens`` is what the
       provider counted, and only the pairing with ``chars`` is suspect.

    The anchor (count + the characters it was counted for) lands on the
    session when one is named, never on the model-level state a sibling
    lane would read. The ratio lands on both.
    """
    if chars <= 0 or tokens <= 0:
        return
    if context_window and context_window > 0 and tokens > context_window:
        log.debug("token count %d exceeds the %d window; dropped",
                  tokens, context_window)
        return
    tcpt = thinking_chars_per_token(model, session)
    reasoning = max(0, min(int(reasoning_chars or 0), chars))
    ratio = chars / tokens
    residual = tokens
    if reasoning > 0 and tcpt > 0:
        residual = tokens - math.ceil(reasoning / tcpt)
        if reasoning < chars and residual > 0:
            ratio = (chars - reasoning) / residual
    anchor = _session_for(model, session)
    if reasoning > 0 and residual < RESIDUAL_SIGNAL_SHARE * tokens:
        _store_anchor(anchor, chars, tokens, reasoning)
        return
    if ratio < MIN_OBSERVED_CHARS_PER_TOKEN:
        return
    _store_anchor(anchor, chars, tokens, reasoning)
    if ratio > MAX_OBSERVED_CHARS_PER_TOKEN:
        return
    with _lock:
        for state in _targets(model, session):
            state.chars_per_token = _blend(state.chars_per_token, ratio)


def _store_anchor(state: _Calibration, chars: int, tokens: int,
                  reasoning: int) -> None:
    with _lock:
        state.request_tokens = int(tokens)
        state.request_chars = int(chars)
        state.request_reasoning_chars = int(reasoning)


def last_observed_request_tokens(model: str = "",
                                 session: Optional[str] = None
                                 ) -> Optional[int]:
    """What the last request actually cost, per the provider.

    Unlike an estimate this cannot be wrong — it is what was counted —
    but it describes the *previous* request, so a caller trades
    exactness for being one turn behind.
    """
    return _session_for(model, session).request_tokens


def anchored_request_tokens(chars: int, reasoning_chars: int = 0,
                            model: str = "",
                            session: Optional[str] = None) -> int:
    """Project the next request's cost from the last one counted.

    The fork's ``anchoredRequestTokens``: a conversation's next request is
    the last one plus whatever was added since, so its cost is the last
    count plus the *added* characters at the anchor's own measured ratio
    — not the whole request re-estimated from an average. Reasoning is
    split out when either side has any, so a turn that dropped or added
    a thinking block moves the estimate at the reasoning rate.

    Without an anchor this is the plain split estimate.
    """
    reasoning = min(chars, max(0, int(reasoning_chars or 0)))
    state = _session_for(model, session)
    anchor_tokens = state.request_tokens
    anchor_chars = state.request_chars
    if (not anchor_tokens or not anchor_chars or anchor_tokens <= 0
            or anchor_chars <= 0):
        return _plain_split(chars, reasoning, model, session)
    delta = chars - anchor_chars
    if delta == 0:
        blended = anchor_tokens
    else:
        blended = max(1, round(anchor_tokens
                               + delta / (anchor_chars / anchor_tokens)))
    tcpt = thinking_chars_per_token(model, session)
    anchor_reasoning = state.request_reasoning_chars or 0
    if tcpt <= 0 or (anchor_reasoning <= 0 and reasoning <= 0):
        return blended
    anchor_content_tokens = anchor_tokens - anchor_reasoning / tcpt
    anchor_content_chars = anchor_chars - anchor_reasoning
    if anchor_content_tokens <= 0 or anchor_content_chars <= 0:
        return blended
    content_ratio = anchor_content_chars / anchor_content_tokens
    content_delta = chars - reasoning - anchor_content_chars
    return max(1, round(anchor_content_tokens + content_delta / content_ratio
                        + reasoning / tcpt))


def _plain_split(chars: int, reasoning: int, model: str,
                 session: Optional[str]) -> int:
    if reasoning <= 0:
        return estimate_tokens(chars, model, session)
    return (estimate_tokens(chars - reasoning, model, session)
            + estimate_thinking_tokens(reasoning, model, session))


def reset_calibration(model: Optional[str] = None) -> None:
    """Drop measurements — one model's, or every model's.

    Tests must call this between cases: the state outlives a single
    request by design, which is exactly what makes one test's fake token
    counts able to skew the next test's estimate.
    """
    with _lock:
        if model is None:
            _states.clear()
            _sessions.clear()
        else:
            _states.pop(model, None)
            for key in [k for k in _sessions if k[1] == model]:
                _sessions.pop(key, None)


# --------------------------------------------------------------------- #
# Measuring a request
# --------------------------------------------------------------------- #

def _safe_dumps(value: Any) -> str:
    try:
        return json.dumps(value, default=str, ensure_ascii=False)
    except Exception:
        return str(value)


def _message_reasoning_chars(message: dict) -> int:
    chars = 0
    for key in REASONING_KEYS:
        value = message.get(key)
        if isinstance(value, str):
            chars += len(value)
    content = message.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") in REASONING_KEYS:
                chars += len(_safe_dumps(block))
    return chars


def measure_request_chars(messages: list[dict], *,
                          system: Optional[str] = None,
                          tools: Optional[list] = None) -> tuple[int, int]:
    """Serialized size of a request, and how much of it is reasoning.

    Both measured the same way — serialized — so the two counts are in
    the same unit and one can be subtracted from the other without the
    remainder quietly meaning something else.
    """
    total = len(_safe_dumps({"system": system, "messages": messages,
                             "tools": tools}))
    reasoning = sum(_message_reasoning_chars(m) for m in (messages or [])
                    if isinstance(m, dict))
    return total, min(reasoning, total)


def estimate_request_tokens(messages: list[dict], *,
                            system: Optional[str] = None,
                            tools: Optional[list] = None,
                            model: str = "",
                            session: Optional[str] = None) -> int:
    """Estimate a request's prompt cost, counting reasoning apart.

    Tool schemas are part of the request and are counted: the provider's
    count includes them, so an estimate without them is low by exactly
    the part that is the same on every call. With a ``session`` the
    estimate is anchored on that session's last counted request.
    """
    chars, reasoning = measure_request_chars(
        messages, system=system, tools=tools)
    if session:
        return anchored_request_tokens(chars, reasoning, model, session)
    return _plain_split(chars, reasoning, model, None)


__all__ = [
    "CHARS_PER_TOKEN", "THINKING_CHARS_PER_TOKEN", "REASONING_KEYS",
    "RESIDUAL_SIGNAL_SHARE", "MOSTLY_REASONING_SHARE", "MAX_SESSIONS",
    "release_session", "anchored_request_tokens",
    "OUTPUT_CAP_SOURCES", "WINDOW_BOUND_SOURCES",
    "OutputCapReport", "ContextOverflowReport",
    "chars_per_token", "thinking_chars_per_token",
    "estimate_tokens", "estimate_thinking_tokens",
    "observe_thinking_tokens", "observe_request_tokens",
    "last_observed_request_tokens", "reset_calibration",
    "measure_request_chars", "estimate_request_tokens",
]
