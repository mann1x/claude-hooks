"""Tests for ``install.find_conda_env_python``.

The function takes an ``env_name`` and returns a Path the caller
``.exists()``-checks. Two regression-critical guarantees:

1. The result's path includes ``env_name`` — never the hardcoded
   main-env name. Otherwise calling
   ``find_conda_env_python("claude-hooks-consultants")`` on a host
   that has ``claude-hooks`` but no consultants env returns a path
   whose ``.exists()`` is True (because the main env exists),
   silently misleading ``_install_consultants`` into pip-installing
   the heavy LangChain stack into the wrong env. Caught on solidpc
   2026-05-06 — install report read
   ``claude-hooks-consultants env exists at /root/anaconda3/envs/
   claude-hooks/bin/python`` and proceeded to pollute the main env.

2. The cache is keyed by ``env_name`` so a prior successful call
   with the default doesn't leak into a subsequent call with a
   different env_name. Already covered by the v1.0.5-dev fix in
   commit 11074be; we re-test here to keep the regression tight.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import install  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_cache():
    """Clear the per-process cache so tests don't see stale state."""
    install._CONDA_PY_CACHE = {}
    yield
    install._CONDA_PY_CACHE = {}


class TestFallbackRespectsEnvName:
    """When no env is found on disk and conda env list yields
    nothing, the fallback path must include env_name — NOT the
    canonical main-env path."""

    def test_default_fallback_uses_main_name(self, tmp_path, monkeypatch):
        monkeypatch.setattr(install.Path, "home", lambda: tmp_path)
        monkeypatch.setattr(install, "_find_conda", lambda: None)
        result = install.find_conda_env_python()
        # Default env_name is CONDA_ENV_NAME = "claude-hooks".
        assert install.CONDA_ENV_NAME in str(result)
        assert not result.exists()  # the env doesn't exist on disk

    def test_consultants_fallback_uses_consultants_name(
            self, tmp_path, monkeypatch):
        # SolidPC bug: with main env present but consultants not,
        # find_conda_env_python("claude-hooks-consultants") used to
        # return the main env's python (fallback) which exists()-ed
        # to True. Now it must return a path under the consultants
        # env name — non-existent — so the caller knows to create it.
        monkeypatch.setattr(install.Path, "home", lambda: tmp_path)
        monkeypatch.setattr(install, "_find_conda", lambda: None)
        result = install.find_conda_env_python("claude-hooks-consultants")
        assert "claude-hooks-consultants" in str(result)
        assert install.CONDA_ENV_NAME != "claude-hooks-consultants"
        # Different env_name => different path
        main = install.find_conda_env_python()
        assert result != main
        assert not result.exists()

    def test_arbitrary_env_name_returns_canonical_path(
            self, tmp_path, monkeypatch):
        monkeypatch.setattr(install.Path, "home", lambda: tmp_path)
        monkeypatch.setattr(install, "_find_conda", lambda: None)
        result = install.find_conda_env_python("my-custom-env")
        assert "my-custom-env" in str(result)
        assert "envs" in str(result)


class TestCacheKeyedByEnvName:
    """Cache must NOT leak across env_name."""

    def test_cache_does_not_leak_across_envs(self, tmp_path, monkeypatch):
        # Simulate finding the main env on disk.
        monkeypatch.setattr(install.Path, "home", lambda: tmp_path)
        monkeypatch.setattr(install, "_find_conda", lambda: None)
        main_env = (tmp_path / "anaconda3" / "envs"
                    / install.CONDA_ENV_NAME / "bin" / "python")
        main_env.parent.mkdir(parents=True, exist_ok=True)
        main_env.write_text("")

        # Hot the cache for the main env.
        first = install.find_conda_env_python()
        assert first == main_env

        # Now ask for a DIFFERENT env. It must NOT return the cached
        # main env path.
        second = install.find_conda_env_python("claude-hooks-consultants")
        assert second != main_env
        assert "claude-hooks-consultants" in str(second)
        assert not second.exists()
