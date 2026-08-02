"""Cooperative cancel + pause for a running council.

Before this module ``POST /cancel`` and ``POST /interrupt`` set a flag
that nothing read (audit 2026-08-02). Wiring a node to read the flag off
``runtime_control`` would not have fixed it either, and understanding
why decides the whole design:

**A mid-invoke graph does not re-read its own state.** The control routes
apply their delta with ``compiled.update_state(...)``, which writes a
checkpoint. A graph that is *already inside* ``invoke``/``stream`` carries
its channel values through the superstep in memory; the write lands in
the checkpointer and the running invocation never looks. So the flag was
unreadable, not merely unread — a node consulting ``state["runtime_control"]
["cancel_requested"]`` would have seen ``False`` for the entire run.

This module is therefore an **out-of-band** channel: a plain threading
object on ``SessionState`` that the node gate reads directly, bypassing
LangGraph entirely. That is not a workaround — it is the same shape as
the two cross-thread controls in this codebase that *do* work, the
adversary ack and the tool-approval broker. All three are answered by an
HTTP thread and observed by a runner worker thread, and none of them
travels through graph state.

**Cancel skips, it does not raise.** Every remaining node becomes a
no-op and the graph drains to END in milliseconds, keeping whatever
partial state exists. Raising would abort the stream mid-superstep and
lose the partial result that ``--keep-partial`` exists to preserve.
Note what this means: a cancelled run has **no synthesized answer**,
because the synthesizer is a node like any other and running it would be
spending after the user said stop. "Stop and synthesize what you have"
is a different verb; it is not this one.

**Pause blocks the node, not the graph.** Same reason per-lane parking
works for tool approval: LangGraph runs sync nodes on its own worker
threads, so a paused node holds one thread while its x-tier siblings
keep going. A graph-level ``interrupt()`` would need a durable
checkpoint plus a re-invoke, and re-invoking while the runner still owns
the stream is the double-resume bug ``/resume`` already guards against
for the adversary checkpoint.

**Pause resumes on timeout; cancel denies on timeout.** They are
opposites on purpose. An unanswered spend approval must not authorize
spend, so it denies. An unanswered pause has already spent everything up
to that point, and abandoning the run would throw that away — so the
safe default is to carry on.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Optional

log = logging.getLogger("consultants.engine.run_control")

#: How long a paused node waits before giving up on the pause and
#: continuing. Deliberately generous: the run is already paid for, and
#: the cost of resuming a pause nobody released is one council; the cost
#: of abandoning it is the same council plus everything spent so far.
DEFAULT_PAUSE_TIMEOUT_S = 1800.0

#: Wake cadence for the pause wait. Only bounds how fast a release is
#: noticed when the Event is missed; the Event itself wakes instantly.
_POLL_INTERVAL_S = 0.5


class RunControl:
    """Cancel / pause state for one consultation, shared across threads.

    Lives on ``SessionState``. HTTP handlers call the ``request_*`` /
    ``release_*`` methods; the node gate calls :meth:`check`.
    """

    def __init__(self, sid: str = "",
                 pause_timeout_s: float = DEFAULT_PAUSE_TIMEOUT_S):
        self.sid = sid
        self.pause_timeout_s = float(pause_timeout_s)
        self._lock = threading.RLock()
        self._cancelled = False
        self._cancel_reason = ""
        self._cancelled_at: Optional[float] = None
        self._paused = False
        self._pause_reason = ""
        self._paused_at: Optional[float] = None
        # Set == "not paused". Starting set means an un-paused council
        # never touches the wait path at all.
        self._resume_evt = threading.Event()
        self._resume_evt.set()
        #: Roles observed skipping / parking, for the status payload and
        #: for tests that need to prove the gate actually fired.
        self.skipped: list[str] = []
        self.paused_roles: list[str] = []

    # ------------------------------------------------------------------ #
    # Requests (HTTP thread)
    # ------------------------------------------------------------------ #
    def request_cancel(self, reason: str = "user-cancel",
                       now: Optional[float] = None) -> bool:
        """Ask the run to stop. Returns True the first time only.

        Also releases any pause: a cancel arriving while a node is
        parked must not queue behind the pause it is trying to end.
        """
        with self._lock:
            first = not self._cancelled
            self._cancelled = True
            if first:
                self._cancel_reason = reason or "user-cancel"
                self._cancelled_at = time.time() if now is None else now
            self._paused = False
            self._resume_evt.set()
        if first:
            log.warning("run %s: cancel requested (%s)", self.sid, reason)
        return first

    def request_pause(self, reason: str = "user-pause",
                      now: Optional[float] = None) -> bool:
        """Ask the next node to park. Returns False if already cancelled
        — pausing a run that is stopping is meaningless, and honouring
        it would park a node that should be draining."""
        with self._lock:
            if self._cancelled:
                return False
            self._paused = True
            self._pause_reason = reason or "user-pause"
            self._paused_at = time.time() if now is None else now
            self._resume_evt.clear()
        log.info("run %s: pause requested (%s)", self.sid, reason)
        return True

    def release_pause(self, by: str = "user") -> bool:
        """Let parked nodes continue. Returns True if one was paused."""
        with self._lock:
            was = self._paused
            self._paused = False
            self._pause_reason = ""
            self._resume_evt.set()
        if was:
            log.info("run %s: pause released by %s", self.sid, by)
        return was

    # ------------------------------------------------------------------ #
    # Observation (runner / node threads)
    # ------------------------------------------------------------------ #
    @property
    def cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    @property
    def paused(self) -> bool:
        with self._lock:
            return self._paused

    @property
    def cancel_reason(self) -> str:
        with self._lock:
            return self._cancel_reason

    def snapshot(self) -> dict:
        """The shape ``GET /state`` merges in. Empty when nothing has
        been requested, so a default run's status stays byte-identical."""
        with self._lock:
            out: dict = {}
            if self._cancelled:
                out["cancel_requested"] = True
                out["cancel_reason"] = self._cancel_reason
                if self._cancelled_at is not None:
                    out["cancelled_at"] = self._cancelled_at
                if self.skipped:
                    out["skipped_roles"] = list(self.skipped)
            if self._paused:
                out["paused"] = True
                out["pause_reason"] = self._pause_reason
                # ``paused`` alone conflates two very different
                # situations, and the difference is what a caller
                # actually wants to know: has anything stopped yet?
                # ``pending`` means the request is registered and the
                # next node to enter will take it; ``parked`` means a
                # node is blocked right now. A run can sit in
                # ``pending`` for half an hour when the runner is inside
                # the adversary checkpoint, and reading that as "paused"
                # is how a pause looks like it did nothing.
                out["pause_state"] = "parked" if self.paused_roles else "pending"
                if self._paused_at is not None:
                    out["paused_at"] = self._paused_at
                    out["pause_deadline_ts"] = (
                        self._paused_at + self.pause_timeout_s)
                if self.paused_roles:
                    out["paused_roles"] = list(self.paused_roles)
            return out

    # ------------------------------------------------------------------ #
    def check(self, role: str = "", *,
              emit: Optional[Callable[[str, dict], None]] = None,
              is_closed: Optional[Callable[[], bool]] = None,
              now_fn: Callable[[], float] = time.time,
              wait_fn: Optional[Callable[[float], bool]] = None,
              ) -> str:
        """Gate one node. Returns ``"run"`` or ``"cancel"``.

        Blocks here while the run is paused, which parks exactly this
        node's worker thread and nothing else.
        """
        if self.cancelled:
            self._note_skipped(role)
            return "cancel"
        if not self.paused:
            return "run"

        # Bound from when the pause was REQUESTED, not from when this
        # node happened to reach the gate. A node can enter minutes
        # after the request (live smoke 2026-08-02: the pause landed at
        # 10:43, the synthesizer parked at 10:49 because the adversary
        # checkpoint held the runner in between). Measuring from park
        # time would let a node keep waiting past the
        # ``pause_deadline_ts`` that ``status`` is already advertising —
        # a deadline that has visibly passed while the thing it bounds
        # is still blocked.
        with self._lock:
            started = self._paused_at
            if role and role not in self.paused_roles:
                self.paused_roles.append(role)
            reason = self._pause_reason
        deadline = (started + self.pause_timeout_s
                    if started is not None
                    else now_fn() + self.pause_timeout_s)
        if emit is not None:
            _safe_emit(emit, "awaiting_resume", {
                "role": role, "reason": reason,
                "deadline_ts": deadline,
                "sid": self.sid,
            })
        log.warning(
            "run %s: node %s parked on pause (%s) — deadline %.0f",
            self.sid, role or "?", reason, deadline,
        )

        waiter = wait_fn or self._resume_evt.wait
        while now_fn() < deadline:
            if self.cancelled:
                break
            if is_closed is not None and is_closed():
                break
            remaining = deadline - now_fn()
            if remaining <= 0:
                break
            if waiter(min(_POLL_INTERVAL_S, remaining)):
                if not self.paused:
                    break

        with self._lock:
            if role in self.paused_roles:
                self.paused_roles.remove(role)
            timed_out = self._paused and now_fn() >= deadline
            if timed_out:
                # Resume rather than abandon: everything up to here is
                # already paid for.
                self._paused = False
                self._resume_evt.set()

        if timed_out:
            log.warning(
                "run %s: pause timed out after %ss — RESUMING (the run "
                "is already paid for; abandoning it would waste that)",
                self.sid, self.pause_timeout_s,
            )
        if emit is not None:
            _safe_emit(emit, "resumed", {
                "role": role, "sid": self.sid,
                "timed_out": bool(timed_out),
            })
        if self.cancelled:
            self._note_skipped(role)
            return "cancel"
        return "run"

    def _note_skipped(self, role: str) -> None:
        with self._lock:
            if role and role not in self.skipped:
                self.skipped.append(role)


def _safe_emit(emit: Callable[[str, dict], None], kind: str,
               payload: dict) -> None:
    try:
        emit(kind, payload)
    except Exception:  # pragma: no cover — telemetry must not break a run
        log.exception("run_control emit(%s) raised", kind)


# ---------------------------------------------------------------------- #
# The node gate
# ---------------------------------------------------------------------- #

def gate_node(fn: Callable, *, role: str,
              run_control: Optional[RunControl],
              emit: Optional[Callable[[str, dict], None]] = None,
              is_closed: Optional[Callable[[], bool]] = None,
              skip_delta: Optional[Callable[[str, Any], dict]] = None,
              ) -> Callable:
    """Wrap a council node with the cancel / pause gate.

    Applied in ``build_council_graph``'s single ``_wrap`` choke point, so
    all eight node bodies get it without being touched — and so a node
    added later cannot forget it.

    ``run_control=None`` returns ``fn`` unchanged, which is what keeps
    every existing unit test and every caller that builds a graph
    without a session (benchmarks, the parity cohorts) byte-identical.
    """
    if run_control is None:
        return fn

    def _gated(state, *a, **kw):
        verdict = run_control.check(
            role, emit=emit, is_closed=is_closed,
        )
        if verdict == "cancel":
            log.info("node %s skipped: run cancelled", role)
            if emit is not None:
                _safe_emit(emit, "node_cancelled", {
                    "role": role, "sid": run_control.sid,
                    "reason": run_control.cancel_reason,
                })
            if skip_delta is not None:
                return skip_delta(role, state)
            # An empty delta is a valid partial update for every
            # channel: reducers see nothing, the node contributes
            # nothing, and the graph moves on to the next node — which
            # is also cancelled, so the whole remainder drains fast.
            return {}
        return fn(state, *a, **kw)

    _gated.__name__ = getattr(fn, "__name__", f"gated_{role}")
    _gated.__doc__ = getattr(fn, "__doc__", None)
    return _gated


__all__ = [
    "DEFAULT_PAUSE_TIMEOUT_S",
    "RunControl",
    "gate_node",
]
