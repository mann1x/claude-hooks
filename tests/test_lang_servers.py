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

    def test_every_spec_install_command_present_for_each_installer(self):
        """For every spec (Tier 1 OR 2) and every non-MANUAL installer
        it declares, there must be a matching entry in
        ``INSTALL_COMMANDS`` — otherwise ``select_installer_for``
        could return an installer the dispatch table can't act on.

        Extended in v1.9.x from a Tier-1-only check to cover Tier 2 as
        well, after lua / zls / omnisharp were promoted from MANUAL to
        real installers via scoop / brew / winget.
        """
        for spec in ls.SPECS:
            for inst in spec.installers:
                if inst is ls.Installer.MANUAL:
                    continue
                cmds = ls.INSTALL_COMMANDS.get(inst, {})
                assert spec.name in cmds, (
                    f"missing INSTALL_COMMANDS[{inst}][{spec.name!r}]"
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



# --------------------------------------------------------------------- #
# Windows binary resolution (WinError 2 fix)
#
# install_language_server must resolve the manager binary via
# ``shutil.which`` before invoking subprocess, otherwise plain argv
# like ["npm", "install", ...] fails on Windows with
# ``WinError 2 ("The system cannot find the file specified")``
# because CreateProcess doesn't honor PATHEXT and the real binary is
# ``npm.cmd``. See v1.9.x post-deploy regression on pandorum:
#   ✗ typescript-language-server install failed: binary not found:
#     [WinError 2] The system cannot find the file specified
# --------------------------------------------------------------------- #

class TestInstallBinaryResolution:
    def test_resolves_bare_npm_via_shutil_which(self):
        """``shutil.which`` returns the full path including ``.cmd`` on
        Windows; install_language_server must replace ``cmd[0]`` with
        that path so subprocess can actually find it."""
        spec = next(s for s in ls.SPECS if s.name == "pyright")

        captured_argv: list[list[str]] = []

        class _Proc:
            returncode = 0
            stdout = "+ pyright@1.2.3"
            stderr = ""

        def _fake_run(argv, **kw):  # noqa: ANN001
            captured_argv.append(list(argv))
            return _Proc()

        with patch.object(ls.shutil, "which",
                          side_effect=lambda b: r"C:\Users\u\AppData\Roaming\npm\npm.cmd"
                          if b == "npm" else None), \
             patch.object(ls.subprocess, "run", side_effect=_fake_run):
            ok, _ = ls.install_language_server(spec, ls.Installer.NPM)
        assert ok is True
        assert captured_argv, "subprocess.run was never called"
        argv = captured_argv[0]
        # cmd[0] must be the .cmd-resolved path, not the bare "npm"
        assert argv[0].endswith("npm.cmd"), (
            f"argv[0]={argv[0]!r} — install_language_server did not "
            "swap in the shutil.which result; would WinError 2 on Windows"
        )
        # The rest of the argv stays untouched.
        assert argv[1:] == ["install", "-g", "pyright"]

    def test_falls_back_to_bare_name_when_which_returns_none(self):
        """When ``shutil.which`` finds nothing, keep ``cmd[0]`` as-is
        so the caller still gets a deterministic FileNotFoundError
        from the OS rather than us short-circuiting silently."""
        spec = next(s for s in ls.SPECS if s.name == "pyright")
        captured_argv: list[list[str]] = []

        def _fake_run(argv, **kw):  # noqa: ANN001
            captured_argv.append(list(argv))
            raise FileNotFoundError("npm not on PATH")

        with patch.object(ls.shutil, "which", return_value=None), \
             patch.object(ls.subprocess, "run", side_effect=_fake_run):
            ok, msg = ls.install_language_server(spec, ls.Installer.NPM)
        assert ok is False
        assert "binary not found" in msg
        # argv stays the original bare-name form — no silent rewrite.
        assert captured_argv and captured_argv[0][0] == "npm"

    def test_posix_resolution_is_noop_safe(self):
        """``shutil.which`` returns an absolute path on POSIX; the
        substitution is harmless (same binary, same call result)."""
        spec = next(s for s in ls.SPECS if s.name == "gopls")
        captured_argv: list[list[str]] = []

        class _Proc:
            returncode = 0
            stdout = "gopls installed"
            stderr = ""

        def _fake_run(argv, **kw):  # noqa: ANN001
            captured_argv.append(list(argv))
            return _Proc()

        with patch.object(ls.shutil, "which",
                          side_effect=lambda b: "/usr/local/bin/go" if b == "go" else None), \
             patch.object(ls.subprocess, "run", side_effect=_fake_run):
            ok, _ = ls.install_language_server(spec, ls.Installer.GO)
        assert ok is True
        assert captured_argv[0][0] == "/usr/local/bin/go"


# --------------------------------------------------------------------- #
# Tier-2 LS now have real installers (v1.9.x)
# --------------------------------------------------------------------- #

class TestTier2Installers:
    """Tier 2 LSs (lua-language-server / zls / omnisharp) were
    promoted from MANUAL-only to real per-OS installers so the
    install.py loop can offer auto-install. Lock the matrix down so
    future regressions surface here, not on a Windows user's deploy."""

    def test_lua_language_server_has_brew_scoop_winget(self):
        spec = next(s for s in ls.SPECS if s.name == "lua-language-server")
        names = {i for i in spec.installers}
        assert ls.Installer.BREW in names
        assert ls.Installer.SCOOP in names
        assert ls.Installer.WINGET in names
        # Each must have an actual command registered.
        for inst in (ls.Installer.BREW, ls.Installer.SCOOP, ls.Installer.WINGET):
            assert "lua-language-server" in ls.INSTALL_COMMANDS[inst]

    def test_zls_has_brew_scoop(self):
        spec = next(s for s in ls.SPECS if s.name == "zls")
        names = {i for i in spec.installers}
        assert ls.Installer.BREW in names
        assert ls.Installer.SCOOP in names
        assert "zls" in ls.INSTALL_COMMANDS[ls.Installer.BREW]
        assert "zls" in ls.INSTALL_COMMANDS[ls.Installer.SCOOP]

    def test_omnisharp_has_scoop(self):
        spec = next(s for s in ls.SPECS if s.name == "omnisharp")
        names = {i for i in spec.installers}
        assert ls.Installer.SCOOP in names
        assert "omnisharp" in ls.INSTALL_COMMANDS[ls.Installer.SCOOP]

    def test_select_installer_picks_winget_for_lua_on_windows(self):
        spec = next(s for s in ls.SPECS if s.name == "lua-language-server")
        # Spec installers tuple is (BREW, WINGET, SCOOP); on win32 with
        # only winget available, BREW is filtered by OS, SCOOP not on
        # PATH → WINGET wins.
        ctxs = _patch_platform_and_path("win32", {"winget"})
        entered = _apply(ctxs)
        try:
            picked = ls.select_installer_for(spec)
        finally:
            _exit(entered)
        assert picked is ls.Installer.WINGET


# --------------------------------------------------------------------- #
# clangd Windows installer (winget LLVM.LLVM)
# --------------------------------------------------------------------- #

class TestClangdWindows:
    def test_clangd_has_winget_installer(self):
        spec = next(s for s in ls.SPECS if s.name == "clangd")
        assert ls.Installer.WINGET in spec.installers
        cmd = ls.INSTALL_COMMANDS[ls.Installer.WINGET]["clangd"]
        # LLVM bundle is the canonical winget package containing clangd.
        assert "LLVM.LLVM" in cmd
        # Non-interactive flags so the install actually proceeds when
        # driven from install.py without a tty for the EULA prompt.
        assert "--silent" in cmd
        assert "--accept-source-agreements" in cmd
        assert "--accept-package-agreements" in cmd

    def test_clangd_picks_winget_on_windows_when_only_winget_available(self):
        spec = next(s for s in ls.SPECS if s.name == "clangd")
        ctxs = _patch_platform_and_path("win32", {"winget"})
        entered = _apply(ctxs)
        try:
            picked = ls.select_installer_for(spec)
        finally:
            _exit(entered)
        assert picked is ls.Installer.WINGET
