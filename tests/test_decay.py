"""Unit tests for claude_hooks.decay."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from claude_hooks import decay
from claude_hooks.decay import (
    _frequency_boost,
    _load_history,
    _prune_old,
    _recency_boost,
    _save_history,
    apply_decay,
    memory_hash,
    update_recalled,
)
from claude_hooks.providers.base import Memory


class TestMemoryHash:
    def test_stable_across_calls(self):
        m = Memory(text="hello world")
        assert memory_hash(m) == memory_hash(m)

    def test_different_text_different_hash(self):
        assert memory_hash(Memory(text="a")) != memory_hash(Memory(text="b"))

    def test_whitespace_stripped(self):
        a = Memory(text="  hello  ")
        b = Memory(text="hello")
        assert memory_hash(a) == memory_hash(b)

    def test_divergence_after_200_chars_detected(self):
        # Post-port: the composite key (prefix + length + suffix)
        # distinguishes memories that share a 200-char prefix but
        # differ in tail. Ported from thedotmack/claude-mem's
        # null-byte-delimited dedup key pattern.
        a = Memory(text="x" * 200 + "A" * 100)
        b = Memory(text="x" * 200 + "B" * 100)
        assert memory_hash(a) != memory_hash(b)

    def test_length_difference_detected(self):
        # Two texts with identical first 200 + identical last 50 but
        # different length must still hash differently.
        a = Memory(text="x" * 200 + "y" * 50 + "tail" + "z" * 50)
        b = Memory(text="x" * 200 + "y" * 50 + "tail" + "z" * 50 + "EXTRA")
        # Tail differs slightly after truncation to last-50; length
        # differs; composite key catches both.
        assert memory_hash(a) != memory_hash(b)


class TestRecencyBoost:
    def test_just_now_returns_one(self):
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        assert abs(_recency_boost(now, halflife_days=14) - 1.0) < 0.01

    def test_empty_returns_one(self):
        assert _recency_boost("", halflife_days=14) == 1.0

    def test_unparseable_returns_one(self):
        assert _recency_boost("not-a-date", halflife_days=14) == 1.0

    def test_halflife_matches(self):
        # At one halflife, exp(-ln(2)) = 0.5 → boost = 0.5 + 0.5*0.5 = 0.75
        past = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat(timespec="seconds")
        v = _recency_boost(past, halflife_days=14)
        assert 0.72 < v < 0.78

    def test_asymptotes_to_half(self):
        far_past = (datetime.now(timezone.utc) - timedelta(days=3650)).isoformat(timespec="seconds")
        v = _recency_boost(far_past, halflife_days=14)
        assert 0.50 <= v < 0.55


class TestFrequencyBoost:
    def test_zero_returns_one(self):
        assert _frequency_boost(0, cap=5) == 1.0

    def test_one_or_two_boosted(self):
        assert _frequency_boost(1, cap=5) == 1.1
        assert _frequency_boost(2, cap=5) == 1.1

    def test_at_cap_penalised(self):
        assert _frequency_boost(5, cap=5) == 0.6
        assert _frequency_boost(10, cap=5) == 0.6

    def test_monotone_down_between_2_and_cap(self):
        v3 = _frequency_boost(3, cap=5)
        v4 = _frequency_boost(4, cap=5)
        assert 0.6 <= v4 < v3 <= 1.1


class TestLoadSaveHistory:
    def test_roundtrip(self, tmp_path):
        path = tmp_path / "decay.json"
        data = {"abc": {"last_recalled": "2026-04-14", "recall_count": 3}}
        _save_history(path, data)
        loaded = _load_history(path)
        assert loaded == data

    def test_missing_file_returns_empty(self, tmp_path):
        assert _load_history(tmp_path / "missing.json") == {}

    def test_corrupt_file_returns_empty(self, tmp_path):
        path = tmp_path / "corrupt.json"
        path.write_text("not valid json {")
        assert _load_history(path) == {}


class TestPruneOld:
    def test_removes_old_entries(self):
        now = datetime.now(timezone.utc)
        old = (now - timedelta(days=120)).isoformat(timespec="seconds")
        recent = (now - timedelta(days=1)).isoformat(timespec="seconds")
        entries = {
            "old": {"last_recalled": old, "recall_count": 1},
            "new": {"last_recalled": recent, "recall_count": 1},
        }
        _prune_old(entries)
        assert "new" in entries
        assert "old" not in entries

    def test_removes_entries_with_no_timestamp(self):
        entries = {"ghost": {"recall_count": 1}}
        _prune_old(entries)
        assert entries == {}

    def test_removes_entries_with_bad_timestamp(self):
        entries = {"bad": {"last_recalled": "not-a-date", "recall_count": 1}}
        _prune_old(entries)
        assert entries == {}


class TestUpdateRecalled:
    def test_writes_entries_to_file(self, tmp_path, base_config):
        path = tmp_path / "decay.json"
        cfg = base_config(hooks={"user_prompt_submit": {"decay_file": str(path)}})
        mems = [Memory(text="one"), Memory(text="two")]
        update_recalled(mems, cfg)
        loaded = json.loads(path.read_text())["entries"]
        assert len(loaded) == 2
        for entry in loaded.values():
            assert entry["recall_count"] == 1

    def test_increments_existing(self, tmp_path, base_config):
        path = tmp_path / "decay.json"
        cfg = base_config(hooks={"user_prompt_submit": {"decay_file": str(path)}})
        mem = Memory(text="one")
        update_recalled([mem], cfg)
        update_recalled([mem], cfg)
        entries = json.loads(path.read_text())["entries"]
        assert list(entries.values())[0]["recall_count"] == 2


class TestApplyDecay:
    def test_empty_history_preserves_order(self, tmp_path, base_config):
        path = tmp_path / "decay.json"
        cfg = base_config(hooks={"user_prompt_submit": {"decay_file": str(path)}})
        mems = [Memory(text="a"), Memory(text="b"), Memory(text="c")]
        out = apply_decay(mems, cfg)
        assert [m.text for m in out] == ["a", "b", "c"]

    def test_over_recalled_sinks_to_bottom(self, tmp_path, base_config):
        path = tmp_path / "decay.json"
        cfg = base_config(hooks={"user_prompt_submit": {"decay_file": str(path)}})
        fresh = Memory(text="fresh-and-new")
        stale = Memory(text="overused-memory")
        # Recall the stale one 10 times to push it past the cap.
        for _ in range(10):
            update_recalled([stale], cfg)
        out = apply_decay([stale, fresh], cfg)
        assert out[0] is fresh
        assert out[-1] is stale
