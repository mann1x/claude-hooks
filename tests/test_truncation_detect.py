"""Truncation detection — claude_hooks/truncation.py.

The regression these lock down is csl-2026-08-12-0831-905a: a 43-minute
council whose 655-character final answer ended ``of $\\text{`` and was
filed as ``status: completed, error: null``.
"""

import unittest

from claude_hooks.truncation import (
    Truncation, classify, unterminated_construct,
)


def _resp(content, *, done_reason=None, finish_reason=None,
          completion_tokens=0):
    choice = {"message": {"content": content}}
    if done_reason is not None:
        choice["done_reason"] = done_reason
    if finish_reason is not None:
        choice["finish_reason"] = finish_reason
    out = {"choices": [choice]}
    if completion_tokens:
        out["usage"] = {"completion_tokens": completion_tokens}
    return out


class TestReportedTruncation(unittest.TestCase):
    def test_done_reason_length_is_truncation(self):
        t = classify(_resp("a complete-looking sentence.",
                           done_reason="length"))
        self.assertIsNotNone(t)
        self.assertEqual(t.kind, "reported")

    def test_done_reason_stop_is_clean(self):
        self.assertIsNone(
            classify(_resp("a complete sentence.", done_reason="stop")))

    def test_reported_wins_at_any_length(self):
        """Short content still counts. The structural floor exists to
        stop false positives on brief replies, not to make a backend's
        explicit 'I was cut' unreadable."""
        t = classify(_resp("Yes", done_reason="length"))
        self.assertIsNotNone(t)
        self.assertEqual(t.kind, "reported")

    def test_finish_reason_length_without_done_reason(self):
        """OpenAI-shape backends report it under finish_reason."""
        t = classify(_resp("some text", finish_reason="length"))
        self.assertIsNotNone(t)
        self.assertEqual(t.kind, "reported")

    def test_tool_call_turn_reporting_length_is_truncated(self):
        """finish_reason='tool_calls' with done_reason='length' means
        the arguments may be half-written — more dangerous than cut
        prose, not less."""
        t = classify(_resp("", done_reason="length",
                           finish_reason="tool_calls"))
        self.assertIsNotNone(t)
        self.assertEqual(t.kind, "reported")

    def test_tool_call_turn_reporting_stop_is_clean(self):
        self.assertIsNone(
            classify(_resp("", done_reason="stop",
                           finish_reason="tool_calls")))

    def test_carries_usage_and_identity(self):
        t = classify(_resp("x" * 100, done_reason="length",
                           completion_tokens=656),
                     role="critic", model="gemma4:31b-cloud")
        self.assertEqual(t.role, "critic")
        self.assertEqual(t.model, "gemma4:31b-cloud")
        self.assertEqual(t.completion_tokens, 656)
        self.assertEqual(t.chars, 100)


class TestStructuralTruncation(unittest.TestCase):
    """The backstop that would have caught the incident even with the
    translator bug hiding done_reason."""

    def test_the_actual_incident_tail(self):
        text = ("The evaluation counts rows rather than tokens, so the "
                "metric does not count the **presence** of $\\text{")
        t = classify(_resp(text, done_reason="stop"))
        self.assertIsNotNone(t)
        self.assertEqual(t.kind, "structural")
        self.assertIn("$", t.detail)

    def test_the_critic_tail_unclosed_backtick(self):
        text = ("The harness treats each row independently, which means "
                "the aggregate is computed over `")
        t = classify(_resp(text, done_reason="stop"))
        self.assertIsNotNone(t)
        self.assertEqual(t.kind, "structural")

    def test_unclosed_code_fence(self):
        text = "Here is the fix:\n\n```python\ndef f():\n    return 1"
        self.assertEqual(unterminated_construct(text),
                         "unclosed ``` code fence")

    def test_closed_code_fence_is_clean(self):
        text = "Here is the fix:\n\n```python\ndef f():\n    return 1\n```\n"
        self.assertIsNone(unterminated_construct(text))

    def test_unclosed_emphasis(self):
        text = ("A reasonably long paragraph of ordinary prose that then "
                "ends on **")
        self.assertEqual(unterminated_construct(text),
                         "unclosed ** emphasis")

    def test_unclosed_latex_macro(self):
        self.assertIn("group", unterminated_construct(
            "The denominator is \\frac{1"))

    def test_closed_latex_macro_is_clean(self):
        self.assertIsNone(unterminated_construct(
            "The denominator is \\frac{1}{2} exactly."))

    def test_short_content_is_not_structurally_judged(self):
        """A brief intentional reply must not be called truncated."""
        self.assertIsNone(classify(_resp("Yes — `x`. No `")))

    def test_balanced_inline_code_is_clean(self):
        text = ("The function `resolve_in_roots` walks each root in turn "
                "and returns the first `path` that exists on disk.")
        self.assertIsNone(classify(_resp(text)))

    def test_math_across_paragraphs_is_clean(self):
        """Only the final line is scanned. Dollar signs used as currency
        earlier in a document must not read as an open math span."""
        text = ("The budget was $500 in the first quarter.\n\n"
                "That figure is unrelated to the metric under review "
                "and does not affect the conclusion.")
        self.assertIsNone(classify(_resp(text)))

    def test_currency_on_the_final_line_is_the_known_false_positive(self):
        """Documented, accepted cost: a single unpaired '$' on the last
        line reads as an open math span. It costs one continuation call
        and the continuation returns quickly; the alternative is missing
        the incident this module exists for."""
        text = ("A long enough closing line of prose that mentions $500 "
                "and then simply ends.")
        t = classify(_resp(text))
        self.assertIsNotNone(t)
        self.assertEqual(t.kind, "structural")

    def test_escaped_delimiters_do_not_count(self):
        text = ("A sufficiently long line of prose containing a literal "
                "\\` escaped backtick and nothing else of note.")
        self.assertIsNone(classify(_resp(text)))

    def test_normal_prose_ending_is_clean(self):
        text = ("The council reviewed the metric and concluded that the "
                "row-level aggregation is correct for this dataset.")
        self.assertIsNone(classify(_resp(text)))


class TestRobustness(unittest.TestCase):
    def test_non_dict_returns_none(self):
        for bad in (None, "text", 42, []):
            self.assertIsNone(classify(bad))

    def test_empty_response_returns_none(self):
        self.assertIsNone(classify({}))
        self.assertIsNone(classify({"choices": []}))

    def test_ollama_native_shape(self):
        """A caller that passes an untranslated response still works."""
        t = classify({"message": {"content": "x" * 80},
                      "done_reason": "length"})
        self.assertIsNotNone(t)

    def test_text_override_wins(self):
        """The agent loop concatenates across iterations; only it knows
        the final string."""
        whole = ("A complete concatenated answer that ends properly on "
                 "a full sentence with no open constructs.")
        self.assertIsNone(classify(_resp("cut off at `"), text=whole))

    def test_public_dict_omits_empty_fields(self):
        d = Truncation(kind="reported", detail="d", chars=5).public_dict()
        self.assertNotIn("role", d)
        self.assertNotIn("model", d)
        self.assertEqual(d["kind"], "reported")


if __name__ == "__main__":
    unittest.main()
