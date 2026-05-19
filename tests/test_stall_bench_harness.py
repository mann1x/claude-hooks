"""Tests for the M11a stall-bench additions to
``benchmarks/consultants/harness.py``.

Covers three areas:

1. ``StallTrial`` schema — round-trips through JSON, default
   values match the spec.
2. ``load_questions(..., require_oracle=False)`` — the stall
   bench's no-oracle path loads questions without warning about
   the missing oracle file. The coder default (require_oracle=True)
   still works.
3. ``estimate_stall_cost`` — produces a sane summary line and
   per-model breakdown for both tiers.

No cloud calls; everything runs against fixture files in a
tmpdir.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from benchmarks.consultants.harness import (
    BenchQuestion,
    StallTrial,
    estimate_stall_cost,
    load_questions,
    load_suite_manifest,
)


# ====================================================================== #
# Fixture: a minimal stall suite on disk
# ====================================================================== #

STALL_SUITE_MD = """\
---
suite: stall
suite_version: "1.0"
released: 2026-05-17
manifest:
  - standalone-01-fixture
  - council-01-fixture
rubric:
  stall_margin_factor: 2.5
  hardcap_margin_factor: 3.0
---

# Stall Skill-Eval Suite (test fixture)
"""

STANDALONE_QUESTION = """\
---
id: standalone-01-fixture
tier: standalone
source: synthetic
task: |
  Explain in 3 paragraphs how a B-tree differs from a binary
  search tree.
---

Background prompt body. The bench feeds the ``task`` field plus
this body to the model.
"""

COUNCIL_QUESTION = """\
---
id: council-01-fixture
tier: council
source: synthetic
task: |
  Compare two architectures for streaming-token cancellation
  in an LLM gateway. Recommend one.
---

Council prompt body. The bench wraps this with a single-model
GraphDeps and runs the full council.
"""


def _materialize_stall_fixture(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SUITE.md").write_text(STALL_SUITE_MD, encoding="utf-8")
    (directory / "standalone-01-fixture.md").write_text(
        STANDALONE_QUESTION, encoding="utf-8",
    )
    (directory / "council-01-fixture.md").write_text(
        COUNCIL_QUESTION, encoding="utf-8",
    )


# ====================================================================== #
# Schema tests
# ====================================================================== #

class TestStallTrialSchema(unittest.TestCase):

    def test_required_fields_only(self) -> None:
        """StallTrial is constructible with just the four required
        bookkeeping fields. All metrics default to 0/empty."""
        t = StallTrial(
            question_id="q1",
            tier="standalone",
            model="m1",
            trial_idx=0,
        )
        self.assertEqual(t.question_id, "q1")
        self.assertEqual(t.tier, "standalone")
        self.assertEqual(t.error, None)
        self.assertEqual(t.wall_s, 0.0)
        self.assertEqual(t.num_chat_calls, 0)
        self.assertEqual(t.call_timings, [])
        self.assertEqual(t.council_node_set, [])

    def test_to_dict_round_trips_through_json(self) -> None:
        t = StallTrial(
            question_id="q1",
            tier="council",
            model="kimi-k2.6:cloud",
            trial_idx=2,
            timestamp="2026-05-17T12:34:56Z",
            wall_s=42.5,
            num_chat_calls=6,
            total_tokens=2400,
            time_to_first_token_p99_ms=850.0,
            inter_token_p99_ms=120.0,
            call_timings=[{"model": "kimi-k2.6:cloud",
                           "total_tokens": 400}],
            council_effort="medium",
            council_node_set=["planner", "researcher", "synthesizer"],
        )
        s = json.dumps(t.to_dict())
        round_tripped = json.loads(s)
        self.assertEqual(round_tripped["question_id"], "q1")
        self.assertEqual(round_tripped["num_chat_calls"], 6)
        self.assertEqual(round_tripped["council_node_set"],
                         ["planner", "researcher", "synthesizer"])

    def test_fields_are_independent(self) -> None:
        """Mutable defaults must not be shared between instances —
        a regression test for the field(default_factory=list) idiom.
        """
        a = StallTrial(question_id="a", tier="standalone",
                       model="m", trial_idx=0)
        b = StallTrial(question_id="b", tier="standalone",
                       model="m", trial_idx=0)
        a.call_timings.append({"test": 1})
        self.assertEqual(b.call_timings, [])


# ====================================================================== #
# Loader tests — require_oracle=False path
# ====================================================================== #

class TestLoadQuestionsNoOracle(unittest.TestCase):

    def test_loads_stall_questions_without_oracle(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            _materialize_stall_fixture(tdp)

            questions = load_questions(tdp, require_oracle=False)
            self.assertEqual(len(questions), 2)
            ids = sorted(q.id for q in questions)
            self.assertEqual(ids, ["council-01-fixture",
                                   "standalone-01-fixture"])

    def test_tier_filter_separates_standalone_vs_council(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            _materialize_stall_fixture(tdp)

            standalone = load_questions(
                tdp, require_oracle=False, tier_filter=["standalone"],
            )
            council = load_questions(
                tdp, require_oracle=False, tier_filter=["council"],
            )
            self.assertEqual(len(standalone), 1)
            self.assertEqual(standalone[0].id, "standalone-01-fixture")
            self.assertEqual(len(council), 1)
            self.assertEqual(council[0].id, "council-01-fixture")

    def test_default_require_oracle_skips_stall_questions(self) -> None:
        """The coder bench's default require_oracle=True path
        WARNS-and-skips a question without an oracle. This
        preserves backwards compat."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            _materialize_stall_fixture(tdp)

            # Default require_oracle=True — both stall questions
            # lack oracles, so all get skipped (count == 0).
            questions = load_questions(tdp)
            self.assertEqual(questions, [])

    def test_suite_manifest_loads(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            _materialize_stall_fixture(tdp)

            manifest = load_suite_manifest(tdp)
            self.assertEqual(manifest.suite, "stall")
            self.assertEqual(manifest.suite_version, "1.0")
            self.assertEqual(len(manifest.manifest), 2)
            # rubric values are coerced to float by the loader.
            self.assertEqual(manifest.rubric["stall_margin_factor"], 2.5)
            self.assertEqual(manifest.rubric["hardcap_margin_factor"], 3.0)
            # suite_hash is non-empty and deterministic.
            self.assertEqual(len(manifest.suite_hash), 64)   # sha256 hex

    def test_oracle_path_is_directory_placeholder_when_no_oracle(self) -> None:
        """In no-oracle mode the placeholder is the directory itself.
        Harmless (no read access required); the bench never opens it."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            _materialize_stall_fixture(tdp)

            questions = load_questions(tdp, require_oracle=False)
            for q in questions:
                self.assertEqual(q.oracle_path, tdp)


# ====================================================================== #
# Cost estimator tests
# ====================================================================== #

class TestEstimateStallCost(unittest.TestCase):

    def _make_q(self, qid: str) -> BenchQuestion:
        return BenchQuestion(
            id=qid, tier="standalone",
            source="test", task="t", sandbox_path="",
            oracle_path=Path("/tmp"),
        )

    def test_both_tiers_default(self) -> None:
        qs = [self._make_q(f"q{i}") for i in range(4)]
        models = ["kimi-k2.6:cloud", "glm-5.1:cloud"]
        est = estimate_stall_cost(qs, models)

        # 2 models × 4 questions × (3 tier1 + 2 tier2) = 40 trials.
        self.assertEqual(est["trials"], 40)
        self.assertIn("kimi-k2.6:cloud", est["by_model"])
        self.assertIn("glm-5.1:cloud", est["by_model"])
        self.assertEqual(est["tiers"], "tier1+tier2")
        # Summary line mentions counts.
        self.assertIn("2 models", est["summary_line"])
        self.assertIn("4 questions", est["summary_line"])

    def test_tier1_only(self) -> None:
        qs = [self._make_q(f"q{i}") for i in range(2)]
        est = estimate_stall_cost(qs, ["glm-5.1:cloud"], tier2=False)
        # 1 model × 2 questions × 3 tier1 trials = 6.
        self.assertEqual(est["trials"], 6)
        self.assertEqual(est["tiers"], "tier1")
        m = est["by_model"]["glm-5.1:cloud"]
        self.assertEqual(m["trials_tier1"], 6)
        self.assertEqual(m["trials_tier2"], 0)

    def test_tier2_only(self) -> None:
        qs = [self._make_q(f"q{i}") for i in range(2)]
        est = estimate_stall_cost(qs, ["glm-5.1:cloud"], tier1=False)
        # 1 × 2 × 2 = 4 trials, but each runs ~6 chat calls.
        self.assertEqual(est["trials"], 4)
        self.assertEqual(est["tiers"], "tier2")
        m = est["by_model"]["glm-5.1:cloud"]
        self.assertEqual(m["trials_tier1"], 0)
        self.assertEqual(m["trials_tier2"], 4)
        # ~6 chat calls per council run × 4 runs = 24 calls.
        self.assertEqual(m["chat_calls"], 24)

    def test_unknown_model_uses_default_coeffs(self) -> None:
        """An unknown model still gets a budget — no KeyError."""
        qs = [self._make_q("q1")]
        est = estimate_stall_cost(qs, ["who-knows:v0"])
        self.assertIn("who-knows:v0", est["by_model"])
        self.assertGreater(
            est["by_model"]["who-knows:v0"]["total_tokens"], 0,
        )

    def test_zero_models_zero_trials(self) -> None:
        qs = [self._make_q("q1")]
        est = estimate_stall_cost(qs, [])
        self.assertEqual(est["trials"], 0)
        self.assertEqual(est["total_tokens"], 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
