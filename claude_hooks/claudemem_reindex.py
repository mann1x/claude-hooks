"""
claudemem auto-reindex helpers.

claudemem (https://github.com/MadAppGang/claudemem) is a semantic code
search tool whose index must be kept in sync with the project files.
Upstream ships:

- ``claudemem hooks install`` — a git post-commit hook that reindexes
  after each commit. Installed at ``.git/hooks/post-commit``.
- ``claudemem watch`` — a daemon with a filesystem watcher.

Neither covers the gap of **mid-session edits that haven't been
committed yet**. This module provides two helpers for claude-hooks to
bridge that gap:

1. :func:`reindex_if_dirty_async` — fires at end of assistant turn
   (Stop hook). If any Edit/Write/MultiEdit ran this turn, spawn
   ``claudemem index --quiet`` as a detached background process so the
   user doesn't see per-turn latency.

2. :func:`reindex_if_stale_async` — fires on SessionStart. If the
   index mtime trails the newest source-file mtime by more than
   ``staleness_minutes``, kick off a detached reindex. Catches changes
   made outside Claude Code (manual edits, git pulls, branch switches).

Both helpers are **silent no-ops** when:

- ``claudemem`` binary is not on PATH
- project is not a git repo or has no .claudemem/ dir
- ``claudemem index`` is already running for this project
- background spawn fails for any reason

Design principles:

- Never block the hook. The detached ``Popen`` returns immediately.
- Never raise — all failure modes swallowed, logged at debug level.
- Never re-index more than once per minute per project (cheap lock
  file guard avoids pileups when the Stop hook fires rapidly).
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("claude_hooks.claudemem_reindex")


_LOCK_FILENAME = ".claudemem-reindex.lock"
# Default cooldown — don't spawn another reindex if one ran this recently.
# Configurable per-call via ``lock_min_age_seconds``.
_DEFAULT_LOCK_MIN_AGE_SECONDS = 60

# Windows-only Popen creation flags. Suppresses the cmd-shim console
# window that ``claudemem.cmd`` would otherwise allocate (claudemem is
# a Node CLI shipped via npm, so its bin is a .cmd shim that goes
# through cmd.exe → spawns a new console). On a long-running reindex
# (5+ minutes on a large repo) the window stays visible for the entire
# duration. CREATE_NO_WINDOW prevents the allocation; DETACHED_PROCESS
# keeps the child off the parent's process group too. Imported lazily
# inside ``_spawn_reindex`` so POSIX platforms never touch the symbols.

# Directories we never need to scan for staleness detection. Users can
# extend this via config (``hooks.claudemem_reindex.ignored_dirs``).
_DEFAULT_IGNORED_DIRS: frozenset[str] = frozenset({
    ".git", ".claudemem", ".caliber", ".wolf",
    "node_modules", "__pycache__", ".venv", "venv", ".env",
    ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".cache", ".npm", ".yarn",
    "dist", "build", "target", "out",
})


def _find_claudemem() -> Optional[str]:
    return shutil.which("claudemem")


def _project_root(cwd: str) -> Optional[Path]:
    """Walk up from cwd until we find a ``.git`` entry (file or dir).

    Note: ``.git`` can be either a directory (regular repo) or a plain file
    (git worktree / submodule pointing at the real gitdir). Either counts
    as a project root for our purposes.
    """
    if not cwd:
        return None
    p = Path(cwd).resolve()
    while True:
        if (p / ".git").exists():
            return p
        if p.parent == p:
            return None
        p = p.parent


def _claudemem_indexed(root: Path) -> bool:
    """True iff this project has been indexed at least once."""
    return (root / ".claudemem").is_dir()


def _pid_running(pid: int) -> bool:
    """Best-effort cross-platform 'is this PID alive?' check.

    POSIX uses ``kill(pid, 0)``; Windows uses ``OpenProcess`` via ctypes
    (no psutil dependency — keep this module stdlib-only). Returns
    ``False`` on any error so a stale-but-uncheckable lock falls
    through and a new reindex can spawn. Conservative: prefers
    re-spawning over assuming a process is still running.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes  # local import — POSIX never pays this
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            handle = kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid),
            )
            if not handle:
                return False
            # Pull exit code; STILL_ACTIVE (259) means the process is alive.
            exit_code = ctypes.c_ulong()
            ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            kernel32.CloseHandle(handle)
            if not ok:
                return False
            return exit_code.value == 259  # STILL_ACTIVE
        except Exception:
            return False
    # POSIX
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def _read_lock(lock: Path) -> tuple[Optional[int], Optional[float]]:
    """Parse the lock file. Returns (pid, timestamp) — either may be
    ``None`` if the file is missing or in an older single-timestamp
    format. New format is two lines: ``<pid>\\n<unix-ts>``."""
    try:
        body = lock.read_text(encoding="utf-8").strip()
    except OSError:
        return None, None
    if not body:
        return None, None
    parts = body.split("\n", 1)
    if len(parts) == 2:
        try:
            return int(parts[0]), float(parts[1])
        except ValueError:
            return None, None
    # Legacy single-line timestamp (pre-PID lock format). Treat as
    # "no PID known" so the cooldown still applies but a stale-running
    # check can't be done.
    try:
        return None, float(parts[0])
    except ValueError:
        return None, None


def _acquire_lock(
    root: Path,
    min_age_seconds: int = _DEFAULT_LOCK_MIN_AGE_SECONDS,
) -> bool:
    """Return True if we should proceed, False otherwise.

    Two guards combine:

    1. **Live-process check** — if the lock names a PID and that PID
       is still running, refuse regardless of age. Prevents pile-ups
       when the previous reindex outlives the cooldown (claudemem on a
       large repo over a remote Ollama can run for many minutes).
    2. **Cooldown** — if the lock's timestamp is younger than
       ``min_age_seconds``, refuse. Prevents rapid Stop-hook
       reentry from spawning multiple back-to-back indexes when no
       previous PID was recorded (legacy lock format).
    """
    lock = root / _LOCK_FILENAME
    now = time.time()
    pid, ts = _read_lock(lock)

    if pid is not None and _pid_running(pid):
        log.debug("reindex lock held by live pid %d — skipping", pid)
        return False

    if ts is not None:
        age = now - ts
        if age < min_age_seconds:
            log.debug("reindex lock fresh (%ds old) — skipping", int(age))
            return False

    # Caller is expected to spawn next and then call ``_record_lock_pid``
    # to stamp the actual child PID. We pre-write the timestamp here so
    # a crash between this and the spawn still updates the cooldown.
    try:
        lock.write_text(str(int(now)), encoding="utf-8")
    except OSError as e:
        log.debug("could not write reindex lock: %s", e)
        return False
    return True


def _record_lock_pid(root: Path, pid: int) -> None:
    """Stamp the spawned PID into the lock file. Safe-by-design — any
    failure (disk full, perms) leaves the legacy timestamp-only lock,
    which still serves the cooldown role."""
    lock = root / _LOCK_FILENAME
    try:
        lock.write_text(f"{pid}\n{int(time.time())}", encoding="utf-8")
    except OSError as e:
        log.debug("could not stamp pid into reindex lock: %s", e)


def _index_mtime(claudemem_dir: Path) -> Optional[float]:
    """Return the index's effective last-updated time.

    Prefers ``index.db`` (the primary store) when present so we don't have
    to walk the whole ``.claudemem/`` tree. Falls back to the directory's
    own mtime, then to the max mtime of its contents.
    """
    primary = claudemem_dir / "index.db"
    if primary.exists():
        try:
            return primary.stat().st_mtime
        except OSError:
            pass
    try:
        return claudemem_dir.stat().st_mtime
    except OSError:
        pass
    try:
        return max(
            p.stat().st_mtime for p in claudemem_dir.rglob("*") if p.is_file()
        )
    except (OSError, ValueError):
        return None


_CMD_SHIM_TARGET_RE = re.compile(
    r'"(?P<prog>[^"]+)"\s+"(?P<js>[^"]+\.js)"\s+%\*\s*$',
    re.IGNORECASE,
)


def _resolve_npm_cmd_shim(cmd_path: str) -> Optional[tuple[str, str]]:
    """Parse an npm-generated ``.cmd`` shim and return ``(runtime, js_path)``.

    npm cmd shims hand off to ``node`` / ``bun`` / ``deno`` via a line
    of the form

        ... & "%_prog%"  "%dp0%\\node_modules\\<pkg>\\dist\\index.js" %*

    On Windows, spawning the shim directly — even with
    ``CREATE_NO_WINDOW | DETACHED_PROCESS`` — leaves a visible
    ``cmd.exe`` console for the lifetime of the child, because the
    shim's ``title %COMSPEC%`` line forces a console allocation
    before the runtime starts. The fix that makes the flash go away
    for real is the same one that landed in caliber PR #197: skip
    the shim, invoke the runtime + entry-point JS directly.

    Returns None when the path doesn't exist, isn't a recognisable
    npm shim, or refers to a runtime not on PATH. Caller falls back
    to spawning the shim.
    """
    try:
        with open(cmd_path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return None
    m = _CMD_SHIM_TARGET_RE.search(content)
    if not m:
        return None
    prog = m.group("prog").strip()
    js_template = m.group("js").strip()
    # The shim uses ``%dp0%`` (the .cmd's own dir) with backslash
    # separators; cmd shims are Windows-only so we use ``\\`` directly
    # instead of os.sep (lets the resolver be unit-tested cross-platform).
    dp0 = os.path.dirname(os.path.abspath(cmd_path))
    if not (dp0.endswith("\\") or dp0.endswith("/")):
        dp0 += "\\"
    js_path = js_template.replace("%dp0%\\", dp0).replace("%dp0%/", dp0).replace("%dp0%", dp0)
    # Convert all backslashes to forward slashes after substitution.
    # Windows accepts both as path separators, and this lets the unit
    # tests run on POSIX (where os.path.normpath does not translate
    # backslashes). We then normalise once more for cleanliness.
    js_path = os.path.normpath(js_path.replace("\\", "/"))
    if not os.path.exists(js_path):
        return None
    # Resolve the runtime — shims often use literal ``%_prog%`` or
    # ``"%~dp0\\bun.exe"``. Try absolute → PATH lookup → bare name.
    if "%" in prog:
        # Pattern like "%_prog%" — fall back to the bare runtime name
        # used by claudemem (bun) / most npm packages (node).
        runtime_candidates = ["bun", "node"]
    elif os.path.exists(prog):
        runtime_candidates = [prog]
    else:
        runtime_candidates = [prog]
    for rc in runtime_candidates:
        resolved = shutil.which(rc) or (rc if os.path.exists(rc) else None)
        if resolved:
            return (resolved, js_path)
    return None


def _spawn_reindex(binary: str, root: Path) -> Optional[int]:
    """Start ``claudemem index --quiet`` as a detached background process.

    Returns the spawned PID on success, ``None`` on failure. The PID
    is recorded into the lock file by the caller so subsequent
    invocations can skip while the prior reindex is still running.

    On Windows, ``creationflags=CREATE_NO_WINDOW | DETACHED_PROCESS``
    alone is NOT enough — the npm-generated ``claudemem.cmd`` shim
    has a ``title %COMSPEC%`` line that forces a visible cmd window
    before the runtime even starts. We resolve the shim's target
    (``bun ...\\mnemex\\dist\\index.js``) and spawn the runtime
    directly; only then does CREATE_NO_WINDOW suppress the console
    for real. Same pattern as caliber PR #197 (cmd-shim flash fix).

    On POSIX, ``start_new_session=True`` already detaches the child
    from our session and pgrp; the binary is a real ELF/Mach-O so
    the shim-bypass logic never fires.
    """
    kwargs: dict = {
        "cwd": str(root),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
            | subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]
        )
    else:
        kwargs["start_new_session"] = True

    argv: list[str] = [binary, "index", "--quiet"]
    if os.name == "nt" and binary.lower().endswith(".cmd"):
        resolved = _resolve_npm_cmd_shim(binary)
        if resolved:
            runtime, js_path = resolved
            argv = [runtime, js_path, "index", "--quiet"]
            log.debug("bypassing cmd shim: %s -> %s %s", binary, runtime, js_path)

    try:
        proc = subprocess.Popen(argv, **kwargs)
        log.info("spawned claudemem index --quiet in %s (pid=%d)", root, proc.pid)
        return proc.pid
    except OSError as e:
        log.debug("could not spawn claudemem: %s", e)
        return None


def reindex_if_dirty_async(
    *,
    cwd: str,
    turn_modified: bool,
    lock_min_age_seconds: int = _DEFAULT_LOCK_MIN_AGE_SECONDS,
) -> None:
    """Spawn a reindex if the turn touched source files. Never raises."""
    try:
        if not turn_modified:
            return
        binary = _find_claudemem()
        if not binary:
            log.debug("claudemem not on PATH — skip")
            return
        root = _project_root(cwd)
        if not root:
            log.debug("cwd %r is not inside a git repo — skip", cwd)
            return
        if not _claudemem_indexed(root):
            log.debug("project %s has no .claudemem — skip", root)
            return
        if not _acquire_lock(root, min_age_seconds=lock_min_age_seconds):
            return
        pid = _spawn_reindex(binary, root)
        if pid is not None:
            _record_lock_pid(root, pid)
    except Exception as e:
        log.debug("reindex_if_dirty_async failed: %s", e)


def reindex_if_stale_async(
    *,
    cwd: str,
    staleness_minutes: int = 10,
    max_files_to_scan: int = 2000,
    ignored_dirs: Optional[frozenset[str]] = None,
    lock_min_age_seconds: int = _DEFAULT_LOCK_MIN_AGE_SECONDS,
) -> None:
    """Spawn a reindex when the project has drifted past the staleness window.

    Semantics (NB: not the same as "index trails newest source by N minutes"):

    1. Compute the index's last-updated time. If less than
       ``staleness_minutes`` have passed since then, return without any
       further work. This is a cooldown: it bounds how often we can spawn
       a reindex regardless of source churn, preventing thrash when files
       are edited rapidly.

    2. Otherwise, walk the project (skipping ``ignored_dirs``) looking for
       any source file with an mtime greater than the index's. Bail on the
       first hit and spawn a detached reindex.

    ``max_files_to_scan`` caps the walk so that pathologically large
    projects don't freeze a SessionStart hook. If the cap is reached
    without a hit we emit a debug log and return — downstream the
    post-commit hook or a later SessionStart will still catch up.

    Never raises; all OS errors are swallowed.
    """
    try:
        binary = _find_claudemem()
        if not binary:
            return
        root = _project_root(cwd)
        if not root:
            return
        claudemem_dir = root / ".claudemem"
        if not claudemem_dir.is_dir():
            return

        index_mtime = _index_mtime(claudemem_dir)
        if index_mtime is None:
            return

        # Cooldown: don't reindex within the staleness window, even if
        # source files are already newer than the index.
        if index_mtime + staleness_minutes * 60 > time.time():
            return

        ignored = ignored_dirs if ignored_dirs is not None else _DEFAULT_IGNORED_DIRS
        count = 0
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in ignored]
            for fn in filenames:
                count += 1
                if count > max_files_to_scan:
                    log.debug(
                        "stale-scan reached max_files_to_scan=%d for %s — "
                        "no stale file found yet; will re-check next session",
                        max_files_to_scan, root,
                    )
                    return
                try:
                    mt = (Path(dirpath) / fn).stat().st_mtime
                except OSError:
                    continue
                if mt > index_mtime:
                    if _acquire_lock(root, min_age_seconds=lock_min_age_seconds):
                        pid = _spawn_reindex(binary, root)
                        if pid is not None:
                            _record_lock_pid(root, pid)
                    return
    except Exception as e:
        log.debug("reindex_if_stale_async failed: %s", e)
