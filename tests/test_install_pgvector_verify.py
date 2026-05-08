"""Tests for ``install._verify_pgvector_dsn``.

The function has two paths:

1. **Fast path** — psycopg importable in the current interpreter; calls
   ``PgvectorProvider.verify`` directly.
2. **Fallback** — psycopg not importable; shells out to the conda env's
   python (where psycopg IS expected to be installed) and parses a
   small JSON status line.

The fallback fixes the misleading "FAILED (psycopg not installed)"
message the user got when invoking install.py with a python that
isn't the conda env's — common when ``python install.py`` resolves
to system py3 instead of ``~/anaconda3/envs/claude-hooks/bin/python``.
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


class TestFastPath:
    def test_psycopg_present_returns_provider_result(self, monkeypatch):
        # psycopg + provider both importable → fast path runs.
        fake_provider = MagicMock()
        fake_provider.verify = MagicMock(return_value=True)
        with patch.dict(sys.modules, {
            "psycopg": MagicMock(),
            "claude_hooks.providers.pgvector": MagicMock(
                PgvectorProvider=fake_provider),
        }):
            ok, reason = install._verify_pgvector_dsn(
                "postgresql://x@127.0.0.1/db")
        assert ok is True
        assert reason == "ok"
        fake_provider.verify.assert_called_once()

    def test_psycopg_present_provider_fails(self, monkeypatch):
        fake_provider = MagicMock()
        fake_provider.verify = MagicMock(return_value=False)
        with patch.dict(sys.modules, {
            "psycopg": MagicMock(),
            "claude_hooks.providers.pgvector": MagicMock(
                PgvectorProvider=fake_provider),
        }):
            ok, reason = install._verify_pgvector_dsn(
                "postgresql://x@127.0.0.1/db")
        assert ok is False
        assert "verify returned false" in reason


class TestSubprocessFallback:
    def test_falls_back_when_psycopg_missing(self, tmp_path, monkeypatch):
        fake_py = tmp_path / "envs" / "claude-hooks" / "bin" / "python"
        fake_py.parent.mkdir(parents=True, exist_ok=True)
        fake_py.write_text("")

        # Force the fast path to fail with ImportError.
        monkeypatch.setitem(sys.modules, "psycopg", None)
        rc = MagicMock()
        rc.returncode = 0
        rc.stdout = json.dumps({"ok": True, "reason": "ok"}) + "\n"
        rc.stderr = ""
        with patch("install.find_conda_env_python", return_value=fake_py), \
             patch("install.subprocess.run", return_value=rc) as run:
            ok, reason = install._verify_pgvector_dsn(
                "postgresql://x@127.0.0.1/db")
        assert ok is True
        assert reason == "ok"
        # Ensure we actually invoked the conda env's python with the dsn.
        args = run.call_args.args[0]
        assert args[0] == str(fake_py)
        assert args[-1] == "postgresql://x@127.0.0.1/db"
        assert "psycopg" in args[2]  # the embedded probe script

    def test_subprocess_returns_failure_reason(self, tmp_path, monkeypatch):
        fake_py = tmp_path / "envs" / "claude-hooks" / "bin" / "python"
        fake_py.parent.mkdir(parents=True, exist_ok=True)
        fake_py.write_text("")
        monkeypatch.setitem(sys.modules, "psycopg", None)
        rc = MagicMock()
        rc.returncode = 0
        rc.stdout = json.dumps(
            {"ok": False,
             "reason": "OperationalError: connection refused"}) + "\n"
        rc.stderr = ""
        with patch("install.find_conda_env_python", return_value=fake_py), \
             patch("install.subprocess.run", return_value=rc):
            ok, reason = install._verify_pgvector_dsn(
                "postgresql://x@127.0.0.1/db")
        assert ok is False
        assert "OperationalError" in reason
        assert "connection refused" in reason

    def test_no_conda_env_returns_clear_error(self, tmp_path, monkeypatch):
        # Conda env path doesn't exist on disk.
        missing = tmp_path / "no" / "such" / "python"
        monkeypatch.setitem(sys.modules, "psycopg", None)
        with patch("install.find_conda_env_python", return_value=missing):
            ok, reason = install._verify_pgvector_dsn(
                "postgresql://x@127.0.0.1/db")
        assert ok is False
        assert "conda env not found" in reason

    def test_subprocess_garbage_output(self, tmp_path, monkeypatch):
        fake_py = tmp_path / "envs" / "claude-hooks" / "bin" / "python"
        fake_py.parent.mkdir(parents=True, exist_ok=True)
        fake_py.write_text("")
        monkeypatch.setitem(sys.modules, "psycopg", None)
        rc = MagicMock(returncode=0, stdout="not-json\n", stderr="")
        with patch("install.find_conda_env_python", return_value=fake_py), \
             patch("install.subprocess.run", return_value=rc):
            ok, reason = install._verify_pgvector_dsn(
                "postgresql://x@127.0.0.1/db")
        assert ok is False
        assert "not JSON" in reason

    def test_subprocess_exception(self, tmp_path, monkeypatch):
        fake_py = tmp_path / "envs" / "claude-hooks" / "bin" / "python"
        fake_py.parent.mkdir(parents=True, exist_ok=True)
        fake_py.write_text("")
        monkeypatch.setitem(sys.modules, "psycopg", None)
        with patch("install.find_conda_env_python", return_value=fake_py), \
             patch("install.subprocess.run",
                   side_effect=OSError("boom")):
            ok, reason = install._verify_pgvector_dsn(
                "postgresql://x@127.0.0.1/db")
        assert ok is False
        assert "subprocess failed" in reason


# ----------------------- _pgvector_tables_present ----------------- #
# Same fallback shape as the verify function — without it, install.py
# running on system py3 returned False here even when the table
# existed, re-prompting schema init on every re-run (bug observed on
# solidpc 2026-05-06).

class TestTablesPresent:
    def test_subprocess_present_true(self, tmp_path, monkeypatch):
        fake_py = tmp_path / "envs" / "claude-hooks" / "bin" / "python"
        fake_py.parent.mkdir(parents=True, exist_ok=True)
        fake_py.write_text("")
        monkeypatch.setitem(sys.modules, "psycopg", None)
        rc = MagicMock(returncode=0,
                       stdout=json.dumps({"present": True}) + "\n",
                       stderr="")
        with patch("install.find_conda_env_python", return_value=fake_py), \
             patch("install.subprocess.run", return_value=rc) as run:
            ok = install._pgvector_tables_present(
                "postgresql://x@127.0.0.1/db", "memories_qwen3")
        assert ok is True
        # Confirm we called the conda env's python with both the dsn
        # and the table name.
        args = run.call_args.args[0]
        assert args[0] == str(fake_py)
        assert args[-2] == "postgresql://x@127.0.0.1/db"
        assert args[-1] == "memories_qwen3"

    def test_subprocess_present_false(self, tmp_path, monkeypatch):
        fake_py = tmp_path / "envs" / "claude-hooks" / "bin" / "python"
        fake_py.parent.mkdir(parents=True, exist_ok=True)
        fake_py.write_text("")
        monkeypatch.setitem(sys.modules, "psycopg", None)
        rc = MagicMock(returncode=0,
                       stdout=json.dumps({"present": False}) + "\n",
                       stderr="")
        with patch("install.find_conda_env_python", return_value=fake_py), \
             patch("install.subprocess.run", return_value=rc):
            ok = install._pgvector_tables_present(
                "postgresql://x@127.0.0.1/db", "missing_table")
        assert ok is False

    def test_no_conda_env_returns_false(self, tmp_path, monkeypatch):
        missing = tmp_path / "no" / "such" / "python"
        monkeypatch.setitem(sys.modules, "psycopg", None)
        with patch("install.find_conda_env_python", return_value=missing):
            ok = install._pgvector_tables_present(
                "postgresql://x@127.0.0.1/db", "memories_qwen3")
        assert ok is False

    def test_subprocess_garbage_returns_false(self, tmp_path, monkeypatch):
        fake_py = tmp_path / "envs" / "claude-hooks" / "bin" / "python"
        fake_py.parent.mkdir(parents=True, exist_ok=True)
        fake_py.write_text("")
        monkeypatch.setitem(sys.modules, "psycopg", None)
        rc = MagicMock(returncode=0, stdout="banana\n", stderr="")
        with patch("install.find_conda_env_python", return_value=fake_py), \
             patch("install.subprocess.run", return_value=rc):
            ok = install._pgvector_tables_present(
                "postgresql://x@127.0.0.1/db", "x")
        assert ok is False


# ----------------------- _init_pgvector_schema ------------------- #

class TestInitSchema:
    def test_subprocess_path_pipes_ddl_via_stdin(
            self, tmp_path, monkeypatch):
        fake_py = tmp_path / "envs" / "claude-hooks" / "bin" / "python"
        fake_py.parent.mkdir(parents=True, exist_ok=True)
        fake_py.write_text("")
        monkeypatch.setitem(sys.modules, "psycopg", None)
        rc = MagicMock(returncode=0, stdout="", stderr="")
        with patch("install.find_conda_env_python", return_value=fake_py), \
             patch("install.subprocess.run", return_value=rc) as run:
            install._init_pgvector_schema(
                "postgresql://x@127.0.0.1/db", model="qwen3")
        kwargs = run.call_args.kwargs
        # DDL flows in via stdin, dsn via argv.
        assert "CREATE EXTENSION" in (kwargs.get("input") or "")
        args = run.call_args.args[0]
        assert args[0] == str(fake_py)
        assert args[-1] == "postgresql://x@127.0.0.1/db"

    def test_subprocess_failure_raises(self, tmp_path, monkeypatch):
        fake_py = tmp_path / "envs" / "claude-hooks" / "bin" / "python"
        fake_py.parent.mkdir(parents=True, exist_ok=True)
        fake_py.write_text("")
        monkeypatch.setitem(sys.modules, "psycopg", None)
        rc = MagicMock(returncode=1, stdout="", stderr="oh no")
        with patch("install.find_conda_env_python", return_value=fake_py), \
             patch("install.subprocess.run", return_value=rc):
            with pytest.raises(RuntimeError, match="schema init failed"):
                install._init_pgvector_schema(
                    "postgresql://x@127.0.0.1/db", model="qwen3")

    def test_no_conda_env_raises(self, tmp_path, monkeypatch):
        missing = tmp_path / "no" / "such" / "python"
        monkeypatch.setitem(sys.modules, "psycopg", None)
        with patch("install.find_conda_env_python", return_value=missing):
            with pytest.raises(RuntimeError, match="conda env not found"):
                install._init_pgvector_schema(
                    "postgresql://x@127.0.0.1/db", model="qwen3")
