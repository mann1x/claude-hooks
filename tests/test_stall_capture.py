"""Unit tests for ``benchmarks/consultants/stall_capture.py``.

Drives ``TimingCaptureChat`` against fake chat clients that
produce controlled per-token timing. Verifies that the captured
``CallTiming`` matches the input distribution and that the
percentile + aggregation helpers compute the documented values.

No cloud calls. Uses an injected ``time_source`` instead of real
``time.monotonic`` so tests stay sub-millisecond fast and
deterministic.
"""

from __future__ import annotations

import unittest
from typing import Any, Callable, Optional

from benchmarks.consultants.stall_capture import (
    CallTiming,
    CallTimingAggregate,
    TimingCaptureChat,
    aggregate_calls,
    _percentile,
)


# ====================================================================== #
# Fake clients
# ====================================================================== #

class _FakeClock:
    """Monotonic-clock stand-in. ``now()`` returns the current
    virtual time; ``advance(delta_s)`` moves it forward. Tests
    drive the clock manually from the fake clients' on_token
    callbacks so token timings are deterministic.
    """

    def __init__(self, start: float = 1000.0):
        self.t = float(start)

    def now(self) -> float:
        return self.t

    def advance(self, delta_s: float) -> None:
        self.t += delta_s


class _FakeStreamingClient:
    """Fake ``ChatClient.chat_streamed`` that emits ``tokens``
    tokens with the gap schedule in ``gaps_ms`` (len == tokens or
    tokens-1; missing tail-gaps default to 1ms each).

    The first token gap (before any token arrives) is the TTFT.
    Subsequent gaps are inter-token. Drives an injected
    ``_FakeClock`` so the wrapper's captured timings reflect the
    schedule exactly.
    """

    def __init__(self, clock: _FakeClock, tokens: int = 10,
                 schedule_ms: Optional[list[float]] = None,
                 raise_after: Optional[int] = None,
                 raise_cls: type = RuntimeError):
        self.clock = clock
        self.tokens = tokens
        self.schedule_ms = list(schedule_ms or [])
        self.raise_after = raise_after
        self.raise_cls = raise_cls
        # Pass-through state TimingCaptureChat may read.
        self.last_inference_s = 0.0
        self.total_inference_s = 0.0

    def chat_streamed(self, payload: dict, *,
                      on_token: Optional[Callable[[str], None]] = None,
                      cancel_check: Optional[Callable[[], bool]] = None,
                      ) -> dict:
        for i in range(self.tokens):
            gap = self.schedule_ms[i] if i < len(self.schedule_ms) else 1.0
            self.clock.advance(gap / 1000.0)   # ms -> s
            if on_token is not None:
                on_token(f"t{i}")
            if self.raise_after is not None and i + 1 == self.raise_after:
                raise self.raise_cls(f"forced after {i + 1} tokens")
            if cancel_check is not None and cancel_check():
                # Cooperative abort — mirror the real ChatClient.
                from consultants.engine.stall import CancelledByOrchestrator
                raise CancelledByOrchestrator("test cancel")
        return {
            "choices": [{"message": {"role": "assistant",
                                     "content": "".join(f"t{i}" for i in range(self.tokens))}}],
            "usage": {"prompt_tokens": 10,
                      "completion_tokens": self.tokens,
                      "total_tokens": 10 + self.tokens},
        }

    def chat(self, payload: dict) -> dict:
        # Non-streamed shape: returns the same dict the streamed
        # path would, but no per-token visibility.
        self.clock.advance(0.1)
        return {
            "choices": [{"message": {"role": "assistant", "content": "x"}}],
            "usage": {"prompt_tokens": 5,
                      "completion_tokens": 7,
                      "total_tokens": 12},
        }

    def reset_inference_timer(self) -> None:
        self.last_inference_s = 0.0
        self.total_inference_s = 0.0


# ====================================================================== #
# Tests
# ====================================================================== #

class TestPercentileHelper(unittest.TestCase):

    def test_empty_returns_none(self) -> None:
        self.assertIsNone(_percentile([], 50))

    def test_single_value_returns_that_value(self) -> None:
        self.assertEqual(_percentile([42.0], 99), 42.0)
        self.assertEqual(_percentile([42.0], 1), 42.0)

    def test_p0_returns_min(self) -> None:
        self.assertEqual(_percentile([5.0, 10.0, 15.0], 0), 5.0)

    def test_p100_returns_max(self) -> None:
        self.assertEqual(_percentile([5.0, 10.0, 15.0], 100), 15.0)

    def test_p50_returns_median_two_values(self) -> None:
        # statistics.quantiles inclusive method on [10, 20]:
        # p50 ≈ 15 (linear interp at the midpoint).
        self.assertAlmostEqual(_percentile([10.0, 20.0], 50), 15.0, places=1)

    def test_p99_outlier_dominates_when_tail_is_top_1pct(self) -> None:
        # ``inclusive`` p99 = the boundary below which 99% of data sits.
        # With a single outlier in 100 values, the outlier is at p100,
        # NOT p99 — so a 1-of-100 spike doesn't dominate p99 (it's
        # captured at p100 / max instead). For p99 to point INTO the
        # slow tail we need the top ~1% to be slow. 95 small + 5 big
        # puts 5% of the data in the outlier band, well into p99.
        small = [10.0] * 95
        big = [500.0] * 5
        p99 = _percentile(small + big, 99)
        self.assertIsNotNone(p99)
        self.assertGreater(p99, 400.0)

    def test_p100_catches_lone_outlier(self) -> None:
        # Companion to the above: a single 1-of-100 outlier IS at p100.
        small = [10.0] * 99
        big = [500.0]
        self.assertEqual(_percentile(small + big, 100), 500.0)


class TestCallTiming(unittest.TestCase):

    def test_total_wall_ms_computes_delta(self) -> None:
        ct = CallTiming(
            model="m", started_ts=10.0, ended_ts=11.5,
            time_to_first_token_ms=200.0,
            inter_token_gaps_ms=[10.0, 20.0, 30.0],
            total_tokens=4,
        )
        self.assertEqual(ct.total_wall_ms(), 1500.0)

    def test_p_inter_token_ms_uses_helper(self) -> None:
        ct = CallTiming(
            model="m", started_ts=0.0, ended_ts=1.0,
            time_to_first_token_ms=0.0,
            inter_token_gaps_ms=[10.0, 20.0, 30.0, 40.0, 50.0],
            total_tokens=6,
        )
        self.assertEqual(ct.p_inter_token_ms(0), 10.0)
        self.assertEqual(ct.p_inter_token_ms(100), 50.0)

    def test_p_inter_token_ms_none_on_empty_gaps(self) -> None:
        ct = CallTiming(
            model="m", started_ts=0.0, ended_ts=1.0,
            time_to_first_token_ms=None,
            inter_token_gaps_ms=[],
            total_tokens=0,
        )
        self.assertIsNone(ct.p_inter_token_ms(50))

    def test_to_dict_round_trips_through_json(self) -> None:
        import json
        ct = CallTiming(
            model="kimi-k2.6:cloud", started_ts=10.0, ended_ts=15.0,
            time_to_first_token_ms=300.0,
            inter_token_gaps_ms=[5.0, 10.0],
            total_tokens=3,
            error=None,
            cancelled=False,
        )
        d = ct.to_dict()
        self.assertEqual(d["model"], "kimi-k2.6:cloud")
        # Round-trips through JSON without loss.
        s = json.dumps(d)
        self.assertEqual(json.loads(s)["total_tokens"], 3)


class TestTimingCaptureChatStreamed(unittest.TestCase):
    """The streaming path is the main event."""

    def test_captures_ttft_from_first_token(self) -> None:
        clk = _FakeClock(start=1000.0)
        inner = _FakeStreamingClient(
            clk, tokens=3, schedule_ms=[50.0, 10.0, 10.0],
        )
        wrap = TimingCaptureChat(inner, model="m", time_source=clk.now)

        wrap.chat_streamed({"model": "m"})

        self.assertEqual(len(wrap.calls), 1)
        ct = wrap.calls[0]
        # First gap was 50ms BEFORE the first on_token fired.
        self.assertAlmostEqual(ct.time_to_first_token_ms, 50.0, places=1)
        self.assertEqual(ct.total_tokens, 3)
        # Inter-token gaps: 10ms each between subsequent tokens.
        self.assertEqual(len(ct.inter_token_gaps_ms), 2)
        for g in ct.inter_token_gaps_ms:
            self.assertAlmostEqual(g, 10.0, places=1)

    def test_p99_dominated_when_slow_tail_is_top_pct(self) -> None:
        # 95 fast tokens (10 ms gaps) + 5 slow ones (500 ms gaps),
        # plus one initial gap for TTFT. The slow tail is 5% of
        # the inter-token distribution → p99 lands deep in the
        # slow region.
        clk = _FakeClock(start=0.0)
        schedule = [10.0] + [10.0] * 95 + [500.0] * 5
        inner = _FakeStreamingClient(
            clk, tokens=101, schedule_ms=schedule,
        )
        wrap = TimingCaptureChat(inner, model="m", time_source=clk.now)

        wrap.chat_streamed({"model": "m"})
        ct = wrap.calls[0]
        # 101 tokens → 100 inter-token gaps; the first schedule entry
        # is the TTFT (before any token), then 100 gaps between
        # tokens.
        self.assertEqual(len(ct.inter_token_gaps_ms), 100)
        self.assertAlmostEqual(ct.time_to_first_token_ms, 10.0, places=1)
        p99 = ct.p_inter_token_ms(99)
        self.assertIsNotNone(p99)
        self.assertGreater(p99, 400.0)

    def test_empty_stream_captures_no_tokens(self) -> None:
        clk = _FakeClock(start=0.0)
        inner = _FakeStreamingClient(clk, tokens=0)
        wrap = TimingCaptureChat(inner, model="m", time_source=clk.now)

        wrap.chat_streamed({"model": "m"})

        ct = wrap.calls[0]
        self.assertIsNone(ct.time_to_first_token_ms)
        self.assertEqual(ct.inter_token_gaps_ms, [])
        self.assertEqual(ct.total_tokens, 0)
        self.assertIsNone(ct.error)
        self.assertFalse(ct.cancelled)

    def test_inner_exception_records_partial_and_reraises(self) -> None:
        clk = _FakeClock(start=0.0)
        inner = _FakeStreamingClient(
            clk, tokens=5, schedule_ms=[10.0, 10.0, 10.0],
            raise_after=2, raise_cls=ValueError,
        )
        wrap = TimingCaptureChat(inner, model="m", time_source=clk.now)

        with self.assertRaises(ValueError):
            wrap.chat_streamed({"model": "m"})

        # CallTiming still appended in the finally block.
        self.assertEqual(len(wrap.calls), 1)
        ct = wrap.calls[0]
        self.assertEqual(ct.total_tokens, 2)
        self.assertIsNotNone(ct.error)
        self.assertIn("ValueError", ct.error)
        self.assertFalse(ct.cancelled)

    def test_cancelled_by_orchestrator_sets_cancelled_flag(self) -> None:
        from consultants.engine.stall import CancelledByOrchestrator

        clk = _FakeClock(start=0.0)
        inner = _FakeStreamingClient(clk, tokens=5, schedule_ms=[10.0] * 5)
        wrap = TimingCaptureChat(inner, model="m", time_source=clk.now)

        # cancel_check returns True after the 2nd token.
        called = [0]

        def cancel():
            called[0] += 1
            return called[0] > 2

        with self.assertRaises(CancelledByOrchestrator):
            wrap.chat_streamed({"model": "m"}, cancel_check=cancel)

        ct = wrap.calls[0]
        self.assertTrue(ct.cancelled)
        self.assertIsNotNone(ct.error)
        # CancelledByOrchestrator subclasses RuntimeError so the
        # class name appears in the error preview.
        self.assertIn("Cancelled", ct.error)

    def test_on_token_forwarded(self) -> None:
        """Caller's on_token still fires."""
        clk = _FakeClock(start=0.0)
        inner = _FakeStreamingClient(clk, tokens=3)
        wrap = TimingCaptureChat(inner, model="m", time_source=clk.now)

        seen: list[str] = []
        wrap.chat_streamed({"model": "m"}, on_token=lambda t: seen.append(t))

        self.assertEqual(seen, ["t0", "t1", "t2"])
        # And the wrapper still records its own timing.
        self.assertEqual(wrap.calls[0].total_tokens, 3)


class TestTimingCaptureChatSync(unittest.TestCase):
    """``chat()`` path: coarse TTFT, no gaps."""

    def test_records_coarse_timing(self) -> None:
        clk = _FakeClock(start=0.0)
        inner = _FakeStreamingClient(clk, tokens=0)
        wrap = TimingCaptureChat(inner, model="m", time_source=clk.now)

        wrap.chat({"model": "m"})

        ct = wrap.calls[0]
        # fake chat() advances clock by 100ms.
        self.assertAlmostEqual(ct.time_to_first_token_ms, 100.0, places=1)
        self.assertEqual(ct.inter_token_gaps_ms, [])
        self.assertEqual(ct.total_tokens, 7)   # from usage.completion_tokens

    def test_chat_exception_reraises(self) -> None:
        class _Boom:
            def chat(self, _payload: dict) -> dict:
                raise RuntimeError("nope")

        clk = _FakeClock()
        wrap = TimingCaptureChat(_Boom(), model="m", time_source=clk.now)
        with self.assertRaises(RuntimeError):
            wrap.chat({})
        self.assertEqual(len(wrap.calls), 1)
        self.assertIn("RuntimeError", wrap.calls[0].error)


class TestTimingCaptureChatPassThrough(unittest.TestCase):
    """The wrapper acts as a transparent proxy for unknown attrs
    and for the inference-timer pass-throughs.
    """

    def test_getattr_forwards_to_inner(self) -> None:
        class _Inner:
            custom_field = "hello"

            def chat(self, _payload: dict) -> dict:
                return {"ok": True}

            def custom_method(self) -> str:
                return "world"

        wrap = TimingCaptureChat(_Inner(), model="m")
        self.assertEqual(wrap.custom_field, "hello")
        self.assertEqual(wrap.custom_method(), "world")

    def test_clear_calls_drops_recorded(self) -> None:
        clk = _FakeClock()
        inner = _FakeStreamingClient(clk, tokens=2)
        wrap = TimingCaptureChat(inner, model="m", time_source=clk.now)
        wrap.chat_streamed({"model": "m"})
        self.assertEqual(len(wrap.calls), 1)
        wrap.clear_calls()
        self.assertEqual(wrap.calls, [])

    def test_reset_inference_timer_passes_through(self) -> None:
        inner = _FakeStreamingClient(_FakeClock(), tokens=0)
        inner.total_inference_s = 99.0
        wrap = TimingCaptureChat(inner, model="m")
        wrap.total_inference_s = 99.0
        wrap.reset_inference_timer()
        self.assertEqual(inner.total_inference_s, 0.0)
        self.assertEqual(wrap.total_inference_s, 0.0)


class TestAggregateCalls(unittest.TestCase):
    """``aggregate_calls`` is what the bench uses to fill StallTrial."""

    def _make_ct(self, ttft_ms: Optional[float],
                 gaps_ms: list[float], tokens: int,
                 error: Optional[str] = None,
                 cancelled: bool = False) -> CallTiming:
        return CallTiming(
            model="m", started_ts=0.0, ended_ts=1.0,
            time_to_first_token_ms=ttft_ms,
            inter_token_gaps_ms=gaps_ms,
            total_tokens=tokens,
            error=error,
            cancelled=cancelled,
        )

    def test_empty_input(self) -> None:
        agg = aggregate_calls([])
        self.assertEqual(agg.num_calls, 0)
        self.assertEqual(agg.total_tokens, 0)
        self.assertIsNone(agg.time_to_first_token_p99_ms)

    def test_single_call(self) -> None:
        ct = self._make_ct(50.0, [10.0, 20.0, 30.0], 4)
        agg = aggregate_calls([ct])
        self.assertEqual(agg.num_calls, 1)
        self.assertEqual(agg.total_tokens, 4)
        self.assertEqual(agg.time_to_first_token_p50_ms, 50.0)
        self.assertEqual(agg.time_to_first_token_p99_ms, 50.0)

    def test_multiple_calls_combines_gaps(self) -> None:
        a = self._make_ct(100.0, [10.0, 20.0], 3)
        b = self._make_ct(200.0, [30.0, 40.0], 3)
        agg = aggregate_calls([a, b])
        self.assertEqual(agg.num_calls, 2)
        self.assertEqual(agg.total_tokens, 6)
        # 4 gaps combined.
        # p50 lies between 20 and 30; p99 ≈ 40.
        self.assertGreater(agg.inter_token_p99_ms, 30.0)

    def test_errors_collected(self) -> None:
        a = self._make_ct(50.0, [10.0], 2, error="ValueError: bad")
        b = self._make_ct(50.0, [10.0], 2, error=None)
        c = self._make_ct(50.0, [10.0], 2, error="HTTPError: 500",
                          cancelled=False)
        agg = aggregate_calls([a, b, c])
        self.assertEqual(agg.errors, ["ValueError: bad", "HTTPError: 500"])

    def test_cancelled_count(self) -> None:
        a = self._make_ct(50.0, [10.0], 2, cancelled=True,
                          error="CancelledByOrchestrator")
        b = self._make_ct(50.0, [10.0], 2)
        c = self._make_ct(50.0, [10.0], 2, cancelled=True,
                          error="CancelledByOrchestrator")
        agg = aggregate_calls([a, b, c])
        self.assertEqual(agg.cancelled_count, 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
