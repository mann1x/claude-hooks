"""Tests for ``GitWatcher`` — polling-based HEAD/refs change detector.

We synthesise a minimal ``.git/`` layout (HEAD pointing at a branch
ref + the ref file with a SHA) and toggle its contents to verify
the watcher fires its callback. No actual ``git`` commands are run
— the polled state is fully under our control.
"""

from __future__ import annotations

import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import List

from claude_hooks.lsp_engine.git_watch import (
    GitWatcher,
    _diff_reason,
    _find_in_packed_refs,
)


def _make_repo(tmp: Path, *, branch: str = "main", sha: str = "a" * 40) -> Path:
    """Build a synthetic ``.git/`` directory: HEAD pointing at a
    branch ref, ref file with a SHA. Returns the repo root.
    """
    git = tmp / ".git"
    refs = git / "refs" / "heads"
    refs.mkdir(parents=True, exist_ok=True)
    (git / "HEAD").write_text(f"ref: refs/heads/{branch}\n", encoding="utf-8")
    (refs / branch).write_text(f"{sha}\n", encoding="utf-8")
    return tmp


class TestGitWatcherDetection(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "repo"
        self.root.mkdir()
        _make_repo(self.root, branch="main", sha="a" * 40)
        self.fired: List[str] = []
        self.watcher = GitWatcher(
            self.root,
            on_change=self.fired.append,
            poll_interval=0.05,
        )
        # Don't auto-start — most tests use poll_once for determinism.

    def tearDown(self) -> None:
        self.watcher.stop()

    def test_no_change_means_no_fire(self) -> None:
        self.watcher.start()
        # First poll just snapshots; any further polls should see
        # the identical state and stay silent.
        time.sleep(0.20)
        self.assertEqual(self.fired, [])

    def test_branch_switch_fires_head_changed(self) -> None:
        # Snapshot before the switch (HEAD is on main from setUp).
        self.watcher.poll_once()  # initial snapshot
        # Add a second branch ref *without* moving HEAD yet.
        (self.root / ".git" / "refs" / "heads" / "feature").write_text(
            "b" * 40 + "\n", encoding="utf-8",
        )
        # Now flip HEAD to that branch — this is the actual switch.
        (self.root / ".git" / "HEAD").write_text(
            "ref: refs/heads/feature\n", encoding="utf-8",
        )
        reason = self.watcher.poll_once()
        self.assertEqual(reason, "head_changed")
        self.assertEqual(self.fired, ["head_changed"])

    def test_pull_or_reset_fires_ref_updated(self) -> None:
        self.watcher.poll_once()  # initial snapshot
        (self.root / ".git" / "refs" / "heads" / "main").write_text(
            "c" * 40 + "\n", encoding="utf-8",
        )
        reason = self.watcher.poll_once()
        self.assertEqual(reason, "ref_updated")
        self.assertEqual(self.fired, ["ref_updated"])

    def test_detached_head_then_branch_fires_head_changed(self) -> None:
        # Detach HEAD (write a literal SHA, no "ref: " prefix).
        self.watcher.poll_once()  # initial snapshot
        (self.root / ".git" / "HEAD").write_text("d" * 40 + "\n", encoding="utf-8")
        reason = self.watcher.poll_once()
        self.assertEqual(reason, "head_changed")

    def test_non_git_directory_silently_inactive(self) -> None:
        with TemporaryDirectory() as tmp:
            non_git = Path(tmp)
            fired: List[str] = []
            w = GitWatcher(non_git, on_change=fired.append, poll_interval=0.05)
            self.assertFalse(w.is_git_repo)
            w.start()  # no-op
            time.sleep(0.10)
            self.assertEqual(fired, [])
            w.stop()

    def test_start_twice_raises(self) -> None:
        self.watcher.start()
        with self.assertRaises(RuntimeError):
            self.watcher.start()


class TestGitWatcherCallbackThreading(unittest.TestCase):
    """The polling thread must actually fire the callback on real
    elapsed time, not just inside ``poll_once``."""

    def test_real_poll_thread_fires_within_a_few_intervals(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            _make_repo(root)
            evt = threading.Event()
            w = GitWatcher(
                root,
                on_change=lambda _reason: evt.set(),
                poll_interval=0.05,
            )
            w.start()
            try:
                time.sleep(0.10)  # let the watcher snapshot
                # Trigger a change.
                (root / ".git" / "refs" / "heads" / "main").write_text(
                    "f" * 40 + "\n", encoding="utf-8",
                )
                self.assertTrue(evt.wait(timeout=1.0))
            finally:
                w.stop()


class TestPackedRefsScanner(unittest.TestCase):
    def test_finds_known_ref_in_packed_refs(self) -> None:
        packed = (
            b"# pack-refs with: peeled fully-peeled sorted\n"
            b"abc123def 456 refs/heads/main\n".replace(b" 456 ", b" ")
        )
        # Simpler explicit fixture:
        packed = (
            b"# pack-refs with: peeled fully-peeled sorted\n"
            b"abc123def4567890abc123def4567890abc12345 refs/heads/main\n"
            b"^deadbeef0123456789abcdef0123456789abcdef\n"  # peeled tag, ignored
            b"feedfacecafebeef0123456789abcdef01234567 refs/heads/feature\n"
        )
        self.assertEqual(
            _find_in_packed_refs(packed, "refs/heads/main"),
            b"abc123def4567890abc123def4567890abc12345\n",
        )
        self.assertEqual(
            _find_in_packed_refs(packed, "refs/heads/feature"),
            b"feedfacecafebeef0123456789abcdef01234567\n",
        )

    def test_returns_none_for_missing_ref(self) -> None:
        self.assertIsNone(
            _find_in_packed_refs(b"abc refs/heads/main\n", "refs/heads/dev"),
        )


class TestDiffReason(unittest.TestCase):
    def test_head_change_dominates(self) -> None:
        old = (b"ref: refs/heads/main\n", b"sha-1\n")
        new = (b"ref: refs/heads/feature\n", b"sha-2\n")
        self.assertEqual(_diff_reason(old, new), "head_changed")

    def test_ref_only_change(self) -> None:
        old = (b"ref: refs/heads/main\n", b"sha-1\n")
        new = (b"ref: refs/heads/main\n", b"sha-2\n")
        self.assertEqual(_diff_reason(old, new), "ref_updated")


if __name__ == "__main__":
    unittest.main()
