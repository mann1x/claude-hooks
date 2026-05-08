"""Tests for the consultants-engine Windows + macOS install paths.

Covers ``_install_consultants_windows`` (always-on + smart-start),
``_register_consultants_task`` (the task-registration helper that
both modes share), the launchd path, and the HTTP health-check
helper. All shell-outs and HTTP polls are mocked so the tests run
on Linux.
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


@pytest.fixture(autouse=True)
def _health_succeeds():
    """Default: HTTP health check returns True. Tests can override."""
    with patch("install._wait_for_consultants_health",
               return_value=True) as p:
        yield p


@pytest.fixture
def fake_consultants_py(tmp_path: Path) -> Path:
    p = tmp_path / "envs" / "claude-hooks-consultants" / "python.exe"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("")
    return p


# ----------------------- _wait_for_consultants_health ------------ #

class TestWaitForHealth:
    """Exercise the real polling loop with mocked urlopen."""

    def test_returns_true_on_first_success(self, monkeypatch):
        # Patch out the autouse override.
        monkeypatch.setattr(install, "_wait_for_consultants_health",
                            install._wait_for_consultants_health.__wrapped__
                            if hasattr(install._wait_for_consultants_health,
                                       "__wrapped__")
                            else install.__dict__["_wait_for_consultants_health"])
        # Re-import the real symbol to bypass the autouse mock.
        from importlib import reload
        reload(install)

        class _Resp:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): pass
        with patch("urllib.request.urlopen", return_value=_Resp()):
            assert install._wait_for_consultants_health(
                38095, timeout=2.0) is True

    def test_returns_false_on_timeout(self, monkeypatch):
        from importlib import reload
        reload(install)
        import urllib.error
        with patch("urllib.request.urlopen",
                   side_effect=urllib.error.URLError("nope")):
            assert install._wait_for_consultants_health(
                38095, timeout=0.3) is False


# ----------------------- _write_consultants_task_xml ----------- #

class TestWriteTaskXML:
    def test_writes_utf16_with_substitutions(self, tmp_path, monkeypatch):
        monkeypatch.setattr(install, "_windows_user_id",
                            lambda: "S-1-5-21-test")
        # Use the real tempfile under tmp_path so we can read the
        # output back; tempfile is imported inside the function so
        # we set TMPDIR to redirect rather than patching the import.
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        path = install._write_consultants_task_xml(
            description="my desc",
            command=r"C:\python.exe",
            arguments="-m consultants.server",
            workdir=r"C:\repo",
            prefix="test-",
        )
        try:
            data = path.read_bytes()
            # UTF-16 BOM check (LE = ff fe).
            assert data[:2] in (b"\xff\xfe", b"\xfe\xff")
            decoded = data.decode("utf-16")
            assert "my desc" in decoded
            assert "S-1-5-21-test" in decoded
            assert "consultants.server" in decoded
        finally:
            path.unlink()


# ----------------------- _register_consultants_task ------------- #

class TestRegisterTaskAlreadyExists:
    def test_non_interactive_verify_only(self):
        with patch("install._windows_task_exists", return_value=True):
            ok = install._register_consultants_task(
                task_name="claude-hooks-consultants",
                description="x", exec_command=r"C:\py.exe",
                exec_arguments="-m consultants.server",
                workdir=r"C:\repo", port=38095,
                health_timeout=5.0, non_interactive=True,
            )
        assert ok is True

    def test_non_interactive_health_fails(self):
        with patch("install._windows_task_exists", return_value=True), \
             patch("install._wait_for_consultants_health",
                   return_value=False):
            ok = install._register_consultants_task(
                task_name="claude-hooks-consultants",
                description="x", exec_command="cmd",
                exec_arguments="", workdir=".",
                port=38095, health_timeout=5.0,
                non_interactive=True,
            )
        assert ok is False

    def test_interactive_user_skips_reinstall(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda _: "n")
        with patch("install._windows_task_exists", return_value=True):
            ok = install._register_consultants_task(
                task_name="t", description="d", exec_command="c",
                exec_arguments="a", workdir=".", port=38095,
                health_timeout=2.0, non_interactive=False,
            )
        assert ok is True  # because health check stub returns True

    def test_interactive_user_reinstalls(self, monkeypatch, tmp_path):
        # First _windows_task_exists call returns True (existing),
        # second returns True (after /Create succeeded).
        existence = iter([True, True])
        monkeypatch.setattr("builtins.input", lambda _: "y")
        with patch("install._windows_task_exists",
                   side_effect=lambda *a, **k: next(existence)), \
             patch("install._run_schtasks_elevated",
                   return_value=True) as elev, \
             patch("install._write_consultants_task_xml",
                   return_value=tmp_path / "fake.xml") as xml_w:
            (tmp_path / "fake.xml").write_text("")
            ok = install._register_consultants_task(
                task_name="t", description="d", exec_command="c",
                exec_arguments="a", workdir=".", port=38095,
                health_timeout=2.0, non_interactive=False,
            )
        assert ok is True
        # Delete + Create + Run
        assert elev.call_count >= 2


class TestRegisterTaskFreshInstall:
    def test_non_interactive_prints_commands_and_returns_false(
            self, capsys, tmp_path):
        with patch("install._windows_task_exists", return_value=False), \
             patch("install._write_consultants_task_xml",
                   return_value=tmp_path / "fake.xml"):
            (tmp_path / "fake.xml").write_text("")
            ok = install._register_consultants_task(
                task_name="t", description="d", exec_command="c",
                exec_arguments="a", workdir=".", port=38095,
                health_timeout=2.0, non_interactive=True,
            )
        assert ok is False
        out = capsys.readouterr().out
        assert "schtasks" in out

    def test_interactive_create_run_verify(self, monkeypatch, tmp_path):
        monkeypatch.setattr("builtins.input", lambda _: "y")
        # task_exists: False (first), True (after create), True (post-run)
        existence = iter([False, True, True])
        with patch("install._windows_task_exists",
                   side_effect=lambda *a, **k: next(existence)), \
             patch("install._run_schtasks_elevated",
                   return_value=True) as elev, \
             patch("install._write_consultants_task_xml",
                   return_value=tmp_path / "fake.xml"):
            (tmp_path / "fake.xml").write_text("")
            ok = install._register_consultants_task(
                task_name="t", description="d", exec_command="c",
                exec_arguments="a", workdir=".", port=38095,
                health_timeout=2.0, non_interactive=False,
            )
        assert ok is True
        # Create + Run = 2 schtasks calls
        assert elev.call_count == 2

    def test_uac_declined_returns_false(self, monkeypatch, tmp_path):
        monkeypatch.setattr("builtins.input", lambda _: "y")
        with patch("install._windows_task_exists", return_value=False), \
             patch("install._run_schtasks_elevated",
                   return_value=False), \
             patch("install._write_consultants_task_xml",
                   return_value=tmp_path / "fake.xml"):
            (tmp_path / "fake.xml").write_text("")
            ok = install._register_consultants_task(
                task_name="t", description="d", exec_command="c",
                exec_arguments="a", workdir=".", port=38095,
                health_timeout=2.0, non_interactive=False,
            )
        assert ok is False

    def test_health_failure_returns_false(self, monkeypatch, tmp_path):
        monkeypatch.setattr("builtins.input", lambda _: "y")
        existence = iter([False, True, True])
        with patch("install._windows_task_exists",
                   side_effect=lambda *a, **k: next(existence)), \
             patch("install._run_schtasks_elevated", return_value=True), \
             patch("install._wait_for_consultants_health",
                   return_value=False), \
             patch("install._write_consultants_task_xml",
                   return_value=tmp_path / "fake.xml"):
            (tmp_path / "fake.xml").write_text("")
            ok = install._register_consultants_task(
                task_name="t", description="d", exec_command="c",
                exec_arguments="a", workdir=".", port=38095,
                health_timeout=2.0, non_interactive=False,
            )
        assert ok is False


# ----------------------- _install_consultants_windows ----------- #

class TestInstallConsultantsWindowsAlwaysOn:
    def test_dispatches_to_engine_task(self, fake_consultants_py):
        # Stub the pythonw lookup so we get a deterministic exec_command.
        pyw = fake_consultants_py.parent / "pythonw.exe"
        pyw.write_text("")
        with patch("install.find_conda_env_pythonw",
                   return_value=pyw), \
             patch("install._register_consultants_task",
                   return_value=True) as reg:
            install._install_consultants_windows(
                consultants_py=fake_consultants_py,
                service_mode="always-on",
                engine_port=38095, forwarder_port=38096,
                non_interactive=True, dry_run=False,
            )
        kwargs = reg.call_args.kwargs
        assert kwargs["task_name"] == install._CONSULTANTS_TASK_NAME
        assert "consultants.server" in kwargs["exec_arguments"]
        assert "--port 38095" in kwargs["exec_arguments"]
        assert kwargs["port"] == 38095

    def test_falls_back_to_python_when_pythonw_missing(
            self, fake_consultants_py, capsys):
        with patch("install.find_conda_env_pythonw", return_value=None), \
             patch("install._register_consultants_task",
                   return_value=True) as reg:
            install._install_consultants_windows(
                consultants_py=fake_consultants_py,
                service_mode="always-on",
                engine_port=38095, forwarder_port=38096,
                non_interactive=True, dry_run=False,
            )
        kwargs = reg.call_args.kwargs
        assert str(fake_consultants_py) == kwargs["exec_command"]
        assert "console window will be visible" in capsys.readouterr().out


class TestInstallConsultantsWindowsSmartStart:
    def test_dispatches_to_forwarder_task(self, fake_consultants_py,
                                          tmp_path):
        # Pretend the main env has pythonw at a known path.
        main_pyw = tmp_path / "envs" / "claude-hooks" / "pythonw.exe"
        main_pyw.parent.mkdir(parents=True, exist_ok=True)
        main_pyw.write_text("")
        with patch("install.find_conda_env_pythonw",
                   return_value=main_pyw), \
             patch("install._register_consultants_task",
                   return_value=True) as reg:
            install._install_consultants_windows(
                consultants_py=fake_consultants_py,
                service_mode="smart-start",
                engine_port=38095, forwarder_port=38096,
                non_interactive=True, dry_run=False,
            )
        kwargs = reg.call_args.kwargs
        assert kwargs["task_name"] == \
            install._CONSULTANTS_FORWARDER_TASK_NAME
        assert "consultants_forwarder" in kwargs["exec_arguments"]
        assert "--listen-port 38096" in kwargs["exec_arguments"]
        assert kwargs["port"] == 38096

    def test_aborts_when_main_env_missing(
            self, fake_consultants_py, tmp_path, capsys):
        missing_py = tmp_path / "missing" / "python.exe"
        with patch("install.find_conda_env_pythonw", return_value=None), \
             patch("install.find_conda_env_python",
                   return_value=missing_py), \
             patch("install._register_consultants_task") as reg:
            install._install_consultants_windows(
                consultants_py=fake_consultants_py,
                service_mode="smart-start",
                engine_port=38095, forwarder_port=38096,
                non_interactive=True, dry_run=False,
            )
        # Should bail before reaching the registration helper.
        reg.assert_not_called()
        assert "main claude-hooks env not found" in capsys.readouterr().out


class TestInstallConsultantsWindowsDryRun:
    def test_does_nothing(self, fake_consultants_py, capsys):
        with patch("install._register_consultants_task") as reg:
            install._install_consultants_windows(
                consultants_py=fake_consultants_py,
                service_mode="always-on",
                engine_port=38095, forwarder_port=38096,
                non_interactive=True, dry_run=True,
            )
        reg.assert_not_called()
        assert "[dry-run]" in capsys.readouterr().out


# ----------------------- macOS launchd path --------------------- #

class TestInstallConsultantsLaunchdAlwaysOn:
    def test_writes_plist_and_loads(self, fake_consultants_py,
                                    tmp_path, monkeypatch):
        monkeypatch.setattr(install.Path, "home", lambda: tmp_path)
        rc = MagicMock()
        rc.returncode = 0
        with patch("install.subprocess.run", return_value=rc) as run:
            install._install_consultants_launchd(
                consultants_py=fake_consultants_py,
                service_mode="always-on",
                engine_port=38095, forwarder_port=38096,
                non_interactive=True, dry_run=False,
            )
        plist = (tmp_path / "Library" / "LaunchAgents"
                 / "com.claude-hooks.consultants.plist")
        assert plist.exists()
        text = plist.read_text()
        assert "consultants.server" in text
        assert "--port</string>" in text or "<string>--port</string>" in text
        # launchctl load was called
        assert any("launchctl" in str(c.args[0])
                   for c in run.call_args_list)

    def test_dry_run_writes_nothing(self, fake_consultants_py,
                                    tmp_path, monkeypatch):
        monkeypatch.setattr(install.Path, "home", lambda: tmp_path)
        with patch("install.subprocess.run") as run:
            install._install_consultants_launchd(
                consultants_py=fake_consultants_py,
                service_mode="always-on",
                engine_port=38095, forwarder_port=38096,
                non_interactive=True, dry_run=True,
            )
        run.assert_not_called()


class TestInstallConsultantsLaunchdSmartStart:
    def test_uses_main_env_python_for_forwarder(
            self, fake_consultants_py, tmp_path, monkeypatch):
        monkeypatch.setattr(install.Path, "home", lambda: tmp_path)
        main_py = tmp_path / "envs" / "claude-hooks" / "bin" / "python"
        main_py.parent.mkdir(parents=True, exist_ok=True)
        main_py.write_text("")
        rc = MagicMock(returncode=0)
        with patch("install.find_conda_env_python", return_value=main_py), \
             patch("install.subprocess.run", return_value=rc):
            install._install_consultants_launchd(
                consultants_py=fake_consultants_py,
                service_mode="smart-start",
                engine_port=38095, forwarder_port=38096,
                non_interactive=True, dry_run=False,
            )
        plist = (tmp_path / "Library" / "LaunchAgents"
                 / "com.claude-hooks.consultants-forwarder.plist")
        assert plist.exists()
        text = plist.read_text()
        assert str(main_py) in text
        assert "consultants_forwarder" in text
        assert str(fake_consultants_py) in text  # passed via --engine-python
