"""The P1 Linux backend: bwrap inside a transient systemd scope.

The interim backend of the design (04 §5.2, plan E2): bubblewrap is on
the host, verified, and needs no build step, so a council can have a
shell before the static ``council-sandbox`` launcher lands. The command
line is a chain of programs that each set up one layer and ``exec`` the
next, so the command ends up as the scope's own process:

    systemd-run --scope -p MemoryMax= -p TasksMax= -p CPUQuota=   layer 1 (cgroup)
      prlimit --core=0 --nofile= --fsize=                         layer 5 (rlimits)
        setpriv --reuid=65534 … --nnp --bounding-set=-all         layers 2, 6 (uid, caps, nnp)
          bwrap --unshare-all … --seccomp FD                      layers 3, 4, 7
            <argv>

Two orders here are measured, not stylistic (solidpc, 2026-10-16):

* **Drop to nobody before bwrap.** Root running ``bwrap --uid 65534``
  maps the inside uid back to the *caller's*: a file written inside is
  owned by host uid 0, so an escape would land as root.
* **A fresh root, never ``--ro-bind / /``.** ``/root/.claude.json`` is
  world-readable here; being nobody does not protect it, not mounting it
  does.

``/dev`` is bwrap's minimal set, remounted read-only after a private
``/dev/shm``, and the root itself is remounted read-only last: both are
tmpfs mounts bwrap creates writable for the sandbox user, so without it
``touch /x`` or ``touch /etc/x`` succeeds (privately, but the design
says read-only).
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
from typing import Optional

from claude_hooks.sandbox import seccomp
from claude_hooks.sandbox.spec import (HOME, SCRATCH, Limits, Preflight,
                                       RunResult, RunSpec)

log = logging.getLogger("claude_hooks.sandbox.bwrap")

NAME = "bwrap"
NOBODY = 65534
SLICE = "claude-council.slice"
#: Layers a shell cannot run without (§5.3, "escape-critical rule").
REQUIRED = ("userns", "mountns", "pidns", "netns", "cgroup", "nnp", "seccomp")
#: Read from the host's ``/etc``; everything else there stays out.
ETC_FILES = ("passwd", "group", "hosts", "resolv.conf", "nsswitch.conf",
             "localtime", "ld.so.cache", "ld.so.conf", "ld.so.conf.d",
             "alternatives", "ssl", "ca-certificates", "ca-certificates.conf",
             "mime.types", "magic", "python3", "pythonstartup")
#: The usr-merge links (or real dirs on an unmerged host).
TOP_DIRS = ("bin", "sbin", "lib", "lib32", "lib64", "libx32")
#: The council's session store under a root, masked in every command.
STORE = (".claude-hooks", "consultants")
#: Bytes of each stream kept from either end while the command runs.
CAPTURE_EDGE_BYTES = 256 * 1024
_PREFLIGHT_TTL_S = 600.0


def is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def _unit(name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", name or "cmd").strip("-")[:60]
    return f"council-{safe or 'cmd'}-{uuid.uuid4().hex[:8]}"


def sandbox_env(spec: RunSpec) -> dict[str, str]:
    """The allowlisted environment (layer 9). Everything else — tokens,
    keys, ``OLLAMA_*``, ``SSH_AUTH_SOCK`` — never reaches the command."""
    # A bound ``bin`` directory goes on PATH itself, an install prefix
    # by its ``bin`` (the toolchains of claude_hooks.sandbox.toolchains
    # come both ways).
    path = [b if os.path.basename(b.rstrip("/")) == "bin"
            else os.path.join(b, "bin") for b in spec.ro_binds]
    path = [p for p in dict.fromkeys(path) if os.path.isdir(p)]
    path += ["/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/usr/bin",
             "/sbin", "/bin"]
    env = {
        "PATH": ":".join(path),
        "HOME": HOME,
        "TMPDIR": "/tmp",
        "TERM": "dumb",
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
        # The repo is owned by another uid; newer git refuses it without this.
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "safe.directory",
        "GIT_CONFIG_VALUE_0": "*",
        "GIT_TERMINAL_PROMPT": "0",
    }
    for k, v in os.environ.items():
        if k.startswith("LC_"):
            env[k] = v
    env.update(spec.env or {})
    return env


def bwrap_args(spec: RunSpec, seccomp_fd: Optional[int]) -> list[str]:
    a = ["bwrap", "--unshare-all", "--unshare-user", "--die-with-parent",
         "--new-session", "--hostname", "council"]
    if spec.net != "none":   # pragma: no cover - P5
        raise ValueError(f"net={spec.net!r} is not available in this backend")
    a += ["--ro-bind", "/usr", "/usr"]
    for d in TOP_DIRS:
        p = f"/{d}"
        if os.path.islink(p):
            a += ["--symlink", os.readlink(p), p]
        elif os.path.isdir(p):
            a += ["--ro-bind", p, p]
    for f in ETC_FILES:
        a += ["--ro-bind-try", f"/etc/{f}", f"/etc/{f}"]
    a += ["--dev", "/dev", "--tmpfs", "/dev/shm", "--remount-ro", "/dev",
          "--proc", "/proc", "--tmpfs", "/tmp", "--tmpfs", HOME]
    seen: set[str] = set()
    for p in sorted({os.path.realpath(p) for p in (*spec.ro_binds,
                                                   *spec.ro_roots) if p},
                    key=len):
        if p in seen or not os.path.exists(p) or p in ("/",):
            continue
        seen.add(p)
        a += ["--ro-bind", p, p]
    for kind, src, dst in spec.binds:
        a += ["--bind" if kind == "rw" else "--ro-bind", src, dst]
    # The session store under each root (other lanes' scratch and views,
    # transcripts, checkpoints) is masked: a lane sees its own /scratch
    # and nothing of its siblings'.
    for p in seen:
        store = os.path.join(p, *STORE)
        if os.path.isdir(store):
            a += ["--tmpfs", store]
    a += ["--bind", spec.scratch, SCRATCH, "--chdir", spec.cwd]
    # Last: the root tmpfs itself (bwrap's, owned by the sandbox user)
    # goes read-only, so nothing but /scratch, /tmp, $HOME and /dev/shm
    # takes a write — not even a private one at / or /etc.
    a += ["--remount-ro", "/"]
    if seccomp_fd is not None:
        a += ["--seccomp", str(seccomp_fd)]
    return a + ["--", *spec.argv]


def scope_args(spec: RunSpec, *, unit: str,
               root: Optional[bool] = None) -> list[str]:
    """Layer 1: the transient systemd scope holding the cgroup limits."""
    root = is_root() if root is None else root
    lim: Limits = spec.limits
    scope = ["systemd-run", "--scope", "--quiet", "--collect",
             f"--unit={unit}", f"--slice={SLICE}",
             "-p", f"MemoryMax={lim.memory_mib}M", "-p", "MemorySwapMax=0",
             "-p", f"TasksMax={lim.pids}", "-p", f"CPUQuota={lim.cpu_pct}%"]
    if not root:
        scope.insert(1, "--user")
    return scope


def chain(spec: RunSpec, seccomp_fd: Optional[int], *, unit: str,
          root: Optional[bool] = None,
          launcher: Optional[str] = None) -> list[str]:
    """The full command line, outermost layer first.

    With ``launcher`` (the static ``council-sandbox``) that one binary
    replaces ``prlimit → setpriv → bwrap``, reading the same mount plan.
    """
    root = is_root() if root is None else root
    lim: Limits = spec.limits
    scope = scope_args(spec, unit=unit, root=root)
    fsize = lim.fsize_mib * 1024 * 1024
    if launcher:
        own = [launcher]
        if root:
            own += ["--drop-to", f"{NOBODY}:{NOBODY}"]
        own += ["--rlimit", "core=0:0",
                "--rlimit", f"nofile={lim.nofile}:{lim.nofile}",
                "--rlimit", f"fsize={fsize}:{fsize}"]
        return scope + own + bwrap_args(spec, seccomp_fd)[1:]
    rl = ["prlimit", "--core=0", f"--nofile={lim.nofile}:{lim.nofile}",
          f"--fsize={fsize}:{fsize}", "--"]
    priv = ["setpriv"]
    if root:
        priv += [f"--reuid={NOBODY}", f"--regid={NOBODY}", "--clear-groups",
                 "--bounding-set=-all", "--inh-caps=-all"]
    priv += ["--nnp"]
    return scope + rl + priv + bwrap_args(spec, seccomp_fd)


class _Capture(threading.Thread):
    """Read one pipe to EOF keeping only its head and tail, so a command
    printing gigabytes costs a bounded amount of engine memory."""

    def __init__(self, stream):
        super().__init__(daemon=True)
        self.stream, self.head, self.tail, self.total = stream, b"", b"", 0

    def run(self) -> None:
        edge = CAPTURE_EDGE_BYTES
        while True:
            chunk = self.stream.read(65536)
            if not chunk:
                break
            self.total += len(chunk)
            if len(self.head) < edge:
                take = edge - len(self.head)
                self.head += chunk[:take]
                chunk = chunk[take:]
            if chunk:
                self.tail = (self.tail + chunk)[-edge:]

    def text(self) -> str:
        dropped = self.total - len(self.head) - len(self.tail)
        mid = (f"\n...[{dropped} bytes not kept by the sandbox]...\n"
               .encode() if dropped > 0 else b"")
        return (self.head + mid + self.tail).decode("utf-8", "replace")


class BwrapBackend:
    name = NAME
    #: The programs the chain needs on PATH (beyond the launcher itself).
    tools = ("bwrap", "systemd-run", "prlimit", "setpriv")
    #: How this backend's own setup failure starts on stderr.
    setup_prefix = "bwrap: "

    def __init__(self, *, required: tuple[str, ...] = REQUIRED):
        self.required = tuple(required)
        self._pre: Optional[tuple[float, Preflight]] = None
        self._lock = threading.Lock()

    def _chain(self, spec: RunSpec, fd: Optional[int], unit: str) -> list[str]:
        return chain(spec, fd, unit=unit)

    def sandbox_uid(self) -> Optional[int]:
        """The host uid commands run as when it differs from ours."""
        return NOBODY if is_root() else None

    def _extra_layers(self, layers: dict[str, str]) -> bool:
        """Backend-specific layers; False stops the probe there."""
        return True

    # ------------------------------------------------------------------ #
    def preflight(self, *, refresh: bool = False) -> Preflight:
        with self._lock:
            if (not refresh and self._pre is not None
                    and time.monotonic() - self._pre[0] < _PREFLIGHT_TTL_S):
                return self._pre[1]
            pre = self._probe()
            self._pre = (time.monotonic(), pre)
            return pre

    def _probe(self) -> Preflight:
        layers: dict[str, str] = {}
        tools = {t: shutil.which(t) for t in self.tools}
        for t, p in tools.items():
            if not p:
                layers[f"tool:{t}"] = "absent"
        if not self._extra_layers(layers):
            tools["launcher"] = None
        try:
            seccomp.build_filter()
            layers["seccomp"] = "ok"
        except ValueError as e:
            layers["seccomp"] = f"failed: {e}"
        layers["landlock"] = "absent (optional)"
        if all(tools.values()):
            scratch = None
            try:
                import tempfile
                scratch = tempfile.mkdtemp(prefix="council-preflight-")
                if is_root():
                    os.chown(scratch, NOBODY, NOBODY)
                spec = RunSpec(argv=["/bin/sh", "-c", "exit 0"], cwd="/",
                               ro_roots=(), scratch=scratch, timeout_s=30,
                               limits=Limits(memory_mib=256, pids=32),
                               name="preflight")
                ns = self._smoke(spec, scope=False)
                for k in ("userns", "mountns", "pidns", "netns"):
                    layers[k] = ns
                layers["nnp"] = ns
                if is_root():
                    layers["uid"] = ns
                layers["cgroup"] = self._smoke(spec, scope=True) \
                    if ns == "ok" else "untested"
            finally:
                if scratch:
                    shutil.rmtree(scratch, ignore_errors=True)
        else:
            for k in ("userns", "mountns", "pidns", "netns", "nnp", "cgroup"):
                layers.setdefault(k, "absent (tool missing)")
        required = list(self.required) + (["uid"] if is_root() else [])
        missing = [k for k in required if layers.get(k) != "ok"]
        missing += [k for k in layers if k.startswith("tool:")]
        return Preflight(backend=self.name, ok=not missing, layers=layers,
                         missing=missing)

    def _smoke(self, spec: RunSpec, *, scope: bool) -> str:
        try:
            r = self._run(spec, use_scope=scope)
        except OSError as e:
            return f"failed: {e}"
        if r.exit_code == 0:
            return "ok"
        if r.killed_by == "setup":
            return f"failed: {r.setup_error}"
        return f"failed: exit {r.exit_code}: {(r.stderr or '').strip()[:200]}"

    # ------------------------------------------------------------------ #
    def run(self, spec: RunSpec) -> RunResult:
        pre = self.preflight()
        if not pre.ok:
            return RunResult(exit_code=None, killed_by="setup",
                             setup_error=pre.reason())
        return self._run(spec, use_scope=True)

    def _run(self, spec: RunSpec, *, use_scope: bool) -> RunResult:
        unit = _unit(spec.name)
        rfd, wfd = os.pipe()
        try:
            os.write(wfd, seccomp.build_filter())
        finally:
            os.close(wfd)
        argv = self._chain(spec, rfd, unit)
        if not use_scope:
            argv = argv[len(scope_args(spec, unit=unit)):]
        t0 = time.monotonic()
        try:
            proc = subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, pass_fds=(rfd,),
                env=sandbox_env(spec), start_new_session=True, cwd="/")
        finally:
            os.close(rfd)
        out, err = _Capture(proc.stdout), _Capture(proc.stderr)
        out.start(), err.start()
        killed_by = "none"
        try:
            proc.wait(timeout=spec.timeout_s)
        except subprocess.TimeoutExpired:
            killed_by = "timeout"
            self._kill(proc, unit if use_scope else None)
        out.join(5), err.join(5)
        rc = proc.returncode
        if killed_by == "none" and rc is not None and rc < 0:
            killed_by = f"signal:{-rc}"
        stderr = err.text()
        if killed_by == "none" and rc == 1 and stderr.startswith(self.setup_prefix):
            # The sandbox's own failure (a bind source nobody can reach, say):
            # the command never started, so this is not its exit code.
            return RunResult(exit_code=None, killed_by="setup",
                             wall_s=round(time.monotonic() - t0, 3),
                             setup_error=stderr.strip()[:500])
        return RunResult(exit_code=rc, killed_by=killed_by,
                         wall_s=round(time.monotonic() - t0, 3),
                         stdout=out.text(), stderr=stderr)

    @staticmethod
    def _kill(proc: subprocess.Popen, unit: Optional[str]) -> None:
        if unit:
            cmd = ["systemctl", "kill", "--signal=SIGKILL", f"{unit}.scope"]
            if not is_root():
                cmd.insert(1, "--user")
            try:
                subprocess.run(cmd, capture_output=True, timeout=10)
            except (OSError, subprocess.SubprocessError):
                pass
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            log.error("sandboxed command %s survived SIGKILL", proc.pid)


class LauncherBackend(BwrapBackend):
    """The static ``council-sandbox`` launcher (plan E2) behind the same
    scope and mount plan: one pinned binary instead of three host tools
    whose versions vary. Used only when :func:`launcher.verify` passes —
    a binary that is not the pinned one, or that anyone but us could
    swap, is never run as root."""
    name = "launcher"
    tools = ("systemd-run",)
    setup_prefix = "council-sandbox: "

    def __init__(self, path: Optional[str] = None, **kw):
        super().__init__(**kw)
        self._path = path

    def path(self) -> str:
        from claude_hooks.sandbox import launcher
        return str(self._path or launcher.installed_path())

    def _chain(self, spec: RunSpec, fd: Optional[int], unit: str) -> list[str]:
        return chain(spec, fd, unit=unit, launcher=self.path())

    def _extra_layers(self, layers: dict[str, str]) -> bool:
        from claude_hooks.sandbox import launcher
        chk = launcher.verify(self.path())
        layers["launcher"] = "ok" if chk.ok else f"failed: {chk.reason}"
        return chk.ok

    def _probe(self) -> Preflight:
        pre = super()._probe()
        if pre.layers.get("launcher") != "ok" and "launcher" not in pre.missing:
            pre.missing.insert(0, "launcher")
            pre.ok = False
        return pre


__all__ = ["BwrapBackend", "LauncherBackend", "REQUIRED", "chain",
           "bwrap_args", "sandbox_env", "scope_args", "NAME", "NOBODY"]
