"""Tests for the degraded-answer fallback when the synthesizer fails.

When the synthesizer's primary model exhausts its retry budget on a
cloud flap (HTTP 500), the council should still surface the
researcher's findings and the critic's verdict as a usable answer
rather than ``(consultation incomplete)``. These tests exercise the
``_compose_degraded_answer`` helper directly — the engine wiring
that calls it is covered by ``test_consultants_council`` (the
synthesizer_node failure-path test).
"""

import unittest

from consultants.engine.council import _compose_degraded_answer


class ComposeDegradedAnswerTests(unittest.TestCase):
    def test_research_and_critique_both_present(self) -> None:
        state = {
            "research": ["## Analysis\nThe LR schedule is reasonable."],
            "critique": "DECISION: ready\nCoverage of the question is sufficient.",
        }
        out = _compose_degraded_answer(state, error="HTTP 500: Internal Server Error")
        # Banner present
        self.assertIn("# Degraded answer (synthesizer failed)", out)
        # Error surfaced
        self.assertIn("HTTP 500", out)
        # Both sections present
        self.assertIn("## Researcher findings", out)
        self.assertIn("## Critic verdict", out)
        # Researcher content inlined
        self.assertIn("LR schedule is reasonable", out)
        # Critic content inlined
        self.assertIn("Coverage of the question is sufficient", out)
        # Recovery hint
        self.assertIn("## How to recover", out)
        self.assertIn("claude-consultants follow-up", out)
        # Single-round path: no Round-N subheaders
        self.assertNotIn("### Round", out)

    def test_multiple_research_rounds_get_subheaders(self) -> None:
        state = {
            "research": [
                "First-round evidence about file A.",
                "Second-round evidence about file B.",
                "Third-round refinement.",
            ],
            "critique": "DECISION: ready",
        }
        out = _compose_degraded_answer(state, error="x")
        self.assertIn("### Round 1", out)
        self.assertIn("### Round 2", out)
        self.assertIn("### Round 3", out)
        # And each body lands inside
        self.assertIn("file A", out)
        self.assertIn("file B", out)
        self.assertIn("Third-round refinement", out)

    def test_research_only_no_critic(self) -> None:
        # /consultants at low effort skips the critic — degraded
        # answer must still produce useful output.
        state = {"research": ["Researcher said the bug is in main.py:42."]}
        out = _compose_degraded_answer(state, error="HTTP 500")
        self.assertIn("## Researcher findings", out)
        self.assertNotIn("## Critic verdict", out)
        self.assertIn("main.py:42", out)

    def test_critique_only_no_research(self) -> None:
        # Edge case: research empty but critique present (e.g. critic
        # ran on a pre-seeded follow-up). Still surface what we have.
        state = {"research": [], "critique": "Verdict: needs grounding."}
        out = _compose_degraded_answer(state, error="HTTP 500")
        self.assertNotIn("## Researcher findings", out)
        self.assertIn("## Critic verdict", out)
        self.assertIn("needs grounding", out)

    def test_falls_back_to_placeholder_when_both_empty(self) -> None:
        # When neither survived, no useful degraded answer to compose;
        # caller still treats the consultation as failed.
        state = {"research": [], "critique": ""}
        out = _compose_degraded_answer(state, error="HTTP 500: Internal Server Error")
        self.assertIn("consultation incomplete", out)
        self.assertIn("HTTP 500", out)
        self.assertNotIn("## Researcher findings", out)
        self.assertNotIn("## Critic verdict", out)

    def test_skips_non_string_research_entries(self) -> None:
        # Defensive: state["research"] is documented as list[str] but
        # external code mutates it occasionally; tolerate stray dicts
        # / None without crashing.
        state = {
            "research": [
                None,
                "Real text.",
                {"role": "researcher"},  # stray dict, must be skipped
                "",  # empty string skipped
                "Second real text.",
            ],
            "critique": "DECISION: ready",
        }
        out = _compose_degraded_answer(state, error="x")
        self.assertIn("Real text", out)
        self.assertIn("Second real text", out)
        # Both real strings landed under Round subheaders (2 strings -> 2 rounds)
        self.assertIn("### Round 1", out)
        self.assertIn("### Round 2", out)
        self.assertNotIn("### Round 3", out)

    def test_multiline_error_indented_in_blockquote(self) -> None:
        state = {"research": ["evidence"], "critique": "verdict"}
        err = "HTTP 500\nupstream gateway timeout\nref: 3d301833"
        out = _compose_degraded_answer(state, error=err)
        # Each error line should appear with the blockquote prefix
        for line in err.splitlines():
            self.assertIn(f"> {line}", out)


if __name__ == "__main__":
    unittest.main()
