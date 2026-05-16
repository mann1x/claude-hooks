"""Stall detection + retry orchestrator for researcher / coder /
tool_executor LLM calls.

The 2026-05-15 audit session pathology — two gemini-3-flash lanes
that held the TCP connection open for 31 min / 27 min while
producing zero tokens the synthesizer ended up using — motivates a
*detection*-first strategy. A blanket hard timeout would clip
legitimate deep thinking on hard questions (we don't yet have data
on GPQA-Diamond-level workloads). The right primitive is **stall
detection keyed on token-emission cadence + retry**, with a wide
conservative absolute ceiling that only fires when the detector
doesn't.

Design (per plan §M3):

- ``classify_stall(...)`` — pure decision function. Given the
  per-call clock state (started_ts, last_token_ts, tokens_emitted)
  + thresholds + now_ts, return one of:
    ``PROGRESSING`` | ``STARTUP_STALL`` | ``MID_STREAM_STALL`` |
    ``HARD_CAP_EXCEEDED``.
  Testable without threading or wall clock.

- ``StallController`` — data + cooperation primitive passed into
  the streaming chat callable. The chat fn calls
  ``controller.mark_token()`` on every received token and
  ``controller.is_cancelled()`` periodically (e.g. between SSE
  chunks). Thread-safe.

- ``StallMonitor`` — orchestrator. Wraps a ``chat_streamed_fn`` in
  a worker thread + a watchdog timer; on stall, sets the cancel
  flag, joins the worker, sleeps a small backoff, then retries up
  to ``retries`` times with a fresh controller. On hard-cap or
  exhausted retries, raises a typed exception so the researcher
  node can tombstone the lane cleanly.

The module is pure-Python (threading + stdlib only); no LangGraph
or async dependency. The researcher node calls
``StallMonitor(...).run()`` from inside ``asyncio.to_thread`` (the
existing pattern for the synchronous agent-loop runner) so async
nodes stay non-blocking.
"""

from __future__ import annotations

import enum
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

log = logging.getLogger("consultants.engine.stall")


# ---------- decision function ------------------------------------ #

class StallOutcome(str, enum.Enum):
    """Result of one ``classify_stall`` check.

    String-valued so log lines and event payloads are
    human-readable without an extra map.
    """

    PROGRESSING = "progressing"           # keep waiting
    STARTUP_STALL = "startup_stall"       # no first token yet
    MID_STREAM_STALL = "mid_stream_stall" # stream went silent
    HARD_CAP_EXCEEDED = "hard_cap"        # absolute ceiling


def classify_stall(
    *,
    started_ts: float,
    last_token_ts: Optional[float],
    tokens_emitted: int,
    now_ts: float,
    stall_threshold_s: float,
    hard_cap_s: float,
) -> StallOutcome:
    """Decide whether a call is progressing, stalled, or past the
    hard cap.

    Precedence (checked top-down):

    1. ``now - started_ts > hard_cap_s`` -> ``HARD_CAP_EXCEEDED``.
       Hard cap wins over stall detection — once we're past it,
       there's no point retrying.
    2. ``tokens_emitted == 0 and now - started_ts > stall_threshold_s``
       -> ``STARTUP_STALL``. TCP open, no first byte.
    3. ``tokens_emitted > 0 and now - last_token_ts > stall_threshold_s``
       -> ``MID_STREAM_STALL``. Stream went silent mid-flight.
    4. Otherwise ``PROGRESSING``.

    ``last_token_ts`` may be ``None`` when no token has been seen
    yet (the call is in its startup window). The function defends
    against that — pass ``None`` until the first token, then the
    monotonic timestamp.

    Pure function; no I/O, no clock access. Inject ``now_ts`` from
    the caller (real clock at runtime, fake clock in tests).
    """
    elapsed = now_ts - started_ts
    if elapsed > hard_cap_s:
        return StallOutcome.HARD_CAP_EXCEEDED
    if tokens_emitted == 0:
        if elapsed > stall_threshold_s:
            return StallOutcome.STARTUP_STALL
        return StallOutcome.PROGRESSING
    # tokens_emitted > 0: we expect last_token_ts set; defend anyway.
    last = last_token_ts if last_token_ts is not None else started_ts
    gap = now_ts - last
    if gap > stall_threshold_s:
        return StallOutcome.MID_STREAM_STALL
    return StallOutcome.PROGRESSING


# ---------- exceptions ------------------------------------------- #

class StallError(RuntimeError):
    """Base class for stall-related failures the researcher
    tombstones on. Carries enough metadata for the recorder /
    event log to attribute the failure correctly."""

    def __init__(self, message: str, *,
                 outcome: StallOutcome,
                 attempts: int,
                 tokens_emitted: int,
                 elapsed_s: float):
        super().__init__(message)
        self.outcome = outcome
        self.attempts = attempts
        self.tokens_emitted = tokens_emitted
        self.elapsed_s = elapsed_s


class StallRetryExhausted(StallError):
    """All retry attempts ended in a stall. The lane should be
    tombstoned. ``attempts`` is the total number of tries (initial
    + retries)."""


class HardCapExceeded(StallError):
    """One attempt ran past the per-lane hard cap. We never retry
    past the cap — by definition the call has consumed more wall
    time than the policy allows."""


# ---------- cooperation primitive -------------------------------- #

@dataclass
class StallProgress:
    """Snapshot of one in-flight call's progress. Read-only; the
    controller publishes copies of this to the watchdog so the
    decision function gets a consistent view without locking."""

    started_ts: float
    last_token_ts: Optional[float]
    tokens_emitted: int


class StallController:
    """Lightweight cooperative-cancellation primitive passed to the
    streaming chat callable.

    Thread-safe. The chat fn:

    - calls ``mark_token()`` each time a token arrives (the SSE
      delta loop is the obvious place);
    - polls ``is_cancelled()`` between chunks and bails out if
      ``True`` (with a partial result discarded — the orchestrator
      retries).

    Cancellation is cooperative: the controller does not interrupt
    the worker thread. If the chat fn ignores the flag, the
    orchestrator joins with a deadline and gives up on the worker
    (it will eventually exit when the underlying socket closes).
    """

    def __init__(self,
                 *,
                 started_ts: float,
                 time_source: Callable[[], float] = time.monotonic):
        self._lock = threading.Lock()
        self._started_ts = float(started_ts)
        self._last_token_ts: Optional[float] = None
        self._tokens_emitted: int = 0
        self._cancelled = threading.Event()
        self._time_source = time_source

    @property
    def started_ts(self) -> float:
        return self._started_ts

    def mark_token(self) -> None:
        """Record that one token (or chunk) was received. Updates
        ``last_token_ts`` to ``time_source()``.
        """
        now = self._time_source()
        with self._lock:
            self._last_token_ts = now
            self._tokens_emitted += 1

    def progress(self) -> StallProgress:
        """Snapshot the current progress without exposing the lock."""
        with self._lock:
            return StallProgress(
                started_ts=self._started_ts,
                last_token_ts=self._last_token_ts,
                tokens_emitted=self._tokens_emitted,
            )

    def cancel(self) -> None:
        """Signal the chat fn to abort cooperatively. Idempotent."""
        self._cancelled.set()

    def is_cancelled(self) -> bool:
        return self._cancelled.is_set()


# ---------- orchestrator ----------------------------------------- #

# Chat-streaming callable signature. The fn runs synchronously,
# emits tokens via ``controller.mark_token()`` as they arrive, and
# returns the final aggregated response (same shape ``ChatClient.chat``
# returns: ``{"choices": [...], "usage": {...}}``). Honors
# ``controller.is_cancelled()`` cooperatively — if cancelled
# mid-stream, raise ``CancelledByOrchestrator`` so the orchestrator
# can distinguish cooperative abort from a real upstream error.

ChatStreamedFn = Callable[[dict, "StallController"], dict]


class CancelledByOrchestrator(RuntimeError):
    """Raised by a well-behaved chat_streamed_fn when it notices
    ``controller.is_cancelled()``. The orchestrator catches this
    and treats it as 'aborted on stall' — not a real error."""


@dataclass
class StallConfig:
    """Bundle of stall-detection thresholds.

    Default values match ``control.DEFAULT_STALL_THRESHOLD_S``
    (300 s) and ``control.PER_LANE_HARD_S_DEFAULT`` (3600 s) and
    1 retry. The researcher node passes the live values from
    ``state["runtime_control"]`` here so a mid-flight mutation
    changes the next call's behavior.
    """

    stall_threshold_s: float = 300.0
    hard_cap_s: float = 3600.0
    retries: int = 1
    check_interval_s: float = 5.0      # how often the watchdog wakes
    retry_backoff_s: float = 2.0       # sleep between attempts
    join_grace_s: float = 5.0          # how long to wait for worker
                                       # to honor cancellation


class StallMonitor:
    """Run a ``chat_streamed_fn`` with stall detection + retry.

    Usage::

        cfg = StallConfig(stall_threshold_s=300, hard_cap_s=3600)
        mon = StallMonitor(cfg)
        response = mon.run(chat_streamed_fn, payload)

    On success: returns the chat response unchanged.
    On stall + retry success: returns the retried chat response
    (the recorder sees both attempts).
    On retries exhausted: raises ``StallRetryExhausted``.
    On hard cap: raises ``HardCapExceeded``.

    Each attempt is observable via the ``attempts`` list on the
    monitor instance — the recorder reads it after ``run()``
    returns/raises to log both the stall and the eventual outcome.
    """

    def __init__(self, cfg: StallConfig,
                 *,
                 time_source: Callable[[], float] = time.monotonic,
                 sleep_fn: Callable[[float], None] = time.sleep,
                 on_event: Optional[Callable[[dict], None]] = None):
        self.cfg = cfg
        self._time = time_source
        self._sleep = sleep_fn
        self._on_event = on_event
        # Per-call audit trail; reset on each ``run()``.
        self.attempts: list[dict[str, Any]] = []

    def _emit(self, kind: str, **fields: Any) -> None:
        """Push an event to the optional sink. Best-effort; never
        raises into the orchestrator path."""
        if self._on_event is None:
            return
        try:
            self._on_event({"kind": kind, **fields})
        except Exception:  # pragma: no cover — sink failures ignored
            log.exception("stall on_event sink raised; ignored")

    def run(self, chat_streamed_fn: ChatStreamedFn,
            payload: dict) -> dict:
        """Execute the chat with stall detection + retry.

        The caller passes in ``payload`` as-is; the orchestrator
        doesn't inspect it. ``chat_streamed_fn`` is responsible for
        actual streaming + token marking.
        """
        self.attempts = []
        max_attempts = max(1, self.cfg.retries + 1)
        for attempt_idx in range(max_attempts):
            controller = StallController(
                started_ts=self._time(),
                time_source=self._time,
            )
            outcome, result, error = self._run_once(
                chat_streamed_fn, payload, controller,
            )
            entry: dict[str, Any] = {
                "attempt": attempt_idx + 1,
                "outcome": outcome.value if isinstance(outcome, StallOutcome)
                           else "ok",
                "tokens_emitted": controller.progress().tokens_emitted,
                "elapsed_s": self._time() - controller.started_ts,
                "error": (f"{type(error).__name__}: {error}"
                          if error is not None else None),
            }
            self.attempts.append(entry)

            if outcome == "ok":
                self._emit(
                    "stall.attempt.ok",
                    attempt=attempt_idx + 1,
                    tokens=entry["tokens_emitted"],
                    elapsed_s=entry["elapsed_s"],
                )
                return result   # type: ignore[return-value]

            if outcome == StallOutcome.HARD_CAP_EXCEEDED:
                self._emit(
                    "stall.attempt.hard_cap",
                    attempt=attempt_idx + 1,
                    tokens=entry["tokens_emitted"],
                    elapsed_s=entry["elapsed_s"],
                )
                # Hard cap means we already exceeded the absolute
                # ceiling. No retry — by definition this lane has
                # consumed more than the policy allows.
                raise HardCapExceeded(
                    f"per-lane hard cap of {self.cfg.hard_cap_s}s "
                    f"exceeded on attempt {attempt_idx + 1}: "
                    f"{entry['elapsed_s']:.1f}s elapsed, "
                    f"{entry['tokens_emitted']} tokens",
                    outcome=outcome,
                    attempts=attempt_idx + 1,
                    tokens_emitted=entry["tokens_emitted"],
                    elapsed_s=entry["elapsed_s"],
                )

            # Stall (startup or mid-stream). Either retry or give up.
            self._emit(
                "stall.attempt.stalled",
                attempt=attempt_idx + 1,
                outcome=outcome.value,
                tokens=entry["tokens_emitted"],
                elapsed_s=entry["elapsed_s"],
            )
            if attempt_idx + 1 < max_attempts:
                self._sleep(self.cfg.retry_backoff_s)
                continue
            # No more retries.
            total_tokens = sum(a["tokens_emitted"] for a in self.attempts)
            raise StallRetryExhausted(
                f"stall retries exhausted after {max_attempts} attempt(s); "
                f"last outcome={outcome.value}",
                outcome=outcome,
                attempts=max_attempts,
                tokens_emitted=total_tokens,
                elapsed_s=entry["elapsed_s"],
            )
        # Unreachable — max_attempts >= 1 and the loop body either
        # returns or raises.
        raise RuntimeError("StallMonitor.run: no attempts executed")

    def _run_once(self,
                  chat_streamed_fn: ChatStreamedFn,
                  payload: dict,
                  controller: StallController,
                  ) -> tuple[Any, Optional[dict], Optional[BaseException]]:
        """Run one attempt with the watchdog. Returns
        ``("ok", result, None)`` on success,
        ``(StallOutcome, None, error_or_None)`` on stall/hard-cap.

        Errors from the chat fn that aren't cooperative
        cancellation are re-raised — the stall layer only owns the
        stall/cap decisions, not "the upstream returned 500".
        """
        result_box: dict[str, Any] = {"result": None}
        error_box: dict[str, Optional[BaseException]] = {"error": None}

        def worker():
            try:
                result_box["result"] = chat_streamed_fn(
                    payload, controller,
                )
            except CancelledByOrchestrator:
                # Expected when the orchestrator cancelled mid-call.
                pass
            except BaseException as e:  # noqa: BLE001
                error_box["error"] = e

        t = threading.Thread(target=worker, daemon=True,
                             name="StallMonitor.worker")
        t.start()
        # Watchdog loop.
        outcome: Any = "ok"
        while t.is_alive():
            t.join(timeout=self.cfg.check_interval_s)
            if not t.is_alive():
                break
            now = self._time()
            progress = controller.progress()
            decision = classify_stall(
                started_ts=progress.started_ts,
                last_token_ts=progress.last_token_ts,
                tokens_emitted=progress.tokens_emitted,
                now_ts=now,
                stall_threshold_s=self.cfg.stall_threshold_s,
                hard_cap_s=self.cfg.hard_cap_s,
            )
            if decision != StallOutcome.PROGRESSING:
                outcome = decision
                controller.cancel()
                # Give the worker a brief grace to exit cooperatively.
                t.join(timeout=self.cfg.join_grace_s)
                # If the worker is STILL alive, it's ignoring the
                # cancel flag. We leak the thread (daemon=True so it
                # dies with the process); the orchestrator just
                # treats it as cancelled and moves on.
                break

        # If the worker finished and recorded a real error (not
        # CancelledByOrchestrator), re-raise it. Real errors take
        # precedence over a 'PROGRESSING' classification — even if
        # the call finished within the budget, an upstream failure
        # is the failure mode the caller cares about.
        if error_box["error"] is not None and outcome == "ok":
            raise error_box["error"]

        if outcome == "ok":
            return ("ok", result_box["result"], None)
        return (outcome, None, error_box["error"])


# ---------- thin sync wrapper for non-streaming clients ---------- #

def chat_with_stall_protection(
    chat_streamed_fn: ChatStreamedFn,
    payload: dict,
    *,
    stall_threshold_s: float,
    hard_cap_s: float,
    retries: int = 1,
    check_interval_s: float = 5.0,
    retry_backoff_s: float = 2.0,
    on_event: Optional[Callable[[dict], None]] = None,
) -> dict:
    """Convenience wrapper: build a StallMonitor with the supplied
    thresholds, run a single payload, return the response.

    The researcher node typically reads its thresholds from
    ``runtime_control`` and calls this directly. The monitor's
    ``attempts`` audit trail is dropped — the recorder takes the
    same data via the ``on_event`` sink. When the recorder isn't
    wired, attempts are still classified correctly but the audit
    trail is best-effort.
    """
    cfg = StallConfig(
        stall_threshold_s=stall_threshold_s,
        hard_cap_s=hard_cap_s,
        retries=retries,
        check_interval_s=check_interval_s,
        retry_backoff_s=retry_backoff_s,
    )
    mon = StallMonitor(cfg, on_event=on_event)
    return mon.run(chat_streamed_fn, payload)
