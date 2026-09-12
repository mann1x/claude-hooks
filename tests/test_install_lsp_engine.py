"""Tests for ``install._setup_lsp_engine`` and its helpers (v1.9+).

The dialog has four user-visible phases:

1. Detection table (always shown when interactive).
2. Per-missing-spec install loop (only if any Tier 1 LS is missing
   AND the user answers ``y`` to the "install missing now?" prompt).
3. Starter ``cclsp.json`` offer (only when no cclsp.json exists at
   the project root AND at least one Tier 1 LS is on disk).
4. Enable toggle for ``hooks.lsp_engine.enabled``.

Non-interactive runs skip the install loop entirely — we never
auto-execute destructive ops without explicit y/N.

All tests mock ``lang_servers.detect_language_servers`` and
``install_language_server`` so the suite stays portable and fast
(no real subprocess spawns, no real LS binaries required).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterable
from unittest.mock import patch


REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import install  # noqa: E402
from claude_hooks import lang_servers as ls  # noqa: E402


# --------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------- #

def _scripted_input(answers: Iterable[str]):
    it = iter(answers)

    def _fn(prompt: str = "") -> str:
        try:
            return next(it)
        except StopIteration:
            raise AssertionError(
                f"dialog asked an unscripted question: {prompt!r}"
            )
    return _fn


def _state_all_installed() -> dict:
    """Build an ``InstalledState`` dict where every Tier-1 LS is
    present and Tier-2 LSs are missing."""
    out = {}
    for spec in ls.SPECS:
        installed = (spec.tier == 1)
        out[spec.name] = ls.InstalledState(
            spec=spec,
            installed=installed,
            binary_path=f"/fake/bin/{spec.bin}" if installed else None,
            installer_for_missing=None,
        )
    return out


def _state_all_missing() -> dict:
    """Every LS missing. Tier-1 specs have NPM installer available;
    Tier-2 specs have no installer."""
    out = {}
    for spec in ls.SPECS:
        installer = None
        if spec.tier == 1:
            # Pick the first non-MANUAL installer for this spec.
            installer = next(
                (i for i in spec.installers if i is not ls.Installer.MANUAL),
                None,
            )
        out[spec.name] = ls.InstalledState(
            spec=spec,
            installed=False,
            binary_path=None,
            installer_for_missing=installer,
        )
    return out


def _state_only_pyright() -> dict:
    """Only pyright is installed; others missing with installer
    available."""
    out = {}
    for spec in ls.SPECS:
        if spec.name == "pyright":
            out[spec.name] = ls.InstalledState(
                spec=spec, installed=True,
                binary_path="/fake/bin/pyright-langserver",
                installer_for_missing=None,
            )
        else:
            installer = next(
                (i for i in spec.installers if i is not ls.Installer.MANUAL),
                None,
            )
            out[spec.name] = ls.InstalledState(
                spec=spec, installed=False, binary_path=None,
                installer_for_missing=installer,
            )
    return out


# --------------------------------------------------------------------- #
# Non-interactive
# --------------------------------------------------------------------- #

class TestNonInteractive:
    def test_disabled_engine_skipped_quietly(self, capsys, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        with patch.object(ls, "detect_language_servers",
                          return_value=_state_all_installed()):
            cfg = {}
            install._setup_lsp_engine(
                cfg, non_interactive=True, dry_run=False,
            )
        out = capsys.readouterr().out
        assert "non-interactive: keeping LSP engine disabled" in out
        # cfg was touched only to default-initialize the block.
        assert cfg["hooks"]["lsp_engine"] == {}

    def test_already_enabled_non_interactive_reports_state(self, capsys,
                                                           tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        cfg = {"hooks": {"lsp_engine": {"enabled": True}}}
        with patch.object(ls, "detect_language_servers",
                          return_value=_state_all_installed()):
            install._setup_lsp_engine(
                cfg, non_interactive=True, dry_run=False,
            )
        out = capsys.readouterr().out
        # Detection table printed even in non-interactive when already on.
        assert "pyright" in out.lower()
        assert "cclsp.json: MISSING" in out


# --------------------------------------------------------------------- #
# Interactive — disabled → enable
# --------------------------------------------------------------------- #

class TestEnableFlow:
    def test_first_run_all_installed_enables_engine(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        cfg = {}
        # All Tier-1 LSs already installed → no install loop offered,
        # cclsp.json prompt fires (file doesn't exist).
        monkeypatch.setattr(
            "builtins.input",
            _scripted_input([
                "",     # accept default Y to drop starter cclsp.json
                "",     # accept default Y to enable engine
            ]),
        )
        with patch.object(ls, "detect_language_servers",
                          return_value=_state_all_installed()):
            install._setup_lsp_engine(
                cfg, non_interactive=False, dry_run=False,
            )
        # Engine toggle flipped on.
        assert cfg["hooks"]["lsp_engine"]["enabled"] is True
        # Starter cclsp.json landed at the project root.
        cclsp = tmp_path / "cclsp.json"
        assert cclsp.exists()
        import json
        loaded = json.loads(cclsp.read_text(encoding="utf-8"))
        names = [s["command"][0] for s in loaded["servers"]]
        assert "pyright-langserver" in names

    def test_user_declines_enable(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        cfg = {}
        monkeypatch.setattr(
            "builtins.input",
            _scripted_input([
                "",   # default Y starter cclsp.json (so cclsp.json gets written
                      # but engine stays off)
                "n",  # don't enable engine
            ]),
        )
        with patch.object(ls, "detect_language_servers",
                          return_value=_state_all_installed()):
            install._setup_lsp_engine(
                cfg, non_interactive=False, dry_run=False,
            )
        # Engine toggle NOT flipped.
        assert cfg["hooks"]["lsp_engine"].get("enabled") in (None, False)

    def test_no_tier1_installed_no_enable_offered(self, monkeypatch, tmp_path,
                                                  capsys):
        monkeypatch.chdir(tmp_path)
        cfg = {}
        # User declines the install loop → all Tier-1 missing after
        # re-detect → engine prompt skipped.
        monkeypatch.setattr(
            "builtins.input",
            _scripted_input(["n"]),  # decline install loop
        )
        with patch.object(ls, "detect_language_servers",
                          return_value=_state_all_missing()):
            install._setup_lsp_engine(
                cfg, non_interactive=False, dry_run=False,
            )
        out = capsys.readouterr().out
        assert "No Tier-1 language servers detected" in out
        assert cfg["hooks"]["lsp_engine"].get("enabled") in (None, False)
        # No starter cclsp.json written.
        assert not (tmp_path / "cclsp.json").exists()


# --------------------------------------------------------------------- #
# Install loop
# --------------------------------------------------------------------- #

class TestInstallLoop:
    def test_install_loop_dispatches_per_missing_spec(self, monkeypatch,
                                                     tmp_path):
        monkeypatch.chdir(tmp_path)
        cfg = {}
        # Only pyright installed; user opts into install loop and accepts
        # every install prompt. Post-v1.9.x the loop covers Tier 2 too
        # (lua / zls / omnisharp), so there are 8 install prompts
        # (5 Tier-1 + 3 Tier-2) rather than 5.
        monkeypatch.setattr(
            "builtins.input",
            _scripted_input([
                "y",                                  # opt into install loop
                "y", "y", "y", "y", "y",              # 5 Tier-1
                "y", "y", "y",                        # 3 Tier-2
                "",        # accept starter cclsp.json default Y
                "",        # accept enable default Y
            ]),
        )
        install_calls = []

        def fake_install(spec, installer, *, dry_run=False):
            install_calls.append((spec.name, installer))
            return True, f"installed {spec.name}"

        # On re-detect after the install loop, return state with
        # everything installed.
        with patch.object(ls, "detect_language_servers",
                          side_effect=[_state_only_pyright(),
                                       _state_all_installed()]), \
             patch.object(ls, "install_language_server",
                          side_effect=fake_install):
            install._setup_lsp_engine(
                cfg, non_interactive=False, dry_run=False,
            )

        # All 8 missing-with-installer specs dispatch.
        names = [n for n, _ in install_calls]
        assert "gopls" in names
        assert "rust-analyzer" in names
        assert "clangd" in names
        assert "typescript-language-server" in names
        assert "bash-language-server" in names
        # Tier-2 also dispatches under v1.9.x semantics.
        assert "lua-language-server" in names
        assert "zls" in names
        assert "omnisharp" in names

    def test_install_loop_skipped_when_no_tier1_missing(self, monkeypatch,
                                                       tmp_path):
        monkeypatch.chdir(tmp_path)
        cfg = {}
        # All Tier-1 installed → install loop entirely skipped.
        monkeypatch.setattr(
            "builtins.input",
            _scripted_input([
                "",   # starter cclsp.json default Y
                "",   # enable default Y
            ]),
        )
        with patch.object(ls, "detect_language_servers",
                          return_value=_state_all_installed()), \
             patch.object(ls, "install_language_server") as install_mock:
            install._setup_lsp_engine(
                cfg, non_interactive=False, dry_run=False,
            )
        install_mock.assert_not_called()

    def test_install_loop_per_spec_decline_skips_only_that_one(
            self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        cfg = {}
        # Only pyright installed; user opts into install loop but
        # declines gopls — others still get installed (4 remaining
        # Tier-1 + 3 Tier-2 under v1.9.x semantics).
        monkeypatch.setattr(
            "builtins.input",
            _scripted_input([
                "y",                          # opt into install loop
                "n",                          # decline gopls
                "y", "y", "y", "y",           # remaining 4 Tier-1
                "y", "y", "y",                # 3 Tier-2
                "",        # starter cclsp.json default Y
                "",        # enable default Y
            ]),
        )
        install_calls = []

        def fake_install(spec, installer, *, dry_run=False):
            install_calls.append(spec.name)
            return True, ""

        with patch.object(ls, "detect_language_servers",
                          side_effect=[_state_only_pyright(),
                                       _state_all_installed()]), \
             patch.object(ls, "install_language_server",
                          side_effect=fake_install):
            install._setup_lsp_engine(
                cfg, non_interactive=False, dry_run=False,
            )
        assert "gopls" not in install_calls
        assert "rust-analyzer" in install_calls

    def test_install_loop_continues_after_one_failure(self, monkeypatch,
                                                     tmp_path, capsys):
        monkeypatch.chdir(tmp_path)
        cfg = {}
        monkeypatch.setattr(
            "builtins.input",
            _scripted_input([
                "y",                                  # opt in
                "y", "y", "y", "y", "y",              # 5 Tier-1
                "y", "y", "y",                        # 3 Tier-2
                "",        # starter cclsp.json default Y
                "",        # enable default Y
            ]),
        )

        def flaky_install(spec, installer, *, dry_run=False):
            if spec.name == "gopls":
                return False, "EACCES"
            return True, ""

        with patch.object(ls, "detect_language_servers",
                          side_effect=[_state_only_pyright(),
                                       _state_only_pyright()]), \
             patch.object(ls, "install_language_server",
                          side_effect=flaky_install):
            install._setup_lsp_engine(
                cfg, non_interactive=False, dry_run=False,
            )
        out = capsys.readouterr().out
        assert "gopls install failed" in out
        # Subsequent installs still attempted (capture proves it via the
        # success markers for others).
        assert "rust-analyzer" in out

    def test_install_loop_no_installer_prints_manual_pointer(
            self, monkeypatch, tmp_path, capsys):
        monkeypatch.chdir(tmp_path)
        cfg = {}
        # Build a state where one Tier-1 spec has no installer (e.g.
        # the toolchain is missing).
        state = _state_only_pyright()
        gopls = state["gopls"]
        state["gopls"] = ls.InstalledState(
            spec=gopls.spec, installed=False, binary_path=None,
            installer_for_missing=None,  # no go toolchain
        )
        monkeypatch.setattr(
            "builtins.input",
            _scripted_input([
                "y",                                  # opt in
                "y", "y", "y", "y",                   # remaining 4 Tier-1 (gopls auto-skips)
                "y", "y", "y",                        # 3 Tier-2
                "",        # starter cclsp.json default Y
                "",        # enable default Y
            ]),
        )
        with patch.object(ls, "detect_language_servers",
                          side_effect=[state, state]), \
             patch.object(ls, "install_language_server",
                          return_value=(True, "")):
            install._setup_lsp_engine(
                cfg, non_interactive=False, dry_run=False,
            )
        out = capsys.readouterr().out
        assert "Install gopls? Requires a package manager not found on PATH" in out


# --------------------------------------------------------------------- #
# Scoop bootstrap (Windows only) — install loop side
#
# When the user opts into the install loop on a Windows host and any
# missing LS is SCOOP-only (currently: OmniSharp), install.py offers a
# one-shot scoop bootstrap before iterating. Decline keeps OmniSharp
# in MANUAL; accept installs scoop, adds the ``extras`` bucket, and
# re-detects so the install loop dispatches it.
# --------------------------------------------------------------------- #


def _state_omnisharp_blocked_on_windows() -> dict:
    """Linux/POSIX OS by default — the helper sets installer_for_missing
    correctly. For the bootstrap tests we then patch ``os.name`` to
    ``"nt"`` and ``ls.is_scoop_installed`` to ``False`` to simulate
    the Windows-without-scoop case."""
    out = {}
    for spec in ls.SPECS:
        if spec.name == "pyright":
            out[spec.name] = ls.InstalledState(
                spec=spec, installed=True,
                binary_path="/fake/bin/pyright-langserver",
                installer_for_missing=None,
            )
            continue
        if spec.name == "omnisharp":
            # On Windows-without-scoop, omnisharp is SCOOP-only and
            # blocked. ``installer_for_missing=None`` mirrors what
            # detect_language_servers would set.
            out[spec.name] = ls.InstalledState(
                spec=spec, installed=False, binary_path=None,
                installer_for_missing=None,
            )
            continue
        # All other LSs have an installer available (gopls→go,
        # ts-LS→npm, lua→winget, etc.).
        installer = next(
            (i for i in spec.installers if i is not ls.Installer.MANUAL),
            None,
        )
        out[spec.name] = ls.InstalledState(
            spec=spec, installed=False, binary_path=None,
            installer_for_missing=installer,
        )
    return out


class TestScoopBootstrapDialog:
    def test_offered_when_omnisharp_scoop_only_on_windows(
            self, monkeypatch, tmp_path, capsys):
        """The 'Install scoop to unlock...' prompt fires exactly once
        per install.py run, mentioning the affected LS."""
        monkeypatch.chdir(tmp_path)
        cfg = {}
        # Decline the bootstrap, then accept the rest so the install
        # loop still runs against the LSs that have non-scoop
        # installers (omnisharp gets skipped because no installer).
        # Tier-1 missing (5): gopls, rust-analyzer, clangd, ts-LS, bash-LS.
        # Tier-2 missing (3): lua, zls, omnisharp — but omnisharp has
        # no installer so the loop skips it (no prompt). 7 install prompts.
        monkeypatch.setattr(
            "builtins.input",
            _scripted_input([
                "y",                     # opt into install loop
                "n",                     # decline scoop bootstrap
                "y", "y", "y", "y", "y", # 5 Tier-1
                "y", "y",                # lua + zls Tier-2
                "",   # accept cclsp default Y
                "",   # accept engine default Y
            ]),
        )

        install_calls = []

        def fake_install(spec, installer, *, dry_run=False):
            install_calls.append(spec.name)
            return True, "ok"

        with patch.object(install, "_lsp_is_windows", return_value=True), \
             patch.object(ls, "is_scoop_installed", return_value=False), \
             patch.object(ls, "install_scoop_windows") as scoop_install, \
             patch.object(ls, "ensure_scoop_bucket") as ensure_bucket, \
             patch.object(ls, "detect_language_servers",
                          side_effect=[_state_omnisharp_blocked_on_windows(),
                                       _state_all_installed()]), \
             patch.object(ls, "install_language_server",
                          side_effect=fake_install):
            install._setup_lsp_engine(
                cfg, non_interactive=False, dry_run=False,
            )

        out = capsys.readouterr().out
        # The bootstrap helper prints this banner (the input() prompt
        # text is swallowed by _scripted_input, so we assert on the
        # surrounding print()-emitted lines).
        assert "can be installed via scoop on" in out
        assert "omnisharp" in out
        assert "scoop installs entirely to the user profile" in out
        # User declined → bootstrap did not run.
        scoop_install.assert_not_called()
        ensure_bucket.assert_not_called()
        # omnisharp dispatch was skipped (no installer was assigned).
        assert "omnisharp" not in install_calls

    def test_accepted_installs_scoop_and_dispatches_omnisharp(
            self, monkeypatch, tmp_path, capsys):
        """Accept the bootstrap → install_scoop_windows runs, state is
        re-detected, and omnisharp dispatches via SCOOP.

        Post-v1.9.x: no extras-bucket dance — all our LSs live in
        scoop's ``main`` bucket which is added at scoop install."""
        monkeypatch.chdir(tmp_path)
        cfg = {}

        # After bootstrap, the re-detected state must promote
        # omnisharp's installer_for_missing from None to SCOOP.
        post_bootstrap = _state_omnisharp_blocked_on_windows()
        post_bootstrap["omnisharp"] = ls.InstalledState(
            spec=post_bootstrap["omnisharp"].spec,
            installed=False,
            binary_path=None,
            installer_for_missing=ls.Installer.SCOOP,
        )

        # 8 install prompts now (omnisharp included).
        monkeypatch.setattr(
            "builtins.input",
            _scripted_input([
                "y",                                  # opt into install loop
                "y",                                  # accept scoop bootstrap
                "y", "y", "y", "y", "y",              # 5 Tier-1
                "y", "y", "y",                        # lua + zls + omnisharp
                "",   # cclsp default Y
                "",   # enable default Y
            ]),
        )

        install_calls = []

        def fake_install(spec, installer, *, dry_run=False):
            install_calls.append((spec.name, installer))
            return True, "ok"

        with patch.object(install, "_lsp_is_windows", return_value=True), \
             patch.object(ls, "is_scoop_installed", return_value=False), \
             patch.object(ls, "install_scoop_windows",
                          return_value=(True, "Scoop was installed successfully!")) as scoop_install, \
             patch.object(ls, "ensure_scoop_bucket") as ensure_bucket, \
             patch.object(ls, "detect_language_servers",
                          side_effect=[_state_omnisharp_blocked_on_windows(),
                                       post_bootstrap,        # after bootstrap
                                       _state_all_installed()]), \
             patch.object(ls, "install_language_server",
                          side_effect=fake_install):
            install._setup_lsp_engine(
                cfg, non_interactive=False, dry_run=False,
            )

        scoop_install.assert_called_once()
        # extras-bucket dance is gone — main bucket has everything.
        ensure_bucket.assert_not_called()
        # omnisharp dispatched via SCOOP after the bootstrap.
        names = [n for n, _ in install_calls]
        assert "omnisharp" in names
        omnisharp_installer = next(i for n, i in install_calls if n == "omnisharp")
        assert omnisharp_installer is ls.Installer.SCOOP

    def test_not_offered_on_posix(self, monkeypatch, tmp_path, capsys):
        """Bootstrap prompt must not appear on Linux/macOS even when
        omnisharp is in the missing list."""
        monkeypatch.chdir(tmp_path)
        cfg = {}
        # On POSIX, omnisharp's installer_for_missing stays None (no
        # scoop/winget available). Loop skips it with a manual pointer.
        # Tier-1 (5) all dispatch; Tier-2: lua + zls dispatch (via brew
        # on macOS, but we don't actually invoke brew in the test);
        # omnisharp gets the manual-pointer path.
        monkeypatch.setattr(
            "builtins.input",
            _scripted_input([
                "y",                                  # opt into install loop
                "y", "y", "y", "y", "y",              # 5 Tier-1
                "y", "y",                             # lua + zls Tier-2
                "",   # cclsp
                "",   # enable
            ]),
        )

        def fake_install(spec, installer, *, dry_run=False):
            return True, "ok"

        with patch.object(install, "_lsp_is_windows", return_value=False), \
             patch.object(ls, "install_scoop_windows") as scoop_install, \
             patch.object(ls, "ensure_scoop_bucket") as ensure_bucket, \
             patch.object(ls, "detect_language_servers",
                          side_effect=[_state_omnisharp_blocked_on_windows(),
                                       _state_all_installed()]), \
             patch.object(ls, "install_language_server",
                          side_effect=fake_install):
            install._setup_lsp_engine(
                cfg, non_interactive=False, dry_run=False,
            )

        out = capsys.readouterr().out
        assert "can be installed via scoop" not in out
        scoop_install.assert_not_called()
        ensure_bucket.assert_not_called()

    def test_scoop_already_installed_short_circuits(
            self, monkeypatch, tmp_path, capsys):
        """When scoop is already on PATH, the bootstrap helper
        short-circuits — no prompt, no install_scoop_windows call,
        no extras-bucket work. Detection promotes omnisharp's
        installer_for_missing to SCOOP, and the install loop
        dispatches ``scoop install omnisharp`` (main bucket).

        Post-v1.9.x: extras-bucket logic is gone. All our LSs are
        in scoop's main bucket — added by default at install time."""
        monkeypatch.chdir(tmp_path)
        cfg = {}
        state_with_scoop = _state_omnisharp_blocked_on_windows()
        state_with_scoop["omnisharp"] = ls.InstalledState(
            spec=state_with_scoop["omnisharp"].spec,
            installed=False, binary_path=None,
            installer_for_missing=ls.Installer.SCOOP,
        )

        monkeypatch.setattr(
            "builtins.input",
            _scripted_input([
                "y",                                  # opt into install loop
                "y", "y", "y", "y", "y",              # 5 Tier-1
                "y", "y", "y",                        # 3 Tier-2 (incl omnisharp)
                "",   # cclsp
                "",   # enable
            ]),
        )

        install_calls = []

        def fake_install(spec, installer, *, dry_run=False):
            install_calls.append((spec.name, installer))
            return True, "ok"

        with patch.object(install, "_lsp_is_windows", return_value=True), \
             patch.object(ls, "is_scoop_installed", return_value=True), \
             patch.object(ls, "install_scoop_windows") as scoop_install, \
             patch.object(ls, "ensure_scoop_bucket") as ensure_bucket, \
             patch.object(ls, "detect_language_servers",
                          side_effect=[state_with_scoop,
                                       _state_all_installed()]), \
             patch.object(ls, "install_language_server",
                          side_effect=fake_install):
            install._setup_lsp_engine(
                cfg, non_interactive=False, dry_run=False,
            )

        out = capsys.readouterr().out
        # Short-circuit: no prompt, no install_scoop_windows, no
        # ensure_scoop_bucket — none of those are needed when scoop
        # is already on PATH and the matrix uses main-bucket names.
        assert "can be installed via scoop" not in out
        scoop_install.assert_not_called()
        ensure_bucket.assert_not_called()
        # The install loop still dispatches omnisharp via SCOOP.
        names = [n for n, _ in install_calls]
        assert "omnisharp" in names

    def test_dry_run_does_not_invoke_subprocess(
            self, monkeypatch, tmp_path, capsys):
        monkeypatch.chdir(tmp_path)
        cfg = {}
        monkeypatch.setattr(
            "builtins.input",
            _scripted_input([
                "y",                                  # opt into install loop
                "y",                                  # accept scoop bootstrap
                "y", "y", "y", "y", "y",              # 5 Tier-1
                "y", "y",                             # lua + zls Tier-2
                # omnisharp not dispatched in dry-run path because state
                # didn't get re-detected (we don't actually install scoop).
                "",
                "",
            ]),
        )

        def fake_install(spec, installer, *, dry_run=False):
            assert dry_run is True
            return True, "[dry-run] ok"

        with patch.object(install, "_lsp_is_windows", return_value=True), \
             patch.object(ls, "is_scoop_installed", return_value=False), \
             patch.object(ls, "install_scoop_windows") as scoop_install, \
             patch.object(ls, "ensure_scoop_bucket") as ensure_bucket, \
             patch.object(ls, "detect_language_servers",
                          side_effect=[_state_omnisharp_blocked_on_windows(),
                                       _state_all_installed()]), \
             patch.object(ls, "install_language_server",
                          side_effect=fake_install):
            install._setup_lsp_engine(
                cfg, non_interactive=False, dry_run=True,
            )

        out = capsys.readouterr().out
        assert "[dry-run]" in out
        # In dry-run we print "would install scoop" but don't actually
        # call install_scoop_windows / ensure_scoop_bucket.
        scoop_install.assert_not_called()
        ensure_bucket.assert_not_called()


# --------------------------------------------------------------------- #
# Starter cclsp.json
# --------------------------------------------------------------------- #

class TestStarterCclsp:
    def test_refuses_to_overwrite_existing(self, monkeypatch, tmp_path, capsys):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "cclsp.json").write_text(
            '{"existing": true}', encoding="utf-8",
        )
        cfg = {}
        monkeypatch.setattr(
            "builtins.input",
            _scripted_input([
                "",   # enable default Y
            ]),
        )
        with patch.object(ls, "detect_language_servers",
                          return_value=_state_all_installed()):
            install._setup_lsp_engine(
                cfg, non_interactive=False, dry_run=False,
            )
        out = capsys.readouterr().out
        assert "already at" in out and "leaving it untouched" in out
        # The existing file is unchanged.
        assert '{"existing": true}' in (tmp_path / "cclsp.json").read_text(
            encoding="utf-8",
        )

    def test_user_declines_starter_cclsp(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        cfg = {}
        monkeypatch.setattr(
            "builtins.input",
            _scripted_input([
                "n",  # decline starter cclsp.json
                "",   # enable default Y
            ]),
        )
        with patch.object(ls, "detect_language_servers",
                          return_value=_state_all_installed()):
            install._setup_lsp_engine(
                cfg, non_interactive=False, dry_run=False,
            )
        assert not (tmp_path / "cclsp.json").exists()


# --------------------------------------------------------------------- #
# Validate-only shortcut
# --------------------------------------------------------------------- #

class TestValidateOnly:
    def test_fully_configured_offers_v_r_s_default_v(self, monkeypatch,
                                                     tmp_path, capsys):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "cclsp.json").write_text(
            '{"servers": []}', encoding="utf-8",
        )
        cfg = {"hooks": {"lsp_engine": {"enabled": True}}}
        prompts: list[str] = []

        def _input(p: str = "") -> str:
            prompts.append(p)
            return ""  # accept default

        monkeypatch.setattr("builtins.input", _input)
        with patch.object(ls, "detect_language_servers",
                          return_value=_state_all_installed()):
            install._setup_lsp_engine(
                cfg, non_interactive=False, dry_run=False,
            )
        # The [V/r/s] prompt is shown via input(), so it lives in
        # the captured prompts list, not in stdout. Verify both.
        assert any("[V]alidate only" in p for p in prompts), (
            f"no [V]alidate-only prompt: {prompts}"
        )
        out = capsys.readouterr().out
        assert "validate-only complete" in out

    def test_fully_configured_skip(self, monkeypatch, tmp_path, capsys):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "cclsp.json").write_text(
            '{"servers": []}', encoding="utf-8",
        )
        cfg = {"hooks": {"lsp_engine": {"enabled": True}}}
        monkeypatch.setattr(
            "builtins.input",
            _scripted_input(["s"]),
        )
        with patch.object(ls, "detect_language_servers",
                          return_value=_state_all_installed()):
            install._setup_lsp_engine(
                cfg, non_interactive=False, dry_run=False,
            )
        out = capsys.readouterr().out
        assert "Skipped." in out


# --------------------------------------------------------------------- #
# Dry-run honored
# --------------------------------------------------------------------- #

class TestDryRun:
    def test_dry_run_does_not_write_cclsp_json(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        cfg = {}
        monkeypatch.setattr(
            "builtins.input",
            _scripted_input([
                "",   # accept starter cclsp.json
                "",   # accept enable
            ]),
        )
        with patch.object(ls, "detect_language_servers",
                          return_value=_state_all_installed()):
            install._setup_lsp_engine(
                cfg, non_interactive=False, dry_run=True,
            )
        # File NOT created in dry-run.
        assert not (tmp_path / "cclsp.json").exists()
        # cfg enabled key NOT flipped under dry-run.
        assert cfg["hooks"]["lsp_engine"].get("enabled") in (None, False)

    def test_dry_run_install_loop_no_subprocess(self, monkeypatch, tmp_path,
                                                capsys):
        monkeypatch.chdir(tmp_path)
        cfg = {}
        monkeypatch.setattr(
            "builtins.input",
            _scripted_input([
                "y",                                  # opt into install loop
                "y", "y", "y", "y", "y",              # 5 Tier-1
                "y", "y", "y",                        # 3 Tier-2 (v1.9.x)
                "",                    # cclsp.json default Y
                "",                    # enable default Y
            ]),
        )
        with patch.object(ls, "detect_language_servers",
                          side_effect=[_state_only_pyright(),
                                       _state_only_pyright()]), \
             patch.object(ls, "install_language_server") as inst:
            install._setup_lsp_engine(
                cfg, non_interactive=False, dry_run=True,
            )
        # install_language_server NOT called in dry-run.
        inst.assert_not_called()
        out = capsys.readouterr().out
        assert "[dry-run] Would run:" in out


# --------------------------------------------------------------------- #
# main() wiring
# --------------------------------------------------------------------- #

class TestMainWiring:
    def test_main_calls_setup_lsp_engine_after_proxy_orchestrator(self):
        """Regression guard: ``main()`` must invoke ``_setup_lsp_engine``
        AFTER ``_setup_proxy_orchestrator`` (cfg-mutation order matters
        only loosely, but we want the LSP knob set before the cfg
        save below)."""
        src = (REPO / "install.py").read_text(encoding="utf-8")
        i_proxy = src.find("_setup_proxy_orchestrator(\n        cfg,")
        i_lsp = src.find("_setup_lsp_engine(\n        cfg,")
        i_save = src.find("save_config(cfg, cfg_path)")
        assert i_proxy > 0 and i_lsp > 0 and i_save > 0
        assert i_proxy < i_lsp < i_save


# --------------------------------------------------------------------- #
# On-disk-but-not-on-PATH USER PATH fix (Windows)
#
# When detection finds a binary at a known install location (e.g.
# clangd at C:\Program Files\LLVM\bin\clangd.exe from winget LLVM.LLVM)
# but it's NOT on the current shell's PATH, install.py offers to add
# the bin dir to the user's PATH via the existing
# ``_ensure_windows_user_path_includes`` helper. Skips on POSIX.
# Skips silently when the dir is already in the registry User PATH
# (process just hasn't picked it up — shell restart fixes it).
# Live-caught on pandorum 2026-05-21 after the user saw clangd's bin
# dir wasn't added to PATH despite winget reporting success.
# --------------------------------------------------------------------- #


class TestOnDiskPathFix:
    def test_offers_path_fix_for_on_disk_clangd(
            self, monkeypatch, tmp_path, capsys):
        """Accept the offer → _ensure_windows_user_path_includes
        is called with the LLVM bin dir."""
        monkeypatch.chdir(tmp_path)
        cfg = {}

        state = _state_all_installed()
        clangd_spec = state["clangd"].spec
        fake_llvm_bin = tmp_path / "Program Files" / "LLVM" / "bin"
        fake_llvm_bin.mkdir(parents=True)
        fake_clangd = fake_llvm_bin / "clangd.exe"
        fake_clangd.write_text("")
        state["clangd"] = ls.InstalledState(
            spec=clangd_spec, installed=False, binary_path=None,
            installer_for_missing=None,
            on_disk_path=str(fake_clangd),
        )

        monkeypatch.setattr(
            "builtins.input",
            _scripted_input([
                "y",   # accept "Add ... to your User PATH?"
                "",    # starter cclsp default Y
                "",    # enable engine default Y
            ]),
        )

        ensure_calls: list[str] = []

        def fake_ensure(wrapper_dir):
            ensure_calls.append(str(wrapper_dir))

        with patch.object(install, "_lsp_is_windows", return_value=True), \
             patch.object(install, "_ensure_windows_user_path_includes",
                          side_effect=fake_ensure), \
             patch.object(install, "_read_windows_user_path",
                          return_value=""), \
             patch.object(ls, "detect_language_servers",
                          return_value=state):
            install._setup_lsp_engine(
                cfg, non_interactive=False, dry_run=False,
            )

        out = capsys.readouterr().out
        assert "is installed at" in out
        assert "not on your User PATH" in out
        assert len(ensure_calls) == 1
        assert ensure_calls[0] == str(fake_llvm_bin)

    def test_declines_path_fix_keeps_state(
            self, monkeypatch, tmp_path, capsys):
        monkeypatch.chdir(tmp_path)
        cfg = {}

        state = _state_all_installed()
        clangd_spec = state["clangd"].spec
        fake_clangd = tmp_path / "LLVM" / "clangd.exe"
        fake_clangd.parent.mkdir(parents=True)
        fake_clangd.write_text("")
        state["clangd"] = ls.InstalledState(
            spec=clangd_spec, installed=False, binary_path=None,
            installer_for_missing=None,
            on_disk_path=str(fake_clangd),
        )

        monkeypatch.setattr(
            "builtins.input",
            _scripted_input(["n", "", ""]),
        )

        with patch.object(install, "_lsp_is_windows", return_value=True), \
             patch.object(install, "_ensure_windows_user_path_includes") as ensure_mock, \
             patch.object(install, "_read_windows_user_path",
                          return_value=""), \
             patch.object(ls, "detect_language_servers",
                          return_value=state):
            install._setup_lsp_engine(
                cfg, non_interactive=False, dry_run=False,
            )

        out = capsys.readouterr().out
        assert "[skipped]" in out
        ensure_mock.assert_not_called()

    def test_skipped_when_already_on_registry_user_path(
            self, monkeypatch, tmp_path, capsys):
        """If the bin dir is ALREADY in the registry User PATH (just
        not visible to this process yet), don't ask the user — print
        a one-liner saying the next shell will pick it up."""
        monkeypatch.chdir(tmp_path)
        cfg = {}

        state = _state_all_installed()
        clangd_spec = state["clangd"].spec
        fake_dir = tmp_path / "LLVM"
        fake_dir.mkdir()
        fake_clangd = fake_dir / "clangd.exe"
        fake_clangd.write_text("")
        state["clangd"] = ls.InstalledState(
            spec=clangd_spec, installed=False, binary_path=None,
            installer_for_missing=None,
            on_disk_path=str(fake_clangd),
        )

        registry_user_path = str(fake_dir) + r";C:\Other"

        monkeypatch.setattr(
            "builtins.input",
            _scripted_input(["", ""]),
        )

        with patch.object(install, "_lsp_is_windows", return_value=True), \
             patch.object(install, "_ensure_windows_user_path_includes") as ensure_mock, \
             patch.object(install, "_read_windows_user_path",
                          return_value=registry_user_path), \
             patch.object(ls, "detect_language_servers",
                          return_value=state):
            install._setup_lsp_engine(
                cfg, non_interactive=False, dry_run=False,
            )

        out = capsys.readouterr().out
        assert "already on" in out and "User PATH" in out
        ensure_mock.assert_not_called()

    def test_not_offered_on_posix(self, monkeypatch, tmp_path, capsys):
        """On Linux/macOS the on-disk PATH-fix is a no-op — the helper
        is gated on ``os.name == 'nt'`` because it writes to the
        Windows registry."""
        monkeypatch.chdir(tmp_path)
        cfg = {}

        # Even with on_disk_path set, POSIX should skip.
        state = _state_all_installed()
        clangd_spec = state["clangd"].spec
        fake_clangd = tmp_path / "LLVM" / "clangd"
        fake_clangd.parent.mkdir(parents=True)
        fake_clangd.write_text("")
        state["clangd"] = ls.InstalledState(
            spec=clangd_spec, installed=False, binary_path=None,
            installer_for_missing=None,
            on_disk_path=str(fake_clangd),
        )

        monkeypatch.setattr(
            "builtins.input",
            _scripted_input(["", ""]),
        )

        with patch.object(install, "_lsp_is_windows", return_value=False), \
             patch.object(install, "_ensure_windows_user_path_includes") as ensure_mock, \
             patch.object(ls, "detect_language_servers",
                          return_value=state):
            install._setup_lsp_engine(
                cfg, non_interactive=False, dry_run=False,
            )

        ensure_mock.assert_not_called()
        out = capsys.readouterr().out
        assert "not on your User PATH" not in out
