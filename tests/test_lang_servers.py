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
        # Match the args, not the resolved executable: shutil.which("npm")
        # returns the bare ``npm`` on POSIX but ``...\npm.CMD`` (full path)
        # on Windows, which breaks a literal ``npm install`` substring.
        assert "install -g pyright" in msg

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
# Winget "already installed" is NOT a failure (v1.9.x)
# --------------------------------------------------------------------- #
# Winget returns non-zero ("No newer package versions are available
# from the configured sources.") when the package is already at the
# latest version. install_language_server must recognize this as
# success, not surface a misleading "[FAIL] clangd install failed"
# to the user — pandorum live regression 2026-05-21.

class TestWingetAlreadyInstalled:
    def test_no_newer_versions_phrase_treated_as_success(self):
        """Winget exit-code-1 + stdout phrase 'No newer package
        versions are available' means the package is in fact
        installed. Treat as ok=True so install.py prints [ok], not
        [FAIL]."""
        spec = next(s for s in ls.SPECS if s.name == "clangd")

        class _Proc:
            returncode = 1
            stdout = (
                "Found Clang [LLVM.LLVM] Version 22.1.4\n"
                "No newer package versions are available from the "
                "configured sources.\n"
            )
            stderr = ""

        with patch.object(ls.shutil, "which",
                          side_effect=lambda b: "C:\\winget.exe"
                          if b == "winget" else None), \
             patch.object(ls.subprocess, "run", return_value=_Proc()):
            ok, msg = ls.install_language_server(
                spec, ls.Installer.WINGET,
            )
        assert ok is True, (
            "winget 'No newer package versions' should be success, "
            f"got ok={ok!r} msg={msg!r}"
        )
        assert "already installed" in msg

    def test_already_installed_case_insensitive_phrase(self):
        """Older winget builds emit 'Package is already installed';
        the success heuristic must catch that variant too."""
        spec = next(s for s in ls.SPECS if s.name == "lua-language-server")

        class _Proc:
            returncode = 2
            stdout = "Package is already installed.\n"
            stderr = ""

        with patch.object(ls.shutil, "which",
                          side_effect=lambda b: "C:\\winget.exe"
                          if b == "winget" else None), \
             patch.object(ls.subprocess, "run", return_value=_Proc()):
            ok, _ = ls.install_language_server(
                spec, ls.Installer.WINGET,
            )
        assert ok is True

    def test_other_winget_failures_still_fail(self):
        """Don't over-swallow — a real winget failure (network,
        package-not-found) must still surface as ok=False."""
        spec = next(s for s in ls.SPECS if s.name == "clangd")

        class _Proc:
            returncode = 1
            stdout = ""
            stderr = "No package found matching input criteria."

        with patch.object(ls.shutil, "which",
                          side_effect=lambda b: "C:\\winget.exe"
                          if b == "winget" else None), \
             patch.object(ls.subprocess, "run", return_value=_Proc()):
            ok, msg = ls.install_language_server(
                spec, ls.Installer.WINGET,
            )
        assert ok is False
        assert "No package found" in msg

    def test_already_installed_heuristic_does_not_apply_to_npm(self):
        """npm has its own already-installed semantics ('up to date');
        the winget heuristic must not leak across installers."""
        spec = next(s for s in ls.SPECS if s.name == "pyright")

        class _Proc:
            returncode = 1
            stdout = ""
            stderr = "No newer package versions are available."  # red herring

        with patch.object(ls.shutil, "which",
                          side_effect=lambda b: "C:\\npm.cmd"
                          if b == "npm" else None), \
             patch.object(ls.subprocess, "run", return_value=_Proc()):
            ok, _ = ls.install_language_server(spec, ls.Installer.NPM)
        # NPM exit-1 stays a failure regardless of the phrase —
        # the heuristic is winget-only.
        assert ok is False


# --------------------------------------------------------------------- #
# Tier-2 LS now have real installers (v1.9.x)
# --------------------------------------------------------------------- #

class TestScoopManifestNotFound:
    """Scoop exits with code 0 even when the requested package isn't
    in any added bucket — ``Couldn't find manifest for 'X' from 'Y'
    bucket.`` Without explicit detection, install_language_server
    reports [ok] for an install that did nothing. The phrase-based
    re-classification was the v1.9.x fix that surfaced after the
    extras-bucket matrix mistake on pandorum 2026-05-21."""

    def test_couldnt_find_manifest_treated_as_failure(self):
        spec = next(s for s in ls.SPECS if s.name == "omnisharp")

        class _Proc:
            returncode = 0
            stdout = "Couldn't find manifest for 'omnisharp' from 'extras' bucket.\n"
            stderr = ""

        with patch.object(ls.shutil, "which",
                          side_effect=lambda b: r"C:\scoop.cmd"
                          if b == "scoop" else None), \
             patch.object(ls.subprocess, "run", return_value=_Proc()):
            ok, msg = ls.install_language_server(spec, ls.Installer.SCOOP)
        assert ok is False, (
            "scoop 'Couldn't find manifest' should be a failure, "
            f"got ok={ok!r} msg={msg!r}"
        )
        assert "manifest not found" in msg.lower()

    def test_case_insensitive_could_not_find_variant(self):
        """Older scoop builds emit lowercased variants — must catch."""
        spec = next(s for s in ls.SPECS if s.name == "clangd")

        class _Proc:
            returncode = 0
            stdout = "could not find manifest for 'clangd'\n"
            stderr = ""

        with patch.object(ls.shutil, "which",
                          side_effect=lambda b: r"C:\scoop.cmd"
                          if b == "scoop" else None), \
             patch.object(ls.subprocess, "run", return_value=_Proc()):
            ok, _ = ls.install_language_server(spec, ls.Installer.SCOOP)
        assert ok is False

    def test_normal_scoop_success_still_succeeds(self):
        """Don't over-swallow — a legitimate scoop install (exit 0
        without the manifest-not-found phrase) must still report
        success."""
        spec = next(s for s in ls.SPECS if s.name == "omnisharp")

        class _Proc:
            returncode = 0
            stdout = (
                "Installing 'omnisharp' (1.39.15) [64bit] from 'main' bucket\n"
                "Linking ~\\scoop\\apps\\omnisharp\\current => ...\n"
                "'omnisharp' (1.39.15) was installed successfully!\n"
            )
            stderr = ""

        with patch.object(ls.shutil, "which",
                          side_effect=lambda b: r"C:\scoop.cmd"
                          if b == "scoop" else None), \
             patch.object(ls.subprocess, "run", return_value=_Proc()):
            ok, msg = ls.install_language_server(spec, ls.Installer.SCOOP)
        assert ok is True
        assert "successfully" in msg

    def test_manifest_check_does_not_leak_to_other_installers(self):
        """The scoop-specific phrase check must NOT misfire on other
        installers (npm/winget) that happen to emit similar text."""
        spec = next(s for s in ls.SPECS if s.name == "pyright")

        class _Proc:
            returncode = 0
            stdout = "Couldn't find manifest for 'pyright'\n"  # red herring
            stderr = ""

        with patch.object(ls.shutil, "which",
                          side_effect=lambda b: r"C:\npm.cmd"
                          if b == "npm" else None), \
             patch.object(ls.subprocess, "run", return_value=_Proc()):
            ok, _ = ls.install_language_server(spec, ls.Installer.NPM)
        # NPM exit-0 stays a success regardless of phrase — the
        # check is scoop-only.
        assert ok is True


class TestScoopMatrixBucket:
    """All scoop install commands must use bare package names (or
    ``main/<name>``) — NOT ``extras/<name>``. v1.9.x earlier shipped
    extras/ prefixes that silently failed because scoop's main
    bucket has these packages. Lock the matrix down."""

    def test_no_scoop_command_uses_extras_bucket(self):
        for name, cmd in ls.INSTALL_COMMANDS[ls.Installer.SCOOP].items():
            assert not any("extras/" in arg for arg in cmd), (
                f"Scoop command for {name!r} uses extras/ prefix: {cmd}. "
                "Move to bare <name> — packages are in main bucket."
            )

    def test_omnisharp_uses_bare_name(self):
        cmd = ls.INSTALL_COMMANDS[ls.Installer.SCOOP]["omnisharp"]
        assert cmd[:3] == ["scoop", "install", "omnisharp"]

    def test_clangd_scoop_uses_llvm_package(self):
        # The scoop main bucket ships llvm (not clangd as a separate
        # package); install llvm gives you clangd.exe.
        cmd = ls.INSTALL_COMMANDS[ls.Installer.SCOOP]["clangd"]
        assert cmd[:3] == ["scoop", "install", "llvm"]


class TestOnDiskNotOnPathDetection:
    """When the binary exists at a known install location but isn't
    on the current shell's PATH, detect_language_servers surfaces
    it as on_disk_path so install.py can tell the user "restart
    your shell" instead of offering a redundant re-install.

    Live-caught on pandorum 2026-05-21 after the user reopened cmd
    and saw clangd reported MISSING despite winget LLVM.LLVM having
    installed it to ``C:\\Program Files\\LLVM\\bin\\clangd.exe``."""

    def test_extra_search_paths_empty_on_posix(self):
        with patch.object(ls.os, "name", "posix"):
            spec = next(s for s in ls.SPECS if s.name == "clangd")
            assert ls._windows_extra_search_paths(spec) == []

    def test_extra_search_paths_includes_winget_links(self):
        spec = next(s for s in ls.SPECS if s.name == "lua-language-server")
        with patch.object(ls.os, "name", "nt"):
            paths = ls._windows_extra_search_paths(spec)
        # Should include winget Links dir under %LOCALAPPDATA%.
        assert any("Microsoft\\WinGet\\Links" in p or
                   "Microsoft/WinGet/Links" in p
                   for p in paths), paths

    def test_extra_search_paths_includes_scoop_shims(self):
        spec = next(s for s in ls.SPECS if s.name == "omnisharp")
        with patch.object(ls.os, "name", "nt"):
            paths = ls._windows_extra_search_paths(spec)
        # Scoop shims path.
        assert any("scoop" in p and "shims" in p for p in paths), paths

    def test_extra_search_paths_includes_program_files_llvm_for_clangd(self):
        spec = next(s for s in ls.SPECS if s.name == "clangd")
        with patch.object(ls.os, "name", "nt"):
            paths = ls._windows_extra_search_paths(spec)
        # The canonical winget LLVM.LLVM install location.
        assert any("LLVM" in p and "clangd.exe" in p for p in paths), paths

    def test_detect_promotes_on_disk_when_shutil_which_fails(
            self, tmp_path, monkeypatch):
        """When shutil.which can't find the binary but it exists at
        a known install location, the state should have
        installed=False but on_disk_path set, and installer_for_missing
        should be None (we don't offer to re-install)."""
        # Create a fake clangd at a path that mimics Program Files\LLVM\bin
        fake_pf = tmp_path / "Program Files"
        fake_clangd = fake_pf / "LLVM" / "bin" / "clangd.exe"
        fake_clangd.parent.mkdir(parents=True)
        fake_clangd.write_text("")  # empty binary stand-in

        monkeypatch.setenv("ProgramFiles", str(fake_pf))

        with patch.object(ls.os, "name", "nt"), \
             patch.object(ls.shutil, "which", return_value=None):
            state = ls.detect_language_servers()

        clangd = state["clangd"]
        assert clangd.installed is False
        assert clangd.on_disk_path == str(fake_clangd), (
            f"expected on_disk_path={fake_clangd}, got {clangd.on_disk_path}"
        )
        assert clangd.installer_for_missing is None, (
            "should NOT offer to install when binary already on disk"
        )

    def test_detect_prefers_on_path_over_on_disk(
            self, tmp_path, monkeypatch):
        """When the binary IS on PATH, on_disk_path stays None even
        if it also exists at a known install location."""
        fake_pf = tmp_path / "Program Files"
        fake_clangd = fake_pf / "LLVM" / "bin" / "clangd.exe"
        fake_clangd.parent.mkdir(parents=True)
        fake_clangd.write_text("")

        monkeypatch.setenv("ProgramFiles", str(fake_pf))

        with patch.object(ls.os, "name", "nt"), \
             patch.object(ls.shutil, "which",
                          side_effect=lambda b: r"C:\some\path\clangd.exe"
                          if b == "clangd" else None):
            state = ls.detect_language_servers()

        clangd = state["clangd"]
        assert clangd.installed is True
        assert clangd.binary_path == r"C:\some\path\clangd.exe"
        assert clangd.on_disk_path is None


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

    def test_zls_has_brew_winget_scoop(self):
        """zls gained WINGET (``zigtools.zls``) so Windows hosts can
        auto-install without needing scoop."""
        spec = next(s for s in ls.SPECS if s.name == "zls")
        names = {i for i in spec.installers}
        assert ls.Installer.BREW in names
        assert ls.Installer.WINGET in names
        assert ls.Installer.SCOOP in names
        # Each must have an actual command registered.
        for inst in (ls.Installer.BREW, ls.Installer.WINGET, ls.Installer.SCOOP):
            assert "zls" in ls.INSTALL_COMMANDS[inst], (
                f"missing INSTALL_COMMANDS[{inst.value}]['zls']"
            )
        # The winget package ID is the upstream-published one.
        assert "zigtools.zls" in ls.INSTALL_COMMANDS[ls.Installer.WINGET]["zls"]

    def test_select_installer_picks_winget_for_zls_on_windows(self):
        """On a Windows host with only winget available (no scoop),
        zls picks WINGET rather than falling to MANUAL."""
        spec = next(s for s in ls.SPECS if s.name == "zls")
        ctxs = _patch_platform_and_path("win32", {"winget"})
        entered = _apply(ctxs)
        try:
            picked = ls.select_installer_for(spec)
        finally:
            _exit(entered)
        assert picked is ls.Installer.WINGET

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


# --------------------------------------------------------------------- #
# Scoop bootstrap (Windows only)
#
# Scoop is the only auto-install path for OmniSharp on Windows. The
# bootstrap helpers must:
#   - refuse to run on non-Windows,
#   - short-circuit when scoop is already on PATH,
#   - run the official PowerShell installer,
#   - add the ``extras`` bucket idempotently.
# --------------------------------------------------------------------- #

class TestScoopBootstrap:
    def test_is_scoop_installed_true_when_on_path(self):
        with patch.object(ls.shutil, "which",
                          side_effect=lambda b: r"C:\Users\u\scoop\shims\scoop.cmd"
                          if b == "scoop" else None):
            assert ls.is_scoop_installed() is True

    def test_is_scoop_installed_false_when_not_on_path(self):
        with patch.object(ls.shutil, "which", return_value=None):
            assert ls.is_scoop_installed() is False

    def test_install_scoop_refuses_on_non_windows(self):
        with patch.object(ls.os, "name", "posix"):
            ok, msg = ls.install_scoop_windows()
        assert ok is False
        assert "Windows-only" in msg

    def test_install_scoop_short_circuits_when_already_installed(self):
        with patch.object(ls.os, "name", "nt"), \
             patch.object(ls.shutil, "which",
                          side_effect=lambda b: r"C:\scoop.cmd"
                          if b == "scoop" else None), \
             patch.object(ls.subprocess, "run") as mock_run:
            ok, msg = ls.install_scoop_windows()
        assert ok is True
        assert "already installed" in msg
        mock_run.assert_not_called()

    def test_install_scoop_dry_run_no_subprocess(self):
        with patch.object(ls.os, "name", "nt"), \
             patch.object(ls.shutil, "which", return_value=None), \
             patch.object(ls.subprocess, "run") as mock_run:
            ok, msg = ls.install_scoop_windows(dry_run=True)
        assert ok is True
        assert "[dry-run]" in msg
        mock_run.assert_not_called()

    def test_install_scoop_invokes_powershell_one_liner(self):
        """The official scoop installer needs ``Set-ExecutionPolicy``
        + ``Invoke-RestMethod | Invoke-Expression``. Verify we pass
        both to PowerShell in one invocation so the policy change is
        in effect when the script downloads."""
        captured: list[list[str]] = []

        class _Proc:
            returncode = 0
            stdout = "Scoop was installed successfully!"
            stderr = ""

        def _fake_run(argv, **kw):  # noqa: ANN001
            captured.append(list(argv))
            return _Proc()

        with patch.object(ls.os, "name", "nt"), \
             patch.object(ls.shutil, "which",
                          side_effect=lambda b: r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
                          if b in ("powershell", "pwsh") else None), \
             patch.object(ls.subprocess, "run", side_effect=_fake_run):
            ok, msg = ls.install_scoop_windows()
        assert ok is True
        assert "successfully" in msg
        argv = captured[0]
        assert argv[0].endswith("powershell.exe")
        script = argv[-1]
        assert "Set-ExecutionPolicy" in script
        assert "RemoteSigned" in script
        assert "CurrentUser" in script
        assert "Invoke-RestMethod" in script
        assert "get.scoop.sh" in script
        assert "Invoke-Expression" in script

    def test_install_scoop_propagates_failure(self):
        class _Proc:
            returncode = 1
            stdout = ""
            stderr = "Could not connect to https://get.scoop.sh"

        with patch.object(ls.os, "name", "nt"), \
             patch.object(ls.shutil, "which",
                          side_effect=lambda b: "powershell.exe"
                          if b in ("powershell", "pwsh") else None), \
             patch.object(ls.subprocess, "run", return_value=_Proc()):
            ok, msg = ls.install_scoop_windows()
        assert ok is False
        assert "Could not connect" in msg

    def test_install_scoop_appends_shims_to_path_on_success(self, monkeypatch,
                                                            tmp_path):
        """After PowerShell returns success, the running process's
        ``os.environ['PATH']`` MUST be updated to include scoop's
        shims dir — otherwise the immediately-following
        ``is_scoop_installed()`` / ``ensure_scoop_bucket()`` calls
        see stale PATH and report 'scoop not installed' even though
        the binary is on disk. Live-caught on pandorum 2026-05-21."""
        class _Proc:
            returncode = 0
            stdout = "Type 'scoop help' for instructions."
            stderr = ""

        fake_home = tmp_path
        fake_shims = fake_home / "scoop" / "shims"
        fake_shims.mkdir(parents=True)

        starting_path = "/usr/bin:/bin"
        monkeypatch.setenv("PATH", starting_path)
        monkeypatch.delenv("SCOOP", raising=False)

        with patch.object(ls.os, "name", "nt"), \
             patch.object(ls.os.path, "expanduser",
                          side_effect=lambda p: str(fake_home) if p == "~" else p), \
             patch.object(ls.shutil, "which",
                          side_effect=lambda b: "powershell.exe"
                          if b in ("powershell", "pwsh") else None), \
             patch.object(ls.subprocess, "run", return_value=_Proc()):
            ok, _ = ls.install_scoop_windows()

        assert ok is True
        # PATH MUST now contain the shims dir we created.
        assert str(fake_shims) in ls.os.environ["PATH"], (
            "PATH was not refreshed after scoop install — "
            f"os.environ['PATH']={ls.os.environ['PATH']!r} "
            f"did not include {fake_shims}"
        )
        # The starting PATH entries are preserved (prepended, not replaced).
        assert starting_path in ls.os.environ["PATH"]

    def test_install_scoop_honors_scoop_env_override(self, monkeypatch, tmp_path):
        """If the user sets ``$SCOOP=<custom>``, the shims dir we
        append must be ``<custom>/shims``, not the default
        ``~/scoop/shims``."""
        class _Proc:
            returncode = 0
            stdout = "ok"
            stderr = ""

        custom_root = tmp_path / "custom-scoop"
        custom_shims = custom_root / "shims"
        custom_shims.mkdir(parents=True)

        monkeypatch.setenv("SCOOP", str(custom_root))
        monkeypatch.setenv("PATH", "/usr/bin")

        with patch.object(ls.os, "name", "nt"), \
             patch.object(ls.shutil, "which",
                          side_effect=lambda b: "powershell.exe"
                          if b in ("powershell", "pwsh") else None), \
             patch.object(ls.subprocess, "run", return_value=_Proc()):
            ok, _ = ls.install_scoop_windows()

        assert ok is True
        assert str(custom_shims) in ls.os.environ["PATH"]

    def test_install_scoop_skips_path_update_if_shims_dir_missing(
            self, monkeypatch, tmp_path):
        """Safety: if scoop install claims success but the shims dir
        doesn't actually exist on disk (weird edge case), don't add
        a bogus entry to PATH."""
        class _Proc:
            returncode = 0
            stdout = "ok"
            stderr = ""

        fake_home = tmp_path  # NO scoop/shims subdir created
        starting_path = "/usr/bin:/bin"
        monkeypatch.setenv("PATH", starting_path)
        monkeypatch.delenv("SCOOP", raising=False)

        with patch.object(ls.os, "name", "nt"), \
             patch.object(ls.os.path, "expanduser",
                          side_effect=lambda p: str(fake_home) if p == "~" else p), \
             patch.object(ls.shutil, "which",
                          side_effect=lambda b: "powershell.exe"
                          if b in ("powershell", "pwsh") else None), \
             patch.object(ls.subprocess, "run", return_value=_Proc()):
            ok, _ = ls.install_scoop_windows()

        assert ok is True
        # PATH unchanged — no scoop/shims appended.
        assert ls.os.environ["PATH"] == starting_path

    def test_ensure_bucket_refuses_when_scoop_missing(self):
        with patch.object(ls.shutil, "which", return_value=None):
            ok, msg = ls.ensure_scoop_bucket("extras")
        assert ok is False
        assert "scoop not installed" in msg

    def test_ensure_bucket_idempotent_when_already_added(self):
        """If ``scoop bucket list`` already contains the bucket name,
        skip the add and return success — avoids a noisy non-zero exit
        from a benign 'already added' case."""
        class _ListProc:
            returncode = 0
            stdout = "Name    Source\nextras  https://github.com/ScoopInstaller/Extras\nmain    https://github.com/ScoopInstaller/Main\n"
            stderr = ""

        run_calls: list[list[str]] = []

        def _fake_run(argv, **kw):  # noqa: ANN001
            run_calls.append(list(argv))
            return _ListProc()

        with patch.object(ls.shutil, "which",
                          side_effect=lambda b: r"C:\scoop.cmd"
                          if b == "scoop" else None), \
             patch.object(ls.subprocess, "run", side_effect=_fake_run):
            ok, msg = ls.ensure_scoop_bucket("extras")
        assert ok is True
        assert "already" in msg.lower()
        # Only the list probe ran — no `bucket add` because already present.
        assert len(run_calls) == 1
        assert run_calls[0][1:] == ["bucket", "list"]

    def test_ensure_bucket_adds_when_missing(self):
        class _ListProc:
            returncode = 0
            stdout = "Name    Source\nmain    https://github.com/ScoopInstaller/Main\n"
            stderr = ""

        class _AddProc:
            returncode = 0
            stdout = "Bucket 'extras' added successfully."
            stderr = ""

        run_seq = [_ListProc(), _AddProc()]
        captured: list[list[str]] = []

        def _fake_run(argv, **kw):  # noqa: ANN001
            captured.append(list(argv))
            return run_seq.pop(0)

        with patch.object(ls.shutil, "which",
                          side_effect=lambda b: r"C:\scoop.cmd"
                          if b == "scoop" else None), \
             patch.object(ls.subprocess, "run", side_effect=_fake_run):
            ok, msg = ls.ensure_scoop_bucket("extras")
        assert ok is True
        assert "added bucket extras" in msg
        # Two subprocess calls: bucket list, then bucket add.
        assert len(captured) == 2
        assert captured[1][1:] == ["bucket", "add", "extras"]

    def test_ensure_bucket_handles_older_scoop_already_message(self):
        """Older scoop builds return exit-1 with 'The bucket is
        already added.' — treat that as success defensively."""
        class _ListProc:
            returncode = 0
            stdout = ""  # bucket name not in list (older scoop quirk)
            stderr = ""

        class _AddProc:
            returncode = 1
            stdout = "The 'extras' bucket is already added."
            stderr = ""

        run_seq = [_ListProc(), _AddProc()]

        def _fake_run(argv, **kw):  # noqa: ANN001
            return run_seq.pop(0)

        with patch.object(ls.shutil, "which",
                          side_effect=lambda b: r"C:\scoop.cmd"
                          if b == "scoop" else None), \
             patch.object(ls.subprocess, "run", side_effect=_fake_run):
            ok, _ = ls.ensure_scoop_bucket("extras")
        assert ok is True
