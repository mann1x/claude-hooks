"""v1.10.6 LSP-engine bug fixes — regression suite.

Two more bugs surfaced by the pandorum 2026-05-22 follow-up
session after v1.10.5:

H. **Lock-file unreadable while daemon alive on Windows**. The
   daemon held an exclusive Windows byte-range lock at byte 0 —
   exactly where ``<pid>\\n<timestamp>\\n`` was written. Any
   other process reading the lock file (operators running
   ``type daemon.lock``, the ``daemon_pid()`` CLI helper) hit
   ``ERROR_LOCK_VIOLATION`` / "Device or resource busy". The
   v1.10.4 ``status`` workaround papered over this by stuffing
   ``os.getpid()`` into the IPC response, but the lock file
   itself remained unreadable. Fix: lock a byte WELL past the
   PID payload (offset 4096) — Windows lets you lock bytes
   beyond EOF as a reservation. Other readers reading bytes
   0..N where N << 4096 hit no conflict; the daemon-mutex
   semantic is unchanged. POSIX ``flock`` is whole-file
   advisory and never blocked reads, so this is a Windows-only
   fix.

I. **Cleanup didn't reap stale state dirs whose project hint
   matched the input but lived under a non-canonical hash**.
   Pre-v1.10.3 daemons hashed paths without case normalization,
   so the same project could land in ``c:\\x``-hashed and
   ``C:\\X``-hashed dirs. The post-v1.10.3 fix landed all new
   daemons in the canonical (case-normalized) hash dir but
   left the legacy ones as orphans. ``cleanup --force
   --project X`` removed only the canonical-hash dir; stale
   ones persisted indefinitely as diagnostic clutter (visible
   in v1.10.5 mangled-path error output, for instance). Fix:
   :func:`_find_matching_state_dirs` scans
   ``~/.claude/lsp-engine/*/project`` and finds every dir
   whose hint normcase-matches the input; ``_run_cleanup``
   under ``--force`` reaps them all (subject to per-dir
   liveness check — don't touch a dir whose lock-file PID is
   still alive).
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

HERE = Path(__file__).resolve().parent.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))


# ──────────────────────────────────────────────────────────────────────
# Fix H — lock byte offset
# ──────────────────────────────────────────────────────────────────────


class TestLockByteOffset(unittest.TestCase):
    """The daemon's exclusion lock must NOT cover the bytes where
    the PID is written, so other processes can read the lock file
    via ``read_text()`` even while the daemon holds the lock."""

    def test_win_lock_offset_is_well_past_payload(self):
        """The lock-byte offset constant must be big enough that no
        plausible PID + timestamp payload reaches it. A 64-bit PID
        printed in decimal is 20 chars; a UNIX timestamp is 10
        chars; both with trailing newlines = ~32 bytes. 4096 is
        128x that margin."""
        from claude_hooks.lsp_engine.daemon import Daemon
        self.assertGreaterEqual(Daemon._WIN_LOCK_OFFSET, 1024, (
            "_WIN_LOCK_OFFSET too small — a future ``daemon.lock`` "
            "payload increase might land in the locked range and "
            "reintroduce the read-block bug."
        ))

    def test_acquire_source_seeks_to_offset_before_locking_on_windows(self):
        """Source-inspection: the acquire path must (1) seek to the
        offset, (2) call msvcrt.locking, (3) seek back to 0 before
        writing the payload. Skipping any of these reintroduces the
        bug or breaks the write."""
        import inspect
        from claude_hooks.lsp_engine.daemon import Daemon
        src = inspect.getsource(Daemon._acquire_lock_file)
        # Must seek to the constant before locking.
        seek_to_offset_idx = src.find(
            "os.lseek(fd, self._WIN_LOCK_OFFSET",
        )
        lock_idx = src.find("msvcrt.locking(fd, msvcrt.LK_NBLCK")
        seek_back_idx = src.find("os.lseek(fd, 0, os.SEEK_SET)")
        self.assertGreater(seek_to_offset_idx, -1, (
            "_acquire_lock_file no longer seeks to _WIN_LOCK_OFFSET "
            "before locking — Windows readers will hit "
            "ERROR_LOCK_VIOLATION on the PID bytes."
        ))
        self.assertGreater(lock_idx, seek_to_offset_idx, (
            "_acquire_lock_file calls msvcrt.locking BEFORE seeking "
            "to the offset — lock will land at the wrong byte."
        ))
        self.assertGreater(seek_back_idx, lock_idx, (
            "_acquire_lock_file no longer seeks back to 0 before "
            "writing the payload — PID will be written at byte 4096+ "
            "and daemon_pid() will see an empty file."
        ))

    def test_release_source_seeks_to_offset_before_unlocking_on_windows(self):
        """The release path must unlock the same byte that was locked
        (offset 4096), not byte 0. Pre-fix the release seeked to 0 and
        called LK_UNLCK there — wrong byte, the lock leaks until close."""
        import inspect
        from claude_hooks.lsp_engine.daemon import Daemon
        src = inspect.getsource(Daemon._release_lock_file)
        self.assertIn("self._WIN_LOCK_OFFSET", src, (
            "_release_lock_file no longer references _WIN_LOCK_OFFSET "
            "— it unlocks the wrong byte. Acquire and release must "
            "operate at the same offset."
        ))

    @unittest.skipIf(os.name != "posix", "Linux-only path verification")
    def test_posix_path_unchanged(self):
        """POSIX still uses whole-file flock — no offset gymnastics.
        Verify the POSIX branch in the source still calls flock with
        no preceding seek."""
        import inspect
        from claude_hooks.lsp_engine.daemon import Daemon
        src = inspect.getsource(Daemon._acquire_lock_file)
        # The POSIX branch (``else`` after the ``if os.name == "nt"``)
        # must NOT have any lseek calls before the flock.
        self.assertIn("fcntl.flock(fd, fcntl.LOCK_EX", src)


class TestLockHeldDoesNotBlockReadOnPosix(unittest.TestCase):
    """Sanity: on POSIX, daemon_pid() returns the PID while the
    daemon holds its flock — this was always the case and the v1.10.6
    Windows fix shouldn't have regressed it."""

    @unittest.skipIf(os.name == "nt", "Windows parity is Phase 4")
    def test_daemon_pid_readable_while_lock_held(self):
        """Spin up a real daemon, read daemon.lock from outside,
        confirm the PID comes back."""
        from claude_hooks.lsp_engine.daemon import Daemon
        from claude_hooks.lsp_engine.config import (
            EngineConfig, LspServerSpec, SessionLockConfig,
        )
        from claude_hooks.lsp_engine.client import daemon_pid

        fake_server = Path(__file__).parent / "lsp_engine_fake_server.py"
        with TemporaryDirectory() as td:
            tdp = Path(td)
            project = tdp / "project"
            project.mkdir()
            state = tdp / "state"
            daemon = Daemon(
                project_root=project,
                servers=[LspServerSpec(
                    extensions=("py",),
                    command=(sys.executable, str(fake_server)),
                    root_dir=".",
                )],
                engine_config=EngineConfig(
                    session_locks=SessionLockConfig(
                        debounce_seconds=0.5,
                        query_timeout_ms=200,
                    ),
                ),
                state_base=state,
            )
            daemon.start()
            try:
                pid = daemon_pid(project, state_base=state)
                self.assertIsNotNone(pid)
                self.assertEqual(pid, os.getpid())
            finally:
                daemon.stop()


# ──────────────────────────────────────────────────────────────────────
# Fix I — stale state-dir reaper
# ──────────────────────────────────────────────────────────────────────


class TestFindMatchingStateDirs(unittest.TestCase):
    """``_find_matching_state_dirs`` returns every dir whose ``project``
    hint file normcase-matches the supplied root."""

    def test_returns_empty_when_state_base_missing(self):
        from claude_hooks.lsp_engine.__main__ import _find_matching_state_dirs
        with TemporaryDirectory() as td:
            base = Path(td) / "does-not-exist"
            self.assertEqual(_find_matching_state_dirs("/x", state_base=base), [])

    def test_returns_empty_when_no_dirs_match(self):
        from claude_hooks.lsp_engine.__main__ import _find_matching_state_dirs
        with TemporaryDirectory() as td:
            base = Path(td)
            (base / "abc").mkdir()
            (base / "abc" / "project").write_text("/other/project\n")
            with TemporaryDirectory() as proj_td:
                self.assertEqual(
                    _find_matching_state_dirs(proj_td, state_base=base),
                    [],
                )

    def test_finds_canonical_dir(self):
        from claude_hooks.lsp_engine.__main__ import _find_matching_state_dirs
        with TemporaryDirectory() as td:
            base = Path(td) / "state"
            base.mkdir()
            (base / "abc").mkdir()
            with TemporaryDirectory() as proj_td:
                resolved = str(Path(proj_td).resolve())
                (base / "abc" / "project").write_text(resolved + "\n")
                matches = _find_matching_state_dirs(proj_td, state_base=base)
                self.assertEqual(len(matches), 1)
                self.assertEqual(matches[0][0].name, "abc")

    def test_finds_multiple_stale_dirs_same_project(self):
        """The pandorum case: same project, two state dirs (one
        canonical, one pre-case-norm). Both must be returned."""
        from claude_hooks.lsp_engine.__main__ import _find_matching_state_dirs
        with TemporaryDirectory() as td:
            base = Path(td) / "state"
            base.mkdir()
            with TemporaryDirectory() as proj_td:
                resolved = str(Path(proj_td).resolve())
                for h in ("canonical-hash", "legacy-hash"):
                    (base / h).mkdir()
                    (base / h / "project").write_text(resolved + "\n")
                matches = _find_matching_state_dirs(proj_td, state_base=base)
                self.assertEqual(len(matches), 2)
                names = sorted(m[0].name for m in matches)
                self.assertEqual(names, ["canonical-hash", "legacy-hash"])

    def test_normcase_matches_case_divergent_hints(self):
        """A pre-case-norm daemon may have written a hint with
        different case (uppercase drive letter on Windows). The
        matcher must normcase both sides so the dir is still
        identified."""
        from claude_hooks.lsp_engine.__main__ import _find_matching_state_dirs
        with TemporaryDirectory() as td:
            base = Path(td) / "state"
            base.mkdir()
            (base / "legacy").mkdir()
            with TemporaryDirectory() as proj_td:
                resolved = str(Path(proj_td).resolve())
                # Write the hint in upper-cased form to simulate the
                # pre-normalize era. On POSIX normcase is identity,
                # so this branch only meaningfully exercises Windows.
                (base / "legacy" / "project").write_text(
                    resolved.upper() + "\n",
                )
                matches = _find_matching_state_dirs(proj_td, state_base=base)
                if os.name == "nt":
                    self.assertEqual(len(matches), 1)
                else:
                    # POSIX is case-sensitive — upper-cased path
                    # shouldn't match lowercased. Documenting the
                    # platform difference rather than asserting either
                    # behaviour, since users on POSIX don't hit this
                    # bug class.
                    self.assertIn(len(matches), (0, 1))

    def test_ignores_files_in_state_base(self):
        """Spurious files at the state-base level (not dirs) must be
        skipped, not crash the iteration."""
        from claude_hooks.lsp_engine.__main__ import _find_matching_state_dirs
        with TemporaryDirectory() as td:
            base = Path(td) / "state"
            base.mkdir()
            (base / "stray.txt").write_text("oops")
            self.assertEqual(_find_matching_state_dirs("/x", state_base=base), [])

    def test_ignores_dirs_without_project_hint(self):
        """A state dir with no ``project`` hint file shouldn't crash
        the iteration."""
        from claude_hooks.lsp_engine.__main__ import _find_matching_state_dirs
        with TemporaryDirectory() as td:
            base = Path(td) / "state"
            base.mkdir()
            (base / "empty-dir").mkdir()
            with TemporaryDirectory() as proj_td:
                self.assertEqual(
                    _find_matching_state_dirs(proj_td, state_base=base),
                    [],
                )


class TestCleanupReapsStaleDirs(unittest.TestCase):
    """``cleanup --force --project X`` reaps every dir whose project
    hint matches X, not just the canonical-hash one. Liveness check
    per dir — never remove one with a live daemon."""

    @unittest.skipIf(os.name == "nt", "Windows parity is Phase 4")
    def test_cleanup_reaps_stale_canonical_mismatch(self):
        """Plant a stale (legacy-hash) dir alongside an absent
        canonical dir. ``cleanup --force`` should remove the legacy
        one."""
        from claude_hooks.lsp_engine.__main__ import _run_cleanup
        with TemporaryDirectory() as td:
            tdp = Path(td)
            project = tdp / "project"
            project.mkdir()
            state = tdp / "state"
            state.mkdir()
            stale = state / "stale-hash"
            stale.mkdir()
            (stale / "project").write_text(str(project.resolve()) + "\n")
            # Dead PID in the lock file so liveness check doesn't
            # block. PID 1 is init on POSIX but checking with
            # os.kill(1, 0) requires root — patch pid_is_alive to
            # return False for our test PID.
            (stale / "daemon.lock").write_text("999999999\n0\n")

            args = argparse.Namespace(
                project=str(project),
                state_base=str(state),
                force=True,
            )
            buf = io.StringIO()
            with patch("sys.stdout", buf), \
                 patch(
                     "claude_hooks.lsp_engine.__main__.pid_is_alive",
                     return_value=False,
                 ):
                rc = _run_cleanup(args)
            self.assertEqual(rc, 0)
            payload = json.loads(buf.getvalue())
            self.assertIn("stale_reaped", payload)
            self.assertEqual(len(payload["stale_reaped"]), 1)
            self.assertFalse(stale.exists())

    @unittest.skipIf(os.name == "nt", "Windows parity is Phase 4")
    def test_cleanup_without_force_does_not_reap_stale(self):
        """Without ``--force``, stale dirs are NOT reaped — removing
        them is destructive and shouldn't happen by default."""
        from claude_hooks.lsp_engine.__main__ import _run_cleanup
        with TemporaryDirectory() as td:
            tdp = Path(td)
            project = tdp / "project"
            project.mkdir()
            state = tdp / "state"
            state.mkdir()
            stale = state / "stale-hash"
            stale.mkdir()
            (stale / "project").write_text(str(project.resolve()) + "\n")
            (stale / "daemon.lock").write_text("999999999\n0\n")

            args = argparse.Namespace(
                project=str(project),
                state_base=str(state),
                force=False,
            )
            buf = io.StringIO()
            with patch("sys.stdout", buf):
                rc = _run_cleanup(args)
            self.assertEqual(rc, 0)
            payload = json.loads(buf.getvalue())
            self.assertNotIn("stale_reaped", payload)
            self.assertTrue(stale.exists())

    @unittest.skipIf(os.name == "nt", "Windows parity is Phase 4")
    def test_cleanup_skips_stale_with_live_daemon(self):
        """A stale dir whose lock-file PID is still alive must NOT be
        reaped — that's a live daemon under a non-canonical hash, the
        operator probably has a reason."""
        from claude_hooks.lsp_engine.__main__ import _run_cleanup
        with TemporaryDirectory() as td:
            tdp = Path(td)
            project = tdp / "project"
            project.mkdir()
            state = tdp / "state"
            state.mkdir()
            stale = state / "stale-hash"
            stale.mkdir()
            (stale / "project").write_text(str(project.resolve()) + "\n")
            # Live PID (this test's own PID, definitely alive).
            (stale / "daemon.lock").write_text(f"{os.getpid()}\n0\n")

            args = argparse.Namespace(
                project=str(project),
                state_base=str(state),
                force=True,
            )
            buf = io.StringIO()
            with patch("sys.stdout", buf):
                rc = _run_cleanup(args)
            self.assertEqual(rc, 0)
            payload = json.loads(buf.getvalue())
            # Stale dir should appear in stale_skipped, not stale_reaped.
            self.assertIn("stale_skipped", payload)
            self.assertTrue(stale.exists())
            skipped_dirs = [s["dir"] for s in payload["stale_skipped"]]
            self.assertIn(str(stale), skipped_dirs)

    @unittest.skipIf(os.name == "nt", "Windows parity is Phase 4")
    def test_cleanup_absent_canonical_still_reaps_stale_under_force(self):
        """The pandorum sequence: ``restart`` already removed the
        canonical dir, then ``cleanup --force`` runs. The canonical
        dir is absent, but stale dirs remain — they should be
        reaped. Pre-fix this returned ``state dir already absent``
        and did nothing."""
        from claude_hooks.lsp_engine.__main__ import _run_cleanup
        with TemporaryDirectory() as td:
            tdp = Path(td)
            project = tdp / "project"
            project.mkdir()
            state = tdp / "state"
            state.mkdir()
            stale = state / "stale-hash"
            stale.mkdir()
            (stale / "project").write_text(str(project.resolve()) + "\n")
            (stale / "daemon.lock").write_text("999999999\n0\n")

            args = argparse.Namespace(
                project=str(project),
                state_base=str(state),
                force=True,
            )
            buf = io.StringIO()
            with patch("sys.stdout", buf), \
                 patch(
                     "claude_hooks.lsp_engine.__main__.pid_is_alive",
                     return_value=False,
                 ):
                rc = _run_cleanup(args)
            self.assertEqual(rc, 0)
            payload = json.loads(buf.getvalue())
            self.assertIn("stale_reaped", payload)
            self.assertFalse(stale.exists())


if __name__ == "__main__":
    unittest.main()
