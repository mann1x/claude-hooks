"""Tests for the v2 state schema in ``consultants.engine.state_v2``.

These tests run in the main ``claude-hooks`` env — they exercise
plain Python (dataclasses, typing, reducers) without importing
langgraph. The companion LangGraph-integrated tests live in
``test_consultants_v2_checkpointer.py`` and the future M2 graph
tests.
"""

from __future__ import annotations

import time
import unittest
from pathlib import Path

from consultants.engine.state_v2 import (
    CouncilStateV2,
    Doc,
    InterruptState,
    RuntimeControl,                 # noqa: F401
    append_doc,
    latest_confidence,
    latest_or_none,
    merge_runtime_control,
    time_remaining_s,
    unconsumed_context_for,
)


class TestDoc(unittest.TestCase):

    def test_content_hash_stable_for_same_inputs(self):
        d1 = Doc(role="researcher", text="alpha", ts=1.0)
        d2 = Doc(role="researcher", text="alpha", ts=2.0)  # different ts
        # Hash ignores ts/source — only role + text matter for dedup.
        self.assertEqual(d1.content_hash, d2.content_hash)

    def test_content_hash_differs_on_role_or_text(self):
        d1 = Doc(role="researcher", text="alpha")
        d2 = Doc(role="planner", text="alpha")
        d3 = Doc(role="researcher", text="beta")
        self.assertNotEqual(d1.content_hash, d2.content_hash)
        self.assertNotEqual(d1.content_hash, d3.content_hash)

    def test_default_ts_is_now(self):
        before = time.time()
        d = Doc(role="any", text="x")
        after = time.time()
        self.assertGreaterEqual(d.ts, before)
        self.assertLessEqual(d.ts, after)


class TestMergeRuntimeControl(unittest.TestCase):

    def test_empty_left_returns_right(self):
        right: RuntimeControl = {"max_rounds": 5}
        out = merge_runtime_control(None, right)
        self.assertEqual(out, {"max_rounds": 5})

    def test_empty_right_preserves_left(self):
        left: RuntimeControl = {"deadline_ts": 1.0, "max_rounds": 3}
        out = merge_runtime_control(left, None)
        self.assertEqual(out, {"deadline_ts": 1.0, "max_rounds": 3})

    def test_right_overrides_specific_keys(self):
        left: RuntimeControl = {
            "deadline_ts": 1.0, "max_rounds": 3, "max_reroutes": 2,
        }
        right: RuntimeControl = {"max_rounds": 7}
        out = merge_runtime_control(left, right)
        # Only the specified key changes.
        self.assertEqual(out["deadline_ts"], 1.0)
        self.assertEqual(out["max_rounds"], 7)
        self.assertEqual(out["max_reroutes"], 2)

    def test_none_values_in_right_are_ignored(self):
        left: RuntimeControl = {"max_rounds": 3}
        right: RuntimeControl = {"max_rounds": None, "max_reroutes": 5}  # type: ignore[typeddict-item]
        out = merge_runtime_control(left, right)
        # max_rounds=None in right does NOT clobber left.
        self.assertEqual(out["max_rounds"], 3)
        self.assertEqual(out["max_reroutes"], 5)

    def test_does_not_mutate_inputs(self):
        left: RuntimeControl = {"max_rounds": 3}
        right: RuntimeControl = {"max_rounds": 7}
        original_left = dict(left)
        original_right = dict(right)
        merge_runtime_control(left, right)
        self.assertEqual(left, original_left)
        self.assertEqual(right, original_right)


class TestAppendDoc(unittest.TestCase):

    def test_appends_in_order(self):
        d1 = Doc(role="researcher", text="a")
        d2 = Doc(role="planner", text="b")
        out = append_doc([d1], [d2])
        self.assertEqual(out, [d1, d2])

    def test_deduplicates_by_hash_within_right(self):
        d1 = Doc(role="researcher", text="a")
        d1_dup = Doc(role="researcher", text="a", ts=999.0)
        out = append_doc([], [d1, d1_dup])
        self.assertEqual(len(out), 1)
        # The first occurrence wins (preserve user-perceived order).
        self.assertEqual(out[0].ts, d1.ts)

    def test_deduplicates_across_left_and_right(self):
        # User retried an inject — same content in both sides.
        d_left = Doc(role="researcher", text="x", ts=1.0)
        d_right = Doc(role="researcher", text="x", ts=2.0)
        out = append_doc([d_left], [d_right])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].ts, 1.0)

    def test_handles_none_sides(self):
        d = Doc(role="any", text="x")
        self.assertEqual(append_doc(None, [d]), [d])
        self.assertEqual(append_doc([d], None), [d])
        self.assertEqual(append_doc(None, None), [])


class TestLatestOrNoneReducer(unittest.TestCase):

    def test_concatenates(self):
        self.assertEqual(
            latest_or_none([1, 2], [3, 4]),
            [1, 2, 3, 4],
        )

    def test_none_sides(self):
        self.assertEqual(latest_or_none(None, [1]), [1])
        self.assertEqual(latest_or_none([1], None), [1])
        self.assertEqual(latest_or_none(None, None), [])

    def test_does_not_mutate_inputs(self):
        left = [1, 2]
        right = [3]
        latest_or_none(left, right)
        self.assertEqual(left, [1, 2])
        self.assertEqual(right, [3])


class TestLatestConfidence(unittest.TestCase):

    def test_empty_state(self):
        self.assertIsNone(latest_confidence({}))

    def test_empty_series(self):
        self.assertIsNone(latest_confidence({"confidence": []}))

    def test_returns_last(self):
        self.assertEqual(
            latest_confidence({"confidence": [0.5, 0.7, 0.9]}),
            0.9,
        )

    def test_non_numeric_entry_returns_none(self):
        self.assertIsNone(
            latest_confidence({"confidence": ["bad"]}),
        )


class TestUnconsumedContextFor(unittest.TestCase):

    def test_filters_by_role(self):
        docs = [
            Doc(role="researcher", text="r"),
            Doc(role="planner", text="p"),
            Doc(role="any", text="a"),
            Doc(role="critic", text="c"),
        ]
        state = {"additional_context": docs}
        out = unconsumed_context_for(state, "researcher")
        # researcher + any, but NOT planner / critic.
        self.assertEqual([d.text for d in out], ["r", "a"])

    def test_empty_state(self):
        self.assertEqual(unconsumed_context_for({}, "researcher"), [])


class TestTimeRemaining(unittest.TestCase):

    def test_no_deadline_returns_none(self):
        self.assertIsNone(time_remaining_s({}))
        self.assertIsNone(time_remaining_s({"runtime_control": {}}))

    def test_future_deadline_positive(self):
        state = {"runtime_control": {"deadline_ts": time.time() + 60.0}}
        remaining = time_remaining_s(state)
        self.assertIsNotNone(remaining)
        self.assertGreater(remaining, 50.0)
        self.assertLessEqual(remaining, 60.0)

    def test_past_deadline_negative(self):
        state = {"runtime_control": {"deadline_ts": time.time() - 30.0}}
        remaining = time_remaining_s(state)
        self.assertIsNotNone(remaining)
        self.assertLess(remaining, 0)


class TestInterruptState(unittest.TestCase):

    def test_construct_and_read(self):
        intr = InterruptState(
            kind="review",
            prompt="approve the draft?",
            posted_at=123.0,
            payload={"draft": "hello"},
        )
        self.assertEqual(intr.kind, "review")
        self.assertEqual(intr.prompt, "approve the draft?")
        self.assertEqual(intr.payload, {"draft": "hello"})

    def test_default_payload_empty_dict(self):
        intr = InterruptState(
            kind="approve",
            prompt="p",
            posted_at=1.0,
        )
        self.assertEqual(intr.payload, {})


# --------------------------------------------------------------- #
# extra_roots — the 2026-08-02 channel-drop regression.
# --------------------------------------------------------------- #

class TestExtraRootsChannel(unittest.TestCase):
    """``extra_roots`` must be a declared channel, and every Send
    payload must carry it.

    LangGraph builds its channels from ``CouncilStateV2`` and silently
    drops any key of the invoke input that isn't one of them. Between
    2026-05-18 and 2026-08-02 ``extra_roots`` was written into the
    initial state by the runner and declared nowhere, so it reached no
    node: ``state.get("extra_roots")`` was ``None`` in every council
    run, and the citation linter verified cites against ``[cwd]``
    alone. Every citation under an ``--add-dir`` root came back
    "file not found in any allowed_root" — a fully-grounded answer
    that read as fabricated, after 55 minutes of cloud inference.

    Nothing raised, nothing logged, and each individual link in the
    chain looked correct in isolation. These two tests are the pins
    that make the next such drop fail loudly in CI instead.
    """

    def test_extra_roots_is_declared_in_every_compiled_schema(self):
        # NOT just in CouncilStateV2. The first attempt at this fix
        # declared the channel there and stopped, but every
        # ``StateGraph(...)`` in graph.py compiles against
        # ``CouncilState`` — V2 is the not-yet-adopted successor. The
        # key kept being stripped, a 33-minute follow-up linted against
        # cwd alone, and this test was green the whole time.
        #
        # So resolve the schema STRUCTURALLY: whatever class name is
        # passed to StateGraph is the one that has to declare it.
        import ast

        repo_root = Path(__file__).resolve().parent.parent
        src = (repo_root / "consultants" / "engine" / "graph.py").read_text()
        tree = ast.parse(src)

        declared: dict[str, set[str]] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                declared[node.name] = {
                    stmt.target.id for stmt in node.body
                    if isinstance(stmt, ast.AnnAssign)
                    and isinstance(stmt.target, ast.Name)
                }

        compiled: set[str] = set()
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "StateGraph"
                    and node.args
                    and isinstance(node.args[0], ast.Name)):
                compiled.add(node.args[0].id)

        self.assertTrue(compiled, "no StateGraph(<Name>) call found")
        for name in sorted(compiled):
            self.assertIn(
                name, declared,
                f"StateGraph compiles {name} but it isn't a class in "
                f"graph.py — widen this test",
            )
            self.assertIn(
                "extra_roots", declared[name],
                f"{name} is compiled by StateGraph but doesn't declare "
                f"extra_roots; LangGraph will strip it and the citation "
                f"verifiers will run against cwd alone",
            )

    def test_extra_roots_is_declared_in_the_v2_schema_too(self):
        # V2 isn't compiled yet; when it is adopted the channel has to
        # already be there or this regresses on the switchover.
        self.assertIn("extra_roots", CouncilStateV2.__annotations__)

    def test_every_send_payload_carrying_cwd_also_carries_extra_roots(self):
        # A Send delivers ONLY the keys in its own dict — a fanout lane
        # never inherits a global channel. So declaring the channel is
        # necessary but not sufficient: each payload has to pass it
        # along or the lane's own citation lint runs blind.
        import ast

        repo_root = Path(__file__).resolve().parent.parent
        src = (repo_root / "consultants" / "engine" / "graph.py").read_text()
        tree = ast.parse(src)

        checked = 0
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "Send"):
                continue
            for arg in node.args:
                if not isinstance(arg, ast.Dict):
                    continue
                keys = {
                    k.value for k in arg.keys
                    if isinstance(k, ast.Constant)
                }
                if "cwd" not in keys:
                    continue
                checked += 1
                self.assertIn(
                    "extra_roots", keys,
                    f"Send payload at graph.py:{node.lineno} passes cwd "
                    f"but not extra_roots — this lane's citation lint "
                    f"will run against cwd alone",
                )
        self.assertGreaterEqual(
            checked, 5,
            "expected at least 5 Send payloads carrying cwd; the "
            "walk found fewer, so this test is no longer covering "
            "the fanout paths it was written for",
        )


if __name__ == "__main__":
    unittest.main()
