"""M0 — latest_confidence: synthesizer + critic now emit a real
self-confidence signal into ``state['confidence']`` so the
fully-plumbed-but-dead channel actually fires (interrupt_on_low_confidence,
xauto escalation, the M2 adversary-checkpoint gate).

Covers:
- ``_extract_confidence`` pure parsing: trailing ``CONFIDENCE: <x>`` line is
  parsed, clamped to [0,1], and STRIPPED from the text; absent/malformed → None.
- ``synthesizer_node`` emits the rating into ``confidence`` and the rating
  never leaks into ``final_answer``.
- ``critic_node`` (single path) emits + strips; decision parsing unaffected.
- Integration: a low score makes ``should_interrupt_on_low_confidence`` fire;
  an empty series stays silent (no stall).
"""

from __future__ import annotations

import pytest

from consultants.engine import council
from consultants.engine import interrupt_policy
from consultants.engine.state_v2 import latest_confidence


# ----------------------- pure parser ----------------------------- #

class TestExtractConfidence:
    def test_parses_and_strips_trailing_line(self):
        text, score = council._extract_confidence("The answer.\nCONFIDENCE: 0.82")
        assert score == pytest.approx(0.82)
        assert text == "The answer."
        assert "CONFIDENCE" not in text

    def test_absent_returns_none_and_unchanged(self):
        text, score = council._extract_confidence("Just an answer, no rating.")
        assert score is None
        assert text == "Just an answer, no rating."

    def test_clamps_out_of_range(self):
        # >1 clamps to 1.0 only when it parses; the regex only admits
        # 0..1-shaped numbers, so an out-of-shape value is ignored.
        _, score = council._extract_confidence("x\nCONFIDENCE: 1.0")
        assert score == 1.0
        _, none_score = council._extract_confidence("x\nCONFIDENCE: 7")
        assert none_score is None  # 7 is not 0..1-shaped → not a rating

    def test_last_match_wins(self):
        text, score = council._extract_confidence(
            "CONFIDENCE: 0.10\nbody\nCONFIDENCE: 0.90"
        )
        assert score == pytest.approx(0.90)
        # only the trailing line is stripped
        assert "CONFIDENCE: 0.10" in text

    def test_tolerates_decorations(self):
        # bullet / blockquote / equals variants the model might emit
        for raw in ("- CONFIDENCE: 0.5", "> CONFIDENCE = 0.5", "CONFIDENCE:0.5"):
            _, score = council._extract_confidence("ans\n" + raw)
            assert score == pytest.approx(0.5), raw

    def test_empty_text(self):
        assert council._extract_confidence("") == ("", None)


# ----------------------- stub chat client ------------------------ #

class _StubChatClient:
    def __init__(self, reply: str):
        self.reply = reply
        self.calls: list[dict] = []

    def chat(self, payload: dict) -> dict:
        self.calls.append(payload)
        return {
            "choices": [{"message": {"content": self.reply},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 30, "completion_tokens": 12},
        }


def _state(effort: str = "high") -> dict:
    st = council.initial_state(
        question="why is the sky blue?", cwd="/nonexistent-root",
        models={"synthesizer": "m:cloud", "critic": "m:cloud"},
        topology="council", effort=effort,
    )
    st["plan"] = "1. measure"
    st["research"] = ["finding one", "finding two"]
    st["research_rounds_used"] = 1
    st["critique"] = "DECISION: ready\nevidence is fine."
    return st


# ----------------------- synthesizer ----------------------------- #

class TestSynthesizerConfidence:
    def test_emits_and_strips(self):
        # No path:line tokens → citation lint is a no-op even on a
        # nonexistent root.
        client = _StubChatClient(
            "Rayleigh scattering makes the sky appear blue.\nCONFIDENCE: 0.66"
        )
        out = council.synthesizer_node(_state(), chat_client=client, model="m:cloud")
        assert out["confidence"] == [pytest.approx(0.66)]
        assert "CONFIDENCE" not in out["final_answer"]
        assert out["final_answer"].endswith("blue.")

    def test_no_rating_no_confidence_key(self):
        client = _StubChatClient("A plain answer with no self-rating line.")
        out = council.synthesizer_node(_state(), chat_client=client, model="m:cloud")
        assert "confidence" not in out
        assert out["final_answer"] == "A plain answer with no self-rating line."


# ----------------------- critic ---------------------------------- #

class TestCriticConfidence:
    def test_single_path_emits_and_strips(self):
        client = _StubChatClient(
            "DECISION: ready\nThe evidence covers it.\nCONFIDENCE: 0.91"
        )
        out = council.critic_node(_state(), chat_client=client, model="m:cloud")
        assert out["critic_decision"] == "ready"
        assert out["confidence"] == [pytest.approx(0.91)]
        assert "CONFIDENCE" not in out["critique"]


# ----------------------- integration with interrupt -------------- #

class TestLowConfidenceInterrupt:
    def test_low_score_fires_when_enabled(self):
        st = _state()
        st["confidence"] = [0.30]
        st["runtime_control"] = dict(st.get("runtime_control") or {})
        st["runtime_control"]["interrupt_on_low_confidence"] = True
        st["runtime_control"]["confidence_target"] = 0.70
        assert latest_confidence(st) == pytest.approx(0.30)
        decision = interrupt_policy.should_interrupt_on_low_confidence(st)
        assert decision is not None

    def test_empty_series_stays_silent(self):
        st = _state()
        st["runtime_control"] = dict(st.get("runtime_control") or {})
        st["runtime_control"]["interrupt_on_low_confidence"] = True
        assert latest_confidence(st) is None
        assert interrupt_policy.should_interrupt_on_low_confidence(st) is None
