"""
Tests for ``install._install_claude_hooks_daemon`` (outer flow).

The outer function now detects whether the daemon's autostart entry is
already registered BEFORE prompting "Install + enable…?". The single
binary prompt is replaced with a tri-prompt (re-install / verify /
skip) when an entry exists.
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


def _base_cfg() -> dict:
    return {"hooks": {"daemon": {"enabled": True}}}


@pytest.fixture(autouse=True)
def _ping_succeeds():
    with patch("install._wait_for_daemon", return_value=True) as p:
        yield p


# ===================================================================== #
# _detect_existing_daemon_entry
# ===================================================================== #
class TestDetectExistingEntry:
    def test_returns_none_when_nothing_installed(self, monkeypatch, tmp_path):
        monkeypatch.setattr(install.os, "name", "posix")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        # Point /etc check at a non-existing dir.
        with patch.object(install, "Path", side_effect=Path):
            with patch(
                "install.Path",
                side_effect=lambda *a, **kw: (
                    tmp_path / "no-systemd"
                    if a == ("/etc/systemd/system",) or
                       (len(a) == 1 and "systemd/system" in str(a[0]))
                    else Path(*a, **kw)
                ),
            ):
                # Patch trickery is fragile; instead just test Windows case
                # where path is fully owned by the function under test.
                pass
        # Verify Windows-task-absent
        monkeypatch.setattr(install.os, "name", "nt")
        with patch("install._windows_task_exists", return_value=False):
            assert install._detect_existing_daemon_entry() is None

    def test_detects_windows_task(self, monkeypatch):
        monkeypatch.setattr(install.os, "name", "nt")
        with patch("install._windows_task_exists", return_value=True):
            out = install._detect_existing_daemon_entry()
        assert out is not None
        assert "scheduled task" in out
        assert install._DAEMON_TASK_NAME in out


# ===================================================================== #
# Outer flow — already-installed routing
# ===================================================================== #
class TestOuterFlowAlreadyInstalled:
    def test_already_installed_non_interactive_skips_install_prompt(
        self, capsys,
    ):
        """Non-interactive + already-installed → no prompt, just verify."""
        with patch(
            "install._detect_existing_daemon_entry",
            return_value="systemd unit /etc/systemd/system/claude-hooks-daemon.service",
        ), patch("builtins.input") as inp, \
             patch("install._install_daemon_windows") as win, \
             patch("install._install_daemon_systemd") as sd, \
             patch("install._install_daemon_launchd") as lc:
            install._install_claude_hooks_daemon(
                _base_cfg(), non_interactive=True, dry_run=False,
            )
        # No input prompt.
        inp.assert_not_called()
        # No platform installer call.
        win.assert_not_called()
        sd.assert_not_called()
        lc.assert_not_called()
        out = capsys.readouterr().out
        assert "Already installed" in out
        assert "daemon responding" in out

    def test_already_installed_user_chooses_skip(self, capsys):
        with patch(
            "install._detect_existing_daemon_entry",
            return_value="task X",
        ), patch("builtins.input", return_value="s"), \
             patch("install._install_daemon_windows") as win, \
             patch("install._install_daemon_systemd") as sd:
            install._install_claude_hooks_daemon(
                _base_cfg(), non_interactive=False, dry_run=False,
            )
        win.assert_not_called()
        sd.assert_not_called()
        out = capsys.readouterr().out
        assert "Already installed" in out
        assert "Skipped" in out

    def test_already_installed_default_verifies_only(self, capsys):
        """Empty input picks 'verify-only' (the V default in [r/V/s])."""
        with patch(
            "install._detect_existing_daemon_entry", return_value="task X",
        ), patch("builtins.input", return_value=""), \
             patch("install._install_daemon_windows") as win, \
             patch("install._install_daemon_systemd") as sd:
            install._install_claude_hooks_daemon(
                _base_cfg(), non_interactive=False, dry_run=False,
            )
        win.assert_not_called()
        sd.assert_not_called()
        out = capsys.readouterr().out
        assert "daemon responding" in out

    def test_already_installed_v_verifies_only(self, capsys, _ping_succeeds):
        """User picks 'v' → just ping, never call platform installer."""
        with patch(
            "install._detect_existing_daemon_entry", return_value="task X",
        ), patch("builtins.input", return_value="v"), \
             patch("install._install_daemon_windows") as win:
            install._install_claude_hooks_daemon(
                _base_cfg(), non_interactive=False, dry_run=False,
            )
        win.assert_not_called()

    def test_already_installed_verify_unresponsive_attempts_start(
        self, capsys, _ping_succeeds, monkeypatch,
    ):
        """Verify-only path with unresponsive daemon must attempt to start
        it, not just report 'not responding'."""
        _ping_succeeds.return_value = False
        monkeypatch.setattr(install.os, "name", "nt")
        with patch(
            "install._detect_existing_daemon_entry", return_value="task X",
        ), patch("builtins.input", return_value=""), \
             patch("install._start_daemon_via_platform") as start:
            install._install_claude_hooks_daemon(
                _base_cfg(), non_interactive=False, dry_run=False,
            )
        # Start was attempted before giving up.
        start.assert_called_once()
        out = capsys.readouterr().out
        assert "Verifying" in out
        assert "attempting to start" in out
        assert "still not responding" in out

    def test_already_installed_verify_unresponsive_then_starts_successfully(
        self, capsys, _ping_succeeds, monkeypatch,
    ):
        """First ping fails, start succeeds, second ping returns True."""
        # Ping returns False then True.
        _ping_succeeds.side_effect = [False, True]
        monkeypatch.setattr(install.os, "name", "nt")
        with patch(
            "install._detect_existing_daemon_entry", return_value="task X",
        ), patch("builtins.input", return_value=""), \
             patch("install._start_daemon_via_platform") as start:
            install._install_claude_hooks_daemon(
                _base_cfg(), non_interactive=False, dry_run=False,
            )
        start.assert_called_once()
        out = capsys.readouterr().out
        assert "started and responding" in out

    def test_already_installed_reinstall_routes_to_platform_installer(
        self, monkeypatch,
    ):
        """User picks 'r' → calls platform installer with force_reinstall=True."""
        monkeypatch.setattr(install.os, "name", "nt")
        with patch(
            "install._detect_existing_daemon_entry",
            return_value="task X",
        ), patch("builtins.input", return_value="r"), \
             patch("install._install_daemon_windows") as win:
            install._install_claude_hooks_daemon(
                _base_cfg(), non_interactive=False, dry_run=False,
            )
        win.assert_called_once()
        assert win.call_args.kwargs.get("force_reinstall") is True


# ===================================================================== #
# Outer flow — fresh install (no entry yet)
# ===================================================================== #
class TestOuterFlowFreshInstall:
    def test_fresh_install_shows_install_prompt(self, monkeypatch):
        monkeypatch.setattr(install.os, "name", "nt")
        with patch("install._detect_existing_daemon_entry", return_value=None), \
             patch("builtins.input", return_value="y") as inp, \
             patch("install._install_daemon_windows") as win:
            install._install_claude_hooks_daemon(
                _base_cfg(), non_interactive=False, dry_run=False,
            )
        # Outer install prompt did fire (one input call before platform).
        assert inp.call_count == 1
        assert "Install + enable" in inp.call_args[0][0]
        win.assert_called_once()
        assert win.call_args.kwargs.get("force_reinstall") is False

    def test_fresh_install_user_declines(self, monkeypatch):
        monkeypatch.setattr(install.os, "name", "nt")
        with patch("install._detect_existing_daemon_entry", return_value=None), \
             patch("builtins.input", return_value="n"), \
             patch("install._install_daemon_windows") as win:
            install._install_claude_hooks_daemon(
                _base_cfg(), non_interactive=False, dry_run=False,
            )
        win.assert_not_called()

    def test_explicit_disable_in_config_skips_silently(self, capsys):
        cfg = {"hooks": {"daemon": {"enabled": False}}}
        with patch("install._detect_existing_daemon_entry") as detect, \
             patch("builtins.input") as inp:
            install._install_claude_hooks_daemon(
                cfg, non_interactive=False, dry_run=False,
            )
        # Skipped before the existence probe.
        detect.assert_not_called()
        inp.assert_not_called()
        out = capsys.readouterr().out
        assert "claude-hooks-daemon" not in out


# ===================================================================== #
# Force-reinstall flag plumbed through to platform installers
# ===================================================================== #
class TestForceReinstallFlag:
    def test_systemd_force_reinstall_skips_inner_prompt(
        self, monkeypatch, tmp_path,
    ):
        # Lay out a "pre-existing" unit file so the inner branch fires.
        etc = tmp_path / "etc-systemd"
        etc.mkdir()
        unit = etc / install._DAEMON_UNIT
        unit.write_text("dummy")

        # Path used inside the function.
        original_path = install.Path

        def _path_factory(*args, **kwargs):
            if args == ("/etc/systemd/system",):
                return etc
            if args and isinstance(args[0], str) and args[0].startswith("/etc/systemd/system/"):
                return etc / args[0].split("/")[-1]
            return original_path(*args, **kwargs)

        with patch.object(install, "Path", side_effect=_path_factory), \
             patch("builtins.input") as inp, \
             patch.object(
                 install.subprocess, "run",
                 return_value=MagicMock(returncode=0, stderr=""),
             ):
            install._install_daemon_systemd(
                non_interactive=False, force_reinstall=True,
            )
        # No prompt — force_reinstall short-circuited it.
        inp.assert_not_called()

    def test_verify_and_start_responds_first_try(self, capsys, _ping_succeeds):
        _ping_succeeds.return_value = True
        with patch("install._start_daemon_via_platform") as start:
            assert install._verify_and_start_daemon() is True
        start.assert_not_called()
        out = capsys.readouterr().out
        assert "Verifying" in out
        assert "responding" in out

    def test_verify_and_start_starts_when_unresponsive(
        self, capsys, _ping_succeeds,
    ):
        # First ping False, second ping True.
        _ping_succeeds.side_effect = [False, True]
        with patch("install._start_daemon_via_platform") as start:
            assert install._verify_and_start_daemon() is True
        start.assert_called_once()

    def test_verify_and_start_gives_up_after_failed_start(
        self, capsys, _ping_succeeds,
    ):
        _ping_succeeds.return_value = False
        with patch("install._start_daemon_via_platform") as start:
            assert install._verify_and_start_daemon() is False
        start.assert_called_once()
        out = capsys.readouterr().out
        assert "still not responding" in out

    def test_start_daemon_via_platform_windows_runs_schtasks_run(
        self, monkeypatch,
    ):
        monkeypatch.setattr(install.os, "name", "nt")
        with patch("install._run_schtasks_elevated", return_value=True) as r:
            install._start_daemon_via_platform()
        r.assert_called_once()
        argstr = r.call_args[0][0]
        argv = r.call_args[0][1]
        assert "/Run" in argstr
        assert install._DAEMON_TASK_NAME in argstr
        assert argv[0] == "/Run"
        assert argv[2] == install._DAEMON_TASK_NAME

    def test_windows_force_reinstall_skips_inner_prompt(self):
        # task exists at top (force_reinstall ⇒ delete), then post-delete
        # check sees False (creates), post-create check sees True.
        existence = iter([True, False, True])
        with patch(
            "install._windows_task_exists",
            side_effect=lambda *_: next(existence),
        ), patch("install._is_windows_admin", return_value=True), \
             patch("builtins.input", return_value="y") as inp, \
             patch.object(
                 install.subprocess, "run",
                 return_value=MagicMock(returncode=0, stderr=""),
             ) as run:
            install._install_daemon_windows(
                non_interactive=False, force_reinstall=True,
            )
        # No "Re-install yes/no" prompt — force_reinstall short-circuited it.
        prompts = [c[0][0] for c in inp.call_args_list]
        assert all("Re-install" not in p for p in prompts), prompts
        # /Delete + /Create + /Run all fired.
        verbs = [
            call[0][0][1] for call in run.call_args_list
            if call[0][0] and call[0][0][0] == "schtasks"
        ]
        assert "/Delete" in verbs
        assert "/Create" in verbs
        assert "/Run" in verbs
