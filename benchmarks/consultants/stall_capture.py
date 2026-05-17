"""Timing-capture shim for the M11a stall skill-eval bench.

Wraps any object that exposes ``chat`` (and optionally
``chat_streamed``) — typically a ``ChatClient`` from
``claude_hooks.get_advice.chat_client``, but also test stubs — and
records per-token monotonic timestamps on every streaming call.
The bench then reads ``capture.calls`` to compute per-trial
percentiles (time-to-first-token, inter-token gaps, total wall).

Two design points:

1. **Transparent**. The wrapper preserves the underlying client's
   exception surface — ``CancelledByOrchestrator``, upstream
   ``HTTPError``, etc. all propagate unchanged. The agent loop's
   retry logic sees the same client behavior whether or not the
   wrapper is interposed.

2. **Recording-on-failure**. The ``CallTiming`` row is appended in
   a ``finally`` block, so trials that raised mid-stream still
   contribute the partial-data tokens-emitted-so-far to the
   aggregate. ``CallTiming.error`` carries the exception preview;
   ``CallTiming.cancelled`` distinguishes cooperative cancel from a
   real failure (the orchestrator's retry-on-cancel pattern is
   normal, not an error).

The shim is used in two places by ``stall_bench.py``:

- **Tier 1 (standalone)**: wrap the real ``ChatClient`` for the
  model under test; call ``chat_streamed`` directly with the
  bench question; one ``CallTiming`` per trial.

- **Tier 2 (fake-consultancy)**: wrap the ``ChatClient`` once per
  council role (planner / researcher / critic / synthesizer) but
  with all wrappers pinned to the SAME model. The council fires
  its normal flow; the bench aggregates ``CallTiming`` lists
  across all role-wrappers into one per-trial percentile.

Pure-Python; no numpy dependency. Percentile uses
``statistics.quantiles`` (stdlib) with inclusive method for
small-sample friendliness.
"""

from __future__ import annotations

import logging
import statistics
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Optional

log = logging.getLogger("benchmarks.consultants.stall_capture")


# ====================================================================== #
# CallTiming — one captured chat_streamed (or chat) invocation
# ====================================================================== #

@dataclass(frozen=True)
class CallTiming:
    """One captured chat call's timing data.

    All timestamps are ``time.monotonic`` (seconds, monotonic
    clock, NOT wall-clock — use only for deltas, never display).

    ``time_to_first_token_ms`` is ``None`` when no tokens arrived
    (empty stream, immediate error, or non-streamed ``chat()``
    call where we can't see the first byte).

    ``inter_token_gaps_ms`` has ``len(tokens) - 1`` elements: the
    gap between consecutive tokens. Empty list when fewer than 2
    tokens arrived.

    ``cancelled`` is True when the inner call raised
    ``CancelledByOrchestrator`` — the StallMonitor's cooperative
    abort. Distinguishes "stall-watchdog killed it" from a real
    upstream failure. ``error`` still carries the exception class
    name for both cases so the renderer can tell them apart.
    """

    model: str
    started_ts: float
    ended_ts: float
    time_to_first_token_ms: Optional[float]
    inter_token_gaps_ms: list[float]
    total_tokens: int
    error: Optional[str] = None
    cancelled: bool = False

    def total_wall_ms(self) -> float:
        """Wall-clock duration in milliseconds (ended - started)."""
        return (self.ended_ts - self.started_ts) * 1000.0

    def p_inter_token_ms(self, p: float) -> Optional[float]:
        """Return the ``p``-th percentile inter-token gap in ms, or
        ``None`` when fewer than 2 tokens arrived.

        ``p`` is in [0, 100]. Uses ``statistics.quantiles`` with
        ``method='inclusive'`` — friendliest for small samples (the
        first/last sample anchor the 0/100 ends).
        """
        return _percentile(self.inter_token_gaps_ms, p)

    def to_dict(self) -> dict:
        """JSON-clean dict for the trial's ``call_timings`` field."""
        return asdict(self)


# ====================================================================== #
# TimingCaptureChat — the wrapper
# ====================================================================== #

class TimingCaptureChat:
    """Wraps a chat client and records per-call timing.

    Duck-typed against ``ChatClient`` — provides ``chat`` and
    ``chat_streamed`` plus the inference-timer pass-throughs the
    bench reads (``total_inference_s``, ``reset_inference_timer``).

    The wrapper is single-thread-safe; one wrapper per concurrent
    lane is the intended usage. If a single wrapper is shared
    across concurrent lanes the ``calls`` list will still be
    consistent (Python list ``append`` is atomic) but per-call
    timing fidelity may degrade because the inner client's wall
    clock isn't synchronized with the lanes.
    """

    def __init__(self, inner: Any, model: str,
                 *,
                 time_source: Callable[[], float] = time.monotonic):
        self.inner = inner
        self.model = model
        self.calls: list[CallTiming] = []
        self._time = time_source
        # Pass-through state the bench / agent loop may read.
        # We mirror the underlying client's last_inference_s value
        # on each call so callers reading these attributes get the
        # same numbers they would directly from the inner client.
        self.last_inference_s: float = 0.0
        self.total_inference_s: float = 0.0

    # ---------- non-streaming path ---------- #

    def chat(self, payload: dict) -> dict:
        """Forward to ``inner.chat()`` and record a coarse
        ``CallTiming``: TTFT ≈ total wall, no inter-token data.

        Non-streamed calls don't expose per-token timing — we only
        see one big response. The recorded CallTiming sets
        ``time_to_first_token_ms`` equal to the total wall (since
        the first byte of any response is also the last byte from
        our perspective) and leaves ``inter_token_gaps_ms`` empty.
        """
        if not hasattr(self.inner, "chat"):
            raise AttributeError(
                f"TimingCaptureChat: inner {type(self.inner).__name__} "
                f"has no chat method",
            )

        t0 = self._time()
        err: Optional[str] = None
        cancelled = False
        result: Optional[dict] = None
        total_tokens = 0
        try:
            result = self.inner.chat(payload)
            # Best-effort token count from the OpenAI-shape usage block.
            if isinstance(result, dict):
                usage = result.get("usage") or {}
                total_tokens = int(usage.get("completion_tokens", 0))
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {e}"
            # Don't classify as cancelled here — chat() doesn't have
            # the cancel_check contract; any exception is real.
            raise
        finally:
            t1 = self._time()
            wall_ms = (t1 - t0) * 1000.0
            self.calls.append(CallTiming(
                model=self.model,
                started_ts=t0,
                ended_ts=t1,
                # TTFT ≈ wall for the non-streamed path; we can't
                # observe the first byte separately.
                time_to_first_token_ms=wall_ms if err is None else None,
                inter_token_gaps_ms=[],
                total_tokens=total_tokens,
                error=err,
                cancelled=cancelled,
            ))
            self._sync_inference_timer()

        return result   # type: ignore[return-value]

    # ---------- streaming path (the main event) ---------- #

    def chat_streamed(self, payload: dict,
                      *,
                      on_token: Optional[Callable[[str], None]] = None,
                      cancel_check: Optional[Callable[[], bool]] = None,
                      ) -> dict:
        """Forward to ``inner.chat_streamed()`` and record per-token
        timestamps.

        The recorded ``CallTiming`` row is appended in a ``finally``
        block, so trials that raise mid-stream still contribute
        their partial token data.

        ``CancelledByOrchestrator`` is treated as a cooperative
        abort: ``cancelled=True`` is recorded but the exception
        re-raises unchanged. Other exceptions record ``cancelled=False``
        with the exception preview in ``error``, then re-raise.
        """
        if not hasattr(self.inner, "chat_streamed"):
            raise AttributeError(
                f"TimingCaptureChat: inner {type(self.inner).__name__} "
                f"has no chat_streamed method",
            )

        # Lazy import — same reason as chat_client.py:
        # avoid coupling at the module level.
        try:
            from consultants.engine.stall import CancelledByOrchestrator
        except ImportError:  # pragma: no cover — defensive
            class CancelledByOrchestrator(Exception):  # type: ignore[no-redef]
                pass

        t0 = self._time()
        first_token_ts: list[Optional[float]] = [None]
        token_ts: list[float] = []

        def capture(token: str) -> None:
            now = self._time()
            if first_token_ts[0] is None:
                first_token_ts[0] = now
            token_ts.append(now)
            if on_token is not None:
                # Forward to the caller's on_token so the
                # StallController.mark_token chain stays intact.
                on_token(token)

        err: Optional[str] = None
        cancelled = False
        result: Optional[dict] = None
        try:
            result = self.inner.chat_streamed(
                payload,
                on_token=capture,
                cancel_check=cancel_check,
            )
        except CancelledByOrchestrator as e:
            err = f"{type(e).__name__}: {e}"
            cancelled = True
            raise
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {e}"
            raise
        finally:
            t1 = self._time()
            ttft_ms = (
                (first_token_ts[0] - t0) * 1000.0
                if first_token_ts[0] is not None else None
            )
            gaps_ms = [
                (token_ts[i] - token_ts[i - 1]) * 1000.0
                for i in range(1, len(token_ts))
            ]
            self.calls.append(CallTiming(
                model=self.model,
                started_ts=t0,
                ended_ts=t1,
                time_to_first_token_ms=ttft_ms,
                inter_token_gaps_ms=gaps_ms,
                total_tokens=len(token_ts),
                error=err,
                cancelled=cancelled,
            ))
            self._sync_inference_timer()

        return result   # type: ignore[return-value]

    # ---------- pass-throughs ---------- #

    def reset_inference_timer(self) -> None:
        """Reset the inner client's inference timer if it has one;
        also reset our mirror.
        """
        if hasattr(self.inner, "reset_inference_timer"):
            try:
                self.inner.reset_inference_timer()
            except Exception:  # pragma: no cover — best-effort
                log.exception("inner.reset_inference_timer raised; ignored")
        self.last_inference_s = 0.0
        self.total_inference_s = 0.0

    def _sync_inference_timer(self) -> None:
        """Copy the inner client's inference-timer values into our
        mirror so callers reading either get the same number.
        """
        for attr in ("last_inference_s", "total_inference_s"):
            if hasattr(self.inner, attr):
                try:
                    setattr(self, attr, float(getattr(self.inner, attr)))
                except Exception:  # pragma: no cover — defensive
                    pass

    # ---------- introspection ---------- #

    def clear_calls(self) -> None:
        """Drop the recorded ``CallTiming`` list. Useful between
        trials when the wrapper is reused (Tier 2 reuses one
        wrapper per role across questions; the bench clears the
        list at trial boundaries).
        """
        self.calls = []

    def __getattr__(self, name: str) -> Any:
        """Forward any other attribute access to the inner client.

        Means the wrapper can stand in wherever the bench passes a
        ``ChatClient`` directly (e.g. ``deps.researcher_chat_clients``)
        without adapting every interface. Triggered ONLY when a
        normal attribute lookup misses on the wrapper — explicit
        methods + fields above take precedence.
        """
        return getattr(self.inner, name)


# ====================================================================== #
# Percentile helper
# ====================================================================== #

def _percentile(values: list[float], p: float) -> Optional[float]:
    """Return the ``p``-th percentile of ``values`` (linear-interp,
    inclusive method) in the same units as the input.

    Returns ``None`` for empty input. For single-value input,
    returns the value itself regardless of ``p``. For 2+ values,
    uses ``statistics.quantiles(n=100, method='inclusive')``.

    ``p`` is in [0, 100].
    """
    if not values:
        return None
    if len(values) == 1:
        return float(values[0])
    if p <= 0:
        return float(min(values))
    if p >= 100:
        return float(max(values))
    quantiles = statistics.quantiles(
        values, n=100, method="inclusive",
    )
    # quantiles has length 99 (the 1st..99th percentile boundaries).
    # quantiles[i] = (i+1)-th percentile, so index = round(p) - 1.
    idx = int(round(p)) - 1
    idx = max(0, min(len(quantiles) - 1, idx))
    return float(quantiles[idx])


# ====================================================================== #
# Aggregation helpers (used by stall_bench when computing StallTrial)
# ====================================================================== #

@dataclass
class CallTimingAggregate:
    """Aggregate of a list of ``CallTiming``s into the per-trial
    percentile fields the bench records.

    All times in milliseconds. None values when the underlying
    sample list was empty.
    """

    num_calls: int = 0
    total_tokens: int = 0
    time_to_first_token_p50_ms: Optional[float] = None
    time_to_first_token_p99_ms: Optional[float] = None
    inter_token_p50_ms: Optional[float] = None
    inter_token_p99_ms: Optional[float] = None
    total_wall_ms: float = 0.0
    errors: list[str] = field(default_factory=list)
    cancelled_count: int = 0


def aggregate_calls(calls: list[CallTiming]) -> CallTimingAggregate:
    """Compute the per-trial aggregate from a list of CallTimings.

    Used by both Tier 1 (single call list, single trial) and
    Tier 2 (multiple call list, aggregated across all chat calls
    in the council run for the trial).
    """
    if not calls:
        return CallTimingAggregate()

    all_gaps: list[float] = []
    all_ttft: list[float] = []
    total_tokens = 0
    total_wall_ms = 0.0
    errors: list[str] = []
    cancelled_count = 0
    for c in calls:
        all_gaps.extend(c.inter_token_gaps_ms)
        if c.time_to_first_token_ms is not None:
            all_ttft.append(c.time_to_first_token_ms)
        total_tokens += c.total_tokens
        total_wall_ms += c.total_wall_ms()
        if c.error is not None:
            errors.append(c.error)
        if c.cancelled:
            cancelled_count += 1

    return CallTimingAggregate(
        num_calls=len(calls),
        total_tokens=total_tokens,
        time_to_first_token_p50_ms=_percentile(all_ttft, 50),
        time_to_first_token_p99_ms=_percentile(all_ttft, 99),
        inter_token_p50_ms=_percentile(all_gaps, 50),
        inter_token_p99_ms=_percentile(all_gaps, 99),
        total_wall_ms=total_wall_ms,
        errors=errors,
        cancelled_count=cancelled_count,
    )


__all__ = [
    "CallTiming",
    "CallTimingAggregate",
    "TimingCaptureChat",
    "aggregate_calls",
]
