"""The prompts compaction sends (plan Milestone C2).

Taken verbatim from the Cline fork (``/shared/dev/cline``,
``sdk/packages/core/src/extensions/context/``) where the fork has the
text, and kept to its rules where it does not:

* ``replay-compaction.ts`` — the present-tense replay the writer is asked
  for whenever a tail is kept, which for the council is always. Written
  in the past tense the replay reads as history, and history is something
  the model feels entitled to doubt: it re-reads files it already knows,
  re-derives what it has settled and treats a warning still in force as
  something that once happened (measured on a 29-minute pandorum run).
  Only positive exemplars: a negative exemplar is still an exemplar, and
  a forbidden-phrasing column came back nearly verbatim. Section labels
  are in the tense they ask for.
* ``full-compaction.ts`` — the state record, for the no-tail rescue only,
  where the digest *is* the context. No length adjective anywhere:
  "concise" and "very detailed" barely move output length
  (arXiv 2605.23296); completeness is structural instead.
* ``council-compaction.ts`` — the halfway marker, the half-critics and the
  synthesizer of the full flow.
* ``agentic-compaction.ts`` — the writer's system prompts and the overrun
  feedback (a measurement is not an adjective).

The reduced flow's reviewer has no fork counterpart: the fork has no
reduced flow (report 02 §0). Its prompt is the critic's and the
synthesizer's rules applied to the whole replay at once.
"""
from __future__ import annotations

import re

# --------------------------------------------------------------------- #
# System prompts (agentic-compaction.ts:127-135, council-compaction.ts:42-47)
# --------------------------------------------------------------------- #

WRITER_SYSTEM_PROMPT = (
    "You are re-telling your own recent work in your own voice, because the "
    "earlier part of your transcript is about to be discarded and what you "
    "write takes its place directly in front of the turns that remain. "
    "Match the prose of those turns. Keep every specific — names, paths, "
    "quoted wording, errors, numbers — that you would otherwise have to "
    "rediscover.")

FULL_SYSTEM_PROMPT = (
    "You write the state record for a working session of any kind. The "
    "transcript you are given is about to be discarded, so what you write "
    "is the only record that remains. Follow the requested structure "
    "exactly, section for section, and keep every specific — names, paths, "
    "quoted wording, errors, numbers — that whoever continues would "
    "otherwise have to rediscover.")

RETROSPECTIVE_SYSTEM_PROMPT = (
    "You are assessing your own reasoning on work that is about to be "
    "discarded. This is judgement, not a record — what happened is written "
    "up separately. Be terse, and say only what you would want to know "
    "before starting the next hour of this task.")

CRITIC_SYSTEM_PROMPT = (
    "You are rewriting one half of an account of your own work, with the "
    "record of that work in front of you. Correct what the record "
    "contradicts, add what it shows missing, fix every quotation against "
    "it, and improve the prose where it has drifted out of voice. You "
    "return your half and only your half: another writer is rewriting the "
    "other one, and the two will be joined into a single continuous replay.")

SYNTHESIZER_SYSTEM_PROMPT = (
    "You are joining two independently rewritten halves of one replay into "
    "a single continuous piece, and revising a retrospective against the "
    "result. Each half was rewritten by someone holding the whole record "
    "but owning only that half, so both are grounded — and both could be "
    "wrong about the other's territory. Your answer is the finished text, "
    "not an account of how you produced it.")

REVIEWER_SYSTEM_PROMPT = (
    "You are revising an account of your own work, with the record of that "
    "work in front of you. Correct what the record contradicts, add what "
    "it shows missing, fix every quotation against it, improve the prose "
    "where it has drifted out of voice, and then revise the retrospective "
    "against the result. Your answer is the finished text, not an account "
    "of how you produced it.")

# --------------------------------------------------------------------- #
# The writer (replay-compaction.ts:59-100)
# --------------------------------------------------------------------- #

REPLAY_PROMPT = """\
Your transcript has grown too long and the earlier part of it is about to be discarded. Write the replay that takes its place.

Your replay will be **prepended directly to the messages that remain** — your own most recent turns, which are still there and which you will read immediately after this. Write it so that seam is invisible.

Write in the **first person, present continuous tense**, in your own voice, in the same prose as the turns it sits in front of. You are not reporting on a session to someone else and you are not recounting something finished: you are picking the work back up, and everything in the replay is the situation as it stands right now.

Write **every step** as the step itself, then what came back. The opening of a
step is what you are about to do; the outcome is its own short sentence after
it. These are the shapes — reuse them:

- "The user is asking me to fix the collision."
- "Let me start by reading the file."
- "Let me read a large chunk of the file." — then what it showed.
- "Let me try reconstructing the script section. It failed: `check_file` reports an unterminated regular expression at line 274."
- "Now the editor is warning me this change shifted the line numbers by +11."
- "The file parses now, so what is left is the collision check."

This holds for the middle of the replay and not only its first and last
sentences. A step that opens by reporting itself has changed voice, and the
seam this replay exists to remove is back.

Written in the past tense the replay reads as history, and history is something you are entitled to doubt: you will re-read files you already know, re-derive what you have already settled, and treat a warning that is still in force as something that merely once happened. Written in the present it is the state of play, which is what it actually is. Every sentence, not only the first and the last.

Carry all of this:

- **What you are asked to do, verbatim.** Quote the instructions you have been given word for word. Everything else here can be rebuilt from the files or from the record below your replay; what was asked exists nowhere else once these messages are gone.
- **What you have done, in order.** Narrate it, and **cite the calls instead of writing them out.** Every call you made, with its arguments and what it returned, is numbered in the record below. Where a call belongs in your account, put its number in square brackets with a `#`, and carry on:

  "Let me run the checker. [#3] It reports a `SyntaxError` at line 90."

  The harness puts call 3 in at that point, exactly as it happened. Writing the call out yourself instead would spend your budget copying a record that is already exact, and yours would be the less accurate of the two. Cite each call once, in the order you made them. Where a run of calls in sequence belongs at the same point — the same check repeated, a read and the edit that followed it — cite the span as `[#2-5]` rather than one at a time; the harness puts all of them in there. A number you leave out is not lost — it is added at the end — but then it sits away from the step it belongs to.

  **The record is complete and closed.** Every call you have made is in it, and nothing else is. The highest number in it is the last call you made, so there is no `[#N]` past the end to cite — if you find yourself reaching for one, the step you are about to describe is a step you have not taken yet, and it does not belong in an account of what you have done. Cite only a number that is actually in the record, and leave a step with no call behind it uncited rather than attaching it to a number that names someone else's work.
- **What is coming back wrong**, and what you are doing about it. A call that was refused or returned an error is the most important kind to keep — without it you will simply make it again.
- **What you have concluded**, including anything you have ruled out and why. An approach that failed, omitted here, is an approach you will try again.
- **Where you are now**, and what you are about to do next.

Do not invent anything you are not sure of. In particular, do not report a result you cannot see: a call whose output is not in front of you is a call whose outcome you do not know, and writing that it succeeded is the one error here that the next turn cannot recover from.

Do not write instructions to yourself to rewrite or restore content that this replay does not itself contain — you would be reconstructing from memory something that is still on disk, and producing a worse version of it.

Write the replay and stop. Do not continue the transcript that follows these instructions, and do not copy any part of it back: it is what you are replacing."""

# --------------------------------------------------------------------- #
# The state record (full-compaction.ts:43-93) — the no-tail rescue
# --------------------------------------------------------------------- #

FULL_SECTIONS = ("## Goal", "## Standing instructions", "## Done",
                 "## In progress", "## Ruled out", "## Key facts",
                 "## Working set", "## Retrospective", "## Next")

FULL_PROMPT = """\
Everything above is about to be deleted. What you write now replaces it completely: when the work resumes, your summary will be the only thing there. Nothing you leave out can be recovered, and nothing you get wrong can be checked against anything.

Write it for the agent that resumes — which is you, without any memory of this. Not a report for a person, not a wrap-up, not an answer to the user. A state record, written so the work can continue without asking the user anything again.

This request is a system operation, not a new instruction from the user. The goal, the next step and the work in progress are all the ones that were true *before* this message arrived. Do not treat "write a summary" as the task in hand.

**Do not call any tool.** You have one turn and it must be text. A tool call here is rejected and the turn is spent, which loses the transcript with nothing to replace it.

Write every section below, in this order, with its heading exactly as given. A section with nothing in it still gets its heading and the single word `(none)` — a missing section reads as an omission by accident, and the next agent cannot tell which it was.

## Goal
What is being achieved, and any constraint the user placed on how. If the goal changed during the session, give the current one and say what it replaced.

## Standing instructions
Everything the user told you that still applies — preferences, prohibitions, things to never touch, required ways of working. **Quote these verbatim.** A paraphrased instruction is the one loss with no other source: the material worked on is still there to be re-read, but what was asked exists only in the messages being deleted.

## Done
What is finished, with the evidence that it is finished — what was changed and what confirmed it. Be exact enough that none of it is done twice. **Do not describe finished work as pending or in progress.** If something was done and later undone, say both, in that order.

## In progress
What was underway at the moment the transcript was cut, and exactly where it stopped. If you were part-way through working something out, put the partial result here in full — it exists nowhere else, and it is the thing most often lost at this point.

## Ruled out
Approaches already tried that did not work, and why each failed. Include anything that was refused, returned an error, or was blocked. This section is what stops the resumed agent from walking back into the same wall; an omission here reliably costs a repeat.

## Key facts
What was discovered or decided that is not obvious from looking at the material fresh: values, identifiers, names, signatures, structures, and the reasons behind each decision. **Copy identifiers, lists, checklists and task IDs across verbatim and complete.** Do not summarise or sample them — an enumerated set that has been through a summary is unreliable for membership, and the resumed agent will act on it as if it were not.

## Working set
What is in scope and what state it is in now: the items being worked on, where the work is happening, and what exists now that did not before. One line each. Quote a fragment only where the exact wording is what matters; otherwise name it and say what it is.

## Retrospective
Your own account of how the work actually went, which the reasoning being deleted is the only record of. Cover: what you understood late that would have helped earlier, where effort went that did not pay, any assumption you made that turned out wrong, and where you currently think the difficulty is. This is the one section whose content is judgement rather than fact — write it as judgement, and say when you are unsure.

## Next
The immediate next steps, in order, specific enough to act on without re-deriving them.

Two rules over all of it. **Do not invent anything.** If you cannot recall something, leave it out or say it is uncertain — a confident wrong detail in this summary cannot be checked against anything and will be acted on. And do not write a step that rewrites or restores content this summary does not itself contain: the material is still there to be read, and reconstructing it from memory produces a worse copy of something intact."""

# --------------------------------------------------------------------- #
# The full flow's council (council-compaction.ts:59-124)
# --------------------------------------------------------------------- #

HALF_MARKER = "<<<HALFWAY>>>"

WRITER_HALFWAY_SECTION = """\
## Mark the halfway point

Exactly once, put a line containing `<<<HALFWAY>>>` and nothing else, at the
point where you are about half way through the work you are describing.

Measure the half by the **work**, not by the words: the marker goes where the
first half of what happened ends and the second half begins. It must sit on a
boundary between steps — after one step and its outcome are complete, never
inside a step, never between a call and what it returned, and never inside a
fenced block.

Nothing else about the replay changes. It is one continuous piece of prose that
happens to carry a marker; do not write headings for the halves, do not
summarise each half, and do not refer to the marker in the text."""

CRITIC_PROMPT = """\
You wrote the replay below from the transcript that follows it. The replay has been cut in two and you own the **{{half}} half**. Someone else is rewriting the {{other_half}} half from the same transcript, at the same time, and the two halves will be joined back into one continuous replay.

So: **return the {{half}} half only.** Do not return the {{other_half}} half, do not restate it, do not summarise it, do not lead into it or round it off. It is shown to you for one reason only — so you can check your own half against it and see where your half ends. Anything of it you reproduce will appear twice in the joined replay, once from you and once from the writer who owns it.

**The user's own words stay.** If your half quotes what the user asked for, that quotation is the most load-bearing text in it — it is the only place the instruction survives at all once the transcript is gone. Keep it word for word. Do not paraphrase it, do not shorten it, and never drop it to make room.

**You are revising a draft, not writing one.** Start from the text below and change what is wrong with it. Do not re-derive your half from the transcript and write it out afresh: a step that is already right is already done, and rewriting it from scratch is how a correct sentence becomes a different, shorter, wronger one.

**About the `[#7]` marks.** Those are citations, not step numbers and not part of the prose. Each one names a call in the numbered record, and the harness replaces it with that call before anyone reads this. So:

- **Keep every citation your half already has, exactly as it is, where it is.** Do not renumber them, do not turn them into a numbered list, do not write them out as "Step 7".
- **Only cite a number that appears in the record.** If your half describes something with no call behind it, leave it uncited — inventing a number attaches your sentence to somebody else's call, or to nothing.
- If the transcript shows a call your half never mentions, add the step and cite its number from the record.

What to change in your half:

- **Something that happened and is missing.** A call that was made, an answer that came back, an instruction that was given, a conclusion that was reached, an approach that was ruled out. Add it.
- **Something the transcript contradicts.** A call reported as succeeding that returned an error; a file said to have been read that was refused; a count, a line number or a filename that does not match. Correct it to what the transcript shows.
- **Something quoted that does not match.** The user's own words, error text, identifiers, paths and numbers have to be character for character what the transcript holds.
- **Something the {{other_half}} half contradicts.** Ground your half against it: the two are one account of one session, and a fact stated one way in your half and another way there is a fact to settle from the transcript.
- **Prose that has drifted.** Fix it. If a step is written as a report of itself — past tense, or narrated as something finished — rewrite it as the step and its outcome. First person, present continuous, the voice of someone picking the work back up rather than recounting it.

Write every step as the step itself, then what came back as its own short sentence after it. This holds in the middle of your half and not only at its ends.

**Length.** Your half is {{half_length}} characters. Return something close to that — within about 10% either way. You are correcting and rewriting it, not condensing it: material you drop is material nothing else will carry, because the transcript it came from is being deleted.

Answer with the {{half}} half of the replay and nothing else. No heading, no preamble, no note about what you changed, no marker line. Just the prose."""

SYNTHESIZER_PROMPT = """\
A replay of your recent work was cut in half, and each half was rewritten against the full transcript by a different writer. Join them back into one continuous replay.

Both writers had the whole transcript, so both halves are grounded in the record — but each owned only its own half, and either could be wrong about the seam between them or about a fact the other half settles differently. Cross-check the two against each other: where they disagree about the same fact, keep the version that quotes the transcript over the version that describes it; where one states something the other contradicts, keep the one that is specific.

What you are producing is one piece of prose, not two halves stacked up. Make the seam invisible: no heading between them, no marker line, no sentence that restarts or recaps. If the two writers both wrote the same step — once at the end of the first half and once at the start of the second — keep it once.

Keep it in the first person and the present continuous tense, every step written as the step and its outcome after it.

**Length.** The replay was {{original_length}} characters before it was rewritten. Aim at that. You may go up to {{max_length}} — about 10% more — and you should use that allowance only where it takes the extra room to keep something that would otherwise be lost. Do not use it to be more thorough for its own sake, and do not come in far under: material dropped here is material nothing else carries."""

RETROSPECTIVE_REVISION = (
    "Then the retrospective, which is a judgement about how the work went "
    "rather than a record of what happened. You are the first to see the "
    "finished replay, so you are the first who can judge it properly. "
    "Revise the retrospective against the replay you have just joined: drop "
    "a judgement the replay does not bear out, add one it makes obvious, "
    "sharpen one that is vague. Its rules hold — no file names, no "
    "identifiers, no narration of events, and terse. If it is already "
    "right, return it unchanged.")

# --------------------------------------------------------------------- #
# The reduced flow's reviewer (ours; the critic's rules on the whole)
# --------------------------------------------------------------------- #

REVIEWER_PROMPT = """\
You wrote the replay below from the transcript that follows it. Before it replaces that transcript, check it against the record and correct it.

**The user's own words stay.** Where the replay quotes what the user asked for, that quotation is the most load-bearing text in it — it is the only place the instruction survives at all once the transcript is gone. Keep it word for word. Do not paraphrase it, do not shorten it, and never drop it to make room.

**You are revising a draft, not writing one.** Start from the text below and change what is wrong with it. Do not re-derive the replay from the transcript and write it out afresh: a step that is already right is already done, and rewriting it from scratch is how a correct sentence becomes a different, shorter, wronger one.

**About the `[#7]` marks.** Those are citations, not step numbers and not part of the prose. Each one names a call in the numbered record, and the harness replaces it with that call before anyone reads this. So:

- **Keep every citation the replay already has, exactly as it is, where it is.** Do not renumber them, do not turn them into a numbered list, do not write them out as "Step 7".
- **Only cite a number that appears in the record.** If the replay describes something with no call behind it, leave it uncited — inventing a number attaches your sentence to somebody else's call, or to nothing.
- If the transcript shows a call the replay never mentions, add the step and cite its number from the record.

What to change:

- **Something that happened and is missing.** A call that was made, an answer that came back, an instruction that was given, a conclusion that was reached, an approach that was ruled out. Add it.
- **Something the transcript contradicts.** A call reported as succeeding that returned an error; a file said to have been read that was refused; a count, a line number or a filename that does not match. Correct it to what the transcript shows.
- **Something quoted that does not match.** The user's own words, error text, identifiers, paths and numbers have to be character for character what the transcript holds.
- **Prose that has drifted.** Fix it. If a step is written as a report of itself — past tense, or narrated as something finished — rewrite it as the step and its outcome. First person, present continuous, the voice of someone picking the work back up rather than recounting it.

Write every step as the step itself, then what came back as its own short sentence after it. This holds in the middle of the replay and not only at its ends.

**Length.** The replay is {{original_length}} characters. Return something close to that. You may go up to {{max_length}} — about 10% more — and only where the extra room keeps something that would otherwise be lost. You are correcting it, not condensing it: material you drop is material nothing else will carry, because the transcript it came from is being deleted."""

# --------------------------------------------------------------------- #
# Overrun feedback (agentic-compaction.ts:224-232)
# --------------------------------------------------------------------- #


def describe_past_key(cited: list[int], calls: int) -> str:
    """Tell the writer it narrated calls that never happened. A writer
    whose span ended mid-task carried on in the present tense ("Then
    src/cluster/. [#63-67]") to #174 against a 55-call key
    (compaction-v7-cite, 2026-10-24): a forecast written as a record."""
    lo, hi = min(cited), max(cited)
    span = f"#{lo}" if lo == hi else f"#{lo}–#{hi}"
    return "\n".join([
        "",
        "",
        f"Your previous attempt cited {span}. Only #1–#{calls} happened in "
        f"this part of the conversation; nothing after #{calls} has been "
        f"done yet, so it was rejected.",
        "",
        f"Write it again ending where the conversation above ends, at "
        f"#{calls}. Do not narrate what comes next: the turns after it are "
        f"still in front of you and will say for themselves what was done.",
    ])


def describe_overrun(measured_tokens: int, limit_tokens: int) -> str:
    """Tell the writer, in numbers, that its last attempt did not fit.
    The one place length is worth talking about: a measurement against a
    number the model cannot otherwise see, not an adjective."""
    return "\n".join([
        "",
        "",
        f"Your previous attempt was about {measured_tokens} tokens. It has "
        f"to fit in {limit_tokens} and it did not, so it was rejected and "
        f"nothing was kept from it.",
        "",
        f"Write it again under {limit_tokens} tokens. Keep every section — a "
        f"missing section reads as an omission by accident. Shorten within "
        f"them instead: fewer words per item, no restating the same fact in "
        f"two sections, no preamble. Two things are never shortened: quoted "
        f"standing instructions, and identifiers, lists and checklists, "
        f"which have to stay verbatim and complete however little room is "
        f"left.",
    ])


def render(template: str, **values) -> str:
    """Substitute ``{{name}}``; an unknown placeholder stays as written."""
    return re.sub(r"\{\{\s*([a-z_]+)\s*\}\}",
                  lambda m: str(values[m.group(1)]) if m.group(1) in values
                  else m.group(0), template)


def with_halfway_section(prompt: str) -> str:
    """The writer prompt with the halfway instruction, placed before the
    closing "stop" paragraph so the last thing before the transcript is
    still the instruction not to continue it. Not added twice."""
    if re.search(r"<<<\s*HALFWAY\s*>>>", prompt):
        return prompt
    stop = prompt.rfind("\n\nWrite the replay and stop.")
    if stop >= 0:
        return prompt[:stop] + "\n\n" + WRITER_HALFWAY_SECTION + prompt[stop:]
    return prompt.rstrip() + "\n\n" + WRITER_HALFWAY_SECTION


__all__ = [
    "WRITER_SYSTEM_PROMPT", "FULL_SYSTEM_PROMPT", "RETROSPECTIVE_SYSTEM_PROMPT",
    "CRITIC_SYSTEM_PROMPT", "SYNTHESIZER_SYSTEM_PROMPT",
    "REVIEWER_SYSTEM_PROMPT", "REPLAY_PROMPT", "FULL_PROMPT", "FULL_SECTIONS",
    "HALF_MARKER", "WRITER_HALFWAY_SECTION", "CRITIC_PROMPT",
    "SYNTHESIZER_PROMPT", "RETROSPECTIVE_REVISION", "REVIEWER_PROMPT",
    "describe_overrun", "render", "with_halfway_section",
]
