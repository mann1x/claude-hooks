"""Tests for ``claude_hooks/lang_servers.py`` — the LS detection +
installer matrix that backs install.py's LSP-engine dialog (v1.9+).

These tests assert the matrix's invariants (every Tier 1 spec has a
non-MANUAL installer; every (Installer, spec) listed in
``spec.installers`` either has an entry in ``INSTALL_COMMANDS`` or
is filtered by OS gating), and they cover the per-OS dispatch logic
without touching the filesystem or running real installers.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from claude_hooks import lang_servers as ls  # noqa: E402


# --------------------------------------------------------------------- #
# Matrix invariants
# --------------------------------------------------------------------- #

class TestMatrixInvariants:
    def test_tier1_specs_have_non_manual_installer(self):
        """Every Tier 1 spec must declare at least one non-MANUAL
        installer — otherwise the auto-install offer would never
        fire and the LS belongs in Tier 2."""
        for spec in ls.SPECS:
            if spec.tier != 1:
                continue
            non_manual = [
                i for i in spec.installers if i is not ls.Installer.MANUAL
            ]
            assert non_manual, (
                f"Tier 1 spec {spec.name!r} has only MANUAL installers"
            )

    def test_tier1_spec_has_install_command_for_each_installer(self):
        """For every Tier 1 spec and every non-MANUAL installer it
        declares, there must be a matching entry in
        ``INSTALL_COMMANDS`` — otherwise ``select_installer_for``
        could return an installer the dispatch table can't act on."""
        for spec in ls.SPECS:
            if spec.tier != 1:
                continue
            for inst in spec.installers:
                if inst is ls.Installer.MANUAL:
                    continue
                cmds = ls.INSTALL_COMMANDS.get(inst, {})
                assert spec.name in cmds, (
                    f"missing INSTALL_COMMANDS[{inst}][{spec.name!r}]"
                )

    def test_tier2_specs_use_only_manual(self):
        for spec in ls.SPECS:
            if spec.tier != 2:
                continue
            assert all(i is ls.Installer.MANUAL for i in spec.installers), (
                f"Tier 2 spec {spec.name!r} declares non-MANUAL installers"
            )

    def test_extensions_are_normalized_no_leading_dot(self):
        """Engine config.py normalises extensions to lowercase
        no-leading-dot; SPECS must be in the same form so cclsp.json
        round-trips without surprises."""
        for spec in ls.SPECS:
            for ext in spec.extensions:
                assert "." not in ext, f"{spec.name}: leading dot in {ext!r}"
                assert ext == ext.lower(), f"{spec.name}: non-lower {ext!r}"


# --------------------------------------------------------------------- #
# Per-OS dispatch — _installer_allowed_on
# --------------------------------------------------------------------- #

class TestPlatformFilters:
    def test_apt_skipped_on_macos(self):
        assert not ls._installer_allowed_on(ls.Installer.APT, "darwin")
        assert not ls._installer_allowed_on(ls.Installer.APT, "win32")
        assert ls._installer_allowed_on(ls.Installer.APT, "linux")

    def test_brew_skipped_on_linux(self):
        assert not ls._installer_allowed_on(ls.Installer.BREW, "linux")
        assert not ls._installer_allowed_on(ls.Installer.BREW, "win32")
        assert ls._installer_allowed_on(ls.Installer.BREW, "darwin")

    def test_scoop_skipped_on_linux(self):
        assert not ls._installer_allowed_on(ls.Installer.SCOOP, "linux")
        assert not ls._installer_allowed_on(ls.Installer.SCOOP, "darwin")
        assert ls._installer_allowed_on(ls.Installer.SCOOP, "win32")

    def test_cross_platform_installers_allowed_everywhere(self):
        for inst in (ls.Installer.NPM, ls.Installer.GO, ls.Installer.RUSTUP):
            for plat in ("linux", "darwin", "win32"):
                assert ls._installer_allowed_on(inst, plat), (
                    f"{inst} should be allowed on {plat}"
                )


# --------------------------------------------------------------------- #
# select_installer_for — happy paths
# --------------------------------------------------------------------- #

def _patch_platform_and_path(plat: str, available: set[str]):
    """Return a context manager that simulates a given OS and a set
    of available package-manager binaries (``apt-get``, ``npm``,
    etc — the underlying CLI names, not the Installer enum)."""
    def _which(name: str) -> str | None:
        return f"/fake/bin/{name}" if name in available else None

    return [
        patch.object(ls, "_current_platform", return_value=plat),
        patch.object(ls.shutil, "which", side_effect=_which),
    ]


def _apply(ctxs):
    """Enter every context manager in order; returns the list of
    entered context managers so the caller can exit them."""
    entered = []
    try:
        for c in ctxs:
            c.__enter__()
            entered.append(c)
    except Exception:
        for c in reversed(entered):
            c.__exit__(None, None, None)
        raise
    return entered


def _exit(entered):
    for c in reversed(entered):
        c.__exit__(None, None, None)


class TestSelectInstallerFor:
    def test_linux_pyright_picks_npm(self):
        ctxs = _patch_platform_and_path("linux", {"npm"})
        entered = _apply(ctxs)
        try:
            spec = next(s for s in ls.SPECS if s.name == "pyright")
            assert ls.select_installer_for(spec) is ls.Installer.NPM
        finally:
            _exit(entered)

    def test_linux_clangd_picks_apt_before_dnf(self):
        ctxs = _patch_platform_and_path("linux", {"apt-get", "dnf"})
        entered = _apply(ctxs)
        try:
            spec = next(s for s in ls.SPECS if s.name == "clangd")
            # APT comes first in clangd's installers tuple.
            assert ls.select_installer_for(spec) is ls.Installer.APT
        finally:
            _exit(entered)

    def test_linux_clangd_picks_dnf_when_only_dnf_available(self):
        ctxs = _patch_platform_and_path("linux", {"dnf"})
        entered = _apply(ctxs)
        try:
            spec = next(s for s in ls.SPECS if s.name == "clangd")
            assert ls.select_installer_for(spec) is ls.Installer.DNF
        finally:
            _exit(entered)

    def test_macos_clangd_picks_brew(self):
        ctxs = _patch_platform_and_path("darwin", {"brew"})
        entered = _apply(ctxs)
        try:
            spec = next(s for s in ls.SPECS if s.name == "clangd")
            assert ls.select_installer_for(spec) is ls.Installer.BREW
        finally:
            _exit(entered)

    def test_windows_clangd_picks_scoop(self):
        ctxs = _patch_platform_and_path("win32", {"scoop"})
        entered = _apply(ctxs)
        try:
            spec = next(s for s in ls.SPECS if s.name == "clangd")
            assert ls.select_installer_for(spec) is ls.Installer.SCOOP
        finally:
            _exit(entered)

    def test_returns_none_when_no_manager_available(self):
        ctxs = _patch_platform_and_path("linux", set())
        entered = _apply(ctxs)
        try:
            spec = next(s for s in ls.SPECS if s.name == "pyright")
            assert ls.select_installer_for(spec) is None
        finally:
            _exit(entered)

    def test_returns_none_when_only_wrong_os_managers_available(self):
        """If a Linux host only has ``brew`` (rare but possible),
        clangd has no valid installer because brew is mac-only."""
        ctxs = _patch_platform_and_path("linux", {"brew"})
        entered = _apply(ctxs)
        try:
            spec = next(s for s in ls.SPECS if s.name == "clangd")
            assert ls.select_installer_for(spec) is None
        finally:
            _exit(entered)

    def test_tier2_always_returns_none(self):
        """MANUAL-only specs never have an installer to dispatch."""
        ctxs = _patch_platform_and_path("linux", {"npm", "go", "rustup"})
        entered = _apply(ctxs)
        try:
            for spec in ls.SPECS:
                if spec.tier != 2:
                    continue
                assert ls.select_installer_for(spec) is None, spec.name
        finally:
            _exit(entered)


# --------------------------------------------------------------------- #
# detect_language_servers
# --------------------------------------------------------------------- #

class TestDetect:
    def test_detect_returns_one_entry_per_spec(self):
        with patch.object(ls.shutil, "which", return_value=None):
            state = ls.detect_language_servers()
        assert set(state.keys()) == {s.name for s in ls.SPECS}

    def test_detect_marks_present_binaries_installed(self):
        def fake_which(name: str) -> str | None:
            return f"/fake/bin/{name}" if name == "pyright-langserver" else None
        with patch.object(ls.shutil, "which", side_effect=fake_which):
            state = ls.detect_language_servers()
        assert state["pyright"].installed is True
        assert state["pyright"].binary_path == "/fake/bin/pyright-langserver"
        assert state["gopls"].installed is False
        assert state["gopls"].binary_path is None

    def test_missing_tier1_gets_installer_for_missing(self):
        with patch.object(ls.shutil, "which", side_effect=lambda n: "/fake/bin/npm" if n == "npm" else None):
            with patch.object(ls, "_current_platform", return_value="linux"):
                state = ls.detect_language_servers()
        assert state["pyright"].installer_for_missing is ls.Installer.NPM
        # gopls (GO installer) — no go on PATH → None
        assert state["gopls"].installer_for_missing is None

    def test_installed_specs_have_none_installer_for_missing(self):
        """When a binary is found, we don't need to recommend an
        installer — installer_for_missing is None."""
        with patch.object(ls.shutil, "which", return_value="/fake/bin/x"):
            state = ls.detect_language_servers()
        for st in state.values():
            assert st.installer_for_missing is None


# --------------------------------------------------------------------- #
# install_language_server
# --------------------------------------------------------------------- #

class TestInstall:
    def test_dry_run_does_not_invoke_subprocess(self):
        with patch.object(ls.subprocess, "run") as run:
            spec = next(s for s in ls.SPECS if s.name == "pyright")
            ok, msg = ls.install_language_server(
                spec, ls.Installer.NPM, dry_run=True,
            )
        run.assert_not_called()
        assert ok is True
        assert "[dry-run]" in msg
        assert "npm install -g pyright" in msg

    def test_success_returns_true_and_stdout_tail(self):
        class _Proc:
            returncode = 0
            stdout = "old version removed\nnew version: 1.2.3\n"
            stderr = ""
        with patch.object(ls.subprocess, "run", return_value=_Proc()):
            spec = next(s for s in ls.SPECS if s.name == "pyright")
            ok, msg = ls.install_language_server(spec, ls.Installer.NPM)
        assert ok is True
        assert "1.2.3" in msg

    def test_failure_returns_false_and_stderr_tail(self):
        class _Proc:
            returncode = 1
            stdout = ""
            stderr = "EACCES: permission denied\n"
        with patch.object(ls.subprocess, "run", return_value=_Proc()):
            spec = next(s for s in ls.SPECS if s.name == "pyright")
            ok, msg = ls.install_language_server(spec, ls.Installer.NPM)
        assert ok is False
        assert "EACCES" in msg

    def test_timeout_returns_false_no_crash(self):
        import subprocess as _sp
        with patch.object(ls.subprocess, "run",
                          side_effect=_sp.TimeoutExpired("npm", 1)):
            spec = next(s for s in ls.SPECS if s.name == "pyright")
            ok, msg = ls.install_language_server(spec, ls.Installer.NPM)
        assert ok is False
        assert "timeout" in msg.lower()

    def test_unknown_combination_returns_false(self):
        # Tier-2 spec via NPM (no entry).
        spec = next(s for s in ls.SPECS if s.name == "lua-language-server")
        with patch.object(ls.subprocess, "run") as run:
            ok, msg = ls.install_language_server(spec, ls.Installer.NPM)
        run.assert_not_called()
        assert ok is False
        assert "no install command" in msg


# --------------------------------------------------------------------- #
# starter_cclsp_json + write_starter_cclsp_json
# --------------------------------------------------------------------- #

class TestStarterCclspJson:
    def test_only_installed_servers_appear(self):
        with patch.object(ls.shutil, "which",
                          side_effect=lambda n: "/fake/bin/x" if n == "pyright-langserver" else None):
            state = ls.detect_language_servers()
        blob = ls.starter_cclsp_json(state)
        names = {tuple(s["command"])[0] for s in blob["servers"]}
        assert "pyright-langserver" in names
        # gopls not installed → not in blob.
        assert "gopls" not in names

    def test_empty_when_nothing_installed(self):
        with patch.object(ls.shutil, "which", return_value=None):
            state = ls.detect_language_servers()
        blob = ls.starter_cclsp_json(state)
        assert blob == {"servers": []}

    def test_extensions_normalized_lowercase_no_dot(self):
        with patch.object(ls.shutil, "which", return_value="/fake/bin/x"):
            state = ls.detect_language_servers()
        blob = ls.starter_cclsp_json(state)
        for entry in blob["servers"]:
            for ext in entry["extensions"]:
                assert "." not in ext
                assert ext == ext.lower()

    def test_write_starter_refuses_to_overwrite(self, tmp_path):
        target = tmp_path / "cclsp.json"
        target.write_text('{"existing": true}', encoding="utf-8")
        with patch.object(ls.shutil, "which", return_value="/fake/bin/x"):
            state = ls.detect_language_servers()
        ok, msg = ls.write_starter_cclsp_json(state, target)
        assert ok is False
        assert "refusing to overwrite" in msg
        # Existing file untouched.
        assert '{"existing": true}' in target.read_text(encoding="utf-8")

    def test_write_starter_dry_run_does_not_create_file(self, tmp_path):
        target = tmp_path / "cclsp.json"
        with patch.object(ls.shutil, "which", return_value="/fake/bin/x"):
            state = ls.detect_language_servers()
        ok, msg = ls.write_starter_cclsp_json(state, target, dry_run=True)
        assert ok is True
        assert "[dry-run]" in msg
        assert not target.exists()

    def test_write_starter_writes_file_with_indent(self, tmp_path):
        target = tmp_path / "cclsp.json"
        with patch.object(ls.shutil, "which",
                          side_effect=lambda n: "/fake/bin/x" if n == "pyright-langserver" else None):
            state = ls.detect_language_servers()
        ok, msg = ls.write_starter_cclsp_json(state, target)
        assert ok is True
        import json
        loaded = json.loads(target.read_text(encoding="utf-8"))
        assert "servers" in loaded
        assert any("pyright-langserver" in s["command"][0] for s in loaded["servers"])
