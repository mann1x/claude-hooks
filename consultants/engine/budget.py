"""Ask the endpoint how big its context is, and spend it deliberately.

Before this module, every consultants role sent ``{"model", "messages",
"stream", "think"}`` and nothing else — no ``num_predict``, no awareness
of the window. Incident ``csl-2026-08-12-0831-905a`` ended a 43-minute
audit after roughly 650 completion tokens with 94k tokens of window
still free: the prompt was nowhere near the limit, so the only thing
that could have stopped it was an output cap nobody had set or seen.

The first version of this module fixed that with flat constants — a
single chars-per-token ratio and a fixed 32k output request. The ladders
and reservations here replace them, ported from the Cline fork at
``/shared/dev/cline`` (``extensions/context/compaction-shared.ts``),
where each was derived from a live failure:

**Reserve what turns cost, not what they may cost.** ``num_predict`` is
a ceiling, not a forecast. Reserving all of it takes the whole cap out
of the prompt budget on every turn, including the turns that answer in
two hundred tokens — on a 110k window with a 32k cap that is 21% of the
window permanently unavailable to pay for an output that almost never
arrives. Once turns have been observed, the reservation is sized from
their high-water mark instead (:func:`observed_output_tokens`). The two
models the fork measured want opposite things: one answers a tool call
in 24–2,164 output tokens and never reasons at length; the other opens
35,000–45,000 characters of thinking on most turns. No single fraction
serves both, and the transcript already says which one is running.

**The recency floor scales with the window, sub-linearly.** A flat
"preserve the last N tokens" is right for exactly one window size: on a
32k window it asks to preserve more than the compaction target, so
compaction reclaims nothing; on a 1M window it throws away everything
but the last few turns of a task with room for forty times that. A
window ten times larger does not mean ten times as much recent work is
worth carrying — the useful tail is set by the task, not the hardware —
but it does not stay flat either.

**Fitting the prompt is not the same as fitting the turn.** The trigger
used to ask only whether the prompt fit. A turn needs the prompt *and*
its output inside one window. The fork measured a 110k window where the
transcript was allowed to 99k, the request path then found itself 11k
short of what it needed and sent no cap at all, and the turn died on the
output limit with compaction still reporting there was room.

Token estimates come from :mod:`claude_hooks.token_calib`, which counts
reasoning at its own ratio and calibrates both against real provider
counts.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from claude_hooks import token_calib

log = logging.getLogger("consultants.engine.budget")

CHARS_PER_TOKEN = token_calib.CHARS_PER_TOKEN


def estimate_tokens(text: str, model: str = "") -> int:
    """Tokens for a piece of text.

    Takes **text**, unlike :func:`claude_hooks.token_calib.estimate_tokens`
    which takes a character count. Two functions with one name and
    different units would be a trap, so the units are in the signature:
    callers here have strings, the calibration layer has counts.
    """
    return token_calib.estimate_tokens(len(text or ""), model)

#: Ceiling on what any single call may request, however much window is
#: free. Past this a larger budget stops buying a better answer.
MAX_OUTPUT_TOKENS = 32_768

#: Never request less than this. A budget this small means the prompt
#: has crowded out the answer, and the right response is to compact, not
#: to ask for a two-paragraph audit.
MIN_OUTPUT_TOKENS = 4_096

#: Left free for the backend's own framing — chat template, tool schema
#: re-serialisation, thinking blocks that never appear in ``content``.
WINDOW_MARGIN_TOKENS = 2_048

#: Kept for callers that predate the ladder. The planner asks for this
#: much when nothing has been measured; the reservation shrinks it as
#: soon as the session has turns to learn from.
TARGET_OUTPUT_TOKENS = MAX_OUTPUT_TOKENS

# --- observed-output reservation ------------------------------------- #

#: How many recent turns the reservation is measured over. Long enough
#: that one unusually long think does not set the budget for the rest of
#: the run; short enough to follow a model that changes register.
OUTPUT_ROOM_SAMPLE_TURNS = 12

#: Multiplier on the observed high-water turn. The next turn is not
#: bounded by the last twelve, so the measurement is a starting point
#: rather than an answer. Half again leaves room for a turn somewhat
#: larger than anything seen without reserving for one that never comes.
OUTPUT_ROOM_HEADROOM = 1.5

#: Never reserve less than this, however quiet the run has been.
MIN_OUTPUT_ROOM_TOKENS = 2_048

#: The largest share of the window the measurement may claim. Above half,
#: reserving for output and having a prompt become the same argument.
MAX_MEASURED_OUTPUT_ROOM_WINDOW_SHARE = 0.5

#: Share reserved before the run has produced any turns to measure.
COLD_START_OUTPUT_ROOM_WINDOW_SHARE = 0.25

# --- recency floor ---------------------------------------------------- #

#: The window the floor below is denominated against.
PRESERVE_RECENT_REFERENCE_WINDOW = 128_000

#: Tokens of recent history to preserve at the reference window.
DEFAULT_PRESERVE_RECENT_TOKENS = 20_000

#: Sub-linear growth. Two-thirds is the exponent that puts 20,000 at
#: 128k and ~79,000 at 1M — the two anchors this was specified against.
PRESERVE_RECENT_WINDOW_EXPONENT = 2 / 3

#: The largest share of a compaction target the recent tail may claim.
#: Without it the ladder wins every argument on a small window and
#: leaves the summary nothing to be written into.
PRESERVE_RECENT_TARGET_SHARE = 0.6

#: Fraction of the usable window a prompt may reach before compaction.
COMPACTION_TRIGGER_RATIO = 0.8

#: Floor on the trigger as a share of the window. Output room is
#: subtracted from the window, and on a small window a large cap can eat
#: most of it — a 32,000 cap against a 40,000 window would put the
#: trigger at 8,000 and compact almost every turn. Below this share the
#: cap is the unreasonable figure, not the transcript.
MIN_TRIGGER_WINDOW_SHARE = 0.5


def _positive(value: Any) -> bool:
    return isinstance(value, (int, float)) and value > 0


def estimate_messages_tokens(messages: list[dict], model: str = "") -> int:
    """Estimate the prompt cost of a message list.

    Delegates to :mod:`claude_hooks.token_calib` so reasoning is charged
    at its own ratio — the mix of prose and serialized tool output moves
    every turn, and a single ratio is wrong in the direction that lets a
    request be built too large.
    """
    if not messages:
        return 0
    return token_calib.estimate_request_tokens(
        [m for m in messages if isinstance(m, dict)], model=model)


def observed_output_tokens(
        messages: list[dict],
        sample_turns: int = OUTPUT_ROOM_SAMPLE_TURNS) -> Optional[int]:
    """High-water output of this run's recent turns, or ``None``.

    The high-water mark rather than the mean: the reservation exists for
    the largest turn, and an average over a run that thinks on one turn
    in four describes none of them.

    ``None`` below two samples — one turn is not a pattern, and the first
    turn of a run is routinely the smallest it will produce.
    """
    observed: list[int] = []
    for message in reversed(messages or []):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        tokens = (message.get("_output_tokens")
                  or (message.get("metrics") or {}).get("output_tokens"))
        if _positive(tokens):
            observed.append(int(tokens))
        if len(observed) >= sample_turns:
            break
    if len(observed) < 2:
        return None
    return max(observed)


def preserve_recent_tokens(*, context_length: Optional[int] = None,
                           target_tokens: Optional[int] = None,
                           override: Optional[int] = None) -> int:
    """How much recent history to keep, scaled to the window."""
    if _positive(override):
        return int(override)
    if not _positive(context_length):
        ladder = DEFAULT_PRESERVE_RECENT_TOKENS
    else:
        ladder = round(
            DEFAULT_PRESERVE_RECENT_TOKENS
            * (context_length / PRESERVE_RECENT_REFERENCE_WINDOW)
            ** PRESERVE_RECENT_WINDOW_EXPONENT)
    if not _positive(target_tokens):
        return max(1, int(ladder))
    return max(1, min(int(ladder),
                      int(target_tokens * PRESERVE_RECENT_TARGET_SHARE)))


def compaction_trigger_tokens(*, context_length: int,
                              model_max_tokens: Optional[int] = None,
                              observed_output: Optional[int] = None) -> int:
    """The largest prompt that still leaves the model room to answer.

    Takes the smaller of the ratio trigger and the room trigger, which
    keeps the old ratio as an upper bound while making room for a full
    turn the binding constraint — which is what it actually is.
    """
    ratio_trigger = context_length * COMPACTION_TRIGGER_RATIO
    declared_room = (int(model_max_tokens) if _positive(model_max_tokens)
                     else MAX_OUTPUT_TOKENS)
    if _positive(observed_output):
        output_room = min(
            declared_room,
            context_length * MAX_MEASURED_OUTPUT_ROOM_WINDOW_SHARE,
            max(MIN_OUTPUT_ROOM_TOKENS,
                observed_output * OUTPUT_ROOM_HEADROOM),
        )
    else:
        output_room = min(
            declared_room,
            context_length * COLD_START_OUTPUT_ROOM_WINDOW_SHARE)
    room_trigger = max(context_length - output_room,
                       context_length * MIN_TRIGGER_WINDOW_SHARE)
    return int(min(ratio_trigger, room_trigger))


@dataclass(frozen=True)
class Budget:
    """What one call may spend, whether it fits, and what capped it."""
    context_length: Optional[int]   # None when the endpoint didn't say
    prompt_tokens: int              # estimated, reasoning counted apart
    output_tokens: int              # what to request as num_predict
    needs_compaction: bool
    cap_source: str = "requested"
    detail: str = ""
    trigger_tokens: Optional[int] = None
    overflow: Optional[token_calib.ContextOverflowReport] = None

    @property
    def window_bound(self) -> bool:
        """Whether the window, rather than our own ask, set the ceiling.

        The property everything downstream branches on: a window-bound
        cap is compaction's to fix, and any other cap is not.
        """
        return self.cap_source in token_calib.WINDOW_BOUND_SOURCES

    def cap_report(self) -> token_calib.OutputCapReport:
        return token_calib.OutputCapReport(
            source=self.cap_source, max_tokens=self.output_tokens)

    def public_dict(self) -> dict:
        return {"context_length": self.context_length,
                "prompt_tokens_est": self.prompt_tokens,
                "num_predict": self.output_tokens,
                "cap_source": self.cap_source,
                "window_bound": self.window_bound,
                "needs_compaction": self.needs_compaction}


def context_length_of(chat_client: Any, model: str) -> Optional[int]:
    """Best-effort probe of the model's real context window.

    Uses the client's ``context_length()`` when it has one — for the
    Ollama client that reads ``model_info["<arch>.context_length"]`` off
    ``/api/show`` and caches it. Clients without the method return
    ``None``, which callers must treat as "unknown", never as a default:
    guessing 4096 for a model with a 1M window would compact away most of
    a council's evidence.
    """
    probe = getattr(chat_client, "context_length", None)
    if not callable(probe):
        return None
    try:
        value = probe(model)
    except Exception:  # pragma: no cover — probing must never fail a call
        log.debug("context_length probe failed for %s", model, exc_info=True)
        return None
    return int(value) if _positive(value) else None


def plan(chat_client: Any, model: str, messages: list[dict], *,
         target_output: Optional[int] = None,
         retry_cap: Optional[int] = None) -> Budget:
    """Decide the output budget for one call, and name what capped it.

    ``retry_cap`` is the ceiling a previous truncated attempt reported.
    When the cap that ended that turn was **not** the window's, the retry
    is given *less* to spend rather than the same again: the ceiling
    cannot be raised, so asking for the same budget reproduces the same
    overlong reply.

    With an unknown window we still request a budget — the point of
    sending one is to override a provider default we cannot see, and
    that motive is unchanged by not knowing the window. Backends clamp a
    ``num_predict`` larger than they can serve; none of them fail on it.
    """
    prompt_tokens = estimate_messages_tokens(messages, model)
    ctx = context_length_of(chat_client, model)
    ask = int(target_output) if _positive(target_output) else MAX_OUTPUT_TOKENS

    if _positive(retry_cap):
        # Deliberately below the cap that just truncated: at or above it
        # the model has no reason to answer any shorter.
        ask = max(MIN_OUTPUT_TOKENS, int(retry_cap) // 2)

    if ctx is None:
        return Budget(None, prompt_tokens, ask, False,
                      cap_source="requested",
                      detail="context length unknown; requesting the "
                             "target budget so a provider default cannot "
                             "silently rule")

    observed = observed_output_tokens(messages)
    trigger = compaction_trigger_tokens(
        context_length=ctx, model_max_tokens=ask, observed_output=observed)
    available = ctx - prompt_tokens - WINDOW_MARGIN_TOKENS

    if available < MIN_OUTPUT_TOKENS:
        overflow = token_calib.ContextOverflowReport(
            context_window=ctx,
            estimated_input_tokens=prompt_tokens,
            remaining_context=max(0, available),
            min_output_tokens=MIN_OUTPUT_TOKENS,
        )
        token_calib.note_context_overflow(overflow, model)
        return Budget(
            ctx, prompt_tokens, MIN_OUTPUT_TOKENS, True,
            cap_source="context-overflow",
            detail=(f"prompt ~{prompt_tokens} tok leaves {available} of "
                    f"{ctx} — below the {MIN_OUTPUT_TOKENS} tok floor for "
                    f"an answer"),
            trigger_tokens=trigger, overflow=overflow,
        )

    if prompt_tokens > trigger:
        # Fits, but not with a full turn's output alongside it. This is
        # the case the old "does the prompt fit?" test waved through.
        return Budget(
            ctx, prompt_tokens, max(MIN_OUTPUT_TOKENS, min(ask, available)),
            True, cap_source="remaining-context",
            detail=(f"prompt ~{prompt_tokens} tok is past the {trigger} tok "
                    f"trigger for a {ctx} window — fits, but not with a "
                    f"full turn's answer beside it"),
            trigger_tokens=trigger,
        )

    if ask <= available:
        return Budget(ctx, prompt_tokens, ask, False, cap_source="requested",
                      detail=f"prompt ~{prompt_tokens} tok of {ctx}",
                      trigger_tokens=trigger)
    return Budget(ctx, prompt_tokens, max(MIN_OUTPUT_TOKENS, available), False,
                  cap_source="remaining-context",
                  detail=(f"prompt ~{prompt_tokens} tok of {ctx}; the window "
                          f"caps output at {available} rather than the "
                          f"{ask} asked for"),
                  trigger_tokens=trigger)


@dataclass
class CompactionResult:
    messages: list[dict]
    changed: bool
    dropped: int = 0
    generation: int = 1
    reclaimed_thinking: list[str] = field(default_factory=list)
    #: The elided messages themselves, in order. What a retrospective is
    #: written from: the reasoning alone reads as a plan and every plan
    #: reads as sound, so the tool calls and their outcomes have to
    #: travel with it.
    dropped_messages: list[dict] = field(default_factory=list)
    #: Where the marker sits in ``messages``, so a caller that writes a
    #: digest can replace it without re-deriving the layout.
    marker_index: Optional[int] = None

    def __iter__(self):
        """Backwards compatibility with the ``(messages, changed)``
        tuple this used to return."""
        return iter((self.messages, self.changed))


def compact_messages(messages: list[dict], *, keep_tokens: int,
                     keep_recent: int = 4,
                     context_length: Optional[int] = None,
                     model: str = "",
                     make_marker: Optional[Any] = None) -> CompactionResult:
    """Drop from the middle until the history fits ``keep_tokens``.

    What survives:

    * every leading ``system`` message — the role's instructions;
    * the first non-system message — the question being answered;
    * the most recent exchanges — the live state of the work.

    How many count as "recent" is derived from the window when one is
    known (:func:`preserve_recent_tokens`), falling back to
    ``keep_recent`` messages otherwise. A flat message count is right for
    exactly one window size.

    The elided span is replaced by a visible marker rather than removed
    silently, because a model that can see it was given an abridged
    history reasons differently about gaps in it than one that believes
    it has the whole record.

    The dropped messages are returned rather than discarded, so a
    caller can distil them into a summary and a retrospective —
    compaction is the right place to do that, because it is where the
    turns that produced them are going away. ``make_marker`` lets that
    caller supply the replacement message; the default is the bare
    elision note, which carries the fact of the loss but none of its
    content.
    """
    if not messages:
        return CompactionResult(messages, False)
    if estimate_messages_tokens(messages, model) <= keep_tokens:
        return CompactionResult(messages, False)

    head: list[dict] = []
    i = 0
    while i < len(messages) and messages[i].get("role") == "system":
        head.append(messages[i])
        i += 1
    if i < len(messages):
        head.append(messages[i])   # the question
        i += 1

    tail_budget = preserve_recent_tokens(
        context_length=context_length, target_tokens=keep_tokens)
    tail: list[dict] = []
    spent = 0
    for message in reversed(messages[i:]):
        cost = estimate_messages_tokens([message], model)
        if tail and spent + cost > tail_budget:
            break
        tail.insert(0, message)
        spent += cost
    if len(tail) < keep_recent:
        tail = messages[max(i, len(messages) - keep_recent):]

    middle_start, middle_end = i, len(messages) - len(tail)
    if middle_end <= middle_start:
        return CompactionResult(messages, False)

    dropped_msgs = messages[middle_start:middle_end]
    reclaimed = []
    for message in dropped_msgs:
        for key in token_calib.REASONING_KEYS:
            value = message.get(key)
            if isinstance(value, str) and value.strip():
                reclaimed.append(value)

    dropped = len(dropped_msgs)
    generation = 1 + sum(
        1 for m in messages
        if m.get("_compaction_marker") and m.get("role") == "system")
    marker = None
    if make_marker is not None:
        try:
            marker = make_marker(dropped_msgs, generation)
        except Exception:  # pragma: no cover — a digest must not block
            log.exception("marker factory raised; falling back to the bare "
                          "elision note")
            marker = None
    if marker is None:
        marker = {
            "role": "system",
            "_compaction_marker": True,
            "content": (
                f"[{dropped} earlier message(s) elided to fit the model's "
                f"context window. The instructions above and the most recent "
                f"exchanges below are intact; intermediate working notes are "
                f"not. Say so if the elided span is load-bearing for your "
                f"answer rather than inferring what it contained.]"
            ),
        }
    out = head + [marker] + tail
    log.warning(
        "compacted history (gen %d): %d -> %d messages "
        "(~%d -> ~%d tok, budget %d, tail %d tok)",
        generation, len(messages), len(out),
        estimate_messages_tokens(messages, model),
        estimate_messages_tokens(out, model), keep_tokens, tail_budget,
    )
    return CompactionResult(out, True, dropped=dropped,
                            generation=generation,
                            reclaimed_thinking=reclaimed,
                            dropped_messages=list(dropped_msgs),
                            marker_index=len(head))


__all__ = ["Budget", "CompactionResult", "estimate_tokens",
           "estimate_messages_tokens", "context_length_of", "plan",
           "compact_messages", "observed_output_tokens",
           "preserve_recent_tokens", "compaction_trigger_tokens",
           "MAX_OUTPUT_TOKENS", "MIN_OUTPUT_TOKENS", "TARGET_OUTPUT_TOKENS",
           "WINDOW_MARGIN_TOKENS", "CHARS_PER_TOKEN"]
