"""Two-phase compaction — consultants/engine/retrospective.py.

Tier D of the Cline-fork port. What compaction used to leave behind was
a marker counting the messages it dropped; what it leaves now is a
hand-over note plus an honest assessment of how the work went, which is
the half that only ever existed in the thinking blocks.
"""

import unittest

from consultants.engine import budget, retrospective as retro


class _Client:
    """Returns queued texts, one per phase, recording each payload."""

    def __init__(self, *texts, raises_on=None, tokens=100):
        self.texts = list(texts)
        self.raises_on = raises_on
        self.tokens = tokens
        self.payloads = []

    def chat(self, payload):
        self.payloads.append(payload)
        n = len(self.payloads)
        if self.raises_on == n:
            raise RuntimeError("summariser down")
        text = self.texts.pop(0) if self.texts else "text"
        return {"choices": [{"message": {"content": text}}],
                "usage": {"completion_tokens": self.tokens}}


def _turn(thinking="", calls=(), output_tokens=None):
    m = {"role": "assistant", "content": None}
    if thinking:
        m["thinking"] = thinking
    if calls:
        m["tool_calls"] = [
            {"id": f"tc{i}", "type": "function",
             "function": {"name": name, "arguments": f'{{"path": "{path}"}}'}}
            for i, (name, path) in enumerate(calls)]
    if output_tokens:
        m["_output_tokens"] = output_tokens
    return m


def _result(tool_call_id, content, is_error=False):
    return {"role": "tool", "tool_call_id": tool_call_id,
            "content": content, "is_error": is_error}


class TestBudgetLadder(unittest.TestCase):
    def test_budget_grows_with_generation(self):
        """A first compaction summarises one stretch of work; a fifth is
        carrying everything the task has learned."""
        shares = [retro.resolve_output_budgets(
            target_tokens=100_000, generation=g).combined_tokens
            for g in range(1, 6)]
        self.assertEqual(shares, sorted(shares))
        self.assertLess(shares[0], shares[-1])

    def test_ladder_has_a_ceiling(self):
        """Beyond the last rung the standing context crowds out the
        recent turns it exists to give perspective on."""
        fifth = retro.resolve_output_budgets(
            target_tokens=100_000, generation=5).combined_tokens
        twentieth = retro.resolve_output_budgets(
            target_tokens=100_000, generation=20).combined_tokens
        self.assertEqual(fifth, twentieth)

    def test_summary_writes_first_against_seventy_percent(self):
        b = retro.resolve_output_budgets(target_tokens=100_000)
        self.assertAlmostEqual(
            b.summary_max_tokens / b.combined_tokens, 0.7, places=2)

    def test_an_economical_summary_buys_the_retrospective_room(self):
        """Measured against what the summary actually cost, not what it
        was allowed — that is the point of writing them in this order."""
        b = retro.resolve_output_budgets(target_tokens=100_000)
        lavish = retro.resolve_thinking_max_tokens(b, b.summary_max_tokens)
        frugal = retro.resolve_thinking_max_tokens(b, 100)
        self.assertGreater(frugal, lavish)

    def test_retrospective_has_a_floor_and_a_ceiling(self):
        b = retro.resolve_output_budgets(target_tokens=100_000)
        self.assertGreaterEqual(
            retro.resolve_thinking_max_tokens(b, 10 ** 9),
            int(b.combined_tokens * retro.THINKING_BUDGET_MIN_SHARE))
        self.assertLessEqual(
            retro.resolve_thinking_max_tokens(b, 0),
            int(b.combined_tokens * retro.THINKING_BUDGET_MAX_SHARE))

    def test_tiny_targets_still_get_a_usable_floor(self):
        b = retro.resolve_output_budgets(target_tokens=10)
        self.assertGreaterEqual(b.combined_tokens,
                                retro.MIN_PHASE_OUTPUT_TOKENS)


class TestToolOutcomes(unittest.TestCase):
    """Reduced to a verdict: a retrospective about method has no use for
    the contents of a file, and results are most of the bytes."""

    def test_distinguishes_the_cases_that_matter(self):
        cases = {
            "3 matches in src/a.py": "returned results",
            "": "no result recorded",
            "No change: the file already contains that": "refused",
            "Error: ENOENT no such file": "failed",
            'ok {"success": false, "why": "x"}': "reported failure",
            "no matches found": "returned nothing",
            "you have 2 strikes left": "loop guard",
        }
        for text, expected in cases.items():
            self.assertIn(expected.split()[0],
                          retro.describe_tool_outcome(text).lower(),
                          f"for {text!r}")

    def test_error_flag_wins_over_content(self):
        self.assertTrue(retro.describe_tool_outcome(
            "some output", is_error=True).startswith("failed"))

    def test_verdicts_are_bounded(self):
        verdict = retro.describe_tool_outcome("Error: " + "x" * 5000)
        self.assertLess(len(verdict), retro.MAX_OUTCOME_CHARS + 40)


class TestReasoningPairing(unittest.TestCase):
    """The retrospective's whole value is the pairing: reasoning on its
    own reads as a plan, and every plan reads as sound."""

    def test_pairs_reasoning_with_what_it_produced(self):
        msgs = [_turn("I will check the loader.",
                      [("read_file", "loader.py")]),
                _result("tc0", "Error: ENOENT", is_error=True)]
        text = retro.serialize_reasoning_with_outcomes(msgs)
        self.assertIn("check the loader", text)
        self.assertIn("read_file ->", text)
        self.assertIn("failed", text)

    def test_names_the_cost_of_a_long_think(self):
        """The one thing a model cannot infer from reading its own
        thinking back: whether a stretch that felt thorough was the turn
        that spent eighteen thousand tokens for one refused call."""
        msgs = [_turn("t" * 4000, [("grep", "x")], output_tokens=9000)]
        self.assertIn("tokens", retro.serialize_reasoning_with_outcomes(msgs))

    def test_one_runaway_think_cannot_crowd_out_the_others(self):
        msgs = [_turn("t" * 500_000, [("grep", "a")]),
                _turn("the second turn reasoning", [("grep", "b")])]
        text = retro.serialize_reasoning_with_outcomes(msgs)
        self.assertIn("second turn reasoning", text)
        self.assertLess(len(text), retro.MAX_REASONING_CHARS_PER_TURN * 3)

    def test_turns_with_neither_are_skipped(self):
        self.assertEqual(
            retro.serialize_reasoning_with_outcomes(
                [{"role": "assistant", "content": "just text"}]), "")

    def test_summary_input_excludes_reasoning(self):
        """The summariser is reading the transcript to describe it; how
        the model talked itself into each call is the retrospective's
        input, not this one's."""
        msgs = [_turn("SECRET_REASONING_MARKER", [("grep", "x")])]
        self.assertNotIn("SECRET_REASONING_MARKER",
                         retro.serialize_conversation(msgs))


class TestFileOps(unittest.TestCase):
    def test_collects_read_and_edited_paths(self):
        msgs = [_turn(calls=[("read_file", "a.py"), ("write_file", "b.py")])]
        ops = retro.collect_file_ops(msgs)
        self.assertEqual(ops.read, ["a.py"])
        self.assertEqual(ops.edited, ["b.py"])

    def test_deduplicates(self):
        msgs = [_turn(calls=[("read_file", "a.py")]),
                _turn(calls=[("read_file", "a.py")])]
        self.assertEqual(retro.collect_file_ops(msgs).read, ["a.py"])

    def test_files_section_is_appended_to_a_template_that_omits_it(self):
        """Losing track of which files are in play is the one failure
        that makes a note actively misleading rather than merely thin."""
        prompt = retro.build_summary_prompt(
            [_turn(calls=[("read_file", "zz.py")])],
            template="Write a note.")
        self.assertIn("zz.py", prompt)
        self.assertIn("## Files", prompt)

    def test_placeholders_are_substituted_when_present(self):
        prompt = retro.build_summary_prompt(
            [_turn(calls=[("read_file", "zz.py")])],
            template="Read: {{files_read}} Edited: {{files_edited}}")
        self.assertIn("zz.py", prompt)
        self.assertNotIn("{{files_read}}", prompt)


class TestPrompts(unittest.TestCase):
    def test_retrospective_prompt_bans_specifics(self):
        """Those are in the summary; repeating them spends the budget
        twice for one fact."""
        p = retro.DEFAULT_THINKING_COMPACTION_PROMPT
        self.assertIn("No file names", p)
        self.assertIn("about method", p)

    def test_retrospective_prompt_is_written_against_verbosity(self):
        p = retro.DEFAULT_THINKING_COMPACTION_PROMPT
        self.assertIn("Terse", p)
        self.assertIn("a page is a failed one", p)

    def test_previous_retrospective_is_revised_not_restated(self):
        prompt = retro.build_retrospective_prompt(
            [_turn("reasoning", [("grep", "x")])],
            previous_retrospective="the earlier assessment")
        self.assertIn("the earlier assessment", prompt)
        self.assertIn("do not simply restate", prompt)

    def test_previous_summary_is_carried(self):
        prompt = retro.build_summary_prompt(
            [_turn(calls=[("grep", "x")])],
            previous_summary="what came before")
        self.assertIn("what came before", prompt)


class TestWrite(unittest.TestCase):
    def _span(self):
        return [_turn("I ruled out the tokenizer.",
                      [("read_file", "eval.py")], output_tokens=800),
                _result("tc0", "120 lines")]

    def test_writes_both_phases(self):
        client = _Client("THE SUMMARY", "THE RETROSPECTIVE")
        d = retro.write(self._span(), chat_client=client, model="m",
                        target_tokens=50_000)
        self.assertEqual(d.summary, "THE SUMMARY")
        self.assertEqual(d.retrospective, "THE RETROSPECTIVE")
        self.assertEqual(len(client.payloads), 2)

    def test_summary_writes_first(self):
        client = _Client("s", "r")
        retro.write(self._span(), chat_client=client, model="m",
                    target_tokens=50_000)
        self.assertIn("hand-over note", client.payloads[0]["messages"][1]
                      ["content"])
        self.assertIn("retrospective", client.payloads[1]["messages"][1]
                      ["content"])

    def test_neither_phase_reasons(self):
        """A summariser that reasons spends its budget on thinking and
        returns no text."""
        client = _Client("s", "r")
        retro.write(self._span(), chat_client=client, model="m",
                    target_tokens=50_000)
        for payload in client.payloads:
            self.assertIs(payload["think"], False)

    def test_no_reasoning_means_no_retrospective(self):
        """Nothing to assess; asking anyway produces a model inventing
        an assessment of work it cannot see."""
        client = _Client("s")
        span = [{"role": "user", "content": "a question"},
                {"role": "assistant", "content": "an answer"}]
        d = retro.write(span, chat_client=client, model="m",
                        target_tokens=50_000)
        self.assertEqual(len(client.payloads), 1)
        self.assertEqual(d.retrospective, "")

    def test_a_failed_summary_does_not_stop_the_retrospective(self):
        client = _Client("r", raises_on=1)
        d = retro.write(self._span(), chat_client=client, model="m",
                        target_tokens=50_000)
        self.assertEqual(d.summary, "")
        self.assertEqual(d.retrospective, "r")

    def test_a_failed_call_never_raises(self):
        """A compaction that cannot summarise still has to compact."""
        client = _Client(raises_on=1)
        d = retro.write(self._span(), chat_client=client, model="m",
                        target_tokens=50_000)
        self.assertIsInstance(d, retro.Digest)

    def test_no_client_stands_down(self):
        d = retro.write(self._span(), chat_client=None, model="m",
                        target_tokens=50_000)
        self.assertTrue(d.empty)
        self.assertEqual(d.stand_down, "no-summariser")

    def test_degenerate_retrospective_is_dropped(self):
        client = _Client("s", "\n".join(["What did not work:"] * 10))
        d = retro.write(self._span(), chat_client=client, model="m",
                        target_tokens=50_000)
        self.assertEqual(d.retrospective, "")
        self.assertEqual(d.summary, "s")

    def test_empty_span_stands_down(self):
        d = retro.write([], chat_client=_Client("s"), model="m",
                        target_tokens=50_000)
        self.assertEqual(d.stand_down, "nothing-dropped")


class TestMarker(unittest.TestCase):
    def test_retrospective_comes_first(self):
        """How the work went, before what the work was."""
        marker = retro.render_marker(
            retro.Digest(summary="SUM", retrospective="RETRO"), 12)
        self.assertLess(marker["content"].index("RETRO"),
                        marker["content"].index("SUM"))

    def test_everything_is_plain_text(self):
        """Reasoning parts are only valid on an assistant message, and
        this is the message replacing a span of transcript. A live run
        died on a schema error with a good retrospective inside it."""
        marker = retro.render_marker(
            retro.Digest(summary="SUM", retrospective="RETRO"), 12)
        self.assertIsInstance(marker["content"], str)
        self.assertEqual(marker["role"], "system")
        self.assertNotIn("thinking", marker)

    def test_says_how_many_were_elided(self):
        marker = retro.render_marker(retro.Digest(summary="s"), 12)
        self.assertIn("12 earlier message(s)", marker["content"])

    def test_carries_the_digest_for_the_next_compaction(self):
        marker = retro.render_marker(
            retro.Digest(summary="SUM", retrospective="RETRO",
                         generation=3), 12)
        self.assertEqual(marker["_compaction_generation"], 3)
        found = retro.previous_digest([{"role": "user"}, marker])
        self.assertEqual(found.summary, "SUM")
        self.assertEqual(found.retrospective, "RETRO")
        self.assertEqual(found.generation, 3)

    def test_no_previous_digest_is_none(self):
        self.assertIsNone(retro.previous_digest([{"role": "user"}]))


class TestCompactionIntegration(unittest.TestCase):
    """budget.compact_messages hands the dropped span to a marker
    factory, because that is the only moment those turns still exist."""

    def _history(self):
        msgs = [{"role": "system", "content": "instructions"},
                {"role": "user", "content": "the question"}]
        for i in range(30):
            msgs.append(_turn(f"reasoning {i} " * 200,
                              [("read_file", f"m{i}.py")]))
            msgs.append(_result("tc0", "content " * 300))
        return msgs

    def test_factory_receives_the_dropped_span(self):
        seen = {}

        def factory(dropped, generation):
            seen["n"] = len(dropped)
            seen["gen"] = generation
            return {"role": "system", "content": "digest",
                    "_compaction_marker": True}

        result = budget.compact_messages(
            self._history(), keep_tokens=8_000, make_marker=factory)
        self.assertTrue(result.changed)
        self.assertGreater(seen["n"], 0)
        self.assertEqual(seen["gen"], 1)
        self.assertEqual(result.messages[result.marker_index]["content"],
                         "digest")

    def test_dropped_messages_are_returned(self):
        result = budget.compact_messages(self._history(), keep_tokens=8_000)
        self.assertEqual(len(result.dropped_messages), result.dropped)

    def test_a_raising_factory_falls_back_to_the_bare_note(self):
        """A digest that cannot be written must not block the compaction
        it was supposed to enrich."""
        def factory(dropped, generation):
            raise RuntimeError("summariser exploded")

        result = budget.compact_messages(
            self._history(), keep_tokens=8_000, make_marker=factory)
        self.assertTrue(result.changed)
        self.assertIn("elided",
                      result.messages[result.marker_index]["content"])

    def test_a_factory_returning_none_falls_back(self):
        result = budget.compact_messages(
            self._history(), keep_tokens=8_000,
            make_marker=lambda d, g: None)
        self.assertIn("elided",
                      result.messages[result.marker_index]["content"])

    def test_second_compaction_reports_generation_two(self):
        first = budget.compact_messages(self._history(), keep_tokens=8_000)
        grown = first.messages + [
            _turn(f"more {i} " * 300, [("grep", "x")]) for i in range(20)]
        seen = {}

        def factory(dropped, generation):
            seen["gen"] = generation
            return None

        budget.compact_messages(grown, keep_tokens=8_000,
                                make_marker=factory)
        self.assertEqual(seen["gen"], 2)


if __name__ == "__main__":
    unittest.main()


class TestInputIsBounded(unittest.TestCase):
    """A digest that overflows the window is a digest that never
    arrives — and the span being dropped is by definition the part that
    did not fit."""

    def _big_span(self):
        msgs = []
        for i in range(60):
            msgs.append(_turn(f"reasoning turn {i} " * 300,
                              [("read_file", f"m{i}.py")]))
            msgs.append(_result("tc0", "x" * 4000))
        return msgs

    def test_retrospective_input_is_capped(self):
        full = retro.build_retrospective_prompt(self._big_span())
        capped = retro.build_retrospective_prompt(
            self._big_span(), max_input_chars=20_000)
        self.assertGreater(len(full), len(capped))
        self.assertLess(len(capped), 30_000)

    def test_summary_input_is_capped(self):
        capped = retro.build_summary_prompt(
            self._big_span(), max_input_chars=10_000)
        self.assertLess(len(capped), 20_000)

    def test_the_most_recent_turns_are_the_ones_kept(self):
        """They are what 'in progress' and 'next' are written from; the
        oldest are what a previous digest already covers."""
        text = retro.serialize_reasoning_with_outcomes(
            self._big_span(), max_chars=20_000)
        self.assertIn("reasoning turn 59", text)
        self.assertNotIn("reasoning turn 0 ", text)

    def test_omission_is_announced(self):
        text = retro.serialize_reasoning_with_outcomes(
            self._big_span(), max_chars=20_000)
        self.assertIn("turn(s) omitted", text)

    def test_a_span_that_fits_is_untouched(self):
        small = [_turn("brief reasoning", [("grep", "x")])]
        self.assertEqual(
            retro.serialize_reasoning_with_outcomes(small, max_chars=100_000),
            retro.serialize_reasoning_with_outcomes(small))

    def test_write_bounds_both_phases(self):
        client = _Client("s", "r")
        retro.write(self._big_span(), chat_client=client, model="m",
                    target_tokens=8_000)
        for payload in client.payloads:
            self.assertLess(len(payload["messages"][1]["content"]), 30_000)
