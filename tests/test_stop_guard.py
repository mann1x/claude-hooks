"""Tests for the stop-phrase guard."""

import unittest

from claude_hooks.stop_guard import DEFAULT_PATTERNS, check_message, load_patterns


class StopGuardTests(unittest.TestCase):
    def test_ownership_dodging_triggers(self):
        patterns = load_patterns([])
        cases = [
            "This is a pre-existing issue, not from my changes.",
            "The test was already failing before my changes.",
            "That appears to be an existing bug.",
        ]
        for msg in cases:
            self.assertIsNotNone(
                check_message(msg, patterns), f"expected match on: {msg!r}"
            )

    def test_session_quitting_triggers(self):
        patterns = load_patterns([])
        cases = [
            "This is a good stopping point — we can continue in the next session.",
            "Given the length of this session, let me pause here.",
            "We can come back to this later.",
        ]
        for msg in cases:
            self.assertIsNotNone(
                check_message(msg, patterns), f"expected match on: {msg!r}"
            )

    def test_permission_seeking_triggers(self):
        patterns = load_patterns([])
        cases = [
            "Should I continue with the next step?",
            "Would you like me to keep going?",
            "Shall I proceed with the migration?",
        ]
        for msg in cases:
            self.assertIsNotNone(
                check_message(msg, patterns), f"expected match on: {msg!r}"
            )

    def test_safe_messages_pass_through(self):
        patterns = load_patterns([])
        cases = [
            "Done. All tests pass.",
            "I've refactored the authentication module and added tests.",
            "Fixed the parser — it was mishandling empty input.",
            "",
        ]
        for msg in cases:
            self.assertIsNone(
                check_message(msg, patterns), f"unexpected match on: {msg!r}"
            )

    def test_custom_patterns_override_defaults(self):
        custom = [{"pattern": r"\bfrobnicate\b", "correction": "Do not frobnicate."}]
        patterns = load_patterns(custom)
        self.assertIsNotNone(check_message("We should frobnicate this.", patterns))
        # Default patterns are NOT active when custom is supplied.
        self.assertIsNone(check_message("pre-existing issue.", patterns))

    def test_empty_custom_falls_back_to_defaults(self):
        patterns = load_patterns([])
        # "pre-existing" is a default-matched phrase
        self.assertIsNotNone(check_message("pre-existing bug.", patterns))

    def test_bad_regex_skipped(self):
        custom = [
            {"pattern": "[invalid(", "correction": "bad"},
            {"pattern": r"\bgood\b", "correction": "good match"},
        ]
        patterns = load_patterns(custom)
        # Bad pattern dropped; good pattern still works.
        self.assertEqual(len(patterns), 1)
        self.assertIsNotNone(check_message("good day", patterns))

    def test_default_patterns_are_nonempty(self):
        self.assertGreater(len(DEFAULT_PATTERNS), 5)

    def test_case_insensitive(self):
        patterns = load_patterns([])
        self.assertIsNotNone(check_message("PRE-EXISTING issue", patterns))
        self.assertIsNotNone(check_message("Pre-Existing Issue", patterns))


class MetaContextEscapeTests(unittest.TestCase):
    """Option B — skip the check when the message is meta-discussion."""

    def setUp(self):
        self.patterns = load_patterns([])

    def test_match_only_in_double_quotes_is_skipped(self):
        msg = 'For example, "This is a pre-existing issue" would trigger the block.'
        self.assertIsNone(check_message(msg, self.patterns))

    def test_match_in_single_quotes_is_skipped(self):
        msg = "An example phrase 'pre-existing issue' shows the trigger phrase rule."
        self.assertIsNone(check_message(msg, self.patterns))

    def test_match_in_backticks_is_skipped(self):
        msg = "The guard fires on `pre-existing` as a trigger phrase."
        self.assertIsNone(check_message(msg, self.patterns))

    def test_real_match_outside_quotes_still_triggers(self):
        msg = ('We saw "some example" but honestly the test failure is a '
               'pre-existing issue not from my changes.')
        self.assertIsNotNone(check_message(msg, self.patterns))

    def test_meta_marker_alone_skips_even_unquoted(self):
        msg = "Testing the hook — pre-existing fires the stop_guard rule."
        self.assertIsNone(check_message(msg, self.patterns))

    def test_skip_meta_context_disabled_restores_raw(self):
        msg = 'For example, "pre-existing issue" would trigger.'
        self.assertIsNotNone(
            check_message(msg, self.patterns, skip_meta_context=False)
        )

    def test_custom_meta_markers(self):
        msg = "pre-existing issue — this is a DEMO phrase."
        self.assertIsNotNone(check_message(msg, self.patterns))
        self.assertIsNone(
            check_message(
                msg,
                self.patterns,
                meta_markers=("DEMO phrase",),
            )
        )


if __name__ == "__main__":
    unittest.main()
