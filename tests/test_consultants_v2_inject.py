"""Tests for the v2 ``additional_context`` channel + prompt wiring.

Covers two layers:

1. **The block renderer** ``_additional_context_block`` —
   pure-Python; controls the literal text appended to user
   messages.
2. **The message builders** ``build_{planner,researcher,critic,
   synthesizer}_messages`` — they accept an optional
   ``additional_context`` kwarg and append the block to the
   tail of the user message. The kwarg is keyword-only so v1
   call sites that omit it render byte-identical messages.
3. **Node-level integration** — planner/researcher/synthesizer
   nodes call ``unconsumed_context_for(state, role)`` and pass
   the result through. Verified against a stub chat_client that
   records the messages it received.

All tests run on the main ``claude-hooks`` env (no langgraph).
"""

from __future__ import annotations

import unittest
from typing import Any

from consultants.engine.council import (
    _additional_context_block,
    build_critic_messages,
    build_planner_messages,
    build_researcher_messages,
    build_synthesizer_messages,
    planner_node,
    synthesizer_node,
)
from consultants.engine.state_v2 import Doc


# ============================================================== #
# Block renderer
# ============================================================== #

class TestAdditionalContextBlock(unittest.TestCase):

    def test_empty_returns_empty_string(self):
        self.assertEqual(_additional_context_block([]), "")
        self.assertEqual(_additional_context_block(None), "")

    def test_single_doc_renders_numbered(self):
        out = _additional_context_block(
            [Doc(role="researcher", text="Check GDPR.")]
        )
        self.assertIn("ADDITIONAL CONTEXT", out)
        self.assertIn("1. Check GDPR.", out)

    def test_multi_doc_preserves_order(self):
        docs = [
            Doc(role="researcher", text="first"),
            Doc(role="researcher", text="second"),
            Doc(role="any", text="third"),
        ]
        out = _additional_context_block(docs)
        # Numbering preserves list order.
        first_idx = out.index("1. first")
        second_idx = out.index("2. second")
        third_idx = out.index("3. third")
        self.assertLess(first_idx, second_idx)
        self.assertLess(second_idx, third_idx)

    def test_strips_per_doc_whitespace(self):
        doc = Doc(role="researcher", text="  padded  \n")
        out = _additional_context_block([doc])
        self.assertIn("1. padded", out)
        # No stray leading/trailing whitespace in the rendered line.
        self.assertNotIn("padded  \n", out)


# ============================================================== #
# Message builders — v1 byte-parity when channel absent
# ============================================================== #

class TestMessageBuildersV1Parity(unittest.TestCase):
    """Without the new kwarg, output is byte-identical to v1."""

    def test_planner_no_extra_unchanged(self):
        baseline = build_planner_messages("What is X?")
        with_kwarg = build_planner_messages(
            "What is X?", additional_context=None,
        )
        self.assertEqual(baseline, with_kwarg)
        # User content is the question, nothing more.
        self.assertEqual(baseline[1]["content"], "What is X?")

    def test_researcher_no_extra_unchanged(self):
        baseline = build_researcher_messages(
            "Q", "plan body", ["round 1 report"], [],
        )
        with_kwarg = build_researcher_messages(
            "Q", "plan body", ["round 1 report"], [],
            additional_context=[],
        )
        self.assertEqual(baseline, with_kwarg)
        # No ADDITIONAL CONTEXT block.
        self.assertNotIn("ADDITIONAL CONTEXT",
                         baseline[-1]["content"])

    def test_synthesizer_no_extra_unchanged(self):
        baseline = build_synthesizer_messages(
            "Q", "plan", ["r1"], "critique",
        )
        with_kwarg = build_synthesizer_messages(
            "Q", "plan", ["r1"], "critique",
            additional_context=None,
        )
        self.assertEqual(baseline, with_kwarg)

    def test_critic_no_extra_unchanged(self):
        baseline = build_critic_messages("Q", "p", ["r1"])
        with_kwarg = build_critic_messages(
            "Q", "p", ["r1"], additional_context=[],
        )
        self.assertEqual(baseline, with_kwarg)


# ============================================================== #
# Message builders — appended block
# ============================================================== #

class TestMessageBuildersWithExtra(unittest.TestCase):

    def test_planner_appends_to_user_message(self):
        docs = [Doc(role="planner", text="Aim for ≤ 3 plan items.")]
        msgs = build_planner_messages(
            "Decompose X.", additional_context=docs,
        )
        user_content = msgs[1]["content"]
        self.assertTrue(user_content.startswith("Decompose X."))
        self.assertIn("ADDITIONAL CONTEXT", user_content)
        self.assertIn("Aim for ≤ 3 plan items.", user_content)

    def test_researcher_appends_to_user_message(self):
        docs = [Doc(role="researcher", text="Also check shadow PRs.")]
        msgs = build_researcher_messages(
            "Q", "p", [], [], additional_context=docs,
        )
        user_content = msgs[-1]["content"]
        self.assertIn("ADDITIONAL CONTEXT", user_content)
        self.assertIn("Also check shadow PRs.", user_content)
        # USER QUESTION block stays at the head.
        self.assertTrue(user_content.startswith("USER QUESTION:"))

    def test_synthesizer_block_precedes_final_directive(self):
        # The synthesizer's user message ends with the "Now write the
        # final answer..." line. Injected context must come BEFORE
        # that line so the directive is the last thing the model
        # sees (in-context-window primacy of last-instruction).
        docs = [Doc(role="synthesizer", text="Lead with the bottom line.")]
        msgs = build_synthesizer_messages(
            "Q", "p", ["r1"], None, additional_context=docs,
        )
        user_content = msgs[-1]["content"]
        ctx_idx = user_content.index("ADDITIONAL CONTEXT")
        write_idx = user_content.index("Now write the final answer")
        self.assertLess(ctx_idx, write_idx)


# ============================================================== #
# Node-level integration with a stub chat_client
# ============================================================== #

class _Captured:
    """Container for the messages a stub chat_client sees."""
    def __init__(self):
        self.payloads: list[dict] = []


def _make_stub_chat_client(captured: _Captured, *, reply: str):
    """Returns an object exposing .chat(payload) -> response_dict.

    The stub records every payload it sees on ``captured.payloads``
    and returns a uniform reply so we can assert the node reached
    happy-path completion.
    """
    class _Stub:
        def chat(self, payload: dict, *, think: Any = True) -> dict:
            captured.payloads.append(payload)
            return {
                "choices": [{
                    "message": {"role": "assistant", "content": reply},
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }
    return _Stub()


class TestNodeIntegration(unittest.TestCase):
    """End-to-end: state.additional_context surfaces in node prompts."""

    def test_planner_node_surfaces_injected_doc(self):
        cap = _Captured()
        client = _make_stub_chat_client(cap, reply="1. step\n2. step")
        state = {
            "question": "What is X?",
            "additional_context": [
                Doc(role="planner", text="Aim for ≤ 3 plan items."),
            ],
        }
        out = planner_node(state, chat_client=client, model="m")
        self.assertEqual(len(cap.payloads), 1)
        user_msg = cap.payloads[0]["messages"][1]["content"]
        self.assertIn("ADDITIONAL CONTEXT", user_msg)
        self.assertIn("Aim for ≤ 3 plan items.", user_msg)
        # Node still returned a usable plan.
        self.assertIn("plan", out)

    def test_planner_filters_other_role_docs(self):
        cap = _Captured()
        client = _make_stub_chat_client(cap, reply="x")
        state = {
            "question": "Q",
            "additional_context": [
                Doc(role="researcher", text="researcher-only"),
                Doc(role="planner", text="planner-only"),
                Doc(role="any", text="for everyone"),
            ],
        }
        planner_node(state, chat_client=client, model="m")
        user_msg = cap.payloads[0]["messages"][1]["content"]
        self.assertIn("planner-only", user_msg)
        self.assertIn("for everyone", user_msg)
        self.assertNotIn("researcher-only", user_msg)

    def test_planner_no_extra_block_when_channel_absent(self):
        # v1 path: state has no additional_context channel at all.
        cap = _Captured()
        client = _make_stub_chat_client(cap, reply="x")
        state = {"question": "Q"}
        planner_node(state, chat_client=client, model="m")
        user_msg = cap.payloads[0]["messages"][1]["content"]
        self.assertNotIn("ADDITIONAL CONTEXT", user_msg)

    def test_synthesizer_node_surfaces_injected_doc(self):
        cap = _Captured()
        client = _make_stub_chat_client(cap, reply="Final answer.")
        state = {
            "question": "Q",
            "plan": "p",
            "research": ["r1"],
            "critique": None,
            "additional_context": [
                Doc(role="synthesizer", text="Lead with the bottom line."),
                Doc(role="any", text="Cite path:line."),
            ],
        }
        synthesizer_node(state, chat_client=client, model="m")
        user_msg = cap.payloads[0]["messages"][1]["content"]
        self.assertIn("Lead with the bottom line.", user_msg)
        self.assertIn("Cite path:line.", user_msg)


# ============================================================== #
# Reducer behavior (state_v2 surface, sanity)
# ============================================================== #

class TestReducerIdempotency(unittest.TestCase):
    """The state_v2 ``append_doc`` reducer is hash-deduped.

    Concrete behavior: re-injecting the same (role, text) twice does
    NOT produce a duplicate block entry. The renderer here would
    still happily print whatever it's given; the de-dup is the
    state-channel's responsibility.
    """

    def test_reducer_dedups_on_role_text_pair(self):
        from consultants.engine.state_v2 import append_doc
        left = [Doc(role="r", text="hello")]
        right = [Doc(role="r", text="hello")]  # retry
        merged = append_doc(left, right)
        self.assertEqual(len(merged), 1)

    def test_reducer_keeps_distinct_docs(self):
        from consultants.engine.state_v2 import append_doc
        merged = append_doc(
            [Doc(role="r", text="a")],
            [Doc(role="r", text="b")],
        )
        self.assertEqual(len(merged), 2)


if __name__ == "__main__":
    unittest.main()
