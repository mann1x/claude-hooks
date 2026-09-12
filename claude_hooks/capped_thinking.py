"""Continuation notes for a turn whose thinking ran out of budget.

Ported from the Cline fork at ``/shared/dev/cline``
(``sdk/packages/core/src/extensions/context/capped-thinking.ts``). The
observations behind the constants are the fork's, from live runs; they
are reproduced because a threshold without its measurement is a number
someone will later round off.

**The failure.** A model that hits its thinking cap does not stop
reasoning — it stops mid-sentence, emits whatever tool call it can, and
on the next turn starts the same reasoning from the beginning. Observed
live: the same ground covered a dozen times, each pass reaching "this is
the final plan" and then "wait, I just realised…", each ending at the
same cap, each producing a differently-malformed call because the
argument-writing was the part that got cut.

The reasoning is in the transcript, so in principle nothing is lost. In
practice seventeen thousand tokens of interrupted rambling is not
something a model reads and continues from; it is something it
re-derives.

**In this repo the loss is total, not partial.** The council's agent
loop strips ``thinking`` / ``reasoning`` / ``reasoning_content`` from
every assistant message before resending it
(``claude_hooks/agent_loop/runner.py``). So a researcher that reasons to
its budget on iteration 1 begins iteration 2 with no record of having
reasoned at all. This module is what goes in that gap: when the cap
fires, the turn's thinking is replaced — for the next request only —
with a short note of what it settled, what it ruled out, and what the
tool call it managed to make came back as. Short enough to be read, and
framed as conclusions rather than as a transcript to resume.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Optional

from claude_hooks import token_calib

log = logging.getLogger("claude_hooks.capped_thinking")

#: How close to the budget counts as having hit it.
#:
#: Not equality: a budget is enforced on whole tokens as they are
#: produced and the turn stops on the first one that does not fit, so the
#: last few are never spent. A turn that spent nine tenths of its
#: allowance was cut short in every way that matters.
#:
#: **What this is compared against matters more than the ratio.** The
#: fork's first version compared an *estimate* — reasoning characters
#: over the request-wide chars-per-token ratio, 3.8-4.4 on a serialized
#: request full of JSON and code — against a budget denominated in the
#: model's own tokens. A turn that spent all 16,000 of its allowance,
#: about 43,000 characters of prose, therefore measured as ~10,300 and
#: never crossed the line. The cap fired on nearly 300 requests in one
#: session and the detector saw none of them. Hence
#: :func:`measured_thinking_tokens`.
CAP_PROXIMITY = 0.9

#: How far back from the end of the reasoning the budget message can be.
#: It is appended when the budget runs out, so it is at the end. Scanning
#: only the tail keeps a model that happens to *discuss* its thinking
#: budget mid-reasoning from being read as one that ran out of it.
BUDGET_MESSAGE_TAIL_MARGIN_CHARS = 400

#: Guard against a runaway note becoming the thing that fills the window.
#: Generous, because the note is the only surviving record of a think that
#: ran to twenty or thirty thousand characters and has to be detailed
#: enough to work from. A real note lands nowhere near this, which is what
#: makes it a guard rather than a target.
CONDENSED_THINKING_MAX_CHARS = 12_000

#: The output cap for the condensation call itself.
#:
#: A fixed cap was removed once, for a good reason: at 700 tokens against
#: a summariser that reasons, the whole budget went into thinking and the
#: call returned no text at all. The condensation call now asks for no
#: reasoning, so the budget is the note — and something has to bound it,
#: because a small model asked for prose in its own voice can fall into
#: repeating itself. Measured live: a note that opened "Summary-of-thought
#: process:" and then said a variant of that line thirty times.
CONDENSED_THINKING_MAX_OUTPUT_TOKENS = 2_000

#: The line the condensed reasoning arrives under, inside the thinking
#: channel.
#:
#: First person, because of where it lands. The note replaces a thinking
#: block and is read as thinking; a line announcing "this is the note you
#: left yourself" turns the model's own reasoning into a document handed
#: to it, and a document gets checked rather than continued. Measured on
#: a live run: the model opened its next turn quoting the note back and
#: arguing with a tool result about it, then re-read the same file and
#: reasoned to the budget again.
CONDENSED_THINKING_LEAD_IN = (
    "I have already reasoned this far on this problem, up to the point "
    "where I ran out of thinking budget. Picking up from here rather "
    "than starting again:"
)

CAPPED_THINKING_SYSTEM_PROMPT = (
    "You are compressing a train of thought, in the voice of the person "
    "who was thinking it. What you write goes back into that same "
    "reasoning as its own continuation -- not as a report about it -- so "
    "write it the way thinking is written: first person, present tense, "
    "plain sentences, no headings and no lists. Keep every specific: "
    "paths, symbols, line numbers, error text, and above all what was "
    "already ruled out and why."
)

DEFAULT_CAPPED_THINKING_PROMPT = """\
You were reasoning about a problem and ran out of thinking budget \
mid-thought. Below is how far you got, and what the tool call you \
managed to make returned.

Rewrite that reasoning, compressed, as the thinking it is. It is going \
back into your own reasoning channel as the part you have already done, \
so that your next pass starts from here instead of starting over and \
arriving at this same point again.

Write it as thought, not as a report about thought:
- First person, present tense, the way you were already thinking. No \
headings, no bullet lists, no preamble, no sign-off. Do not label it or \
announce what it is; just think.
- A paragraph or two. Long enough to carry the specifics, short enough \
that none of it is padding. If you find yourself restating a line you \
have already written, you are finished: stop.
- State what you have settled as settled. Do not re-derive it and do \
not hedge it.
- Say what you ruled out and why, in a clause each. This is the part \
that stops the next pass repeating this one, and it is the part that \
gets dropped first if you are careless.
- End on the one question still open and the single next action, \
stated concretely.

Two things to keep out of it:
- Do not copy file contents. Name the file and the line and say what \
you concluded about it. Quoted code goes stale the moment the file is \
edited, and reasoning that argues with the next tool result costs more \
than it saved.
- Do not narrate the interruption. It is not part of the problem."""


# --------------------------------------------------------------------- #
# Reading the turn
# --------------------------------------------------------------------- #

def thinking_text(message: dict) -> str:
    """Every reasoning block on a message, joined."""
    if not isinstance(message, dict):
        return ""
    parts: list[str] = []
    for key in token_calib.REASONING_KEYS:
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value)
    content = message.get("content")
    if isinstance(content, list):
        for block in content:
            if (isinstance(block, dict)
                    and block.get("type") in token_calib.REASONING_KEYS):
                text = block.get("thinking") or block.get("text") or ""
                if isinstance(text, str) and text.strip():
                    parts.append(text)
    return "\n".join(parts).strip()


def produced_characters(message: dict) -> int:
    """Everything the turn wrote — what its output tokens were spent on."""
    if not isinstance(message, dict):
        return 0
    chars = len(thinking_text(message))
    content = message.get("content")
    if isinstance(content, str):
        chars += len(content)
    for tc in message.get("tool_calls") or []:
        fn = (tc or {}).get("function") or {}
        chars += len(str(fn.get("arguments") or ""))
        chars += len(str(fn.get("name") or ""))
    return chars


def measured_thinking_tokens(message: dict, thinking: str,
                             completion_tokens: Optional[int] = None) -> int:
    """What this turn's reasoning cost, in the model's own tokens.

    Measured rather than estimated wherever the turn reported its output:
    the ratio between the characters a turn produced and the tokens the
    provider counted for them is *that turn's own*, and needs no
    assumption about how prose tokenizes.

    The estimate is the fallback, and uses the reasoning-specific ratio
    rather than the request-wide one — which is exactly what made the
    fork's first version never fire.
    """
    if completion_tokens and completion_tokens > 0:
        produced = produced_characters(message)
        if produced > 0:
            return round((len(thinking) / produced) * completion_tokens)
    return token_calib.estimate_thinking_tokens(len(thinking))


# --------------------------------------------------------------------- #
# The budget message
# --------------------------------------------------------------------- #

def _collapse_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def budget_message_marker(message: str) -> str:
    """The line of the budget message to look for.

    The **longest** one, not the first. A message from a Modelfile
    arrives as the author wrote it — one known model opens its with two
    blank lines — and one a user typed can open with anything at all,
    including a word short enough to appear in ordinary reasoning. The
    longest line is the one least likely to be either.
    """
    marker = ""
    for line in (message or "").split("\n"):
        collapsed = _collapse_whitespace(line)
        if len(collapsed) > len(marker):
            marker = collapsed
    return marker


def ends_with_budget_message(thinking: str, message: Optional[str]) -> bool:
    """Whether the model's own budget message is at the end of a think.

    Both sides collapsed: the same sentence is not always laid out the
    same way twice. A Modelfile writes its line breaks as ``\\n``
    escapes, a user types real ones, and the model's own copy is whatever
    the server streamed — which is not required to keep either.
    """
    if not message or not thinking:
        return False
    marker = budget_message_marker(message)
    if not marker:
        return False
    tail = thinking[-(len(marker) + BUDGET_MESSAGE_TAIL_MARGIN_CHARS):]
    return marker in _collapse_whitespace(tail)


# --------------------------------------------------------------------- #
# Degeneration
# --------------------------------------------------------------------- #

def is_degenerate_note(note: str) -> bool:
    """Whether a note is the model repeating itself rather than writing.

    Degeneration is not a graceful failure here: the note goes into the
    thinking channel *as the model's own reasoning*, so thirty
    near-identical lines are read as thirty things it thought. No note at
    all is strictly better — the turn then re-derives, which is the
    behaviour this feature improves on rather than one it breaks.

    Two cheap tests, because the shape is unmistakable: mostly-repeated
    lines, or one line that repeats a phrase over and over. Thresholds
    are set against the observed sample rather than by taste — ten lines,
    five exact repeats and four sharing an opening. Prose that is
    actually written repeats neither way, so the gap between a real note
    and this one is wide.
    """
    if not note:
        return False
    lines = [ln.strip().lower() for ln in note.split("\n") if ln.strip()]
    if len(lines) >= 6:
        if len(set(lines)) / len(lines) <= 0.6:
            return True
        # Near-identical rather than identical: "Summary-of-thought
        # process:" and "Summary of the task at hand is at the thought
        # process:" are different strings and the same failure.
        stems = [" ".join(ln.split()[:4]) for ln in lines]
        if len(set(stems)) / len(stems) <= 0.5:
            return True
    words = note.strip().lower().split()
    if len(words) >= 40 and len(set(words)) / len(words) < 0.35:
        return True
    return False


# --------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------- #

#: Why a look for a capped turn came back with nothing. Every one of
#: these has been mistaken for "the feature is broken" at least once, and
#: from the outside they are indistinguishable: no note, no error, no
#: line. They are distinguishable from in here, so they are reported.
STAND_DOWN_REASONS = (
    "no-budget", "turn-did-not-reason", "reasoning-within-budget",
    "no-condenser", "condensation-empty", "condensation-degenerate",
    "condensation-failed",
)


@dataclass(frozen=True)
class CappedThinking:
    """A turn whose reasoning ended because the budget did."""
    thinking: str
    thinking_tokens: int
    budget_tokens: Optional[int]
    evidence: str   # "budget-message" | "cap-proximity"

    def public_dict(self) -> dict:
        return {"thinking_tokens": self.thinking_tokens,
                "budget_tokens": self.budget_tokens,
                "evidence": self.evidence,
                "thinking_chars": len(self.thinking)}


def detect(message: dict, *, budget_tokens: Optional[int] = None,
           budget_message: Optional[str] = None,
           completion_tokens: Optional[int] = None
           ) -> tuple[Optional[CappedThinking], str]:
    """Was this turn's thinking ended by the budget? ``(result, reason)``.

    Two independent signals, either of which is sufficient:

    * the model's own budget message sitting at the end of the think —
      direct evidence, needs no threshold;
    * measured thinking tokens within :data:`CAP_PROXIMITY` of a known
      budget.

    Returns ``(None, reason)`` when it stood down, so a caller can say
    *why* nothing happened rather than leaving it indistinguishable from
    a broken feature.
    """
    thinking = thinking_text(message)
    if not thinking:
        return None, "turn-did-not-reason"

    if ends_with_budget_message(thinking, budget_message):
        return CappedThinking(
            thinking=thinking,
            thinking_tokens=measured_thinking_tokens(
                message, thinking, completion_tokens),
            budget_tokens=budget_tokens,
            evidence="budget-message",
        ), ""

    if not budget_tokens or budget_tokens <= 0:
        return None, "no-budget"

    tokens = measured_thinking_tokens(message, thinking, completion_tokens)
    if tokens < budget_tokens * CAP_PROXIMITY:
        return None, "reasoning-within-budget"
    return CappedThinking(
        thinking=thinking, thinking_tokens=tokens,
        budget_tokens=budget_tokens, evidence="cap-proximity",
    ), ""


# --------------------------------------------------------------------- #
# Condensation
# --------------------------------------------------------------------- #

def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[... {len(text) - limit} more characters]"


def build_condensation_messages(capped: CappedThinking, *,
                                tool_outcomes: Optional[list[dict]] = None,
                                prompt: str = DEFAULT_CAPPED_THINKING_PROMPT
                                ) -> list[dict]:
    """The two-message request that turns a capped think into a note.

    The instruction half is short and separate from the task itself,
    which travels as the user message — the same split the compaction
    summariser uses.
    """
    parts = [prompt, "", "--- how far you got ---", capped.thinking]
    for outcome in tool_outcomes or []:
        parts += [
            "",
            f"--- tool call: {outcome.get('name', '?')} ---",
            f"arguments: {_truncate(str(outcome.get('input') or ''), 1_000)}",
            f"result: {_truncate(str(outcome.get('result') or '(no result yet)'), 2_000)}",
        ]
    return [
        {"role": "system", "content": CAPPED_THINKING_SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(parts)},
    ]


def condense(capped: CappedThinking, *, chat_client: Any, model: str,
             tool_outcomes: Optional[list[dict]] = None,
             prompt: str = DEFAULT_CAPPED_THINKING_PROMPT
             ) -> tuple[Optional[str], str]:
    """Turn a capped think into a note. ``(note, stand_down_reason)``.

    The call asks for **no reasoning** (``think=False``): a summariser
    that reasons spends the whole budget on thinking and returns no text,
    which is how the fixed cap got removed the first time.

    Never raises. A condensation that fails leaves the turn to re-derive,
    which is the behaviour without this module — strictly no worse.
    """
    if chat_client is None:
        return None, "no-condenser"
    messages = build_condensation_messages(
        capped, tool_outcomes=tool_outcomes, prompt=prompt)
    try:
        response = chat_client.chat({
            "model": model,
            "messages": messages,
            "stream": False,
            "think": False,
            "options": {"num_predict": CONDENSED_THINKING_MAX_OUTPUT_TOKENS},
        })
    except Exception:
        log.warning("capped-thinking condensation call failed; the turn "
                    "will re-derive", exc_info=True)
        return None, "condensation-failed"

    choices = response.get("choices") or []
    note = ""
    if choices:
        note = ((choices[0].get("message") or {}).get("content") or "").strip()
    if not note:
        return None, "condensation-empty"
    if is_degenerate_note(note):
        log.warning("capped-thinking note degenerated into repetition; "
                    "dropping it (no note beats a note that loops)")
        return None, "condensation-degenerate"
    return _truncate(note, CONDENSED_THINKING_MAX_CHARS), ""


def as_thinking_block(note: str) -> str:
    """The note as it goes back into the reasoning channel."""
    return f"{CONDENSED_THINKING_LEAD_IN}\n\n{note}"


__all__ = [
    "CAP_PROXIMITY", "CONDENSED_THINKING_MAX_CHARS",
    "CONDENSED_THINKING_MAX_OUTPUT_TOKENS", "CONDENSED_THINKING_LEAD_IN",
    "CAPPED_THINKING_SYSTEM_PROMPT", "DEFAULT_CAPPED_THINKING_PROMPT",
    "STAND_DOWN_REASONS", "CappedThinking",
    "thinking_text", "produced_characters", "measured_thinking_tokens",
    "budget_message_marker", "ends_with_budget_message",
    "is_degenerate_note", "detect", "condense",
    "build_condensation_messages", "as_thinking_block",
]
