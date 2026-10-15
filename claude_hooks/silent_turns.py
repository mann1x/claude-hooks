"""A turn without a tool call is not always an answer (plan D3, standard).

In a tool loop a turn that calls nothing ends the lane: that is the
contract, and a lane's final report is exactly such a turn. Three shapes
end it wrongly, each ported from Cerebriline with its own reminder:

thinking only      reasoning and nothing else — no answer, no call.
                   13 of 15 fork agents ended this way on one session.
                   When the reasoning carries tool-call markup the call
                   was written before the thinking closed, so it never
                   ran; the reminder says that instead.
unparsed call      a ``<tool_call><function=…>`` block the provider handed
                   back as text (Ollama's qwen parser does, rather than
                   fail the request). The model tried to act.
announced intent   the answer ends on a first-person promise of imminent
                   work ("I'll now read …") with no claim of completion.

Budgets: 2 thinking-only re-asks in a row (plan D3; the fork allows 3),
2 unparsed-call re-asks in a row, 1 announced-intent nudge per lane; a
tool-calling turn resets the first two. Non-convergence (moved here from
D1): consecutive silent turns that were re-asked, each with at least
:data:`NON_CONVERGENCE_MIN_REASONING_CHARS` of reasoning — at
:data:`NON_CONVERGENCE_STREAK` one line is added, once per lane.

Divergence: the fork's intent pattern matches "let me …", which a council
report ending "Let me know if …" trips; that phrase is excluded.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

THINKING_ONLY_RETRY_BUDGET = 2
TOOL_CALL_PARSE_RETRY_BUDGET = 2
ANNOUNCED_INTENT_BUDGET = 1
NON_CONVERGENCE_STREAK = 2
NON_CONVERGENCE_MIN_REASONING_CHARS = 2000

THINKING_ONLY_REMINDER = (
    "[SYSTEM MESSAGE] Your last reply was thinking only: no answer and no "
    "tool call arrived. End your thinking, then either make one tool call or "
    "write your answer. Keep the thinking short.")

THINKING_ONLY_ANSWER_REMINDER = (
    "[SYSTEM MESSAGE] Your last reply was thinking only: no answer arrived. "
    "Tools are off now. End your thinking and write your answer. Keep the "
    "thinking short.")

TOOL_CALL_IN_THINKING_REMINDER = (
    "[SYSTEM MESSAGE] Your last reply ended inside your thinking: the tool "
    "call you wrote there was part of the thinking, so it never ran and "
    "nothing was changed by it. Close your thinking first, then send that "
    "one call again as an actual tool call. Keep the thinking short.")

_TOOL_CALL_MARKUP = re.compile(r"</?tool_call>|</function>|<function=|</parameter>")
_TOOL_CALL_TAG = re.compile(r"<tool_call>|<parameter=[^>]*>|</parameter>")
_FUNCTION_OPEN = re.compile(r"<function=([^\s>]+)\s*>")

_ANNOUNCED_INTENT = re.compile(
    r"\b(?:let me(?! know)|i'?ll|i will|i'?m going to|i am going to|"
    r"now i(?:'?ll| will)|next,? i(?:'?ll| will)|let'?s)\b", re.I)
_CLAIMS_COMPLETION = re.compile(
    r"\b(?:task (?:is )?(?:complete|finished|done)|all done|is now "
    r"(?:complete|finished|fixed|working)|i(?:'?m| am) (?:done|finished)|"
    r"nothing (?:else|more) to do|no further changes)\b", re.I)


def thinking_only(content: str, reasoning: str) -> Optional[bool]:
    """``None`` when the turn delivered something; else whether the
    reasoning holds tool-call markup."""
    if (content or "").strip() or not (reasoning or "").strip():
        return None
    return bool(_TOOL_CALL_MARKUP.search(reasoning))


@dataclass(frozen=True)
class UnparsedCall:
    blocks: int
    restarts: int
    name: Optional[str] = None


def unparsed_tool_call(text: str) -> Optional[UnparsedCall]:
    """A tool call handed back as text. Both tags are required: prose about
    ``<tool_call>`` names no function."""
    if not text or "<tool_call>" not in text or not _FUNCTION_OPEN.search(text):
        return None
    blocks = opened = closed = 0
    for m in _TOOL_CALL_TAG.finditer(text):
        tag = m.group(0)
        if tag == "<tool_call>":
            blocks += 1
        elif tag == "</parameter>":
            closed += 1
        else:
            opened += 1
    if blocks == 0:
        return None
    unterminated = max(opened - closed, 0)
    restarts = min(max(blocks - 1, 0), unterminated)
    names = _FUNCTION_OPEN.findall(text)
    return UnparsedCall(blocks, restarts, names[-1] if names else None)


def unparsed_reminder(found: UnparsedCall) -> str:
    called = f"`{found.name}`" if found.name else "a tool"
    if found.restarts > 0:
        times = f", {found.restarts} times over" if found.restarts > 1 else ""
        what = (f" You began {called} and then, part-way through a parameter "
                f"you never closed, started the whole call again{times}. The "
                "reader cannot tell an abandoned call from the value of the "
                "parameter it was abandoned inside, so all of it came back as "
                "text.")
    else:
        what = (f" It named {called}, but the block was not well formed and "
                "could not be read.")
    return ("[SYSTEM MESSAGE] Your last message contained a tool call that "
            "could not be read." + what + " Nothing ran, so nothing has "
            "changed. Emit the call once, in one clean block: open it, give "
            "each parameter and close each parameter, then close the function "
            "and the call. Do not restart a call once you have begun it — "
            "finish it, or send nothing and start the next turn fresh.")


#: Plan D4: re-asks for a call the server rejected as unreadable
#: (``ChatFault.fault_class == "tool_call_unparsable"``), in a row; counted
#: apart from the HTTP retries, which never resend such a call.
TOOL_CALL_REJECTED_RETRY_BUDGET = 2

TOOL_CALL_UNPARSABLE_REMINDER = (
    "[SYSTEM MESSAGE] Your last tool call did not parse, so it never ran and "
    "nothing was changed by it. The text you wrote before it is still here "
    "and still correct — the call around it was malformed, most often "
    "because it was cut short. Send that one call again, complete, and "
    "nothing else in this reply. If it carries a large argument, make the "
    "argument smaller rather than sending the same one again: name a line "
    "range instead of a whole file, or split the work across two calls.")


def rejected_call_reminder(rejection: Optional[dict] = None) -> str:
    """The reminder for a call the server rejected (Cerebriline
    ``toolCallUnparsableReminder``). ``swallowed_key`` is the one measured
    shape — a string argument full of code closed with a backtick from that
    code, so the parser read the next argument into the value — and naming
    that argument is the one thing that makes the next attempt different;
    every other reason gets the generic text."""
    if not rejection or rejection.get("reason") != "swallowed_key":
        return TOOL_CALL_UNPARSABLE_REMINDER
    tool, key = rejection.get("tool"), rejection.get("key")
    call = f"`{tool}` call" if tool else "tool call"
    into = (f"ran on into `{key}`, so the parser read `{key}` and what "
            "followed as part of that value" if key else
            "ran on into the next argument, so the parser read that argument "
            "as part of the value")
    return (f"[SYSTEM MESSAGE] Your last {call} was rejected before it ran, "
            "and nothing was changed by it. A string argument in it never "
            f"closed: it {into}. This happens when a string argument that "
            "contains code is ended with a character from that code, such as "
            "a backtick or a quote, instead of the tool-call format's own "
            "string delimiter. Send that one call again, complete, and "
            "nothing else in this reply, with every string argument closed by "
            "the format's delimiter.")


def truncated_arguments_answer(chars: int) -> str:
    """The answer to a call whose arguments the output cap cut off. It is
    not bad JSON, and a model told so hunts for a syntax error that is not
    there (Cerebriline ``agent-runtime.ts`` ``finishReason == max-tokens``).
    Starts with ``error`` so the strike counter sees a failure."""
    return ("error: this call was cut off by the output budget before its "
            f"arguments finished: the turn ended at the cap with {chars:,} "
            "characters of arguments written. Nothing is wrong with what you "
            "were writing — there was no room left to finish it. Nothing ran. "
            "Send the same work in smaller pieces: one region or one range per "
            "call, with arguments short enough to complete.")


def announced_intent(text: str) -> Optional[str]:
    """The text when it ends on a promise of imminent work and claims no
    completion anywhere; else ``None``."""
    trimmed = (text or "").strip()
    if not trimmed or _CLAIMS_COMPLETION.search(trimmed):
        return None
    return trimmed if _ANNOUNCED_INTENT.search(trimmed[-400:]) else None


def announced_intent_reminder(announcement: str) -> str:
    quoted = " ".join(announcement.split())[-200:]
    return (f'[SYSTEM MESSAGE] You wrote "{quoted}" and then called nothing. '
            "Saying what you are about to do is not doing it, and this task "
            "ends on a turn with no tool call, with the work undone. Emit the "
            "tool call itself now — the first one, not a description of it. "
            "If you are finished instead, write your answer; if you cannot "
            "go on, say plainly what is stopping you in one sentence.")


def non_convergence_note(turns: int, reasoning_chars: int) -> str:
    thousands = round(reasoning_chars / 1000)
    spent = (f"about {thousands:,},000 characters of reasoning" if thousands >= 1
             else "a full turn of reasoning")
    return (f"You have now taken {turns} turns in a row without calling a "
            f"single tool, spending {spent} on them. Thinking further has not "
            "moved this task, and it will not. Stop analysing and make one "
            "tool call now — the smallest one that makes any progress at all, "
            "because a call you can check beats an analysis you cannot. If you "
            "genuinely cannot make one, say what is blocking you in a single "
            "sentence and stop.")


__all__ = ["thinking_only", "unparsed_tool_call", "unparsed_reminder",
           "announced_intent", "announced_intent_reminder",
           "non_convergence_note", "UnparsedCall",
           "rejected_call_reminder", "truncated_arguments_answer",
           "TOOL_CALL_UNPARSABLE_REMINDER", "TOOL_CALL_REJECTED_RETRY_BUDGET",
           "THINKING_ONLY_REMINDER", "THINKING_ONLY_ANSWER_REMINDER",
           "TOOL_CALL_IN_THINKING_REMINDER", "THINKING_ONLY_RETRY_BUDGET",
           "TOOL_CALL_PARSE_RETRY_BUDGET", "ANNOUNCED_INTENT_BUDGET",
           "NON_CONVERGENCE_STREAK", "NON_CONVERGENCE_MIN_REASONING_CHARS"]
