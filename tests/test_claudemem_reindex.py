"""Tests for the claudemem auto-reindex helpers."""

import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from claude_hooks import claudemem_reindex


class ProjectRootTests(unittest.TestCase):
    def test_finds_git_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            sub = root / "a" / "b"
            sub.mkdir(parents=True)
            self.assertEqual(claudemem_reindex._project_root(str(sub)), root)

    def test_none_when_no_git(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(claudemem_reindex._project_root(tmp))

    def test_none_on_empty_cwd(self):
        self.assertIsNone(claudemem_reindex._project_root(""))


class LockTests(unittest.TestCase):
    def test_fresh_lock_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock = root / claudemem_reindex._LOCK_FILENAME
            lock.write_text(str(int(time.time())))
            self.assertFalse(claudemem_reindex._acquire_lock(root))

    def test_stale_lock_allows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock = root / claudemem_reindex._LOCK_FILENAME
            lock.write_text("x")
            # Backdate the lock well past the threshold.
            old = time.time() - claudemem_reindex._DEFAULT_LOCK_MIN_AGE_SECONDS - 30
            os.utime(lock, (old, old))
            self.assertTrue(claudemem_reindex._acquire_lock(root))

    def test_no_lock_allows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertTrue(claudemem_reindex._acquire_lock(root))
            self.assertTrue((root / claudemem_reindex._LOCK_FILENAME).exists())


class ReindexIfDirtyTests(unittest.TestCase):
    def _make_project(self, tmp: str) -> Path:
        root = Path(tmp)
        (root / ".git").mkdir()
        (root / ".claudemem").mkdir()
        return root

    def test_skip_when_not_modified(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._make_project(tmp)
            with patch("claude_hooks.claudemem_reindex._spawn_reindex") as spawn:
                claudemem_reindex.reindex_if_dirty_async(
                    cwd=str(root), turn_modified=False,
                )
                spawn.assert_not_called()

    def test_skip_when_binary_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._make_project(tmp)
            with patch("claude_hooks.claudemem_reindex.shutil.which", return_value=None), \
                 patch("claude_hooks.claudemem_reindex._spawn_reindex") as spawn:
                claudemem_reindex.reindex_if_dirty_async(
                    cwd=str(root), turn_modified=True,
                )
                spawn.assert_not_called()

    def test_skip_when_no_git(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("claude_hooks.claudemem_reindex.shutil.which", return_value="/usr/bin/claudemem"), \
                 patch("claude_hooks.claudemem_reindex._spawn_reindex") as spawn:
                claudemem_reindex.reindex_if_dirty_async(
                    cwd=tmp, turn_modified=True,
                )
                spawn.assert_not_called()

    def test_skip_when_no_claudemem_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            with patch("claude_hooks.claudemem_reindex.shutil.which", return_value="/usr/bin/claudemem"), \
                 patch("claude_hooks.claudemem_reindex._spawn_reindex") as spawn:
                claudemem_reindex.reindex_if_dirty_async(
                    cwd=str(root), turn_modified=True,
                )
                spawn.assert_not_called()

    def test_spawns_when_all_conditions_met(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._make_project(tmp)
            with patch("claude_hooks.claudemem_reindex.shutil.which", return_value="/usr/bin/claudemem"), \
                 patch("claude_hooks.claudemem_reindex._spawn_reindex") as spawn:
                claudemem_reindex.reindex_if_dirty_async(
                    cwd=str(root), turn_modified=True,
                )
                spawn.assert_called_once()

    def test_does_not_raise_on_spawn_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._make_project(tmp)
            with patch("claude_hooks.claudemem_reindex.shutil.which", return_value="/usr/bin/claudemem"), \
                 patch(
                    "claude_hooks.claudemem_reindex.subprocess.Popen",
                    side_effect=OSError("simulated"),
                 ):
                # Must not raise.
                claudemem_reindex.reindex_if_dirty_async(
                    cwd=str(root), turn_modified=True,
                )


class ReindexIfStaleTests(unittest.TestCase):
    def _make_indexed_project(self, tmp: str) -> Path:
        root = Path(tmp)
        (root / ".git").mkdir()
        claudemem_dir = root / ".claudemem"
        claudemem_dir.mkdir()
        (claudemem_dir / "index.db").write_text("x")
        return root

    def test_skip_when_no_source_newer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._make_indexed_project(tmp)
            # Make the index newer than any source.
            src = root / "main.py"
            src.write_text("x")
            os.utime(src, (time.time() - 3600, time.time() - 3600))
            idx = root / ".claudemem" / "index.db"
            os.utime(idx, (time.time(), time.time()))
            with patch("claude_hooks.claudemem_reindex.shutil.which", return_value="/usr/bin/claudemem"), \
                 patch("claude_hooks.claudemem_reindex._spawn_reindex") as spawn:
                claudemem_reindex.reindex_if_stale_async(
                    cwd=str(root), staleness_minutes=0,
                )
                spawn.assert_not_called()

    def test_spawns_when_source_newer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._make_indexed_project(tmp)
            # Backdate the index.
            idx = root / ".claudemem" / "index.db"
            old = time.time() - 3600
            os.utime(idx, (old, old))
            # New source file
            (root / "main.py").write_text("x")
            with patch("claude_hooks.claudemem_reindex.shutil.which", return_value="/usr/bin/claudemem"), \
                 patch("claude_hooks.claudemem_reindex._spawn_reindex") as spawn:
                claudemem_reindex.reindex_if_stale_async(
                    cwd=str(root), staleness_minutes=0,
                )
                spawn.assert_called_once()

    def test_skip_inside_staleness_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._make_indexed_project(tmp)
            # Index updated 2 minutes ago.
            idx = root / ".claudemem" / "index.db"
            two_min_ago = time.time() - 120
            os.utime(idx, (two_min_ago, two_min_ago))
            (root / "main.py").write_text("x")
            with patch("claude_hooks.claudemem_reindex.shutil.which", return_value="/usr/bin/claudemem"), \
                 patch("claude_hooks.claudemem_reindex._spawn_reindex") as spawn:
                # Window is 10 min, so even if a source is newer we wait.
                claudemem_reindex.reindex_if_stale_async(
                    cwd=str(root), staleness_minutes=10,
                )
                spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
