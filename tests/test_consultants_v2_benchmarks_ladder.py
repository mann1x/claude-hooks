"""Tests for the comparative-ladder builders + the offline ``rejudge``
tool (gemini second-judge re-score + ladder).

No cloud calls: the judge client is stubbed. Covers:

1. ``parse_ladder_response`` — clean total order, ties, missing /
   unknown / duplicated labels, the no-``RANKING:``-keyword fallback,
   decorated labels (``A (best)``), prose (must be *invalid*, not a
   bogus parse), and empty input.
2. ``assign_ladder_labels`` — deterministic per question_id, de-dups
   models, raises past 26 candidates.
3. ``build_ladder_system`` / ``build_ladder_messages`` — shape +
   every solution rendered with its fenced code.
4. ``rejudge`` offline smoke — ``cmd_rescore`` writes
   ``quality_secondary_*`` without mutating the kimi fields and renders
   a report; ``cmd_ladder`` ranks ≥2 models per question, translates
   labels back to models, and renders a report. Judge client stubbed;
   a tiny persisted sandbox is built in a temp dir.

Runs on the main ``claude-hooks`` env. No ``langgraph`` import.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from benchmarks.consultants.harness import (  # noqa: E402
    LadderRanking,
    assign_ladder_labels,
    build_ladder_messages,
    build_ladder_system,
    load_questions,
    parse_ladder_response,
)
import benchmarks.consultants.rejudge as rejudge  # noqa: E402

_EASY_DIR = _REPO_ROOT / "benchmarks" / "consultants" / "questions" / "coder_easy"


# ============================================================== #
# parse_ladder_response
# ============================================================== #

class TestParseLadderResponse(unittest.TestCase):
    def test_clean_total_order_with_tie(self):
        r = parse_ladder_response(
            "RANKING: C>A=D>B\nC is cleanest; B leaks memory.",
            ["A", "B", "C", "D"],
        )
        self.assertTrue(r.valid)
        self.assertEqual(r.order, [["C"], ["A", "D"], ["B"]])
        # Fractional ranks: C=1, A&D average of positions 2,3 = 2.5, B=4.
        self.assertEqual(r.ranks, {"C": 1.0, "A": 2.5, "D": 2.5, "B": 4.0})
        self.assertIn("cleanest", r.rationale)

    def test_rank_sum_invariant_under_ties(self):
        # The fractional-rank sum must equal 1+2+...+N regardless of
        # how the judge groups ties (keeps mean-rank aggregation fair).
        r = parse_ladder_response("RANKING: A=B=C", ["A", "B", "C"])
        self.assertEqual(set(r.ranks.values()), {2.0})  # all tie at avg(1,2,3)
        self.assertAlmostEqual(sum(r.ranks.values()), 6.0)

    def test_missing_and_unknown(self):
        r = parse_ladder_response("RANKING: A>Z", ["A", "B"])
        self.assertFalse(r.valid)
        self.assertEqual(r.missing, ["B"])
        self.assertEqual(r.unknown, ["Z"])

    def test_duplicate_label(self):
        r = parse_ladder_response("RANKING: A>A>B", ["A", "B"])
        self.assertFalse(r.valid)
        self.assertEqual(r.duplicated, ["A"])

    def test_decorated_labels(self):
        r = parse_ladder_response(
            "RANKING: A (best) > B=C > D.", ["A", "B", "C", "D"],
        )
        self.assertTrue(r.valid)
        self.assertEqual(r.order, [["A"], ["B", "C"], ["D"]])

    def test_fallback_no_keyword(self):
        # Structured expression on its own line, no RANKING: keyword.
        r = parse_ladder_response("B>A", ["A", "B"])
        self.assertTrue(r.valid)
        self.assertEqual(r.order, [["B"], ["A"]])

    def test_prose_is_invalid_not_bogus(self):
        # Free prose must NOT yield a partial ranking (don't grab the
        # first letter of "I think").
        r = parse_ladder_response(
            "I think B>A overall.\nbecause B is more robust", ["A", "B"],
        )
        self.assertFalse(r.valid)
        self.assertEqual(set(r.missing), {"A", "B"})

    def test_empty(self):
        r = parse_ladder_response("", ["A", "B"])
        self.assertFalse(r.valid)
        self.assertEqual(set(r.missing), {"A", "B"})
        r2 = parse_ladder_response("no ranking here at all", ["A", "B"])
        self.assertFalse(r2.valid)

    def test_returns_ladderranking(self):
        self.assertIsInstance(
            parse_ladder_response("RANKING: A>B", ["A", "B"]), LadderRanking,
        )


# ============================================================== #
# assign_ladder_labels
# ============================================================== #

class TestAssignLadderLabels(unittest.TestCase):
    def test_deterministic_per_question(self):
        a = assign_ladder_labels("c-med-01", ["kimi", "glm", "nemo"])
        b = assign_ladder_labels("c-med-01", ["kimi", "glm", "nemo"])
        self.assertEqual(a, b)

    def test_labels_are_sequential_letters(self):
        out = assign_ladder_labels("q", ["m1", "m2", "m3"])
        self.assertEqual([lab for lab, _ in out], ["A", "B", "C"])
        self.assertEqual({m for _, m in out}, {"m1", "m2", "m3"})

    def test_shuffle_uncorrelated_with_input_order(self):
        # At least one question id must permute the input order (else
        # the shuffle is a no-op). Try a handful of ids.
        models = ["a", "b", "c", "d", "e"]
        permuted = False
        for qid in ("q1", "q2", "q3", "q4", "q5"):
            order = [m for _, m in assign_ladder_labels(qid, models)]
            if order != models:
                permuted = True
                break
        self.assertTrue(permuted)

    def test_dedups_models(self):
        out = assign_ladder_labels("q", ["m1", "m1", "m2"])
        self.assertEqual(len(out), 2)

    def test_overflow_raises(self):
        with self.assertRaises(ValueError):
            assign_ladder_labels("q", [f"m{i}" for i in range(27)])


# ============================================================== #
# ladder prompt builders
# ============================================================== #

class TestLadderPromptBuilders(unittest.TestCase):
    def test_system_mentions_ranking_and_language(self):
        sysmsg = build_ladder_system("Rust")
        self.assertIn("RANKING", sysmsg)
        self.assertIn("Rust", sysmsg)
        self.assertIn("=", sysmsg)  # ties documented

    def test_messages_render_each_solution(self):
        msgs = build_ladder_messages(
            "do a thing",
            [("A", "fn main(){}"), ("B", "int main(){}")],
            "C", "c",
        )
        self.assertEqual(msgs[0]["role"], "system")
        self.assertEqual(msgs[1]["role"], "user")
        user = msgs[1]["content"]
        self.assertIn("SOLUTION A", user)
        self.assertIn("SOLUTION B", user)
        self.assertIn("```c", user)
        self.assertIn("do a thing", user)


# ============================================================== #
# rejudge offline smoke (stubbed judge)
# ============================================================== #

class _StubJudge:
    """Canned judge: emits a RANKING reply when shown the ladder
    system prompt, a SCORE reply otherwise. Records calls."""

    def __init__(self, score_line="SCORE: 4\nclean and idiomatic",
                 ranking_line="RANKING: A>B\nA edges B on clarity"):
        self.score_line = score_line
        self.ranking_line = ranking_line
        self.calls = []

    def chat(self, payload):
        self.calls.append(payload)
        system = ""
        for m in payload.get("messages", []):
            if m.get("role") == "system":
                system = m.get("content", "")
                break
        body = self.ranking_line if "RANKING" in system else self.score_line
        return {"choices": [{"message": {"content": body}}]}


def _real_qids():
    """First python-easy and c-easy question ids from the real suite."""
    qs = load_questions(_EASY_DIR, require_oracle=False)
    py = next(q.id for q in qs if q.id.split("-", 1)[0] == "python")
    c = next(q.id for q in qs if q.id.split("-", 1)[0] == "c")
    return py, c


def _write_sandbox(root: Path, name: str, filename: str, code: str) -> Path:
    """Build a persisted sandbox mirroring coder_bench's layout and
    return the sandbox_dir."""
    sandbox = root / name
    produced = sandbox / ".claude-hooks" / "consultants" / "bench" / "coder-out"
    produced.mkdir(parents=True, exist_ok=True)
    (produced / filename).write_text(code, encoding="utf-8")
    return sandbox


def _args(**kw):
    base = dict(
        rescore=False, ladder=False, trials="", questions_dir="",
        judge_model="stub:cloud", ollama_base="http://x", num_predict=800,
        timeout_s=30.0, lang="", limit=0, output="", report="", concurrency=1,
        report_only=False, retry_unparseable=False,
    )
    base.update(kw)
    return argparse.Namespace(**base)


class TestRejudgeOfflineSmoke(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.py_qid, self.c_qid = _real_qids()
        # Two models, one trial each on the python qid; build sandboxes.
        self.sb1 = _write_sandbox(self.tmp / "sb", "t1", "solution.py", "print(sum(map(int,input().split())))\n")
        self.sb2 = _write_sandbox(self.tmp / "sb", "t2", "solution.py", "import sys\nprint(eval('+'.join(sys.stdin.read().split())))\n")
        rows = [
            {"question_id": self.py_qid, "tier": "easy", "model": "alpha:cloud",
             "passes_algorithm": True, "passes_tests": True, "quality_score": 3.0,
             "sandbox_dir": str(self.sb1),
             "files_written": [{"path": "solution.py", "bytes": 10}]},
            {"question_id": self.py_qid, "tier": "easy", "model": "beta:cloud",
             "passes_algorithm": True, "passes_tests": True, "quality_score": 5.0,
             "sandbox_dir": str(self.sb2),
             "files_written": [{"path": "solution.py", "bytes": 20}]},
        ]
        self.trials = self.tmp / "trials.jsonl"
        with open(self.trials, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        self._stub = _StubJudge()
        self._orig_make = rejudge._make_judge
        rejudge._make_judge = lambda *a, **k: self._stub

    def tearDown(self):
        rejudge._make_judge = self._orig_make
        self._tmp.cleanup()

    def test_rescore_writes_secondary_without_mutating_primary(self):
        out = self.tmp / "trials-rejudged.jsonl"
        report = self.tmp / "rescore-report.md"
        rc = rejudge.cmd_rescore(_args(
            rescore=True, trials=str(self.trials),
            questions_dir=str(_EASY_DIR), lang="python",
            output=str(out), report=str(report),
        ))
        self.assertEqual(rc, 0)
        rows = [json.loads(l) for l in out.read_text().splitlines() if l.strip()]
        self.assertEqual(len(rows), 2)
        for r in rows:
            # Primary preserved.
            self.assertIn(r["quality_score"], (3.0, 5.0))
            # Secondary added from the stub (SCORE: 4).
            self.assertEqual(r["quality_secondary_score"], 4.0)
            self.assertEqual(r["quality_secondary_judge_model"], "stub:cloud")
        self.assertTrue(report.is_file())
        self.assertIn("Cross-judge", report.read_text())

    def test_rescore_num_predict_injected(self):
        out = self.tmp / "o.jsonl"
        rejudge.cmd_rescore(_args(
            rescore=True, trials=str(self.trials),
            questions_dir=str(_EASY_DIR), lang="python",
            num_predict=777, output=str(out), report=str(self.tmp / "r.md"),
        ))
        # The stub recorded the payloads; the judge must have been told
        # to budget 777 tokens (gemini reasoning-model guard).
        self.assertTrue(self._stub.calls)
        self.assertEqual(
            self._stub.calls[0]["options"]["num_predict"], 777,
        )

    def test_rescore_skips_when_source_missing(self):
        # A trial whose sandbox has no produced file -> secondary None.
        bad = self.tmp / "bad.jsonl"
        with open(bad, "w", encoding="utf-8") as f:
            f.write(json.dumps({
                "question_id": self.py_qid, "tier": "easy",
                "model": "gamma:cloud", "quality_score": 2.0,
                "sandbox_dir": str(self.tmp / "does-not-exist"),
                "files_written": [{"path": "solution.py"}],
            }) + "\n")
        out = self.tmp / "bad-rej.jsonl"
        rejudge.cmd_rescore(_args(
            rescore=True, trials=str(bad), questions_dir=str(_EASY_DIR),
            lang="python", output=str(out), report=str(self.tmp / "br.md"),
        ))
        row = json.loads(out.read_text().splitlines()[0])
        self.assertIsNone(row["quality_secondary_score"])
        self.assertIn("skipped", row["quality_secondary_rationale"])

    def test_ladder_ranks_models_and_renders(self):
        out = self.tmp / "ladder.jsonl"
        report = self.tmp / "ladder-report.md"
        rc = rejudge.cmd_ladder(_args(
            ladder=True, trials=str(self.trials),
            questions_dir=str(_EASY_DIR), lang="python",
            output=str(out), report=str(report),
        ))
        self.assertEqual(rc, 0)
        recs = [json.loads(l) for l in out.read_text().splitlines() if l.strip()]
        self.assertEqual(len(recs), 1)
        rec = recs[0]
        self.assertTrue(rec["valid"])
        # Both models got a fractional rank; the winner (rank 1.0) and
        # loser (2.0) are the two models, whichever way the shuffle
        # assigned A/B.
        self.assertEqual(set(rec["model_ranks"]), {"alpha:cloud", "beta:cloud"})
        self.assertEqual(set(rec["model_ranks"].values()), {1.0, 2.0})
        self.assertTrue(report.is_file())
        self.assertIn("Comparative ladder", report.read_text())

    def test_rescore_concurrency_matches_sequential(self):
        # The thread-pool path (concurrency>1) must produce the same
        # secondary scores as the sequential path.
        out = self.tmp / "conc.jsonl"
        rc = rejudge.cmd_rescore(_args(
            rescore=True, trials=str(self.trials),
            questions_dir=str(_EASY_DIR), lang="python", concurrency=4,
            output=str(out), report=str(self.tmp / "cr.md"),
        ))
        self.assertEqual(rc, 0)
        rows = [json.loads(l) for l in out.read_text().splitlines() if l.strip()]
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["quality_secondary_score"] == 4.0 for r in rows))

    def test_rescore_unparseable_categorized_not_counted(self):
        # A judge reply with no parseable SCORE line -> score None, and
        # the report must categorize it as "unparseable", excluded from
        # the numeric count (not silently counted as scored).
        self._stub.score_line = "the code looks fine to me"  # no SCORE:
        out = self.tmp / "unp.jsonl"
        report = self.tmp / "unp-report.md"
        rejudge.cmd_rescore(_args(
            rescore=True, trials=str(self.trials),
            questions_dir=str(_EASY_DIR), lang="python",
            output=str(out), report=str(report),
        ))
        rows = [json.loads(l) for l in out.read_text().splitlines() if l.strip()]
        self.assertTrue(all(r["quality_secondary_score"] is None for r in rows))
        text = report.read_text()
        self.assertIn("numeric score: 0", text)
        self.assertIn("unparseable judge reply (no SCORE line): 2", text)

    def test_report_only_rerenders_without_judging(self):
        # First produce a rejudged file, then re-render from it with a
        # stub that would RAISE if called — proving no judging happens.
        out = self.tmp / "ro.jsonl"
        rejudge.cmd_rescore(_args(
            rescore=True, trials=str(self.trials),
            questions_dir=str(_EASY_DIR), lang="python",
            output=str(out), report=str(self.tmp / "ro1.md"),
        ))

        def _boom(*a, **k):
            raise AssertionError("judge must not be called in --report-only")

        orig = rejudge._make_judge
        rejudge._make_judge = _boom
        try:
            report2 = self.tmp / "ro2.md"
            rc = rejudge.cmd_rescore(_args(
                rescore=True, report_only=True, trials=str(out),
                lang="python", report=str(report2),
            ))
            self.assertEqual(rc, 0)
            self.assertIn("numeric score: 2", report2.read_text())
        finally:
            rejudge._make_judge = orig

    def test_rescore_retry_unparseable_recovers_only_failed(self):
        # First pass: stub returns no SCORE -> both rows unparseable.
        self._stub.score_line = "no verdict here"
        out = self.tmp / "retry.jsonl"
        rejudge.cmd_rescore(_args(
            rescore=True, trials=str(self.trials),
            questions_dir=str(_EASY_DIR), lang="python",
            output=str(out), report=str(self.tmp / "r1.md"),
        ))
        rows = [json.loads(l) for l in out.read_text().splitlines() if l.strip()]
        self.assertTrue(all(r["quality_secondary_score"] is None for r in rows))
        # Retry pass on the rejudged file: stub now returns a real score.
        self._stub.score_line = "SCORE: 5\nrecovered"
        rejudge.cmd_rescore(_args(
            rescore=True, retry_unparseable=True, trials=str(out),
            questions_dir=str(_EASY_DIR), lang="python",
            output=str(out), report=str(self.tmp / "r2.md"),
        ))
        rows2 = [json.loads(l) for l in out.read_text().splitlines() if l.strip()]
        self.assertTrue(all(r["quality_secondary_score"] == 5.0 for r in rows2))

    def test_rescore_retry_keeps_existing_numeric(self):
        # Rows that already have a numeric score must NOT be re-judged.
        out = self.tmp / "keep.jsonl"
        rejudge.cmd_rescore(_args(
            rescore=True, trials=str(self.trials),
            questions_dir=str(_EASY_DIR), lang="python",
            output=str(out), report=str(self.tmp / "k1.md"),
        ))  # stub SCORE: 4 -> both numeric
        # Retry with a stub that would RAISE if called.
        def _boom(*a, **k):
            raise AssertionError("must not re-judge already-scored rows")
        orig = rejudge._make_judge
        rejudge._make_judge = lambda *a, **k: type("X", (), {"chat": _boom})()
        try:
            rejudge.cmd_rescore(_args(
                rescore=True, retry_unparseable=True, trials=str(out),
                questions_dir=str(_EASY_DIR), lang="python",
                output=str(out), report=str(self.tmp / "k2.md"),
            ))
        finally:
            rejudge._make_judge = orig
        rows = [json.loads(l) for l in out.read_text().splitlines() if l.strip()]
        self.assertTrue(all(r["quality_secondary_score"] == 4.0 for r in rows))

    def test_ladder_retry_recovers_invalid_and_merges(self):
        # First pass: stub returns no RANKING -> the one question is invalid.
        self._stub.ranking_line = "no ranking emitted"
        lout = self.tmp / "lad.jsonl"
        rejudge.cmd_ladder(_args(
            ladder=True, trials=str(self.trials),
            questions_dir=str(_EASY_DIR), lang="python",
            output=str(lout), report=str(self.tmp / "l1.md"),
        ))
        recs = [json.loads(l) for l in lout.read_text().splitlines() if l.strip()]
        self.assertEqual(len(recs), 1)
        self.assertFalse(recs[0]["valid"])
        # Retry: stub now returns a clean RANKING.
        self._stub.ranking_line = "RANKING: A>B\nA edges B"
        rejudge.cmd_ladder(_args(
            ladder=True, retry_unparseable=True, trials=str(self.trials),
            questions_dir=str(_EASY_DIR), lang="python",
            output=str(lout), report=str(self.tmp / "l2.md"),
        ))
        recs2 = [json.loads(l) for l in lout.read_text().splitlines() if l.strip()]
        self.assertEqual(len(recs2), 1)  # merged, not duplicated
        self.assertTrue(recs2[0]["valid"])
        self.assertEqual(set(recs2[0]["model_ranks"].values()), {1.0, 2.0})

    def test_ladder_skips_single_model_questions(self):
        # Only one model on a question -> no ladder call (need >=2).
        solo = self.tmp / "solo.jsonl"
        with open(solo, "w", encoding="utf-8") as f:
            f.write(json.dumps({
                "question_id": self.py_qid, "tier": "easy",
                "model": "only:cloud", "quality_score": 4.0,
                "sandbox_dir": str(self.sb1),
                "files_written": [{"path": "solution.py"}],
            }) + "\n")
        out = self.tmp / "solo-ladder.jsonl"
        rejudge.cmd_ladder(_args(
            ladder=True, trials=str(solo), questions_dir=str(_EASY_DIR),
            lang="python", output=str(out), report=str(self.tmp / "sl.md"),
        ))
        recs = [l for l in out.read_text().splitlines() if l.strip()]
        self.assertEqual(len(recs), 0)


if __name__ == "__main__":
    unittest.main()
