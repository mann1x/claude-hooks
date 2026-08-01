"""Tests for ``consultants.engine.stall`` — stall detection +
retry orchestrator.

Three test surfaces:

1. ``classify_stall`` — pure decision function, no threading.
2. ``StallController`` — cooperation primitive, sanity-check
   thread-safety + the mark/cancel contract.
3. ``StallMonitor`` / ``chat_with_stall_protection`` — orchestrator
   wired through a stub ``chat_streamed_fn`` that emits tokens on
   a controllable cadence. These tests use REAL threads but with
   sub-second thresholds so the suite stays fast.

Pure-Python, no langgraph. Lives in the main ``claude-hooks`` test
env.
"""

from __future__ import annotations

import threading
import time
import unittest

from consultants.engine.stall import (
    CancelledByOrchestrator,
    HardCapExceeded,
    StallConfig,
    StallController,
    StallMonitor,
    StallOutcome,
    StallRetryExhausted,
    chat_with_stall_protection,
    classify_stall,
)


# ============================================================== #
# 1. classify_stall — pure function, no threading needed.
# ============================================================== #

class TestClassifyStall(unittest.TestCase):

    def test_progressing_no_tokens_within_threshold(self):
        # 100s elapsed, 0 tokens, threshold 300s → still progressing.
        out = classify_stall(
            started_ts=0.0, last_token_ts=None, tokens_emitted=0,
            now_ts=100.0,
            stall_threshold_s=300.0, hard_cap_s=3600.0,
        )
        self.assertEqual(out, StallOutcome.PROGRESSING)

    def test_startup_stall_past_threshold_no_tokens(self):
        # 350s elapsed, 0 tokens → startup stall.
        out = classify_stall(
            started_ts=0.0, last_token_ts=None, tokens_emitted=0,
            now_ts=350.0,
            stall_threshold_s=300.0, hard_cap_s=3600.0,
        )
        self.assertEqual(out, StallOutcome.STARTUP_STALL)

    def test_progressing_with_recent_token(self):
        # 500s elapsed, last token 50s ago, threshold 300s → progressing.
        out = classify_stall(
            started_ts=0.0, last_token_ts=450.0, tokens_emitted=20,
            now_ts=500.0,
            stall_threshold_s=300.0, hard_cap_s=3600.0,
        )
        self.assertEqual(out, StallOutcome.PROGRESSING)

    def test_mid_stream_stall_when_gap_exceeds_threshold(self):
        # 500s elapsed, last token 100s in (so 400s ago), threshold 300s.
        out = classify_stall(
            started_ts=0.0, last_token_ts=100.0, tokens_emitted=15,
            now_ts=500.0,
            stall_threshold_s=300.0, hard_cap_s=3600.0,
        )
        self.assertEqual(out, StallOutcome.MID_STREAM_STALL)

    def test_hard_cap_wins_over_stall(self):
        # Past hard_cap even though tokens are flowing — hard cap wins.
        out = classify_stall(
            started_ts=0.0, last_token_ts=3700.0, tokens_emitted=100,
            now_ts=3800.0,
            stall_threshold_s=300.0, hard_cap_s=3600.0,
        )
        self.assertEqual(out, StallOutcome.HARD_CAP_EXCEEDED)

    def test_hard_cap_wins_over_startup_stall(self):
        # Both startup-stalled (no tokens for 4000s) AND past hard
        # cap; hard cap is the more authoritative outcome.
        out = classify_stall(
            started_ts=0.0, last_token_ts=None, tokens_emitted=0,
            now_ts=4000.0,
            stall_threshold_s=300.0, hard_cap_s=3600.0,
        )
        self.assertEqual(out, StallOutcome.HARD_CAP_EXCEEDED)

    def test_defends_against_none_last_token_with_count(self):
        # tokens_emitted > 0 but last_token_ts None — would normally
        # be a bug from the caller, but we should fall back to
        # started_ts and not raise.
        out = classify_stall(
            started_ts=0.0, last_token_ts=None, tokens_emitted=5,
            now_ts=100.0,
            stall_threshold_s=300.0, hard_cap_s=3600.0,
        )
        self.assertEqual(out, StallOutcome.PROGRESSING)


# ============================================================== #
# 2. StallController — cooperation primitive.
# ============================================================== #

class TestStallController(unittest.TestCase):

    def test_initial_state(self):
        c = StallController(started_ts=100.0,
                            time_source=lambda: 150.0)
        p = c.progress()
        self.assertEqual(p.started_ts, 100.0)
        self.assertIsNone(p.last_token_ts)
        self.assertEqual(p.tokens_emitted, 0)
        self.assertFalse(c.is_cancelled())

    def test_mark_token_updates_progress(self):
        clock = {"now": 100.0}
        c = StallController(started_ts=100.0,
                            time_source=lambda: clock["now"])
        clock["now"] = 110.0
        c.mark_token()
        p = c.progress()
        self.assertEqual(p.last_token_ts, 110.0)
        self.assertEqual(p.tokens_emitted, 1)
        clock["now"] = 115.0
        c.mark_token()
        p2 = c.progress()
        self.assertEqual(p2.last_token_ts, 115.0)
        self.assertEqual(p2.tokens_emitted, 2)

    def test_cancel_is_idempotent(self):
        c = StallController(started_ts=0.0)
        self.assertFalse(c.is_cancelled())
        c.cancel()
        self.assertTrue(c.is_cancelled())
        # Second cancel must not raise.
        c.cancel()
        self.assertTrue(c.is_cancelled())

    def test_thread_safety_under_concurrent_marks(self):
        """Smoke check: 8 threads each mark 100 tokens; final
        count is exactly 800."""
        c = StallController(started_ts=0.0,
                            time_source=lambda: 0.0)
        def worker():
            for _ in range(100):
                c.mark_token()
        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(c.progress().tokens_emitted, 800)


# ============================================================== #
# 3. StallMonitor — orchestrator with stub chat fns.
# ============================================================== #

def _make_streaming_stub(*, token_intervals_s: list[float],
                          stop_after_tokens=None,
                          ignore_cancel: bool = False,
                          raise_after_tokens: BaseException = None,
                          result: dict = None):
    """Build a chat_streamed_fn stub that emits tokens at the given
    real-time intervals (using time.sleep), honors cancellation by
    default, and returns the supplied result dict on completion.

    ``token_intervals_s``: list of sleeps BEFORE each token. e.g.
    ``[0.05, 0.05, 0.05]`` emits 3 tokens with 50ms between each.
    """
    if result is None:
        result = {"choices": [{"message": {"role": "assistant",
                                            "content": "ok"}}],
                  "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    def chat_streamed_fn(payload, controller):
        emitted = 0
        for sleep_for in token_intervals_s:
            # Cooperative cancellation poll BEFORE the sleep so the
            # watchdog can interrupt us cleanly.
            if not ignore_cancel and controller.is_cancelled():
                raise CancelledByOrchestrator()
            # Use small sleep slices so we honor cancel snappily.
            slept = 0.0
            slice_s = 0.02
            while slept < sleep_for:
                if not ignore_cancel and controller.is_cancelled():
                    raise CancelledByOrchestrator()
                time.sleep(min(slice_s, sleep_for - slept))
                slept += slice_s
            controller.mark_token()
            emitted += 1
            if (stop_after_tokens is not None
                    and emitted >= stop_after_tokens):
                break
            if (raise_after_tokens is not None
                    and emitted >= 1):
                raise raise_after_tokens
        return result

    return chat_streamed_fn


class TestStallMonitorHappyPath(unittest.TestCase):

    def test_call_returns_result_when_progressing(self):
        # 5 tokens at 20ms each = 100ms total. Threshold 1s, cap 5s.
        stub = _make_streaming_stub(token_intervals_s=[0.02] * 5)
        cfg = StallConfig(
            stall_threshold_s=1.0, hard_cap_s=5.0,
            retries=1, check_interval_s=0.05,
            retry_backoff_s=0.01, join_grace_s=0.5,
        )
        mon = StallMonitor(cfg)
        out = mon.run(stub, {"model": "stub"})
        self.assertEqual(out["choices"][0]["message"]["content"], "ok")
        self.assertEqual(len(mon.attempts), 1)
        self.assertEqual(mon.attempts[0]["outcome"], "ok")
        self.assertEqual(mon.attempts[0]["tokens_emitted"], 5)


class TestStallMonitorStallPaths(unittest.TestCase):

    def test_mid_stream_stall_then_retry_succeeds(self):
        """First call: 2 fast tokens then hangs forever (well past
        the stall threshold). Retry: 3 quick tokens to completion.
        Should return retry's result with attempts[0]=stalled,
        attempts[1]=ok.
        """
        # Use a list with 2 quick + 1 absurdly long interval so the
        # call effectively hangs after token 2.
        first_call = _make_streaming_stub(
            token_intervals_s=[0.02, 0.02, 60.0],
        )
        second_call = _make_streaming_stub(
            token_intervals_s=[0.02, 0.02, 0.02],
            result={"choices": [{"message": {"role": "assistant",
                                              "content": "retry-ok"}}],
                    "usage": {}},
        )

        # Chain them: the first time chat_streamed_fn is called,
        # use first_call; second time, use second_call.
        call_count = {"n": 0}
        def dispatch(payload, controller):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return first_call(payload, controller)
            return second_call(payload, controller)

        cfg = StallConfig(
            stall_threshold_s=0.5, hard_cap_s=10.0,
            retries=1, check_interval_s=0.05,
            retry_backoff_s=0.01, join_grace_s=0.5,
        )
        mon = StallMonitor(cfg)
        out = mon.run(dispatch, {"model": "stub"})
        self.assertEqual(
            out["choices"][0]["message"]["content"], "retry-ok",
        )
        self.assertEqual(len(mon.attempts), 2)
        self.assertEqual(mon.attempts[0]["outcome"], "mid_stream_stall")
        self.assertEqual(mon.attempts[1]["outcome"], "ok")

    def test_mid_stream_stall_retry_also_stalls_raises_exhausted(self):
        """Both attempts stall mid-stream → StallRetryExhausted."""
        always_stalls = _make_streaming_stub(
            token_intervals_s=[0.02, 60.0],
        )
        cfg = StallConfig(
            stall_threshold_s=0.4, hard_cap_s=10.0,
            retries=1, check_interval_s=0.05,
            retry_backoff_s=0.01, join_grace_s=0.5,
        )
        mon = StallMonitor(cfg)
        with self.assertRaises(StallRetryExhausted) as cm:
            mon.run(always_stalls, {"model": "stub"})
        self.assertEqual(len(mon.attempts), 2)
        for a in mon.attempts:
            self.assertEqual(a["outcome"], "mid_stream_stall")
        self.assertEqual(cm.exception.attempts, 2)
        self.assertEqual(cm.exception.outcome,
                         StallOutcome.MID_STREAM_STALL)

    def test_startup_stall_detected(self):
        """Worker sleeps before first token longer than threshold."""
        slow_start = _make_streaming_stub(token_intervals_s=[60.0])
        cfg = StallConfig(
            stall_threshold_s=0.3, hard_cap_s=10.0,
            retries=0, check_interval_s=0.05,
            retry_backoff_s=0.01, join_grace_s=0.5,
        )
        mon = StallMonitor(cfg)
        with self.assertRaises(StallRetryExhausted) as cm:
            mon.run(slow_start, {"model": "stub"})
        self.assertEqual(mon.attempts[0]["outcome"], "startup_stall")
        self.assertEqual(cm.exception.outcome,
                         StallOutcome.STARTUP_STALL)
        self.assertEqual(cm.exception.attempts, 1)

    def test_zero_retries_means_one_attempt(self):
        """``retries=0`` → exactly 1 attempt allowed."""
        stub = _make_streaming_stub(token_intervals_s=[60.0])
        cfg = StallConfig(
            stall_threshold_s=0.3, hard_cap_s=10.0, retries=0,
            check_interval_s=0.05, retry_backoff_s=0.01,
            join_grace_s=0.5,
        )
        mon = StallMonitor(cfg)
        with self.assertRaises(StallRetryExhausted):
            mon.run(stub, {"model": "stub"})
        self.assertEqual(len(mon.attempts), 1)


class TestStallMonitorHardCap(unittest.TestCase):

    def test_hard_cap_raises_without_retry(self):
        """Worker emits tokens steadily for longer than the hard cap.
        No stall (token cadence is fast), but elapsed time exceeds
        hard_cap_s → HardCapExceeded with no retry.
        """
        # 40 tokens at 50ms each = ~2s of work; hard cap 0.4s.
        stub = _make_streaming_stub(token_intervals_s=[0.05] * 40)
        cfg = StallConfig(
            stall_threshold_s=10.0, hard_cap_s=0.4,
            retries=2, check_interval_s=0.05,
            retry_backoff_s=0.01, join_grace_s=0.5,
        )
        mon = StallMonitor(cfg)
        with self.assertRaises(HardCapExceeded) as cm:
            mon.run(stub, {"model": "stub"})
        # Exactly ONE attempt — hard cap never retries.
        self.assertEqual(len(mon.attempts), 1)
        self.assertEqual(mon.attempts[0]["outcome"], "hard_cap")
        self.assertEqual(cm.exception.outcome,
                         StallOutcome.HARD_CAP_EXCEEDED)


class TestStallMonitorErrorPropagation(unittest.TestCase):

    def test_real_error_from_chat_fn_propagates(self):
        """If the chat fn raises a non-cancellation exception, the
        orchestrator re-raises it (stall layer doesn't own that
        failure mode)."""
        boom = RuntimeError("upstream 500")
        def chat_fn(payload, controller):
            # Mark one token so we're not in startup-stall territory,
            # then raise.
            controller.mark_token()
            raise boom

        cfg = StallConfig(
            stall_threshold_s=10.0, hard_cap_s=10.0, retries=1,
            check_interval_s=0.05, retry_backoff_s=0.01,
            join_grace_s=0.5,
        )
        mon = StallMonitor(cfg)
        with self.assertRaises(RuntimeError) as cm:
            mon.run(chat_fn, {"model": "stub"})
        self.assertIn("upstream 500", str(cm.exception))


class TestStallMonitorEvents(unittest.TestCase):

    def test_on_event_sink_receives_attempts(self):
        events: list[dict] = []
        stub = _make_streaming_stub(token_intervals_s=[0.02, 0.02])
        cfg = StallConfig(
            stall_threshold_s=1.0, hard_cap_s=5.0, retries=0,
            check_interval_s=0.05, retry_backoff_s=0.01,
            join_grace_s=0.5,
        )
        mon = StallMonitor(cfg, on_event=events.append)
        mon.run(stub, {"model": "stub"})
        # One ok event for the happy path.
        kinds = [e["kind"] for e in events]
        self.assertIn("stall.attempt.ok", kinds)

    def test_on_event_sink_receives_stalled_then_ok(self):
        events: list[dict] = []
        first = _make_streaming_stub(token_intervals_s=[0.02, 60.0])
        second = _make_streaming_stub(token_intervals_s=[0.02, 0.02])
        n = {"i": 0}
        def dispatch(p, c):
            n["i"] += 1
            return (first if n["i"] == 1 else second)(p, c)

        cfg = StallConfig(
            stall_threshold_s=0.4, hard_cap_s=5.0, retries=1,
            check_interval_s=0.05, retry_backoff_s=0.01,
            join_grace_s=0.5,
        )
        mon = StallMonitor(cfg, on_event=events.append)
        mon.run(dispatch, {"model": "stub"})
        kinds = [e["kind"] for e in events]
        # We expect at least one stalled and one ok event.
        self.assertIn("stall.attempt.stalled", kinds)
        self.assertIn("stall.attempt.ok", kinds)

    def test_event_sink_failure_does_not_break_orchestrator(self):
        """A buggy on_event must not propagate into the call path."""
        def buggy_sink(_event):
            raise RuntimeError("bad sink")
        stub = _make_streaming_stub(token_intervals_s=[0.02, 0.02])
        cfg = StallConfig(
            stall_threshold_s=1.0, hard_cap_s=5.0, retries=0,
            check_interval_s=0.05, retry_backoff_s=0.01,
            join_grace_s=0.5,
        )
        mon = StallMonitor(cfg, on_event=buggy_sink)
        out = mon.run(stub, {"model": "stub"})
        # Result still returned.
        self.assertIn("choices", out)


class TestChatWithStallProtectionWrapper(unittest.TestCase):

    def test_wrapper_runs_a_call_returns_result(self):
        stub = _make_streaming_stub(token_intervals_s=[0.02])
        out = chat_with_stall_protection(
            stub, {"model": "x"},
            stall_threshold_s=1.0, hard_cap_s=5.0,
            retries=0, check_interval_s=0.05, retry_backoff_s=0.01,
        )
        self.assertEqual(out["choices"][0]["message"]["content"], "ok")

    def test_wrapper_emits_events_when_sink_provided(self):
        seen: list[dict] = []
        stub = _make_streaming_stub(token_intervals_s=[0.02])
        chat_with_stall_protection(
            stub, {"model": "x"},
            stall_threshold_s=1.0, hard_cap_s=5.0,
            retries=0, check_interval_s=0.05, retry_backoff_s=0.01,
            on_event=seen.append,
        )
        self.assertTrue(any(e["kind"] == "stall.attempt.ok"
                            for e in seen))


# ============================================================== #
# 4. Misbehaving chat fn — ignores cancel flag.
# ============================================================== #

class TestStallMonitorIgnoresUncoopChatFn(unittest.TestCase):

    def test_chat_fn_ignoring_cancel_does_not_block_orchestrator(self):
        """If the chat fn ignores controller.is_cancelled() (or
        doesn't poll often enough), the orchestrator must still
        treat the call as stalled and move on. The worker thread
        is daemon=True so it dies with the process."""
        # Stub that ignores cancel completely; will keep sleeping
        # for 5s after the stall is detected.
        stub = _make_streaming_stub(
            token_intervals_s=[0.02, 0.02, 5.0],
            ignore_cancel=True,
        )
        cfg = StallConfig(
            stall_threshold_s=0.3, hard_cap_s=10.0,
            retries=0, check_interval_s=0.05,
            retry_backoff_s=0.01, join_grace_s=0.2,
        )
        mon = StallMonitor(cfg)
        # Should raise StallRetryExhausted within ~ stall_threshold +
        # join_grace ≈ 0.5s, NOT wait for the worker to finish (5s).
        t0 = time.monotonic()
        with self.assertRaises(StallRetryExhausted):
            mon.run(stub, {"model": "stub"})
        elapsed = time.monotonic() - t0
        # Generous bound — we should finish well under 2 seconds.
        self.assertLess(elapsed, 2.0,
                        f"orchestrator blocked on stuck worker for {elapsed:.2f}s")


if __name__ == "__main__":
    unittest.main()
