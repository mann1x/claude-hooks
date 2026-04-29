"""Tests for ``install._install_proxy_launchd`` (macOS LaunchAgent for the proxy).

OS-gate test calls the outer entry; behavior tests call
``_install_proxy_launchd_steps`` directly to avoid having to fake
``sys.platform``.
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


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Redirect ``Path.home()`` to a tmp dir."""
    monkeypatch.setattr(install.Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


@pytest.fixture
def fake_run(monkeypatch):
    calls = []

    def _run(argv, **kw):
        calls.append(list(argv))
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(install.subprocess, "run", _run)
    return calls


# ---------------------------------------------------------------- #
# OS gate (outer entry)
# ---------------------------------------------------------------- #
class TestOuterOsGate:
    def test_no_op_on_non_macos(self, fake_home, fake_run, monkeypatch, capsys):
        monkeypatch.setattr(install.sys, "platform", "linux")
        install._install_proxy_launchd(
            {"proxy": {"enabled": True}},
            non_interactive=True, dry_run=False,
        )
        assert not (fake_home / "Library" / "LaunchAgents" /
                    install._PROXY_LAUNCHD_FILENAME).exists()
        assert fake_run == []
        assert "Proxy launchd agent" not in capsys.readouterr().out

    def test_outer_dispatches_to_steps_on_macos(self, monkeypatch):
        monkeypatch.setattr(install.sys, "platform", "darwin")
        with patch.object(install, "_install_proxy_launchd_steps") as steps:
            install._install_proxy_launchd(
                {"proxy": {"enabled": True}},
                non_interactive=True, dry_run=True,
            )
        steps.assert_called_once()


# ---------------------------------------------------------------- #
# Config gate (steps fn)
# ---------------------------------------------------------------- #
class TestConfigGate:
    def test_no_op_when_proxy_disabled(self, fake_home, fake_run, capsys):
        install._install_proxy_launchd_steps(
            {"proxy": {"enabled": False}},
            non_interactive=True, dry_run=False,
        )
        assert not (fake_home / "Library" / "LaunchAgents" /
                    install._PROXY_LAUNCHD_FILENAME).exists()
        assert fake_run == []

    def test_no_op_when_proxy_section_missing(self, fake_home, fake_run):
        install._install_proxy_launchd_steps(
            {}, non_interactive=True, dry_run=False,
        )
        assert not (fake_home / "Library" / "LaunchAgents" /
                    install._PROXY_LAUNCHD_FILENAME).exists()
        assert fake_run == []


# ---------------------------------------------------------------- #
# Install path (steps fn)
# ---------------------------------------------------------------- #
class TestInstall:
    def test_writes_plist_and_loads_when_absent(
        self, fake_home, fake_run, capsys,
    ):
        install._install_proxy_launchd_steps(
            {"proxy": {"enabled": True, "listen_port": 38080}},
            non_interactive=True, dry_run=False,
        )
        plist = (fake_home / "Library" / "LaunchAgents" /
                 install._PROXY_LAUNCHD_FILENAME)
        assert plist.exists()
        content = plist.read_text(encoding="utf-8")
        # Substitutions happened.
        assert "__REPO_PATH__" not in content
        assert "__HOME__" not in content
        assert "__LABEL__" not in content
        assert install._PROXY_LAUNCHD_LABEL in content
        # launchctl load got called.
        assert any(
            argv[:3] == ["launchctl", "load", "-w"] and argv[-1] == str(plist)
            for argv in fake_run
        )
        out = capsys.readouterr().out
        assert "ANTHROPIC_BASE_URL" in out

    def test_idempotent_when_plist_exists(
        self, fake_home, fake_run, capsys,
    ):
        plist_dir = fake_home / "Library" / "LaunchAgents"
        plist_dir.mkdir(parents=True)
        plist = plist_dir / install._PROXY_LAUNCHD_FILENAME
        plist.write_text("pre-existing", encoding="utf-8")

        install._install_proxy_launchd_steps(
            {"proxy": {"enabled": True}},
            non_interactive=True, dry_run=False,
        )
        assert plist.read_text(encoding="utf-8") == "pre-existing"
        assert fake_run == []
        assert "leaving as-is" in capsys.readouterr().out

    def test_dry_run_writes_nothing(
        self, fake_home, fake_run, capsys,
    ):
        install._install_proxy_launchd_steps(
            {"proxy": {"enabled": True}},
            non_interactive=True, dry_run=True,
        )
        plist = (fake_home / "Library" / "LaunchAgents" /
                 install._PROXY_LAUNCHD_FILENAME)
        assert not plist.exists()
        assert fake_run == []
        assert "dry-run" in capsys.readouterr().out

    def test_launchctl_failure_logged_but_not_raised(
        self, fake_home, monkeypatch, capsys,
    ):
        def _run(argv, **kw):
            return MagicMock(returncode=1, stdout="", stderr="bootstrap failed")

        monkeypatch.setattr(install.subprocess, "run", _run)
        install._install_proxy_launchd_steps(
            {"proxy": {"enabled": True}},
            non_interactive=True, dry_run=False,
        )
        out = capsys.readouterr().out
        assert "launchctl load failed" in out
        # No ANTHROPIC_BASE_URL hint on failure -- the proxy isn't running.
        assert "ANTHROPIC_BASE_URL" not in out


# ---------------------------------------------------------------- #
# Post-install hint helper
# ---------------------------------------------------------------- #
class TestPostInstallHint:
    def test_uses_loopback_when_listen_host_is_zero(self, capsys):
        install._print_proxy_post_install_hint(
            {"listen_host": "0.0.0.0", "listen_port": 38080},
        )
        out = capsys.readouterr().out
        assert "127.0.0.1:38080" in out

    def test_uses_lan_address_when_listen_host_is_explicit(self, capsys):
        install._print_proxy_post_install_hint(
            {"listen_host": "192.168.178.2", "listen_port": 38090},
        )
        out = capsys.readouterr().out
        assert "192.168.178.2:38090" in out

    def test_defaults_when_fields_missing(self, capsys):
        install._print_proxy_post_install_hint({})
        out = capsys.readouterr().out
        assert "127.0.0.1:38080" in out
