"""Repeated tool-call loop detection and the mistake limit (plan D10, D14).

Ported from Cerebriline (``sdk/packages/core/src/runtime/safety/
loop-detection.ts``) with its thresholds. Three ways one call repeats:

consecutive    the same call, back to back: warned at 2, hard at 5.
barren         the same call failing again with the same arguments,
               counted per signature so an interleaved call does not reset
               it: warned on every attempt after the first, hard at 6.
result cycle   the same call answered the same way, however far apart:
               warned at 3; hard at 6 for a write-class tool only — a read
               or a command answered the same way is sometimes the job.

Polling tools are exempt (each answer is a later picture of something
that moves by itself). A success clears its own failure tally.

The fork's no-op path ("futile": the tool answered the payload in advance
as already in place) has no counterpart here: none of our tools refuse a
write as a no-op, so it is not ported.

Verdicts are the diagnosis only. ``run_loop`` decides what follows: a soft
verdict rides on the tool result, the first hard one is a last warning
(the call is not run) and the second ends the loop with one forced report,
``stop_reason = loop_guard``.

:class:`MistakeLimit` is D14: consecutive failed tool calls of any kind;
the sixth ends the loop with ``stop_reason = mistake_limit``.

Always on, every backend, in no profile (owner, 2026-10-10).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

#: Failures of one unchanged call before it is stopped (fork STRIKE_LIMIT);
#: equal to the mistake limit on purpose, so a model told it has strikes
#: left does not discover a second, shorter budget also counting.
STRIKE_LIMIT = 6
SOFT_THRESHOLD = 2
HARD_THRESHOLD = 5
CYCLE_REPEAT_LIMIT = 3
CYCLE_HARD_LIMIT = 6
MISTAKE_LIMIT = 6

#: Tools that change the workspace (named, never inferred). Our write tools
#: are the ones ``agent_loop.runner.is_write_tool`` recognises; these are
#: the names the shipped surfaces use.
WRITE_CLASS_TOOL_NAMES = frozenset({"write_file", "edit_file", "apply_patch",
                                    "create_file", "delete_file", "move_file"})

#: Tools whose same call rightly repeats. None ship today; a caller with a
#: status/await tool passes it through ``LoopTracker(polling=...)``.
POLLING_TOOL_NAMES: frozenset[str] = frozenset()

#: Tools addressing a file by matching its text (steering step 2).
EDITING_TOOL_NAMES = frozenset({"edit_file", "apply_patch"})

REPEATED_CALL_VALUE_CHARS = 48


def is_failed_output(output: str) -> bool:
    """Whether a tool's answer is a failure, by the convention every tool
    surface here follows (``error: …``) plus a JSON ``success: false``."""
    text = (output or "").lstrip().lower()
    if text.startswith("error") or text.startswith("traceback"):
        return True
    head = text[:200].replace(" ", "")
    return '"success":false' in head


def _sort_keys(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _sort_keys(value[k]) for k in sorted(value)}
    if isinstance(value, list):
        return [_sort_keys(v) for v in value]
    return value


def call_signature(args: Any) -> str:
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (TypeError, ValueError):
            return args
    if args is None:
        return "null"
    try:
        return json.dumps(_sort_keys(args), separators=(",", ":"))
    except (TypeError, ValueError):
        return str(args)


def result_signature(output: str) -> str:
    """The answer with the part that cannot repeat taken out (revision
    ordinals), so an otherwise identical answer compares equal."""
    return re.sub(r"#\d+", "#n", output or "")


def describe_call(name: str, args: Any) -> str:
    """A repeated call as someone who did not make it can read it."""
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (TypeError, ValueError):
            pass

    def quote(v: Any) -> str:
        if isinstance(v, str):
            flat = " ".join(v.split())
            if len(flat) > REPEATED_CALL_VALUE_CHARS:
                flat = flat[:REPEATED_CALL_VALUE_CHARS - 1] + "…"
            return json.dumps(flat)
        if isinstance(v, list):
            more = ", …" if len(v) > 3 else ""
            return "[" + ", ".join(quote(x) for x in v[:3]) + more + "]"
        if isinstance(v, dict):
            items = list(v.items())
            more = ", …" if len(items) > 6 else ""
            return ("{" + ", ".join(f"{k}: {quote(x)}" for k, x in items[:6])
                    + more + "}")
        return str(v)
    return f"`{name}` " + ("with no arguments" if args is None else quote(args))


def strike_warning(remaining: int) -> str:
    if remaining <= 1:
        return ("WARNING: this is the LAST strike! Another failure and the "
                "harness will STOP this task. Do not send this call again — "
                "change the arguments, use a different tool, or say what you "
                "are stuck on.")
    return (f"Warning: you have only {remaining} strikes left before the "
            "harness stops this task. Sending the same call again spends one "
            "for nothing.")


def steering_for(attempt: int, tool: str) -> str:
    """A different concrete move each time, escalating in scope."""
    if attempt <= 1:
        return ("Before sending anything else: look at what this call targets "
                "as it is right now, with a tool that reads rather than "
                "writes, and compare that against what you meant it to be. If "
                "it already says what you wanted, this piece is done — record "
                "that and move to the next one.")
    if attempt == 2:
        if tool in EDITING_TOOL_NAMES:
            return ("If it does not say what you wanted, this call is not "
                    "landing where you think it is. Change how it addresses "
                    "the file rather than what it writes: re-read the exact "
                    "lines and match them as they are, or rewrite the smaller "
                    "range around them.")
        return ("If the answer matters, change what produces it rather than "
                "asking again. Nothing about this call has changed, so nothing "
                "about its answer can — something has to happen first: make "
                "the edit, or use a different tool that answers the same "
                "question from the file itself.")
    return ("Leave this alone now. Take the next thing that is still wrong "
            "and work on that; come back here only with something you have "
            "not already tried. If there is nothing else wrong, say in one "
            "sentence what is blocking you and stop — that is more use than "
            "another attempt.")


@dataclass(frozen=True)
class LoopVerdict:
    kind: str                     # "ok" | "soft" | "hard"
    message: str = ""
    rule: str = ""                # consecutive | barren | result_cycle
    count: int = 0


OK = LoopVerdict("ok")


@dataclass
class LoopTracker:
    polling: frozenset = POLLING_TOOL_NAMES
    write_class: frozenset = WRITE_CLASS_TOOL_NAMES
    last_key: str = ""
    consecutive: int = 0
    barren: dict = field(default_factory=dict)
    cycles: dict = field(default_factory=dict)    # key -> (answer, repeats)
    pending: str = ""

    def inspect(self, name: str, args: Any) -> LoopVerdict:
        if name in self.polling:
            # Not counted, and not an interruption of another call's run.
            self.pending = ""
            return OK
        key = f"{name}:{call_signature(args)}"
        self.pending = key

        barren = self.barren.get(key, 0)
        if barren >= STRIKE_LIMIT:
            return LoopVerdict(
                "hard", f"This exact call to `{name}` has already been made "
                f"{barren} times and failed every time. The arguments have not "
                "changed between attempts, so neither will the result.",
                "barren", barren)
        if barren > 0:
            return LoopVerdict(
                "soft", f"This `{name}` call has now failed {barren} "
                f"time{'s' if barren != 1 else ''} with these exact arguments, "
                "and nothing about them has changed between attempts.\n\n"
                f"{steering_for(barren, name)}\n\n"
                f"{strike_warning(STRIKE_LIMIT - barren)}",
                "barren", barren)

        # Checked before the consecutive rule: it survives an interleaved call.
        cycle = self.cycles.get(key)
        if cycle and cycle[1] >= CYCLE_REPEAT_LIMIT - 1:
            n = self._consecutive(key)
            repeats = cycle[1]
            # Divergence from the fork, which discards the consecutive
            # verdict here, so a call sent back to back and answered the same
            # way only ever warns. The softness exists for a command re-run
            # after each edit; with nothing in between there was no edit.
            if n >= HARD_THRESHOLD:
                return LoopVerdict(
                    "hard", f"Detected {n} consecutive identical calls to "
                    f"`{name}`, answered the same way each time. The arguments "
                    "have not changed between them, so neither will the "
                    "result.", "consecutive", n)
            if name in self.write_class and repeats >= CYCLE_HARD_LIMIT - 1:
                return LoopVerdict(
                    "hard", f"This `{name}` call has been made {repeats} times "
                    "with the same arguments and answered the same way every "
                    "time. It changes the file to a state it has already been "
                    "in, so repeating it cannot move the task forward.",
                    "result_cycle", repeats)
            return LoopVerdict(
                "soft", f"This `{name}` call has been answered {repeats} times "
                "with the same answer, and the arguments have not changed. "
                "Whatever it is being asked, it has already said everything it "
                f"is going to say.\n\n{steering_for(repeats, name)}",
                "result_cycle", repeats)

        n = self._consecutive(key)
        if n >= HARD_THRESHOLD:
            return LoopVerdict(
                "hard", f"Detected {n} consecutive identical calls to `{name}`. "
                "The arguments have not changed between them, so neither will "
                "the result.", "consecutive", n)
        if n == SOFT_THRESHOLD:
            return LoopVerdict(
                "soft", f"Detected {n} consecutive identical calls to "
                f"`{name}`.\n\n{steering_for(n - 2, name)}", "consecutive", n)
        return OK

    def _consecutive(self, key: str) -> int:
        self.consecutive = self.consecutive + 1 if key == self.last_key else 1
        self.last_key = key
        return self.consecutive

    def note_outcome(self, productive: bool, answer: Optional[str] = None) -> None:
        """Attribute an outcome to the call :meth:`inspect` last saw."""
        key = self.pending
        if not key:
            return
        if answer is not None:
            sig = result_signature(answer)
            prev = self.cycles.get(key)
            self.cycles[key] = ((sig, prev[1] + 1) if prev and prev[0] == sig
                                else (sig, 1))
        if productive:
            self.barren.pop(key, None)
        else:
            self.barren[key] = self.barren.get(key, 0) + 1

    def reset(self) -> None:
        self.__init__(polling=self.polling, write_class=self.write_class)


@dataclass
class MistakeLimit:
    """Plan D14: consecutive failed tool calls; a success resets it."""
    limit: int = MISTAKE_LIMIT
    streak: int = 0

    def note(self, failed: bool) -> bool:
        """Count one outcome; ``True`` when the limit is reached."""
        self.streak = self.streak + 1 if failed else 0
        return self.streak >= self.limit


__all__ = ["LoopTracker", "LoopVerdict", "MistakeLimit", "is_failed_output",
           "call_signature", "result_signature", "describe_call",
           "STRIKE_LIMIT", "MISTAKE_LIMIT", "WRITE_CLASS_TOOL_NAMES",
           "POLLING_TOOL_NAMES"]
