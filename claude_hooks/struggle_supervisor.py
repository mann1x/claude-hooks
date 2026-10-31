"""The struggle supervisor for one tool loop (plan D2).

Ported from Cerebriline's worker supervisor (``sdk/packages/core/src/runtime/
safety/worker-struggle.ts``), whose thresholds were set by replaying two
75-agent swarms (150 workers) through it:

- **The loop is thinking that runs out its budget, turn after turn**, while
  the model tells itself it has enough to report and probes once more. Not
  exact repetition (the repeat guards stay quiet), not a long run.
- **Long is not stuck.** The longest workers all answered in the end, so a
  turn count and a failing-call streak only ever *nudge*.

Machine: ``watching`` → (3 budget-exhausted turns in the last 6, or half
the iteration cap reached, or half the recent tool calls failing) → one
nudge, ``nudged`` → after a 2-turn grace window, 2 more budget-exhausted
turns → ``stopped``, which ``run_loop`` turns into one forced report with
``stop_reason = struggle``. It never suspends: the fork's stop hands the
worker to its lead, and a council lane has none.

Refit for our lanes (plan D2): the nudge sits at half the loop's own
``max_iterations``, floored at the fork's minimum iteration (8), rather
than the fork's fixed 20 — our loops run 6 to 25 iterations, so a loop
shorter than 8 is never nudged for its length. The fork's struggle
detector (refused-edit streaks, distress, a scored window) is approximated
by the failure share of the last tool results: it only nudges either way.

Refit by replay (2026-10-16, ``benchmarks/consultants/guard_replay.py``,
911 recorded lanes): a length nudge on the count alone fired on 443 lanes,
204 of which went on to find something new in most of their next calls
and 5 of which were stuck. Past the length the nudge now also needs the
lane to have gone stale: at most :data:`STALE_MAX_NEW` of its last
:data:`STALE_WINDOW` calls brought anything new (a call not made before,
that did not fail, whose answer was not seen before). That fires 8 times
on the same lanes, none on a lane that was still progressing.

Always on, every backend (owner, 2026-10-10).
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Optional

MIN_ITERATION = 8
NUDGE_AFTER_ITERATIONS = 20
BUDGET_WINDOW = 6
BUDGET_TURNS_TO_NUDGE = 3
BUDGET_TURNS_TO_STOP = 2
GRACE_ITERATIONS = 2
FAILURE_WINDOW = 6
FAILURES_TO_NUDGE = 3
STALE_WINDOW = 6
STALE_MAX_NEW = 1


def nudge_after(max_iterations: Optional[int]) -> int:
    """Half the cap, floored at :data:`MIN_ITERATION`; the fork's default
    with no cap."""
    if not isinstance(max_iterations, int) or max_iterations <= 0:
        return NUDGE_AFTER_ITERATIONS
    return max(MIN_ITERATION, round(max_iterations * 0.5))


def describe_nudge(reason: str) -> str:
    measured = {
        "thinking-budget": ("Your thinking has run out its whole budget on "
                            "turn after turn, and each time you have gone "
                            "back for one more probe instead of answering."),
        "struggle": "Your recent calls keep coming back failing.",
    }.get(reason, "You have spent many turns on this without converging.")
    return "\n".join([
        f"[SYSTEM MESSAGE] {measured} The turns left before this task is "
        "stopped for you are few.",
        "",
        "Commit what you have now. Write your final answer as a summary of "
        "what you found — what you are sure of, what you are not, and anything "
        "still open — rather than taking another probe at the same thing. A "
        "partial finding you report is worth more than a complete one the "
        "stop throws away.",
    ])


def describe_stop(reason: str, spent_after_nudge: int) -> str:
    return (f"you were told to commit a summary of what you had, and your "
            f"thinking then ran out its whole budget on {spent_after_nudge} "
            f"more turn{'s' if spent_after_nudge != 1 else ''}, each going back "
            "for another probe.")


@dataclass
class StruggleSupervisor:
    max_iterations: Optional[int] = None
    phase: str = "watching"            # watching | nudged | stopped
    reason: str = ""
    nudged_at: Optional[int] = None
    spent: list = field(default_factory=list)
    outcomes: deque = field(default_factory=lambda: deque(maxlen=FAILURE_WINDOW))
    novel: deque = field(default_factory=lambda: deque(maxlen=STALE_WINDOW))
    _calls_seen: set = field(default_factory=set)
    _answers_seen: set = field(default_factory=set)

    def close_iteration(self, iteration: int, *, budget_spent: bool,
                        failures: list[bool] = (),
                        calls: list[tuple[str, str, bool]] = ()
                        ) -> Optional[tuple[str, str]]:
        """Fold one finished iteration. ``("nudge", reason)``,
        ``("stop", reason)`` or ``None``. ``calls`` is the batch as
        ``(call key, answer, failed)``: what the staleness window reads."""
        if budget_spent:
            self.spent.append(iteration)
        self.outcomes.extend(failures)
        for key, answer, failed in calls:
            digest = hash(answer or "")
            self.novel.append(key not in self._calls_seen and not failed
                              and digest not in self._answers_seen)
            self._calls_seen.add(key)
            self._answers_seen.add(digest)
        if self.phase == "watching":
            recent = sum(1 for s in self.spent if s > iteration - BUDGET_WINDOW)
            if recent >= BUDGET_TURNS_TO_NUDGE:
                return self._nudge(iteration, "thinking-budget")
            if (iteration >= MIN_ITERATION
                    and sum(self.outcomes) >= FAILURES_TO_NUDGE):
                return self._nudge(iteration, "struggle")
            # 0-based iterations: this is turn ``iteration + 1``.
            if (iteration + 1 >= nudge_after(self.max_iterations)
                    and self.stale()):
                return self._nudge(iteration, "non-progress")
            return None
        if self.phase == "nudged" and self.nudged_at is not None:
            after = sum(1 for s in self.spent
                        if s > self.nudged_at + GRACE_ITERATIONS)
            if after >= BUDGET_TURNS_TO_STOP:
                self.phase = "stopped"
                self.reason = "thinking-budget"
                return ("stop", "thinking-budget")
        return None

    def stale(self) -> bool:
        """A full window of recent calls with at most
        :data:`STALE_MAX_NEW` bringing anything new."""
        return (len(self.novel) == STALE_WINDOW
                and sum(self.novel) <= STALE_MAX_NEW)

    def spent_after_nudge(self) -> int:
        if self.nudged_at is None:
            return 0
        return sum(1 for s in self.spent if s > self.nudged_at + GRACE_ITERATIONS)

    def _nudge(self, iteration: int, reason: str) -> tuple[str, str]:
        self.phase = "nudged"
        self.reason = reason
        self.nudged_at = iteration
        return ("nudge", reason)


__all__ = ["StruggleSupervisor", "nudge_after", "describe_nudge",
           "describe_stop", "MIN_ITERATION", "BUDGET_WINDOW",
           "BUDGET_TURNS_TO_NUDGE", "BUDGET_TURNS_TO_STOP", "GRACE_ITERATIONS",
           "STALE_WINDOW", "STALE_MAX_NEW"]
