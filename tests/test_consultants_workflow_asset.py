"""M6 — the committed Workflow driver asset + its SKILL link.

The Q2 deliverable is a committed Workflow script that drives the
council programmatically. These guards keep the asset from silently
rotting: the file must exist, declare the canonical ``meta.name``, map
the three ``verify_budget`` tiers to a skeptic-panel size, and stay
linked from the SKILL (so the doc and the script don't drift apart).

Structural only — JS syntax is validated separately with node; here we
assert the load-bearing invariants a Python test can see without a JS
runtime.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WF = REPO / ".claude" / "workflows" / "consult-with-adversarial-review.mjs"
SKILL = REPO / ".claude" / "skills" / "consultants" / "SKILL.md"

WF_NAME = "consult-with-adversarial-review"


class TestWorkflowAsset(unittest.TestCase):
    def setUp(self):
        self.assertTrue(WF.is_file(), f"missing workflow script: {WF}")
        self.src = WF.read_text(encoding="utf-8")

    def test_starts_with_meta_export(self):
        # The Workflow harness requires the script to begin with the
        # meta export.
        self.assertTrue(
            self.src.lstrip().startswith("export const meta"),
            "workflow must begin with `export const meta`")

    def test_meta_name_matches_filename(self):
        m = re.search(r"name:\s*'([^']+)'", self.src)
        self.assertIsNotNone(m, "meta.name not found")
        self.assertEqual(m.group(1), WF_NAME)

    def test_verify_budget_panel_map(self):
        # Discipline #4: the skeptic panel is sized by verify_budget.
        # All three tiers must appear in the panel map.
        for tier, size in (("minimal", "2"), ("bounded", "3"),
                           ("generous", "5")):
            self.assertRegex(
                self.src, rf"{tier}:\s*{size}",
                f"verify_budget tier {tier}->{size} missing from panel map")

    def test_threads_cwd_discipline(self):
        # Discipline #1 — the --cwd rule must be present.
        self.assertIn("--cwd", self.src)
        self.assertIn("CWD_RULE", self.src)

    def test_detects_followup_cap_on_json(self):
        # Discipline #2 — the cap is detected on the JSON ok field.
        self.assertIn("followup_limit_reached", self.src)
        self.assertIn("ok === false", self.src)

    def test_uses_wait_for_completion_gate(self):
        # Discipline #3 — --wait is the completion gate.
        self.assertIn("--wait", self.src)

    def test_compose_challenge_present(self):
        # Q1-meets-Q2: surviving refutations -> focused follow-up.
        self.assertIn("function composeChallenge", self.src)

    def test_uses_parallel_skeptic_panel(self):
        self.assertIn("parallel(", self.src)


class TestSkillLink(unittest.TestCase):
    def test_skill_references_the_committed_script(self):
        self.assertTrue(SKILL.is_file())
        skill = SKILL.read_text(encoding="utf-8")
        self.assertIn(
            ".claude/workflows/consult-with-adversarial-review.mjs", skill,
            "SKILL.md must link the committed workflow script")

    def test_skill_has_workflow_section(self):
        skill = SKILL.read_text(encoding="utf-8")
        self.assertIn("Driving the council from a Workflow", skill)
        self.assertIn("Four mandatory disciplines", skill)


if __name__ == "__main__":
    unittest.main()
