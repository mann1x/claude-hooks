"""Milestone E1 of docs/PLAN-council-cerebriline.md: the fixes the
sandboxed shell needs before it can land.

1. ``POST /control`` tool permissions reach the gate of a running council.
2. The control surface validates the gate's four-level ladder.
3. A lane-isolated effect invalidates only its own lane's cached reads.
4. The shared executor knows which lane is calling.
5. Council leftovers on disk are collected.
7. ``scripts/sandbox_probe.py`` reports a host matrix and leaves nothing.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from consultants.engine.lane_executor import (
    LaneBoundExecutor,
    bind_lane,
    researcher_lane_id,
    tool_executor_lane_id,
)
from consultants.engine.run_control import RunControl
from consultants.server.tool_cache import SharedReadCache

try:
    from fastapi.testclient import TestClient
    HAVE_FASTAPI = True
except ImportError:  # pragma: no cover
    HAVE_FASTAPI = False

REPO = Path(__file__).resolve().parents[1]


def _args(**kw):
    return json.dumps(kw)


class _Recorder:
    def __init__(self, output="ok"):
        self.output = output
        self.calls: list[tuple] = []

    def __call__(self, name, raw_args, cwd, **kw):
        self.calls.append((name, raw_args, kw))
        return self.output


# ====================================================================== #
# 1. runtime permissions reach the gate
# ====================================================================== #
class TestRuntimePermissionsReachTheGate(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        Path(self.tmp, "a.txt").write_text("hello\n")

    def _surface(self, perms):
        from consultants import config as cc
        from consultants.server.tool_surface import build_tool_surface
        _s, executor, _r = build_tool_surface(
            cc.ConsultantsConfig(), runtime_permissions=perms)
        return executor

    def test_a_live_deny_refuses_the_next_call(self):
        rc = RunControl("csl-e1")
        executor = self._surface(rc.tool_permissions)
        first = executor("read_file", _args(path="a.txt"), self.tmp)
        self.assertIn("hello", first)
        # The operator tightens the running council.
        rc.set_tool_permissions({"read_file": "deny"})
        second = executor("read_file", _args(path="a.txt"), self.tmp)
        self.assertTrue(second.startswith("error: tool 'read_file' is not "
                                          "permitted"), second)

    def test_a_cached_read_does_not_bypass_a_live_deny(self):
        rc = RunControl("csl-e1")
        executor = self._surface(rc.tool_permissions)
        executor("list_files", _args(path="."), self.tmp)   # now cached
        rc.set_tool_permissions({"list_files": "deny"})
        out = executor("list_files", _args(path="."), self.tmp)
        self.assertIn("not permitted", out)

    def test_a_broken_source_refuses_rather_than_ignores(self):
        def boom():
            raise RuntimeError("source down")
        executor = self._surface(boom)
        out = executor("glob", _args(pattern="*.txt"), self.tmp)
        self.assertTrue(out.startswith("error"), out)

    def test_legacy_spellings_are_read_as_the_operator_meant(self):
        from claude_hooks.tool_registry import PolicyGate
        gate = PolicyGate()
        self.assertEqual(gate.resolve(
            "read_file", runtime_permissions={"read_file": "allow"}).level,
            "auto")
        self.assertEqual(gate.resolve(
            "read_file", runtime_permissions={"read_file": "ask"}).level,
            "ask_human")
        # A real typo still fails closed.
        self.assertEqual(gate.resolve(
            "read_file", runtime_permissions={"read_file": "alow"}).level,
            "deny")

    def test_the_runner_hands_the_live_source_to_the_surface(self):
        from consultants.server.runner import _live_tool_permissions
        rc = RunControl("x")
        rc.set_tool_permissions({"grep": "deny"})
        src = _live_tool_permissions(SimpleNamespace(run_control=rc))
        self.assertEqual(src(), {"grep": "deny"})
        self.assertIsNone(_live_tool_permissions(SimpleNamespace()))

    def test_resume_restores_the_checkpoint_permissions(self):
        from consultants.server.runner import _restore_tool_permissions
        rc = RunControl("x")
        compiled = SimpleNamespace(get_state=lambda cfg: SimpleNamespace(
            values={"runtime_control": {"tool_permissions": {
                "read_file": "deny"}}}))
        _restore_tool_permissions(compiled, {}, SimpleNamespace(run_control=rc))
        self.assertEqual(rc.tool_permissions(), {"read_file": "deny"})


@unittest.skipUnless(HAVE_FASTAPI, "fastapi not installed")
class TestControlRouteReachesTheGate(unittest.TestCase):
    """The regression: before E1 ``POST /control`` wrote only the graph
    channel, which a running graph never re-reads, and the surface was
    built without any permission source, so a live deny never applied."""

    def test_post_control_deny_applies_to_the_running_executor(self):
        from consultants import config as cc
        from consultants.server.app import SessionState, create_app
        from consultants.server.runner import _live_tool_permissions
        from consultants.server.tool_surface import build_tool_surface

        tmp = tempfile.mkdtemp()
        Path(tmp, "a.txt").write_text("hello\n")
        app = create_app(run_council=None, start_reaper=False)
        s = SessionState(sid="csl-e1", cwd=tmp, question="Q",
                         effort="medium", topology="council",
                         status="running")

        class _Graph:
            def update_state(self, cfg, delta, as_node=None):
                pass
        s._compiled = _Graph()
        s._thread_config = {"configurable": {"thread_id": "csl-e1"}}
        app.state.sessions["csl-e1"] = s

        _specs, executor, _reg = build_tool_surface(
            cc.ConsultantsConfig(),
            runtime_permissions=_live_tool_permissions(s))
        self.assertIn("hello",
                      executor("read_file", _args(path="a.txt"), tmp))

        r = TestClient(app).post(
            "/v1/consult/csl-e1/control",
            json={"runtime_control": {"tool_permissions": {
                "read_file": "deny"}}})
        self.assertEqual(r.status_code, 200, r.text)
        out = executor("read_file", _args(path="a.txt"), tmp)
        self.assertIn("not permitted", out)


# ====================================================================== #
# 2. one ladder on the control surface
# ====================================================================== #
class TestControlLadder(unittest.TestCase):

    def test_every_ladder_level_is_accepted_and_stored(self):
        from claude_hooks.tool_registry import LEVELS
        from consultants.server.control import build_runtime_control_delta
        for level in LEVELS:
            d = build_runtime_control_delta(
                {"tool_permissions": {"read_file": level}})
            self.assertEqual(
                d["runtime_control"]["tool_permissions"]["read_file"], level)

    def test_legacy_spellings_are_stored_as_ladder_levels(self):
        from consultants.server.control import build_runtime_control_delta
        d = build_runtime_control_delta({"tool_permissions": {
            "read_file": "allow", "run_command": "ask", "grep": "deny"}})
        self.assertEqual(d["runtime_control"]["tool_permissions"], {
            "read_file": "auto", "run_command": "ask_human", "grep": "deny"})

    def test_the_control_enum_is_the_gate_ladder(self):
        from claude_hooks.tool_registry import LEVELS
        from consultants.server.control import VALID_TOOL_PERMISSION_VALUES
        self.assertEqual(tuple(VALID_TOOL_PERMISSION_VALUES), tuple(LEVELS))

    def test_unknown_values_are_rejected(self):
        from consultants.server.control import (
            ControlInputError,
            build_runtime_control_delta,
        )
        for bad in ("maybe", 3, None):
            with self.assertRaises(ControlInputError):
                build_runtime_control_delta(
                    {"tool_permissions": {"read_file": bad}})


# ====================================================================== #
# 3. lane-isolated effects
# ====================================================================== #
class TestIsolatedEffects(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        Path(self.tmp, "a.txt").write_text("x")
        self.inner = _Recorder("contents")
        self.cache = SharedReadCache(self.inner)

    def _reads(self):
        return sum(1 for c in self.inner.calls if c[0] == "read_file")

    def test_a_lane_shell_call_leaves_other_lanes_cached(self):
        self.cache("read_file", _args(path="a.txt"), self.tmp, lane="r:0")
        self.cache("run_command", _args(cmd="make"), self.tmp, lane="r:0")
        self.cache("read_file", _args(path="a.txt"), self.tmp, lane="r:1")
        self.assertEqual(self._reads(), 1)        # lane 1 hit the shared copy

    def test_the_lane_itself_reads_again_after_its_effect(self):
        self.cache("read_file", _args(path="a.txt"), self.tmp, lane="r:0")
        self.cache("run_command", _args(cmd="make"), self.tmp, lane="r:0")
        self.cache("read_file", _args(path="a.txt"), self.tmp, lane="r:0")
        self.assertEqual(self._reads(), 2)

    def test_a_diverged_lane_never_feeds_the_shared_cache(self):
        self.cache("run_command", _args(cmd="make"), self.tmp, lane="r:0")
        self.cache("read_file", _args(path="a.txt"), self.tmp, lane="r:0")
        self.cache("read_file", _args(path="a.txt"), self.tmp, lane="r:1")
        self.assertEqual(self._reads(), 2)

    def test_its_next_effect_clears_only_its_private_reads(self):
        self.cache("read_file", _args(path="a.txt"), self.tmp, lane="r:1")
        self.cache("run_command", _args(cmd="a"), self.tmp, lane="r:0")
        self.cache("read_file", _args(path="a.txt"), self.tmp, lane="r:0")
        self.cache("read_file", _args(path="a.txt"), self.tmp, lane="r:0")
        self.assertEqual(self._reads(), 2)        # second lane-0 read hit
        self.cache("run_command", _args(cmd="b"), self.tmp, lane="r:0")
        self.cache("read_file", _args(path="a.txt"), self.tmp, lane="r:0")
        self.assertEqual(self._reads(), 3)
        self.cache("read_file", _args(path="a.txt"), self.tmp, lane="r:1")
        self.assertEqual(self._reads(), 3)        # lane 1 untouched

    def test_without_a_lane_it_is_treated_as_effectful(self):
        self.cache("read_file", _args(path="a.txt"), self.tmp)
        self.cache("run_command", _args(cmd="make"), self.tmp)
        self.cache("read_file", _args(path="a.txt"), self.tmp)
        self.assertEqual(self._reads(), 2)

    def test_other_effectful_tools_still_clear_everything(self):
        self.cache("read_file", _args(path="a.txt"), self.tmp, lane="r:1")
        self.cache("write_file", _args(path="b"), self.tmp, lane="r:0")
        self.cache("read_file", _args(path="a.txt"), self.tmp, lane="r:1")
        self.assertEqual(self._reads(), 2)

    def test_the_lane_is_not_forwarded_to_the_tool(self):
        self.cache("run_command", _args(cmd="make"), self.tmp, lane="r:0")
        self.assertNotIn("lane", self.inner.calls[-1][2])


# ====================================================================== #
# 4. lane identity
# ====================================================================== #
class TestLaneBoundExecutor(unittest.TestCase):

    def test_the_lane_reaches_an_executor_that_accepts_it(self):
        seen = {}

        class Accepting:
            accepts_lane = True

            def __call__(self, name, raw_args, cwd, **kw):
                seen.update(kw)
                return "ok"
        LaneBoundExecutor(Accepting(), "researcher:1")("glob", "{}", ".")
        self.assertEqual(seen, {"lane": "researcher:1"})

    def test_a_plain_executor_is_called_as_before(self):
        def plain(name, raw_args, cwd):
            return name
        self.assertEqual(LaneBoundExecutor(plain, "x")("glob", "{}", "."),
                         "glob")

    def test_none_stays_none(self):
        self.assertIsNone(bind_lane(None, "x"))

    def test_lane_ids(self):
        self.assertEqual(researcher_lane_id({"lane_idx": 2}), "researcher:2")
        self.assertEqual(researcher_lane_id({}), "researcher:solo")
        item = SimpleNamespace(parent_lane_idx=1, lane_idx=3)
        self.assertEqual(tool_executor_lane_id({"tool_plan_item": item}),
                         "tool_executor:1:3")
        self.assertEqual(tool_executor_lane_id({}), "tool_executor:solo:0")

    def test_the_graph_binds_each_researcher_lane(self):
        from consultants.engine import council, graph
        captured = {}

        def fake_researcher(state, **kw):
            captured["executor"] = kw["tool_executor"]
            return {}
        deps = SimpleNamespace(
            enabled_roles=(), chat_clients={"researcher": None},
            tool_executor=SharedReadCache(_Recorder()), tool_specs=[],
            grounding_msgs=[], models={"researcher": "m"}, cwd=".",
            recorder=None, prior_messages_by_role={}, store=None,
            sid="csl-x", think_by_role={})
        orig = council.researcher_node
        council.researcher_node = fake_researcher
        try:
            graph._wrap_researcher(deps)({"lane_idx": 1})
        finally:
            council.researcher_node = orig
        ex = captured["executor"]
        self.assertIsInstance(ex, LaneBoundExecutor)
        self.assertEqual(ex.lane, "researcher:1")
        self.assertIs(ex.inner, deps.tool_executor)


# ====================================================================== #
# 5. collecting what councils leave
# ====================================================================== #
class TestSessionGc(unittest.TestCase):

    def setUp(self):
        self.cwd = Path(tempfile.mkdtemp())
        self.root = self.cwd / ".claude-hooks" / "consultants"
        self.inflight = self.cwd / "inflight.json"
        self._env = os.environ.get("CONSULTANTS_INFLIGHT_PATH")
        os.environ["CONSULTANTS_INFLIGHT_PATH"] = str(self.inflight)

    def tearDown(self):
        if self._env is None:
            os.environ.pop("CONSULTANTS_INFLIGHT_PATH", None)
        else:
            os.environ["CONSULTANTS_INFLIGHT_PATH"] = self._env

    def _session(self, sid, *, coder_age_days=None, record=True):
        d = self.root / sid
        d.mkdir(parents=True)
        if record:
            (d / "metadata.json").write_text("{}")
        if coder_age_days is not None:
            out = d / "coder-out" / "pkg"
            out.mkdir(parents=True)
            f = out / "x.py"
            f.write_text("print(1)\n")
            t = time.time() - coder_age_days * 86400
            for p in (f, out, out.parent):
                os.utime(p, (t, t))
        return d

    def test_prune_empty_keeps_records(self):
        from consultants.server.session_gc import prune_empty
        d = self._session("s1")
        (d / "coder-out").mkdir()
        (d / "empty" / "deeper").mkdir(parents=True)
        self.assertEqual(prune_empty(d), 3)
        self.assertTrue((d / "metadata.json").exists())
        self.assertFalse((d / "coder-out").exists())

    def test_stale_coder_out_is_removed_fresh_is_kept(self):
        from consultants.server.session_gc import sweep_project
        old = self._session("old", coder_age_days=40)
        new = self._session("new", coder_age_days=1)
        actions = sweep_project(self.cwd, ttl_days=30)
        self.assertFalse((old / "coder-out").exists())
        self.assertTrue((new / "coder-out" / "pkg" / "x.py").exists())
        self.assertTrue((old / "metadata.json").exists())
        self.assertEqual([a["sid"] for a in actions
                          if a["action"] == "remove coder-out"], ["old"])

    def test_dry_run_removes_nothing(self):
        from consultants.server.session_gc import sweep_project
        old = self._session("old", coder_age_days=40)
        actions = sweep_project(self.cwd, ttl_days=30, dry_run=True)
        self.assertTrue((old / "coder-out").exists())
        self.assertEqual(actions[0]["action"], "remove coder-out")

    def test_in_flight_and_skipped_sessions_are_left_alone(self):
        from consultants.server import inflight
        from consultants.server.session_gc import sweep_project
        a = self._session("running", coder_age_days=40)
        b = self._session("live", coder_age_days=40)
        inflight.register({"sid": "running", "cwd": str(self.cwd)})
        sweep_project(self.cwd, ttl_days=30, skip_sids=["live"])
        self.assertTrue((a / "coder-out").exists())
        self.assertTrue((b / "coder-out").exists())

    def test_the_reaper_sweep_is_throttled_per_project(self):
        from consultants.server import session_gc
        self._session("old", coder_age_days=40)
        session_gc._last_sweep.pop(str(self.cwd), None)
        now = time.time()
        first = session_gc.maybe_sweep([str(self.cwd)], now=now)
        self._session("old2", coder_age_days=40)
        second = session_gc.maybe_sweep([str(self.cwd)], now=now + 60)
        self.assertGreaterEqual(first, 1)
        self.assertEqual(second, 0)
        self.assertTrue((self.root / "old2" / "coder-out").exists())

    def test_cli_gc(self):
        self._session("old", coder_age_days=40)
        env = dict(os.environ, PYTHONPATH=str(REPO))
        p = subprocess.run(
            [sys.executable, "-m", "consultants.cli", "gc", "--cwd",
             str(self.cwd), "--dry-run"],
            capture_output=True, text=True, env=env, cwd=str(REPO))
        self.assertEqual(p.returncode, 0, p.stderr)
        out = json.loads(p.stdout)
        self.assertTrue(out["dry_run"])
        self.assertEqual(out["actions"][0]["sid"], "old")
        self.assertTrue((self.root / "old" / "coder-out").exists())


# ====================================================================== #
# 7. the probe
# ====================================================================== #
class TestSandboxProbe(unittest.TestCase):

    def test_json_matrix_and_no_leftovers(self):
        tmp = Path(tempfile.mkdtemp())
        p = subprocess.run(
            [sys.executable, str(REPO / "scripts" / "sandbox_probe.py"),
             "--json", str(tmp)],
            capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stderr)
        report = json.loads(p.stdout)
        self.assertEqual(set(report["host"]), {
            "namespaces", "cgroup_v2", "seccomp", "landlock", "bwrap",
            "platform_sandbox"})
        self.assertTrue(report["host"]["landlock"].get("optional")
                        or report["host"]["landlock"]["ok"] is None)
        path = report["paths"][0]
        self.assertIn("free", path)
        self.assertIn("ficlone", path)
        self.assertEqual(list(tmp.iterdir()), [])

    def test_human_output_renders(self):
        tmp = tempfile.mkdtemp()
        p = subprocess.run(
            [sys.executable, str(REPO / "scripts" / "sandbox_probe.py"), tmp],
            capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("sandbox probe", p.stdout)
        self.assertIn("landlock", p.stdout)


if __name__ == "__main__":
    unittest.main()
