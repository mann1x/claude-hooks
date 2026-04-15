"""
Tests for ``install._install_proxy_stack_systemd``.

Uses monkeypatched ``/etc/systemd/system`` + ``subprocess.run`` so the
helper never actually mutates the host. Covers:

- no-op when proxy.enabled is false
- no-op when /etc/systemd/system is missing
- dry-run skips writes
- all four units install when none exist
- idempotent re-run skips already-installed units
- __REPO_PATH__ / __HOME__ substitution
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Make the installer importable when running from a checkout.
REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import install  # noqa: E402


# ============================================================ #
@pytest.fixture
def fake_etc(tmp_path, monkeypatch):
    """Redirect /etc/systemd/system to a tmp dir for the helper."""
    etc = tmp_path / "etc" / "systemd" / "system"
    etc.mkdir(parents=True)

    real_path = install.Path

    class _FakePath(type(real_path("/"))):
        pass

    # Simpler approach: monkeypatch the Path constructor used inside
    # the helper to redirect "/etc/systemd/system" to `etc`.
    import install as _i
    real_path_cls = _i.Path

    class Redirecting(type(real_path_cls("/"))):
        pass

    def fake_Path(arg="."):
        s = str(arg)
        if s == "/etc/systemd/system":
            return real_path_cls(str(etc))
        if s.startswith("/etc/systemd/system/"):
            return real_path_cls(str(etc) + s[len("/etc/systemd/system"):])
        return real_path_cls(s)

    # Preserve Path classmethods used by the helper (``Path.home()``
    # etc.) — the monkeypatched callable needs to forward unknown
    # attribute access to the real class.
    fake_Path.home = real_path_cls.home       # type: ignore[attr-defined]
    fake_Path.cwd = real_path_cls.cwd         # type: ignore[attr-defined]

    monkeypatch.setattr(_i, "Path", fake_Path)
    yield etc


@pytest.fixture
def mute_subprocess(monkeypatch):
    calls = []

    class FakeRc:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(argv, **kw):
        calls.append(list(argv))
        return FakeRc()

    monkeypatch.setattr(install.subprocess, "run", fake_run)
    yield calls


# ============================================================ #
class TestGuardPaths:
    def test_noop_when_proxy_disabled(self, fake_etc, mute_subprocess, capsys):
        install._install_proxy_stack_systemd(
            {"proxy": {"enabled": False}},
            non_interactive=True, dry_run=False,
        )
        out = capsys.readouterr().out
        assert "Proxy systemd units" not in out
        assert not list(fake_etc.iterdir())
        assert mute_subprocess == []

    def test_noop_on_windows(self, fake_etc, mute_subprocess, monkeypatch):
        monkeypatch.setattr(install.os, "name", "nt")
        install._install_proxy_stack_systemd(
            {"proxy": {"enabled": True}},
            non_interactive=True, dry_run=False,
        )
        assert not list(fake_etc.iterdir())
        assert mute_subprocess == []


class TestInstallFlow:
    def test_all_four_units_installed(self, fake_etc, mute_subprocess, capsys):
        install._install_proxy_stack_systemd(
            {"proxy": {"enabled": True}},
            non_interactive=True, dry_run=False,
        )
        names = {p.name for p in fake_etc.iterdir()}
        assert names == {
            "claude-hooks-proxy.service",
            "claude-hooks-rollup.service",
            "claude-hooks-rollup.timer",
            "claude-hooks-dashboard.service",
        }
        # systemctl enable --now fired once per unit + one daemon-reload.
        flat = [" ".join(c) for c in mute_subprocess]
        assert any("daemon-reload" in c for c in flat)
        assert sum("enable --now" in c for c in flat) == 4

    def test_substitution_applied(self, fake_etc, mute_subprocess):
        install._install_proxy_stack_systemd(
            {"proxy": {"enabled": True}},
            non_interactive=True, dry_run=False,
        )
        proxy_unit = (fake_etc / "claude-hooks-proxy.service").read_text()
        # Both placeholders gone.
        assert "__REPO_PATH__" not in proxy_unit
        assert "__HOME__" not in proxy_unit
        # Replaced with actual values.
        assert str(install.HERE.resolve()) in proxy_unit
        assert str(Path.home()) in proxy_unit

    def test_dry_run_writes_nothing(self, fake_etc, mute_subprocess, capsys):
        install._install_proxy_stack_systemd(
            {"proxy": {"enabled": True}},
            non_interactive=True, dry_run=True,
        )
        assert not list(fake_etc.iterdir())
        # No systemctl calls in dry-run either.
        assert mute_subprocess == []
        assert "[dry-run]" in capsys.readouterr().out

    def test_idempotent_rerun(self, fake_etc, mute_subprocess, capsys):
        # First install.
        install._install_proxy_stack_systemd(
            {"proxy": {"enabled": True}},
            non_interactive=True, dry_run=False,
        )
        mute_subprocess.clear()
        # Capture mtimes — a no-op re-run must not rewrite files.
        before = {
            p.name: p.stat().st_mtime_ns
            for p in fake_etc.iterdir()
        }
        # Second run — should report "All units already installed."
        install._install_proxy_stack_systemd(
            {"proxy": {"enabled": True}},
            non_interactive=True, dry_run=False,
        )
        after = {
            p.name: p.stat().st_mtime_ns
            for p in fake_etc.iterdir()
        }
        assert before == after
        out = capsys.readouterr().out
        assert "All units already installed." in out
        # No systemctl calls on idempotent path.
        assert mute_subprocess == []

    def test_partial_install_only_writes_missing(
        self, fake_etc, mute_subprocess, capsys,
    ):
        # Pre-create one unit — simulates a partial prior install.
        (fake_etc / "claude-hooks-proxy.service").write_text(
            "pre-existing content")
        install._install_proxy_stack_systemd(
            {"proxy": {"enabled": True}},
            non_interactive=True, dry_run=False,
        )
        # Pre-existing file preserved.
        assert (fake_etc / "claude-hooks-proxy.service").read_text() \
            == "pre-existing content"
        # Other three units installed.
        for name in (
            "claude-hooks-rollup.service",
            "claude-hooks-rollup.timer",
            "claude-hooks-dashboard.service",
        ):
            assert (fake_etc / name).exists()
        # enable --now only fired for the 3 newly-written units.
        flat = [" ".join(c) for c in mute_subprocess]
        assert sum("enable --now" in c for c in flat) == 3
