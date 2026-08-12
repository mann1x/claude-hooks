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

**Calibration is per model.** The fork keys none of this — its sessions
run one model at a time, so a single global ratio describes whatever is
loaded. The council does not: x-tier fanout runs N models concurrently
in one process, and gemma4 (262k vocab, measured at 8.26 chars/token on
serialized JSON) shares nothing useful with a 32k-vocab model. A blended
ratio would describe neither and would move every time the lane mix
changed. So every entry point takes a ``model`` and the state is a dict
keyed by it.

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
import threading
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
    output_cap: Optional["OutputCapReport"] = None
    context_overflow: Optional["ContextOverflowReport"] = None


#: One calibration per model. ``""`` is the entry for a caller that
#: does not name one, which keeps the API usable without making the
#: unkeyed case silently share a bucket with a real model.
_states: dict[str, _Calibration] = {}


def _for(model: Optional[str]) -> _Calibration:
    key = model or ""
    with _lock:
        state = _states.get(key)
        if state is None:
            state = _Calibration()
            _states[key] = state
        return state


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


def note_output_cap(report: OutputCapReport, model: str = "") -> None:
    """Record which term capped the reply on the request going out.

    Read rather than consumed, unlike an overflow report: this is a
    standing fact about the last request, overwritten by the next. A
    reader asking "was the cap that just truncated this turn the
    window's?" wants the answer to survive being asked.
    """
    _for(model).output_cap = report


def last_output_cap(model: str = "") -> Optional[OutputCapReport]:
    return _for(model).output_cap


def note_context_overflow(report: ContextOverflowReport,
                          model: str = "") -> None:
    _for(model).context_overflow = report


def consume_context_overflow(
        model: str = "") -> Optional[ContextOverflowReport]:
    """Take the pending overflow report and clear it.

    Consumed rather than read so a single overflow forces a single
    compaction: left set, it would force one on every following turn,
    including the ones the compaction it triggered already made room for.
    """
    state = _for(model)
    with _lock:
        report = state.context_overflow
        state.context_overflow = None
        return report


# --------------------------------------------------------------------- #
# Ratios
# --------------------------------------------------------------------- #

def chars_per_token(model: str = "") -> float:
    return _for(model).chars_per_token or CHARS_PER_TOKEN


def thinking_chars_per_token(model: str = "") -> float:
    return _for(model).thinking_chars_per_token or THINKING_CHARS_PER_TOKEN


def estimate_tokens(chars: int, model: str = "") -> int:
    if chars <= 0:
        return 0
    return max(1, int(chars / chars_per_token(model)) + 1)


def estimate_thinking_tokens(chars: int, model: str = "") -> int:
    if chars <= 0:
        return 0
    return max(1, int(chars / thinking_chars_per_token(model)) + 1)


def _blend(current: Optional[float], ratio: float) -> float:
    if current is None:
        return ratio
    return current * (1 - OBSERVATION_WEIGHT) + ratio * OBSERVATION_WEIGHT


def observe_thinking_tokens(chars: int, tokens: int,
                            model: str = "") -> None:
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
    state = _for(model)
    with _lock:
        state.thinking_chars_per_token = _blend(
            state.thinking_chars_per_token, ratio)


def observe_request_tokens(chars: int, tokens: int,
                           reasoning_chars: int = 0,
                           model: str = "") -> None:
    """Record that a request of ``chars`` cost ``tokens`` input tokens.

    The count and the ratio are recorded **independently**, because only
    one of them can be wrong. ``tokens`` is what the provider counted for
    the request that just ran; nothing about the character measurement
    can make that untrue. The ratio pairs it with a character count, and
    a mismatched pairing is what the bounds reject — so a rejected ratio
    must not take the count down with it. Keeping them together froze
    the fork's last-observed count at a value fourteen turns stale while
    the compaction trigger kept reading it.

    With ``reasoning_chars`` known, the ratio describes the *rest* of the
    request rather than a blend. Charging reasoning at its own rate first
    and calibrating on what is left is what keeps the two halves of the
    estimate from both accounting for the same characters.
    """
    if chars <= 0 or tokens <= 0:
        return
    state = _for(model)
    with _lock:
        state.request_tokens = int(tokens)

    reasoning = max(0, min(int(reasoning_chars or 0), chars))
    ratio = chars / tokens
    if 0 < reasoning < chars:
        remaining_tokens = tokens - estimate_thinking_tokens(reasoning, model)
        if remaining_tokens > 0:
            ratio = (chars - reasoning) / remaining_tokens
    if not (MIN_OBSERVED_CHARS_PER_TOKEN <= ratio
            <= MAX_OBSERVED_CHARS_PER_TOKEN):
        return
    with _lock:
        state.chars_per_token = _blend(state.chars_per_token, ratio)


def last_observed_request_tokens(model: str = "") -> Optional[int]:
    """What the last request actually cost, per the provider.

    Unlike an estimate this cannot be wrong — it is what was counted —
    but it describes the *previous* request, so a caller trades
    exactness for being one turn behind.
    """
    return _for(model).request_tokens


def reset_calibration(model: Optional[str] = None) -> None:
    """Drop measurements — one model's, or every model's.

    Tests must call this between cases: the state outlives a single
    request by design, which is exactly what makes one test's fake token
    counts able to skew the next test's estimate.
    """
    with _lock:
        if model is None:
            _states.clear()
        else:
            _states.pop(model, None)


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
                            model: str = "") -> int:
    """Estimate a request's prompt cost, counting reasoning apart."""
    chars, reasoning = measure_request_chars(
        messages, system=system, tools=tools)
    if reasoning <= 0:
        return estimate_tokens(chars, model)
    return (estimate_tokens(chars - reasoning, model)
            + estimate_thinking_tokens(reasoning, model))


__all__ = [
    "CHARS_PER_TOKEN", "THINKING_CHARS_PER_TOKEN", "REASONING_KEYS",
    "OUTPUT_CAP_SOURCES", "WINDOW_BOUND_SOURCES",
    "OutputCapReport", "ContextOverflowReport",
    "note_output_cap", "last_output_cap",
    "note_context_overflow", "consume_context_overflow",
    "chars_per_token", "thinking_chars_per_token",
    "estimate_tokens", "estimate_thinking_tokens",
    "observe_thinking_tokens", "observe_request_tokens",
    "last_observed_request_tokens", "reset_calibration",
    "measure_request_chars", "estimate_request_tokens",
]
