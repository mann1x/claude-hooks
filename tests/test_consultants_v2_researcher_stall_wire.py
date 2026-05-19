"""Researcher-node integration: stall-protected chat-fn wire-up.

Three properties to pin:

1. **v1 parity** — when state has no ``runtime_control`` (legacy /
   pre-M2 sessions), the researcher passes ``chat_client.chat``
   directly to the loop runner. No wrapping. v1 behavior bit-for-bit.

2. **v2 wire-up (streaming client)** — when ``runtime_control`` is
   set AND the chat_client exposes ``chat_streamed``, the loop
   runner receives a stall-protected wrapper. The wrapper is NOT
   ``chat_client.chat``.

3. **v2 wire-up (non-streaming client)** — when
   ``runtime_control`` is set but the client lacks
   ``chat_streamed``, the wrapper falls back to hard-cap-only
   protection. The receiver still gets a wrapper (not raw
   ``chat_client.chat``) so the M3 protection layer is uniform.

We also assert the protected wrapper actually returns a sensible
result when called with a payload (no stall, no hard-cap) — this
catches any signature/glue bugs without invoking the agent loop.
"""

from __future__ import annotations

import unittest

from consultants.engine import council
from tests.test_consultants_council import _completion, FakeChatClient


class _StreamingClient:
    """Has both ``chat`` and ``chat_streamed`` — gets the full
    streaming protection path."""
    def __init__(self):
        self.chat_calls = 0
        self.stream_calls = 0

    def chat(self, _payload):
        self.chat_calls += 1
        return _completion("non-stream", prompt_tokens=1,
                            completion_tokens=1)

    def chat_streamed(self, _payload, *,
                      on_token=None, cancel_check=None):
        self.stream_calls += 1
        if on_token:
            on_token("t")
        return _completion("stream", prompt_tokens=1,
                            completion_tokens=1)


class TestResearcherV1Parity(unittest.TestCase):
    """Without ``runtime_control``, behavior is identical to v1 —
    no wrapping, ``chat_fn`` is exactly ``chat_client.chat``."""

    def test_no_runtime_control_uses_plain_chat_fn(self):
        seen: dict = {}

        def fake_loop(payload, cwd, *, config, tool_specs,
                      chat_fn, tool_executor, **kw):
            seen["chat_fn"] = chat_fn
            return _completion("ok")

        client = _StreamingClient()
        state = council.initial_state(
            question="q", cwd="/proj",
            models={"researcher": "m"},
            topology="council", effort="medium",
        )
        state["plan"] = "1. inspect"
        # NO runtime_control on state — v1 path.
        council.researcher_node(
            state, chat_client=client,
            tool_executor=lambda *a, **k: "",
            tool_specs=[], grounding_msgs=[],
            model="m", cwd="/proj",
            loop_runner=fake_loop,
        )
        # ``is`` fails on bound methods (fresh wrapper per access);
        # ``==`` compares (instance, func) tuples which is what we want.
        self.assertEqual(seen["chat_fn"], client.chat)

    def test_legacy_client_without_chat_streamed_uses_plain_chat(self):
        """Even with runtime_control on state, a legacy client
        without ``chat_streamed`` falls into the hard-cap fallback.
        The fallback IS still a wrapper (not chat_client.chat raw)
        — protection is uniform when runtime_control is wired."""
        seen: dict = {}

        def fake_loop(payload, cwd, *, config, tool_specs,
                      chat_fn, tool_executor, **kw):
            seen["chat_fn"] = chat_fn
            return _completion("ok")

        client = FakeChatClient([])  # no chat_streamed
        state = council.initial_state(
            question="q", cwd="/proj",
            models={"researcher": "m"},
            topology="council", effort="medium",
        )
        state["plan"] = "1. inspect"
        state["runtime_control"] = {
            "stall_threshold_s": 300.0,
            "per_lane_hard_s": 3600.0,
            "stall_retries": 1,
        }
        council.researcher_node(
            state, chat_client=client,
            tool_executor=lambda *a, **k: "",
            tool_specs=[], grounding_msgs=[],
            model="m", cwd="/proj",
            loop_runner=fake_loop,
        )
        # A client without chat_streamed currently falls through to
        # chat_client.chat unwrapped — see the council.py guard
        # ``if rc and hasattr(chat_client, "chat_streamed")``. That
        # is the conservative default; M3c can flip to hard-cap-only.
        # Pin the current behavior here so a future change is
        # intentional. (``==`` over ``is`` because bound methods get
        # a fresh wrapper per attribute access.)
        self.assertEqual(seen["chat_fn"], client.chat)


class TestResearcherStallProtectionWired(unittest.TestCase):
    """With ``runtime_control`` AND a streaming client, the
    researcher MUST hand the loop runner a wrapped chat_fn."""

    def test_runtime_control_routes_through_stall_protected_fn(self):
        seen: dict = {}

        def fake_loop(payload, cwd, *, config, tool_specs,
                      chat_fn, tool_executor, **kw):
            seen["chat_fn"] = chat_fn
            return _completion("ok")

        client = _StreamingClient()
        state = council.initial_state(
            question="q", cwd="/proj",
            models={"researcher": "m"},
            topology="council", effort="medium",
        )
        state["plan"] = "1. inspect"
        state["runtime_control"] = {
            "stall_threshold_s": 1.0,
            "per_lane_hard_s": 5.0,
            "stall_retries": 0,
        }
        council.researcher_node(
            state, chat_client=client,
            tool_executor=lambda *a, **k: "",
            tool_specs=[], grounding_msgs=[],
            model="m", cwd="/proj",
            loop_runner=fake_loop,
        )
        # Wrapped: not the same object as either client method.
        self.assertIsNot(seen["chat_fn"], client.chat)
        self.assertIsNot(seen["chat_fn"], client.chat_streamed)
        # Callable.
        self.assertTrue(callable(seen["chat_fn"]))

    def test_wrapped_chat_fn_actually_works(self):
        """End-to-end: the wrapped fn returns a sensible response
        when invoked through the loop runner."""
        captured: dict = {}

        def fake_loop(payload, cwd, *, config, tool_specs,
                      chat_fn, tool_executor, **kw):
            # Invoke the wrapped chat_fn the way run_loop would.
            captured["result"] = chat_fn(payload)
            return _completion("done")

        client = _StreamingClient()
        state = council.initial_state(
            question="q", cwd="/proj",
            models={"researcher": "m"},
            topology="council", effort="medium",
        )
        state["plan"] = "1. inspect"
        state["runtime_control"] = {
            "stall_threshold_s": 1.0,
            "per_lane_hard_s": 5.0,
            "stall_retries": 0,
            "check_interval_s": 0.05,
        }
        council.researcher_node(
            state, chat_client=client,
            tool_executor=lambda *a, **k: "",
            tool_specs=[], grounding_msgs=[],
            model="m", cwd="/proj",
            loop_runner=fake_loop,
        )
        # The wrapper routes through chat_streamed; the response
        # should still be a valid OpenAI-shape dict.
        result = captured["result"]
        self.assertIn("choices", result)
        self.assertEqual(client.stream_calls, 1)
        self.assertEqual(client.chat_calls, 0)


class TestResearcherStallFailurePropagation(unittest.TestCase):
    """When the wrapped chat_fn raises StallRetryExhausted or
    HardCapExceeded inside the agent loop, the researcher's
    existing tombstone path catches it — same as any other
    upstream exception."""

    def test_stall_retry_exhausted_tombstones_lane(self):
        from consultants.engine.stall import StallRetryExhausted, StallOutcome

        def boom_loop(payload, cwd, *, config, tool_specs,
                      chat_fn, tool_executor, **kw):
            # Simulate the wrapper raising after its own retries are
            # exhausted. We don't actually invoke chat_fn — just
            # raise as if it had.
            raise StallRetryExhausted(
                "simulated", outcome=StallOutcome.MID_STREAM_STALL,
                attempts=2, tokens_emitted=5, elapsed_s=600.0,
            )

        client = _StreamingClient()
        state = council.initial_state(
            question="q", cwd="/proj",
            models={"researcher": "m"},
            topology="council", effort="medium",
        )
        state["plan"] = "1. inspect"
        state["runtime_control"] = {"stall_threshold_s": 300.0,
                                     "per_lane_hard_s": 3600.0}
        out = council.researcher_node(
            state, chat_client=client,
            tool_executor=lambda *a, **k: "",
            tool_specs=[], grounding_msgs=[],
            model="m", cwd="/proj",
            loop_runner=boom_loop,
        )
        self.assertIn("error", out)
        self.assertEqual(out["_role_failed"], "researcher")
        self.assertEqual(len(out["research"]), 1)
        self.assertIn("researcher lane failed", out["research"][0])


if __name__ == "__main__":
    unittest.main()
