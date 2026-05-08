"""Tests for the synthesizer fallback-model chain.

When the primary synthesizer model exhausts its retry budget on a
persistent cloud flap (HTTP 500), the engine walks
``fallback_models`` in order before declaring the consultation
failed. Same chat_client, same prior_messages — only the ``model``
field of the payload changes per attempt. First success wins.
"""

import unittest
from unittest.mock import MagicMock

from consultants.engine.council import synthesizer_node


class _FakeChat:
    """Records the model-per-call sequence and lets a test script
    success / failure per call. ``responses`` is a list of either
    Exception (raises) or dict (returns)."""

    def __init__(self, responses: list) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def chat(self, payload: dict) -> dict:
        self.calls.append({"model": payload["model"]})
        if not self.responses:
            raise AssertionError("FakeChat ran out of scripted responses")
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def _ok_response(text: str) -> dict:
    return {
        "message": {"role": "assistant", "content": text},
        "prompt_eval_count": 100,
        "eval_count": 50,
    }


class SynthesizerFallbackTests(unittest.TestCase):
    def _state(self) -> dict:
        return {
            "question": "Q",
            "plan": "P",
            "research": ["R1"],
            "critique": "ready",
        }

    def test_primary_succeeds_no_fallback_used(self) -> None:
        chat = _FakeChat([_ok_response("PRIMARY ANSWER")])
        out = synthesizer_node(
            self._state(),
            chat_client=chat,
            model="primary-model:cloud",
            think=False,
            fallback_models=["fallback-a:cloud", "fallback-b:cloud"],
        )
        self.assertNotIn("error", out)
        # Exactly one call was made, with the primary model
        self.assertEqual(len(chat.calls), 1)
        self.assertEqual(chat.calls[0]["model"], "primary-model:cloud")
        # Final answer is the primary's text
        self.assertEqual(out["final_answer"], "PRIMARY ANSWER")

    def test_primary_fails_first_fallback_succeeds(self) -> None:
        chat = _FakeChat([
            RuntimeError("ollama chat HTTP 500: Internal Server Error"),
            _ok_response("FALLBACK A ANSWER"),
        ])
        out = synthesizer_node(
            self._state(),
            chat_client=chat,
            model="primary-model:cloud",
            think=False,
            fallback_models=["fallback-a:cloud", "fallback-b:cloud"],
        )
        self.assertNotIn("error", out)
        # Two calls, primary then fallback-a
        self.assertEqual(len(chat.calls), 2)
        self.assertEqual(chat.calls[0]["model"], "primary-model:cloud")
        self.assertEqual(chat.calls[1]["model"], "fallback-a:cloud")
        self.assertEqual(out["final_answer"], "FALLBACK A ANSWER")

    def test_walks_through_chain_until_success(self) -> None:
        chat = _FakeChat([
            RuntimeError("HTTP 500"),
            RuntimeError("HTTP 502"),
            _ok_response("FALLBACK B ANSWER"),
        ])
        out = synthesizer_node(
            self._state(),
            chat_client=chat,
            model="primary:cloud",
            think=False,
            fallback_models=["fallback-a:cloud", "fallback-b:cloud"],
        )
        self.assertNotIn("error", out)
        self.assertEqual(len(chat.calls), 3)
        self.assertEqual(
            [c["model"] for c in chat.calls],
            ["primary:cloud", "fallback-a:cloud", "fallback-b:cloud"],
        )
        self.assertEqual(out["final_answer"], "FALLBACK B ANSWER")

    def test_all_models_fail_falls_through_to_degraded(self) -> None:
        chat = _FakeChat([
            RuntimeError("HTTP 500: primary down"),
            RuntimeError("HTTP 500: fallback-a down"),
            RuntimeError("HTTP 500: fallback-b down"),
        ])
        out = synthesizer_node(
            self._state(),
            chat_client=chat,
            model="primary:cloud",
            think=False,
            fallback_models=["fallback-a:cloud", "fallback-b:cloud"],
        )
        # Failure surfaced
        self.assertIn("error", out)
        self.assertEqual(out["_role_failed"], "synthesizer")
        # Last error is the one preserved
        self.assertIn("fallback-b down", out["error"])
        # Degraded answer composed from research + critique
        self.assertIn("# Degraded answer (synthesizer failed)", out["final_answer"])
        self.assertIn("R1", out["final_answer"])
        self.assertIn("ready", out["final_answer"])
        # All three models tried
        self.assertEqual(
            [c["model"] for c in chat.calls],
            ["primary:cloud", "fallback-a:cloud", "fallback-b:cloud"],
        )

    def test_no_fallback_list_behaves_like_v1_0(self) -> None:
        """Backward-compat: fallback_models=None / [] should give exactly
        the pre-2026-05-07 behavior — single attempt, fail to degraded
        on Exception."""
        chat = _FakeChat([RuntimeError("HTTP 500")])
        out = synthesizer_node(
            self._state(),
            chat_client=chat,
            model="primary:cloud",
            think=False,
            fallback_models=None,
        )
        self.assertEqual(len(chat.calls), 1)
        self.assertIn("error", out)
        self.assertEqual(out["_role_failed"], "synthesizer")
        self.assertIn("# Degraded answer", out["final_answer"])

    def test_empty_fallback_list_same_as_none(self) -> None:
        chat = _FakeChat([RuntimeError("HTTP 500")])
        out = synthesizer_node(
            self._state(),
            chat_client=chat,
            model="primary:cloud",
            think=False,
            fallback_models=[],
        )
        self.assertEqual(len(chat.calls), 1)
        self.assertIn("# Degraded answer", out["final_answer"])

    def test_fallback_models_recorded_with_actual_model_used(self) -> None:
        """Each attempt fires a recorder.record_llm with the model
        actually used — so a post-hoc audit can see which fallback
        produced the final answer."""
        recorder = MagicMock()
        chat = _FakeChat([
            RuntimeError("HTTP 500"),
            _ok_response("FALLBACK ANSWER"),
        ])
        synthesizer_node(
            self._state(),
            chat_client=chat,
            model="primary:cloud",
            think=False,
            fallback_models=["fallback-a:cloud"],
            recorder=recorder,
        )
        # record_llm called twice — once for the failed primary, once
        # for the successful fallback. ``model`` arg differs per call.
        record_calls = [
            c for c in recorder.record_llm.call_args_list
        ]
        self.assertEqual(len(record_calls), 2)
        self.assertEqual(record_calls[0].kwargs["model"], "primary:cloud")
        self.assertEqual(record_calls[1].kwargs["model"], "fallback-a:cloud")
        # First call recorded an error, second didn't
        self.assertIsNotNone(record_calls[0].kwargs.get("error"))
        self.assertIsNone(record_calls[1].kwargs.get("error"))


if __name__ == "__main__":
    unittest.main()
