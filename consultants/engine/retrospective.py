"""Two-phase compaction: what happened, and how the work went.

Tier D of the Cline-fork port (``/shared/dev/cline``,
``extensions/context/compaction-shared.ts``). The prompts are reused
from there; so is the reasoning-with-outcomes pairing that makes the
second phase worth its tokens.

**The problem it solves.** Compaction drops messages. Until now the
council replaced them with a marker saying how many were elided, which
is honest and carries nothing: every finding, every dead end, every
stretch of reasoning in that span was simply gone. The role continued
from its instructions and the last few turns, with no memory of having
been wrong — and a model with no memory of having been wrong makes the
same mistakes in the same order.

**Why two passes and not one.** They answer different questions and
compete for the same budget, so a single pass silently drops one of
them — in practice the second, because "what happened" is easier to
write than "how it went".

*Summary* is the hand-over note: goal, what is done, what is in
progress, what was ruled out, key facts, next steps. Specifics —
paths, symbols, error text — because a vague note costs the whole
investigation while an over-long one costs a little context.

*Retrospective* is the honest assessment of method: what worked, what
did not and its failure mode, where the time went, what to do
differently. It is written from the discarded **reasoning**, which is
the only place that information ever existed. It is explicitly banned
from repeating the summary's specifics — that is what keeps it from
becoming a second, worse summary.

**Both are placed as plain text.** The retrospective riding as a
reasoning block was the fork's first attempt and it fails at the wire:
reasoning parts are only valid on an assistant message, and this is the
message that *replaces* a span of the transcript. A live run died on
``AI_TypeValidationError: The messages do not match the ModelMessage[]
schema`` with a perfectly good retrospective inside it. The heading
carries the framing the block type cannot.

**The budget grows with generation.** A first compaction summarises one
stretch of work; a fifth is carrying everything the task has learned.
Holding those to the same budget is how a long run degrades — the
standing context stops growing while the thing it describes keeps
going, so the model relearns what it already knew.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from claude_hooks import capped_thinking, token_calib

log = logging.getLogger("consultants.engine.retrospective")

#: What the summary and the retrospective may spend **together**, as a
#: share of the compaction target, indexed by generation. Shares of the
#: target rather than of the window, because the target is what they
#: compete for. The last rung is the ceiling: beyond it the standing
#: context crowds out the recent turns it exists to give perspective on.
COMPACTION_BUDGET_LADDER = (0.33, 0.4, 0.45, 0.5, 0.55)

#: The summary's share of the combined budget. It writes first.
SUMMARY_BUDGET_SHARE = 0.7

#: The retrospective is written second and takes what the summary left,
#: so its cap is a range rather than a number: a summary that used its
#: whole 70% leaves the floor, one that came in at half leaves the
#: ceiling. What it must not do is expand to fill the room — the
#: instruction to be terse is in the prompt, and this is only the wall
#: behind it.
THINKING_BUDGET_MAX_SHARE = 0.5
THINKING_BUDGET_MIN_SHARE = 0.2

#: Floor for either phase. Below this there is no point making the call.
MIN_PHASE_OUTPUT_TOKENS = 512

#: A single runaway think must not crowd out the twenty turns around it.
MAX_REASONING_CHARS_PER_TURN = 6_000

#: Tool results are reduced to a verdict, so this bounds only the few
#: cases where the verdict quotes an error.
MAX_OUTCOME_CHARS = 200

#: How much of one message survives into the summary's input.
MAX_MESSAGE_CHARS = 2_000

#: The largest share of the compaction target either phase's **input**
#: may occupy.
#:
#: Without this the digest can overflow the window it exists to relieve.
#: Measured on the first end-to-end run: 61 dropped messages serialized
#: to 51,442 characters of reasoning — about 17,000 tokens against a
#: 24,000-token window, before the phase's own output budget. It fit,
#: barely, and a span half again as large would not have. The summariser
#: reads the transcript to describe it, and a description written from a
#: request the model refused is worth nothing.
#:
#: When the span is too big, the **most recent** turns are kept: they are
#: the ones "in progress" and "next" are written from, and the oldest
#: turns are the ones a previous digest already covers.
MAX_INPUT_SHARE_OF_TARGET = 0.5


@dataclass(frozen=True)
class OutputBudgets:
    generation: int
    combined_tokens: int
    summary_max_tokens: int

    def public_dict(self) -> dict:
        return {"generation": self.generation,
                "combined_tokens": self.combined_tokens,
                "summary_max_tokens": self.summary_max_tokens}


def resolve_output_budgets(*, target_tokens: int,
                           generation: int = 1) -> OutputBudgets:
    generation = max(1, int(generation))
    share = COMPACTION_BUDGET_LADDER[
        min(generation, len(COMPACTION_BUDGET_LADDER)) - 1]
    combined = max(MIN_PHASE_OUTPUT_TOKENS,
                   int(max(0, target_tokens) * share))
    return OutputBudgets(
        generation=generation,
        combined_tokens=combined,
        summary_max_tokens=max(MIN_PHASE_OUTPUT_TOKENS,
                               int(combined * SUMMARY_BUDGET_SHARE)),
    )


def resolve_thinking_max_tokens(budgets: OutputBudgets,
                                summary_tokens: int) -> int:
    """What is left for the retrospective once the summary is written.

    Measured against what the summary actually **cost**, not what it was
    allowed: writing them in this order is what lets an economical
    summary buy the retrospective room rather than wasting it.
    """
    combined = budgets.combined_tokens
    remaining = combined - max(0, summary_tokens)
    return max(int(combined * THINKING_BUDGET_MIN_SHARE),
               min(int(combined * THINKING_BUDGET_MAX_SHARE), remaining))


# --------------------------------------------------------------------- #
# Prompts — reused from the fork
# --------------------------------------------------------------------- #

DEFAULT_COMPACTION_PROMPT = """\
You are writing the hand-over note for a piece of investigative work \
that is about to lose its transcript. Everything below will be \
discarded; only your note survives, and the agent continuing this work \
will have nothing else to go on.

Write for that reader. Prefer specifics over summary: exact file paths, \
function and symbol names, error text, commands, and numbers. Do not \
compress away detail that would have to be rediscovered — an over-long \
note costs a little context, a vague one costs the whole investigation.

## Goal
What is being investigated or built, in one or two sentences, including \
any constraint that was stated.

## Done
What is finished and verified, with the evidence — which files were \
read, what the command or search said. Be specific enough that none of \
it gets redone.

## In progress
What is underway right now, and exactly where it stopped.

## Ruled out
Approaches already tried that did not work, and why. Omit if none — but \
never drop one that was tried, or it will be tried again.

## Key facts
Decisions taken, values discovered, identifiers, signatures, and \
anything learned about the codebase that is not obvious from reading it. \
Omit if none.

## Next
The immediate next steps, in order.

Write them for a reader holding no file contents. Every step that \
touches a file must begin by reading the part it touches.

## Files
Read: {{files_read}}
Edited: {{files_edited}}"""


#: Reused verbatim from the fork. Written hard against verbosity,
#: because the models this matters most for are the ones that ruminate:
#: on a model that hits its thinking cap most turns, an unbounded
#: "reflect on your reasoning" produces more of exactly the behaviour it
#: is meant to flag. Hence the ban on specifics — those are in the
#: summary, and repeating them spends the budget twice for one fact.
DEFAULT_THINKING_COMPACTION_PROMPT = """\
You are writing the retrospective that goes at the top of your own \
context after compaction. It is not a summary — what happened is \
written up separately, and repeating it here wastes the space this \
needs.

You are reading your own reasoning from the work about to be discarded, \
together with what each stretch of it produced: which tool calls \
landed, which were refused, which were retried unchanged, and which \
turns ran long.

Write the honest assessment. Be terse. Every line must be something you \
would want to know before starting the next hour of this task.

## What worked
Approaches that produced progress, stated as approaches rather than as \
events.

## What did not
Approaches that cost time and produced nothing. Name the failure mode: \
repeating an unchanged call, editing from a stale read, rewriting where \
narrowing would do, chasing a symptom.

## Where the time went
The stretches that were expensive against what they achieved, including \
any turn where the reasoning ran long without converging.

## Do differently
What to do instead. Concrete enough to act on, short enough to remember.

Rules:
- No file names, line numbers, code, identifiers or values. Those are \
in the summary. This is about method.
- No narration of the sequence of events. Judgement only.
- Leave out any section with nothing worth saying.
- Terse throughout. A dozen short lines is a good retrospective; a page \
is a failed one."""

SUMMARY_SYSTEM_PROMPT = (
    "You are writing a hand-over note. Be concrete and complete; the "
    "reader has nothing but what you write."
)

RETROSPECTIVE_SYSTEM_PROMPT = (
    "You are recording what a train of reasoning established about the "
    "problem it was working on, for the agent that is about to reason "
    "about the same problem again. Not what it decided -- that is "
    "written down elsewhere -- but how the work went."
)


# --------------------------------------------------------------------- #
# Rendering the discarded span
# --------------------------------------------------------------------- #

_READ_TOOLS = ("read_file", "grep", "glob", "list_files", "search_files")
_WRITE_TOOLS = ("write_file", "edit_file", "apply_patch")
_PATH_KEYS = ("path", "file", "file_path", "dir", "root")
_PATH_RE = re.compile(r'"(?:path|file|file_path)"\s*:\s*"([^"]+)"')


@dataclass
class FileOps:
    read: list[str] = field(default_factory=list)
    edited: list[str] = field(default_factory=list)


def _tool_paths(arguments: Any) -> list[str]:
    if isinstance(arguments, dict):
        return [str(arguments[k]) for k in _PATH_KEYS if arguments.get(k)]
    if isinstance(arguments, str):
        return _PATH_RE.findall(arguments)
    return []


def collect_file_ops(messages: list[dict]) -> FileOps:
    """Which files the discarded span touched.

    Losing track of which files are in play is the one failure that
    makes a hand-over note actively misleading rather than merely thin,
    so this is appended whether or not the prompt asked for it.
    """
    ops = FileOps()
    for message in messages or []:
        for tc in (message or {}).get("tool_calls") or []:
            fn = (tc or {}).get("function") or {}
            name = str(fn.get("name") or "")
            paths = _tool_paths(fn.get("arguments"))
            bucket = ops.edited if name in _WRITE_TOOLS else (
                ops.read if name in _READ_TOOLS else None)
            if bucket is None:
                continue
            for p in paths:
                if p not in bucket:
                    bucket.append(p)
    return ops


def describe_tool_outcome(content: str, *, is_error: bool = False) -> str:
    """What a tool call came back as, in the fewest words that
    distinguish the cases a retrospective cares about."""
    text = (content or "").strip()
    if not text:
        return "no result recorded"
    lowered = text.lower()
    if "strikes left" in lowered or "last strike" in lowered:
        return "refused by the loop guard as an unchanged repeat"
    if text.startswith("No change:") or "no change:" in lowered[:40]:
        return "refused: the change was already in place"
    if "already returned" in lowered or "duplicate" in lowered[:60]:
        return "refused as a duplicate of an earlier call"
    first = next((ln.strip() for ln in text.split("\n") if ln.strip()), text)
    if is_error or lowered.startswith("error") or "traceback" in lowered:
        return f"failed: {first[:MAX_OUTCOME_CHARS]}"
    if '"success": false' in lowered or '"success":false' in lowered:
        return f"reported failure: {first[:MAX_OUTCOME_CHARS]}"
    if "not found" in lowered or "no matches" in lowered:
        return f"returned nothing: {first[:MAX_OUTCOME_CHARS]}"
    return "returned results"


def _truncate(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f" […{len(text) - limit} more chars]"


def _keep_recent_chars(blocks: list[str], max_chars: Optional[int],
                       separator: str) -> str:
    """Join blocks newest-first until the budget runs out, then restore
    order. An elision note is prepended when anything was dropped, so
    the model is not left to infer that it has the whole span."""
    if not blocks:
        return ""
    joined = separator.join(blocks)
    if max_chars is None or len(joined) <= max_chars:
        return joined
    kept: list[str] = []
    spent = 0
    for block in reversed(blocks):
        if kept and spent + len(block) > max_chars:
            break
        kept.insert(0, block)
        spent += len(block)
    omitted = len(blocks) - len(kept)
    note = (f"[{omitted} earlier turn(s) omitted here — they did not fit "
            f"this pass. Assess what you can see.]")
    return separator.join([note] + kept)


def serialize_reasoning_with_outcomes(
        messages: list[dict], *, max_chars: Optional[int] = None) -> str:
    """Render the discarded reasoning against what it produced.

    The retrospective's whole value is the **pairing**. Reasoning on its
    own reads as a plan and every plan reads as sound; it is the outcome
    next to it that shows which ones were. So each turn is one block:
    what the model thought, what it then called, and how that call came
    back.

    Tool results are reduced to their verdict rather than included. A
    retrospective about method has no use for the contents of a file,
    and the results are most of the bytes.
    """
    outcomes: dict[str, str] = {}
    for message in messages or []:
        if (message or {}).get("role") != "tool":
            continue
        tcid = message.get("tool_call_id") or ""
        if tcid:
            outcomes[tcid] = describe_tool_outcome(
                message.get("content") or "",
                is_error=bool(message.get("is_error")))

    turns: list[str] = []
    for message in messages or []:
        if (message or {}).get("role") != "assistant":
            continue
        thinking = capped_thinking.thinking_text(message)
        calls = []
        for tc in message.get("tool_calls") or []:
            fn = (tc or {}).get("function") or {}
            verdict = outcomes.get(tc.get("id") or "", "no result recorded")
            calls.append(f"  {fn.get('name') or '?'} -> {verdict}")
        if not thinking and not calls:
            continue
        lines = []
        if thinking:
            cost = _reasoning_cost_label(message, thinking)
            lines.append(
                f"Reasoning{cost}: "
                f"{_truncate(thinking, MAX_REASONING_CHARS_PER_TURN)}")
        if calls:
            lines.append("Then called:\n" + "\n".join(calls))
        turns.append("\n".join(lines))
    return _keep_recent_chars(turns, max_chars, "\n\n").strip()


def _reasoning_cost_label(message: dict, thinking: str) -> str:
    """What this turn's reasoning cost, when the transcript recorded it.

    Handed to the model because it is the one thing it cannot infer from
    reading its own thinking back: whether a stretch that felt thorough
    was in fact the turn that spent eighteen thousand tokens and
    produced one refused call.
    """
    output_tokens = message.get("_output_tokens")
    if not isinstance(output_tokens, int) or output_tokens <= 0:
        return ""
    tokens = capped_thinking.measured_thinking_tokens(
        message, thinking, output_tokens)
    return f" (~{tokens:,} tokens)"


def serialize_conversation(messages: list[dict], *,
                           max_chars: Optional[int] = None) -> str:
    """The discarded span as the summary phase sees it.

    Reasoning is deliberately **excluded**: the summariser is reading
    the transcript to describe it, and how the model talked itself into
    each tool call is not part of that description. It is the
    retrospective's input, not this one's.
    """
    lines: list[str] = []
    for message in messages or []:
        role = (message or {}).get("role") or "?"
        content = message.get("content")
        if isinstance(content, list):
            content = " ".join(
                str(b.get("text") or "") for b in content
                if isinstance(b, dict))
        text = _truncate(str(content or "").strip(), MAX_MESSAGE_CHARS)
        calls = [((tc or {}).get("function") or {}).get("name") or "?"
                 for tc in message.get("tool_calls") or []]
        if not text and not calls:
            continue
        piece = f"[{role}]"
        if text:
            piece += f" {text}"
        if calls:
            piece += f" (called: {', '.join(calls)})"
        lines.append(piece)
    return _keep_recent_chars(lines, max_chars, "\n").strip()


def build_summary_prompt(messages: list[dict], *,
                         previous_summary: Optional[str] = None,
                         template: Optional[str] = None,
                         max_input_chars: Optional[int] = None) -> str:
    ops = collect_file_ops(messages)
    read = ", ".join(ops.read) or "none"
    edited = ", ".join(ops.edited) or "none"
    rendered = (template or DEFAULT_COMPACTION_PROMPT)
    rendered = rendered.replace("{{files_read}}", read)
    rendered = rendered.replace("{{files_edited}}", edited)
    if read not in rendered or edited not in rendered:
        rendered = (rendered.rstrip()
                    + f"\n\n## Files\nRead: {read}\nEdited: {edited}")
    parts = [rendered]
    if (previous_summary or "").strip():
        parts.append("Previous summary:\n" + previous_summary.strip())
    parts.append("Conversation:\n"
                 + (serialize_conversation(messages,
                                           max_chars=max_input_chars)
                    or "(empty)"))
    return "\n\n".join(parts)


def build_retrospective_prompt(messages: list[dict], *,
                               previous_retrospective: Optional[str] = None,
                               template: Optional[str] = None,
                               max_input_chars: Optional[int] = None) -> str:
    parts = [template or DEFAULT_THINKING_COMPACTION_PROMPT]
    if (previous_retrospective or "").strip():
        parts.append(
            "Your retrospective from the previous compaction. Carry "
            "forward what still holds, revise what does not, and do not "
            "simply restate it:\n" + previous_retrospective.strip())
    parts.append("Your reasoning and what it produced:\n"
                 + (serialize_reasoning_with_outcomes(
                     messages, max_chars=max_input_chars) or "(none)"))
    return "\n\n".join(parts)


# --------------------------------------------------------------------- #
# Writing the digest
# --------------------------------------------------------------------- #

@dataclass(frozen=True)
class Digest:
    """What survives a compacted span."""
    summary: str = ""
    retrospective: str = ""
    generation: int = 1
    dropped: int = 0
    stand_down: str = ""

    @property
    def empty(self) -> bool:
        return not (self.summary.strip() or self.retrospective.strip())

    def public_dict(self) -> dict:
        return {"generation": self.generation, "dropped": self.dropped,
                "has_summary": bool(self.summary.strip()),
                "has_retrospective": bool(self.retrospective.strip()),
                "stand_down": self.stand_down}


def _call(chat_client: Any, model: str, system: str, user: str,
          max_tokens: int) -> tuple[str, int]:
    """One phase. Returns ``(text, completion_tokens)``; ``("", 0)`` on
    any failure — a compaction that cannot summarise still has to
    compact, and raising here would take the whole call down with it.

    ``think=False`` for the same reason the capped-thinking condenser
    asks for none: a summariser that reasons spends its budget on
    thinking and returns no text.
    """
    try:
        response = chat_client.chat({
            "model": model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "stream": False,
            "think": False,
            "options": {"num_predict": max_tokens},
        })
    except Exception:
        log.warning("compaction phase failed; continuing without it",
                    exc_info=True)
        return "", 0
    choices = response.get("choices") or []
    text = ""
    if choices:
        text = ((choices[0].get("message") or {}).get("content") or "").strip()
    tokens = int((response.get("usage") or {}).get("completion_tokens") or 0)
    return text, tokens


def write(dropped_messages: list[dict], *, chat_client: Any, model: str,
          target_tokens: int, generation: int = 1,
          previous: Optional[Digest] = None) -> Digest:
    """Summarise a span being discarded, then assess how the work went.

    Order matters and is not arbitrary: the summary writes first against
    70% of the combined budget, and the retrospective is then sized from
    what the summary actually spent, so an economical summary buys the
    retrospective room.

    The retrospective is skipped when the span carried no reasoning —
    there is nothing to assess, and asking anyway produces a model
    inventing an assessment of work it cannot see.
    """
    if not dropped_messages:
        return Digest(generation=generation, stand_down="nothing-dropped")
    if chat_client is None:
        return Digest(generation=generation, dropped=len(dropped_messages),
                      stand_down="no-summariser")

    budgets = resolve_output_budgets(target_tokens=target_tokens,
                                     generation=generation)
    # Bound the *input* as well as the output. A digest that overflows
    # the window is a digest that never arrives, and the span being
    # dropped is by definition the part that did not fit.
    max_input_chars = max(
        4_000,
        int(target_tokens * MAX_INPUT_SHARE_OF_TARGET
            * token_calib.chars_per_token(model)))

    summary, summary_tokens = _call(
        chat_client, model, SUMMARY_SYSTEM_PROMPT,
        build_summary_prompt(
            dropped_messages,
            previous_summary=previous.summary if previous else None,
            max_input_chars=max_input_chars),
        budgets.summary_max_tokens)

    retrospective = ""
    reasoning = serialize_reasoning_with_outcomes(dropped_messages)
    if reasoning:
        if not summary_tokens and summary:
            # No usage reported: charge the summary at its estimated cost
            # rather than at zero, or the retrospective is handed the
            # whole combined budget on a phase that already spent some.
            summary_tokens = token_calib.estimate_tokens(len(summary), model)
        retrospective, _ = _call(
            chat_client, model, RETROSPECTIVE_SYSTEM_PROMPT,
            build_retrospective_prompt(
                dropped_messages,
                previous_retrospective=(previous.retrospective
                                        if previous else None),
                max_input_chars=max_input_chars),
            resolve_thinking_max_tokens(budgets, summary_tokens))
        if retrospective and capped_thinking.is_degenerate_note(retrospective):
            log.warning("retrospective degenerated into repetition; dropping "
                        "it — a looping assessment reads as many findings")
            retrospective = ""

    digest = Digest(summary=summary, retrospective=retrospective,
                    generation=generation, dropped=len(dropped_messages),
                    stand_down="" if (summary or retrospective)
                    else "both-phases-empty")
    log.info("compaction digest gen %d: summary %d chars, retrospective "
             "%d chars, from %d dropped message(s)",
             generation, len(summary), len(retrospective),
             len(dropped_messages))
    return digest


def render_marker(digest: Digest, dropped: int) -> dict:
    """The message that replaces the elided span.

    Retrospective first — it is what the model should read first: how
    the work went, before what the work was.

    Everything is plain text. The retrospective riding as a reasoning
    block was the fork's first attempt and fails at the wire: reasoning
    parts are only valid on an assistant message, and this is the
    message replacing a span of transcript. The heading carries the
    framing the block type cannot.
    """
    parts: list[str] = []
    if digest.retrospective.strip():
        parts.append(
            "Retrospective on the work this summary replaces — your own "
            "assessment, carried forward:\n\n"
            + digest.retrospective.strip() + "\n")
    if digest.summary.strip():
        parts.append("Context summary:\n\n" + digest.summary.strip() + "\n")
    parts.append(
        f"[{dropped} earlier message(s) were elided to fit the model's "
        f"context window. The instructions above and the most recent "
        f"exchanges below are intact. What the elided span established is "
        f"{'recorded above' if parts else 'not recorded'}; say so if you "
        f"need something from it that is missing rather than inferring "
        f"what it contained.]")
    return {
        "role": "system",
        "_compaction_marker": True,
        "_compaction_generation": digest.generation,
        "_compaction_summary": digest.summary,
        "_compaction_retrospective": digest.retrospective,
        "content": "\n".join(parts),
    }


def previous_digest(messages: list[dict]) -> Optional[Digest]:
    """The digest left by the last compaction of this history, if any.

    Chained so a fifth compaction revises the fourth's assessment rather
    than writing a fresh one from a span that no longer contains the
    work it is describing.
    """
    for message in reversed(messages or []):
        if (message or {}).get("_compaction_marker"):
            return Digest(
                summary=message.get("_compaction_summary") or "",
                retrospective=message.get("_compaction_retrospective") or "",
                generation=int(message.get("_compaction_generation") or 1),
            )
    return None


__all__ = [
    "COMPACTION_BUDGET_LADDER", "SUMMARY_BUDGET_SHARE",
    "THINKING_BUDGET_MAX_SHARE", "THINKING_BUDGET_MIN_SHARE",
    "DEFAULT_COMPACTION_PROMPT", "DEFAULT_THINKING_COMPACTION_PROMPT",
    "OutputBudgets", "Digest", "FileOps",
    "resolve_output_budgets", "resolve_thinking_max_tokens",
    "collect_file_ops", "describe_tool_outcome",
    "serialize_reasoning_with_outcomes", "serialize_conversation",
    "build_summary_prompt", "build_retrospective_prompt",
    "write", "render_marker", "previous_digest",
]
