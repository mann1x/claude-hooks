"""
Tests for ``install._install_daemon_windows`` and its helpers.

Covers the Windows daemon-install flow: prompt → elevated schtasks via
PowerShell ``Start-Process -Verb RunAs`` (or direct schtasks when
already admin) → verify with ``schtasks /Query`` → retry-or-skip.

The helpers don't actually require Windows at runtime (they shell out
to subprocess), so these tests run on Linux too with subprocess.run
patched.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import install  # noqa: E402


# ===================================================================== #
# _windows_task_exists
# ===================================================================== #
class TestWindowsTaskExists:
    def test_returns_true_when_schtasks_returns_zero(self):
        with patch.object(
            install.subprocess, "run",
            return_value=MagicMock(returncode=0, stderr=""),
        ):
            assert install._windows_task_exists("foo") is True

    def test_returns_false_when_schtasks_nonzero(self):
        with patch.object(
            install.subprocess, "run",
            return_value=MagicMock(returncode=1, stderr="ERROR: not found"),
        ):
            assert install._windows_task_exists("foo") is False

    def test_returns_false_on_oserror(self):
        with patch.object(
            install.subprocess, "run", side_effect=OSError("schtasks missing"),
        ):
            assert install._windows_task_exists("foo") is False


# ===================================================================== #
# _install_daemon_windows — flow logic
# ===================================================================== #
class TestInstallDaemonWindows:
    def test_skips_when_task_already_exists(self, capsys):
        with patch("install._windows_task_exists", return_value=True), \
             patch.object(install.subprocess, "run") as run:
            install._install_daemon_windows(non_interactive=True)
        run.assert_not_called()
        out = capsys.readouterr().out
        assert "already exists" in out

    def test_non_interactive_prints_command_skips_run(self, capsys):
        # Task absent, non-interactive → must print the manual schtasks
        # invocation and not spawn anything.
        with patch("install._windows_task_exists", return_value=False), \
             patch.object(install.subprocess, "run") as run:
            install._install_daemon_windows(non_interactive=True)
        run.assert_not_called()
        out = capsys.readouterr().out
        assert "schtasks /Create /SC ONLOGON /TN" in out
        assert "claude-hooks-daemon" in out

    def test_user_declines_skips_with_manual_command(self, capsys):
        with patch("install._windows_task_exists", return_value=False), \
             patch("builtins.input", return_value="n"), \
             patch.object(install.subprocess, "run") as run:
            install._install_daemon_windows(non_interactive=False)
        run.assert_not_called()
        out = capsys.readouterr().out
        assert "Skipped" in out
        assert "schtasks /Create /SC ONLOGON" in out

    def test_admin_path_calls_schtasks_directly(self):
        """When already elevated, skip the PowerShell Start-Process dance."""
        # Sequence: task absent, user accepts, schtasks succeeds, task exists.
        exists_calls = iter([False, True])
        with patch(
            "install._windows_task_exists",
            side_effect=lambda *_: next(exists_calls),
        ), patch("install._is_windows_admin", return_value=True), \
             patch("builtins.input", return_value="y"), \
             patch.object(
                 install.subprocess, "run",
                 return_value=MagicMock(returncode=0, stderr=""),
             ) as run:
            install._install_daemon_windows(non_interactive=False)
        # First and only subprocess.run call invokes schtasks directly.
        cmdline = run.call_args[0][0]
        assert cmdline[0] == "schtasks"
        assert "/Create" in cmdline
        assert "/SC" in cmdline and "ONLOGON" in cmdline
        assert "/RL" in cmdline and "HIGHEST" in cmdline

    def test_non_admin_path_uses_powershell_runas(self):
        """When not elevated, route through PowerShell Start-Process -Verb RunAs."""
        exists_calls = iter([False, True])
        with patch(
            "install._windows_task_exists",
            side_effect=lambda *_: next(exists_calls),
        ), patch("install._is_windows_admin", return_value=False), \
             patch("builtins.input", return_value="y"), \
             patch.object(
                 install.subprocess, "run",
                 return_value=MagicMock(returncode=0, stderr=""),
             ) as run:
            install._install_daemon_windows(non_interactive=False)
        cmdline = run.call_args[0][0]
        assert cmdline[0] == "powershell"
        assert "-NoProfile" in cmdline
        # The PS command must contain Start-Process and -Verb RunAs.
        ps_body = cmdline[-1]
        assert "Start-Process" in ps_body
        assert "-Verb RunAs" in ps_body
        assert "-Wait" in ps_body
        assert "schtasks" in ps_body

    def test_uac_declined_then_user_skips(self, capsys):
        """Elevation 'fails' (task still absent), user declines retry."""
        # Task absent both before and after → simulates UAC declined.
        with patch("install._windows_task_exists", return_value=False), \
             patch("install._is_windows_admin", return_value=False), \
             patch("builtins.input", side_effect=["y", "n"]), \
             patch.object(
                 install.subprocess, "run",
                 return_value=MagicMock(returncode=0, stderr=""),
             ):
            install._install_daemon_windows(non_interactive=False)
        out = capsys.readouterr().out
        assert "Task not detected" in out
        assert "Skipped" in out

    def test_uac_declined_then_retry_succeeds(self):
        """First elevation fails (task missing), user retries, second works.

        Existence sequence: top-of-fn check (False), after attempt #1
        (False, retry), after attempt #2 (True, succeed).
        Inputs: proceed#1=y, retry=y, proceed#2=y.
        """
        existence = iter([False, False, True])
        ps_calls = []

        def _run_side(cmdline, **kw):
            ps_calls.append(cmdline)
            return MagicMock(returncode=0, stderr="")

        with patch(
            "install._windows_task_exists",
            side_effect=lambda *_: next(existence),
        ), patch("install._is_windows_admin", return_value=False), \
             patch("builtins.input", side_effect=["y", "y", "y"]), \
             patch.object(install.subprocess, "run", side_effect=_run_side):
            install._install_daemon_windows(non_interactive=False)
        # Two PowerShell elevations attempted.
        assert len(ps_calls) == 2
        for c in ps_calls:
            assert c[0] == "powershell"

    def test_powershell_oserror_falls_through_to_retry_prompt(self, capsys):
        """If PowerShell can't be launched at all, we surface the error and
        proceed to the retry prompt (user can decline)."""
        with patch("install._windows_task_exists", return_value=False), \
             patch("install._is_windows_admin", return_value=False), \
             patch("builtins.input", side_effect=["y", "n"]), \
             patch.object(
                 install.subprocess, "run",
                 side_effect=OSError("powershell not found"),
             ):
            install._install_daemon_windows(non_interactive=False)
        out = capsys.readouterr().out
        assert "Failed to launch elevated process" in out
        assert "Skipped" in out
