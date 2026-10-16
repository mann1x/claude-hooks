"""Plan E (P1): the sandboxed ``run_command`` — everything but the jail.

The seccomp program (run through a tiny BPF interpreter, so no kernel is
needed), the cost/reach classifier, the gate's per-call hook, the lane
plumbing from ``LaneBoundExecutor`` to the provider, the provider
against a fake backend, enablement (off by default, ``--shell``,
follow-up banking, restarts), config and CLI. The jail itself is
``tests/test_council_shell_escape.py``.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from claude_hooks.sandbox import Preflight, RunResult, seccomp
from claude_hooks.sandbox.classify import classify
from claude_hooks.tool_registry import (ASK_ASSISTANT, ASK_HUMAN, AUTO,
                                        BuiltinToolProvider, PolicyGate,
                                        ToolRegistry)
from claude_hooks.tool_registry.shell_provider import (INTERRUPTED_NOTE,
                                                       ShellSettings,
                                                       ShellToolProvider,
                                                       interrupted_commands)

X86 = seccomp.AUDIT_ARCH["x86_64"]
A64 = seccomp.AUDIT_ARCH["aarch64"]
EPERM = seccomp.RET_ERRNO | seccomp.EPERM


# --------------------------------------------------------------- seccomp
class TestSeccomp:

    @pytest.fixture(params=["x86_64", "aarch64"])
    def arch(self, request):
        return request.param

    def _run(self, arch, name, *args, nr=None):
        prog = seccomp.instructions(arch)
        n = seccomp.SYSCALLS[arch][name] if nr is None else nr
        return seccomp.evaluate(prog, nr=n, arch=seccomp.AUDIT_ARCH[arch],
                                args=args)

    def test_every_denied_syscall_returns_eperm(self, arch):
        table = seccomp.SYSCALLS[arch]
        for name in seccomp.DENIED:
            if name in table:
                assert self._run(arch, name) == EPERM, name
        missing = set(seccomp.DENIED) - set(table)
        assert missing <= ({"iopl", "ioperm"} if arch == "aarch64" else set())

    def test_ordinary_syscalls_are_allowed(self, arch):
        assert self._run(arch, "", nr=0) == seccomp.RET_ALLOW   # read / io_setup
        assert self._run(arch, "ioctl", 1, 0x5401) == seccomp.RET_ALLOW  # TCGETS

    def test_clone_is_judged_by_its_flags(self, arch):
        assert self._run(arch, "clone", 0x11) == seccomp.RET_ALLOW   # fork
        for flag in (0x10000000, 0x20000000, 0x40000000, 0x00020000,
                     0x08000000, 0x04000000, 0x02000000, 0x80):
            assert self._run(arch, "clone", 0x11 | flag) == EPERM, hex(flag)
        assert self._run(arch, "clone3") == seccomp.RET_ERRNO | seccomp.ENOSYS

    def test_terminal_injection_and_raw_sockets(self, arch):
        assert self._run(arch, "ioctl", 0, seccomp.TIOCSTI) == EPERM
        assert self._run(arch, "ioctl", 0, seccomp.TIOCLINUX) == EPERM
        assert self._run(arch, "socket", 17, 3, 0) == EPERM            # packet
        assert self._run(arch, "socket", 16, 3, 0) == seccomp.RET_ALLOW  # route
        assert self._run(arch, "socket", 16, 3, 9) == EPERM            # audit
        assert self._run(arch, "socket", 2, 1, 0) == seccomp.RET_ALLOW   # inet

    def test_a_foreign_arch_or_x32_call_is_killed(self):
        prog = seccomp.instructions("x86_64")
        assert seccomp.evaluate(prog, nr=0, arch=A64) == seccomp.RET_KILL_PROCESS
        assert seccomp.evaluate(prog, nr=0x40000000 | 165, arch=X86) \
            == seccomp.RET_KILL_PROCESS

    def test_the_raw_program_is_sock_filters(self, arch):
        raw = seccomp.build_filter(arch)
        assert len(raw) == 8 * len(seccomp.instructions(arch))
        assert all(0 <= jt <= 255 and 0 <= jf <= 255
                   for _, jt, jf, _ in seccomp.instructions(arch))


# ------------------------------------------------------------ classifier
class TestClassify:

    @pytest.mark.parametrize("cmd,klass,level", [
        ("ls -la src", "investigate", AUTO),
        ("git log --oneline -5", "investigate", AUTO),
        ("python -m pytest -q tests/test_x.py", "test", AUTO),
        ("pytest -x && ruff check .", "test", AUTO),
        ("cargo test --release", "test", AUTO),
        ("python3 scripts/run.py", "run", ASK_ASSISTANT),
        ("git push origin main", "unmatched", ASK_ASSISTANT),
        ("frobnicate --all", "unmatched", ASK_ASSISTANT),
        ("aws s3 ls", "spend", ASK_HUMAN),
        ("ls; sh -c 'kubectl get pods'", "spend", ASK_HUMAN),
        ("echo $(curl -s example.com)", "spend", ASK_HUMAN),
        ("echo 'unterminated", "unmatched", ASK_ASSISTANT),
    ])
    def test_the_table(self, cmd, klass, level):
        v = classify(command=cmd)
        assert (v.klass, v.level) == (klass, level), v.reason

    def test_argv_and_a_long_timeout(self):
        assert classify(argv=["ls"]).level == AUTO
        v = classify(argv=["ls"], timeout_s=300)
        assert (v.klass, v.level) == ("long", ASK_ASSISTANT)

    def test_spend_level_is_the_callers(self):
        assert classify(command="aws s3 ls",
                        spend_level=ASK_ASSISTANT).level == ASK_ASSISTANT


# --------------------------------------------------- gate + registry lane
class _Recorder(ShellToolProvider):
    """The real provider's surface, recording instead of running."""

    def __init__(self, **kw):
        super().__init__(sid="csl-t", backend=_FakeBackend(), **kw)
        self.calls = []

    def execute(self, tool, raw_args, cwd, *, lane=None):
        self.calls.append(lane)
        return "ran"


class TestGateAndLane:

    def test_the_classifier_sits_between_operator_and_default(self):
        reg = ToolRegistry([_Recorder()], approval_fn=lambda d: True)
        g = reg.gate
        args = json.dumps({"command": "aws s3 ls"})
        assert g.resolve("run_command", raw_args=args).level == ASK_ASSISTANT
        assert "classification (spend" in g.resolve(
            "run_command", raw_args=args).reason
        g.overrides["run_command"] = "deny"
        assert g.resolve("run_command", raw_args=args).level == "deny"
        g.overrides.clear()
        assert g.resolve("run_command", raw_args=args, runtime_permissions={
            "run_command": "auto"}).level == AUTO

    def test_a_raising_classifier_is_not_auto(self):
        g = PolicyGate(call_levels={"x": lambda raw: 1 / 0})
        assert g.resolve("x").level == ASK_ASSISTANT

    def test_the_lane_reaches_a_provider_that_takes_one(self):
        from consultants.engine.lane_executor import LaneBoundExecutor
        from consultants.server.tool_cache import SharedReadCache
        shell = _Recorder()
        reg = ToolRegistry([BuiltinToolProvider(), shell],
                           approval_fn=lambda d: True)
        ex = LaneBoundExecutor(SharedReadCache(
            reg.as_executor(), read_only=reg.gate.read_only_tools),
            "researcher:2")
        assert ex("run_command", '{"command": "ls"}', "/x") == "ran"
        assert shell.calls == ["researcher:2"]

    def test_a_provider_without_lanes_is_called_as_before(self, tmp_path):
        reg = ToolRegistry([BuiltinToolProvider()])
        (tmp_path / "a.txt").write_text("hello\n")
        out = reg.as_executor()("read_file", '{"path": "a.txt"}',
                                str(tmp_path), lane="researcher:1")
        assert "hello" in out


# -------------------------------------------------------------- provider
class _FakeBackend:
    name = "fake"

    def __init__(self, ok=True, result=None):
        self.ok, self.result, self.specs = ok, result, []

    def preflight(self, *, refresh=False):
        return Preflight(backend="fake", ok=self.ok,
                         missing=[] if self.ok else ["netns"],
                         layers={"netns": "ok" if self.ok else "absent"})

    def run(self, spec):
        self.specs.append(spec)
        if callable(self.result):
            return self.result(spec)
        return self.result or RunResult(exit_code=0, stdout="hello\n")


def _provider(tmp_path, backend=None, **settings):
    return ShellToolProvider(sid="csl-t", backend=backend or _FakeBackend(),
                             settings=ShellSettings(**settings))


class TestProvider:

    def test_offers_one_tool_not_read_only(self, tmp_path):
        p = _provider(tmp_path)
        assert [s["function"]["name"] for s in p.specs()] == ["run_command"]
        assert p.is_read_only("run_command") is False
        assert p.accepts_lane is True

    @pytest.mark.parametrize("args,needle", [
        ({}, "exactly one of command or argv"),
        ({"command": "ls", "argv": ["ls"]}, "exactly one"),
        ({"argv": []}, "argv must be"),
        ({"command": "  "}, "command must be"),
        ({"command": "ls", "timeout_s": -1}, "timeout_s"),
    ])
    def test_bad_arguments_are_errors(self, tmp_path, args, needle):
        out = _provider(tmp_path).execute("run_command", json.dumps(args),
                                          str(tmp_path))
        assert out.startswith("error:") and needle in out

    def test_no_sandbox_no_shell(self, tmp_path):
        p = _provider(tmp_path, backend=_FakeBackend(ok=False))
        out = p.execute("run_command", '{"command": "ls"}', str(tmp_path))
        assert out.startswith("error: no sandbox on this host")
        assert "netns" in out

    def test_a_run_is_scoped_to_the_lane_and_logged(self, tmp_path):
        be = _FakeBackend()
        p = ShellToolProvider(sid="csl-t", backend=be,
                              extra_roots=(str(tmp_path / "other"),))
        (tmp_path / "sub").mkdir()
        out = p.execute("run_command", json.dumps(
            {"command": "ls", "cwd": "sub", "timeout_s": 9999}),
            str(tmp_path), lane="researcher:3")
        assert out.startswith("exit=0") and "hello" in out
        spec = be.specs[0]
        assert spec.cwd == str((tmp_path / "sub").resolve())
        assert spec.timeout_s == 600                     # capped
        assert spec.argv == ["/bin/sh", "-c", "ls"]
        assert str(tmp_path.resolve()) in spec.ro_roots
        lane = tmp_path / ".claude-hooks/consultants/csl-t/lanes/researcher-3"
        assert spec.scratch == str(lane / "scratch")
        events = [json.loads(x)["event"] for x in
                  (lane / "commands.jsonl").read_text().splitlines()]
        assert events == ["start", "end"]

    def test_a_cwd_outside_the_roots_is_refused(self, tmp_path):
        out = _provider(tmp_path).execute(
            "run_command", '{"command": "ls", "cwd": "/etc"}', str(tmp_path))
        assert out.startswith("error: cwd '/etc' is outside")

    def test_output_is_capped_with_the_harness_marker(self, tmp_path):
        be = _FakeBackend(result=RunResult(exit_code=0, stdout="x" * 100_000))
        out = _provider(tmp_path, backend=be).execute(
            "run_command", '{"command": "yes"}', str(tmp_path))
        assert len(out) < 21_000
        assert "the harness removed them" in out

    def test_scratch_changes_are_listed(self, tmp_path):
        def write(spec):
            Path(spec.scratch, "out.txt").write_text("1")
            return RunResult(exit_code=0)
        out = _provider(tmp_path, backend=_FakeBackend(result=write)).execute(
            "run_command", '{"command": "x"}', str(tmp_path))
        assert "A /scratch/out.txt" in out

    def test_a_setup_failure_is_an_error_not_an_exit_code(self, tmp_path):
        be = _FakeBackend(result=RunResult(
            exit_code=None, killed_by="setup", setup_error="bwrap: nope"))
        out = _provider(tmp_path, backend=be).execute(
            "run_command", '{"command": "x"}', str(tmp_path))
        assert out.startswith("error: the sandbox could not run the command")

    def test_an_interrupted_command_is_reported_once_never_rerun(self, tmp_path):
        lane = tmp_path / ".claude-hooks/consultants/csl-t/lanes/researcher-0"
        lane.mkdir(parents=True)
        (lane / "commands.jsonl").write_text(json.dumps(
            {"event": "start", "n": 1, "argv": ["make", "all"]}) + "\n")
        be = _FakeBackend()
        p = _provider(tmp_path, backend=be)
        first = p.execute("run_command", '{"command": "ls"}', str(tmp_path),
                          lane="researcher:0")
        assert first.startswith(INTERRUPTED_NOTE.split("(")[0])
        assert "`make all`" in first
        assert [s.argv for s in be.specs] == [["/bin/sh", "-c", "ls"]]
        assert interrupted_commands(str(lane / "commands.jsonl")) == []
        # a new engine (a new provider) does not repeat it
        again = _provider(tmp_path).execute(
            "run_command", '{"command": "ls"}', str(tmp_path),
            lane="researcher:0")
        assert "interrupted" not in again

    def test_the_free_space_floor_and_the_lane_quota(self, tmp_path, monkeypatch):
        import shutil as sh
        from collections import namedtuple
        du = namedtuple("du", "total used free")
        monkeypatch.setattr(sh, "disk_usage",
                            lambda p: du(100 * 2**30, 99 * 2**30, 2**30))
        out = _provider(tmp_path).execute("run_command", '{"command": "ls"}',
                                          str(tmp_path))
        assert out.startswith("error: the disk holding") and "floor" in out
        monkeypatch.undo()
        p = _provider(tmp_path, lane_disk_gib=0.000001, free_floor_gib=0,
                      free_floor_pct=0)
        scratch = Path(p.lane_dir(str(tmp_path), None), "scratch")
        scratch.mkdir(parents=True)
        (scratch / "big").write_bytes(b"0" * 4096)
        out = p.execute("run_command", '{"command": "ls"}', str(tmp_path))
        assert out.startswith("error: this lane's /scratch holds")

    def test_one_command_at_a_time_per_lane(self, tmp_path):
        live, peak = [0], [0]
        lock = threading.Lock()

        def slow(spec):
            with lock:
                live[0] += 1
                peak[0] = max(peak[0], live[0])
            time.sleep(0.05)
            with lock:
                live[0] -= 1
            return RunResult(exit_code=0)
        p = _provider(tmp_path, backend=_FakeBackend(result=slow))
        ts = [threading.Thread(target=p.execute, args=(
            "run_command", '{"command": "x"}', str(tmp_path)),
            kwargs={"lane": "researcher:1"}) for _ in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert peak[0] == 1


# ------------------------------------------------------------ enablement
class _Cfg:
    def __init__(self, default="off", enabled=True):
        from consultants import config as cc
        self.tools = cc.ToolsConfig(enabled=enabled)
        self.tools.shell.default = default


class TestEnablement:

    def test_parse(self):
        from consultants.server import shell_enable as se
        assert se.parse(None) is None and se.parse(False) is False
        assert se.parse(True) == {"mode": "read", "net": "none"}
        assert se.parse("read") == {"mode": "read", "net": "none"}
        assert se.parse("write") == {"mode": "write", "net": "none"}
        for bad, needle in [("rw", "mode must be"), ({"net": "host"}, "plan E6"),
                            (3, "must be")]:
            with pytest.raises(se.ShellRequestError, match=needle):
                se.parse(bad)

    def test_off_by_default_on_by_request_or_knob(self):
        from consultants.server import shell_enable as se
        assert se.resolve_ask(_Cfg(), None) is None
        assert se.resolve_ask(_Cfg(), True)["since"] == "the ask"
        assert se.resolve_ask(_Cfg("always"), None)["since"].startswith("config")
        assert se.resolve_ask(_Cfg("always"), False) is None

    def test_a_follow_up_banks_and_later_ones_inherit(self):
        from consultants.server import shell_enable as se
        run, bank = se.resolve_follow_up(_Cfg(), None, None, 1)
        assert run is None and bank is None
        run, bank = se.resolve_follow_up(_Cfg(), True, None, 2)
        assert run["since"] == "follow-up #2" and bank == run
        run3, bank3 = se.resolve_follow_up(_Cfg(), None, bank, 3)
        assert run3["since"] == "follow-up #2" and bank3 == bank
        run4, bank4 = se.resolve_follow_up(_Cfg(), False, bank, 4)
        assert run4 is None and bank4 is None

    def test_provenance_only_when_on(self):
        from consultants.server import shell_enable as se
        assert se.with_provenance("A", None) == "A"
        out = se.with_provenance("A", {"mode": "read", "net": "none",
                                       "since": "follow-up #2"})
        assert out.endswith("_shell: enabled since follow-up #2 (read, net none)_\n")

    def test_preflight_refuses_with_the_reason(self, monkeypatch):
        from consultants.server import shell_enable as se
        import claude_hooks.sandbox as sb
        assert se.preflight_error(_Cfg(), None) is None
        monkeypatch.setattr(sb, "backend_for",
                            lambda name="auto": sb.UnavailableBackend(
                                "x", "nothing here"))
        err = se.preflight_error(_Cfg(), {"since": "the ask"})
        assert "cannot provide it" in err and "nothing here" in err
        assert "[tools] enabled = false" in se.preflight_error(
            _Cfg(enabled=False), {"since": "the ask"})

    def test_windows_and_macos_say_why(self, monkeypatch):
        import claude_hooks.sandbox as sb
        for plat, needle in (("win32", "AppContainer"), ("darwin", "Seatbelt")):
            monkeypatch.setattr(sb.sys, "platform", plat)
            pre = sb.backend_for().preflight()
            assert pre.ok is False and needle in pre.reason()


# --------------------------------------------------------- config + CLI
class TestConfigAndCli:

    def test_defaults_and_render(self, tmp_path):
        from consultants import config as cc
        cfg = cc.ConsultantsConfig()
        assert cfg.tools.shell.default == "off"
        text = "\n".join(cc._render_shell(cfg.tools.shell))
        assert "\n[tools.shell]" not in text and "# [tools.shell]" in text
        cfg.tools.shell.default = "always"
        cfg.tools.shell.ro_binds = ["/opt/x"]
        text = "\n".join(cc._render_shell(cfg.tools.shell))
        assert '[tools.shell]\ndefault = "always"\nro_binds = ["/opt/x"]' in text

    def test_setter_validates(self, tmp_path):
        from consultants import config as cc
        for values, needle in [({"default": "sometimes"}, "default"),
                               ({"mode": "rw"}, "mode"),
                               ({"net": "host"}, "plan E6"),
                               ({"pids": 0}, "pids"),
                               ({"nope": 1}, "unknown key")]:
            with pytest.raises(ValueError, match=needle):
                cc.set_shell(values=values, scope="project", cwd=tmp_path)

    def test_load_tolerates_a_bad_value(self):
        from consultants import config as cc
        sc = cc.ShellConfig()
        cc.apply_shell(sc, {"default": "always", "pids": "many"}, strict=False)
        assert sc.default == "always" and sc.pids == 256

    def test_cli_flags(self):
        from consultants import cli
        p = cli.build_parser()
        assert p.parse_args(["consult", "--message", "q", "--shell"]).shell == "read"
        assert p.parse_args(["consult", "--message", "q", "--no-shell"]).shell is False
        assert p.parse_args(["consult", "--message", "q"]).shell is None
        fu = p.parse_args(["follow-up", "csl-1", "--message", "q", "--shell"])
        assert fu.shell == "read"
        a = p.parse_args(["config", "set-shell", "--default", "always",
                          "--ro-bind", "/opt/t"])
        assert a.fn is cli.cmd_config_set_shell and a.ro_bind == ["/opt/t"]
        assert p.parse_args(["shell-preflight"]).fn is cli.cmd_shell_preflight


# ------------------------------------------------------------------ graph
def test_roles_without_a_lane_do_not_get_the_shell():
    from consultants.engine.graph import GraphDeps, _tools_for
    seen = []
    specs = [{"type": "function", "function": {"name": n}}
             for n in ("read_file", "run_command")]
    deps = GraphDeps(chat_clients={}, models={}, enabled_roles=(), cwd="/x",
                     tool_specs=specs, tooled_roles=("critic",),
                     tool_executor=lambda *a, **k: seen.append(a[0]) or "ok")
    kw = _tools_for(deps, "critic")
    assert [s["function"]["name"] for s in kw["tool_specs"]] == ["read_file"]
    assert kw["tool_executor"]("run_command", "{}", "/x").startswith("error:")
    assert kw["tool_executor"]("read_file", "{}", "/x") == "ok"
    assert seen == ["read_file"]
    deps.tool_specs = specs[:1]
    assert _tools_for(deps, "critic")["tool_specs"] is deps.tool_specs


# -------------------------------------------------------------- the app
fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture
def ready_sandbox(monkeypatch):
    import claude_hooks.sandbox as sb

    class Ready:
        name = "fake"

        def preflight(self, *, refresh=False):
            return Preflight(backend="fake", ok=True)
    monkeypatch.setattr(sb, "backend_for", lambda name="auto": Ready())


def _done(state, runner_input):
    from consultants.engine import storage
    seen = runner_input.get("shell")
    _done.runs.append(seen)
    cwd = Path(runner_input["cwd"])
    storage.write_consultation(storage.ConsultationResult(
        session_id=state.sid, created=time.strftime("%Y-%m-%dT%H:%M:%S"),
        question=runner_input["question"], models={}, topology=state.topology,
        effort=state.effort, final_answer="answer", turns=[],
        duration_seconds=0.1, status="completed", cwd=str(cwd),
        parent_sid=state.parent_sid,
        root_sid=getattr(state, "root_sid", None) or state.sid), cwd=cwd)
    state.final_answer = "answer"
    state.status = "completed"
    state.finished_at = time.time()


def _wait(pred, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


class TestApp:

    @pytest.fixture(autouse=True)
    def _reset(self):
        _done.runs = []

    def _client(self):
        from consultants.server.app import create_app
        app = create_app(run_council=_done, run_follow_up=_done,
                         start_reaper=False)
        return app, TestClient(app)

    def test_off_by_default_and_nothing_new_is_recorded(self, tmp_path):
        from consultants.server import inflight
        app, c = self._client()
        sid = c.post("/v1/consult", json={"message": "q",
                                          "cwd": str(tmp_path)}).json()["sid"]
        assert _wait(lambda: _done.runs)
        assert _done.runs == [None]
        assert app.state.sessions[sid].shell is None
        assert "shell" not in json.dumps(inflight.load())

    def test_a_host_that_cannot_sandbox_refuses_before_running(
            self, tmp_path, monkeypatch):
        import claude_hooks.sandbox as sb
        monkeypatch.setattr(sb, "backend_for", lambda name="auto":
                            sb.UnavailableBackend("x", "no namespaces"))
        _, c = self._client()
        r = c.post("/v1/consult", json={"message": "q", "cwd": str(tmp_path),
                                        "shell": "read"})
        assert r.status_code == 409 and "no namespaces" in r.json()["detail"]
        r = c.post("/v1/consult", json={"message": "q", "cwd": str(tmp_path),
                                        "shell": {"mode": "read", "net": "host"}})
        assert r.status_code == 400 and "plan E6" in r.json()["detail"]
        assert _done.runs == []

    def test_a_follow_up_adds_the_shell_and_later_ones_keep_it(
            self, tmp_path, ready_sandbox):
        from consultants.server import inflight
        app, c = self._client()
        sid = c.post("/v1/consult", json={"message": "q",
                                          "cwd": str(tmp_path)}).json()["sid"]
        assert _wait(lambda: app.state.sessions[sid].status == "completed")
        fu = c.post(f"/v1/consult/{sid}/follow-up", json={
            "message": "now run the tests", "cwd": str(tmp_path),
            "shell": "read"}).json()
        assert fu["ok"] is True
        assert _wait(lambda: len(_done.runs) == 2)
        assert _done.runs[1]["since"] == "follow-up #1"
        child = fu["sid"]
        assert _wait(lambda: app.state.sessions[child].status == "completed")
        fu2 = c.post(f"/v1/consult/{child}/follow-up", json={
            "message": "and again", "cwd": str(tmp_path)}).json()
        assert _wait(lambda: len(_done.runs) == 3)
        assert _done.runs[2]["since"] == "follow-up #1"     # inherited
        from consultants.engine import storage
        assert storage.read_consultancy(tmp_path, sid)["shell"]["since"] \
            == "follow-up #1"
        assert _wait(lambda: app.state.sessions[fu2["sid"]].status == "completed")
        c.post(f"/v1/consult/{fu2['sid']}/follow-up", json={
            "message": "no shell now", "cwd": str(tmp_path), "shell": False})
        assert _wait(lambda: len(_done.runs) == 4)
        assert _done.runs[3] is None
        assert "shell" not in (storage.read_consultancy(tmp_path, sid) or {})
        assert _wait(lambda: inflight.load() == [])

    def test_a_restarted_engine_resumes_with_the_shell(self, tmp_path,
                                                        ready_sandbox):
        from consultants.server import inflight
        from consultants.server.app import create_app
        shell = {"mode": "read", "net": "none", "since": "the ask"}
        inflight.register({
            "sid": "csl-sh", "kind": "consult", "cwd": str(tmp_path),
            "question": "q", "effort": "high", "extra_roots": [],
            "root_sid": "csl-sh", "started_at": time.time() - 60,
            "state": "suspended", "shell": shell})
        create_app(run_council=_done, start_reaper=False, resume_inflight=True)
        assert _wait(lambda: _done.runs)
        assert _done.runs == [shell]
