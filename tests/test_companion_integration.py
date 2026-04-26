"""Tests for claude_hooks.companion_integration — two-engine coordinator."""

from __future__ import annotations

import pytest

from claude_hooks import (
    axon_integration as ax,
    companion_integration as comp,
    gitnexus_integration as gn,
)


@pytest.fixture(autouse=True)
def _no_real_engines(monkeypatch):
    """Both engines invisible by default; tests opt back in selectively."""
    monkeypatch.setattr(ax.shutil, "which", lambda _: None)
    monkeypatch.setattr(ax, "_global_registry", lambda: None)
    monkeypatch.setattr(gn.shutil, "which", lambda _: None)
    monkeypatch.setattr(gn, "_global_registry", lambda: None)
    yield


# axon and gitnexus modules both import the same stdlib ``shutil``,
# so a single ``which`` patch routes both names. Tests build a per-call
# routing dict and reapply via this helper.
_WHICH_ROUTES: dict[str, str] = {}


def _apply_which_routes(monkeypatch):
    monkeypatch.setattr(ax.shutil, "which",
                        lambda name: _WHICH_ROUTES.get(name))


def _enable_axon(monkeypatch, fake_bin="/usr/bin/axon"):
    _WHICH_ROUTES["axon"] = fake_bin
    _apply_which_routes(monkeypatch)


def _enable_gitnexus(monkeypatch, fake_bin="/usr/bin/gitnexus"):
    _WHICH_ROUTES["gitnexus"] = fake_bin
    _apply_which_routes(monkeypatch)


@pytest.fixture(autouse=True)
def _reset_which_routes():
    _WHICH_ROUTES.clear()
    yield
    _WHICH_ROUTES.clear()


class TestStatus:
    def test_status_dual_shape(self, tmp_path):
        s = comp.status(tmp_path)
        assert "axon" in s
        assert "gitnexus" in s

    def test_status_reflects_individual_engines(self, monkeypatch, tmp_path):
        _enable_axon(monkeypatch)
        (tmp_path / ".axon").mkdir()
        s = comp.status(tmp_path)
        assert s["axon"]["project_indexed"] is True
        assert s["gitnexus"]["project_indexed"] is False


class TestSessionStartHint:
    def test_silent_when_neither_present(self, tmp_path):
        assert comp.session_start_hint(tmp_path) is None

    def test_axon_only(self, monkeypatch, tmp_path):
        _enable_axon(monkeypatch)
        (tmp_path / ".axon").mkdir()
        h = comp.session_start_hint(tmp_path)
        assert h is not None
        assert "axon" in h.lower()
        assert "gitnexus" not in h.lower()

    def test_gitnexus_only(self, monkeypatch, tmp_path):
        _enable_gitnexus(monkeypatch)
        (tmp_path / ".gitnexus").mkdir()
        h = comp.session_start_hint(tmp_path)
        assert h is not None
        assert "gitnexus" in h.lower()

    def test_both_indexed_axon_first(self, monkeypatch, tmp_path):
        _enable_axon(monkeypatch)
        _enable_gitnexus(monkeypatch)
        (tmp_path / ".axon").mkdir()
        (tmp_path / ".gitnexus").mkdir()
        h = comp.session_start_hint(tmp_path)
        assert h is not None
        # axon comes first in the recommendation order
        assert h.lower().index("axon") < h.lower().index("gitnexus")

    def test_installed_but_unindexed_emits_hint(self, monkeypatch, tmp_path):
        _enable_axon(monkeypatch)
        h = comp.session_start_hint(tmp_path)
        assert h is not None
        assert "axon analyze" in h.lower()


class TestReindex:
    """The two engine modules share the ``subprocess`` import, so we
    patch it once and tag spawns by inspecting argv[0]."""

    @staticmethod
    def _install_spawn_capture(monkeypatch):
        spawned: list[tuple[str, tuple]] = []

        def fake_popen(args, *_, **__):
            argv0 = args[0] if args else ""
            tag = "axon" if "axon" in argv0 else (
                "gitnexus" if "gitnexus" in argv0 else "other")
            spawned.append((tag, tuple(args)))

        # Both ax.subprocess and gn.subprocess are the same stdlib module,
        # so patching either patches both — pick one.
        monkeypatch.setattr(ax.subprocess, "Popen", fake_popen)
        return spawned

    def test_no_op_when_unmodified(self, tmp_path):
        comp.reindex_if_dirty_async(cwd=str(tmp_path), turn_modified=False)

    def test_routes_to_axon(self, monkeypatch, tmp_path):
        _enable_axon(monkeypatch)
        (tmp_path / ".axon").mkdir()
        (tmp_path / ".git").mkdir()
        spawned = self._install_spawn_capture(monkeypatch)
        comp.reindex_if_dirty_async(cwd=str(tmp_path), turn_modified=True)
        tags = {tag for tag, _ in spawned}
        assert "axon" in tags
        assert "gitnexus" not in tags

    def test_routes_to_gitnexus(self, monkeypatch, tmp_path):
        _enable_gitnexus(monkeypatch)
        (tmp_path / ".gitnexus").mkdir()
        (tmp_path / ".git").mkdir()
        spawned = self._install_spawn_capture(monkeypatch)
        comp.reindex_if_dirty_async(cwd=str(tmp_path), turn_modified=True)
        tags = {tag for tag, _ in spawned}
        assert "gitnexus" in tags
        assert "axon" not in tags

    def test_routes_to_both_when_both_indexed(self, monkeypatch, tmp_path):
        _enable_axon(monkeypatch)
        _enable_gitnexus(monkeypatch)
        (tmp_path / ".axon").mkdir()
        (tmp_path / ".gitnexus").mkdir()
        (tmp_path / ".git").mkdir()
        spawned = self._install_spawn_capture(monkeypatch)
        comp.reindex_if_dirty_async(cwd=str(tmp_path), turn_modified=True)
        tags = {tag for tag, _ in spawned}
        assert tags == {"axon", "gitnexus"}

    def test_silent_no_op_when_neither(self, tmp_path):
        comp.reindex_if_dirty_async(cwd=str(tmp_path), turn_modified=True)
