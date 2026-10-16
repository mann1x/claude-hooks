"""``run_command``: a lane's shell, run in the council sandbox.

Plan Milestone E (P1 of ``docs/research/cerebriline-map/04-sandbox-shell.md``
§6): the ``read`` mode. The council's roots are visible read-only at
their real paths; ``/scratch`` (one directory per lane, under the
session's store) and ``/tmp`` are writable and private; there is no
network, no host home directory, and limits on time, memory, processes,
open files and file size. The backend (:mod:`claude_hooks.sandbox`) owns
all of that; this provider owns the lane, the log and the classification.

Per lane, on disk (``<cwd>/.claude-hooks/consultants/<sid>/lanes/<lane>/``):

* ``scratch/`` — the lane's writable directory, kept until the session
  is collected;
* ``commands.jsonl`` — a ``start`` record before every command and an
  ``end`` record after it. A ``start`` with no ``end`` is a command the
  engine went away under: the lane's next result says so, and the
  command is never re-run, because commands are not idempotent.

Errors that are the *harness's* (bad arguments, no sandbox, over quota)
start with ``error:``, the convention every tool surface here keeps so
the mistake counter (plan D14) sees them. A command that ran and exited
non-zero is an answer, not an error.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from claude_hooks.tool_registry.base import ToolProvider
from claude_hooks.tool_registry.policy import ASK_ASSISTANT, ASK_HUMAN

log = logging.getLogger("claude_hooks.tool_registry.shell")

TOOL = "run_command"
#: Characters of command output a result may carry (head and tail kept).
OUTPUT_MAX_CHARS = 20_000
#: Scratch entries listed after a command.
SCRATCH_LIST_MAX = 20
INTERRUPTED_NOTE = (
    "note: your previous command in this lane ({cmd}) was interrupted by an "
    "engine restart. It was not re-run; whatever it wrote to /scratch "
    "before it stopped is still there.")


@dataclass
class ShellSettings:
    """What the provider needs from ``[tools.shell]`` (see
    ``consultants.config.ShellConfig``), as plain values."""
    mode: str = "read"
    net: str = "none"
    backend: str = "auto"
    ro_binds: tuple[str, ...] = ()
    timeout_s: int = 120
    max_timeout_s: int = 600
    memory_mib: int = 4096
    pids: int = 256
    cpu_pct: int = 400
    lane_disk_gib: float = 2.0
    free_floor_gib: float = 20.0
    free_floor_pct: float = 10.0
    extra: dict = field(default_factory=dict)


def _spec(settings: ShellSettings) -> dict:
    return {"type": "function", "function": {
        "name": TOOL,
        "description": (
            "Run a shell command in a sandbox, to answer what reading cannot: "
            "run the tests, a script, a linter, a build check. The project and "
            "the other roots are visible READ-ONLY at their real paths. "
            "/scratch and /tmp are writable and private to you (/scratch is "
            "kept between your commands). There is no network and no home "
            "directory from the host. Limits: "
            f"{settings.timeout_s}s by default (timeout_s up to "
            f"{settings.max_timeout_s}s), {settings.memory_mib} MiB memory, "
            f"{settings.pids} processes. To keep build output, point it at "
            "/scratch (e.g. pytest -p no:cacheprovider, "
            "PYTHONPYCACHEPREFIX=/tmp/pyc)."),
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string", "description":
                        "Shell syntax, run by /bin/sh -c."},
            "argv": {"type": "array", "items": {"type": "string"},
                     "description": "An argument vector, run without a "
                     "shell. Give this or command, not both."},
            "cwd": {"type": "string", "description":
                    "Working directory: relative to the project root, or an "
                    "absolute path inside a visible root or /scratch. "
                    "Default: the project root."},
            "timeout_s": {"type": "integer", "description":
                          f"Wall clock in seconds (default "
                          f"{settings.timeout_s}, at most "
                          f"{settings.max_timeout_s})."},
        }},
    }}


def lane_dirname(lane: Optional[str]) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", lane or "solo").strip("-") or "solo"


class ShellToolProvider(ToolProvider):
    name = "shell"
    prefix = ""
    accepts_lane = True

    def __init__(self, *, sid: str, settings: Optional[ShellSettings] = None,
                 extra_roots: tuple[str, ...] = (), backend: Any = None,
                 store_dir: Optional[str] = None):
        self.sid = sid or "nosid"
        self.settings = settings or ShellSettings()
        self.extra_roots = tuple(r for r in extra_roots if r)
        self._backend = backend
        self._store_dir = store_dir
        self._lock = threading.Lock()
        self._lane_locks: dict[str, threading.Lock] = {}
        self._checked: set[str] = set()

    # ------------------------------------------------------------------ #
    @property
    def backend(self):
        if self._backend is None:
            from claude_hooks.sandbox import backend_for
            self._backend = backend_for(self.settings.backend)
        return self._backend

    def preflight(self):
        return self.backend.preflight()

    def specs(self) -> list[dict]:
        return [_spec(self.settings)]

    def default_level(self, tool: str) -> str:
        return ASK_ASSISTANT

    def is_read_only(self, tool: str) -> bool:
        return False

    def call_level(self, tool: str, raw_args: str):
        from claude_hooks.sandbox.classify import classify
        try:
            args = self._args(raw_args)
        except ValueError:
            return (ASK_ASSISTANT, "its arguments do not parse")
        spend = ASK_ASSISTANT if self.settings.net == "none" else ASK_HUMAN
        v = classify(argv=args.get("argv"), command=args.get("command"),
                     timeout_s=int(args.get("timeout_s") or 0),
                     spend_level=spend)
        reason = v.reason
        if v.klass == "spend" and spend == ASK_ASSISTANT:
            reason += "; the sandbox has no network, so it cannot"
        return (v.level, f"{v.klass}: {reason}")

    # ------------------------------------------------------------------ #
    @staticmethod
    def _args(raw_args: str) -> dict:
        try:
            args = json.loads(raw_args or "{}")
        except json.JSONDecodeError as e:
            raise ValueError(f"arguments are not JSON: {e}") from None
        if not isinstance(args, dict):
            raise ValueError("arguments must be an object")
        argv, command = args.get("argv"), args.get("command")
        if argv is not None and (not isinstance(argv, list) or not argv
                                 or not all(isinstance(a, str) for a in argv)):
            raise ValueError("argv must be a non-empty list of strings")
        if command is not None and (not isinstance(command, str)
                                    or not command.strip()):
            raise ValueError("command must be a non-empty string")
        if (argv is None) == (command is None):
            raise ValueError("give exactly one of command or argv")
        t = args.get("timeout_s")
        if t is not None and (isinstance(t, bool) or not isinstance(t, (int, float))
                              or t <= 0):
            raise ValueError("timeout_s must be a positive number")
        return args

    def lane_dir(self, cwd: str, lane: Optional[str]) -> str:
        base = self._store_dir or os.path.join(
            cwd, ".claude-hooks", "consultants", self.sid)
        return os.path.join(base, "lanes", lane_dirname(lane))

    def _roots(self, cwd: str) -> tuple[str, ...]:
        return tuple(os.path.realpath(r) for r in (cwd, *self.extra_roots))

    def _resolve_cwd(self, want: Optional[str], root: str, roots, scratch) -> str:
        from claude_hooks.sandbox import SCRATCH
        if not want:
            return root
        if want == SCRATCH or want.startswith(SCRATCH + "/"):
            return want
        path = os.path.realpath(want if os.path.isabs(want)
                                else os.path.join(root, want))
        for r in roots:
            if path == r or path.startswith(r.rstrip("/") + "/"):
                if not os.path.isdir(path):
                    raise ValueError(f"cwd {want!r} is not a directory")
                return path
        raise ValueError(f"cwd {want!r} is outside the visible roots "
                         f"({', '.join(roots)}) and /scratch")

    # ------------------------------------------------------------------ #
    def execute(self, tool: str, raw_args: str, cwd: str, *,
                lane: Optional[str] = None) -> str:
        if tool != TOOL:
            return f"error: unknown tool {tool!r}"
        try:
            args = self._args(raw_args)
        except ValueError as e:
            return f"error: {e}"
        pre = self.backend.preflight()
        if not pre.ok:
            return (f"error: no sandbox on this host, so no shell — "
                    f"{pre.reason()}")
        with self._lock:
            lock = self._lane_locks.setdefault(lane_dirname(lane),
                                               threading.Lock())
        with lock:     # one command at a time per lane: scratch is shared
            return self._execute(args, cwd, lane)

    def _execute(self, args: dict, cwd: str, lane: Optional[str]) -> str:
        from claude_hooks.sandbox import Limits, RunSpec
        s = self.settings
        root = os.path.realpath(cwd)
        roots = self._roots(cwd)
        ldir = self.lane_dir(cwd, lane)
        scratch = os.path.join(ldir, "scratch")
        try:
            run_cwd = self._resolve_cwd(args.get("cwd"), root, roots, scratch)
        except ValueError as e:
            return f"error: {e}"
        refusal = self._space_refusal(ldir)
        if refusal:
            return refusal
        os.makedirs(scratch, exist_ok=True)
        self._own(ldir, scratch)
        notes = self._interrupted_note(ldir, lane)
        timeout = min(int(args.get("timeout_s") or s.timeout_s),
                      s.max_timeout_s)
        argv = args.get("argv") or ["/bin/sh", "-c", args["command"]]
        before = self._listing(scratch)
        n = self._log(ldir, {"event": "start", "argv": argv, "cwd": run_cwd,
                             "timeout_s": timeout})
        spec = RunSpec(argv=list(argv), cwd=run_cwd, ro_roots=roots,
                       scratch=scratch, timeout_s=timeout,
                       limits=Limits(memory_mib=s.memory_mib, pids=s.pids,
                                     cpu_pct=s.cpu_pct,
                                     default_wall_s=s.timeout_s,
                                     max_wall_s=s.max_timeout_s),
                       ro_binds=tuple(s.ro_binds), net=s.net,
                       name=f"{self.sid}-{lane_dirname(lane)}")
        try:
            r = self.backend.run(spec)
        except Exception as e:   # pragma: no cover - backends return errors
            log.exception("sandbox backend raised")
            self._log(ldir, {"event": "end", "n": n, "error": str(e)})
            return f"error: the sandbox failed to run the command: {e}"
        self._log(ldir, {"event": "end", "n": n, "exit": r.exit_code,
                         "killed_by": r.killed_by, "wall_s": r.wall_s})
        if r.killed_by == "setup":
            return f"error: the sandbox could not run the command — {r.setup_error}"
        return "\n".join(notes + [self._render(r, timeout, before,
                                               self._listing(scratch))])

    # ------------------------------------------------------------------ #
    def _own(self, ldir: str, scratch: str) -> None:
        """Give the scratch dir to the uid the command runs as (nobody,
        on a root engine). The lane dir stays the engine's."""
        from claude_hooks.sandbox.linux_bwrap import NOBODY, is_root
        if is_root() and getattr(self.backend, "name", "") == "bwrap":
            try:
                st = os.stat(scratch)
                if st.st_uid != NOBODY:
                    os.chown(scratch, NOBODY, NOBODY)
            except OSError:
                log.exception("could not chown %s", scratch)

    def _space_refusal(self, ldir: str) -> Optional[str]:
        s = self.settings
        probe = ldir
        while not os.path.exists(probe):
            probe = os.path.dirname(probe)
        try:
            du = shutil.disk_usage(probe)
        except OSError:
            return None
        free_gib = du.free / 2**30
        floor = max(s.free_floor_gib, du.total / 2**30 * s.free_floor_pct / 100)
        if free_gib < floor:
            return (f"error: the disk holding this lane's scratch has "
                    f"{free_gib:.1f} GiB free, under the {floor:.1f} GiB "
                    f"floor, so no command runs. Answer from reading instead.")
        used = _tree_bytes(os.path.join(ldir, "scratch")) / 2**30
        if used > s.lane_disk_gib:
            return (f"error: this lane's /scratch holds {used:.2f} GiB, over "
                    f"its {s.lane_disk_gib:g} GiB quota. Delete files from "
                    f"/scratch (rm) before running anything that writes.")
        return None

    def _log(self, ldir: str, record: dict) -> int:
        path = os.path.join(ldir, "commands.jsonl")
        os.makedirs(ldir, exist_ok=True)
        if record.get("event") == "start":
            record["n"] = _count_starts(path) + 1
        record["ts"] = round(time.time(), 3)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
            f.flush()
            os.fsync(f.fileno())
        return record.get("n", 0)

    def _interrupted_note(self, ldir: str, lane: Optional[str]) -> list[str]:
        key = lane_dirname(lane)
        if key in self._checked:
            return []
        self._checked.add(key)
        dangling = interrupted_commands(os.path.join(ldir, "commands.jsonl"))
        if not dangling:
            return []
        # Close them, so the note is given once even across restarts.
        for rec in dangling:
            self._log(ldir, {"event": "end", "n": rec.get("n"),
                             "killed_by": "interrupted"})
        last = dangling[-1]
        cmd = " ".join(last.get("argv") or [])
        if len(cmd) > 200:
            cmd = cmd[:200] + "…"
        return [INTERRUPTED_NOTE.format(cmd=f"`{cmd}`")]

    @staticmethod
    def _listing(scratch: str) -> dict[str, tuple[int, int]]:
        out: dict[str, tuple[int, int]] = {}
        for dp, dns, fns in os.walk(scratch):
            for fn in fns:
                p = os.path.join(dp, fn)
                try:
                    st = os.lstat(p)
                except OSError:
                    continue
                out[os.path.relpath(p, scratch)] = (st.st_size, st.st_mtime_ns)
            if len(out) > 5000:
                break
        return out

    @staticmethod
    def _render(r, timeout: int, before: dict, after: dict) -> str:
        from consultants.engine.history import middle_truncate
        head = (f"exit={r.exit_code} wall={r.wall_s:.1f}s "
                f"killed_by={r.killed_by}")
        if r.killed_by == "timeout":
            head += f" (the {timeout}s wall clock ran out; it was killed)"
        out, err = r.stdout or "", r.stderr or ""
        share = OUTPUT_MAX_CHARS // (2 if out and err else 1)
        parts = [head]
        if out:
            parts += [f"--- stdout ({len(out)} chars) ---",
                      middle_truncate(out, share)]
        if err:
            parts += [f"--- stderr ({len(err)} chars) ---",
                      middle_truncate(err, share)]
        if not out and not err:
            parts.append("(no output)")
        changed = sorted(
            [("A" if k not in before else "M") + " /scratch/" + k
             for k, v in after.items() if before.get(k) != v]
            + ["D /scratch/" + k for k in before if k not in after])
        if changed:
            more = len(changed) - SCRATCH_LIST_MAX
            parts.append("--- changed in /scratch ---")
            parts += changed[:SCRATCH_LIST_MAX]
            if more > 0:
                parts.append(f"... and {more} more")
        return "\n".join(parts)


def _tree_bytes(path: str) -> int:
    total = 0
    for dp, _, fns in os.walk(path):
        for fn in fns:
            try:
                total += os.lstat(os.path.join(dp, fn)).st_size
            except OSError:
                pass
    return total


def _records(path: str) -> list[dict]:
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue     # a line torn by a crash mid-write
    return out


def _count_starts(path: str) -> int:
    return sum(1 for r in _records(path) if r.get("event") == "start")


def interrupted_commands(path: str) -> list[dict]:
    """``start`` records with no matching ``end``: commands the engine
    went away under."""
    recs = _records(path)
    ended = {r.get("n") for r in recs if r.get("event") == "end"}
    return [r for r in recs if r.get("event") == "start"
            and r.get("n") not in ended]


__all__ = ["ShellToolProvider", "ShellSettings", "TOOL", "lane_dirname",
           "interrupted_commands", "OUTPUT_MAX_CHARS"]
