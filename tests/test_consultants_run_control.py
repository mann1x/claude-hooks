"""Cooperative cancel + pause — ``consultants.engine.run_control``.

Until 2026-08-02 ``POST /cancel`` and ``POST /interrupt`` set a flag on
``runtime_control`` that nothing read. The fix is not "make a node read
the flag", because that would not have worked either: a graph already
inside ``invoke`` carries its channel values in memory through the
superstep and never re-reads the checkpoint ``update_state`` writes. So
the control has to travel out-of-band, on the SessionState, exactly like
the adversary ack and the tool-approval broker — the two cross-thread
controls in this codebase that do work.

These tests pin the decisions:

* **Cancel skips, never raises** — the graph drains to END keeping the
  partial state that ``--keep-partial`` exists to preserve.
* **Cancel skips the synthesizer too.** A cancelled run has no answer;
  running the synthesizer would be spending after the caller said stop.
* **Pause blocks one node's thread**, so x-tier siblings keep running.
* **Pause resumes on timeout, cancel denies on timeout** — opposite
  defaults, because an unanswered spend approval must not authorize
  spend while an unanswered pause has already spent everything up to
  that point.
* **`run_control=None` is byte-identical to pre-gate**, which is what
  keeps every graph built without a session unchanged.
"""

from __future__ import annotations

import threading
import time
import unittest

import pytest

from consultants.engine.run_control import (
    DEFAULT_PAUSE_TIMEOUT_S,
    RunControl,
    gate_node,
)


# ==================================================================== #
# RunControl primitives
# ==================================================================== #

class TestCancelRequests(unittest.TestCase):
    def test_cancel_is_sticky_and_reports_first_only(self):
        rc = RunControl("csl-x")
        assert rc.cancelled is False
        assert rc.request_cancel("stop") is True
        assert rc.cancelled is True
        # A duplicate cancel is a harmless no-op, not a second event.
        assert rc.request_cancel("stop again") is False
        assert rc.cancel_reason == "stop"

    def test_cancel_releases_a_pause(self):
        # A cancel arriving while a node is parked must not queue behind
        # the pause it is trying to end.
        rc = RunControl("csl-x")
        rc.request_pause("look at this")
        assert rc.paused is True
        rc.request_cancel()
        assert rc.paused is False

    def test_pausing_a_cancelling_run_is_refused(self):
        rc = RunControl("csl-x")
        rc.request_cancel()
        assert rc.request_pause() is False
        assert rc.paused is False

    def test_snapshot_is_empty_until_something_is_requested(self):
        # Parity: an untouched run's status payload must be unchanged.
        assert RunControl("csl-x").snapshot() == {}

    def test_snapshot_reports_a_cancel(self):
        rc = RunControl("csl-x")
        rc.request_cancel("too expensive", now=1000.0)
        snap = rc.snapshot()
        assert snap["cancel_requested"] is True
        assert snap["cancel_reason"] == "too expensive"
        assert snap["cancelled_at"] == 1000.0
        assert "paused" not in snap

    def test_snapshot_reports_a_pause_with_its_deadline(self):
        rc = RunControl("csl-x", pause_timeout_s=60.0)
        rc.request_pause("hold on", now=1000.0)
        snap = rc.snapshot()
        assert snap["paused"] is True
        assert snap["pause_reason"] == "hold on"
        assert snap["pause_deadline_ts"] == 1060.0

    def test_release_reports_whether_anything_was_paused(self):
        rc = RunControl("csl-x")
        assert rc.release_pause() is False
        rc.request_pause()
        assert rc.release_pause() is True


class TestCheckGate(unittest.TestCase):
    def test_clean_run_proceeds_without_touching_the_wait_path(self):
        rc = RunControl("csl-x")
        assert rc.check("planner") == "run"

    def test_cancelled_run_is_told_to_cancel(self):
        rc = RunControl("csl-x")
        rc.request_cancel()
        assert rc.check("planner") == "cancel"
        assert rc.skipped == ["planner"]

    def test_a_pause_blocks_until_released(self):
        rc = RunControl("csl-x", pause_timeout_s=20.0)
        rc.request_pause()
        verdicts: list = []

        def _node():
            verdicts.append(rc.check("researcher"))

        th = threading.Thread(target=_node, daemon=True)
        th.start()
        deadline = time.time() + 5
        while time.time() < deadline and not rc.paused_roles:
            time.sleep(0.01)
        assert rc.paused_roles == ["researcher"], "node did not park"
        assert verdicts == [], "node continued while paused"
        rc.release_pause()
        th.join(timeout=5)
        assert verdicts == ["run"]

    def test_a_cancel_during_a_pause_wins(self):
        rc = RunControl("csl-x", pause_timeout_s=20.0)
        rc.request_pause()
        verdicts: list = []
        th = threading.Thread(
            target=lambda: verdicts.append(rc.check("critic")), daemon=True)
        th.start()
        deadline = time.time() + 5
        while time.time() < deadline and not rc.paused_roles:
            time.sleep(0.01)
        rc.request_cancel()
        th.join(timeout=5)
        assert verdicts == ["cancel"]

    def test_a_pause_that_nobody_releases_RESUMES(self):
        # The opposite of the tool-approval deadline, on purpose: an
        # unanswered spend approval must not authorize spend, but an
        # unanswered pause has already spent everything up to here and
        # abandoning the run would waste it.
        rc = RunControl("csl-x", pause_timeout_s=0.15)
        rc.request_pause()
        assert rc.check("synthesizer") == "run"
        assert rc.paused is False

    def test_a_closed_session_does_not_hold_the_thread(self):
        rc = RunControl("csl-x", pause_timeout_s=30.0)
        rc.request_pause()
        assert rc.check("researcher", is_closed=lambda: True) == "run"

    def test_the_park_is_announced_and_the_release_recorded(self):
        rc = RunControl("csl-x", pause_timeout_s=0.15)
        rc.request_pause("hold")
        seen: list = []
        rc.check("planner", emit=lambda k, p: seen.append((k, p)))
        kinds = [k for k, _ in seen]
        assert kinds == ["awaiting_resume", "resumed"]
        assert seen[0][1]["role"] == "planner"
        assert seen[0][1]["reason"] == "hold"
        assert seen[1][1]["timed_out"] is True

    def test_a_broken_emitter_cannot_break_the_gate(self):
        rc = RunControl("csl-x", pause_timeout_s=0.1)
        rc.request_pause()

        def _boom(kind, payload):
            raise RuntimeError("telemetry down")

        assert rc.check("planner", emit=_boom) == "run"


# ==================================================================== #
# gate_node
# ==================================================================== #

class TestGateNode(unittest.TestCase):
    def test_no_run_control_returns_the_function_unchanged(self):
        # Parity: every caller that builds a graph without a session —
        # benchmarks, unit tests, the parity cohorts — must be untouched.
        def _node(state):
            return {"x": 1}

        assert gate_node(_node, role="planner", run_control=None) is _node

    def test_a_live_run_calls_through(self):
        rc = RunControl("csl-x")
        calls: list = []

        def _node(state):
            calls.append(state)
            return {"x": 1}

        gated = gate_node(_node, role="planner", run_control=rc)
        assert gated({"q": "?"}) == {"x": 1}
        assert calls == [{"q": "?"}]

    def test_a_cancelled_node_is_never_called(self):
        rc = RunControl("csl-x")
        rc.request_cancel()
        calls: list = []

        def _node(state):
            calls.append(state)
            return {"x": 1}

        gated = gate_node(_node, role="planner", run_control=rc)
        # Empty delta: a valid partial update for every channel.
        assert gated({"q": "?"}) == {}
        assert calls == [], "the node ran after cancel"

    def test_a_cancelled_node_does_not_raise(self):
        # Raising would abort the stream mid-superstep and lose the
        # partial state --keep-partial exists to preserve.
        rc = RunControl("csl-x")
        rc.request_cancel()
        gated = gate_node(lambda s: {"x": 1}, role="critic", run_control=rc)
        gated({})  # must not raise

    def test_the_skip_is_announced(self):
        rc = RunControl("csl-x")
        rc.request_cancel("user said stop")
        seen: list = []
        gated = gate_node(
            lambda s: {"x": 1}, role="critic", run_control=rc,
            emit=lambda k, p: seen.append((k, p)),
        )
        gated({})
        assert [k for k, _ in seen] == ["node_cancelled"]
        assert seen[0][1]["reason"] == "user said stop"

    def test_the_wrapper_keeps_the_nodes_identity(self):
        def researcher_node(state):
            """Doc."""
            return {}

        gated = gate_node(researcher_node, role="researcher",
                          run_control=RunControl("csl-x"))
        assert gated.__name__ == "researcher_node"
        assert gated.__doc__ == "Doc."


# ==================================================================== #
# Against the REAL compiled graph.
#
# The regression this guards is specifically that the control reaches a
# node of the schema production compiles (CouncilState), through the
# real ``_wrap`` choke point — not a hand-rolled stand-in. `extra_roots`
# was "verified end-to-end" against the wrong schema once already.
# ==================================================================== #

# langgraph is a consultants-env dep; the rest of this file is pure
# stdlib and MUST stay runnable in the main env, or the gate loses its
# regression coverage in the suite that actually gates a commit.
# NOTE: keep the ``try:`` bare — test_langgraph_guard_robustness walks
# back to the nearest line that is exactly "try:" to find this block.
try:
    # Probe a concrete leaf, not the bare namespace: a pip-uninstall
    # leaves empty ghost namespace dirs that a bare import happily
    # resolves (tests/test_langgraph_guard_robustness.py enforces this).
    from langgraph.graph import StateGraph  # noqa: F401
    HAVE_LANGGRAPH = True
except ImportError:  # pragma: no cover — env-dependent
    HAVE_LANGGRAPH = False


@pytest.mark.skipif(not HAVE_LANGGRAPH, reason="langgraph not installed")
class TestAgainstARealGraph(unittest.TestCase):
    def _graph(self, rc, order):
        from langgraph.graph import END, START, StateGraph
        from langgraph.types import Send

        from consultants.engine.graph import CouncilState
        from consultants.engine.run_control import gate_node

        def _lane(state):
            order.append(f"lane{state.get('lane_idx')}-ran")
            return {}

        def entry(state):
            order.append("entry")
            return {}

        g = StateGraph(CouncilState)
        g.add_node("entry", gate_node(entry, role="entry", run_control=rc))
        g.add_node("lane", gate_node(_lane, role="lane", run_control=rc))
        g.add_edge(START, "entry")
        g.add_conditional_edges(
            "entry",
            lambda s: [Send("lane", {"question": "q", "cwd": "/a",
                                     "lane_idx": i}) for i in range(3)],
            ["lane"],
        )
        g.add_edge("lane", END)
        return g.compile()

    def test_a_cancel_drains_the_graph_without_running_a_node(self):
        rc = RunControl("csl-x")
        rc.request_cancel()
        order: list = []
        self._graph(rc, order).invoke({"question": "q", "cwd": "/a"})
        assert order == [], f"nodes ran after cancel: {order}"

    def test_an_uncancelled_graph_runs_every_node(self):
        # The other half of the same claim: the gate is inert by default.
        rc = RunControl("csl-x")
        order: list = []
        self._graph(rc, order).invoke({"question": "q", "cwd": "/a"})
        assert order.count("entry") == 1
        assert sum(1 for o in order if o.startswith("lane")) == 3

    def test_a_pause_parks_one_lane_while_its_siblings_finish(self):
        """The x-tier guarantee. A graph-level pause would idle every
        sibling on exactly the runs where the fanout is the point."""
        from langgraph.graph import END, START, StateGraph
        from langgraph.types import Send

        from consultants.engine.graph import CouncilState

        rc = RunControl("csl-x", pause_timeout_s=25.0)
        order: list = []
        gate = threading.Event()

        def _lane(state):
            idx = state.get("lane_idx")
            # Only lane 0 sees the pause; the others are released
            # before they reach the gate.
            if idx == 0:
                order.append("lane0-parked")
                gate.set()
            order.append(f"lane{idx}-ran")
            return {}

        def _gated_lane(state):
            if state.get("lane_idx") == 0:
                rc.request_pause("inspect lane 0")
                verdict = rc.check("lane0")
                order.append(f"lane0-{verdict}")
            return _lane(state)

        def entry(state):
            return {}

        g = StateGraph(CouncilState)
        g.add_node("entry", entry)
        g.add_node("lane", _gated_lane)
        g.add_edge(START, "entry")
        g.add_conditional_edges(
            "entry",
            lambda s: [Send("lane", {"question": "q", "cwd": "/a",
                                     "lane_idx": i}) for i in range(3)],
            ["lane"],
        )
        g.add_edge("lane", END)

        def _release():
            deadline = time.time() + 20
            while time.time() < deadline:
                if sum(1 for o in order if o.endswith("-ran")) >= 2:
                    break
                time.sleep(0.02)
            rc.release_pause()

        threading.Thread(target=_release, daemon=True).start()
        g.compile().invoke({"question": "q", "cwd": "/a"})

        assert "lane0-run" in order, order
        released_at = order.index("lane0-run")
        finished_while_parked = [
            o for o in order[:released_at] if o.endswith("-ran")]
        assert len(finished_while_parked) >= 2, (
            f"siblings did NOT run while lane 0 was parked: {order}")


class TestGraphDepsWiring(unittest.TestCase):
    """The gate has to be reachable from the graph builder, or none of
    the above matters in production."""

    @pytest.mark.skipif(not HAVE_LANGGRAPH,
                        reason="langgraph not installed")
    def test_graph_deps_carries_the_run_control(self):
        from consultants.engine.graph import GraphDeps
        deps = GraphDeps(chat_clients={}, models={},
                         enabled_roles=("researcher",), cwd="/a")
        assert deps.run_control is None
        assert deps.run_control_emit is None
        assert deps.run_control_is_closed is None

    def test_both_wrap_sites_install_the_gate(self):
        # Two graph builders (council + follow-up) each have their own
        # ``_wrap``. A gate installed in only one is a run that cannot
        # be cancelled after the first follow-up.
        import ast
        import pathlib
        src = pathlib.Path("consultants/engine/graph.py").read_text()
        tree = ast.parse(src)
        wraps = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "_wrap"
        ]
        assert len(wraps) == 2, f"expected 2 _wrap sites, found {len(wraps)}"
        for w in wraps:
            names = {
                n.id for n in ast.walk(w) if isinstance(n, ast.Name)
            } | {
                n.attr for n in ast.walk(w) if isinstance(n, ast.Attribute)
            }
            assert "gate_node" in names, (
                "a _wrap site does not install the cancel/pause gate")


class TestPauseStateIsExplicit(unittest.TestCase):
    """``paused: true`` conflated "registered" with "something stopped".

    A run can sit registered-but-not-parked for half an hour while the
    runner is inside the adversary checkpoint (observed on
    ``csl-2026-08-02-1054-38cf``), and reading that as "paused" is how a
    pause looks like it did nothing.
    """

    def test_a_fresh_pause_is_pending(self):
        rc = RunControl("csl-x")
        rc.request_pause("hold")
        assert rc.snapshot()["pause_state"] == "pending"
        assert "paused_roles" not in rc.snapshot()

    def test_it_becomes_parked_once_a_node_reaches_the_gate(self):
        rc = RunControl("csl-x", pause_timeout_s=20.0)
        rc.request_pause("hold")
        threading.Thread(target=lambda: rc.check("researcher"),
                         daemon=True).start()
        deadline = time.time() + 5
        while time.time() < deadline and not rc.paused_roles:
            time.sleep(0.01)
        snap = rc.snapshot()
        assert snap["pause_state"] == "parked"
        assert snap["paused_roles"] == ["researcher"]
        rc.release_pause()

    def test_no_pause_state_when_nothing_is_paused(self):
        # Parity: the key must not appear on an untouched run.
        rc = RunControl("csl-x")
        assert "pause_state" not in rc.snapshot()
        rc.request_cancel()
        assert "pause_state" not in rc.snapshot()


class TestPauseDeadlineIsMeasuredFromTheRequest(unittest.TestCase):
    """Observed live on ``csl-2026-08-02-1042-1036``.

    The pause landed at 10:43; the synthesizer did not reach the gate
    until 10:49, because the adversary checkpoint held the runner in
    between. Measuring the wait from *park* time would let the node keep
    waiting past the ``pause_deadline_ts`` that ``status`` is already
    advertising — a deadline visibly in the past while the thing it
    bounds is still blocked.
    """

    def test_a_late_arriving_node_inherits_the_original_deadline(self):
        rc = RunControl("csl-x", pause_timeout_s=10.0)
        # Requested 9.9 s ago: only 0.1 s of the budget is left.
        rc.request_pause("hold", now=time.time() - 9.9)
        started = time.time()
        assert rc.check("synthesizer") == "run"
        waited = time.time() - started
        assert waited < 2.0, (
            f"node waited {waited:.1f}s — it restarted the clock instead "
            "of inheriting the pause's own deadline")

    def test_the_advertised_deadline_is_the_one_enforced(self):
        rc = RunControl("csl-x", pause_timeout_s=10.0)
        rc.request_pause("hold", now=1000.0)
        assert rc.snapshot()["pause_deadline_ts"] == 1010.0


class TestDefaults(unittest.TestCase):
    def test_pause_timeout_is_generous_on_purpose(self):
        # The run is already paid for; resuming a pause nobody released
        # costs one council, abandoning it costs that plus everything
        # already spent.
        assert DEFAULT_PAUSE_TIMEOUT_S >= 600.0
