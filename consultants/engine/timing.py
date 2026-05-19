"""Prompt-injection helpers that surface the time budget to the
council nodes.

The plan §"Soft-deadline injection into planner" calls for two
prompt blocks:

- planner sees a fixed "SOFT TIME TARGET" header derived from
  ``RuntimeControl.soft_target_ts`` + ``deadline_ts``;
- researcher sees a live "REMAINING TIME BUDGET" computed at node
  entry from ``deadline_ts - now``.

Both are advisory — the model is free to ignore them — but their
presence is the only way the planner can tune *number and depth*
of plan items to the budget, and the only way the researcher can
make the "should I go one more round or wrap up?" decision without
us hard-cutting it ourselves.

The functions in this module are pure. They take a state dict +
optional ``now_ts`` override (so tests don't depend on wall clock)
and return either a Markdown string ready to splice into the
system prompt, or ``""`` when there is no time budget to surface
(legacy v1 path with no RuntimeControl wired through). Returning
empty string vs ``None`` keeps the call sites cheap — they can
unconditionally splice the result into the prompt.

These helpers do NOT compute timing budgets — that lives in
``consultants/engine/control.py``. Here we only render budgets that
are already on the state.
"""

from __future__ import annotations

import time
from typing import Optional


def _format_minutes(seconds: float) -> str:
    """Format a positive duration as a compact human string for the
    prompt: ``45 s``, ``3 min``, ``12 min``, ``1 h 5 min``.

    The planner LLM reads this and decides plan depth — we lean
    toward 'min' for anything ≥ 60 s so a 180-s budget reads as
    "3 min", not "180 s" (which models occasionally misparse as
    "180 seconds = 3 hours" on bad days).
    """
    if seconds < 60.0:
        # Round to nearest 5s under a minute — sub-second precision
        # is noise the model can't act on. Exact zero renders as
        # "0 s" (truthful when the budget is exhausted); anything
        # strictly positive clamps to "5 s" so we don't surface
        # "1 s" advice that's meaningless for an LLM call.
        if seconds <= 0:
            return "0 s"
        rounded = int(round(seconds / 5.0)) * 5
        if rounded < 5:
            rounded = 5
        return f"{rounded} s"
    minutes = seconds / 60.0
    if minutes < 60.0:
        # Round to whole minutes for budgets under an hour.
        return f"{int(round(minutes))} min"
    hours = int(minutes // 60)
    rem_min = int(round(minutes - hours * 60))
    if rem_min == 0:
        return f"{hours} h"
    return f"{hours} h {rem_min} min"


def planner_soft_target_block(state: dict,
                              *,
                              now_ts: Optional[float] = None) -> str:
    """Render the planner's SOFT TIME TARGET block.

    The block is composed at planner-node entry — once per
    consultation. Returns ``""`` when ``runtime_control`` is not on
    the state at all (legacy v1 path), or when both ``soft_target_ts``
    and ``deadline_ts`` are absent.

    Shape (rendered into the planner system prompt):

        SOFT TIME TARGET: aim to finish in **3 min**. The hard cap is
        9 min; past that the consultation is cancelled and the
        synthesizer composes from whatever evidence is ready. Plan
        the number and depth of steps with this budget in mind —
        shorter on simple questions, deeper on hard ones.
    """
    rc = state.get("runtime_control") or {}
    soft_ts = rc.get("soft_target_ts")
    hard_ts = rc.get("deadline_ts")
    if not soft_ts and not hard_ts:
        return ""

    if now_ts is None:
        now_ts = time.time()

    parts: list[str] = []
    if soft_ts:
        soft_remaining = max(0.0, float(soft_ts) - float(now_ts))
        parts.append(
            f"SOFT TIME TARGET: aim to finish in "
            f"**{_format_minutes(soft_remaining)}**."
        )
    if hard_ts:
        hard_remaining = max(0.0, float(hard_ts) - float(now_ts))
        parts.append(
            f"The hard cap is {_format_minutes(hard_remaining)}; past "
            f"that the consultation is cancelled and the synthesizer "
            f"composes from whatever evidence is ready."
        )
    parts.append(
        "Plan the number and depth of steps with this budget in mind "
        "— shorter on simple questions, deeper on hard ones."
    )
    return " ".join(parts)


def researcher_remaining_block(state: dict,
                               *,
                               now_ts: Optional[float] = None) -> str:
    """Render the researcher's REMAINING TIME BUDGET block.

    Computed at researcher-node entry so each round sees the live
    value — what was 15 min at planner time becomes 7 min by
    researcher round 2. Returns ``""`` when no deadline is set.

    Shape:

        REMAINING TIME BUDGET: 7 min until the hard cap. Soft target
        ends in 2 min. If you're close to the soft target, prefer
        wrapping up over starting a new tool subloop.

    The researcher LLM consults this when deciding whether to go
    one more iteration or finalize its report.
    """
    rc = state.get("runtime_control") or {}
    soft_ts = rc.get("soft_target_ts")
    hard_ts = rc.get("deadline_ts")
    if not soft_ts and not hard_ts:
        return ""

    if now_ts is None:
        now_ts = time.time()

    parts: list[str] = []
    if hard_ts:
        hard_remaining = max(0.0, float(hard_ts) - float(now_ts))
        parts.append(
            f"REMAINING TIME BUDGET: {_format_minutes(hard_remaining)} "
            f"until the hard cap."
        )
    if soft_ts:
        soft_remaining = float(soft_ts) - float(now_ts)
        if soft_remaining > 0:
            parts.append(
                f"Soft target ends in {_format_minutes(soft_remaining)}."
            )
        else:
            parts.append(
                "Soft target has passed; the synthesizer is waiting."
            )
    parts.append(
        "If you're close to the soft target, prefer wrapping up "
        "over starting a new tool subloop."
    )
    return " ".join(parts)


def time_pressure_signal(state: dict,
                         *,
                         now_ts: Optional[float] = None) -> str:
    """Classify how much budget remains relative to the soft target.

    Returns one of:

    - ``"ample"``    — more than 50% of the soft window left
    - ``"normal"``   — 20-50% left
    - ``"tight"``    — 5-20% left
    - ``"critical"`` — less than 5% left OR past soft target
    - ``"none"``     — no runtime_control / no soft_target_ts

    The escalation decider (M7 xauto) and the critic-strictness
    chooser both consume this. Keeping it categorical rather than
    raw-seconds makes the consuming logic side-effect-free and easy
    to test.
    """
    rc = state.get("runtime_control") or {}
    soft_ts = rc.get("soft_target_ts")
    if not soft_ts:
        return "none"

    if now_ts is None:
        now_ts = time.time()

    # We don't know the original budget unless we anchor against
    # something — use deadline_ts as the absolute ceiling and
    # estimate the soft window as (deadline_ts - soft_ts) of head.
    # When deadline_ts is missing, fall back to remaining/soft = ratio.
    remaining = float(soft_ts) - float(now_ts)
    hard_ts = rc.get("deadline_ts")
    if hard_ts and hard_ts > soft_ts:
        # The original soft window is approximated as hard/3 (the
        # default HARD_MULTIPLIER) — the consumer only cares about
        # ordering, so the exact denominator doesn't change the bin.
        original_soft = (float(hard_ts) - float(now_ts) + remaining) / 3.0
        if original_soft <= 0:
            return "critical"
        ratio = remaining / original_soft
    else:
        # No hard cap on state — degrade gracefully.
        if remaining <= 0:
            return "critical"
        # Assume a generous original window so we don't false-positive.
        ratio = 1.0 if remaining > 0 else 0.0

    if ratio <= 0.05:
        return "critical"
    if ratio <= 0.20:
        return "tight"
    if ratio <= 0.50:
        return "normal"
    return "ample"
