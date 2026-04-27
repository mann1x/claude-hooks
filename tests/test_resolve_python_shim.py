"""
Tests for ``bin/_resolve_python.sh`` — the shared Python resolver
sourced by every CLI shim.

Verifies the probe order and Windows-venv fallback the shim was
extended to support: repo-local .venv (POSIX or Windows layout) >
conda env > system python > nothing. Also verifies the
``CLAUDE_HOOKS_PY`` override.

The helper is sh-only, so these tests shell out via ``subprocess``
with a controlled ``HOME`` and ``PATH`` to make the probe paths
deterministic.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HELPER = REPO_ROOT / "bin" / "_resolve_python.sh"


def _make_fake_python(path: Path) -> None:
    """Create a no-op executable at ``path`` so the resolver's
    ``-x`` test passes. We're testing the probe, not interpreter
    behaviour, so a tiny shell script is enough."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _resolve(repo: Path, home: Path, *, env_overrides: dict | None = None,
             empty_path: bool = False) -> str:
    """Source the helper with controlled ``$REPO`` / ``$HOME`` and
    return the resolved ``$PY`` value."""
    script = f". {HELPER}; printf '%s' \"$PY\""
    if empty_path:
        # Need *some* PATH so the subprocess can find /bin/sh, but
        # nothing on it should provide python. /var/empty is a real
        # path on Debian / RHEL-style systems and is guaranteed empty.
        # We invoke /bin/sh by absolute path so PATH doesn't matter
        # for the launch itself.
        path_value = "/var/empty"
    else:
        path_value = os.environ.get("PATH", "")
    env = {
        "REPO": str(repo),
        "HOME": str(home),
        "PATH": path_value,
    }
    if env_overrides:
        env.update(env_overrides)
    out = subprocess.check_output(
        ["/bin/sh", "-c", script], env=env, text=True,
    )
    return out


@pytest.fixture
def repo_and_home(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    repo.mkdir()
    home.mkdir()
    return repo, home


class TestResolverOrder:
    def test_venv_posix_layout_wins(self, repo_and_home):
        repo, home = repo_and_home
        venv_py = repo / ".venv" / "bin" / "python"
        _make_fake_python(venv_py)
        # Conda also exists; venv must win because it's earlier in the probe.
        _make_fake_python(home / "anaconda3" / "envs" / "claude-hooks" / "bin" / "python")
        assert _resolve(repo, home) == str(venv_py)

    def test_venv_windows_layout_picked(self, repo_and_home):
        """Repo-local .venv on Windows uses Scripts/python.exe (no
        bin/python). The resolver must accept that layout — that's
        the whole point of the patch."""
        repo, home = repo_and_home
        win_py = repo / ".venv" / "Scripts" / "python.exe"
        _make_fake_python(win_py)
        assert _resolve(repo, home) == str(win_py)

    def test_anaconda_conda_env_when_no_venv(self, repo_and_home):
        repo, home = repo_and_home
        conda_py = home / "anaconda3" / "envs" / "claude-hooks" / "bin" / "python"
        _make_fake_python(conda_py)
        assert _resolve(repo, home) == str(conda_py)

    def test_miniconda_conda_env_when_no_anaconda(self, repo_and_home):
        repo, home = repo_and_home
        mini_py = home / "miniconda3" / "envs" / "claude-hooks" / "bin" / "python"
        _make_fake_python(mini_py)
        assert _resolve(repo, home) == str(mini_py)

    def test_falls_back_to_system_python3(self, repo_and_home):
        """No venv, no conda — should resolve to the system python3
        from PATH (which exists in the test environment)."""
        repo, home = repo_and_home
        out = _resolve(repo, home)
        assert out in ("python3", "python") or out.endswith(("/python3", "/python"))

    def test_empty_when_nothing_available(self, repo_and_home):
        repo, home = repo_and_home
        assert _resolve(repo, home, empty_path=True) == ""


class TestResolverOverride:
    def test_claude_hooks_py_env_var_wins(self, repo_and_home, tmp_path):
        """``CLAUDE_HOOKS_PY`` must short-circuit the probe entirely."""
        repo, home = repo_and_home
        # Make the conda candidate exist so we know the override is what
        # actually wins, not a coincidental fallback.
        _make_fake_python(home / "anaconda3" / "envs" / "claude-hooks" / "bin" / "python")
        custom = tmp_path / "custompy"
        _make_fake_python(custom)
        out = _resolve(repo, home, env_overrides={"CLAUDE_HOOKS_PY": str(custom)})
        assert out == str(custom)

    def test_invalid_override_falls_through_to_probe(self, repo_and_home):
        """If ``CLAUDE_HOOKS_PY`` points at a non-executable, the
        resolver must ignore it and continue to the normal probe
        (the override has to point at a real binary to count)."""
        repo, home = repo_and_home
        venv_py = repo / ".venv" / "bin" / "python"
        _make_fake_python(venv_py)
        out = _resolve(
            repo, home,
            env_overrides={"CLAUDE_HOOKS_PY": "/path/that/does/not/exist"},
        )
        assert out == str(venv_py)
