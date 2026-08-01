"""Tests for install._offer_pip_install_editable (v1.10.0+).

The function probes the conda env's Python with ``pip show
claude-hooks``, prompts the user for an editable install if absent,
and runs ``pip install -e .`` on accept. Covered branches:

- Conda env absent → silent skip.
- Package already pip-installed → silent skip with status line.
- Package missing + non-interactive → skip with manual-command hint.
- Package missing + interactive accept → pip install runs.
- Package missing + interactive decline → skip cleanly.
- Pip-install fails → surfaces stderr tail + manual retry hint.
- ``--dry-run`` honored when accepted (no subprocess spawn).
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import install  # noqa: E402


def _fake_conda_py(tmp_path: Path) -> Path:
    """Build a fake-but-existing conda env Python path for the test."""
    py = tmp_path / "envs" / "claude-hooks" / "bin" / "python"
    py.parent.mkdir(parents=True, exist_ok=True)
    py.touch()
    return py


class TestIsClaudeHooksPipInstalled(unittest.TestCase):
    def test_returns_false_when_python_missing(self):
        nonexistent = Path("/no/such/python")
        self.assertFalse(install._is_claude_hooks_pip_installed(nonexistent))

    def test_returns_true_on_pip_show_success(self, tmp_path=None):
        with patch("install.subprocess.run") as run_mock, \
             patch.object(Path, "exists", return_value=True):
            run_mock.return_value = MagicMock(returncode=0)
            self.assertTrue(install._is_claude_hooks_pip_installed(Path("/x/python")))

    def test_returns_false_on_pip_show_failure(self):
        with patch("install.subprocess.run") as run_mock, \
             patch.object(Path, "exists", return_value=True):
            run_mock.return_value = MagicMock(returncode=1)
            self.assertFalse(install._is_claude_hooks_pip_installed(Path("/x/python")))

    def test_returns_false_on_subprocess_exception(self):
        with patch("install.subprocess.run", side_effect=OSError("nope")), \
             patch.object(Path, "exists", return_value=True):
            self.assertFalse(install._is_claude_hooks_pip_installed(Path("/x/python")))


class TestOfferPipInstallEditable(unittest.TestCase):
    """Behavior of the interactive opt-in step."""

    def test_skip_when_conda_env_absent(self):
        # No conda env → silent skip (the earlier _check_conda_env
        # call surfaces the "Hook runtime: system python3" line).
        nonexistent = Path("/no/conda/here/python")
        with patch("install.find_conda_env_python", return_value=nonexistent), \
             patch("builtins.input") as input_mock, \
             patch("builtins.print") as print_mock:
            install._offer_pip_install_editable(
                non_interactive=False, dry_run=False,
            )
            input_mock.assert_not_called()
            # No "==> Package:" status line printed
            joined = "".join(c.args[0] for c in print_mock.call_args_list if c.args)
            self.assertNotIn("==> Package", joined)

    def test_skip_when_already_installed(self, tmp_path=None):
        with patch("install.find_conda_env_python") as fcp, \
             patch("install._is_claude_hooks_pip_installed", return_value=True), \
             patch("builtins.input") as input_mock, \
             patch("builtins.print") as print_mock:
            fcp.return_value = MagicMock(spec=Path, exists=lambda: True)
            install._offer_pip_install_editable(
                non_interactive=False, dry_run=False,
            )
            input_mock.assert_not_called()
            joined = "".join(c.args[0] for c in print_mock.call_args_list if c.args)
            self.assertIn("already pip-installed", joined)

    def test_non_interactive_skips_with_manual_hint(self):
        with patch("install.find_conda_env_python") as fcp, \
             patch("install._is_claude_hooks_pip_installed", return_value=False), \
             patch("builtins.input") as input_mock, \
             patch("builtins.print") as print_mock, \
             patch("install.subprocess.run") as run_mock:
            fcp.return_value = MagicMock(spec=Path, exists=lambda: True)
            install._offer_pip_install_editable(
                non_interactive=True, dry_run=False,
            )
            # No prompt
            input_mock.assert_not_called()
            # No pip install run
            run_mock.assert_not_called()
            # Manual hint surfaced
            joined = "".join(c.args[0] for c in print_mock.call_args_list if c.args)
            self.assertIn("--non-interactive: skipping", joined)
            self.assertIn("pip install -e", joined)

    def test_interactive_accept_runs_pip_install(self):
        with patch("install.find_conda_env_python") as fcp, \
             patch("install._is_claude_hooks_pip_installed", return_value=False), \
             patch("builtins.input", return_value="y"), \
             patch("install.subprocess.run") as run_mock:
            conda_py = MagicMock(spec=Path)
            conda_py.exists.return_value = True
            conda_py.parent.parent.name = "claude-hooks"
            fcp.return_value = conda_py
            run_mock.return_value = MagicMock(
                returncode=0,
                stdout="Successfully installed claude-hooks-1.10.0\n",
                stderr="",
            )

            install._offer_pip_install_editable(
                non_interactive=False, dry_run=False,
            )

            run_mock.assert_called_once()
            args = run_mock.call_args.args[0]
            self.assertEqual(args[1:], ["-m", "pip", "install", "-e", str(install.HERE)])

    def test_interactive_decline_does_not_run_pip(self):
        with patch("install.find_conda_env_python") as fcp, \
             patch("install._is_claude_hooks_pip_installed", return_value=False), \
             patch("builtins.input", return_value="n"), \
             patch("install.subprocess.run") as run_mock:
            fcp.return_value = MagicMock(spec=Path, exists=lambda: True)
            install._offer_pip_install_editable(
                non_interactive=False, dry_run=False,
            )
            run_mock.assert_not_called()

    def test_dry_run_does_not_spawn_pip(self):
        with patch("install.find_conda_env_python") as fcp, \
             patch("install._is_claude_hooks_pip_installed", return_value=False), \
             patch("builtins.input", return_value="y"), \
             patch("install.subprocess.run") as run_mock, \
             patch("builtins.print") as print_mock:
            fcp.return_value = MagicMock(spec=Path, exists=lambda: True)
            install._offer_pip_install_editable(
                non_interactive=False, dry_run=True,
            )
            run_mock.assert_not_called()
            joined = "".join(c.args[0] for c in print_mock.call_args_list if c.args)
            self.assertIn("[dry-run]", joined)

    def test_pip_install_failure_surfaces_stderr_tail(self):
        with patch("install.find_conda_env_python") as fcp, \
             patch("install._is_claude_hooks_pip_installed", return_value=False), \
             patch("builtins.input", return_value="y"), \
             patch("install.subprocess.run") as run_mock, \
             patch("builtins.print") as print_mock:
            conda_py = MagicMock(spec=Path)
            conda_py.exists.return_value = True
            conda_py.parent.parent.name = "claude-hooks"
            fcp.return_value = conda_py
            run_mock.return_value = MagicMock(
                returncode=1,
                stdout="",
                stderr="line1\nline2\nline3\nline4\nline5\nline6\nline7\n",
            )

            install._offer_pip_install_editable(
                non_interactive=False, dry_run=False,
            )

            joined = "".join(c.args[0] for c in print_mock.call_args_list if c.args)
            self.assertIn("failed", joined)
            # Stderr tail (last 5 lines) surfaced
            self.assertIn("line7", joined)
            # Retry hint included
            self.assertIn("retry manually", joined)

    def test_subprocess_oserror_does_not_crash(self):
        with patch("install.find_conda_env_python") as fcp, \
             patch("install._is_claude_hooks_pip_installed", return_value=False), \
             patch("builtins.input", return_value="y"), \
             patch("install.subprocess.run", side_effect=OSError("boom")), \
             patch("builtins.print") as print_mock:
            fcp.return_value = MagicMock(spec=Path, exists=lambda: True)
            # Should not raise
            install._offer_pip_install_editable(
                non_interactive=False, dry_run=False,
            )
            joined = "".join(c.args[0] for c in print_mock.call_args_list if c.args)
            self.assertIn("failed to run", joined)


if __name__ == "__main__":
    unittest.main()
