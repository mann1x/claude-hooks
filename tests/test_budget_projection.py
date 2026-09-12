"""Budget projection — claude_hooks/budget_projection.py.

Ported from the Cline fork's ``budget-projection/project.ts``. The
property that matters most is not "it reaches the target" but "what it
returns is still a valid conversation": a projection that severs an
assistant's tool call from the result answering it has not produced a
smaller request, it has produced a rejected one.
"""

import unittest

from claude_hooks import budget_projection as bp


def _user(text="a question"):
    return {"role": "user", "content": text}


def _assistant(text=None, thinking=None, calls=()):
    m = {"role": "assistant", "content": text}
    if thinking:
        m["thinking"] = thinking
    if calls:
        m["tool_calls"] = [
            {"id": cid, "type": "function",
             "function": {"name": name, "arguments": "{}"}}
            for cid, name in calls]
    return m


def _tool(cid, text="result"):
    return {"role": "tool", "tool_call_id": cid, "content": text}


class TestStructure(unittest.TestCase):
    def test_finds_the_typed_user_messages(self):
        msgs = [_user("first"), _assistant("a"), _tool("t1"), _user("last")]
        self.assertEqual(bp.first_typed_user_index(msgs), 0)
        self.assertEqual(bp.latest_typed_user_index(msgs), 3)

    def test_a_tool_result_is_not_a_typed_user_message(self):
        msgs = [_user("q"), _tool("t1")]
        self.assertEqual(bp.latest_typed_user_index(msgs), 0)

    def test_live_tail_is_the_unanswered_call(self):
        msgs = [_user(), _assistant(calls=[("a", "grep")]), _tool("a"),
                _assistant(calls=[("b", "read_file")])]
        self.assertEqual(bp.live_tail_start_index(msgs), 3)

    def test_no_live_tail_when_everything_is_answered(self):
        msgs = [_user(), _assistant(calls=[("a", "grep")]), _tool("a")]
        self.assertEqual(bp.live_tail_start_index(msgs), len(msgs))

    def test_partially_answered_turn_is_still_in_flight(self):
        msgs = [_user(),
                _assistant(calls=[("a", "grep"), ("b", "read_file")]),
                _tool("a")]
        self.assertEqual(bp.live_tail_start_index(msgs), 1)


class TestPairing(unittest.TestCase):
    def test_a_call_and_its_result_are_one_closure(self):
        msgs = [_user(), _assistant(calls=[("a", "grep")]), _tool("a")]
        self.assertEqual(bp.message_closure(msgs, 1), {1, 2})
        self.assertEqual(bp.message_closure(msgs, 2), {1, 2})

    def test_repeated_ids_across_turns_do_not_merge(self):
        """chat_client synthesizes ids from the call's index within its
        own message, so the first call of every turn is ``tc_0``.
        Matching on the id alone links the whole conversation into one
        closure, and dropping one message takes the entire history."""
        msgs = [_user(),
                _assistant(calls=[("tc_0", "grep")]), _tool("tc_0", "1"),
                _assistant(calls=[("tc_0", "grep")]), _tool("tc_0", "2"),
                _assistant(calls=[("tc_0", "grep")]), _tool("tc_0", "3")]
        self.assertEqual(bp.message_closure(msgs, 1), {1, 2})
        self.assertEqual(bp.message_closure(msgs, 3), {3, 4})
        self.assertEqual(bp.message_closure(msgs, 5), {5, 6})

    def test_a_result_binds_to_the_nearest_preceding_call(self):
        msgs = [_assistant(calls=[("x", "grep")]),
                _assistant(calls=[("x", "grep")]),
                _tool("x")]
        self.assertIn(1, bp.message_closure(msgs, 2))
        self.assertNotIn(0, bp.message_closure(msgs, 2))

    def test_multi_call_turn_takes_all_its_results(self):
        msgs = [_user(), _assistant(calls=[("a", "g"), ("b", "r")]),
                _tool("a"), _tool("b")]
        self.assertEqual(bp.message_closure(msgs, 1), {1, 2, 3})

    def test_an_orphan_result_is_its_own_closure(self):
        msgs = [_user(), _tool("nobody-called-this")]
        self.assertEqual(bp.message_closure(msgs, 1), {1})


class TestThinkingPolicy(unittest.TestCase):
    def _msgs(self):
        return [_user(), _assistant("answer", thinking="t" * 5000)]

    def test_summary_input_drops_reasoning(self):
        """The summariser reads the transcript to describe it; how the
        model talked itself into each call is not part of that."""
        r = bp.project(self._msgs(), target_tokens=10_000,
                       policy_intent="summary_input")
        self.assertNotIn("thinking", r.messages[1])

    def test_retrospective_input_keeps_reasoning(self):
        """It is the subject; dropping it leaves nothing to assess."""
        r = bp.project(self._msgs(), target_tokens=10_000,
                       policy_intent="retrospective_input")
        self.assertIn("thinking", r.messages[1])

    def test_provider_request_keeps_reasoning(self):
        r = bp.project(self._msgs(), target_tokens=10_000,
                       policy_intent="provider_request")
        self.assertIn("thinking", r.messages[1])

    def test_keep_latest_only_when_it_fits(self):
        msgs = [_user(), _assistant("a", thinking="t" * 200),
                _assistant("b", thinking="t" * 200)]
        roomy = bp.project(msgs, target_tokens=10_000,
                           policy_intent="compaction_projection")
        self.assertIn("thinking", roomy.messages[-1])
        self.assertNotIn("thinking", roomy.messages[1])

        tight = bp.project(
            [_user(), _assistant("a", thinking="t" * 100_000)],
            target_tokens=100, policy_intent="compaction_projection")
        self.assertNotIn("thinking", tight.messages[-1])

    def test_dropping_reasoning_is_recorded(self):
        r = bp.project(self._msgs(), target_tokens=10_000,
                       policy_intent="summary_input")
        self.assertTrue(any(a.kind == "dropped_field" for a in r.actions))


class TestProtections(unittest.TestCase):
    def _long(self, n=40):
        msgs = [_user("the original question")]
        for i in range(n):
            msgs.append(_assistant(f"turn {i} " * 500,
                                   calls=[(f"c{i}", "grep")]))
            msgs.append(_tool(f"c{i}", "x" * 3000))
        msgs.append(_user("the latest question"))
        return msgs

    def test_first_and_latest_typed_user_survive(self):
        r = bp.project(self._long(), target_tokens=500,
                       policy_intent="summary_input")
        texts = [m.get("content") for m in r.messages]
        self.assertIn("the original question", texts)
        self.assertIn("the latest question", texts)

    def test_the_turn_in_flight_survives(self):
        msgs = self._long()
        msgs.append(_assistant("in flight", calls=[("open", "grep")]))
        r = bp.project(msgs, target_tokens=500,
                       policy_intent="summary_input")
        self.assertIn("in flight", [m.get("content") for m in r.messages])

    def test_no_orphaned_tool_results_survive(self):
        """The property that matters: what comes back is still a valid
        conversation."""
        r = bp.project(self._long(), target_tokens=2_000,
                       policy_intent="summary_input")
        called = set()
        for m in r.messages:
            for tc in m.get("tool_calls") or []:
                called.add(tc["id"])
        for m in r.messages:
            if m.get("tool_call_id"):
                self.assertIn(m["tool_call_id"], called)

    def test_no_calls_left_without_results(self):
        r = bp.project(self._long(), target_tokens=2_000,
                       policy_intent="summary_input")
        answered = {m["tool_call_id"] for m in r.messages
                    if m.get("tool_call_id")}
        live_tail = bp.live_tail_start_index(r.messages)
        for i, m in enumerate(r.messages):
            if i >= live_tail:
                continue
            for tc in m.get("tool_calls") or []:
                self.assertIn(tc["id"], answered)

    def test_unreachable_target_fails_loudly(self):
        """Returning something over budget as if it were fine is how a
        rejected request becomes a mystery."""
        msgs = [_user("q" * 100_000), _user("a" * 100_000)]
        r = bp.project(msgs, target_tokens=1,
                       policy_intent="summary_input")
        self.assertEqual(r.status, "failed")
        self.assertTrue(r.warnings)

    def test_zero_target_is_rejected(self):
        r = bp.project([_user()], target_tokens=0)
        self.assertEqual(r.status, "failed")


class TestReachingTheTarget(unittest.TestCase):
    def _long(self, n=40):
        msgs = [_user("q")]
        for i in range(n):
            msgs.append(_assistant(f"turn {i} " * 500,
                                   thinking=f"thinking {i} " * 500,
                                   calls=[(f"c{i}", "grep")]))
            msgs.append(_tool(f"c{i}", "x" * 3000))
        return msgs

    def test_reaches_the_target(self):
        r = bp.project(self._long(), target_tokens=4_000,
                       policy_intent="summary_input")
        self.assertEqual(r.status, "ok")
        self.assertLessEqual(r.estimated_tokens, 4_000)

    def test_a_span_that_fits_is_returned_verbatim(self):
        msgs = [_user("short"), _assistant("also short")]
        r = bp.project(msgs, target_tokens=100_000,
                       policy_intent="provider_request")
        self.assertEqual(r.messages, msgs)
        self.assertFalse(r.degraded)

    def test_retrospective_intent_sheds_tool_text_first(self):
        """Its serializer reduces every result to a one-line verdict, so
        carrying their full text into the projection only to discard it
        is waste."""
        r = bp.project(self._long(), target_tokens=6_000,
                       policy_intent="retrospective_input")
        kept_thinking = sum(1 for m in r.messages if m.get("thinking"))
        self.assertGreater(kept_thinking, 0)

    def test_actions_describe_what_happened(self):
        r = bp.project(self._long(), target_tokens=4_000,
                       policy_intent="summary_input")
        kinds = {a.kind for a in r.actions}
        self.assertTrue(kinds & {"dropped_message", "truncated_text",
                                 "dropped_field"})
        self.assertIn("ok", r.summary_line())

    def test_non_dict_entries_are_ignored(self):
        r = bp.project([_user(), "junk", None], target_tokens=10_000)
        self.assertEqual(len(r.messages), 1)


if __name__ == "__main__":
    unittest.main()
