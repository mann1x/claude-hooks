"""M-A approval channel — ``consultants.engine.tool_approval``.

The permission ladder (`auto` / `ask_assistant` / `ask_human` / `deny`)
has been enforced at dispatch since the registry landed, but nothing
was wired to the ``ask_*`` rungs: ``ToolRegistry`` refused them for
want of an approval channel, which meant a documented config value
(`set-tools --permission write_file ask_assistant`) silently meant
"deny". `docs/PLAN-council-tool-surface.md` calls this M-A's remaining
piece. These tests pin the behaviours the plan decided:

* ``ask_assistant`` **auto-approves and never stalls** — the plan is
  explicit that it is "auto-approve with discretion, not wait for a
  verdict", because a round-trip per write per lane burns tokens for a
  verdict that is yes by construction.
* ``ask_human`` is **the only rung that can stall**, reserved for spend.
* **Timeout denies** (decision-table row 7): absence of a human never
  authorizes spend.
* A refusal is a **tool-result string, never an exception** — a denied
  tool teaches the model to try another route rather than crashing the
  lane.
"""

from __future__ import annotations

import threading
import time
import unittest

from claude_hooks.tool_registry.policy import GateDecision
from consultants.engine.tool_approval import (
    ApprovalContext,
    ToolApprovalBroker,
    make_approval_fn,
)


def _ctx(broker, **kw):
    kw.setdefault("cwd", "/proj")
    kw.setdefault("timeout_s", 0.2)
    return ApprovalContext(broker=broker, **kw)


def _decision(level: str, tool: str = "write_file", **kw):
    return GateDecision(
        tool=tool, level=level,
        reason=kw.pop("reason", f"{tool} is {level}"),
        raw_args=kw.pop("raw_args", '{"path": "x.py"}'),
        **kw,
    )


class TestAskAssistantNeverStalls(unittest.TestCase):
    def test_auto_approves(self):
        b = ToolApprovalBroker("csl-x")
        fn = make_approval_fn(_ctx(b))
        assert fn(_decision("ask_assistant")) is True

    def test_does_not_park_anything(self):
        # If it parked, an operator would see a request needing an
        # answer that nobody is required to give.
        b = ToolApprovalBroker("csl-x")
        make_approval_fn(_ctx(b))(_decision("ask_assistant"))
        assert b.pending() == []

    def test_returns_immediately(self):
        # The point of not stalling is wall-clock. Give it a long
        # timeout and require it to return anyway.
        b = ToolApprovalBroker("csl-x")
        fn = make_approval_fn(_ctx(b, timeout_s=3600))
        t0 = time.monotonic()
        assert fn(_decision("ask_assistant")) is True
        assert time.monotonic() - t0 < 0.5

    def test_is_recorded_for_the_audit_trail(self):
        # What ask_assistant buys over auto is that the assistant SEES
        # the call and can tighten the rung afterwards.
        seen = []
        b = ToolApprovalBroker("csl-x")
        fn = make_approval_fn(
            _ctx(b, emit=lambda k, p: seen.append((k, p))))
        fn(_decision("ask_assistant"))
        assert [k for k, _ in seen] == ["tool_approval_auto"]
        assert seen[0][1]["tool"] == "write_file"


class TestAskHumanParks(unittest.TestCase):
    def test_timeout_denies(self):
        # Decision-table row 7. The only acceptable verdict when nobody
        # answers is "no" — a timeout that approved would let an
        # unattended run spend money.
        b = ToolApprovalBroker("csl-x")
        ctx = _ctx(b, timeout_s=0.15)
        assert make_approval_fn(ctx)(
            _decision("ask_human", "rent_pod")) is False
        assert ctx.stats["timed_out"] == 1

    def test_timeout_is_recorded_as_a_timeout_not_a_denial(self):
        # Provenance: "nobody answered" and "somebody said no" are
        # different facts about the same run.
        b = ToolApprovalBroker("csl-x")
        make_approval_fn(_ctx(b, timeout_s=0.1))(
            _decision("ask_human", "rent_pod"))
        history = b.history_public()
        assert len(history) == 1
        assert history[0]["resolution"] == "timeout"
        assert history[0]["allowed"] is False

    def test_an_allow_releases_the_lane(self):
        b = ToolApprovalBroker("csl-x")
        ctx = _ctx(b, timeout_s=10)
        result: list = []

        def _run():
            result.append(
                make_approval_fn(ctx)(_decision("ask_human", "rent_pod")))

        th = threading.Thread(target=_run, daemon=True)
        th.start()
        deadline = time.monotonic() + 5
        while not b.pending() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert b.pending(), "request never parked"
        b.resolve(None, allow=True, by="operator")
        th.join(timeout=5)
        assert result == [True]
        assert ctx.stats["granted"] == 1

    def test_a_deny_refuses_the_lane(self):
        b = ToolApprovalBroker("csl-x")
        ctx = _ctx(b, timeout_s=10)
        result: list = []
        th = threading.Thread(
            target=lambda: result.append(
                make_approval_fn(ctx)(_decision("ask_human", "rent_pod"))),
            daemon=True)
        th.start()
        deadline = time.monotonic() + 5
        while not b.pending() and time.monotonic() < deadline:
            time.sleep(0.01)
        b.resolve(None, allow=False, by="operator")
        th.join(timeout=5)
        assert result == [False]
        assert ctx.stats["denied"] == 1

    def test_the_request_carries_what_an_approver_needs(self):
        # "An approver cannot judge sh -c "..." on its own" — the plan.
        seen = []
        b = ToolApprovalBroker("csl-x")
        make_approval_fn(
            _ctx(b, timeout_s=0.1, cwd="/srv/proj",
                 emit=lambda k, p: seen.append((k, p))),
        )(_decision("ask_human", "rent_pod", reason="spends money",
                    raw_args='{"gpu": "h100"}'))
        kinds = [k for k, _ in seen]
        assert kinds[0] == "awaiting_tool_approval"
        payload = seen[0][1]
        assert payload["tool"] == "rent_pod"
        assert payload["level"] == "ask_human"
        assert payload["arguments"] == '{"gpu": "h100"}'
        assert payload["cwd"] == "/srv/proj"
        assert payload["reason"] == "spends money"
        assert payload["deadline_ts"] > payload["opened_at"]

    def test_a_closed_session_stops_the_wait_early(self):
        # A cancelled or reaped session must not hold a worker thread
        # for the full deadline.
        b = ToolApprovalBroker("csl-x")
        ctx = _ctx(b, timeout_s=30, is_closed=lambda: True)
        t0 = time.monotonic()
        assert make_approval_fn(ctx)(
            _decision("ask_human", "rent_pod")) is False
        assert time.monotonic() - t0 < 2.0

    def test_the_approval_fn_never_raises(self):
        # The registry treats a raise as a refusal, but a raising
        # approval channel would also lose the provenance. Feed it a
        # decision object missing every attribute.
        b = ToolApprovalBroker("csl-x")
        fn = make_approval_fn(_ctx(b, timeout_s=0.05))
        assert fn(object()) is False

    def test_a_broken_emit_does_not_break_the_verdict(self):
        def _boom(kind, payload):
            raise RuntimeError("telemetry down")

        b = ToolApprovalBroker("csl-x")
        fn = make_approval_fn(_ctx(b, emit=_boom))
        assert fn(_decision("ask_assistant")) is True


class TestBroker(unittest.TestCase):
    def test_resolve_without_an_id_answers_the_oldest(self):
        b = ToolApprovalBroker("csl-x")
        first, _ = b.open(tool="a", level="ask_human", arguments="",
                          cwd="/p", reason="r", timeout_s=60, now=100.0)
        b.open(tool="b", level="ask_human", arguments="", cwd="/p",
               reason="r", timeout_s=60, now=200.0)
        got = b.resolve(None, allow=True)
        assert got is not None and got.request_id == first.request_id
        assert [r.tool for r in b.pending()] == ["b"]

    def test_resolve_by_id_answers_that_one(self):
        b = ToolApprovalBroker("csl-x")
        b.open(tool="a", level="ask_human", arguments="", cwd="/p",
               reason="r", timeout_s=60, now=100.0)
        second, _ = b.open(tool="b", level="ask_human", arguments="",
                           cwd="/p", reason="r", timeout_s=60, now=200.0)
        got = b.resolve(second.request_id, allow=False)
        assert got is not None and got.tool == "b"
        assert [r.tool for r in b.pending()] == ["a"]

    def test_resolving_nothing_is_a_no_op(self):
        # A duplicate ack after a timeout must not error.
        assert ToolApprovalBroker("csl-x").resolve(None, allow=True) is None

    def test_pending_public_omits_resolution_while_open(self):
        b = ToolApprovalBroker("csl-x")
        b.open(tool="a", level="ask_human", arguments="", cwd="/p",
               reason="r", timeout_s=60)
        [row] = b.pending_public()
        assert "resolution" not in row
        assert row["request_id"].startswith("tap-")

    def test_request_ids_are_unique_across_brokers(self):
        # Two sessions parking at once must not collide on an id the
        # operator quotes back.
        a, b = ToolApprovalBroker("csl-a"), ToolApprovalBroker("csl-b")
        ra, _ = a.open(tool="t", level="ask_human", arguments="", cwd="/p",
                       reason="r", timeout_s=60)
        rb, _ = b.open(tool="t", level="ask_human", arguments="", cwd="/p",
                       reason="r", timeout_s=60)
        assert ra.request_id != rb.request_id

    def test_concurrent_resolves_answer_once(self):
        b = ToolApprovalBroker("csl-x")
        b.open(tool="t", level="ask_human", arguments="", cwd="/p",
               reason="r", timeout_s=60)
        wins: list = []

        def _try():
            wins.append(b.resolve(None, allow=True) is not None)

        threads = [threading.Thread(target=_try) for _ in range(8)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=5)
        assert sum(1 for w in wins if w) == 1


if __name__ == "__main__":
    unittest.main()


class TestThroughTheRealRegistry(unittest.TestCase):
    """The plan's M-A exit criterion: "gate provably fires (``ask`` on a
    builtin tool pauses and resumes)". Drive the actual
    ``ToolRegistry`` + ``PolicyGate``, not a stand-in."""

    def _registry(self, tmp, level: str, approval_fn):
        from claude_hooks.tool_registry import (
            BuiltinToolProvider, PolicyGate, ToolRegistry,
        )
        return ToolRegistry(
            [BuiltinToolProvider((str(tmp),))],
            gate=PolicyGate(default_level="auto",
                            overrides={"read_file": level}),
            approval_fn=approval_fn,
        )

    def test_ask_human_allowed_runs_the_tool(self):
        import json
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "hello.txt").write_text("the file body\n")
            b = ToolApprovalBroker("csl-x")
            ctx = _ctx(b, cwd=td, timeout_s=10)
            reg = self._registry(td, "ask_human", make_approval_fn(ctx))
            out: list = []
            th = threading.Thread(
                target=lambda: out.append(reg.dispatch(
                    "read_file", json.dumps({"path": "hello.txt"}), td)),
                daemon=True)
            th.start()
            deadline = time.monotonic() + 5
            while not b.pending() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert b.pending(), "gate did not park the call"
            b.resolve(None, allow=True, by="operator")
            th.join(timeout=5)
            assert "the file body" in out[0]

    def test_ask_human_denied_returns_an_error_string_not_a_raise(self):
        # The plan: "Denial and approval-denial must return a tool
        # result string, not raise — a denied tool should teach the
        # model to try another route, not crash the lane."
        import json
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "hello.txt").write_text("body\n")
            b = ToolApprovalBroker("csl-x")
            reg = self._registry(
                td, "ask_human",
                make_approval_fn(_ctx(b, cwd=td, timeout_s=0.1)))
            result = reg.dispatch(
                "read_file", json.dumps({"path": "hello.txt"}), td)
            assert isinstance(result, str)
            assert result.startswith("error:")
            assert "not approved" in result
            assert "body" not in result

    def test_ask_assistant_runs_without_parking(self):
        import json
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "hello.txt").write_text("the file body\n")
            b = ToolApprovalBroker("csl-x")
            reg = self._registry(
                td, "ask_assistant",
                make_approval_fn(_ctx(b, cwd=td, timeout_s=3600)))
            result = reg.dispatch(
                "read_file", json.dumps({"path": "hello.txt"}), td)
            assert "the file body" in result
            assert b.pending() == []

    def test_auto_never_touches_the_channel(self):
        # "The gate must be cheap on the common path — a dict lookup
        # that returns auto and never touches the LLM or the interrupt
        # machinery."
        import json
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "hello.txt").write_text("the file body\n")
            calls: list = []
            reg = self._registry(
                td, "auto", lambda decision: calls.append(decision) or True)
            result = reg.dispatch(
                "read_file", json.dumps({"path": "hello.txt"}), td)
            assert "the file body" in result
            assert calls == []


def _has_langgraph() -> bool:
    try:
        import langgraph  # noqa: F401
        return True
    except Exception:
        return False


HAS_LANGGRAPH = _has_langgraph()


@unittest.skipUnless(HAS_LANGGRAPH, "langgraph not installed")
class TestParkingIsPerLane(unittest.TestCase):
    """The plan's decided pause scope: "park the lane, siblings
    continue", with the graph-level pause rejected as "the expensive
    failure on exactly the runs that matter most".

    The plan expected this to cost lane-scoped interrupt state threaded
    through the checkpointer. Blocking inside the tool executor gets it
    for free — LangGraph runs sync nodes on its own worker threads — so
    this test exists to prove the free version actually holds, against
    the real ``CouncilState`` graph rather than an argument.
    """

    def test_a_parked_lane_does_not_stall_its_siblings(self):
        import threading as _th
        import time as _time

        from langgraph.graph import END, START, StateGraph
        from langgraph.types import Send

        from consultants.engine.graph import CouncilState

        broker = ToolApprovalBroker("csl-x")
        approve = make_approval_fn(_ctx(broker, timeout_s=30))
        order: list = []

        def entry(state):
            return {}

        def lane(state):
            idx = state.get("lane_idx")
            if idx == 0:
                order.append("lane0-parking")
                approve(_decision("ask_human", "rent_pod"))
                order.append("lane0-released")
            else:
                _time.sleep(0.2)
                order.append(f"lane{idx}-done")
            return {}

        g = StateGraph(CouncilState)
        g.add_node("entry", entry)
        g.add_node("lane", lane)
        g.add_edge(START, "entry")
        g.add_conditional_edges(
            "entry",
            lambda s: [Send("lane", {"question": "q", "cwd": "/a",
                                     "lane_idx": i}) for i in range(3)],
            ["lane"],
        )
        g.add_edge("lane", END)

        def _release():
            deadline = _time.time() + 20
            while _time.time() < deadline:
                if sum(1 for o in order if o.endswith("-done")) >= 2:
                    break
                _time.sleep(0.02)
            broker.resolve(None, allow=True, by="test")

        _th.Thread(target=_release, daemon=True).start()
        g.compile().invoke({"question": "q", "cwd": "/a"})

        assert "lane0-released" in order, order
        released_at = order.index("lane0-released")
        finished_while_parked = [
            o for o in order[:released_at] if o.endswith("-done")]
        assert len(finished_while_parked) == 2, (
            f"siblings did NOT run while lane 0 was parked: {order}")


# ==================================================================== #
# Coalescing + standing grants (2026-08-02).
#
# The first live run parked FOUR read_file requests in 90 seconds —
# three of them the same file from three x-tier researcher lanes. A
# per-call verdict means a human answers a dozen times a run or watches
# them all deny on timeout, which is worse than `deny` because it costs
# the wall-clock too. Authorization is per COUNCIL: one verdict binds
# every role and every lane.
# ==================================================================== #


class TestCoalescing(unittest.TestCase):
    def test_identical_calls_share_one_request(self):
        b = ToolApprovalBroker("csl-x")
        first, created_a = b.open(
            tool="read_file", level="ask_human", arguments='{"path": "a.py"}',
            cwd="/p", reason="r", timeout_s=60,
        )
        second, created_b = b.open(
            tool="read_file", level="ask_human", arguments='{"path": "a.py"}',
            cwd="/p", reason="r", timeout_s=60,
        )
        assert created_a is True and created_b is False
        assert first.request_id == second.request_id
        assert len(b.pending()) == 1
        assert first.waiters == 2

    def test_different_arguments_do_not_coalesce(self):
        b = ToolApprovalBroker("csl-x")
        b.open(tool="read_file", level="ask_human",
               arguments='{"path": "a.py"}', cwd="/p", reason="r",
               timeout_s=60)
        b.open(tool="read_file", level="ask_human",
               arguments='{"path": "b.py"}', cwd="/p", reason="r",
               timeout_s=60)
        assert len(b.pending()) == 2

    def test_one_answer_releases_every_coalesced_lane(self):
        b = ToolApprovalBroker("csl-x")
        results: list = []
        args = '{"path": "shared.py"}'

        def _lane(idx):
            fn = make_approval_fn(_ctx(b, timeout_s=20))
            results.append(
                fn(_decision("ask_human", tool="read_file", raw_args=args)))

        threads = [threading.Thread(target=_lane, args=(i,))
                   for i in range(3)]
        for t in threads:
            t.start()
        deadline = time.time() + 10
        while time.time() < deadline and not b.pending():
            time.sleep(0.02)
        assert len(b.pending()) == 1, "three lanes should be ONE decision"
        b.resolve(None, allow=True, by="test")
        for t in threads:
            t.join(timeout=10)
        assert results == [True, True, True]

    def test_public_dict_hides_waiters_when_only_one(self):
        # Parity: a single-lane park must render exactly as before.
        b = ToolApprovalBroker("csl-x")
        b.open(tool="t", level="ask_human", arguments="{}", cwd="/p",
               reason="r", timeout_s=60)
        assert "waiters" not in b.pending_public()[0]


class TestStandingGrants(unittest.TestCase):
    def test_tool_scope_covers_later_calls_without_parking(self):
        b = ToolApprovalBroker("csl-x")
        b.open(tool="read_file", level="ask_human",
               arguments='{"path": "a.py"}', cwd="/p", reason="r",
               timeout_s=60)
        b.resolve(None, allow=True, by="human", scope="tool")
        fn = make_approval_fn(_ctx(b, timeout_s=0.2))
        # A *different* file, and it never reaches the queue.
        assert fn(_decision("ask_human", tool="read_file",
                            raw_args='{"path": "z.py"}')) is True
        assert b.pending() == []

    def test_glob_scope_matches_by_target(self):
        b = ToolApprovalBroker("csl-x")
        b.open(tool="read_file", level="ask_human",
               arguments='{"path": "src/a.py"}', cwd="/p", reason="r",
               timeout_s=60)
        b.resolve(None, allow=True, by="human", scope="glob",
                  pattern="src/**")
        fn = make_approval_fn(_ctx(b, timeout_s=0.2))
        assert fn(_decision("ask_human", tool="read_file",
                            raw_args='{"path": "src/deep/b.py"}')) is True
        assert fn(_decision("ask_human", tool="read_file",
                            raw_args='{"path": "other/c.py"}')) is False

    def test_glob_defaults_to_the_answered_calls_own_target(self):
        b = ToolApprovalBroker("csl-x")
        b.open(tool="read_file", level="ask_human",
               arguments='{"path": "cfg.toml"}', cwd="/p", reason="r",
               timeout_s=60)
        b.resolve(None, allow=True, by="human", scope="glob")
        fn = make_approval_fn(_ctx(b, timeout_s=0.2))
        assert fn(_decision("ask_human", tool="read_file",
                            raw_args='{"path": "cfg.toml"}')) is True

    def test_a_grant_does_not_leak_across_tools(self):
        b = ToolApprovalBroker("csl-x")
        b.open(tool="read_file", level="ask_human", arguments="{}",
               cwd="/p", reason="r", timeout_s=60)
        b.resolve(None, allow=True, by="human", scope="tool")
        assert b.matching_grant("write_file", "{}") is None

    def test_installing_a_grant_releases_already_parked_siblings(self):
        # Otherwise "allow all reads under src/" still leaves the three
        # lanes already waiting to time out.
        b = ToolApprovalBroker("csl-x")
        a, _ = b.open(tool="read_file", level="ask_human",
                      arguments='{"path": "src/a.py"}', cwd="/p",
                      reason="r", timeout_s=60)
        c, _ = b.open(tool="read_file", level="ask_human",
                      arguments='{"path": "src/c.py"}', cwd="/p",
                      reason="r", timeout_s=60)
        d, _ = b.open(tool="read_file", level="ask_human",
                      arguments='{"path": "vendor/d.py"}', cwd="/p",
                      reason="r", timeout_s=60)
        b.resolve(a.request_id, allow=True, by="human", scope="glob",
                  pattern="src/**")
        assert c.resolution == "allowed"
        assert [r.request_id for r in b.pending()] == [d.request_id]

    def test_a_standing_deny_is_expressible(self):
        # Stops a model that keeps retrying a forbidden path from
        # parking a lane on every attempt.
        b = ToolApprovalBroker("csl-x")
        b.open(tool="rent_pod", level="ask_human", arguments="{}",
               cwd="/p", reason="r", timeout_s=60)
        b.resolve(None, allow=False, by="human", scope="tool")
        fn = make_approval_fn(_ctx(b, timeout_s=0.2))
        assert fn(_decision("ask_human", tool="rent_pod")) is False
        assert b.pending() == []

    def test_a_later_rule_overrides_an_earlier_one(self):
        b = ToolApprovalBroker("csl-x")
        b.open(tool="read_file", level="ask_human", arguments="{}",
               cwd="/p", reason="r", timeout_s=60)
        b.resolve(None, allow=True, by="human", scope="tool")
        b.open(tool="read_file", level="ask_human",
               arguments='{"path": "secrets.env"}', cwd="/p", reason="r",
               timeout_s=60)
        b.resolve(None, allow=False, by="human", scope="glob",
                  pattern="secrets.env")
        fn = make_approval_fn(_ctx(b, timeout_s=0.2))
        assert fn(_decision("ask_human", tool="read_file",
                            raw_args='{"path": "secrets.env"}')) is False
        assert fn(_decision("ask_human", tool="read_file",
                            raw_args='{"path": "ok.py"}')) is True

    def test_once_scope_installs_nothing(self):
        # Parity: the default answer must not silently widen.
        b = ToolApprovalBroker("csl-x")
        b.open(tool="read_file", level="ask_human", arguments="{}",
               cwd="/p", reason="r", timeout_s=60)
        b.resolve(None, allow=True, by="human")
        assert b.grants_public() == []
        assert b.matching_grant("read_file", "{}") is None

    def test_grant_records_itself_on_the_answered_request(self):
        b = ToolApprovalBroker("csl-x")
        req, _ = b.open(tool="read_file", level="ask_human",
                        arguments='{"path": "a.py"}', cwd="/p",
                        reason="r", timeout_s=60)
        b.resolve(req.request_id, allow=True, by="human", scope="tool")
        assert "read_file" in (req.grant or "")
        assert req.public_dict()["grant"]


class TestTargetExtraction(unittest.TestCase):
    def test_reads_the_common_path_keys(self):
        from consultants.engine.tool_approval import target_of
        assert target_of('{"path": "a.py"}') == "a.py"
        assert target_of('{"file_path": "b.py"}') == "b.py"
        assert target_of('{"pattern": "**/*.py"}') == "**/*.py"

    def test_no_target_means_a_glob_rule_cannot_match(self):
        # A rule scoped to src/** must never silently cover a call whose
        # target can't be established.
        from consultants.engine.tool_approval import GrantRule, target_of
        assert target_of('{"limit": 5}') == ""
        assert target_of("not json") == ""
        rule = GrantRule(scope="glob", tool="t", allow=True, pattern="src/**")
        assert rule.matches("t", '{"limit": 5}') is False
