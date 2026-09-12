"""Truncation handling end-to-end across the consultants roles.

Three properties, one per layer:

* **visibility** — the recorder tallies every cut completion, for every
  role, because ``record_llm`` is the one function all of them reach;
* **recovery** — ``_single_shot`` and the shared agent loop continue a
  cut answer instead of returning half of one;
* **honesty** — a deliverable that is still short after recovery is
  reported as failed, not as ``completed`` with ``error: null``.
"""

import tempfile
import unittest
from pathlib import Path

from claude_hooks.agent_loop.runner import LoopConfig, run_loop
from consultants.engine import council
from consultants.engine.recorder import MessageRecorder, RecorderMeta
from consultants.server.runner import _truncation_verdict


def _resp(content, *, done_reason="stop", tool_calls=None,
          prompt_tokens=10, completion_tokens=20):
    msg = {"content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {
        "choices": [{
            "message": msg,
            "finish_reason": "tool_calls" if tool_calls else done_reason,
            "done_reason": done_reason,
        }],
        "usage": {"prompt_tokens": prompt_tokens,
                  "completion_tokens": completion_tokens},
        "done_reason": done_reason,
    }


class _ScriptedClient:
    """Returns queued responses; records the payloads it was sent."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.payloads = []

    def chat(self, payload):
        self.payloads.append(payload)
        if not self._responses:
            raise AssertionError("scripted client ran out of responses")
        return self._responses.pop(0)

    def context_length(self, model):
        return 262144


# ------------------------------------------------------------------ #
# Layer 1 — visibility
# ------------------------------------------------------------------ #

class TestRecorderTally(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.rec = MessageRecorder(
            Path(self._tmp.name) / "t.db",
            meta=RecorderMeta(sid="s1", cwd=self._tmp.name, question="q",
                              effort="high", topology="council", models={}),
        )

    def tearDown(self):
        try:
            self.rec.close()
        except Exception:
            pass
        self._tmp.cleanup()

    def test_clean_call_tallies_nothing(self):
        self.rec.record_llm(role="researcher",
                            response=_resp("a whole answer."))
        self.assertEqual(self.rec.truncations_by_role(), {})
        self.assertEqual(self.rec.unrecovered_truncations(), [])

    def test_cut_call_is_tallied_per_role(self):
        self.rec.record_llm(role="synthesizer", model="m",
                            response=_resp("cut", done_reason="length"))
        self.assertEqual(self.rec.truncations_by_role(), {"synthesizer": 1})
        self.assertEqual(self.rec.unrecovered_truncations(), ["synthesizer"])

    def test_every_role_is_covered_by_the_same_choke_point(self):
        """The incident's lesson: wiring detection per-node means N
        chances to forget one."""
        for role in ("planner", "researcher", "critic", "refuter",
                     "synthesizer", "tool_executor", "coder"):
            self.rec.record_llm(role=role,
                                response=_resp("x", done_reason="length"))
        self.assertEqual(len(self.rec.truncations_by_role()), 7)

    def test_recovery_clears_the_unrecovered_flag(self):
        """Cut, then continued to completion: the tally remembers it
        happened, the verdict does not fail the run over it."""
        self.rec.record_llm(role="synthesizer",
                            response=_resp("part one", done_reason="length"))
        self.rec.record_llm(role="synthesizer",
                            response=_resp(" and part two."))
        self.assertEqual(self.rec.truncations_by_role(), {"synthesizer": 1})
        self.assertEqual(self.rec.unrecovered_truncations(), [])

    def test_detail_is_kept_for_the_post_mortem(self):
        self.rec.record_llm(role="critic", model="gemma4:31b-cloud", round=2,
                            response=_resp("x" * 80, done_reason="length"))
        entries = self.rec.truncations()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["role"], "critic")
        self.assertEqual(entries[0]["round"], 2)
        self.assertEqual(entries[0]["kind"], "reported")

    def test_a_broken_detector_cannot_sink_a_run(self):
        import claude_hooks.truncation as trunc_mod
        original = trunc_mod.classify
        trunc_mod.classify = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("boom"))
        try:
            self.rec.record_llm(role="researcher", response=_resp("ok"))
        finally:
            trunc_mod.classify = original


# ------------------------------------------------------------------ #
# Layer 2 — recovery
# ------------------------------------------------------------------ #

class TestSingleShotContinuation(unittest.TestCase):
    """Covers every non-tooled role: planner, researcher, critic,
    refuter, synthesizer all reach the backend through _single_shot."""

    def test_clean_answer_makes_exactly_one_call(self):
        client = _ScriptedClient([_resp("A complete answer.")])
        text, pt, ct = council._single_shot(client, "m", [
            {"role": "user", "content": "q"}])
        self.assertEqual(text, "A complete answer.")
        self.assertEqual(len(client.payloads), 1)

    def test_cut_answer_is_continued_and_joined(self):
        client = _ScriptedClient([
            _resp("The metric counts the pre", done_reason="length"),
            _resp("sence of each row."),
        ])
        text, _, _ = council._single_shot(client, "m", [
            {"role": "user", "content": "q"}])
        self.assertEqual(text, "The metric counts the presence of each row.")

    def test_continuation_prompt_carries_the_partial(self):
        client = _ScriptedClient([
            _resp("half", done_reason="length"),
            _resp(" done."),
        ])
        council._single_shot(client, "m", [{"role": "user", "content": "q"}])
        msgs = client.payloads[1]["messages"]
        self.assertEqual(msgs[-2]["role"], "assistant")
        self.assertEqual(msgs[-2]["content"], "half")
        self.assertIn("cut off", msgs[-1]["content"])

    def test_continuations_are_bounded(self):
        client = _ScriptedClient(
            [_resp("more", done_reason="length")] * 10)
        council._single_shot(client, "m", [{"role": "user", "content": "q"}])
        self.assertEqual(len(client.payloads),
                         council.MAX_CONTINUATIONS + 1)

    def test_tokens_are_summed_across_continuations(self):
        client = _ScriptedClient([
            _resp("a", done_reason="length", prompt_tokens=100,
                  completion_tokens=50),
            _resp("b", prompt_tokens=150, completion_tokens=25),
        ])
        _, pt, ct = council._single_shot(client, "m", [
            {"role": "user", "content": "q"}])
        self.assertEqual((pt, ct), (250, 75))

    def test_an_explicit_output_budget_is_always_sent(self):
        """The incident ran with no num_predict at all, so the ceiling
        was a provider default nobody had set or could see."""
        client = _ScriptedClient([_resp("done.")])
        council._single_shot(client, "m", [{"role": "user", "content": "q"}])
        self.assertIn("num_predict", client.payloads[0]["options"])
        self.assertGreater(client.payloads[0]["options"]["num_predict"], 0)

    def test_parts_join_without_inserted_whitespace(self):
        """A continuation resumes mid-word; stripping each half before
        joining would fuse 'pre' onto 'sence' as 'presence' only by
        luck, and 'the' onto 'answer' as 'theanswer' otherwise."""
        client = _ScriptedClient([
            _resp("ends with a space ", done_reason="length"),
            _resp("and continues."),
        ])
        text, _, _ = council._single_shot(client, "m", [
            {"role": "user", "content": "q"}])
        self.assertEqual(text, "ends with a space and continues.")

    def test_recorder_sees_every_continuation(self):
        calls = []

        class _Rec:
            def record_llm(self, **kw):
                calls.append(kw)

        client = _ScriptedClient([
            _resp("a", done_reason="length"), _resp("b"),
        ])
        council._single_shot(client, "m", [{"role": "user", "content": "q"}],
                             recorder=_Rec(), role="synthesizer")
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(c["role"] == "synthesizer" for c in calls))


class TestAgentLoopContinuation(unittest.TestCase):
    """Covers the tooled roles: tool_executor and coder run through
    claude_hooks.agent_loop.runner, not _single_shot."""

    def _run(self, responses, **cfg_kw):
        seen = []

        def chat_fn(payload):
            seen.append(payload)
            return responses.pop(0)

        cfg = LoopConfig(tools_available=False,
                         force_first_tool_call=False, **cfg_kw)
        out = run_loop(payload={"model": "m", "messages": [
            {"role": "user", "content": "q"}]},
            cwd=".", tool_executor=lambda *a: "", config=cfg,
            tool_specs=[], chat_fn=chat_fn)
        return out, seen

    def test_clean_answer_is_untouched(self):
        out, seen = self._run([_resp("A whole answer.")])
        self.assertEqual(len(seen), 1)
        self.assertEqual(
            out["choices"][0]["message"]["content"], "A whole answer.")

    def test_cut_answer_is_continued(self):
        out, seen = self._run([
            _resp("The first ha", done_reason="length"),
            _resp("lf and the second."),
        ])
        self.assertEqual(len(seen), 2)
        self.assertEqual(out["choices"][0]["message"]["content"],
                         "The first half and the second.")

    def test_continuation_drops_tools(self):
        """The model already chose to answer; re-offering tools invites
        it to restart the investigation instead of finishing."""
        responses = [_resp("cut", done_reason="length"), _resp(" end.")]
        seen = []

        def chat_fn(payload):
            seen.append(payload)
            return responses.pop(0)

        run_loop(payload={"model": "m",
                          "messages": [{"role": "user", "content": "q"}],
                          "tools": [{"function": {"name": "read_file"}}]},
                 cwd=".", tool_executor=lambda *a: "",
                 config=LoopConfig(tools_available=True,
                                   force_first_tool_call=False),
                 tool_specs=[], chat_fn=chat_fn)
        self.assertIn("tools", seen[0])
        self.assertNotIn("tools", seen[1])

    def test_bounded_by_config(self):
        out, seen = self._run(
            [_resp("more", done_reason="length")] * 6,
            max_answer_continuations=2)
        self.assertEqual(len(seen), 3)

    def test_zero_restores_the_old_behavior(self):
        out, seen = self._run([_resp("cut", done_reason="length")],
                              max_answer_continuations=0)
        self.assertEqual(len(seen), 1)
        self.assertEqual(out["choices"][0]["message"]["content"], "cut")

    def test_usage_is_summed(self):
        out, _ = self._run([
            _resp("a", done_reason="length", prompt_tokens=10,
                  completion_tokens=5),
            _resp("b", prompt_tokens=20, completion_tokens=7),
        ])
        self.assertEqual(out["usage"]["prompt_tokens"], 30)
        self.assertEqual(out["usage"]["completion_tokens"], 12)

    def test_tool_turns_still_route_to_tools(self):
        """finish_reason must stay 'tool_calls' for a tool turn now that
        done_reason is no longer discarded — otherwise preserving it
        would have broken every tooled role."""
        tc = [{"function": {"name": "read_file",
                            "arguments": '{"path": "a.py"}'}}]
        responses = [_resp("", tool_calls=tc), _resp("Answer.")]
        executed = []

        def chat_fn(payload):
            return responses.pop(0)

        run_loop(payload={"model": "m",
                          "messages": [{"role": "user", "content": "q"}]},
                 cwd=".",
                 tool_executor=lambda n, a, c: executed.append(n) or "ok",
                 config=LoopConfig(tools_available=True,
                                   force_first_tool_call=False),
                 tool_specs=[], chat_fn=chat_fn)
        self.assertEqual(executed, ["read_file"])


# ------------------------------------------------------------------ #
# Layer 3 — honesty
# ------------------------------------------------------------------ #

class _FakeRecorder:
    def __init__(self, counts=None, unrecovered=None):
        self._counts = counts or {}
        self._unrecovered = unrecovered or []

    def truncations_by_role(self):
        return dict(self._counts)

    def unrecovered_truncations(self):
        return list(self._unrecovered)


class TestRunVerdict(unittest.TestCase):
    def test_clean_run_passes(self):
        counts, err = _truncation_verdict(
            _FakeRecorder(), "A complete final answer.")
        self.assertEqual(counts, {})
        self.assertIsNone(err)

    def test_unrecovered_synthesizer_fails_the_run(self):
        """The exact shape of csl-2026-08-12-0831-905a."""
        counts, err = _truncation_verdict(
            _FakeRecorder({"synthesizer": 3}, ["synthesizer"]),
            "...count the **presence** of $\\text{")
        self.assertEqual(counts, {"synthesizer": 3})
        self.assertIsNotNone(err)
        self.assertIn("synthesizer", err)

    def test_recovered_truncation_does_not_fail_the_run(self):
        """40 minutes of correct work must not be discarded because one
        completion overran its budget and was then finished."""
        counts, err = _truncation_verdict(
            _FakeRecorder({"synthesizer": 1}, []),
            "A complete final answer, assembled from two halves.")
        self.assertEqual(counts, {"synthesizer": 1})
        self.assertIsNone(err)

    def test_a_cut_researcher_does_not_fail_a_whole_answer(self):
        counts, err = _truncation_verdict(
            _FakeRecorder({"researcher": 2}, ["researcher"]),
            "A complete final answer.")
        self.assertEqual(counts, {"researcher": 2})
        self.assertIsNone(err)

    def test_structural_backstop_without_a_recorder(self):
        """Recorder construction is best-effort; when it failed, the
        counts prove nothing and the answer itself is the evidence."""
        counts, err = _truncation_verdict(
            None, "The aggregate is computed over the rows in `")
        self.assertEqual(counts, {})
        self.assertIsNotNone(err)

    def test_a_broken_recorder_query_does_not_sink_the_run(self):
        class _Bad:
            def truncations_by_role(self):
                raise RuntimeError("db gone")

            def unrecovered_truncations(self):
                raise RuntimeError("db gone")

        counts, err = _truncation_verdict(_Bad(), "A complete answer.")
        self.assertEqual(counts, {})
        self.assertIsNone(err)

    def test_error_text_tells_the_user_what_to_do(self):
        _, err = _truncation_verdict(
            _FakeRecorder({"refuter": 1}, ["refuter"]), "cut off at `")
        self.assertIn("incomplete", err)
        self.assertIn("re-run", err)

    def test_empty_answer_is_not_called_truncated(self):
        """A cancelled or refused run has no answer; that is a different
        failure with its own, more specific verdict."""
        counts, err = _truncation_verdict(_FakeRecorder(), "")
        self.assertIsNone(err)


if __name__ == "__main__":
    unittest.main()
