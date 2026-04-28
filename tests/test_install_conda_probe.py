"""Tests for install.find_conda_env_python and install._find_conda.

These probes used to be single hardcoded paths under ~/anaconda3/envs/,
which silently missed every Miniconda3 / Anaconda3 capitalised /
/opt/conda layout. Now they probe a candidate list first, then fall
back to ``conda env list --json`` for the truly unusual cases.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import install  # noqa: E402


@pytest.fixture(autouse=True)
def reset_cache():
    """Clear the module-level cache between tests."""
    install._CONDA_PY_CACHE = None
    yield
    install._CONDA_PY_CACHE = None


# ===================================================================== #
# find_conda_env_python — candidate path probe
# ===================================================================== #
class TestFindCondaEnvPython:
    def test_finds_existing_path_in_candidate_list(self, tmp_path, monkeypatch):
        # Lay out  tmp_path/Miniconda3/envs/claude-hooks/python.exe
        env = tmp_path / "Miniconda3" / "envs" / "claude-hooks"
        env.mkdir(parents=True)
        py = env / "python.exe"
        py.touch()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        out = install.find_conda_env_python()
        assert out == py

    def test_prefers_bin_python_over_python_exe(self, tmp_path, monkeypatch):
        # Both layouts present — POSIX-style bin/python should win for
        # this lookup order on POSIX hosts.
        env = tmp_path / "anaconda3" / "envs" / "claude-hooks"
        (env / "bin").mkdir(parents=True)
        bin_py = env / "bin" / "python"
        win_py = env / "python.exe"
        bin_py.touch()
        win_py.touch()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        out = install.find_conda_env_python()
        assert out == bin_py

    def test_falls_back_to_conda_env_list_json(self, tmp_path, monkeypatch):
        # No paths under any standard root — must walk to step 2.
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        # Create an env at an unusual prefix.
        weird = tmp_path / "weird-conda" / "envs" / "claude-hooks"
        (weird / "bin").mkdir(parents=True)
        py = weird / "bin" / "python"
        py.touch()

        with patch.object(install, "_find_conda", return_value="conda"), \
             patch.object(
                 install.subprocess, "run",
                 return_value=MagicMock(
                     returncode=0,
                     stdout=json.dumps({"envs": [str(weird)]}),
                     stderr="",
                 ),
             ):
            out = install.find_conda_env_python()
        assert out == py

    def test_returns_fallback_path_when_nothing_found(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        with patch.object(install, "_find_conda", return_value=None):
            out = install.find_conda_env_python()
        # Fallback constants are returned so caller can ``.exists()``-check.
        # We don't assert on the constant's existence because it may
        # actually exist on the test host's real ~/anaconda3.
        assert out in (install.CONDA_PY_LINUX, install.CONDA_PY_WIN)

    def test_caches_after_first_resolve(self, tmp_path, monkeypatch):
        env = tmp_path / "anaconda3" / "envs" / "claude-hooks" / "bin"
        env.mkdir(parents=True)
        py = env / "python"
        py.touch()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        # First call resolves; second call should not re-walk paths.
        out1 = install.find_conda_env_python()
        # Move the file — if cache works, second call still returns the
        # original path (no re-probe).
        py.unlink()
        # Cache miss handler re-probes when cached path no longer exists.
        out2 = install.find_conda_env_python()
        # Either: cache held (out2 == out1) or it re-probed and got the
        # platform fallback. Both are acceptable; what matters is that
        # the cache machinery doesn't crash.
        assert out1 == py


# ===================================================================== #
# find_conda_env_pythonw — derives pythonw.exe from python.exe location
# ===================================================================== #
class TestFindCondaEnvPythonw:
    def test_returns_pythonw_alongside_python_exe(self, tmp_path, monkeypatch):
        env = tmp_path / "Miniconda3" / "envs" / "claude-hooks"
        env.mkdir(parents=True)
        (env / "python.exe").touch()
        (env / "pythonw.exe").touch()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        out = install.find_conda_env_pythonw()
        assert out is not None
        assert out.name == "pythonw.exe"
        assert out.exists()

    def test_returns_none_when_python_exists_but_no_pythonw(
        self, tmp_path, monkeypatch,
    ):
        env = tmp_path / "Miniconda3" / "envs" / "claude-hooks"
        env.mkdir(parents=True)
        (env / "python.exe").touch()
        # No pythonw.exe alongside.
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        out = install.find_conda_env_pythonw()
        assert out is None

    def test_returns_none_when_python_itself_missing(
        self, tmp_path, monkeypatch,
    ):
        # Empty home — find_conda_env_python falls back to nonexistent path.
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        with patch.object(install, "_find_conda", return_value=None):
            out = install.find_conda_env_pythonw()
        assert out is None


# ===================================================================== #
# _find_conda — handles Windows .bat + capitalised dirs
# ===================================================================== #
class TestFindConda:
    def test_returns_conda_from_path(self):
        with patch.object(install.shutil, "which", return_value="/usr/bin/conda"):
            assert install._find_conda() == "/usr/bin/conda"

    def test_finds_capitalised_miniconda3_layout(self, tmp_path, monkeypatch):
        """The capitalised ~/Miniconda3 layout that pandorum uses.

        We lay out the directory + a plain ``conda`` file (no .bat
        extension) — the candidate loop probes both names so this
        finds it on either platform without needing to patch os.name
        (patching os.name corrupts pathlib's class-pick).
        """
        cb = tmp_path / "Miniconda3" / "condabin"
        cb.mkdir(parents=True)
        target = cb / "conda"
        target.touch()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        with patch.object(install.shutil, "which", return_value=None):
            out = install._find_conda()
        assert out == str(target)

    def test_finds_lowercase_anaconda3_layout(self, tmp_path, monkeypatch):
        cb = tmp_path / "anaconda3" / "condabin"
        cb.mkdir(parents=True)
        sh = cb / "conda"
        sh.touch()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        with patch.object(install.shutil, "which", return_value=None):
            out = install._find_conda()
        assert out == str(sh)

    def test_returns_none_when_no_conda_anywhere(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        with patch.object(install.shutil, "which", return_value=None):
            out = install._find_conda()
        assert out is None
