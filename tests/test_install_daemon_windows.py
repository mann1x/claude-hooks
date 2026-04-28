"""
Tests for ``install._install_daemon_windows`` and its helpers.

Covers the Windows daemon-install flow:

  1. If task exists → ask re-install (yes: delete + reinstall + verify;
     no: ping-verify the running daemon and report).
  2. If task absent → /Create (UAC) → /Run (UAC) → ping-verify with
     ``_wait_for_daemon``. Retry on /Create UAC declined or daemon
     non-responsive.

The helpers don't actually require Windows at runtime (they shell out
to subprocess), so these tests run on Linux too with subprocess.run +
``_wait_for_daemon`` patched.
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

# Capture the unpatched _wait_for_daemon function so the dedicated
# TestWaitForDaemon class below can exercise the real polling logic
# even though every other test patches it via the autouse fixture.
_REAL_WAIT_FOR_DAEMON = install._wait_for_daemon


@pytest.fixture(autouse=True)
def _ping_succeeds():
    """Stub _wait_for_daemon → True so tests don't actually poll TCP."""
    with patch("install._wait_for_daemon", return_value=True) as p:
        yield p


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
# _install_daemon_windows — task-already-exists branch
# ===================================================================== #
class TestAlreadyInstalledBranch:
    def test_already_exists_non_interactive_verifies_daemon(self, capsys):
        """In non-interactive mode + task present, leave as-is and just
        ping. Should not call schtasks at all."""
        with patch("install._windows_task_exists", return_value=True), \
             patch.object(install.subprocess, "run") as run:
            install._install_daemon_windows(non_interactive=True)
        run.assert_not_called()
        out = capsys.readouterr().out
        assert "already exists" in out
        assert "daemon responding" in out

    def test_already_exists_user_declines_reinstall_verifies(self, capsys):
        """Task present + user says no to reinstall → ping daemon."""
        with patch("install._windows_task_exists", return_value=True), \
             patch("builtins.input", return_value="n"), \
             patch.object(install.subprocess, "run") as run:
            install._install_daemon_windows(non_interactive=False)
        # Should NOT have called schtasks (no /Create, no /Delete, no /Run)
        run.assert_not_called()
        out = capsys.readouterr().out
        assert "already registered" in out or "already exists" in out
        assert "daemon responding" in out

    def test_already_exists_user_declines_daemon_unresponsive_warns(
        self, capsys, _ping_succeeds,
    ):
        """Task present + user says no + daemon NOT responding → guidance."""
        _ping_succeeds.return_value = False
        with patch("install._windows_task_exists", return_value=True), \
             patch("builtins.input", return_value="n"), \
             patch.object(install.subprocess, "run") as run:
            install._install_daemon_windows(non_interactive=False)
        run.assert_not_called()
        out = capsys.readouterr().out
        assert "not responding" in out
        # Suggest schtasks /Run as the recovery
        assert "/Run" in out

    def test_already_exists_user_accepts_reinstall_deletes_and_recreates(self):
        """Task present + user says yes → /Delete, then /Create + /Run."""
        # Existence sequence:
        #   - top-of-fn check (True, prompts for reinstall)
        #   - inside fresh-install loop: not _windows_task_exists check (False after delete)
        #   - after /Create: True
        existence = iter([True, False, True])
        with patch(
            "install._windows_task_exists",
            side_effect=lambda *_: next(existence),
        ), patch("install._is_windows_admin", return_value=True), \
             patch("builtins.input", side_effect=["y", "y"]), \
             patch.object(
                 install.subprocess, "run",
                 return_value=MagicMock(returncode=0, stderr=""),
             ) as run:
            install._install_daemon_windows(non_interactive=False)
        # Verify schtasks was called with /Delete, /Create, and /Run.
        verbs = []
        for call in run.call_args_list:
            cmdline = call[0][0]
            if cmdline and cmdline[0] == "schtasks":
                # First arg after "schtasks" is the verb.
                verbs.append(cmdline[1])
        assert "/Delete" in verbs
        assert "/Create" in verbs
        assert "/Run" in verbs


# ===================================================================== #
# _install_daemon_windows — fresh-install branch
# ===================================================================== #
class TestFreshInstallBranch:
    def test_non_interactive_prints_both_commands(self, capsys):
        with patch("install._windows_task_exists", return_value=False), \
             patch.object(install.subprocess, "run") as run:
            install._install_daemon_windows(non_interactive=True)
        run.assert_not_called()
        out = capsys.readouterr().out
        assert "/Create" in out
        assert "/Run" in out

    def test_user_declines_proceed_skips_with_manual_commands(self, capsys):
        with patch("install._windows_task_exists", return_value=False), \
             patch("builtins.input", return_value="n"), \
             patch.object(install.subprocess, "run") as run:
            install._install_daemon_windows(non_interactive=False)
        run.assert_not_called()
        out = capsys.readouterr().out
        assert "Skipped" in out
        assert "/Create" in out
        assert "/Run" in out

    def test_admin_path_calls_create_then_run_directly(self):
        """When elevated, /Create + /Run bypass PowerShell.

        Existence sequence (3 checks during a fresh successful install):
          1. top-of-fn already-exists check → False
          2. loop: if-not-exists check → False (will create)
          3. post-/Create exists check → True
        """
        existence = iter([False, False, True])
        with patch(
            "install._windows_task_exists",
            side_effect=lambda *_: next(existence),
        ), patch("install._is_windows_admin", return_value=True), \
             patch("install.find_conda_env_pythonw", return_value=None), \
             patch("builtins.input", return_value="y"), \
             patch.object(
                 install.subprocess, "run",
                 return_value=MagicMock(returncode=0, stderr=""),
             ) as run:
            install._install_daemon_windows(non_interactive=False)
        verbs = [
            call[0][0][1] for call in run.call_args_list
            if call[0][0] and call[0][0][0] == "schtasks"
        ]
        assert "/Create" in verbs
        assert "/Run" in verbs

    def test_create_uses_pythonw_when_available(self, tmp_path):
        """When pythonw.exe is found, the XML's <Command> points at it +
        the <Arguments> field has run_daemon.py."""
        fake_pyw = tmp_path / "pythonw.exe"
        fake_pyw.touch()
        existence = iter([False, False, True])
        captured = {}

        def _fake_xml(*, command, arguments, workdir):
            captured["command"] = command
            captured["arguments"] = arguments
            captured["workdir"] = workdir
            p = tmp_path / "fake-task.xml"
            p.write_text("<task/>", encoding="utf-8")
            return p

        with patch(
            "install._windows_task_exists",
            side_effect=lambda *_: next(existence),
        ), patch("install._is_windows_admin", return_value=True), \
             patch("install.find_conda_env_pythonw", return_value=fake_pyw), \
             patch("install._write_daemon_task_xml", side_effect=_fake_xml), \
             patch("builtins.input", return_value="y"), \
             patch.object(
                 install.subprocess, "run",
                 return_value=MagicMock(returncode=0, stderr=""),
             ) as run:
            install._install_daemon_windows(non_interactive=False)
        # XML was generated with pythonw.exe as command and run_daemon.py
        # as the argument.
        assert "pythonw.exe" in captured["command"]
        assert "run_daemon.py" in captured["arguments"]
        # schtasks /Create was invoked with /XML pointing at our fake file.
        create_calls = [
            call[0][0] for call in run.call_args_list
            if call[0][0] and call[0][0][0] == "schtasks"
            and call[0][0][1] == "/Create"
        ]
        assert create_calls, "no /Create schtasks call"
        assert "/XML" in create_calls[0]

    def test_create_falls_back_to_cmd_when_pythonw_missing(self, capsys, tmp_path):
        """No pythonw.exe → XML <Command> uses the .cmd shim and a warning prints."""
        existence = iter([False, False, True])
        captured = {}

        def _fake_xml(*, command, arguments, workdir):
            captured["command"] = command
            p = tmp_path / "fake-task.xml"
            p.write_text("<task/>", encoding="utf-8")
            return p

        with patch(
            "install._windows_task_exists",
            side_effect=lambda *_: next(existence),
        ), patch("install._is_windows_admin", return_value=True), \
             patch("install.find_conda_env_pythonw", return_value=None), \
             patch("install._write_daemon_task_xml", side_effect=_fake_xml), \
             patch("builtins.input", return_value="y"), \
             patch.object(
                 install.subprocess, "run",
                 return_value=MagicMock(returncode=0, stderr=""),
             ):
            install._install_daemon_windows(non_interactive=False)
        assert "claude-hooks-daemon.cmd" in captured["command"]
        out = capsys.readouterr().out
        assert "pythonw.exe not found" in out

    def test_non_admin_path_uses_powershell_runas_for_each_step(self):
        """Non-admin: each schtasks step routes through Start-Process RunAs."""
        existence = iter([False, False, True])
        with patch(
            "install._windows_task_exists",
            side_effect=lambda *_: next(existence),
        ), patch("install._is_windows_admin", return_value=False), \
             patch("builtins.input", return_value="y"), \
             patch.object(
                 install.subprocess, "run",
                 return_value=MagicMock(returncode=0, stderr=""),
             ) as run:
            install._install_daemon_windows(non_interactive=False)
        ps_bodies = [
            call[0][0][-1] for call in run.call_args_list
            if call[0][0] and call[0][0][0] == "powershell"
        ]
        # Two PowerShell calls (Create + Run); each via Start-Process RunAs.
        assert len(ps_bodies) == 2
        for body in ps_bodies:
            assert "Start-Process" in body
            assert "-Verb RunAs" in body
            assert "-Wait" in body
            assert "schtasks" in body
        # First contains /Create, second contains /Run.
        assert any("/Create" in b for b in ps_bodies)
        assert any("/Run" in b for b in ps_bodies)

    def test_uac_declined_creates_then_user_skips(self, capsys):
        """/Create elevation declined → task still absent → retry=n → exit."""
        with patch("install._windows_task_exists", return_value=False), \
             patch("install._is_windows_admin", return_value=False), \
             patch("builtins.input", side_effect=["y", "n"]), \
             patch.object(
                 install.subprocess, "run",
                 return_value=MagicMock(returncode=0, stderr=""),
             ):
            install._install_daemon_windows(non_interactive=False)
        out = capsys.readouterr().out
        assert "task not detected" in out

    def test_uac_declined_then_retry_succeeds(self):
        """First /Create fails, user retries, second /Create succeeds.

        Existence sequence (5 checks):
          1. top-of-fn already-exists → False
          2. loop iter 1: if-not-exists → False (create attempt #1)
          3. post-/Create #1 → False (UAC declined → retry path)
          4. loop iter 2: if-not-exists → False (create attempt #2)
          5. post-/Create #2 → True (success)
        """
        existence = iter([False, False, False, False, True])
        with patch(
            "install._windows_task_exists",
            side_effect=lambda *_: next(existence),
        ), patch("install._is_windows_admin", return_value=False), \
             patch("builtins.input", side_effect=["y", "y", "y"]), \
             patch.object(
                 install.subprocess, "run",
                 return_value=MagicMock(returncode=0, stderr=""),
             ) as run:
            install._install_daemon_windows(non_interactive=False)
        # Two /Create elevations were attempted.
        ps_create_bodies = [
            call[0][0][-1] for call in run.call_args_list
            if call[0][0] and call[0][0][0] == "powershell"
            and "/Create" in call[0][0][-1]
        ]
        assert len(ps_create_bodies) == 2

    def test_daemon_unresponsive_after_create_offers_retry(
        self, capsys, _ping_succeeds,
    ):
        """/Create + /Run succeeded but daemon not pinging → retry prompt.

        On retry-decline, we exit (no recreate), since the task already exists.
        """
        _ping_succeeds.return_value = False
        # Existence: top-of-fn=False, then True after /Create on every check.
        with patch(
            "install._windows_task_exists",
            side_effect=[False, True],
        ), patch("install._is_windows_admin", return_value=True), \
             patch("builtins.input", side_effect=["y", "n"]), \
             patch.object(
                 install.subprocess, "run",
                 return_value=MagicMock(returncode=0, stderr=""),
             ):
            install._install_daemon_windows(non_interactive=False)
        out = capsys.readouterr().out
        assert "did not respond" in out
        # User declined retry — function exits cleanly.

    def test_powershell_oserror_surfaces_then_retry_prompt(self, capsys):
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


# ===================================================================== #
# _write_daemon_task_xml — verify the power/timing overrides land in XML
# ===================================================================== #
class TestWriteDaemonTaskXml:
    def test_xml_disables_battery_restrictions(self):
        path = install._write_daemon_task_xml(
            command="C:\\py.exe", arguments="", workdir="C:\\repo",
        )
        try:
            content = path.read_bytes().decode("utf-16")
            assert "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>" in content
            assert "<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>" in content
        finally:
            path.unlink(missing_ok=True)

    def test_xml_removes_execution_time_limit(self):
        path = install._write_daemon_task_xml(
            command="C:\\py.exe", arguments="", workdir="C:\\repo",
        )
        try:
            content = path.read_bytes().decode("utf-16")
            # PT0S = no limit (the default would be PT72H).
            assert "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>" in content
        finally:
            path.unlink(missing_ok=True)

    def test_xml_embeds_command_arguments_workdir(self):
        path = install._write_daemon_task_xml(
            command="C:\\Users\\manni\\Miniconda3\\envs\\claude-hooks\\pythonw.exe",
            arguments='"C:\\Users\\manni\\claude-hooks\\run_daemon.py"',
            workdir="C:\\Users\\manni\\claude-hooks",
        )
        try:
            content = path.read_bytes().decode("utf-16")
            assert "pythonw.exe" in content
            assert "run_daemon.py" in content
            assert "claude-hooks" in content
            # XML escaping of double quotes around the runner path.
            assert "&quot;" in content
        finally:
            path.unlink(missing_ok=True)

    def test_xml_is_utf16_encoded(self):
        path = install._write_daemon_task_xml(
            command="x", arguments="", workdir="y",
        )
        try:
            raw = path.read_bytes()
            # UTF-16 LE BOM is 0xFF 0xFE.
            assert raw[:2] in (b"\xff\xfe", b"\xfe\xff")
        finally:
            path.unlink(missing_ok=True)


# ===================================================================== #
# _wait_for_daemon — uses the captured pre-patch reference so the
# autouse `_ping_succeeds` fixture above doesn't mask the real fn.
# ===================================================================== #
class TestWaitForDaemon:
    def test_returns_true_on_first_ping(self):
        with patch("claude_hooks.daemon_client.ping", return_value=True):
            assert _REAL_WAIT_FOR_DAEMON(timeout=2.0) is True

    def test_returns_false_on_timeout(self):
        with patch("claude_hooks.daemon_client.ping", return_value=False):
            assert _REAL_WAIT_FOR_DAEMON(timeout=0.5) is False

    def test_swallows_exceptions_during_polling(self):
        """A single ping raising should not abort the wait loop."""
        responses = iter([RuntimeError("transient"), True])

        def flaky(**kw):
            r = next(responses)
            if isinstance(r, Exception):
                raise r
            return r

        with patch("claude_hooks.daemon_client.ping", side_effect=flaky):
            assert _REAL_WAIT_FOR_DAEMON(timeout=3.0) is True
