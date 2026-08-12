"""Capped-thinking notes — claude_hooks/capped_thinking.py.

The behaviour this replaces: a model that hits its thinking budget stops
mid-sentence, and the council's agent loop then strips the reasoning
entirely, so the next iteration starts the same reasoning from the
beginning. Observed in the fork as the same ground covered a dozen
times, each pass ending at the same cap.
"""

import unittest

from claude_hooks import capped_thinking as ct


def _msg(thinking="", content=None, tool_calls=None):
    m = {"role": "assistant", "content": content}
    if thinking:
        m["thinking"] = thinking
    if tool_calls:
        m["tool_calls"] = tool_calls
    return m


class TestReadingTheTurn(unittest.TestCase):
    def test_thinking_read_under_every_spelling(self):
        for key in ("thinking", "reasoning", "reasoning_content"):
            self.assertEqual(
                ct.thinking_text({"role": "assistant", key: "the think"}),
                "the think")

    def test_block_form_thinking(self):
        m = {"role": "assistant",
             "content": [{"type": "thinking", "thinking": "block think"}]}
        self.assertEqual(ct.thinking_text(m), "block think")

    def test_produced_characters_counts_everything_written(self):
        m = _msg(thinking="t" * 100, content="c" * 50,
                 tool_calls=[{"function": {"name": "read_file",
                                           "arguments": '{"path":"a"}'}}])
        self.assertGreater(ct.produced_characters(m), 150)

    def test_measured_beats_estimated_when_the_turn_reported(self):
        """The ratio between characters produced and tokens counted is
        that turn's own, and needs no assumption about tokenizing."""
        m = _msg(thinking="t" * 900, content="c" * 100)
        measured = ct.measured_thinking_tokens(m, "t" * 900,
                                               completion_tokens=500)
        self.assertEqual(measured, 450)   # 900/1000 of 500

    def test_estimate_is_the_fallback_and_uses_the_reasoning_ratio(self):
        """Comparing a request-wide estimate against a budget in the
        model's own tokens is what made the fork's first version never
        fire: a turn that spent all 16,000 measured as ~10,300."""
        m = _msg(thinking="t" * 2700)
        est = ct.measured_thinking_tokens(m, "t" * 2700,
                                          completion_tokens=None)
        self.assertGreater(est, 900)


class TestBudgetMessage(unittest.TestCase):
    def test_marker_is_the_longest_line_not_the_first(self):
        """A Modelfile message can open with blank lines, and a
        user-typed one with a word short enough to appear in ordinary
        reasoning."""
        msg = "\n\nStop.\nYou have exhausted your thinking budget now."
        self.assertEqual(ct.budget_message_marker(msg),
                         "You have exhausted your thinking budget now.")

    def test_detects_the_message_at_the_end(self):
        budget = "You have run out of thinking budget."
        self.assertTrue(ct.ends_with_budget_message(
            "reasoning reasoning... " + budget, budget))

    def test_layout_differences_do_not_matter(self):
        """A Modelfile writes line breaks as escapes, a user types real
        ones, and the server streams whatever it streams."""
        self.assertTrue(ct.ends_with_budget_message(
            "thinking...\nYou have   run out\nof thinking budget.",
            "You have run out of thinking budget."))

    def test_discussing_the_budget_mid_think_is_not_running_out(self):
        long_tail = " and then I kept reasoning for a long while. " * 40
        self.assertFalse(ct.ends_with_budget_message(
            "I should watch my thinking budget here." + long_tail,
            "I should watch my thinking budget here."))

    def test_no_message_means_no_marker(self):
        self.assertFalse(ct.ends_with_budget_message("anything", None))
        self.assertFalse(ct.ends_with_budget_message("anything", ""))


class TestDegenerateNote(unittest.TestCase):
    """A note goes into the thinking channel as the model's own
    reasoning, so thirty near-identical lines read as thirty things it
    thought. No note is strictly better."""

    def test_exact_repeats(self):
        self.assertTrue(ct.is_degenerate_note(
            "\n".join(["Summary-of-thought process:"] * 10)))

    def test_near_identical_openings(self):
        """'Summary-of-thought process:' and 'Summary of the task at
        hand is at the thought process:' are different strings and the
        same failure."""
        lines = [
            "Summary of thought process: the first thing",
            "Summary of thought process: another thing entirely",
            "Summary of thought process: a third observation",
            "Summary of thought process: a fourth remark",
            "Summary of thought process here is a fifth",
            "Summary of thought process here is a sixth",
        ]
        # Six distinct lines, so the exact-repeat test passes them; they
        # collide only on their opening, which is the second test's job.
        self.assertEqual(len(set(lines)), len(lines))
        self.assertTrue(ct.is_degenerate_note("\n".join(lines)))

    def test_one_phrase_repeated(self):
        self.assertTrue(ct.is_degenerate_note(
            "the metric counts rows " * 20))

    def test_real_prose_is_not_degenerate(self):
        note = (
            "The aggregation happens before normalisation, which is why "
            "the two cases in issue 214 cannot be told apart. I ruled out "
            "the tokenizer because eval.py:88 counts rows, not tokens, and "
            "I ruled out the loader because the same figure appears when "
            "it is bypassed entirely. What is still open is whether the "
            "harness or the metric owns the fix; next I read metric.py "
            "around line 140."
        )
        self.assertFalse(ct.is_degenerate_note(note))

    def test_short_notes_are_not_judged(self):
        self.assertFalse(ct.is_degenerate_note("Settled: it is the loader."))

    def test_empty_is_not_degenerate(self):
        self.assertFalse(ct.is_degenerate_note(""))


class TestDetect(unittest.TestCase):
    def test_budget_message_needs_no_threshold(self):
        budget = "You have run out of thinking budget."
        capped, reason = ct.detect(_msg(thinking="short think " + budget),
                                   budget_message=budget)
        self.assertIsNotNone(capped)
        self.assertEqual(capped.evidence, "budget-message")

    def test_proximity_fires_at_ninety_percent(self):
        """A budget is enforced on whole tokens as they are produced, so
        the last few are never spent."""
        capped, _ = ct.detect(_msg(thinking="t" * 1000),
                              budget_tokens=1000, completion_tokens=950)
        self.assertIsNotNone(capped)
        self.assertEqual(capped.evidence, "cap-proximity")

    def test_reasoning_within_budget_stands_down(self):
        capped, reason = ct.detect(_msg(thinking="t" * 1000),
                                   budget_tokens=10_000,
                                   completion_tokens=200)
        self.assertIsNone(capped)
        self.assertEqual(reason, "reasoning-within-budget")

    def test_a_turn_that_did_not_reason_stands_down(self):
        capped, reason = ct.detect(_msg(content="just an answer"),
                                   budget_tokens=1000)
        self.assertIsNone(capped)
        self.assertEqual(reason, "turn-did-not-reason")

    def test_no_budget_and_no_message_stands_down(self):
        capped, reason = ct.detect(_msg(thinking="t" * 1000))
        self.assertIsNone(capped)
        self.assertEqual(reason, "no-budget")

    def test_stand_down_reasons_are_named(self):
        """Every one of these has been mistaken for 'the feature is
        broken'; from the outside they are indistinguishable."""
        for _, reason in (
            ct.detect(_msg(content="x")),
            ct.detect(_msg(thinking="t")),
            ct.detect(_msg(thinking="t" * 100), budget_tokens=10 ** 6,
                      completion_tokens=1),
        ):
            self.assertIn(reason, ct.STAND_DOWN_REASONS)


class _Client:
    def __init__(self, content, raises=False):
        self.content = content
        self.raises = raises
        self.payloads = []

    def chat(self, payload):
        self.payloads.append(payload)
        if self.raises:
            raise RuntimeError("upstream down")
        return {"choices": [{"message": {"content": self.content}}]}


class TestCondense(unittest.TestCase):
    def setUp(self):
        self.capped = ct.CappedThinking(
            thinking="t" * 5000, thinking_tokens=1800,
            budget_tokens=2000, evidence="cap-proximity")

    def test_produces_a_note(self):
        client = _Client("I have settled that the loader is not at fault.")
        note, reason = ct.condense(self.capped, chat_client=client, model="m")
        self.assertIn("loader", note)
        self.assertEqual(reason, "")

    def test_asks_for_no_reasoning(self):
        """A summariser that reasons spends the whole budget on thinking
        and returns no text — which is how the fixed cap got removed the
        first time."""
        client = _Client("note")
        ct.condense(self.capped, chat_client=client, model="m")
        self.assertIs(client.payloads[0]["think"], False)

    def test_bounds_its_own_output(self):
        client = _Client("note")
        ct.condense(self.capped, chat_client=client, model="m")
        self.assertEqual(client.payloads[0]["options"]["num_predict"],
                         ct.CONDENSED_THINKING_MAX_OUTPUT_TOKENS)

    def test_degenerate_note_is_dropped(self):
        client = _Client("\n".join(["Summary-of-thought process:"] * 10))
        note, reason = ct.condense(self.capped, chat_client=client, model="m")
        self.assertIsNone(note)
        self.assertEqual(reason, "condensation-degenerate")

    def test_empty_note_is_dropped(self):
        note, reason = ct.condense(self.capped, chat_client=_Client("  "),
                                   model="m")
        self.assertIsNone(note)
        self.assertEqual(reason, "condensation-empty")

    def test_a_failed_call_never_raises(self):
        """Without this module the turn re-derives; a condensation that
        fails is strictly no worse."""
        note, reason = ct.condense(
            self.capped, chat_client=_Client("x", raises=True), model="m")
        self.assertIsNone(note)
        self.assertEqual(reason, "condensation-failed")

    def test_no_client_stands_down(self):
        note, reason = ct.condense(self.capped, chat_client=None, model="m")
        self.assertIsNone(note)
        self.assertEqual(reason, "no-condenser")

    def test_runaway_note_is_bounded(self):
        """The char guard, not the degeneration guard: a note can be
        varied prose and still be far too long to hand back."""
        varied = " ".join(
            f"observation {i} about module_{i}.py line {i * 7}"
            for i in range(4000))
        self.assertFalse(ct.is_degenerate_note(varied))
        note, _ = ct.condense(self.capped, chat_client=_Client(varied),
                              model="m")
        self.assertIsNotNone(note)
        self.assertLessEqual(len(note),
                             ct.CONDENSED_THINKING_MAX_CHARS + 100)

    def test_tool_outcomes_reach_the_prompt(self):
        client = _Client("note")
        ct.condense(self.capped, chat_client=client, model="m",
                    tool_outcomes=[{"name": "grep", "input": "x",
                                    "result": "3 matches"}])
        user = client.payloads[0]["messages"][1]["content"]
        self.assertIn("grep", user)
        self.assertIn("3 matches", user)


class TestLeadIn(unittest.TestCase):
    def test_note_is_first_person_continuation_not_a_document(self):
        """A line announcing 'this is the note you left yourself' turns
        the model's own reasoning into a document handed to it, and a
        document gets checked rather than continued."""
        block = ct.as_thinking_block("the note")
        self.assertTrue(block.startswith("I have already reasoned"))
        self.assertIn("the note", block)


if __name__ == "__main__":
    unittest.main()
