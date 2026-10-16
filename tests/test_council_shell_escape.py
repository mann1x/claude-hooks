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

    def test_tests_run_in_the_jail(self, project):
        (project / "test_a.py").write_text("def test_a():\n    assert 1\n")
        os.chmod(project / "test_a.py", 0o644)
        out = _run(project, f"{PY} -m pytest -q -p no:cacheprovider test_a.py")
        if "No module named pytest" in out:
            pytest.skip("no pytest on the host's system python")
        assert "1 passed" in out
