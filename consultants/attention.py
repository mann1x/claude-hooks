"""What a run is waiting on its caller for.

One reading of a run's status record, shared by the CLI waiter
(``claude-consultants wait`` / ``--wait``, consultants/cli.py) and the
engine's push to the calling session (consultants/server/attention_push.py),
so the two can never disagree about whether a run needs an answer.

The attention keys appear in ``SessionState.public_dict()``
(consultants/server/app.py) only while something waits, so their presence
is the signal.
"""
from __future__ import annotations

from typing import Optional

#: A pause still ``pending`` after this long is held by something; say so.
PAUSE_PENDING_GRACE_S = 60.0

#: Statuses a run is still going in; anything else is its end.
RUN_STILL_GOING = ("running", "suspended")


def attention_of(rec: dict, *, pending_for: float = 0.0) -> Optional[dict]:
    """What the run is waiting on the caller for, or None. Each answer
    names its deadline, what happens on it, and the verb that answers."""
    sid = rec.get("sid") or "<sid>"
    if rec.get("adversary_checkpoint_deadline_ts"):
        return {"reason": "adversary_checkpoint",
                "deadline_ts": rec["adversary_checkpoint_deadline_ts"],
                "on_timeout": "auto-resumes",
                "answer": f"claude-consultants adversary-ack {sid}"}
    pending = rec.get("pending_tool_approvals") or []
    if pending:
        deadlines = [p.get("deadline_ts") for p in pending
                     if isinstance(p, dict) and p.get("deadline_ts")]
        return {"reason": "tool_approval",
                "deadline_ts": min(deadlines) if deadlines else None,
                "on_timeout": "denied", "pending": len(pending),
                "answer": f"claude-consultants tool-ack {sid} --allow|--deny"}
    state = rec.get("pause_state")
    if state == "parked":
        return {"reason": "paused", "deadline_ts": rec.get("pause_deadline_ts"),
                "on_timeout": "resumes", "roles": rec.get("paused_roles"),
                "answer": f"claude-consultants resume {sid}"}
    if state == "pending" and pending_for >= PAUSE_PENDING_GRACE_S:
        return {"reason": "pause_not_taking_effect",
                "blocked_by": rec.get("pause_blocked_by"),
                "deadline_ts": rec.get("pause_deadline_ts"),
                "on_timeout": "resumes",
                "answer": f"claude-consultants status {sid}"}
    return None
