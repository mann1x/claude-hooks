"""M3 — adversary role: the opt-in post-synthesis refuter.

A SINGLETON node that runs once after the synthesizer
(synthesizer → adversary → END) when ``cfg.roles.adversary.enabled``.
No per-lane Send → Phase 9/10 x-tier fanout upstream is untouched.
Default OFF → synthesizer → END (cohort-2 parity lives in
test_consultants_v2_parity.py; the topology assertion is duplicated
here for locality).

Cohorts:
- ``parse_adversary_refutation`` pure parser (none / issues / header
  variants / no-header).
- ``adversary_node`` unit (stub chat): issues → annotate + decision;
  none → answer unchanged; empty / failed synthesis → skipped;
  LLM error → non-fatal decision=error; strictness threads into the
  prompt.
- ``plan_topology`` adversary tail (off vs on).
- Real-langgraph E2E: enabled fires once + annotates; off → no
  adversary turn; composes under an x-tier (researcher extras) run.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _parity_helpers import (  # noqa: E402
    StableChat, build_default_deps, make_default_stubs,
)

from consultants.engine import council  # noqa: E402
from consultants.engine.graph import plan_topology  # noqa: E402

try:
    import langgraph  # noqa: F401
    HAVE_LANGGRAPH = True
except ImportError:
    HAVE_LANGGRAPH = False


# ----------------------- pure parser ----------------------------- #

class TestParseRefutation:
    def test_none(self):
        for raw in ("REFUTATION: none", "REFUTATION:none",
                    "REFUTATION: None — the answer holds", "REFUTATION:\n"):
            decision, body = council.parse_adversary_refutation(raw)
            assert decision == "none", raw
            assert body == ""

    def test_issues_block(self):
        decision, body = council.parse_adversary_refutation(
            "REFUTATION:\n- claim A is unsupported\n- claim B overclaims")
        assert decision == "issues"
        assert "claim A" in body and "claim B" in body

    def test_equals_variant(self):
        decision, body = council.parse_adversary_refutation(
            "REFUTATION = the cite at foo.py:9 is fabricated")
        assert decision == "issues"
        assert "fabricated" in body

    def test_no_header_nontrivial_is_issues(self):
        decision, body = council.parse_adversary_refutation(
            "The claim about X has no evidence backing it.")
        assert decision == "issues"
        assert body

    def test_no_header_empty_is_none(self):
        assert council.parse_adversary_refutation("") == ("none", "")
        assert council.parse_adversary_refutation("   \n ") == ("none", "")


# ----------------------- node unit ------------------------------- #

def _state(final_answer="Rayleigh scattering. CONFIDENCE stripped.",
           **over):
    st = {
        "question": "why is the sky blue?",
        "plan": "1. measure",
        "research": ["finding one", "finding two"],
        "final_answer": final_answer,
    }
    st.update(over)
    return st


class TestAdversaryNode:
    def test_issues_annotates_and_records(self):
        stub = StableChat("REFUTATION:\n- the 'always' is overstated")
        out = council.adversary_node(
            _state(), chat_client=stub, model="m", strictness="normal")
        assert out["adversary_decision"] == "issues"
        assert out["final_answer_refutation"].startswith("- the 'always'")
        assert "Adversarial review" in out["final_answer"]
        # original answer preserved ahead of the annotation
        assert out["final_answer"].startswith("Rayleigh scattering.")
        assert len(out["turns"]) == 1
        assert out["turns"][0].role == "adversary"

    def test_none_leaves_answer_unchanged(self):
        stub = StableChat("REFUTATION: none")
        out = council.adversary_node(
            _state(), chat_client=stub, model="m")
        assert out["adversary_decision"] == "none"
        assert "final_answer" not in out          # answer untouched
        assert "final_answer_refutation" not in out

    def test_empty_answer_is_skipped(self):
        stub = StableChat("REFUTATION: none")
        out = council.adversary_node(
            _state(final_answer=""), chat_client=stub, model="m")
        assert out == {}
        assert stub.calls == []                   # node never called the LLM

    def test_failed_synthesis_is_skipped(self):
        stub = StableChat("REFUTATION: none")
        out = council.adversary_node(
            _state(_role_failed="synthesizer"), chat_client=stub, model="m")
        assert out == {}
        assert stub.calls == []

    def test_llm_error_is_non_fatal(self):
        class _Boom:
            calls: list = []

            def chat(self, payload, *, think=True):
                raise RuntimeError("model down")

        out = council.adversary_node(
            _state(), chat_client=_Boom(), model="m")
        assert out["adversary_decision"] == "error"
        # answer is NOT mutated on adversary failure
        assert "final_answer" not in out
        assert len(out["turns"]) == 1

    @pytest.mark.parametrize("strictness,needle", [
        ("soft", "STRICTNESS: soft"),
        ("normal", "STRICTNESS: normal"),
        ("strict", "STRICTNESS: strict"),
    ])
    def test_strictness_threads_into_prompt(self, strictness, needle):
        stub = StableChat("REFUTATION: none")
        council.adversary_node(
            _state(), chat_client=stub, model="m", strictness=strictness)
        user_msg = stub.calls[0]["messages"][-1]["content"]
        assert needle in user_msg
        assert "FINAL ANSWER TO REFUTE" in user_msg


# ----------------------- topology -------------------------------- #

class TestTopology:
    def test_default_tail_is_synthesizer_end(self):
        edges = plan_topology(("planner", "researcher", "critic",
                               "synthesizer"))
        assert ("synthesizer", "END") in edges
        assert ("synthesizer", "adversary") not in edges

    def test_adversary_tail(self):
        edges = plan_topology(("planner", "researcher", "critic",
                               "synthesizer", "adversary"))
        assert ("synthesizer", "adversary") in edges
        assert ("adversary", "END") in edges
        assert ("synthesizer", "END") not in edges


# ----------------------- graph E2E (langgraph) ------------------- #

@unittest.skipUnless(HAVE_LANGGRAPH, "langgraph not installed")
class TestAdversaryGraphE2E(unittest.TestCase):
    def _build(self, *, enabled, stubs=None):
        from consultants.engine.graph import build_council_graph
        deps = build_default_deps(enabled_roles=enabled,
                                  stubs=stubs or make_default_stubs())
        return build_council_graph(deps)

    def _init(self):
        return council.initial_state(
            question="q?", cwd="/tmp", models={"synthesizer": "m"},
            topology="council", effort="high")

    def test_enabled_runs_once_and_annotates(self):
        stubs = make_default_stubs()
        stubs["adversary"] = StableChat(
            "REFUTATION:\n- claim X is unsupported by the evidence")
        g = self._build(
            enabled=("planner", "researcher", "critic", "synthesizer",
                     "adversary"),
            stubs=stubs)
        out = g.invoke(self._init(),
                       config={"configurable": {"thread_id": "t-iss"}})
        self.assertEqual(out.get("adversary_decision"), "issues")
        self.assertIn("Adversarial review", out.get("final_answer", ""))
        adv = [t for t in out.get("turns", [])
               if getattr(t, "role", "") == "adversary"]
        self.assertEqual(len(adv), 1)

    def test_disabled_no_adversary_turn(self):
        g = self._build(enabled=("planner", "researcher", "critic",
                                 "synthesizer"))
        out = g.invoke(self._init(),
                       config={"configurable": {"thread_id": "t-off"}})
        self.assertIsNone(out.get("adversary_decision"))
        self.assertNotIn("Adversarial review", out.get("final_answer", ""))
        self.assertFalse([t for t in out.get("turns", [])
                          if getattr(t, "role", "") == "adversary"])

    def test_composes_under_xtier_fanout(self):
        # Researcher extras → Phase 9 fanout. The adversary is a
        # post-barrier singleton, so it still runs exactly once and the
        # fanned researcher lanes are untouched.
        stubs = make_default_stubs()
        stubs["adversary"] = StableChat("REFUTATION:\n- overstated")
        deps = build_default_deps(
            enabled_roles=("planner", "researcher", "critic",
                           "synthesizer", "adversary"),
            stubs=stubs,
            extra_models_by_role={"researcher": ["extra-1:cloud",
                                                 "extra-2:cloud"]})
        from consultants.engine.graph import build_council_graph
        g = build_council_graph(deps)
        out = g.invoke(self._init(),
                       config={"configurable": {"thread_id": "t-xtier"}})
        adv = [t for t in out.get("turns", [])
               if getattr(t, "role", "") == "adversary"]
        self.assertEqual(len(adv), 1)              # singleton under fanout
        self.assertEqual(out.get("adversary_decision"), "issues")


if __name__ == "__main__":
    unittest.main()
