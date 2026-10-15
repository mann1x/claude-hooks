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


def surviving_reports(research: Iterable[str]) -> list[str]:
    """The parent's reports with its tombstones left out: the retried
    lanes write their own."""
    return [r for r in research or []
            if isinstance(r, str) and r.strip()
            and not r.lstrip().startswith(_FAILED_PREFIXES)]


__all__ = ["RETRYABLE_CLASSES", "failed_lanes", "lane_stops", "retry_specs",
           "surviving_reports"]
