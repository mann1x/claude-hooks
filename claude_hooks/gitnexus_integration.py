"""
gitnexus integrator — detect and surface https://github.com/abhigyanpatwari/GitNexus
when the user has it installed, without making it a hard dependency.

This module owns gitnexus detection + reindex spawn. When you want
combined behaviour across both supported engines (gitnexus + axon),
import :mod:`claude_hooks.companion_integration` instead — it routes
to the right engine based on what's installed/indexed for the project.

Silent no-op when gitnexus is missing.

Public API:

    is_available() -> bool
    is_indexed(root: Path) -> bool
    binary_path() -> Optional[str]
    status(root: Path) -> dict
    reindex_if_dirty_async(*, cwd, turn_modified, ...) -> None
    session_start_hint(root: Path) -> Optional[str]
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from claude_hooks._popen import detach_kwargs, windowless_python_executable
# The liveness-aware reindex lock is shared with the claudemem engine: same
# two-line ``<pid>\n<unix-ts>`` format, same stdlib-only cross-platform PID
# probe. Imported rather than re-rolled here, following the consolidation
# precedent in claude_hooks/_popen.py (#221).
from claude_hooks.claudemem_reindex import _lock_holder_alive, _read_lock

log = logging.getLogger("claude_hooks.gitnexus")

_LOCK_FILENAME = ".gitnexus-reindex.lock"
_DEFAULT_LOCK_MIN_AGE_SECONDS = 60

# Reindex outcome. gitnexus's graph database (LadybugDB, ``.gitnexus/lbug``)
# leaves ``lbug.shadow.dirty-recovery`` behind when a write does not finish,
# and every later open of the database then segfaults in the native module
# until a full analyze rewrites ``lbug`` (opencoti, 2026-10-01: the 09:45
# analyze was cut off at 09:49, and detect-changes crashed six times between
# 10:31 and 12:18 — the commit-time graph check silently did not run). The
# analyze used to be spawned straight from the Stop hook with its output
# discarded, so nothing ever saw it fail. It now runs under a supervisor
# (``python -m claude_hooks.gitnexus_integration --supervise``) that records
# the outcome and retries a failed rebuild at once.
#: Both live inside ``.gitnexus/``, which gitnexus itself gitignores (``*``).
_STATUS_FILENAME = "claude-hooks-reindex.json"
_LOG_FILENAME = "claude-hooks-reindex.log"
_DIRTY_RECOVERY = "lbug.shadow.dirty-recovery"
_DB_FILENAME = "lbug"
#: Attempts per supervised run (1 + retries).
_MAX_ATTEMPTS = 3
_RETRY_DELAY_SECONDS = 30
#: Once every attempt of a run failed, how long before a turn may try again.
_FAILED_BACKOFF_SECONDS = 15 * 60
_LOG_TAIL_BYTES = 4000


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def binary_path() -> Optional[str]:
    """Path to the gitnexus binary, or None."""
    return shutil.which("gitnexus")


def _global_registry() -> Optional[Path]:
    p = Path.home() / ".gitnexus" / "registry.json"
    return p if p.exists() else None


def _project_index_dir(root: Path) -> Path:
    return root / ".gitnexus"


def is_available() -> bool:
    """True if gitnexus appears to be installed on this machine."""
    if binary_path() is not None:
        return True
    if _global_registry() is not None:
        return True
    return False


def is_indexed(root: Path) -> bool:
    """True iff ``root/.gitnexus/`` exists (project has a built index)."""
    return _project_index_dir(root).is_dir()


def status(root: Path) -> dict:
    """Summary dict — version, install state, project-index state."""
    out: dict = {
        "binary": binary_path(),
        "global_registry": str(_global_registry()) if _global_registry() else None,
        "project_indexed": is_indexed(root),
        "project_index_dir": str(_project_index_dir(root)) if is_indexed(root) else None,
        "version": _probe_version(),
    }
    return out


def _probe_version() -> Optional[str]:
    bin_ = binary_path()
    if not bin_:
        return None
    try:
        cp = subprocess.run(
            [bin_, "--version"],
            capture_output=True, text=True, timeout=3,
        )
        if cp.returncode != 0:
            return None
        return cp.stdout.strip() or cp.stderr.strip() or None
    except (OSError, subprocess.TimeoutExpired):
        return None


# ---------------------------------------------------------------------------
# Reindex spawn (Stop-hook side)
# ---------------------------------------------------------------------------

def _acquire_lock(root: Path, min_age_seconds: int) -> bool:
    """Return True if a reindex may start now.

    Two guards combine, matching :mod:`claude_hooks.claudemem_reindex`:

    1. **Live-process check** — if the lock names a PID that is still
       running, refuse regardless of age. Without this a rebuild that
       outlives the cooldown gets a second ``analyze`` spawned on top of
       it, and the two share ``.gitnexus/graph-csv`` and destroy each
       other's staging files. Observed 2026-09-25 once the gitnexus
       v1.6.12 upgrade made the first analyze per repo a *full* rebuild
       (minutes, not seconds): one run died on a deleted
       ``rel_CodeElement_Class.csv``, its rival on ``EEXIST`` for
       ``rel_File_Route.csv``. The age guard alone cannot see this.
    2. **Cooldown** — refuse while the recorded timestamp is younger
       than ``min_age_seconds``, so rapid Stop-hook reentry cannot pile
       up when no PID was recorded (legacy single-line lock format).
    """
    lock = root / _LOCK_FILENAME
    now = time.time()
    pid, ts = _read_lock(lock)

    if pid is not None and _lock_holder_alive(pid, ts, now):
        log.debug("gitnexus reindex lock held by live pid %d — skipping", pid)
        return False

    if ts is not None and now - ts < min_age_seconds:
        log.debug("gitnexus reindex lock fresh (%ds old) — skipping",
                  int(now - ts))
        return False

    try:
        lock.write_text(str(int(now)), encoding="utf-8")
        return True
    except OSError:
        return False


def _record_lock_pid(root: Path, pid: int) -> None:
    """Stamp the spawned PID into the lock so guard 1 above can see it.

    Safe-by-design: any failure leaves the timestamp-only lock, which
    still serves the cooldown role.
    """
    try:
        (root / _LOCK_FILENAME).write_text(
            f"{pid}\n{int(time.time())}", encoding="utf-8",
        )
    except OSError as e:
        log.debug("could not stamp pid into gitnexus reindex lock: %s", e)


def index_dirty(root: Path) -> bool:
    """True when the graph database was left mid-write.

    The recovery file is not removed by a later successful analyze, so
    its presence alone proves nothing (opencoti still has the 09:49 one,
    beside an ``lbug`` rewritten since). It is dirty when the recovery
    file is newer than the database it belongs to.
    """
    idx = _project_index_dir(root)
    try:
        rec = (idx / _DIRTY_RECOVERY).stat().st_mtime
    except OSError:
        return False
    try:
        db = (idx / _DB_FILENAME).stat().st_mtime
    except OSError:
        return True
    return rec > db


def read_reindex_status(root: Path) -> Optional[dict]:
    try:
        data = json.loads((_project_index_dir(root) / _STATUS_FILENAME)
                          .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_reindex_status(root: Path, status: dict) -> None:
    path = _project_index_dir(root) / _STATUS_FILENAME
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(status, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        log.debug("could not write gitnexus reindex status: %s", e)


def index_broken(root: Path) -> bool:
    """The last rebuild failed, or the database was left mid-write."""
    if index_dirty(root):
        return True
    st = read_reindex_status(root)
    return bool(st) and st.get("ok") is False


def _log_tail(path: Path) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - _LOG_TAIL_BYTES))
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _analyze_argv(binary: str) -> list[str]:
    """``--index-only``: refresh the graph and nothing else.

    A bare ``analyze`` also writes a GitNexus section into ``AGENTS.md``
    and ``CLAUDE.md`` and installs six skill dirs under
    ``.claude/skills/`` — on every run, in every indexed repo, from a
    background rebuild nobody asked to edit docs. Repos here are
    initialised with ``--skip-agents-md`` precisely to keep their curated
    docs out of it; the Stop hook ran bare ``analyze`` regardless.
    (Found 2026-10-01: one supervised rebuild of giano appended 45 lines
    to each doc.)
    """
    return [binary, "analyze", "--index-only"]


def supervise_analyze(binary: str, root: Path, *,
                      attempts: int = _MAX_ATTEMPTS,
                      retry_delay: float = _RETRY_DELAY_SECONDS,
                      sleep_fn=time.sleep, run_fn=None) -> dict:
    """Run ``gitnexus analyze`` until it succeeds or ``attempts`` run out,
    and record the outcome in ``.gitnexus/claude-hooks-reindex.json``.

    Success needs both a zero exit and a database that is not left dirty:
    an analyze can exit 0 after a cut-off write in a worker, and a dirty
    database is what crashes every reader afterwards.
    """
    log_path = _project_index_dir(root) / _LOG_FILENAME
    prev = read_reindex_status(root) or {}
    failures = int(prev.get("consecutive_failures") or 0)
    started = time.time()
    rc: Optional[int] = None
    dirty = False
    for attempt in range(1, attempts + 1):
        try:
            with open(log_path, "wb") as out:
                if run_fn is not None:
                    rc = run_fn(binary, root, out)
                else:
                    rc = subprocess.run(
                        _analyze_argv(binary), cwd=str(root),
                        stdin=subprocess.DEVNULL, stdout=out,
                        stderr=subprocess.STDOUT).returncode
        except OSError as e:
            rc = -1
            log.warning("gitnexus analyze could not start in %s: %s", root, e)
        dirty = index_dirty(root)
        if rc == 0 and not dirty:
            break
        failures += 1
        log.warning("gitnexus analyze failed in %s (attempt %d/%d, rc=%s, "
                    "database %s)", root, attempt, attempts, rc,
                    "left mid-write" if dirty else "not dirty")
        if attempt < attempts:
            sleep_fn(retry_delay)
    ok = rc == 0 and not dirty
    status = {
        "ok": ok,
        "returncode": rc,
        "dirty": dirty,
        "attempts": attempt,
        "started_at": started,
        "finished_at": time.time(),
        "consecutive_failures": 0 if ok else failures,
        "log_tail": "" if ok else _log_tail(log_path),
    }
    _write_reindex_status(root, status)
    return status


def _spawn_analyze(binary: str, root: Path) -> Optional[int]:
    """Detached, supervised ``gitnexus analyze``.

    Returns the supervisor's PID so the caller can stamp it into the
    lock, or None if the spawn failed. The supervisor lives until the
    last attempt ends, so its PID stays a valid liveness handle for the
    whole rebuild, retries included.
    """
    try:
        proc = subprocess.Popen(
            [windowless_python_executable(), "-m",
             "claude_hooks.gitnexus_integration", "--supervise",
             str(root), binary],
            cwd=str(root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **detach_kwargs(),
        )
        log.info("gitnexus: spawned analyze in %s", root)
        return getattr(proc, "pid", None)
    except OSError as e:
        log.debug("could not spawn gitnexus analyze: %s", e)
        return None


def reindex_if_dirty_async(
    *,
    cwd: str,
    turn_modified: bool,
    lock_min_age_seconds: int = _DEFAULT_LOCK_MIN_AGE_SECONDS,
) -> None:
    """Detached gitnexus reindex when the turn touched source files, or
    when the index is broken (a failed rebuild, or a database left
    mid-write) — a broken index crashes every reader, so it is rebuilt
    without waiting for an edit.

    Silent no-op when gitnexus is missing or the project isn't indexed.
    """
    try:
        bin_ = binary_path()
        if not bin_:
            return
        if not cwd:
            return
        root = Path(cwd).resolve()
        marker_root = _find_marker_root(root)
        if marker_root is None:
            return
        if not is_indexed(marker_root):
            return
        if not turn_modified and not index_broken(marker_root):
            return
        st = read_reindex_status(marker_root)
        if st and st.get("ok") is False and not index_dirty(marker_root):
            # Every attempt of the last run failed; don't hammer a repo
            # that fails deterministically on every turn.
            if time.time() - float(st.get("finished_at") or 0) \
                    < _FAILED_BACKOFF_SECONDS:
                return
        if not _acquire_lock(marker_root, lock_min_age_seconds):
            return
        pid = _spawn_analyze(bin_, marker_root)
        if pid is not None:
            _record_lock_pid(marker_root, pid)
    except Exception as e:
        log.debug("gitnexus reindex_if_dirty_async failed: %s", e)


def _find_marker_root(start: Path) -> Optional[Path]:
    p = start.resolve()
    while True:
        if (p / ".gitnexus").is_dir() or (p / ".git").exists():
            return p
        if p.parent == p:
            return None
        p = p.parent


# ---------------------------------------------------------------------------
# SessionStart hint
# ---------------------------------------------------------------------------

_HINT_PREFIX = (
    "_gitnexus is indexed for this repo. For richer queries "
    "(impact, context, cypher, hybrid search), prefer the "
    "`mcp__gitnexus__*` tools when available._"
)


def session_start_hint(
    root: Path,
    *,
    show_init_hint: bool = True,
    enabled: bool = True,
) -> Optional[str]:
    """One-line hint, or None when gitnexus isn't relevant here.

    Parameters mirror :func:`axon_integration.session_start_hint`:

    - ``show_init_hint=False`` suppresses the "Run `gitnexus init` ..."
      nag for projects where the binary is installed but no
      ``.gitnexus/`` index exists. The positive "indexed for this repo"
      hint is unaffected.
    - ``enabled=False`` suppresses all hints — used by the dispatcher
      to skip projects where the user hasn't opted in via per-project
      ``mcpServers`` in ``~/.claude.json``.
    """
    if not enabled:
        return None
    if is_indexed(root):
        if index_broken(root):
            st = read_reindex_status(root) or {}
            when = st.get("finished_at")
            stamp = (time.strftime("%Y-%m-%d %H:%M", time.localtime(when))
                     if when else "unknown time")
            return (
                _HINT_PREFIX + "\n_**The gitnexus index for this repo is "
                f"broken** (last rebuild rc={st.get('returncode')}, "
                f"{'database left mid-write, ' if index_dirty(root) else ''}"
                f"{stamp}). `detect-changes` and graph queries may crash "
                "(segfault) until a rebuild succeeds; one starts after the "
                "next turn. Report a crashed graph check as not run, never "
                "as clean._"
            )
        return _HINT_PREFIX
    if is_available() and show_init_hint:
        return (
            "_gitnexus is installed. Run `gitnexus init` in this repo "
            "to enable richer code-graph queries via its MCP tools._"
        )
    return None


def _main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[0] == "--supervise":
        logging.basicConfig(level=logging.INFO)
        root, binary = Path(argv[1]), argv[2]
        status = supervise_analyze(binary, root)
        return 0 if status["ok"] else 1
    print("usage: python -m claude_hooks.gitnexus_integration "
          "--supervise <root> <gitnexus-binary>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
