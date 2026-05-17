"""Tests for the M11c tool_executor-bench additions to
``benchmarks/consultants/harness.py``.

Covers five areas:

1. ``ToolExecTrial`` schema — required-field positionals,
   defaults, mutable-default independence, JSON round-trip.
2. ``BenchQuestion.fixtures_subdir`` — new field surfaces on
   load_questions reading frontmatter; defaults to "" for
   suites that don't use it (coder, stall).
3. ``estimate_tool_exec_cost`` — shape parity with
   :func:`estimate_cost` / :func:`estimate_stall_cost`,
   per-model breakdown, judge accounting, zero-models guard.
4. ``run_pytest_against_sandbox(extra_env=...)`` — the new env
   forwarding path exposes ``TOOL_EXEC_*`` vars to the oracle
   without disturbing the existing ``CODER_SANDBOX`` contract.
5. Oracle dispatch end-to-end — drive one of the real M11c
   oracles against a fixture cohort and confirm
   :class:`OracleResult` reports the run; this catches breakage
   between the bench wiring and the oracle contract earlier
   than a full bench run would.

No cloud calls; everything runs against the in-repo fixtures and
tmpdirs.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import fields as dc_fields
from pathlib import Path

from benchmarks.consultants.harness import (
    BenchQuestion,
    OracleResult,
    ToolExecTrial,
    estimate_tool_exec_cost,
    load_questions,
    load_suite_manifest,
    run_pytest_against_sandbox,
)


# ====================================================================== #
# Real-suite path: the in-repo M11c suite under
# benchmarks/consultants/questions/tool_executor/
# ====================================================================== #

_REPO_ROOT = Path(__file__).resolve().parents[1]
_TOOL_EXEC_SUITE_DIR = (_REPO_ROOT / "benchmarks" / "consultants"
                        / "questions" / "tool_executor")


# ====================================================================== #
# ToolExecTrial schema
# ====================================================================== #

class TestToolExecTrialSchema(unittest.TestCase):

    def test_required_positionals_only(self) -> None:
        """The four bookkeeping fields are enough; everything else
        defaults to 0 / "" / [] / {}."""
        t = ToolExecTrial(
            question_id="q1", tier="trivial",
            model="gemma4:31b-cloud", trial_idx=0,
        )
        self.assertEqual(t.question_id, "q1")
        self.assertEqual(t.tier, "trivial")
        self.assertEqual(t.model, "gemma4:31b-cloud")
        self.assertEqual(t.trial_idx, 0)
        # Outcome defaults
        self.assertEqual(t.completed, False)
        self.assertEqual(t.passes_tests, False)
        self.assertEqual(t.test_output, "")
        self.assertEqual(t.test_results, {})
        # Cost defaults
        self.assertEqual(t.wall_s, 0.0)
        self.assertEqual(t.inference_s, 0.0)
        self.assertEqual(t.iterations, 0)
        self.assertEqual(t.tokens_prompt, 0)
        self.assertEqual(t.tokens_completion, 0)
        # Tool-call mechanics defaults
        self.assertEqual(t.tool_calls_count, 0)
        self.assertEqual(t.tool_calls_unique, 0)
        self.assertEqual(t.tool_call_log, [])
        self.assertEqual(t.citation_count, 0)
        self.assertEqual(t.final_text, "")
        # Quality defaults
        self.assertIsNone(t.quality_score)
        self.assertEqual(t.quality_rationale, "")
        # Bookkeeping defaults
        self.assertIsNone(t.error)

    def test_to_dict_round_trips_through_json(self) -> None:
        """Every populated field must survive json.dumps / loads
        without TypeError. JSONL persistence depends on this."""
        t = ToolExecTrial(
            question_id="easy-01-grep-read-chain",
            tier="easy",
            model="kimi-k2.6:cloud",
            trial_idx=2,
            timestamp="2026-05-17T18:00:00Z",
            suite_hash="7921555c1f23ab98",
            completed=True,
            passes_tests=True,
            test_output="6 passed",
            test_results={"test_basic": {"status": "passed", "msg": ""}},
            wall_s=12.5,
            inference_s=10.1,
            iterations=3,
            tokens_prompt=4200,
            tokens_completion=900,
            tool_calls_count=4,
            tool_calls_unique=3,
            tool_call_log=[
                {"tool": "grep", "args": "{}", "result_excerpt": "..."},
                {"tool": "read_file", "args": "{}", "result_excerpt": "..."},
            ],
            citation_count=2,
            final_text="See auth.py:33-50 for handle_auth.",
            final_text_len=33,
            quality_score=4.5,
            quality_rationale="cites correct refs",
            quality_judge_model="gemma4:31b-cloud",
        )
        s = json.dumps(t.to_dict())
        rt = json.loads(s)
        self.assertEqual(rt["question_id"], "easy-01-grep-read-chain")
        self.assertEqual(rt["tool_calls_count"], 4)
        self.assertEqual(rt["quality_score"], 4.5)
        self.assertEqual(len(rt["tool_call_log"]), 2)
        # ToolExecTrial(**rt) reconstructs identically — sanity check
        # the round-trip is loss-less.
        rebuilt = ToolExecTrial(**rt)
        self.assertEqual(rebuilt.tool_calls_count, 4)
        self.assertEqual(rebuilt.quality_score, 4.5)

    def test_mutable_defaults_are_independent(self) -> None:
        """field(default_factory=...) regression check — two trials
        must not share their tool_call_log / test_results lists."""
        a = ToolExecTrial(
            question_id="a", tier="trivial", model="m", trial_idx=0,
        )
        b = ToolExecTrial(
            question_id="b", tier="trivial", model="m", trial_idx=0,
        )
        a.tool_call_log.append({"tool": "grep"})
        a.test_results["test_x"] = {"status": "passed"}
        self.assertEqual(b.tool_call_log, [])
        self.assertEqual(b.test_results, {})


# ====================================================================== #
# BenchQuestion.fixtures_subdir surface
# ====================================================================== #

class TestBenchQuestionFixturesSubdir(unittest.TestCase):

    def test_field_exists_with_empty_default(self) -> None:
        names = {f.name: f for f in dc_fields(BenchQuestion)}
        self.assertIn("fixtures_subdir", names)
        self.assertEqual(names["fixtures_subdir"].default, "")

    def test_load_questions_reads_fixtures_subdir(self) -> None:
        """Frontmatter ``fixtures_subdir:`` lands on the
        :class:`BenchQuestion` instance."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            # Materialize a question with fixtures_subdir + a
            # matching empty oracle so require_oracle=True is happy.
            (tdp / "demo.md").write_text(
                "---\n"
                "id: demo\n"
                "tier: trivial\n"
                "source: synthetic\n"
                "task: do thing\n"
                "oracle: demo-oracle.py\n"
                "fixtures_subdir: my_cohort\n"
                "---\n"
                "body\n",
                encoding="utf-8",
            )
            (tdp / "demo-oracle.py").write_text(
                "def test_noop(): assert True\n",
                encoding="utf-8",
            )
            qs = load_questions(tdp, require_oracle=True)
            self.assertEqual(len(qs), 1)
            self.assertEqual(qs[0].fixtures_subdir, "my_cohort")

    def test_load_questions_defaults_fixtures_subdir_empty(self) -> None:
        """A coder/stall question (no fixtures_subdir frontmatter)
        still loads, with ``fixtures_subdir == ""``."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            (tdp / "coder.md").write_text(
                "---\n"
                "id: coder\n"
                "tier: trivial\n"
                "source: humaneval-style\n"
                "task: write a fn\n"
                "sandbox_path: fn.py\n"
                "oracle: coder-oracle.py\n"
                "---\n"
                "body\n",
                encoding="utf-8",
            )
            (tdp / "coder-oracle.py").write_text(
                "def test_noop(): assert True\n",
                encoding="utf-8",
            )
            qs = load_questions(tdp, require_oracle=True)
            self.assertEqual(len(qs), 1)
            self.assertEqual(qs[0].fixtures_subdir, "")
            # back-compat: sandbox_path still surfaces too.
            self.assertEqual(qs[0].sandbox_path, "fn.py")

    def test_real_suite_loads_with_eight_fixture_subdirs(self) -> None:
        """The in-repo M11c suite has 8 questions, each pointing
        at its fixture cohort under fixtures/. This is the
        regression that flags accidental SUITE.md drift."""
        if not _TOOL_EXEC_SUITE_DIR.is_dir():
            self.skipTest("tool_executor suite directory not present")
        qs = load_questions(_TOOL_EXEC_SUITE_DIR, require_oracle=True)
        self.assertEqual(len(qs), 8, f"expected 8 questions, got {len(qs)}")
        # Every question names a non-empty fixtures_subdir.
        for q in qs:
            self.assertTrue(
                q.fixtures_subdir,
                f"question {q.id} has no fixtures_subdir",
            )
            # And the directory it names actually exists on disk.
            sub = _TOOL_EXEC_SUITE_DIR / "fixtures" / q.fixtures_subdir
            self.assertTrue(
                sub.is_dir(),
                f"fixtures_subdir '{q.fixtures_subdir}' for "
                f"question {q.id} is not on disk",
            )

    def test_real_suite_manifest_matches_loaded(self) -> None:
        """The SUITE.md manifest list and loaded ids must agree.
        Drift here means someone added a question without bumping
        the manifest (or vice versa)."""
        if not _TOOL_EXEC_SUITE_DIR.is_dir():
            self.skipTest("tool_executor suite directory not present")
        qs = load_questions(_TOOL_EXEC_SUITE_DIR, require_oracle=True)
        manifest = load_suite_manifest(_TOOL_EXEC_SUITE_DIR)
        loaded_ids = {q.id for q in qs}
        manifest_ids = set(manifest.manifest)
        self.assertEqual(loaded_ids, manifest_ids)
        # Suite hash is deterministic (sha256 = 64 hex chars).
        self.assertEqual(len(manifest.suite_hash), 64)
        self.assertEqual(manifest.suite, "tool_executor")
        self.assertEqual(manifest.suite_version, "1.0")


# ====================================================================== #
# estimate_tool_exec_cost shape
# ====================================================================== #

class TestEstimateToolExecCost(unittest.TestCase):

    def _make_q(self, qid: str) -> BenchQuestion:
        return BenchQuestion(
            id=qid, tier="trivial",
            source="synthetic", task="t", sandbox_path="",
            oracle_path=Path("/tmp"),
        )

    def test_default_trial_count(self) -> None:
        qs = [self._make_q(f"q{i}") for i in range(8)]
        models = ["gemma4:31b-cloud", "kimi-k2.6:cloud"]
        est = estimate_tool_exec_cost(qs, models)
        # 2 models × 8 questions × 1 trial.
        self.assertEqual(est["trials"], 16)
        self.assertIn("gemma4:31b-cloud", est["by_model"])
        self.assertIn("kimi-k2.6:cloud", est["by_model"])
        # Sanity-check the summary line shape.
        self.assertIn("2 models", est["summary_line"])
        self.assertIn("8 questions", est["summary_line"])
        self.assertIn("16 trials", est["summary_line"])
        # No judge requested → judge_tokens stays 0.
        self.assertEqual(est["judge_tokens"], 0)

    def test_judge_model_adds_tokens(self) -> None:
        qs = [self._make_q(f"q{i}") for i in range(8)]
        est_no_judge = estimate_tool_exec_cost(
            qs, ["gemma4:31b-cloud"],
        )
        est_with_judge = estimate_tool_exec_cost(
            qs, ["gemma4:31b-cloud"],
            judge_model="gemma4:31b-cloud",
        )
        self.assertGreater(
            est_with_judge["total_tokens"],
            est_no_judge["total_tokens"],
        )
        self.assertGreater(est_with_judge["judge_tokens"], 0)
        # Judge model surfaced.
        self.assertEqual(
            est_with_judge["judge_model"], "gemma4:31b-cloud",
        )
        self.assertIn("judge", est_with_judge["summary_line"])

    def test_per_model_breakdown(self) -> None:
        qs = [self._make_q("q1"), self._make_q("q2")]
        est = estimate_tool_exec_cost(qs, ["glm-5.1:cloud"])
        m = est["by_model"]["glm-5.1:cloud"]
        # Required keys present on each model row.
        for key in ("trials", "iterations", "prompt_tokens",
                    "completion_tokens", "total_tokens"):
            self.assertIn(key, m)
        # 2 questions × 1 trial.
        self.assertEqual(m["trials"], 2)
        # 3 iterations per trial × 2 trials = 6.
        self.assertEqual(m["iterations"], 6)
        # prompt + completion sums to total.
        self.assertEqual(
            m["prompt_tokens"] + m["completion_tokens"],
            m["total_tokens"],
        )

    def test_unknown_model_uses_default_coeffs(self) -> None:
        qs = [self._make_q("q1")]
        est = estimate_tool_exec_cost(qs, ["who-knows:v0"])
        self.assertIn("who-knows:v0", est["by_model"])
        self.assertGreater(
            est["by_model"]["who-knows:v0"]["total_tokens"], 0,
        )

    def test_trials_per_question_scales_linearly(self) -> None:
        qs = [self._make_q("q1"), self._make_q("q2")]
        models = ["glm-5.1:cloud"]
        est1 = estimate_tool_exec_cost(qs, models, trials_per_question=1)
        est3 = estimate_tool_exec_cost(qs, models, trials_per_question=3)
        # 3× the trials, 3× the tokens.
        self.assertEqual(est3["trials"], 3 * est1["trials"])
        self.assertEqual(
            est3["by_model"]["glm-5.1:cloud"]["total_tokens"],
            3 * est1["by_model"]["glm-5.1:cloud"]["total_tokens"],
        )

    def test_zero_models_zero_trials(self) -> None:
        est = estimate_tool_exec_cost([self._make_q("q1")], [])
        self.assertEqual(est["trials"], 0)
        self.assertEqual(est["total_tokens"], 0)


# ====================================================================== #
# run_pytest_against_sandbox(extra_env=...) plumbing
# ====================================================================== #

class TestRunPytestExtraEnv(unittest.TestCase):

    def test_extra_env_reaches_oracle(self) -> None:
        """A tiny synthetic oracle reads ``TOOL_EXEC_OUTPUT`` from
        the env. The bench will set this on every trial; verify
        the forwarding actually works."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            oracle = tdp / "oracle.py"
            oracle.write_text(
                "import os\n"
                "def test_env_visible():\n"
                "    assert os.environ['TOOL_EXEC_OUTPUT'] == 'hello'\n",
                encoding="utf-8",
            )
            r = run_pytest_against_sandbox(
                oracle_path=oracle,
                sandbox_dir=tdp,
                extra_env={"TOOL_EXEC_OUTPUT": "hello"},
                timeout_s=30.0,
            )
            self.assertIsInstance(r, OracleResult)
            self.assertTrue(
                r.passed,
                f"oracle saw wrong env: stdout={r.stdout[:400]}",
            )

    def test_extra_env_none_preserves_v101_behavior(self) -> None:
        """When extra_env is None / omitted, only CODER_SANDBOX is
        added. A second oracle confirms TOOL_EXEC_OUTPUT is NOT
        present (drift would mean we leaked test state)."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            oracle = tdp / "oracle.py"
            oracle.write_text(
                "import os\n"
                "def test_no_tool_exec_var_when_omitted():\n"
                "    assert 'TOOL_EXEC_OUTPUT' not in os.environ\n",
                encoding="utf-8",
            )
            r = run_pytest_against_sandbox(
                oracle_path=oracle,
                sandbox_dir=tdp,
                timeout_s=30.0,
            )
            self.assertTrue(
                r.passed,
                f"omitting extra_env should leave TOOL_EXEC_* unset; "
                f"stdout={r.stdout[:400]}",
            )

    def test_extra_env_overrides_inherited(self) -> None:
        """If the operator's shell happens to have a stale
        ``TOOL_EXEC_OUTPUT`` set, the bench's per-trial value wins.
        This isn't paranoia — bench operators have hit it before
        on local reruns."""
        import os
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            oracle = tdp / "oracle.py"
            oracle.write_text(
                "import os\n"
                "def test_explicit_wins():\n"
                "    assert os.environ['TOOL_EXEC_OUTPUT'] == 'fresh'\n",
                encoding="utf-8",
            )
            # Pollute the inherited environment.
            prior = os.environ.get("TOOL_EXEC_OUTPUT")
            os.environ["TOOL_EXEC_OUTPUT"] = "stale"
            try:
                r = run_pytest_against_sandbox(
                    oracle_path=oracle,
                    sandbox_dir=tdp,
                    extra_env={"TOOL_EXEC_OUTPUT": "fresh"},
                    timeout_s=30.0,
                )
            finally:
                if prior is None:
                    os.environ.pop("TOOL_EXEC_OUTPUT", None)
                else:
                    os.environ["TOOL_EXEC_OUTPUT"] = prior
            self.assertTrue(
                r.passed,
                f"explicit extra_env should override inherited; "
                f"stdout={r.stdout[:400]}",
            )


# ====================================================================== #
# Oracle dispatch end-to-end against a real M11c fixture cohort
# ====================================================================== #

class TestOracleDispatchAgainstRealFixture(unittest.TestCase):
    """Drives one of the real M11c oracles against its fixture
    with a synthetic happy-path answer and confirms the oracle
    pytest module passes. This is the integration smoke between
    the harness env plumbing and the oracle contract — it would
    flag a contract drift earlier than waiting for the live bench.
    """

    def setUp(self) -> None:
        if not _TOOL_EXEC_SUITE_DIR.is_dir():
            self.skipTest("tool_executor suite directory not present")

    def test_trivial_01_happy_path_oracle_passes(self) -> None:
        """A synthetic well-formed answer should make the oracle
        pass on every assertion. If this regresses, either the
        oracle got stricter without a SUITE.md PATCH bump OR the
        fixture line numbers shifted."""
        suite = _TOOL_EXEC_SUITE_DIR
        oracle = suite / "trivial-01-find-symbol-oracle.py"
        fixture_dir = suite / "fixtures" / "simple_constants"

        # Resolve the actual line where DEFAULT_TIMEOUT_S is
        # declared in the fixture so a future re-indent doesn't
        # break this test for the wrong reason.
        import re
        src = (fixture_dir / "config.py").read_text(encoding="utf-8")
        live_line = None
        for idx, raw in enumerate(src.splitlines(), start=1):
            if re.match(r"^DEFAULT_TIMEOUT_S\s*[:=]", raw.strip()):
                live_line = idx
                break
        self.assertIsNotNone(live_line, "fixture drift: const not found")

        output = (
            f"The constant `DEFAULT_TIMEOUT_S` is set to 30.0 at "
            f"config.py:{live_line}."
        )
        calls = [
            {"tool": "grep", "args": "{}",
             "result_excerpt": "DEFAULT_TIMEOUT_S: float = 30.0"},
            {"tool": "read_file", "args": "{}",
             "result_excerpt": "DEFAULT_TIMEOUT_S: float = 30.0"},
        ]

        with tempfile.TemporaryDirectory() as td:
            r = run_pytest_against_sandbox(
                oracle_path=oracle,
                sandbox_dir=Path(td),
                timeout_s=30.0,
                extra_env={
                    "TOOL_EXEC_OUTPUT": output,
                    "TOOL_EXEC_CALLS": json.dumps(calls),
                    "TOOL_EXEC_FIXTURE_DIR": str(fixture_dir),
                },
            )
        self.assertTrue(
            r.passed,
            f"happy-path oracle should pass; rc={r.returncode}; "
            f"stdout={r.stdout[:600]}; stderr={r.stderr[:200]}",
        )
        # Per-test results parsed back from junit XML; at least
        # one passed test should be visible.
        self.assertTrue(
            any(d.get("status") == "passed"
                for d in r.test_results.values()),
            f"expected at least one 'passed' test; got: {r.test_results}",
        )

    def test_trivial_01_empty_answer_oracle_fails(self) -> None:
        """The negative path: an empty answer must drive the oracle
        to fail. Confirms the assertions actually bite — a "passes
        on garbage" oracle would silently inflate every model's
        score."""
        suite = _TOOL_EXEC_SUITE_DIR
        oracle = suite / "trivial-01-find-symbol-oracle.py"
        fixture_dir = suite / "fixtures" / "simple_constants"

        with tempfile.TemporaryDirectory() as td:
            r = run_pytest_against_sandbox(
                oracle_path=oracle,
                sandbox_dir=Path(td),
                timeout_s=30.0,
                extra_env={
                    "TOOL_EXEC_OUTPUT": "",
                    "TOOL_EXEC_CALLS": "[]",
                    "TOOL_EXEC_FIXTURE_DIR": str(fixture_dir),
                },
            )
        self.assertFalse(
            r.passed,
            "empty answer should NOT pass the oracle — assertions "
            "must bite",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
