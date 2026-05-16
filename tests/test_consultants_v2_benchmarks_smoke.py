"""Smoke tests for the Consultancy Skill-Eval Protocol harness.

Covers the M11 infrastructure that backs every skill-eval sub-protocol
(M11a stall, M11b coder, M11c tool_executor). Today only the coder
suite is shipped, so this file exercises:

1. **Suite manifest loading** — SUITE.md parses, manifest list comes
   through as a Python list, rubric numbers coerce, suite hash is
   stable across re-loads.
2. **Question loading** — 8 questions discovered, frontmatter
   parses, oracle paths resolve.
3. **Cost estimator** — produces sensible numbers + a one-line
   summary; handles models not in the coeff table.
4. **Oracle grader** — runs pytest against a sandbox with correct
   code (passes) vs buggy code (fails). Runs the full canonical
   reference submissions baked into ``coder_bench._DRY_RUN_SUBMISSIONS``
   against the real oracles to lock in the bench's "happy path".
5. **Judge LLM helper** — ``build_judge_messages`` shape +
   ``parse_judge_response`` covers fenced/bare/malformed inputs.
6. **Trial JSON round-trip** — ``append_trial`` and ``load_trials``
   round-trip a CoderTrial preserving every field.
7. **Dry-run smoke** — invoke the bench's ``run_bench`` with
   --dry-run, verify it produces 32 PASS trials end-to-end.
8. **Analyzer** — ``aggregate_by_model`` and ``recommend_default``
   apply the rubric correctly; the report renders without errors.
9. **CLI gate** — ``--live`` without ``--accept-cost`` exits 2 + a
   summary line printed.

Runs on the main ``claude-hooks`` env. No cloud calls, no
``langgraph`` import — the bench's coder_node path uses our local
sandbox factory directly so it works in either env.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

# Add repo root so ``benchmarks.consultants.*`` imports cleanly even
# when pytest is invoked from a sub-directory.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from benchmarks.consultants.harness import (  # noqa: E402
    HARNESS_VERSION, CoderTrial, append_trial, build_judge_messages,
    count_code_lines, estimate_cost, load_questions, load_suite_manifest,
    load_trials, make_dry_run_loop_runner, parse_judge_response,
    run_pytest_against_sandbox,
)
from benchmarks.consultants import coder_bench  # noqa: E402
from benchmarks.consultants.analyze import (  # noqa: E402
    aggregate_by_model, recommend_default, render_report,
)


CODER_QUESTIONS_DIR = (
    _REPO_ROOT / "benchmarks" / "consultants" / "questions" / "coder"
)


# ============================================================== #
# Suite manifest + question loading
# ============================================================== #

class TestSuiteManifest(unittest.TestCase):

    def test_loads(self):
        suite = load_suite_manifest(CODER_QUESTIONS_DIR)
        self.assertEqual(suite.suite, "coder")
        self.assertEqual(suite.suite_version, "1.0")
        self.assertEqual(suite.released, "2026-05-16")
        self.assertEqual(len(suite.manifest), 8)
        # Manifest order matches the released order (matters for
        # reproducibility — analyze.py groups by this order).
        self.assertEqual(suite.manifest[0], "trivial-01-truncate")
        self.assertEqual(suite.manifest[-1], "hard-02-digit-filter")

    def test_rubric_numeric_coercion(self):
        suite = load_suite_manifest(CODER_QUESTIONS_DIR)
        self.assertEqual(suite.rubric["pass_rate_floor"], 0.70)
        self.assertEqual(suite.rubric["quality_score_floor"], 3.5)
        self.assertEqual(suite.rubric["tie_breaker"], "median_tokens")

    def test_hash_stable_across_loads(self):
        s1 = load_suite_manifest(CODER_QUESTIONS_DIR)
        s2 = load_suite_manifest(CODER_QUESTIONS_DIR)
        self.assertEqual(s1.suite_hash, s2.suite_hash)
        # 64-char sha256
        self.assertEqual(len(s1.suite_hash), 64)


class TestLoadQuestions(unittest.TestCase):

    def test_loads_all_eight(self):
        qs = load_questions(CODER_QUESTIONS_DIR)
        self.assertEqual(len(qs), 8)
        ids = {q.id for q in qs}
        self.assertIn("trivial-01-truncate", ids)
        self.assertIn("hard-02-digit-filter", ids)

    def test_tier_filter(self):
        qs = load_questions(CODER_QUESTIONS_DIR, tier_filter={"hard"})
        self.assertEqual(len(qs), 2)
        for q in qs:
            self.assertEqual(q.tier, "hard")

    def test_id_filter(self):
        qs = load_questions(
            CODER_QUESTIONS_DIR,
            id_filter={"easy-02-fib", "medium-01-balance"},
        )
        self.assertEqual({q.id for q in qs},
                          {"easy-02-fib", "medium-01-balance"})

    def test_oracle_paths_exist(self):
        for q in load_questions(CODER_QUESTIONS_DIR):
            self.assertTrue(
                q.oracle_path.is_file(),
                f"oracle missing for {q.id}: {q.oracle_path}",
            )

    def test_suite_md_skipped(self):
        # SUITE.md is the manifest, not a question; load_questions
        # must skip it.
        qs = load_questions(CODER_QUESTIONS_DIR)
        for q in qs:
            self.assertNotEqual(q.id, "SUITE")


# ============================================================== #
# Cost estimator
# ============================================================== #

class TestCostEstimator(unittest.TestCase):

    def test_basic_estimate(self):
        qs = load_questions(CODER_QUESTIONS_DIR)
        est = estimate_cost(qs, ["kimi-k2.6:cloud", "glm-5.1:cloud"])
        self.assertEqual(est["trials"], 16)  # 2 models × 8 questions
        self.assertIn("kimi-k2.6:cloud", est["by_model"])
        self.assertGreater(est["total_tokens"], 0)
        # Summary line shape
        self.assertIn("2 models × 8 questions = 16 trials", est["summary_line"])
        self.assertIn("M tokens", est["summary_line"])

    def test_unknown_model_uses_defaults(self):
        # An unrecognized model name falls back to the conservative
        # mid-point coefficients — no crash.
        qs = load_questions(CODER_QUESTIONS_DIR)
        est = estimate_cost(qs, ["brand-new-model:cloud"])
        self.assertGreater(
            est["by_model"]["brand-new-model:cloud"]["total_tokens"], 0,
        )

    def test_judge_tokens_separate_line(self):
        qs = load_questions(CODER_QUESTIONS_DIR)
        without = estimate_cost(qs, ["kimi-k2.6:cloud"])
        with_judge = estimate_cost(
            qs, ["kimi-k2.6:cloud"], judge_model="kimi-k2.6:cloud",
        )
        self.assertGreater(
            with_judge["total_tokens"], without["total_tokens"],
        )
        self.assertGreater(with_judge["judge_tokens"], 0)


# ============================================================== #
# Oracle grader — happy path against canonical reference submissions
# ============================================================== #

class TestOracleGrader(unittest.TestCase):
    """The canonical reference submissions in
    ``coder_bench._DRY_RUN_SUBMISSIONS`` must pass every oracle.
    If one fails, either the reference is wrong OR the oracle has
    drifted — both are blocker issues."""

    def test_every_reference_submission_passes_oracle(self):
        from benchmarks.consultants.coder_bench import (
            _DRY_RUN_SUBMISSIONS,
        )
        qs = load_questions(CODER_QUESTIONS_DIR)
        # Make sure every question has a reference submission.
        for q in qs:
            self.assertIn(
                q.id, _DRY_RUN_SUBMISSIONS,
                f"missing canonical reference for {q.id}",
            )
        # And every reference passes its oracle.
        for q in qs:
            with self.subTest(qid=q.id):
                with tempfile.TemporaryDirectory() as tmp:
                    sandbox = Path(tmp)
                    (sandbox / q.sandbox_path).write_text(
                        _DRY_RUN_SUBMISSIONS[q.id],
                    )
                    result = run_pytest_against_sandbox(
                        q.oracle_path, sandbox,
                        python_executable=sys.executable,
                        timeout_s=30.0,
                    )
                    self.assertTrue(
                        result.passed,
                        f"{q.id} oracle rejected canonical "
                        f"submission. stdout:\n{result.stdout}",
                    )

    def test_buggy_code_rejected(self):
        # Verify the oracle REJECTS broken code — basic guard against
        # the oracle being trivially-true.
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = Path(tmp)
            (sandbox / "truncate.py").write_text(
                "def truncate(s, n):\n    return s\n"  # ignores n
            )
            oracle = (
                CODER_QUESTIONS_DIR / "trivial-01-truncate-oracle.py"
            )
            result = run_pytest_against_sandbox(
                oracle, sandbox,
                python_executable=sys.executable, timeout_s=30.0,
            )
            self.assertFalse(result.passed)
            self.assertNotEqual(result.returncode, 0)

    def test_missing_module_rejected(self):
        # Empty sandbox -> import fails -> tests fail.
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = Path(tmp)
            oracle = (
                CODER_QUESTIONS_DIR / "trivial-01-truncate-oracle.py"
            )
            result = run_pytest_against_sandbox(
                oracle, sandbox,
                python_executable=sys.executable, timeout_s=30.0,
            )
            self.assertFalse(result.passed)


# ============================================================== #
# Code-quality helpers
# ============================================================== #

class TestCodeLineCount(unittest.TestCase):

    def test_basic_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "x.py"
            f.write_text(
                "def foo():\n"
                "    # comment\n"
                "    pass\n"
                "\n"      # blank
                "    return 1\n"
            )
            # 3 non-comment, non-blank lines.
            self.assertEqual(count_code_lines(f), 3)

    def test_missing_file_returns_zero(self):
        self.assertEqual(count_code_lines(Path("/nonexistent.py")), 0)


# ============================================================== #
# Judge LLM helper
# ============================================================== #

class TestJudgeMessages(unittest.TestCase):

    def test_build_judge_messages(self):
        msgs = build_judge_messages(
            "Write a fizzbuzz function",
            "def fizzbuzz(): pass",
        )
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0]["role"], "system")
        self.assertEqual(msgs[1]["role"], "user")
        self.assertIn("Write a fizzbuzz", msgs[1]["content"])
        self.assertIn("def fizzbuzz", msgs[1]["content"])


class TestParseJudgeResponse(unittest.TestCase):

    def test_canonical_shape(self):
        score, rationale = parse_judge_response(
            "SCORE: 4\nCorrect and idiomatic, handles edge cases."
        )
        self.assertEqual(score, 4.0)
        self.assertIn("idiomatic", rationale)

    def test_score_lowercase(self):
        score, _ = parse_judge_response("score: 3\nSomething.")
        self.assertEqual(score, 3.0)

    def test_score_slash_5(self):
        score, _ = parse_judge_response("This is 4/5 quality work.")
        self.assertEqual(score, 4.0)

    def test_no_score_returns_none(self):
        score, _ = parse_judge_response("This is great!")
        self.assertIsNone(score)

    def test_empty_returns_none(self):
        score, rationale = parse_judge_response("")
        self.assertIsNone(score)
        self.assertEqual(rationale, "")


class TestJudgeTrialQualityRetry(unittest.TestCase):
    """Pin the retry-on-empty behaviour added in the post-mortem of
    the 2026-05-16 M11b full run (kimi judging kimi returned empty
    content on trial 29; the harness now retries once and tags the
    rationale so the failure mode is recoverable from
    ``trials.jsonl`` alone).
    """

    def _make_client(self, responses: list[str]):
        # Returns a stub whose .chat() yields successive responses
        # from the supplied list. After exhaustion, returns "".
        class _StubClient:
            def __init__(self, queue):
                self.queue = list(queue)
                self.calls = 0

            def chat(self, _payload):
                self.calls += 1
                content = self.queue.pop(0) if self.queue else ""
                return {"choices": [
                    {"message": {"role": "assistant", "content": content}},
                ]}
        return _StubClient(responses)

    def test_first_attempt_succeeds_no_retry(self):
        import tempfile
        from benchmarks.consultants import coder_bench
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = Path(tmp)
            (sandbox / "x.py").write_text("def f(): pass\n")
            client = self._make_client(["SCORE: 4\nGood code."])
            score, rationale = coder_bench._judge_trial_quality(
                judge_chat_client=client, judge_model="kimi:test",
                task="Write f", sandbox=sandbox, sandbox_path="x.py",
            )
            self.assertEqual(score, 4.0)
            self.assertIn("Good code", rationale)
            self.assertEqual(client.calls, 1)

    def test_empty_then_success_retries_once(self):
        import tempfile
        from benchmarks.consultants import coder_bench
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = Path(tmp)
            (sandbox / "x.py").write_text("def f(): pass\n")
            client = self._make_client(["", "SCORE: 5\nGreat."])
            score, rationale = coder_bench._judge_trial_quality(
                judge_chat_client=client, judge_model="kimi:test",
                task="Write f", sandbox=sandbox, sandbox_path="x.py",
            )
            self.assertEqual(score, 5.0)
            self.assertEqual(client.calls, 2)
            self.assertIn("Great", rationale)

    def test_empty_then_empty_returns_diagnostic(self):
        import tempfile
        from benchmarks.consultants import coder_bench
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = Path(tmp)
            (sandbox / "x.py").write_text("def f(): pass\n")
            client = self._make_client(["", ""])
            score, rationale = coder_bench._judge_trial_quality(
                judge_chat_client=client, judge_model="kimi:test",
                task="Write f", sandbox=sandbox, sandbox_path="x.py",
            )
            self.assertIsNone(score)
            self.assertEqual(client.calls, 2)
            self.assertIn("empty content twice", rationale)
            self.assertIn("kimi:test", rationale)

    def test_call_raises_returns_diagnostic(self):
        import tempfile
        from benchmarks.consultants import coder_bench

        class _RaisingClient:
            def chat(self, _payload):
                raise RuntimeError("HTTP 500: cloud flap")

        with tempfile.TemporaryDirectory() as tmp:
            sandbox = Path(tmp)
            (sandbox / "x.py").write_text("def f(): pass\n")
            score, rationale = coder_bench._judge_trial_quality(
                judge_chat_client=_RaisingClient(),
                judge_model="kimi:test",
                task="Write f", sandbox=sandbox, sandbox_path="x.py",
            )
            self.assertIsNone(score)
            self.assertIn("judge call raised", rationale)
            self.assertIn("HTTP 500", rationale)


# ============================================================== #
# Trial JSON round-trip
# ============================================================== #

class TestTrialRoundTrip(unittest.TestCase):

    def test_append_then_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trials.jsonl"
            t1 = CoderTrial(
                question_id="q1", tier="easy", model="m1",
                compiles=True, passes_tests=True,
                wall_s=1.5, iterations=3,
                tokens_prompt=100, tokens_completion=50,
                code_lines=10, complexity=2,
                quality_score=4.0, quality_rationale="ok",
                sandbox_dir="/tmp/x", timestamp="2026-05-16T00:00:00Z",
            )
            t2 = CoderTrial(
                question_id="q2", tier="hard", model="m1",
                error="boom",
            )
            append_trial(path, t1)
            append_trial(path, t2)
            loaded = load_trials(path)
            self.assertEqual(len(loaded), 2)
            self.assertEqual(loaded[0].question_id, "q1")
            self.assertEqual(loaded[0].quality_score, 4.0)
            self.assertEqual(loaded[1].error, "boom")


# ============================================================== #
# Dry-run loop runner signature contract
# ============================================================== #

class TestDryRunRunnerSignature(unittest.TestCase):
    """Pin the contract between the dry-run stub and the real
    ``run_loop`` it stands in for. The 2026-05-16 smoke run failed
    every coder trial because the live executor was called with 3
    positional args (``name, args, cwd``) but the sandboxed
    executor only accepted 2 — and the dry-run stub also only
    passed 2, so the bug was invisible until the live run. These
    tests pin the 3-positional shape on both sides so the dry-run
    path can no longer mask a live-path bug of the same shape.
    """

    def test_runner_passes_three_positional_to_executor(self):
        captured = []

        def fake_executor(*args, **kw):
            captured.append((args, dict(kw)))
            return "ok"

        runner = make_dry_run_loop_runner(
            file_path="x.py", content="print('hi')",
        )
        runner(
            payload={"messages": []},
            cwd="/some/cwd",
            config={}, tool_specs=[], chat_fn=lambda *_a, **_k: None,
            tool_executor=fake_executor,
        )
        self.assertEqual(len(captured), 1,
                         "expected exactly one write_file call")
        args, _kw = captured[0]
        self.assertEqual(len(args), 3,
                         f"expected 3 positional args, got {args!r}")
        self.assertEqual(args[0], "write_file")
        self.assertEqual(args[2], "/some/cwd")


# ============================================================== #
# End-to-end dry-run smoke
# ============================================================== #

class TestDryRunEndToEnd(unittest.TestCase):

    def test_full_dry_run_all_pass(self):
        # Suppress stdout for the test.
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "out"
            buf = io.StringIO()
            with redirect_stdout(buf):
                n = coder_bench.run_bench(
                    models=["kimi-k2.6:cloud", "glm-5.1:cloud"],
                    questions_dir=CODER_QUESTIONS_DIR,
                    output_dir=output_dir,
                    mode="dry-run",
                    ollama_base=None,
                    judge_model=None,
                    tier_filter=None,
                    id_filter=None,
                    pytest_python=sys.executable,
                )
            self.assertEqual(n, 16)  # 2 models × 8 questions
            # Trial file exists with 16 lines.
            trials_file = output_dir / "trials.jsonl"
            self.assertTrue(trials_file.is_file())
            trials = load_trials(trials_file)
            self.assertEqual(len(trials), 16)
            # Every trial passed (dry-run uses correct stub
            # submissions).
            for t in trials:
                self.assertTrue(t.compiles, f"{t.question_id} no compile")
                self.assertTrue(
                    t.passes_tests,
                    f"{t.question_id} oracle failed: {t.test_output[:200]}",
                )
            # metadata.json present + has provenance fields.
            md = json.loads(
                (output_dir / "metadata.json").read_text()
            )
            self.assertEqual(md["harness_version"], HARNESS_VERSION)
            self.assertEqual(md["suite_version"], "1.0")
            self.assertEqual(md["mode"], "dry-run")

    def test_smoke_filter_runs_only_trivial(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "out"
            buf = io.StringIO()
            with redirect_stdout(buf):
                n = coder_bench.run_bench(
                    models=["kimi-k2.6:cloud"],
                    questions_dir=CODER_QUESTIONS_DIR,
                    output_dir=output_dir,
                    mode="dry-run",
                    ollama_base=None,
                    judge_model=None,
                    tier_filter={"trivial"},
                    id_filter=None,
                    pytest_python=sys.executable,
                )
            self.assertEqual(n, 2)


# ============================================================== #
# Analyzer — rubric application + report render
# ============================================================== #

class TestAnalyzer(unittest.TestCase):

    def _trials(self, *,
                model_scores: dict[str, dict]) -> list[CoderTrial]:
        """Build a synthetic trial list. model_scores maps
        model -> {n_pass, n_fail, n_error, quality}."""
        out: list[CoderTrial] = []
        for model, spec in model_scores.items():
            for i in range(spec.get("n_pass", 0)):
                out.append(CoderTrial(
                    question_id=f"q{i}", tier="easy", model=model,
                    compiles=True, passes_tests=True,
                    quality_score=spec.get("quality"),
                    tokens_prompt=500, tokens_completion=200,
                    wall_s=1.0,
                ))
            for i in range(spec.get("n_fail", 0)):
                out.append(CoderTrial(
                    question_id=f"f{i}", tier="easy", model=model,
                    compiles=True, passes_tests=False,
                    tokens_prompt=500, tokens_completion=200,
                    wall_s=1.0,
                ))
            for i in range(spec.get("n_error", 0)):
                out.append(CoderTrial(
                    question_id=f"e{i}", tier="easy", model=model,
                    error="boom",
                ))
        return out

    def test_aggregate_basic(self):
        trials = self._trials(model_scores={
            "m1": {"n_pass": 7, "n_fail": 1, "quality": 4.0},
            "m2": {"n_pass": 5, "n_fail": 3, "quality": 3.0},
        })
        stats = aggregate_by_model(trials)
        self.assertAlmostEqual(stats["m1"].pass_rate, 7/8)
        self.assertEqual(stats["m1"].avg_quality, 4.0)
        self.assertAlmostEqual(stats["m2"].pass_rate, 5/8)

    def test_recommendation_picks_qualifier(self):
        trials = self._trials(model_scores={
            "m1": {"n_pass": 7, "n_fail": 1, "quality": 4.0},
            "m2": {"n_pass": 5, "n_fail": 3, "quality": 3.0},
        })
        stats = aggregate_by_model(trials)
        winner, rationale = recommend_default(
            stats,
            rubric={"pass_rate_floor": 0.70,
                    "quality_score_floor": 3.5,
                    "tie_breaker": "median_tokens"},
        )
        self.assertEqual(winner, "m1")
        self.assertIn("m1", rationale)

    def test_recommendation_no_qualifier(self):
        trials = self._trials(model_scores={
            "m1": {"n_pass": 4, "n_fail": 4, "quality": 4.0},
            "m2": {"n_pass": 7, "n_fail": 1, "quality": 2.0},
        })
        stats = aggregate_by_model(trials)
        winner, rationale = recommend_default(
            stats,
            rubric={"pass_rate_floor": 0.70,
                    "quality_score_floor": 3.5,
                    "tie_breaker": "median_tokens"},
        )
        # m2 has pass_rate 87% but quality 2.0 < 3.5 — disqualified.
        # m1 has quality 4.0 but pass_rate 50% < 70% — disqualified.
        self.assertIsNone(winner)
        self.assertIn("no model meets the rubric", rationale)

    def test_tie_breaker_prefers_lower_tokens(self):
        # Both models pass equally; the cheaper one wins.
        t1 = self._trials(model_scores={
            "expensive": {"n_pass": 8, "quality": 4.0},
            "cheap": {"n_pass": 8, "quality": 4.0},
        })
        # Bump expensive's tokens up.
        for t in t1:
            if t.model == "expensive":
                t.tokens_prompt = 2000
                t.tokens_completion = 800
        stats = aggregate_by_model(t1)
        winner, _ = recommend_default(
            stats,
            rubric={"pass_rate_floor": 0.70,
                    "quality_score_floor": 3.5,
                    "tie_breaker": "median_tokens"},
        )
        self.assertEqual(winner, "cheap")

    def test_report_renders_without_errors(self):
        trials = self._trials(model_scores={
            "m1": {"n_pass": 7, "n_fail": 1, "quality": 4.0},
        })
        md = render_report(trials, metadata={
            "suite": "coder", "suite_version": "1.0",
            "suite_hash": "abcdef0123456789",
            "harness_version": HARNESS_VERSION,
            "rubric": {"pass_rate_floor": 0.70,
                       "quality_score_floor": 3.5,
                       "tie_breaker": "median_tokens"},
        })
        self.assertIn("Skill-Eval Report", md)
        self.assertIn("Per-model summary", md)
        self.assertIn("`m1`", md)


# ============================================================== #
# CLI gate — --live without --accept-cost prints estimate and exits 2
# ============================================================== #

class TestCliGate(unittest.TestCase):

    def test_live_without_accept_cost_exits_2(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = coder_bench.main([
                "--live",
                "--models", "kimi-k2.6:cloud",
            ])
        self.assertEqual(rc, 2)
        out = buf.getvalue()
        self.assertIn("--accept-cost", out)
        self.assertIn("Cost summary", out)


if __name__ == "__main__":
    unittest.main()
