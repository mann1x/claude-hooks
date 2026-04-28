"""Tests for run_daemon.py — the pythonw launcher.

Two surfaces to exercise:

1. ``_rotate_if_large`` — file-rotation guard. Pure file-ops, runs
   identically on every host. Covers below-threshold no-op, exact
   threshold, over-threshold rotation, missing-file no-op,
   stale-backup overwrite.
2. ``_setup_windows_log_redirect`` — the stdout/stderr swap. Risky to
   run on the actual sys.stdout/stderr so we monkeypatch ``os.name``
   to "nt" and a controlled run_daemon.sys to verify the assignment
   without redirecting the test runner.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import run_daemon  # noqa: E402


# ===================================================================== #
# _rotate_if_large
# ===================================================================== #
class TestRotateIfLarge:
    def test_below_threshold_does_not_rotate(self, tmp_path):
        f = tmp_path / "x.log"
        f.write_bytes(b"hi")
        run_daemon._rotate_if_large(f, max_bytes=1024)
        assert f.exists()
        assert not (tmp_path / "x.log.1").exists()

    def test_at_threshold_does_not_rotate(self, tmp_path):
        """``size <= max_bytes`` is the no-op branch — exact equality
        means we just fit, no need to shuffle files."""
        f = tmp_path / "x.log"
        f.write_bytes(b"a" * 100)
        run_daemon._rotate_if_large(f, max_bytes=100)
        assert f.exists()
        assert not (tmp_path / "x.log.1").exists()

    def test_above_threshold_rotates(self, tmp_path):
        f = tmp_path / "x.log"
        f.write_bytes(b"a" * 200)
        run_daemon._rotate_if_large(f, max_bytes=100)
        assert not f.exists()
        backup = tmp_path / "x.log.1"
        assert backup.exists()
        assert backup.read_bytes() == b"a" * 200

    def test_overwrites_existing_backup(self, tmp_path):
        """Stale .1 from a previous rotation gets clobbered — keeps
        rotation bounded at one prior generation."""
        f = tmp_path / "x.log"
        old_backup = tmp_path / "x.log.1"
        f.write_bytes(b"new" * 200)
        old_backup.write_bytes(b"OLD-BACKUP")
        run_daemon._rotate_if_large(f, max_bytes=100)
        assert not f.exists()
        assert old_backup.exists()
        assert old_backup.read_bytes() == b"new" * 200

    def test_missing_file_is_silent_noop(self, tmp_path):
        # No exception, no backup created. Daemon's first start.
        run_daemon._rotate_if_large(tmp_path / "nonexistent.log", max_bytes=100)
        assert list(tmp_path.iterdir()) == []

    def test_unlink_failure_does_not_block_rotation(self, tmp_path):
        """If we can't delete the stale .1, we silently move on — the
        rename below would just fail too, but neither raises."""
        f = tmp_path / "x.log"
        f.write_bytes(b"a" * 200)
        with patch.object(Path, "unlink", side_effect=OSError("boom")):
            # The rename should still succeed because Path.rename isn't
            # patched — but rename will fail because .1 still exists on
            # POSIX (Python's Path.rename allows overwrite on POSIX,
            # actually). The real assertion: nothing raises.
            run_daemon._rotate_if_large(f, max_bytes=100)


# ===================================================================== #
# _setup_windows_log_redirect
# ===================================================================== #
class TestSetupWindowsLogRedirect:
    def test_posix_is_noop(self, tmp_path, monkeypatch):
        """Linux/macOS rely on systemd / launchd for log capture. We
        must not touch their stdout/stderr — would compete with the
        platform logger and create double-buffering surprises."""
        monkeypatch.setattr(run_daemon.os, "name", "posix")
        # Use a sentinel to detect any swap.
        sentinel_out = SimpleNamespace(name="orig-stdout")
        sentinel_err = SimpleNamespace(name="orig-stderr")
        monkeypatch.setattr(run_daemon.sys, "stdout", sentinel_out)
        monkeypatch.setattr(run_daemon.sys, "stderr", sentinel_err)
        run_daemon._setup_windows_log_redirect(tmp_path / "x.log")
        assert run_daemon.sys.stdout is sentinel_out
        assert run_daemon.sys.stderr is sentinel_err
        assert not (tmp_path / "x.log").exists()

    def test_windows_creates_parent_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(run_daemon.os, "name", "nt")
        # Capture the swap so the test runner's stdout isn't redirected.
        captured = {}

        class _FakeSys:
            stdout = sys.__stdout__
            stderr = sys.__stderr__

        fake = _FakeSys()
        monkeypatch.setattr(run_daemon, "sys", fake)

        target = tmp_path / "deeper" / "log" / "claude-hooks-daemon.log"
        run_daemon._setup_windows_log_redirect(target)
        assert target.parent.is_dir()
        assert target.exists()
        assert fake.stdout is fake.stderr  # both point at the file
        assert hasattr(fake.stdout, "write")
        # Clean up — close the file so tmp_path teardown can remove it
        # on Windows runners (open files block deletion there).
        try:
            fake.stdout.close()
        except Exception:
            pass

    def test_windows_appends_existing_log(self, tmp_path, monkeypatch):
        """Restarting the daemon must not nuke prior logs — append, not
        overwrite. (Rotation happens only when the file crosses the
        size threshold.)"""
        monkeypatch.setattr(run_daemon.os, "name", "nt")
        target = tmp_path / "claude-hooks-daemon.log"
        target.write_text("prior session line\n", encoding="utf-8")

        class _FakeSys:
            stdout = sys.__stdout__
            stderr = sys.__stderr__

        fake = _FakeSys()
        monkeypatch.setattr(run_daemon, "sys", fake)

        run_daemon._setup_windows_log_redirect(target)
        fake.stdout.write("next session line\n")
        fake.stdout.flush()
        try:
            fake.stdout.close()
        except Exception:
            pass

        body = target.read_text(encoding="utf-8")
        assert "prior session line" in body
        assert "next session line" in body

    def test_windows_rotation_kicks_in_on_oversize(self, tmp_path, monkeypatch):
        monkeypatch.setattr(run_daemon.os, "name", "nt")
        target = tmp_path / "claude-hooks-daemon.log"
        # Pre-fill above the threshold we'll pass in.
        target.write_bytes(b"X" * 1024)

        class _FakeSys:
            stdout = sys.__stdout__
            stderr = sys.__stderr__

        fake = _FakeSys()
        monkeypatch.setattr(run_daemon, "sys", fake)

        run_daemon._setup_windows_log_redirect(target, max_bytes=512)

        # Old content moved to .1, new file empty (open mode "a" creates).
        backup = tmp_path / "claude-hooks-daemon.log.1"
        assert backup.exists()
        assert backup.read_bytes() == b"X" * 1024
        # The fresh file is empty after the rotation but still open.
        assert target.exists()
        try:
            fake.stdout.close()
        except Exception:
            pass

    def test_windows_dir_creation_failure_is_silent(self, tmp_path, monkeypatch):
        """If we can't even create the parent dir we must NOT crash —
        the daemon has to come up. Logging is best-effort."""
        monkeypatch.setattr(run_daemon.os, "name", "nt")

        class _FakeSys:
            stdout = sys.__stdout__
            stderr = sys.__stderr__

        fake = _FakeSys()
        original_stdout = fake.stdout
        monkeypatch.setattr(run_daemon, "sys", fake)

        with patch.object(Path, "mkdir", side_effect=OSError("permission denied")):
            run_daemon._setup_windows_log_redirect(tmp_path / "claude-hooks-daemon.log")

        # No swap happened — stdout still points at the original.
        assert fake.stdout is original_stdout

    def test_windows_open_failure_is_silent(self, tmp_path, monkeypatch):
        monkeypatch.setattr(run_daemon.os, "name", "nt")

        class _FakeSys:
            stdout = sys.__stdout__
            stderr = sys.__stderr__

        fake = _FakeSys()
        original_stdout = fake.stdout
        monkeypatch.setattr(run_daemon, "sys", fake)

        # Mock the builtin used inside _setup_windows_log_redirect.
        with patch("builtins.open", side_effect=OSError("disk full")):
            run_daemon._setup_windows_log_redirect(tmp_path / "claude-hooks-daemon.log")

        assert fake.stdout is original_stdout
