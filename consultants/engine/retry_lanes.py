"""Which researcher lanes of a finished run can be run again, and as what
(plan D9, ``retry-lanes``).

A lane is retried when its first-round exit (plan D7's ``node_exit`` row)
carries a failure class that says nothing about the question: the
transport or server was down (``infra``), the model was not there
(``model_missing``), or the prompt did not fit (``context_overflow``).
A lane that failed on its task (``task_error``), was cancelled, or ended
on a guard is not: running it again asks the same thing of the same model.

A lane is identified the way the planner fan-out made it: global lane
``i`` is plan-item group ``i // n_models`` asked of model
``i % n_models`` (``graph._fanout_after_planner``). The groups are rebuilt
from the parent's plan items with today's ``FANOUT_MAX_LANES`` and the
models from today's config, so a change to either between the run and
the retry moves lanes; :func:`retry_specs` returns what it will run, and
the route reports it.

Only first-round lanes are retried. A critic re-route's lane was asked
the critic's gaps, which are not stored as a plan item.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Optional

from consultants.engine import council

#: Failure classes (plan D7) that make a lane worth running again.
RETRYABLE_CLASSES = frozenset({"infra", "model_missing", "context_overflow"})

#: The prefixes every researcher tombstone opens with (``council.py``).
_FAILED_PREFIXES = ("(researcher lane failed", "(researcher lane produced no")


def lane_stops(db_path: Path) -> list[dict]:
    """How each node ended (plan D7): ``stop_reason`` and
    ``failure_class`` from the ``node_exit`` rows that carry them, in the
    order they finished. Empty for a session recorded before D7."""
    if not db_path.exists():
        return []
    import sqlite3
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "SELECT role, round, lane_idx, payload FROM runtime_events "
                "WHERE kind = 'node_exit' ORDER BY ts").fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return []
    out = []
    for role, rnd, lane, payload in rows:
        try:
            body = json.loads(payload or "{}")
        except ValueError:
            continue
        if not body.get("stop_reason"):
            continue
        row = {"role": role, "round": rnd, "lane_idx": lane,
               "stop_reason": body["stop_reason"]}
        if body.get("failure_class"):
            row["failure_class"] = body["failure_class"]
        if body.get("error"):
            row["error"] = str(body["error"])[:300]
        out.append(row)
    return out


def failed_lanes(stops: Iterable[dict],
                 classes: frozenset = RETRYABLE_CLASSES) -> list[int]:
    """Lane indices whose last first-round researcher exit failed with one
    of ``classes``, in lane order. ``stops`` is :func:`lane_stops`'s list
    (finishing order)."""
    last: dict[int, Optional[str]] = {}
    for row in stops:
        if row.get("role") != "researcher" or int(row.get("round") or 1) != 1:
            continue
        lane = row.get("lane_idx")
        if lane is None:
            lane = 0
        last[int(lane)] = row.get("failure_class")
    return sorted(i for i, fc in last.items() if fc in classes)


def retry_specs(lanes: list[int], *, plan: str,
                plan_items: Optional[list[str]],
                models_per_lane: list[str]) -> list[dict]:
    """``{"lane_idx", "plan_item", "model"}`` for each lane in ``lanes``,
    rebuilt as the planner fan-out built it. Below the fan-out threshold
    the run had one lane asked the whole plan, which is what it gets
    again. A lane index outside the rebuilt fan-out is left out."""
    items = list(plan_items or council.parse_plan_items(plan or ""))
    models = [m for m in models_per_lane if m] or [""]
    if len(items) < council.FANOUT_MIN_ITEMS:
        return [{"lane_idx": 0, "plan_item": None, "model": models[0]}
                for i in lanes if i == 0]
    groups = council.group_items_into_lanes(items, council.FANOUT_MAX_LANES)
    out = []
    for i in lanes:
        group, model_idx = divmod(i, len(models))
        if 0 <= group < len(groups):
            out.append({"lane_idx": i,
                        "plan_item": council.join_lane_items(groups[group]),
                        "model": models[model_idx]})
    return out


#: Per-lane escalation (plan D): a lane a guard ended, or one that failed
#: for a reason another model may not hit. ``infra`` is not here: the
#: same model on a working server is the cheaper fix (``retry-lanes``).
ESCALATE_STOPS = frozenset({"struggle", "loop_guard", "mistake_limit",
                            "reasoning_loop"})
ESCALATE_CLASSES = frozenset({"task_error", "context_overflow",
                              "tool_call_unparsable", "model_missing"})

_WHY = {
    "struggle": "its thinking kept running out of budget, turn after turn",
    "loop_guard": "it kept repeating the same tool call",
    "mistake_limit": "too many tool calls in a row failed",
    "reasoning_loop": "its reasoning fell into a repetition loop",
    "task_error": "it failed",
    "context_overflow": "its prompt no longer fit the model's window",
    "tool_call_unparsable": "the server could not read its tool calls",
    "model_missing": "its model was not available",
}


def escalation_lanes(stops: Iterable[dict], limit: int) -> list[dict]:
    """The last first-round researcher exit of each lane that warrants an
    expert, in lane order, at most ``limit``."""
    last: dict[int, dict] = {}
    for row in stops:
        if row.get("role") != "researcher" or int(row.get("round") or 1) != 1:
            continue
        last[int(row.get("lane_idx") or 0)] = row
    out = [dict(row, lane_idx=i) for i, row in sorted(last.items())
           if row.get("stop_reason") in ESCALATE_STOPS
           or row.get("failure_class") in ESCALATE_CLASSES]
    return out[:max(0, int(limit))]


def attempt_brief(db_path: Path, stop: dict, *, model: str = "",
                  last_calls: int = 8) -> str:
    """What the failed attempt did, for the lane that takes it over: why it
    ended and its last tool calls, from this session's ``tool_call`` rows.
    Never its transcript."""
    import sqlite3
    lane = stop.get("lane_idx")
    calls: list[tuple] = []
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            calls = con.execute(
                "SELECT tool, args, output, error FROM events "
                "WHERE kind = 'tool_call' AND role = 'researcher' "
                "AND round = 1 AND COALESCE(lane_idx, 0) = ? ORDER BY ts",
                (int(lane or 0),)).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        calls = []
    reason = stop.get("failure_class") or stop.get("stop_reason") or ""
    why = _WHY.get(reason, f"it ended on {reason}")
    if stop.get("error") and stop.get("failure_class"):
        why += f" ({str(stop['error'])[:160]})"
    who = f" on {model}" if model else ""
    lines = [f"An earlier attempt at this item{who} ended without a usable "
             f"report: {why}. It made {len(calls)} tool call"
             f"{'' if len(calls) == 1 else 's'}."]
    if calls:
        lines.append(f"Its last {min(len(calls), last_calls)}:")
        from claude_hooks.loop_detection import is_failed_output
        for tool, args, output, error in calls[-last_calls:]:
            failed = bool(error) or is_failed_output(str(output or ""))
            head = " ".join(str(error or output or "").split())[:100]
            lines.append(f"- {tool} {str(args or '')[:120]} -> "
                         f"{'FAILED: ' if failed else ''}{head}")
    lines.append("Use what it found, do not repeat the calls that failed, "
                 "and write the report it could not.")
    return "\n".join(lines)


def surviving_reports(research: Iterable[str]) -> list[str]:
    """The parent's reports with its tombstones left out: the retried
    lanes write their own."""
    return [r for r in research or []
            if isinstance(r, str) and r.strip()
            and not r.lstrip().startswith(_FAILED_PREFIXES)]


__all__ = ["RETRYABLE_CLASSES", "ESCALATE_STOPS", "ESCALATE_CLASSES",
           "attempt_brief", "escalation_lanes", "failed_lanes", "lane_stops",
           "retry_specs", "surviving_reports"]
