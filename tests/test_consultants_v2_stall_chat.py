"""Tests for ``consultants.engine.stall_chat`` — adapter that
turns a streaming chat method + stall thresholds into the
``chat(payload) -> dict`` callable the agent loop runner expects.

Two surfaces:

1. ``make_stall_protected_chat_fn`` — uses a fake chat_streamed
   method that calls ``on_token`` on a controllable cadence.
2. ``make_hard_cap_only_chat_fn`` — fallback for clients with no
   streaming support.
3. ``stall_protected_chat_fn_for`` — dispatcher; picks the right
   protector based on client capability.

Pure-Python, no langgraph.
"""

from __future__ import annotations

import time
import unittest

from consultants.engine.stall import (
    CancelledByOrchestrator,
    HardCapExceeded,
    StallRetryExhausted,
)
from consultants.engine.stall_chat import (
    make_hard_cap_only_chat_fn,
    make_stall_protected_chat_fn,
    stall_protected_chat_fn_for,
)


# ---------- factory: streaming + stall detection ---------------- #

def _make_streaming_method(*, intervals_s: list[float],
                            content: str = "ok",
                            ignore_cancel: bool = False):
    """Build a chat_streamed-shaped method (``payload, on_token,
    cancel_check`` kwargs) that emits tokens at the given real-time
    intervals and returns a synthetic OpenAI-shape response."""
    def chat_streamed(payload, *, on_token=None, cancel_check=None):
        for sleep_for in intervals_s:
            if (not ignore_cancel
                    and cancel_check is not None
                    and cancel_check()):
                raise CancelledByOrchestrator()
            slept = 0.0
            slice_s = 0.02
            while slept < sleep_for:
                if (not ignore_cancel
                        and cancel_check is not None
                        and cancel_check()):
                    raise CancelledByOrchestrator()
                time.sleep(min(slice_s, sleep_for - slept))
                slept += slice_s
            if on_token is not None:
                on_token("t")
        return {
            "choices": [{"message": {"role": "assistant",
                                      "content": content},
                          "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                      "total_tokens": 2},
        }
    return chat_streamed


class TestMakeStallProtectedChatFn(unittest.TestCase):

    def test_happy_path_returns_unchanged_response(self):
        chat_streamed = _make_streaming_method(
            intervals_s=[0.02, 0.02], content="happy",
        )
        chat_fn = make_stall_protected_chat_fn(
            chat_streamed,
            stall_threshold_s=1.0, hard_cap_s=5.0,
            retries=0, check_interval_s=0.05,
            retry_backoff_s=0.01,
        )
        out = chat_fn({"model": "stub", "messages": []})
        self.assertEqual(
            out["choices"][0]["message"]["content"], "happy",
        )

    def test_stall_detected_and_retried(self):
        # First call: 1 token then hangs forever. Second: 2 fast tokens.
        first = _make_streaming_method(intervals_s=[0.02, 60.0])
        second = _make_streaming_method(
            intervals_s=[0.02, 0.02], content="retry-ok",
        )
        n = {"i": 0}
        def dispatch(payload, *, on_token=None, cancel_check=None):
            n["i"] += 1
            return (first if n["i"] == 1 else second)(
                payload, on_token=on_token, cancel_check=cancel_check,
            )

        chat_fn = make_stall_protected_chat_fn(
            dispatch,
            stall_threshold_s=0.3, hard_cap_s=10.0,
            retries=1, check_interval_s=0.05,
            retry_backoff_s=0.01,
        )
        out = chat_fn({"model": "stub", "messages": []})
        self.assertEqual(
            out["choices"][0]["message"]["content"], "retry-ok",
        )

    def test_retry_exhausted_raises_stall_retry_exhausted(self):
        always_stalls = _make_streaming_method(intervals_s=[0.02, 60.0])
        chat_fn = make_stall_protected_chat_fn(
            always_stalls,
            stall_threshold_s=0.3, hard_cap_s=10.0,
            retries=1, check_interval_s=0.05,
            retry_backoff_s=0.01,
        )
        with self.assertRaises(StallRetryExhausted):
            chat_fn({"model": "stub", "messages": []})

    def test_emits_events_when_sink_provided(self):
        events: list[dict] = []
        chat_streamed = _make_streaming_method(intervals_s=[0.02])
        chat_fn = make_stall_protected_chat_fn(
            chat_streamed,
            stall_threshold_s=1.0, hard_cap_s=5.0,
            retries=0, check_interval_s=0.05,
            retry_backoff_s=0.01,
            on_event=events.append,
        )
        chat_fn({"model": "stub", "messages": []})
        kinds = [e["kind"] for e in events]
        self.assertIn("stall.attempt.ok", kinds)


# ---------- factory: hard-cap-only fallback --------------------- #

class TestMakeHardCapOnlyChatFn(unittest.TestCase):

    def test_happy_path_returns_result(self):
        def chat(_payload):
            time.sleep(0.02)
            return {"choices": [{"message": {"content": "ok"}}]}
        chat_fn = make_hard_cap_only_chat_fn(chat, hard_cap_s=5.0)
        out = chat_fn({"model": "x"})
        self.assertEqual(out["choices"][0]["message"]["content"], "ok")

    def test_hard_cap_raises_hard_cap_exceeded(self):
        def slow(_payload):
            time.sleep(5.0)
            return {}
        chat_fn = make_hard_cap_only_chat_fn(slow, hard_cap_s=0.2)
        t0 = time.monotonic()
        with self.assertRaises(HardCapExceeded):
            chat_fn({"model": "x"})
        # Should give up promptly — well under the slow sleep.
        self.assertLess(time.monotonic() - t0, 1.0)

    def test_inner_error_propagates(self):
        def boom(_payload):
            raise RuntimeError("upstream gone")
        chat_fn = make_hard_cap_only_chat_fn(boom, hard_cap_s=5.0)
        with self.assertRaises(RuntimeError) as cm:
            chat_fn({"model": "x"})
        self.assertIn("upstream gone", str(cm.exception))

    def test_emits_events_when_sink_provided(self):
        events: list[dict] = []
        def chat(_payload):
            return {"choices": [{"message": {"content": "x"}}]}
        chat_fn = make_hard_cap_only_chat_fn(
            chat, hard_cap_s=5.0, on_event=events.append,
        )
        chat_fn({"model": "x"})
        self.assertEqual([e["kind"] for e in events],
                         ["stall.attempt.ok"])

    def test_hard_cap_emits_event_too(self):
        events: list[dict] = []
        def slow(_payload):
            time.sleep(5.0)
            return {}
        chat_fn = make_hard_cap_only_chat_fn(
            slow, hard_cap_s=0.2, on_event=events.append,
        )
        with self.assertRaises(HardCapExceeded):
            chat_fn({"model": "x"})
        self.assertIn("stall.attempt.hard_cap",
                      [e["kind"] for e in events])


# ---------- dispatcher: stall_protected_chat_fn_for ------------- #

class _StreamingClient:
    """Stub client with both ``chat`` and ``chat_streamed`` — gets
    the full streaming protector."""
    def chat(self, _payload):
        return {"choices": [{"message": {"content": "non-stream"}}]}

    def chat_streamed(self, _payload, *, on_token=None, cancel_check=None):
        if on_token:
            on_token("t")
        return {"choices": [{"message": {"content": "stream"}}]}


class _NonStreamingClient:
    """Stub client with only ``chat`` — gets the hard-cap fallback."""
    def chat(self, _payload):
        return {"choices": [{"message": {"content": "plain"}}]}


class TestStallProtectedChatFnFor(unittest.TestCase):

    def test_streaming_client_gets_streaming_protector(self):
        client = _StreamingClient()
        chat_fn = stall_protected_chat_fn_for(
            client,
            stall_threshold_s=1.0, hard_cap_s=5.0,
            retries=0, check_interval_s=0.05,
            retry_backoff_s=0.01,
        )
        out = chat_fn({"model": "x"})
        # Should route to chat_streamed, not chat.
        self.assertEqual(out["choices"][0]["message"]["content"], "stream")

    def test_non_streaming_client_gets_hard_cap_fallback(self):
        client = _NonStreamingClient()
        chat_fn = stall_protected_chat_fn_for(
            client, stall_threshold_s=1.0, hard_cap_s=5.0,
        )
        out = chat_fn({"model": "x"})
        # Routed to chat() via hard-cap wrapper.
        self.assertEqual(out["choices"][0]["message"]["content"], "plain")

    def test_raises_when_client_lacks_chat(self):
        class NoChat:
            pass
        with self.assertRaises(TypeError):
            stall_protected_chat_fn_for(
                NoChat(),
                stall_threshold_s=1.0, hard_cap_s=5.0,
            )

    def test_non_streaming_client_hard_cap_fires(self):
        class Slow:
            def chat(self, _payload):
                time.sleep(5.0)
                return {}
        chat_fn = stall_protected_chat_fn_for(
            Slow(), stall_threshold_s=1.0, hard_cap_s=0.2,
        )
        with self.assertRaises(HardCapExceeded):
            chat_fn({"model": "x"})


if __name__ == "__main__":
    unittest.main()
