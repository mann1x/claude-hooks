"""Tests for ``claude_hooks.get_advice.state``."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from claude_hooks.get_advice import state as ast


class TestSessionState:
    def test_save_and_load_round_trip(self, tmp_path: Path):
        sess = ast.SessionState(
            sid="abc",
            model="qwen3.5:cloud",
            ctx_max=64000,
            reset_threshold=0.85,
        )
        sess.messages.append({"role": "user", "content": "hi"})
        sess.cumulative_prompt_tokens = 1234
        sess.last_prompt_tokens = 1234
        sess.save(root=tmp_path)
        loaded = ast.load_session("abc", root=tmp_path)
        assert loaded is not None
        assert loaded.sid == "abc"
        assert loaded.model == "qwen3.5:cloud"
        assert loaded.ctx_max == 64000
        assert loaded.cumulative_prompt_tokens == 1234
        assert loaded.last_prompt_tokens == 1234
        assert loaded.messages == [{"role": "user", "content": "hi"}]

    def test_load_missing_returns_none(self, tmp_path: Path):
        assert ast.load_session("nope", root=tmp_path) is None

    def test_load_corrupt_returns_none(self, tmp_path: Path):
        d = tmp_path / "broken"
        d.mkdir()
        (d / "state.json").write_text("not json")
        assert ast.load_session("broken", root=tmp_path) is None


class TestCtxUsedPct:
    def test_returns_none_without_ctx_max(self):
        sess = ast.SessionState(sid="s", model="m", ctx_max=None,
                                reset_threshold=0.85)
        sess.last_prompt_tokens = 1000
        assert sess.ctx_used_pct() is None

    def test_uses_last_not_cumulative(self):
        # Cumulative could exceed ctx_max across many turns; the gate
        # is on the most recent prompt eval (what the model actually
        # sees on the next call).
        sess = ast.SessionState(sid="s", model="m", ctx_max=10000,
                                reset_threshold=0.85)
        sess.cumulative_prompt_tokens = 90000
        sess.last_prompt_tokens = 5000
        assert sess.ctx_used_pct() == pytest.approx(0.5)

    def test_reset_recommended_at_threshold(self):
        sess = ast.SessionState(sid="s", model="m", ctx_max=10000,
                                reset_threshold=0.85)
        sess.last_prompt_tokens = 8500
        assert sess.reset_recommended() is True

    def test_reset_not_recommended_below(self):
        sess = ast.SessionState(sid="s", model="m", ctx_max=10000,
                                reset_threshold=0.85)
        sess.last_prompt_tokens = 8000
        assert sess.reset_recommended() is False

    def test_reset_false_without_ctx(self):
        sess = ast.SessionState(sid="s", model="m", ctx_max=None,
                                reset_threshold=0.85)
        sess.last_prompt_tokens = 999999
        assert sess.reset_recommended() is False


class TestCleanup:
    def test_removes_old_sessions(self, tmp_path: Path):
        old = tmp_path / "old"
        old.mkdir()
        (old / "state.json").write_text("{}")
        # Backdate.
        import os
        ts = time.time() - 200000  # ~2.3 days ago
        os.utime(old / "state.json", (ts, ts))
        os.utime(old, (ts, ts))

        new = tmp_path / "new"
        new.mkdir()
        (new / "state.json").write_text("{}")

        removed = ast.cleanup_old_sessions(root=tmp_path,
                                           max_age_seconds=86400)
        assert removed == 1
        assert not old.exists()
        assert new.exists()

    def test_missing_root_is_zero(self, tmp_path: Path):
        assert ast.cleanup_old_sessions(root=tmp_path / "nope") == 0
