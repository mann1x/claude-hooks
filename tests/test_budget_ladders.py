"""The measured ladders in consultants/engine/budget.py, and the
capped-thinking wiring in the shared agent loop.

Companion to test_consultants_budget.py, which covers the original flat
behaviour that still holds. These cover what the Cline port added.
"""

import unittest

from claude_hooks import token_calib
from claude_hooks.agent_loop.runner import LoopConfig, run_loop
from consultants.engine import budget


class _Client:
    def __init__(self, ctx=262_144):
        self.ctx = ctx

    def context_length(self, model):
        return self.ctx


def _assistant(output_tokens):
    return {"role": "assistant", "content": "x",
            "_output_tokens": output_tokens}


class TestObservedOutputReservation(unittest.TestCase):
    """`num_predict` is a ceiling, not a forecast. Reserving all of it
    takes the whole cap out of the prompt budget on every turn."""

    def test_needs_two_samples(self):
        """One turn is not a pattern, and the first turn of a run is
        routinely the smallest it will produce."""
        self.assertIsNone(budget.observed_output_tokens([_assistant(500)]))

    def test_high_water_not_mean(self):
        """The reservation exists for the largest turn; an average over
        a run that thinks on one turn in four describes none of them."""
        msgs = [_assistant(100), _assistant(100), _assistant(9000),
                _assistant(100)]
        self.assertEqual(budget.observed_output_tokens(msgs), 9000)

    def test_only_looks_at_recent_turns(self):
        msgs = [_assistant(99_000)] + [_assistant(100)] * 20
        self.assertEqual(budget.observed_output_tokens(msgs, sample_turns=5),
                         100)

    def test_user_messages_are_not_turns(self):
        msgs = [{"role": "user", "content": "q", "_output_tokens": 9999},
                _assistant(10), _assistant(20)]
        self.assertEqual(budget.observed_output_tokens(msgs), 20)

    def test_a_quiet_run_lowers_the_trigger_less_than_cold_start(self):
        """The measurement is what stops 21% of the window being held
        for an output that almost never arrives."""
        cold = budget.compaction_trigger_tokens(context_length=110_000)
        measured = budget.compaction_trigger_tokens(
            context_length=110_000, observed_output=800)
        self.assertGreater(measured, cold)


class TestRecencyFloor(unittest.TestCase):
    def test_scales_sub_linearly_with_the_window(self):
        small = budget.preserve_recent_tokens(context_length=32_000)
        ref = budget.preserve_recent_tokens(context_length=128_000)
        big = budget.preserve_recent_tokens(context_length=1_000_000)
        self.assertLess(small, ref)
        self.assertLess(ref, big)
        # Sub-linear: 8x the window buys well under 8x the floor.
        self.assertLess(big, ref * 8)

    def test_anchors(self):
        """20,000 at 128k and ~79,000 at 1M are the two anchors the
        exponent was specified against."""
        self.assertEqual(
            budget.preserve_recent_tokens(context_length=128_000), 20_000)
        self.assertAlmostEqual(
            budget.preserve_recent_tokens(context_length=1_000_000),
            79_000, delta=2_000)

    def test_never_claims_more_than_its_share_of_the_target(self):
        """Without this the ladder wins every argument on a small window
        and leaves the summary nothing to be written into."""
        floor = budget.preserve_recent_tokens(
            context_length=1_000_000, target_tokens=10_000)
        self.assertLessEqual(floor, 6_000)

    def test_unknown_window_uses_the_reference_default(self):
        self.assertEqual(budget.preserve_recent_tokens(),
                         budget.DEFAULT_PRESERVE_RECENT_TOKENS)


class TestTriggerAccountsForOutput(unittest.TestCase):
    def test_trigger_is_below_the_window(self):
        """A turn needs the prompt *and* its output inside one window."""
        trigger = budget.compaction_trigger_tokens(context_length=110_000)
        self.assertLess(trigger, 110_000)

    def test_never_falls_below_half_the_window(self):
        """A 32,000 cap against a 40,000 window would otherwise put the
        trigger at 8,000 and compact almost every turn — at that point
        the cap is the unreasonable figure, not the transcript."""
        trigger = budget.compaction_trigger_tokens(
            context_length=40_000, model_max_tokens=32_000)
        self.assertGreaterEqual(trigger, 20_000)


class TestCapAttribution(unittest.TestCase):
    def test_a_roomy_window_reports_our_own_request(self):
        p = budget.plan(_Client(262_144), "m",
                        [{"role": "user", "content": "short"}])
        self.assertEqual(p.cap_source, "requested")
        self.assertFalse(p.window_bound)

    def test_a_crowded_window_reports_the_window(self):
        big = "x" * (39_000 * 3)
        p = budget.plan(_Client(40_000), "m",
                        [{"role": "user", "content": big}])
        self.assertTrue(p.window_bound)
        self.assertTrue(p.needs_compaction)

    def test_overflow_is_recorded_for_the_next_planner(self):
        big = "x" * (39_000 * 3)
        budget.plan(_Client(40_000), "m",
                    [{"role": "user", "content": big}])
        self.assertIsNotNone(token_calib.consume_context_overflow("m"))

    def test_past_the_trigger_but_still_fitting_is_window_bound(self):
        """The case the old 'does the prompt fit?' test waved through."""
        big = "x" * (30_000 * 3)
        p = budget.plan(_Client(40_000), "m",
                        [{"role": "user", "content": big}])
        self.assertTrue(p.needs_compaction)
        self.assertEqual(p.cap_source, "remaining-context")

    def test_retry_cap_gives_a_regeneration_less_to_spend(self):
        p = budget.plan(_Client(262_144), "m",
                        [{"role": "user", "content": "q"}], retry_cap=8_000)
        self.assertLess(p.output_tokens, 8_000)
        self.assertGreaterEqual(p.output_tokens, budget.MIN_OUTPUT_TOKENS)


class TestCompactionReclaimsThinking(unittest.TestCase):
    def _history(self):
        msgs = [{"role": "system", "content": "instructions"},
                {"role": "user", "content": "the question"}]
        for i in range(30):
            msgs.append({"role": "assistant", "content": f"note {i} " * 400,
                         "thinking": f"reasoning {i} " * 200})
        return msgs

    def test_returns_the_reasoning_it_dropped(self):
        """Compaction is where the reasoning of discarded turns is
        reclaimed — that is the whole point of doing it there."""
        result = budget.compact_messages(self._history(), keep_tokens=8_000)
        self.assertTrue(result.changed)
        self.assertTrue(result.reclaimed_thinking)

    def test_reports_how_many_it_dropped(self):
        result = budget.compact_messages(self._history(), keep_tokens=8_000)
        self.assertGreater(result.dropped, 0)

    def test_unpacks_as_the_old_tuple(self):
        """Callers written against the pre-port signature still work."""
        messages, changed = budget.compact_messages(
            [{"role": "user", "content": "hi"}], keep_tokens=10_000)
        self.assertFalse(changed)
        self.assertEqual(len(messages), 1)

    def test_generation_counts_previous_compactions(self):
        first = budget.compact_messages(self._history(), keep_tokens=8_000)
        self.assertEqual(first.generation, 1)
        grown = first.messages + [
            {"role": "assistant", "content": "more " * 4_000}
            for _ in range(10)]
        second = budget.compact_messages(grown, keep_tokens=8_000)
        self.assertEqual(second.generation, 2)

    def test_tail_is_sized_from_the_window_when_known(self):
        history = self._history()
        small = budget.compact_messages(
            history, keep_tokens=60_000, context_length=32_000)
        large = budget.compact_messages(
            history, keep_tokens=60_000, context_length=1_000_000)
        self.assertLessEqual(len(small.messages), len(large.messages))


def _resp(content, *, done_reason="stop", thinking=None, tool_calls=None,
          completion_tokens=100):
    msg = {"content": content}
    if thinking:
        msg["thinking"] = thinking
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {
        "choices": [{"message": msg, "done_reason": done_reason,
                     "finish_reason": ("tool_calls" if tool_calls
                                       else done_reason)}],
        "usage": {"prompt_tokens": 10,
                  "completion_tokens": completion_tokens},
        "done_reason": done_reason,
    }


_TC = [{"id": "1", "type": "function",
        "function": {"name": "read_file", "arguments": '{"path":"a.py"}'}}]


class TestCappedThinkingInTheAgentLoop(unittest.TestCase):
    """The council's loop stripped thinking unconditionally, so a turn
    that reasoned to its budget began the next iteration with no record
    of having reasoned at all."""

    def _run(self, responses, **cfg):
        sent = []

        def chat_fn(payload):
            sent.append(payload)
            return responses.pop(0)

        run_loop(
            payload={"model": "m",
                     "messages": [{"role": "user", "content": "q"}],
                     "options": {"num_predict": 1000}},
            cwd=".", tool_executor=lambda *a: "ok",
            config=LoopConfig(tools_available=True,
                              force_first_tool_call=False, **cfg),
            tool_specs=[], chat_fn=chat_fn)
        return sent

    def test_thinking_within_budget_is_still_dropped(self):
        sent = self._run([
            _resp("", thinking="brief", tool_calls=_TC,
                  completion_tokens=50),
            _resp("Answer."),
        ])
        replayed = sent[1]["messages"][1]
        self.assertIsNone(replayed.get("thinking"))
        self.assertIsNone(replayed.get("content"))

    def test_capped_thinking_leaves_a_note_in_its_place(self):
        sent = self._run([
            # Burned the whole 1000-token budget, nearly all on thinking.
            _resp("", thinking="t" * 4000, tool_calls=_TC,
                  completion_tokens=980),
            _resp("A condensed note about what was settled."),
            _resp("Answer."),
        ])
        replayed = sent[-1]["messages"][1]
        self.assertIsNone(replayed.get("thinking"))
        self.assertIn("condensed note", replayed["content"])
        self.assertTrue(replayed["content"].startswith(
            "I have already reasoned"))

    def test_the_condensation_asks_for_no_reasoning(self):
        sent = self._run([
            _resp("", thinking="t" * 4000, tool_calls=_TC,
                  completion_tokens=980),
            _resp("note"), _resp("Answer."),
        ])
        self.assertIs(sent[1]["think"], False)

    def test_disabling_restores_the_old_behaviour(self):
        sent = self._run([
            _resp("", thinking="t" * 4000, tool_calls=_TC,
                  completion_tokens=980),
            _resp("Answer."),
        ], capped_thinking_enabled=False)
        replayed = sent[1]["messages"][1]
        self.assertIsNone(replayed.get("thinking"))
        self.assertIsNone(replayed.get("content"))

    def test_a_degenerate_note_leaves_no_note(self):
        """No note beats a note that loops — it lands in the reasoning
        channel and reads as things the model thought."""
        sent = self._run([
            _resp("", thinking="t" * 4000, tool_calls=_TC,
                  completion_tokens=980),
            _resp("\n".join(["Summary-of-thought process:"] * 10)),
            _resp("Answer."),
        ])
        self.assertIsNone(sent[-1]["messages"][1].get("content"))

    def test_a_failed_condensation_does_not_break_the_loop(self):
        calls = {"n": 0}

        def chat_fn(payload):
            calls["n"] += 1
            if calls["n"] == 1:
                return _resp("", thinking="t" * 4000, tool_calls=_TC,
                             completion_tokens=980)
            if calls["n"] == 2:
                raise RuntimeError("condenser down")
            return _resp("Answer.")

        out = run_loop(
            payload={"model": "m",
                     "messages": [{"role": "user", "content": "q"}],
                     "options": {"num_predict": 1000}},
            cwd=".", tool_executor=lambda *a: "ok",
            config=LoopConfig(tools_available=True,
                              force_first_tool_call=False),
            tool_specs=[], chat_fn=chat_fn)
        self.assertEqual(out["choices"][0]["message"]["content"], "Answer.")


if __name__ == "__main__":
    unittest.main()
