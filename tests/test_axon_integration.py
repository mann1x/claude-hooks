"""Tests for claude_hooks.axon_integration — single-engine module."""

from __future__ import annotations

import pytest

from claude_hooks import axon_integration as ax


@pytest.fixture(autouse=True)
def _no_real_axon(monkeypatch):
    """Make sure detection never picks up a real axon install."""
    monkeypatch.setattr(ax.shutil, "which", lambda _: None)
    monkeypatch.setattr(ax, "_global_registry", lambda: None)
    yield


class TestDetection:
    def test_unavailable_when_nothing_present(self):
        assert ax.is_available() is False
        assert ax.binary_path() is None

    def test_available_when_binary_on_path(self, monkeypatch, tmp_path):
        fake = tmp_path / "axon"
        fake.write_text("#!/bin/sh\necho 1.0.0\n")
        fake.chmod(0o755)
        monkeypatch.setattr(ax.shutil, "which",
                            lambda name: str(fake) if name == "axon" else None)
        assert ax.is_available() is True

    def test_available_when_global_registry_present(self, monkeypatch, tmp_path):
        reg = tmp_path / ".axon" / "repos"
        reg.mkdir(parents=True)
        monkeypatch.setattr(ax, "_global_registry", lambda: reg)
        assert ax.is_available() is True

    def test_indexed_when_project_dir_exists(self, tmp_path):
        (tmp_path / ".axon").mkdir()
        assert ax.is_indexed(tmp_path) is True

    def test_not_indexed_when_dir_missing(self, tmp_path):
        assert ax.is_indexed(tmp_path) is False


class TestStatus:
    def test_when_nothing_installed(self, tmp_path):
        s = ax.status(tmp_path)
        assert s["binary"] is None
        assert s["project_indexed"] is False
        assert s["version"] is None

    def test_when_indexed_and_installed(self, monkeypatch, tmp_path):
        fake = tmp_path / "axon"
        fake.write_text("#!/bin/sh\necho axon 1.0.0\n")
        fake.chmod(0o755)
        monkeypatch.setattr(ax.shutil, "which",
                            lambda name: str(fake) if name == "axon" else None)
        (tmp_path / ".axon").mkdir()
        s = ax.status(tmp_path)
        assert s["binary"] == str(fake)
        assert s["project_indexed"] is True
        assert s["version"] == "axon 1.0.0"


class TestHint:
    def test_silent_when_not_present(self, tmp_path):
        assert ax.session_start_hint(tmp_path) is None

    def test_indexed_hint_mentions_mcp_tools(self, tmp_path):
        (tmp_path / ".axon").mkdir()
        h = ax.session_start_hint(tmp_path)
        assert h is not None
        assert "axon" in h.lower()
        assert "mcp__axon__" in h
        assert "dead_code" in h  # the differentiator vs gitnexus

    def test_installed_unindexed_hint(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ax.shutil, "which",
                            lambda name: "/usr/bin/axon" if name == "axon" else None)
        h = ax.session_start_hint(tmp_path)
        assert h is not None
        assert "axon analyze" in h.lower()


class TestReindex:
    def test_no_op_when_unmodified(self, tmp_path):
        ax.reindex_if_dirty_async(cwd=str(tmp_path), turn_modified=False)

    def test_no_op_without_binary(self, tmp_path):
        (tmp_path / ".axon").mkdir()
        (tmp_path / ".git").mkdir()
        ax.reindex_if_dirty_async(cwd=str(tmp_path), turn_modified=True)

    def test_spawns_when_indexed_and_present(self, monkeypatch, tmp_path):
        bin_path = "/usr/bin/axon"
        monkeypatch.setattr(ax.shutil, "which",
                            lambda name: bin_path if name == "axon" else None)
        (tmp_path / ".axon").mkdir()
        (tmp_path / ".git").mkdir()

        spawned = []

        class _FakePopen:
            def __init__(self, args, **kwargs):
                spawned.append((args, kwargs))

        monkeypatch.setattr(ax.subprocess, "Popen", _FakePopen)
        ax.reindex_if_dirty_async(cwd=str(tmp_path), turn_modified=True)
        assert len(spawned) == 1
        args, _ = spawned[0]
        assert args == [bin_path, "analyze", "."]

    def test_lock_blocks_rapid_respawn(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ax.shutil, "which",
                            lambda name: "/usr/bin/axon" if name == "axon" else None)
        (tmp_path / ".axon").mkdir()
        (tmp_path / ".git").mkdir()
        spawned = []
        monkeypatch.setattr(ax.subprocess, "Popen",
                            lambda *a, **kw: spawned.append(a))
        ax.reindex_if_dirty_async(cwd=str(tmp_path), turn_modified=True)
        ax.reindex_if_dirty_async(cwd=str(tmp_path), turn_modified=True)
        assert len(spawned) == 1

    def test_no_op_when_not_indexed(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ax.shutil, "which",
                            lambda name: "/usr/bin/axon" if name == "axon" else None)
        (tmp_path / ".git").mkdir()
        spawned = []
        monkeypatch.setattr(ax.subprocess, "Popen",
                            lambda *a, **kw: spawned.append(a))
        ax.reindex_if_dirty_async(cwd=str(tmp_path), turn_modified=True)
        assert spawned == []

    def test_never_raises_on_garbage_cwd(self):
        ax.reindex_if_dirty_async(cwd="", turn_modified=True)
        ax.reindex_if_dirty_async(cwd="/no/such/path", turn_modified=True)
