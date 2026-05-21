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
from unittest.mock import MagicMock, patch

import pytest

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
        # every install prompt (5 missing Tier-1 specs to confirm).
        monkeypatch.setattr(
            "builtins.input",
            _scripted_input([
                "y",       # opt into install loop
                "y", "y", "y", "y", "y",  # install each missing Tier-1
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

        # 5 install dispatches (the 5 Tier-1 LSs other than pyright).
        names = [n for n, _ in install_calls]
        assert "gopls" in names
        assert "rust-analyzer" in names
        assert "clangd" in names
        assert "typescript-language-server" in names
        assert "bash-language-server" in names

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
        # declines gopls — others still get installed.
        monkeypatch.setattr(
            "builtins.input",
            _scripted_input([
                "y",       # opt into install loop
                "n",       # decline gopls
                "y", "y", "y", "y",  # install remaining 4
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
                "y",       # opt in
                "y", "y", "y", "y", "y",  # try all 5
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
                "y",       # opt in
                "y", "y", "y", "y",  # remaining 4 (gopls auto-skips)
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
                "y",                   # opt into install loop
                "y", "y", "y", "y", "y",  # 5 install confirmations
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
