"""External tool dependencies of language servers.

A missing *server* is visible: the detection table says MISSING and
that language simply has no diagnostics. A missing *dependency* is not
— the server installs, starts, handshakes, reports healthy, and then
returns an empty diagnostic list for every file, which is
indistinguishable from clean code. bash-language-server without
shellcheck sat in that state on both hosts until 2026-09-13.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from claude_hooks import lang_servers as L


class TestDependencyDeclaration(unittest.TestCase):
    def test_bash_language_server_requires_shellcheck(self) -> None:
        spec = next(s for s in L.SPECS if s.name == "bash-language-server")
        self.assertIn("shellcheck", spec.requires)

    def test_every_declared_requirement_has_a_tool_spec(self) -> None:
        """A `requires` naming something absent from TOOL_SPECS would
        be silently unenforceable."""
        for spec in L.SPECS:
            for dep in spec.requires:
                self.assertIn(dep, L.TOOL_SPECS, f"{spec.name} -> {dep}")

    def test_every_tool_spec_is_installable_somewhere(self) -> None:
        for name, spec in L.TOOL_SPECS.items():
            paths = [i for i in spec.installers
                     if name in L.TOOL_INSTALL_COMMANDS.get(i, {})]
            self.assertTrue(paths, f"{name} has no install command anywhere")

    def test_windows_prefers_winget_over_scoop(self) -> None:
        """winget is in-box on Win10 1909+; scoop needs an opt-in
        install first. Same rationale the clangd spec already states."""
        for name, spec in L.TOOL_SPECS.items():
            order = [i for i in spec.installers
                     if i in (L.Installer.WINGET, L.Installer.SCOOP)]
            if len(order) == 2:
                self.assertEqual(order[0], L.Installer.WINGET, name)
        for spec in L.SPECS:
            order = [i for i in spec.installers
                     if i in (L.Installer.WINGET, L.Installer.SCOOP)]
            if len(order) == 2:
                self.assertEqual(order[0], L.Installer.WINGET, spec.name)

    def test_msys2_is_not_offered_for_shellcheck(self) -> None:
        """msys2's package db carries no shellcheck — offering pacman
        would be an install that cannot succeed."""
        self.assertNotIn(
            "pacman",
            [i.value for i in L.TOOL_SPECS["shellcheck"].installers],
        )


class TestDetectTools(unittest.TestCase):
    def _state(self, *, bash_installed: bool):
        spec = next(s for s in L.SPECS if s.name == "bash-language-server")
        return {"bash-language-server": L.InstalledState(
            spec=spec, installed=bash_installed,
            binary_path=("/usr/bin/bash-language-server"
                         if bash_installed else None),
            installer_for_missing=None,
        )}

    def test_needed_by_names_the_server_that_is_silently_broken(self) -> None:
        with patch.object(L.shutil, "which", return_value=None):
            tools = L.detect_tools(self._state(bash_installed=True))
        self.assertEqual(tools["shellcheck"].needed_by,
                         ("bash-language-server",))
        self.assertFalse(tools["shellcheck"].installed)

    def test_uninstalled_server_does_not_raise_a_dependency_flag(self) -> None:
        """A missing dependency for a server you don't have is not a
        problem, and flagging it is noise."""
        with patch.object(L.shutil, "which", return_value=None):
            tools = L.detect_tools(self._state(bash_installed=False))
        self.assertEqual(tools["shellcheck"].needed_by, ())

    def test_present_tool_reports_its_path(self) -> None:
        with patch.object(L.shutil, "which", return_value="/usr/bin/shellcheck"):
            tools = L.detect_tools(self._state(bash_installed=True))
        self.assertTrue(tools["shellcheck"].installed)
        self.assertEqual(tools["shellcheck"].path, "/usr/bin/shellcheck")
        self.assertIsNone(tools["shellcheck"].installer_for_missing)


class TestInstallTool(unittest.TestCase):
    def test_dry_run_reports_the_command(self) -> None:
        ok, msg = L.install_tool(L.TOOL_SPECS["shellcheck"],
                                 L.Installer.APT, dry_run=True)
        self.assertTrue(ok)
        self.assertIn("shellcheck", msg)

    def test_unregistered_combination_fails_cleanly(self) -> None:
        ok, msg = L.install_tool(L.TOOL_SPECS["shellcheck"],
                                 L.Installer.NPM, dry_run=True)
        self.assertFalse(ok)
        self.assertIn("no install command", msg)

    def test_shares_the_manager_trap_handling_with_servers(self) -> None:
        """scoop exits 0 on a missing manifest. Both install paths must
        classify that as failure, or the installer reports [ok] for an
        install that did nothing."""
        class R:
            returncode = 0
            stdout = "Couldn't find manifest for 'shellcheck'"
            stderr = ""
        with patch.object(L.subprocess, "run", return_value=R()), \
             patch.object(L.shutil, "which", return_value="/usr/bin/scoop"):
            ok, msg = L.install_tool(L.TOOL_SPECS["shellcheck"],
                                     L.Installer.SCOOP)
        self.assertFalse(ok)
        self.assertIn("manifest", msg)


if __name__ == "__main__":
    unittest.main()


class TestRepoAvailabilityIsProbedNotAssumed(unittest.TestCase):
    """The installer runs on whatever distro the user has. Debian 11
    carries neither lua-language-server nor zls; Debian 13 carries the
    first. Assuming either answer produces a confident install command
    that cannot work."""

    def _apt_policy(self, candidate: str):
        class R:
            returncode = 0
            stdout = f"pkg:\n  Installed: (none)\n  Candidate: {candidate}\n"
            stderr = ""
        return R()

    def test_absent_package_reports_false(self) -> None:
        with patch.object(L.shutil, "which", return_value="/usr/bin/apt-cache"), \
             patch.object(L.subprocess, "run",
                          return_value=self._apt_policy("(none)")):
            self.assertFalse(L.apt_has_package("lua-language-server"))

    def test_available_package_reports_true(self) -> None:
        with patch.object(L.shutil, "which", return_value="/usr/bin/apt-cache"), \
             patch.object(L.subprocess, "run",
                          return_value=self._apt_policy("0.7.1-1")):
            self.assertTrue(L.apt_has_package("shellcheck"))

    def test_empty_output_reports_false(self) -> None:
        """apt-cache exits 0 with no output for an unknown package."""
        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        with patch.object(L.shutil, "which", return_value="/usr/bin/apt-cache"), \
             patch.object(L.subprocess, "run", return_value=R()):
            self.assertFalse(L.apt_has_package("zls"))

    def test_selector_skips_apt_when_the_package_is_absent(self) -> None:
        """The whole point: don't offer an install that must fail."""
        spec = next(s for s in L.SPECS if s.name == "clangd")
        with patch.object(L, "_manager_available", return_value=True), \
             patch.object(L, "_installer_allowed_on", return_value=True), \
             patch.object(L, "apt_has_package", return_value=False), \
             patch.object(L, "dnf_has_package", return_value=False):
            chosen = L.select_installer_for(spec)
        self.assertNotIn(chosen, (L.Installer.APT, L.Installer.DNF))

    def test_non_repo_managers_are_not_probed(self) -> None:
        """npm/scoop/winget name a verified package id; probing each
        would cost a network round trip per candidate."""
        called = []
        with patch.object(L, "apt_has_package",
                          side_effect=lambda p, **k: called.append(p) or True):
            self.assertTrue(L._repo_has_package(
                L.Installer.NPM, ["npm", "install", "-g", "x"]))
        self.assertEqual(called, [])


class TestReleaseInstall(unittest.TestCase):
    def test_registered_sources_resolve_a_linux_asset(self) -> None:
        for name in ("lua-language-server", "zls"):
            self.assertIn(name, L.RELEASE_SOURCES)
            rel = L.RELEASE_SOURCES[name]
            self.assertTrue(rel.repo and rel.bin_subpath)

    def test_unknown_name_fails_cleanly(self) -> None:
        ok, msg = L.install_from_release("nope", dry_run=True)
        self.assertFalse(ok)
        self.assertIn("no release source", msg)

    def test_prefix_is_overridable_by_environment(self) -> None:
        """Hosts disagree about where non-packaged software belongs;
        solidpc keeps it on /shared/dev, a different disk to /usr/local."""
        with patch.dict(L.os.environ,
                        {"CLAUDE_HOOKS_LSP_PREFIX": "/shared/dev/lsp-servers"}):
            self.assertEqual(L.release_prefix(), "/shared/dev/lsp-servers")

    def test_default_prefix_when_unset(self) -> None:
        with patch.dict(L.os.environ, {}, clear=False):
            L.os.environ.pop("CLAUDE_HOOKS_LSP_PREFIX", None)
            self.assertEqual(L.release_prefix(), L.DEFAULT_RELEASE_PREFIX)

    def test_tar_member_escaping_the_prefix_is_refused(self) -> None:
        """A tar member may name ../ or an absolute path; honouring it
        writes outside the prefix the operator chose."""
        import tarfile, tempfile, os as _os
        with tempfile.TemporaryDirectory() as td:
            payload = _os.path.join(td, "evil.txt")
            with open(payload, "w") as f:
                f.write("x")
            arc = _os.path.join(td, "evil.tar")
            with tarfile.open(arc, "w") as tf:
                tf.add(payload, arcname="../escaped.txt")
            dest = _os.path.join(td, "dest")
            _os.makedirs(dest)
            with self.assertRaises(RuntimeError):
                L._safe_extract(arc, dest)
            self.assertFalse(_os.path.exists(_os.path.join(td, "escaped.txt")))
