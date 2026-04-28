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

    def test_live_pid_blocks_even_when_old(self):
        """The previous reindex outlives the cooldown — pile-up
        prevention. claudemem index on a large repo can run for
        minutes; without this check, the next Stop hook would spawn a
        parallel reindex against the same Lance/sqlite store."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock = root / claudemem_reindex._LOCK_FILENAME
            # Pretend a process owns the lock with a stamp older than cooldown.
            lock.write_text(f"{os.getpid()}\n{int(time.time()) - 999}")
            # _pid_running on our own pid returns True.
            self.assertFalse(claudemem_reindex._acquire_lock(root))

    def test_dead_pid_with_old_timestamp_allows(self):
        """Lock left by a crashed-or-completed reindex must NOT
        permanently wedge the cooldown. PID is gone + timestamp is
        old → allow the next reindex."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock = root / claudemem_reindex._LOCK_FILENAME
            # Pid 1 is init/launchd on POSIX, System on Windows — never
            # something the lock could legitimately have stamped, but
            # _pid_running will report it alive. Use a high unallocated
            # pid instead so the check returns False.
            stale_pid = 999_999_999
            self.assertFalse(claudemem_reindex._pid_running(stale_pid))
            old_ts = int(time.time()) - 999
            lock.write_text(f"{stale_pid}\n{old_ts}")
            self.assertTrue(claudemem_reindex._acquire_lock(root))

    def test_dead_pid_with_recent_timestamp_still_blocks_on_cooldown(self):
        """PID is gone but the cooldown hasn't passed — still refuse,
        otherwise rapid Stop-hook reentry would spawn pile-ups
        regardless of the previous run's outcome."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock = root / claudemem_reindex._LOCK_FILENAME
            stale_pid = 999_999_999
            self.assertFalse(claudemem_reindex._pid_running(stale_pid))
            recent_ts = int(time.time())
            lock.write_text(f"{stale_pid}\n{recent_ts}")
            self.assertFalse(claudemem_reindex._acquire_lock(root))

    def test_legacy_timestamp_only_format_still_works(self):
        """Pre-PID lock format: a single-line unix timestamp. We must
        keep parsing it correctly so existing on-disk locks don't break
        when this code rolls out."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock = root / claudemem_reindex._LOCK_FILENAME
            lock.write_text(str(int(time.time())))
            pid, ts = claudemem_reindex._read_lock(lock)
            self.assertIsNone(pid)
            self.assertIsNotNone(ts)
            self.assertFalse(claudemem_reindex._acquire_lock(root))

    def test_record_lock_pid_writes_new_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claudemem_reindex._record_lock_pid(root, 12345)
            pid, ts = claudemem_reindex._read_lock(
                root / claudemem_reindex._LOCK_FILENAME,
            )
            self.assertEqual(pid, 12345)
            self.assertIsNotNone(ts)
            # Sanity: ts should be approximately now.
            self.assertLess(abs(ts - time.time()), 5)


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


class SpawnReindexTests(unittest.TestCase):
    """``_spawn_reindex`` must (a) launch detached without leaving a
    visible window on Windows, and (b) return the spawned PID so the
    caller can stamp it into the lock for pile-up prevention."""

    def test_returns_pid_on_success(self):
        fake = MagicMock()
        fake.pid = 4242
        with patch("claude_hooks.claudemem_reindex.subprocess.Popen", return_value=fake):
            pid = claudemem_reindex._spawn_reindex("/usr/bin/claudemem", Path("/tmp"))
        self.assertEqual(pid, 4242)

    def test_returns_none_on_oserror(self):
        with patch(
            "claude_hooks.claudemem_reindex.subprocess.Popen",
            side_effect=OSError("simulated"),
        ):
            pid = claudemem_reindex._spawn_reindex("/usr/bin/claudemem", Path("/tmp"))
        self.assertIsNone(pid)

    def test_posix_uses_start_new_session(self):
        """On POSIX, the helper must pass ``start_new_session=True`` so
        the child detaches from our process group — otherwise SIGINT to
        the daemon would also kill the child reindex."""
        with patch("claude_hooks.claudemem_reindex.os.name", "posix"), \
             patch("claude_hooks.claudemem_reindex.subprocess.Popen") as Popen:
            Popen.return_value = MagicMock(pid=1)
            claudemem_reindex._spawn_reindex("/usr/bin/claudemem", Path("/tmp"))
            kwargs = Popen.call_args.kwargs
            self.assertTrue(kwargs.get("start_new_session"))
            self.assertNotIn("creationflags", kwargs)

    def test_windows_uses_creationflags_no_window(self):
        """The pandorum bug: a long-running ``claudemem index --quiet``
        spawned via Popen WITHOUT creationflags pops a visible cmd
        window for the whole multi-minute reindex. CREATE_NO_WINDOW +
        DETACHED_PROCESS prevents the console allocation."""
        # subprocess.CREATE_NO_WINDOW / DETACHED_PROCESS only exist as
        # attrs on Windows builds of Python; provide them as constants
        # on the patched module so the helper's bit-or works on POSIX.
        CREATE_NO_WINDOW = 0x08000000
        DETACHED_PROCESS = 0x00000008
        # Pre-construct the Path BEFORE patching os.name — pathlib's
        # Path() picks Posix vs Windows at instantiation time and would
        # raise on Linux if asked to build a WindowsPath.
        root = Path("/tmp")
        with patch("claude_hooks.claudemem_reindex.os.name", "nt"), \
             patch("claude_hooks.claudemem_reindex.subprocess.Popen") as Popen, \
             patch.object(
                claudemem_reindex.subprocess,
                "CREATE_NO_WINDOW",
                CREATE_NO_WINDOW,
                create=True,
            ), \
             patch.object(
                claudemem_reindex.subprocess,
                "DETACHED_PROCESS",
                DETACHED_PROCESS,
                create=True,
            ):
            Popen.return_value = MagicMock(pid=1)
            claudemem_reindex._spawn_reindex("C:/x/claudemem.cmd", root)
            kwargs = Popen.call_args.kwargs
            self.assertNotIn("start_new_session", kwargs)
            flags = kwargs.get("creationflags", 0)
            self.assertTrue(flags & CREATE_NO_WINDOW,
                            "CREATE_NO_WINDOW must be set on Windows")
            self.assertTrue(flags & DETACHED_PROCESS,
                            "DETACHED_PROCESS must be set on Windows")


class IntegrationLockingTests(unittest.TestCase):
    """End-to-end: reindex_if_dirty_async must stamp the PID into the
    lock so the next call within the cooldown window can see it's
    still running and skip."""

    def _make_project(self, tmp: str) -> Path:
        root = Path(tmp)
        (root / ".git").mkdir()
        (root / ".claudemem").mkdir()
        return root

    def test_pid_stamped_after_successful_spawn(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._make_project(tmp)
            with patch("claude_hooks.claudemem_reindex.shutil.which",
                       return_value="/usr/bin/claudemem"), \
                 patch("claude_hooks.claudemem_reindex._spawn_reindex",
                       return_value=77777):
                claudemem_reindex.reindex_if_dirty_async(
                    cwd=str(root), turn_modified=True,
                )
            pid, ts = claudemem_reindex._read_lock(
                root / claudemem_reindex._LOCK_FILENAME,
            )
            self.assertEqual(pid, 77777)
            self.assertIsNotNone(ts)

    def test_no_pid_stamp_when_spawn_fails(self):
        """If the child failed to launch (Popen returned None), the
        lock must NOT carry a stale PID — the legacy timestamp-only
        format is safe and lets the next attempt retry after cooldown."""
        with tempfile.TemporaryDirectory() as tmp:
            root = self._make_project(tmp)
            with patch("claude_hooks.claudemem_reindex.shutil.which",
                       return_value="/usr/bin/claudemem"), \
                 patch("claude_hooks.claudemem_reindex._spawn_reindex",
                       return_value=None):
                claudemem_reindex.reindex_if_dirty_async(
                    cwd=str(root), turn_modified=True,
                )
            pid, ts = claudemem_reindex._read_lock(
                root / claudemem_reindex._LOCK_FILENAME,
            )
            self.assertIsNone(pid)
            self.assertIsNotNone(ts)


if __name__ == "__main__":
    unittest.main()
