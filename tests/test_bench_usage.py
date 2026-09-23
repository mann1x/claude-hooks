"""Every LLM call a bench makes is recorded by role.

A bench that counted only the model under test reported a run as cheaper
than it was, and most so for the runs that judged the most: the judges
are usually a different, pricier model. These pin that each call site
records into the trial's ``usage`` — retries included, since a retried
call was paid for twice.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from benchmarks.consultants import coder_bench, rejudge  # noqa: E402
from benchmarks.consultants.harness import (  # noqa: E402
    CoderTrial, ToolExecTrial, record_usage, set_role_usage,
)


def _resp(text: str, p: int, c: int) -> dict:
    return {"choices": [{"message": {"content": text}}],
            "usage": {"prompt_tokens": p, "completion_tokens": c}}


class _Stub:
    """A chat client that answers from a queue."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = 0

    def chat(self, payload):
        self.calls += 1
        return self.answers.pop(0)


class RecordUsageTests(unittest.TestCase):

    def test_calls_accumulate_per_role(self):
        usage: dict = {}
        record_usage(usage, "judge", "kimi", _resp("a", 100, 10))
        record_usage(usage, "judge", "kimi", _resp("b", 50, 5))
        record_usage(usage, "meta_judge", "gemma", _resp("c", 7, 3))
        self.assertEqual(usage["judge"], {"model": "kimi", "calls": 2,
                                          "prompt": 150, "completion": 15})
        self.assertEqual(usage["meta_judge"]["calls"], 1)

    def test_a_response_without_usage_is_still_a_call(self):
        usage: dict = {}
        record_usage(usage, "judge", "kimi", {"choices": []})
        record_usage(usage, "judge", "kimi", None)
        self.assertEqual(usage["judge"]["calls"], 2)
        self.assertEqual(usage["judge"]["prompt"], 0)

    def test_set_role_usage_records_a_summed_total(self):
        usage: dict = {}
        set_role_usage(usage, "coder", "glm", calls=3, prompt=900,
                       completion=90)
        self.assertEqual(usage["coder"], {"model": "glm", "calls": 3,
                                          "prompt": 900, "completion": 90})

    def test_both_trial_types_carry_usage(self):
        self.assertEqual(CoderTrial(question_id="q", tier="t",
                                    model="m").usage, {})
        self.assertIn("usage", ToolExecTrial(question_id="q", tier="t",
                                             model="m").to_dict())


class CoderBenchJudgesRecordTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sandbox = Path(self.tmp.name)
        (self.sandbox / "solution.py").write_text("def f():\n    return 1\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_quality_judge_records_its_retry(self):
        usage: dict = {}
        stub = _Stub(_resp("", 400, 0), _resp("SCORE: 4\nfine", 400, 20))
        score, _ = coder_bench._judge_trial_quality(
            judge_chat_client=stub, judge_model="kimi-k2.6:cloud",
            task="t", sandbox=self.sandbox, sandbox_path="solution.py",
            usage=usage)
        self.assertEqual(score, 4.0)
        self.assertEqual(usage["judge"], {"model": "kimi-k2.6:cloud",
                                          "calls": 2, "prompt": 800,
                                          "completion": 20})

    def test_the_audit_judge_records_under_its_own_role(self):
        usage: dict = {}
        stub = _Stub(_resp("SCORE: 3\nok", 300, 30))
        coder_bench._audit_judge_trial(
            judge_chat_client=stub, judge_model="gemma4:31b-cloud",
            task="t", sandbox=self.sandbox, sandbox_path="solution.py",
            passes_algorithm=True, test_results={}, constraint_names=set(),
            language="Python", fence="python", usage=usage)
        self.assertEqual(usage["audit_judge"]["calls"], 1)
        self.assertEqual(usage["audit_judge"]["model"], "gemma4:31b-cloud")
        self.assertNotIn("judge", usage)

    def test_usage_is_optional(self):
        stub = _Stub(_resp("SCORE: 5\ngood", 10, 1))
        score, _ = coder_bench._judge_trial_quality(
            judge_chat_client=stub, judge_model="m", task="t",
            sandbox=self.sandbox, sandbox_path="solution.py")
        self.assertEqual(score, 5.0)


class RejudgeRecordsTests(unittest.TestCase):

    def _work(self):
        return [("a", [{"role": "user", "content": "x"}]),
                ("b", [{"role": "user", "content": "y"}])]

    def test_each_key_gets_its_own_usage(self):
        out: dict = {}
        answers = iter([_resp("SCORE: 4", 10, 1), _resp("SCORE: 3", 20, 2)])

        class _C:
            def chat(self, payload):
                return next(answers)

        rejudge._judge_many(self._work(), make_client=_C, model="gemini",
                            num_predict=50, concurrency=1, usage_out=out,
                            role="secondary_judge")
        self.assertEqual(out["a"]["secondary_judge"]["prompt"], 10)
        self.assertEqual(out["b"]["secondary_judge"]["prompt"], 20)

    def test_the_thread_pool_keeps_keys_apart(self):
        out: dict = {}

        class _C:
            def chat(self, payload):
                n = len(payload["messages"][0]["content"])
                return _resp("SCORE: 4", 100 * n, n)

        rejudge._judge_many(self._work(), make_client=_C, model="gemini",
                            num_predict=50, concurrency=2, usage_out=out,
                            role="ladder_judge")
        self.assertEqual(sorted(out), ["a", "b"])
        for key in out:
            self.assertEqual(out[key]["ladder_judge"]["calls"], 1)


if __name__ == "__main__":
    unittest.main()
