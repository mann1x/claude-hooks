"""Plan E exit: the escape suite, run against the real sandbox.

Each test runs a command through ``ShellToolProvider`` (the path a lane
takes) and asserts the jail held: 04 §5.11's list plus ptrace of a host
PID, ``mount``, ``unshare``, ``io_uring`` and writes to ``/dev``.

Linux only, and only where the sandbox is available (``shell-preflight``
passes): elsewhere every test is skipped with the preflight's reason.
The project lives in a world-traversable directory under ``/tmp``,
because the command runs as nobody and must reach its bind sources.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

if not sys.platform.startswith("linux"):
    pytest.skip("the Linux sandbox", allow_module_level=True)

from claude_hooks.sandbox import backend_for  # noqa: E402
from claude_hooks.sandbox.linux_bwrap import NOBODY, is_root  # noqa: E402
from claude_hooks.tool_registry.shell_provider import (  # noqa: E402
    ShellSettings, ShellToolProvider)

_PRE = backend_for().preflight()
pytestmark = pytest.mark.skipif(not _PRE.ok, reason=_PRE.reason())

PY = "python3"


@pytest.fixture
def project():
    d = tempfile.mkdtemp(prefix="council-escape-", dir="/tmp")
    os.chmod(d, 0o755)
    p = Path(d)
    (p / "src.txt").write_text("project file\n")
    (p / "probe.py").write_text(PROBE)
    os.chmod(p / "src.txt", 0o644)
    os.chmod(p / "probe.py", 0o644)
    yield p
    shutil.rmtree(d, ignore_errors=True)


def _run(project: Path, command: str, **settings) -> str:
    timeout = settings.pop("t", 60)
    settings.setdefault("free_floor_gib", 0)
    settings.setdefault("free_floor_pct", 0)
    p = ShellToolProvider(sid="csl-escape", settings=ShellSettings(**settings))
    return p.execute("run_command", json.dumps(
        {"command": command, "timeout_s": timeout}),
        str(project), lane="researcher:0")


def _probe(project: Path, what: str, **settings) -> str:
    return _run(project, f"{PY} probe.py {what}", **settings)


#: Small attempts, each printing ``<what>: <OK|errno>``.
PROBE = r'''
import ctypes, errno, os, socket, sys
libc = ctypes.CDLL(None, use_errno=True)
def sc(*a):
    r = libc.syscall(*a); e = ctypes.get_errno()
    return "OK" if r >= 0 else errno.errorcode.get(e, str(e))
X86 = os.uname().machine == "x86_64"
NR = {"unshare": 272 if X86 else 97, "mount": 165 if X86 else 40,
      "ptrace": 101 if X86 else 117, "setns": 308 if X86 else 268}
w = sys.argv[1]
if w == "unshare":
    print("unshare:", sc(NR["unshare"], 0x10000000))
elif w == "mount":
    print("mount:", sc(NR["mount"], b"none", b"/tmp", b"tmpfs", 0, None))
elif w == "io_uring":
    print("io_uring:", sc(425, 8, ctypes.create_string_buffer(120)))
elif w == "ptrace":
    print("ptrace:", sc(NR["ptrace"], 16, int(sys.argv[2]), 0, 0))
elif w == "connect":
    s = socket.socket(); s.settimeout(3)
    try:
        s.connect(("1.1.1.1", 443)); print("connect: OK")
    except OSError as e:
        print("connect:", errno.errorcode.get(e.errno, str(e)))
elif w == "packet":
    try:
        socket.socket(socket.AF_PACKET, socket.SOCK_RAW); print("packet: OK")
    except OSError as e:
        print("packet:", errno.errorcode.get(e.errno, str(e)))
'''


class TestEscape:

    def test_runs_unprivileged(self, project):
        out = _run(project, "id -u; grep CapEff /proc/self/status")
        uid = str(NOBODY) if is_root() else str(os.getuid())
        assert f"\n{uid}\n" in out
        assert "CapEff:\t0000000000000000" in out

    def test_no_write_outside_the_view(self, project):
        out = _run(project, "echo x > src.txt; echo y > new.txt; "
                   "touch /usr/x /etc/x /x; echo ok > /scratch/a && echo wrote")
        assert out.count("Read-only file system") >= 4, out
        assert "wrote" in out
        assert (project / "src.txt").read_text() == "project file\n"
        assert not (project / "new.txt").exists()

    def test_no_host_secrets(self, project):
        home = os.path.expanduser("~")
        out = _run(project, f"ls -a {home} /root; cat {home}/.claude.json; "
                   f"ls {home}/.ssh; echo HOME=$HOME")
        assert "No such file or directory" in out
        assert ".claude.json" not in out.split("--- stderr")[0]
        assert "HOME=/home/sandbox" in out

    def test_no_secret_environment(self, project, monkeypatch):
        monkeypatch.setenv("COUNCIL_TEST_TOKEN", "s3cret")
        monkeypatch.setenv("OLLAMA_API_KEY", "k3y")
        out = _run(project, "env")
        assert "s3cret" not in out and "k3y" not in out
        assert "PATH=" in out

    def test_no_network(self, project):
        assert "connect: ENETUNREACH" in _probe(project, "connect")
        out = _run(project, "cat /proc/net/dev")
        assert "lo:" in out and "eth" not in out and "vmbr" not in out
        assert "packet: EPERM" in _probe(project, "packet")

    def test_no_host_processes_and_no_ptrace(self, project):
        out = _run(project, "ls /proc | grep -c '^[0-9]'")
        assert int(out.split("---\n")[1].split()[0]) < 10
        out = _run(project, f"kill -0 {os.getpid()} && echo VISIBLE")
        assert "VISIBLE" not in out
        assert "ptrace: EPERM" in _probe(project, f"ptrace {os.getpid()}")

    def test_mount_unshare_io_uring_are_refused(self, project):
        for what in ("mount", "unshare", "io_uring"):
            assert f"{what}: EPERM" in _probe(project, what), what

    def test_no_writes_to_dev_and_no_raw_disks(self, project):
        out = _run(project, "touch /dev/x; ls /dev; ls /sys")
        assert "/dev/x" in out and "Read-only file system" in out
        assert "nvme" not in out and "sda" not in out
        assert "ls: cannot access '/sys'" in out

    def test_a_fork_bomb_is_capped(self, project):
        out = _run(project, "for i in $(seq 200); do sleep 5 & done; wait",
                   pids=32, t=30)
        assert "fork" in out.lower() or "resource" in out.lower(), out

    def test_memory_is_capped(self, project):
        out = _run(project, f"{PY} -c 'b = bytearray(512 * 2**20); "
                   "b[::4096] = b\"1\" * (len(b) // 4096); print(\"ALLOC\")'",
                   memory_mib=128, t=60)
        assert "ALLOC" not in out
        assert "exit=137" in out or "MemoryError" in out, out

    def test_a_timeout_kills_the_tree_and_leaves_no_unit(self, project):
        out = _run(project, "sleep 300 & sleep 300", t=2)
        assert "killed_by=timeout" in out
        left = subprocess.run(
            ["systemctl"] + ([] if is_root() else ["--user"])
            + ["list-units", "council-csl-escape*", "--all", "--no-legend"],
            capture_output=True, text=True).stdout
        assert left.strip() == ""

    def test_another_lanes_scratch_is_masked(self, project):
        _run(project, "echo secret > /scratch/mine")
        p = ShellToolProvider(sid="csl-escape", settings=ShellSettings(
            free_floor_gib=0, free_floor_pct=0))
        out = p.execute("run_command", json.dumps(
            {"command": "find .claude-hooks/consultants -mindepth 1 | wc -l; "
                        "cat /scratch/mine"}),
            str(project), lane="researcher:1")
        assert out.split("---\n")[1].split()[0] == "0", out   # empty mount
        assert "No such file" in out

    def test_tests_run_in_the_jail(self, project):
        (project / "test_a.py").write_text("def test_a():\n    assert 1\n")
        os.chmod(project / "test_a.py", 0o644)
        out = _run(project, f"{PY} -m pytest -q -p no:cacheprovider test_a.py")
        if "No module named pytest" in out:
            pytest.skip("no pytest on the host's system python")
        assert "1 passed" in out


# ------------------------------------------------- write mode (plan E3/E4)
def _git(repo, *args) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout


@pytest.fixture
def git_project(project):
    if not shutil.which("git"):
        pytest.skip("git")
    (project / ".gitignore").write_text(".claude-hooks/consultants/\n.env\n")
    (project / ".env").write_text("TOKEN=x\n")
    (project / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (project / "test_calc.py").write_text(
        "from calc import add\n\ndef test_add():\n    assert add(2, 2) == 4\n")
    _git(project, "init", "-q")
    _git(project, "add", "-A")
    _git(project, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "i")
    for f in project.iterdir():
        if f.is_file():
            os.chmod(f, 0o644)
    return project


def _write_run(project, command, lane="researcher:0", sid="csl-wr", t=60):
    p = ShellToolProvider(sid=sid, settings=ShellSettings(
        mode="write", free_floor_gib=0, free_floor_pct=0))
    return p.execute("run_command", json.dumps(
        {"command": command, "timeout_s": t}), str(project), lane=lane)


class TestWriteView:

    def test_a_lane_edits_tests_and_commits_in_its_view_only(self, git_project):
        out = _write_run(git_project,
                         "sed -i 's/a - b/a + b/' calc.py && "
                         f"{PY} -m pytest -q -p no:cacheprovider 2>&1 | tail -1; "
                         "git -c user.name=m -c user.email=m@m commit -qam fix "
                         "&& echo COMMITTED; echo y > .env; touch /usr/x")
        if "No module named pytest" in out:
            pytest.skip("no pytest on the host's system python")
        assert "1 passed" in out and "COMMITTED" in out, out
        assert out.count("Read-only file system") == 2
        assert "M calc.py" in out                     # vs the user's tree
        assert "a - b" in (git_project / "calc.py").read_text()
        assert _git(git_project, "log", "--oneline").count("\n") == 1
        assert (git_project / ".env").read_text() == "TOKEN=x\n"

    def test_lanes_do_not_see_each_other(self, git_project):
        _write_run(git_project, "echo one > lane0.txt; echo s > /scratch/s0")
        out = _write_run(git_project, "ls; cat lane0.txt; "
                         "find .claude-hooks -type f | wc -l",
                         lane="researcher:1")
        assert "No such file or directory" in out
        assert "\n0\n" in out, out           # the store is masked

    def test_an_ignored_parent_of_the_store_is_not_mounted(self, git_project):
        (git_project / ".gitignore").write_text(".claude-hooks/\n.env\n")
        os.chmod(git_project / ".gitignore", 0o644)
        _git(git_project, "-c", "user.name=t", "-c", "user.email=t@t",
             "commit", "-qam", "ignore all of .claude-hooks")
        _write_run(git_project, "echo s > /scratch/mine", sid="csl-anc")
        out = _write_run(git_project, "find .claude-hooks",
                         lane="researcher:1", sid="csl-anc")
        assert "mine" not in out and "researcher-0" not in out, out

    def test_kill_9_of_the_engine_mid_command_leaves_nothing(self, git_project):
        import time
        repo_root = str(Path(__file__).resolve().parent.parent)
        code = (f"import sys, json; sys.path.insert(0, {repo_root!r});"
                "from claude_hooks.tool_registry.shell_provider import "
                "ShellToolProvider, ShellSettings;"
                "p = ShellToolProvider(sid='csl-kill', settings=ShellSettings("
                "mode='write', free_floor_gib=0, free_floor_pct=0));"
                "p.execute('run_command', json.dumps({'command': "
                "'echo partial > made.txt; sleep 300'}), "
                f"{str(git_project)!r}, lane='researcher:0')")
        engine = subprocess.Popen([sys.executable, "-c", code], env=os.environ)
        lane = git_project / ".claude-hooks/consultants/csl-kill/lanes/researcher-0"
        made = lane / "wt" / "made.txt"
        deadline = time.time() + 30
        while not made.exists() and time.time() < deadline:
            time.sleep(0.1)
        assert made.exists(), "the command never started"
        engine.kill()
        engine.wait(10)
        sysd = ["systemctl"] + ([] if is_root() else ["--user"])
        deadline = time.time() + 15
        while time.time() < deadline:
            units = subprocess.run(sysd + ["list-units", "council-csl-kill*",
                                           "--all", "--no-legend"],
                                   capture_output=True, text=True).stdout
            if not units.strip():
                break
            time.sleep(0.2)
        assert not units.strip(), units
        ps = subprocess.run(["ps", "-eo", "args"], capture_output=True,
                            text=True).stdout
        assert "made.txt; sleep 300" not in ps
        assert str(lane) not in Path("/proc/self/mountinfo").read_text()
        from claude_hooks.sandbox import views as V
        V.sweep(live_sids=set())
        v = V.session_views(sid="csl-kill", cwd=str(git_project))
        rec = v.lanes()["researcher-0"]
        assert rec["state"] == "held"
        assert [f["path"] for f in rec["files"]] == ["made.txt"]
        assert _git(git_project, "worktree", "list").count("\n") == 1
        # the restarted lane is told; the command is not re-run
        out = _write_run(git_project, "true", sid="csl-kill")
        assert "view is held" in out   # the sweep froze it: no more commands
