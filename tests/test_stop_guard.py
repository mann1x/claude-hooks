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


class UserWrapUpEscapeTests(unittest.TestCase):
    """User-intent escape — the single most important guard exception."""

    def setUp(self):
        self.patterns = load_patterns([])

    def test_user_compact_request_bypasses(self):
        out = check_message(
            "Good stopping point — I'll continue in the next session.",
            self.patterns,
            last_user_message="I need to compact the context",
        )
        self.assertIsNone(out)

    def test_user_wrapup_command_bypasses(self):
        for phrase in ["/wrapup", "wrap up for now", "let's close this session",
                        "we'll continue another time", "save state"]:
            out = check_message(
                "This session has gotten long — good stopping point.",
                self.patterns,
                last_user_message=phrase,
            )
            self.assertIsNone(out, f"wrapup phrase should bypass: {phrase!r}")

    def test_guard_fires_without_user_wrapup(self):
        out = check_message(
            "Good stopping point — continue in the next session.",
            self.patterns,
            last_user_message="please keep going",
        )
        self.assertIsNotNone(out)

    def test_skip_disabled_still_fires(self):
        out = check_message(
            "This is a pre-existing issue.",
            self.patterns,
            last_user_message="let's wrap up",
            skip_on_user_wrap_up=False,
        )
        self.assertIsNotNone(out)

    def test_custom_user_markers(self):
        out = check_message(
            "pre-existing issue.",
            self.patterns,
            last_user_message="ABORT NOW please",
            user_wrap_up_markers=("ABORT NOW",),
        )
        self.assertIsNone(out)
        # Without the override the default marker list doesn't include
        # "ABORT NOW", so the guard fires normally.
        out2 = check_message(
            "pre-existing issue.",
            self.patterns,
            last_user_message="ABORT NOW please",
        )
        self.assertIsNotNone(out2)

    def test_no_user_message_falls_through(self):
        # Without a user message the guard still checks the assistant text.
        out = check_message(
            "pre-existing issue.",
            self.patterns,
            last_user_message=None,
        )
        self.assertIsNotNone(out)

    def test_case_insensitive_match(self):
        out = check_message(
            "good stopping point",
            self.patterns,
            last_user_message="I Need To Compact The Context",
        )
        self.assertIsNone(out)


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


def _assistant_msg(text: str, *, stop_reason: str = "end_turn",
                   tool_uses=None) -> dict:
    """Build a transcript-shape assistant message dict for the stall check."""
    content: list = []
    if text:
        content.append({"type": "text", "text": text})
    for name in tool_uses or []:
        content.append({"type": "tool_use", "name": name, "input": {}})
    return {
        "message": {
            "role": "assistant",
            "stop_reason": stop_reason,
            "content": content,
        }
    }


class StallAfterCommitmentTests(unittest.TestCase):
    """Tests for stop_guard.check_stall_after_commitment.

    The check has to be tight enough to never block a real productive
    turn (which always has tool_use blocks) but catch the specific
    failure mode where the model commits in prose and then end_turns.
    """

    def setUp(self):
        from claude_hooks.stop_guard import reset_commitment_cache
        reset_commitment_cache()

    # --- Positive cases (should fire) ---
    def test_diving_in_now_no_tools_blocks(self):
        from claude_hooks.stop_guard import check_stall_after_commitment, STALL_CORRECTION
        msg = _assistant_msg(
            "I'll smoke-test on one variant first, then run the full "
            "sweep (~6-8h GPU). Diving in now."
        )
        self.assertEqual(check_stall_after_commitment(msg), STALL_CORRECTION)

    def test_writing_the_script_now_blocks(self):
        from claude_hooks.stop_guard import check_stall_after_commitment
        msg = _assistant_msg("Got it — writing the script now.")
        self.assertIsNotNone(check_stall_after_commitment(msg))

    def test_let_me_start_blocks(self):
        from claude_hooks.stop_guard import check_stall_after_commitment
        msg = _assistant_msg(
            "The plan is solid. Let me start writing the implementation."
        )
        self.assertIsNotNone(check_stall_after_commitment(msg))

    def test_bare_on_it_at_end_blocks(self):
        from claude_hooks.stop_guard import check_stall_after_commitment
        msg = _assistant_msg("Sounds good. On it.")
        self.assertIsNotNone(check_stall_after_commitment(msg))

    def test_kicking_off_now_blocks(self):
        from claude_hooks.stop_guard import check_stall_after_commitment
        msg = _assistant_msg(
            "Pre-flight checks pass. Kicking it off now."
        )
        self.assertIsNotNone(check_stall_after_commitment(msg))

    # --- Negative: tool_use present means real action happened ---
    def test_skipped_when_tool_use_present(self):
        from claude_hooks.stop_guard import check_stall_after_commitment
        msg = _assistant_msg(
            "Diving in now.",
            stop_reason="tool_use",
            tool_uses=["Bash"],
        )
        self.assertIsNone(check_stall_after_commitment(msg))

    # --- Negative: stop_reason != end_turn ---
    def test_skipped_when_stop_reason_max_tokens(self):
        from claude_hooks.stop_guard import check_stall_after_commitment
        msg = _assistant_msg("Diving in now.", stop_reason="max_tokens")
        self.assertIsNone(check_stall_after_commitment(msg))

    def test_accepts_missing_stop_reason(self):
        """Some daemon-buffered transcripts omit stop_reason; treat as end_turn."""
        from claude_hooks.stop_guard import check_stall_after_commitment
        msg = _assistant_msg("Diving in now.", stop_reason=None)
        self.assertIsNotNone(check_stall_after_commitment(msg))

    # --- Negative: commitment too far from end ---
    def test_skipped_when_commitment_in_early_paragraph(self):
        """A commitment phrase from earlier in a long message that ends
        on a different topic should NOT fire — the model committed,
        moved on, and presumably did the work / changed plan."""
        from claude_hooks.stop_guard import check_stall_after_commitment
        long_lead = "Diving in now. " + ("A" * 400)  # commitment is buried
        msg = _assistant_msg(long_lead + " Final summary unrelated.")
        self.assertIsNone(check_stall_after_commitment(msg))

    # --- Negative: empty / no text ---
    def test_skipped_when_no_text(self):
        from claude_hooks.stop_guard import check_stall_after_commitment
        msg = {"message": {"role": "assistant", "stop_reason": "end_turn",
                           "content": []}}
        self.assertIsNone(check_stall_after_commitment(msg))

    def test_skipped_when_msg_is_none(self):
        from claude_hooks.stop_guard import check_stall_after_commitment
        self.assertIsNone(check_stall_after_commitment(None))

    # --- Negative: user wrap-up bypass ---
    def test_skipped_when_user_asked_to_wrap_up(self):
        from claude_hooks.stop_guard import check_stall_after_commitment
        msg = _assistant_msg("All done. On it.")
        self.assertIsNone(check_stall_after_commitment(
            msg, last_user_message="let's wrap up for now"))

    # --- Thinking blocks ---
    def test_thinking_only_does_not_match(self):
        """Thinking blocks aren't user-visible text — a turn with only
        thinking + no user text + no tools is a different (rare) failure
        but not the stall pattern this check targets."""
        from claude_hooks.stop_guard import check_stall_after_commitment
        msg = {
            "message": {
                "role": "assistant",
                "stop_reason": "end_turn",
                "content": [{"type": "thinking",
                             "thinking": "...let me dive in now..."}],
            }
        }
        self.assertIsNone(check_stall_after_commitment(msg))

    def test_text_plus_thinking_uses_text_for_match(self):
        """When both thinking and text are present, only text matters."""
        from claude_hooks.stop_guard import check_stall_after_commitment
        msg = {
            "message": {
                "role": "assistant",
                "stop_reason": "end_turn",
                "content": [
                    {"type": "thinking", "thinking": "no commitment here"},
                    {"type": "text", "text": "Plan looks good. Diving in now."},
                ],
            }
        }
        self.assertIsNotNone(check_stall_after_commitment(msg))

    # --- Negative: ordinary closing prose ---
    def test_does_not_fire_on_summary_only_message(self):
        from claude_hooks.stop_guard import check_stall_after_commitment
        msg = _assistant_msg(
            "Here's the summary: file X was modified, tests pass, "
            "deployment is up. Let me know if you want any changes."
        )
        self.assertIsNone(check_stall_after_commitment(msg))


# --------------------------------------------------------------------------- #
# 2026-05-18: five new stall events collected from real solidPC sessions
# (M26/M27, T17.3, Step 1 scripts, Phase 1 fan-out, _maybe_start_store_reaper
# mid-edit). Each test pins the exact tail prose from the event so a future
# regex tightening can be regressed against the captured shape.
# --------------------------------------------------------------------------- #
class StallAfterCommitmentLiveEventsTests(unittest.TestCase):
    """Live stall events captured pre-v1.7.x release cut."""

    def setUp(self):
        from claude_hooks.stop_guard import reset_commitment_cache
        reset_commitment_cache()

    # --- Event 1: M26/M27 "Going to update X, log Y, then start Z" ---
    def test_event1_going_to_update_log_then_start_blocks(self):
        from claude_hooks.stop_guard import check_stall_after_commitment
        msg = _assistant_msg(
            "Logging this as a major finding — it invalidates the whole "
            "off-policy KL on cache approach for cross-vocab and means the "
            "only viable cross-vocab paths remain: On-policy KL (M6b: 53.0), "
            "Same-vocab SFT (M16: 58.5), M27 GRPO+KL hybrid (still untested). "
            "Going to update STYLE_SHIFT_ISSUE.md with the M26 finding, log "
            "the result to RESULTS.md, then start M27."
        )
        self.assertIsNotNone(check_stall_after_commitment(msg))

    # --- Event 2: T17.3 "Now drafting X" ---
    def test_event2_now_drafting_blocks(self):
        from claude_hooks.stop_guard import check_stall_after_commitment
        msg = _assistant_msg(
            "Now drafting T17.3 mapping script in the meantime — when probe "
            "lands I'll surface the diagnosis and we can decide whether to "
            "launch T17.3 immediately."
        )
        self.assertIsNotNone(check_stall_after_commitment(msg))

    # --- Event 3a: "Writing the three Step 1 scripts now:" ---
    def test_event3a_writing_X_now_with_multiword_object_blocks(self):
        """The pre-#218 pattern required strict adjacency
        ``writing the {script|code|...}``; this multi-word object form
        slipped through. The new tail-end "verb … now[:.]\\s*$" pattern
        catches it."""
        from claude_hooks.stop_guard import check_stall_after_commitment
        msg = _assistant_msg(
            "The router is three tensors (Gemma 4's scale + proj + "
            "per_expert_scale decomposition). Config keys are non-standard. "
            "Writing the three Step 1 scripts now:"
        )
        self.assertIsNotNone(check_stall_after_commitment(msg))

    # --- Event 3b: "Now writing the orchestrator wrapper" ---
    def test_event3b_now_writing_orchestrator_blocks(self):
        from claude_hooks.stop_guard import check_stall_after_commitment
        msg = _assistant_msg(
            "All three Step 1 scripts syntactically valid. Sweep alive 1h34m. "
            "Now writing the orchestrator wrapper that applies Step 1 + "
            "re-smokes a variant, and a Step 2 stub for EAC-MoE (full "
            "implementation deferred until sweep results inform whether we "
            "need it):"
        )
        self.assertIsNotNone(check_stall_after_commitment(msg))

    # --- Event 4: "Starting with Phase 1: two parallel Explore agents" ---
    def test_event4_starting_with_phase_blocks(self):
        from claude_hooks.stop_guard import check_stall_after_commitment
        msg = _assistant_msg(
            "Continuing — the existing plan file is for M11c (now complete "
            "and committed as 235fe6c), so I'll overwrite it with a fresh "
            "plan for #103 Option 2. Starting with Phase 1: two parallel "
            "Explore agents to map the current wiring before I draft the "
            "refactor."
        )
        self.assertIsNotNone(check_stall_after_commitment(msg))

    # --- Event 5: "Now add the helper. Let me insert it..." ---
    def test_event5_now_add_and_let_me_insert_blocks(self):
        from claude_hooks.stop_guard import check_stall_after_commitment
        msg = _assistant_msg(
            "Now add the _maybe_start_store_reaper helper. Let me insert "
            "it near _start_idle_reaper:"
        )
        self.assertIsNotNone(check_stall_after_commitment(msg))

    # --- Critical negatives — must NOT fire on these ---
    def test_negative_now_examining_does_not_fire(self):
        """Read verbs (examine, check, search, look) without tool calls
        are fine — the model is thinking aloud, not committing to action."""
        from claude_hooks.stop_guard import check_stall_after_commitment
        msg = _assistant_msg(
            "Now examining the structure of consultants/engine/graph.py to "
            "understand the routing logic before proposing a change."
        )
        self.assertIsNone(check_stall_after_commitment(msg))

    def test_negative_continuing_research_does_not_fire(self):
        """Bare 'Continuing' / 'Continuing the X' is too broad to anchor on."""
        from claude_hooks.stop_guard import check_stall_after_commitment
        msg = _assistant_msg(
            "Continuing the research into how Pregel dispatches Send "
            "operations — looking at the additive reducer for tool_results."
        )
        self.assertIsNone(check_stall_after_commitment(msg))

    def test_negative_past_tense_does_not_fire(self):
        """Past-tense 'I wrote / implemented / added' is reporting, not committing."""
        from claude_hooks.stop_guard import check_stall_after_commitment
        msg = _assistant_msg(
            "I wrote the helper and inserted it near _start_idle_reaper. "
            "All tests pass. Ready for review."
        )
        self.assertIsNone(check_stall_after_commitment(msg))

    def test_negative_starting_with_phase_no_colon_does_not_fire(self):
        """The Starting-with-Phase pattern requires the colon to fire —
        bare 'starting with Phase 1' as conditional planning shouldn't."""
        from claude_hooks.stop_guard import check_stall_after_commitment
        msg = _assistant_msg(
            "We could approach this two ways: starting with Phase 1 would "
            "be too risky given the sweep is still alive, so let's wait."
        )
        self.assertIsNone(check_stall_after_commitment(msg))

    def test_negative_going_to_with_non_action_verb_does_not_fire(self):
        """'Going to think about X' / 'Going to consider Y' aren't tool
        commitments — only action verbs from the curated list fire."""
        from claude_hooks.stop_guard import check_stall_after_commitment
        msg = _assistant_msg(
            "Going to think about this overnight before deciding on the "
            "approach. The trade-offs aren't obvious."
        )
        self.assertIsNone(check_stall_after_commitment(msg))


if __name__ == "__main__":
    unittest.main()
