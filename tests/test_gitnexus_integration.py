"""Tests for claude_hooks.gitnexus_integration — Tier 2."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from claude_hooks import gitnexus_integration as gn


@pytest.fixture(autouse=True)
def _no_real_gitnexus(monkeypatch):
    """Make sure detection never picks up a real gitnexus install on the
    host running the tests. Each test opts back in selectively."""
    monkeypatch.setattr(gn.shutil, "which", lambda _: None)
    monkeypatch.setattr(gn, "_global_registry", lambda: None)
    yield


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

class TestDetection:
    def test_unavailable_when_nothing_present(self):
        assert gn.is_available() is False
        assert gn.binary_path() is None

    def test_available_when_binary_on_path(self, monkeypatch, tmp_path):
        fake = tmp_path / "gitnexus"
        fake.write_text("#!/bin/sh\necho 1.2.3\n")
        fake.chmod(0o755)
        monkeypatch.setattr(gn.shutil, "which",
                            lambda name: str(fake) if name == "gitnexus" else None)
        assert gn.is_available() is True
        assert gn.binary_path() == str(fake)

    def test_available_when_global_registry_present(self, monkeypatch, tmp_path):
        reg = tmp_path / ".gitnexus" / "registry.json"
        reg.parent.mkdir()
        reg.write_text("{}")
        monkeypatch.setattr(gn, "_global_registry", lambda: reg)
        assert gn.is_available() is True

    def test_indexed_when_project_dir_exists(self, tmp_path):
        (tmp_path / ".gitnexus").mkdir()
        assert gn.is_indexed(tmp_path) is True

    def test_not_indexed_when_dir_missing(self, tmp_path):
        assert gn.is_indexed(tmp_path) is False


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

class TestStatus:
    def test_status_when_nothing_installed(self, tmp_path):
        s = gn.status(tmp_path)
        assert s["binary"] is None
        assert s["project_indexed"] is False
        assert s["version"] is None

    def test_status_when_indexed_and_installed(self, monkeypatch, tmp_path):
        fake = tmp_path / "gitnexus"
        fake.write_text("#!/bin/sh\necho gitnexus 1.0.0\n")
        fake.chmod(0o755)
        monkeypatch.setattr(gn.shutil, "which",
                            lambda name: str(fake) if name == "gitnexus" else None)
        (tmp_path / ".gitnexus").mkdir()
        s = gn.status(tmp_path)
        assert s["binary"] == str(fake)
        assert s["project_indexed"] is True
        assert s["version"] == "gitnexus 1.0.0"

    def test_version_probe_swallows_errors(self, monkeypatch):
        monkeypatch.setattr(gn.shutil, "which", lambda _: "/no/such/binary")

        def boom(*a, **kw):
            raise OSError("nope")
        monkeypatch.setattr(gn.subprocess, "run", boom)
        assert gn._probe_version() is None


# ---------------------------------------------------------------------------
# session_start_hint
# ---------------------------------------------------------------------------

class TestHint:
    def test_silent_when_not_present(self, tmp_path):
        assert gn.session_start_hint(tmp_path) is None

    def test_hint_when_indexed(self, tmp_path):
        (tmp_path / ".gitnexus").mkdir()
        h = gn.session_start_hint(tmp_path)
        assert h is not None
        assert "gitnexus" in h.lower()
        assert "mcp__gitnexus__" in h

    def test_hint_when_installed_but_unindexed(self, monkeypatch, tmp_path):
        monkeypatch.setattr(gn.shutil, "which",
                            lambda name: "/usr/bin/gitnexus" if name == "gitnexus" else None)
        h = gn.session_start_hint(tmp_path)
        assert h is not None
        assert "gitnexus init" in h.lower()


# ---------------------------------------------------------------------------
# reindex_if_dirty_async
# ---------------------------------------------------------------------------

class TestReindex:
    def test_no_op_when_unmodified(self, tmp_path):
        gn.reindex_if_dirty_async(cwd=str(tmp_path), turn_modified=False)
        # Nothing to assert beyond "no exception" — we mock spawn below.

    def test_no_op_when_no_binary(self, tmp_path):
        gn.reindex_if_dirty_async(cwd=str(tmp_path), turn_modified=True)
        # No binary = early return; verify nothing got spawned.

    def test_spawns_when_indexed_and_present(self, monkeypatch, tmp_path):
        # Stub the binary
        bin_path = "/usr/bin/gitnexus"
        monkeypatch.setattr(gn.shutil, "which",
                            lambda name: bin_path if name == "gitnexus" else None)
        # Mark project as indexed
        (tmp_path / ".gitnexus").mkdir()
        # Add a .git so _find_marker_root resolves
        (tmp_path / ".git").mkdir()

        spawned = []

        class _FakePopen:
            def __init__(self, args, **kwargs):
                spawned.append((args, kwargs))

        monkeypatch.setattr(gn.subprocess, "Popen", _FakePopen)

        gn.reindex_if_dirty_async(cwd=str(tmp_path), turn_modified=True)

        assert len(spawned) == 1
        args, _ = spawned[0]
        assert args == [bin_path, "analyze"]

    def test_lock_blocks_rapid_respawn(self, monkeypatch, tmp_path):
        monkeypatch.setattr(gn.shutil, "which",
                            lambda name: "/usr/bin/gitnexus" if name == "gitnexus" else None)
        (tmp_path / ".gitnexus").mkdir()
        (tmp_path / ".git").mkdir()
        spawned = []
        monkeypatch.setattr(gn.subprocess, "Popen",
                            lambda *a, **kw: spawned.append(a))

        gn.reindex_if_dirty_async(cwd=str(tmp_path), turn_modified=True)
        gn.reindex_if_dirty_async(cwd=str(tmp_path), turn_modified=True)
        # First call spawned; second hit fresh lock and bailed
        assert len(spawned) == 1

    def test_no_op_when_project_not_indexed(self, monkeypatch, tmp_path):
        monkeypatch.setattr(gn.shutil, "which",
                            lambda name: "/usr/bin/gitnexus" if name == "gitnexus" else None)
        (tmp_path / ".git").mkdir()  # is a project root, but no .gitnexus/
        spawned = []
        monkeypatch.setattr(gn.subprocess, "Popen",
                            lambda *a, **kw: spawned.append(a))
        gn.reindex_if_dirty_async(cwd=str(tmp_path), turn_modified=True)
        assert spawned == []

    def test_never_raises_on_garbage_cwd(self):
        # Exercises the outer try/except
        gn.reindex_if_dirty_async(cwd="", turn_modified=True)
        gn.reindex_if_dirty_async(cwd="/no/such/path/at/all", turn_modified=True)


# ---------------------------------------------------------------------------
# CLI: code_graph companions
# ---------------------------------------------------------------------------

class TestCompanionsCli:
    def test_companions_prints_status(self, tmp_path):
        repo_root = Path(__file__).resolve().parent.parent
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            [str(repo_root), env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
        # Need a git dir for project_root() to work
        subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=True)
        out = subprocess.run(
            [sys.executable, "-m", "claude_hooks.code_graph",
             "companions", "--root", str(tmp_path)],
            capture_output=True, text=True, timeout=10,
            cwd=str(tmp_path), env=env,
        )
        assert out.returncode == 0, out.stderr
        import json
        report = json.loads(out.stdout)
        assert "code_graph" in report
        assert "gitnexus" in report
