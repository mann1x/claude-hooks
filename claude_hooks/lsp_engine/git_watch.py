"""Polling git watcher for branch switches and pulls/resets.

Decision: simple stdlib polling instead of inotify / watchdog. A
1 Hz poll is fast enough for human-scale branch switches (the user's
``git checkout`` finishes far before they invoke Claude Code on the
new branch) and avoids platform-specific FS-watch APIs in Phase 2.
A future Phase 4 pass may add an inotify backend on Linux for tighter
latency on monorepos with deep ref hierarchies.

The watcher tracks two pieces of state:

- ``.git/HEAD`` content. Either ``ref: refs/heads/<branch>\\n`` (on a
  branch) or a 40-char SHA (detached HEAD).
- The contents of the current branch's ref file under
  ``.git/refs/heads/<branch>`` (only when on a branch). Catches pulls,
  commits, resets, rebases — anything that updates the branch tip.

When either changes between polls, the watcher fires its callback.
The callback runs on the watcher thread; expensive work (e.g. bulk
re-``did_open`` of every open file) should be dispatched from the
callback to a worker rather than blocking the watcher's poll loop.

The watcher is a no-op for non-git projects — ``.git/`` simply
doesn't exist, no state to compare, callback never fires. Soft-fail.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("claude_hooks.lsp_engine.git_watch")


DEFAULT_POLL_INTERVAL_S = 1.0


class GitWatcher:
    """Polls ``.git/HEAD`` and the current branch ref for changes."""

    def __init__(
        self,
        project_root: str | os.PathLike,
        on_change: Callable[[str], None],
        *,
        poll_interval: float = DEFAULT_POLL_INTERVAL_S,
    ) -> None:
        self._project_root = Path(project_root)
        self._git_dir = self._project_root / ".git"
        self._on_change = on_change
        self._poll_interval = float(poll_interval)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._snapshot: Optional[tuple[Optional[bytes], Optional[bytes]]] = None

    @property
    def is_git_repo(self) -> bool:
        return self._git_dir.is_dir()

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("GitWatcher already started")
        if not self.is_git_repo:
            log.info(
                "git_watch: %s is not a git repo — watcher inactive",
                self._project_root,
            )
            return
        # Take an initial snapshot so the first poll doesn't fire.
        self._snapshot = self._read_state()
        self._thread = threading.Thread(
            target=self._loop,
            name="lsp-engine-git-watch",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def poll_once(self) -> Optional[str]:
        """Single-shot poll. Returns a short reason string if a change
        fired the callback, ``None`` otherwise. Tests use this to
        drive the watcher deterministically without sleeping the
        poll interval.
        """
        if not self.is_git_repo:
            return None
        if self._snapshot is None:
            self._snapshot = self._read_state()
            return None
        new_state = self._read_state()
        if new_state == self._snapshot:
            return None
        reason = _diff_reason(self._snapshot, new_state)
        self._snapshot = new_state
        try:
            self._on_change(reason)
        except Exception:  # pragma: no cover — defensive
            log.exception("git_watch: on_change callback raised")
        return reason

    # ─── internals ───────────────────────────────────────────────────

    def _loop(self) -> None:
        while not self._stop.wait(timeout=self._poll_interval):
            try:
                self.poll_once()
            except Exception:  # pragma: no cover — defensive
                log.exception("git_watch: poll iteration crashed")

    def _read_state(self) -> tuple[Optional[bytes], Optional[bytes]]:
        head_path = self._git_dir / "HEAD"
        head_bytes: Optional[bytes] = None
        try:
            head_bytes = head_path.read_bytes()
        except OSError:
            return (None, None)

        ref_bytes: Optional[bytes] = None
        # Resolve the branch the symbolic ref points at, if any.
        if head_bytes.startswith(b"ref: "):
            ref_target = head_bytes[5:].strip().decode("utf-8", errors="replace")
            # Ref-target shapes from git: "refs/heads/<branch>",
            # "refs/remotes/...", etc. We only watch local branch refs;
            # detached HEAD already covered by the HEAD bytes above.
            ref_path = self._git_dir / ref_target
            try:
                ref_bytes = ref_path.read_bytes()
            except OSError:
                # Branch tip may live in packed-refs after a gc.
                packed_refs = self._git_dir / "packed-refs"
                try:
                    packed = packed_refs.read_bytes()
                except OSError:
                    packed = b""
                ref_bytes = _find_in_packed_refs(packed, ref_target)
        return (head_bytes, ref_bytes)


def _find_in_packed_refs(packed: bytes, ref_name: str) -> Optional[bytes]:
    """Linear scan ``packed-refs`` for ``ref_name``. Returns the SHA
    line (still ending in newline) if found, else ``None``.

    packed-refs lines look like ``<sha> refs/heads/<branch>``; comments
    start with ``#`` and we ignore them. Annotated tag dereferences
    (``^<sha>``) are likewise ignored — they only matter for tags.
    """
    target = ref_name.encode("utf-8")
    for raw_line in packed.splitlines(keepends=True):
        line = raw_line.strip()
        if not line or line.startswith(b"#") or line.startswith(b"^"):
            continue
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[1] == target:
            return parts[0] + b"\n"
    return None


def _diff_reason(
    old: tuple[Optional[bytes], Optional[bytes]],
    new: tuple[Optional[bytes], Optional[bytes]],
) -> str:
    """Best-effort label for the kind of change observed. Used for
    log lines and as the argument passed to the on_change callback.
    """
    old_head, old_ref = old
    new_head, new_ref = new
    if old_head != new_head:
        return "head_changed"
    if old_ref != new_ref:
        return "ref_updated"
    return "unknown"
