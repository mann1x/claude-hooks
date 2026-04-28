"""Tests for the HyDE expansion cache (Tier 1.2 latency reduction)."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from claude_hooks.hyde_cache import (
    DEFAULT_MAX_ENTRIES,
    DEFAULT_TTL_SECONDS,
    clear,
    get,
    put,
    _key,
)


@pytest.fixture
def cache_path(tmp_path):
    return tmp_path / "hyde-cache.json"


class TestKey:
    def test_key_is_stable(self):
        a = _key("hello", "qwen3.5:2b", "grounding")
        b = _key("hello", "qwen3.5:2b", "grounding")
        assert a == b

    def test_key_differs_by_model(self):
        a = _key("hello", "qwen3.5:2b", "")
        b = _key("hello", "gemma4:e2b", "")
        assert a != b

    def test_key_differs_by_grounding(self):
        a = _key("hello", "m", "grounding-a")
        b = _key("hello", "m", "grounding-b")
        assert a != b

    def test_key_differs_by_prompt(self):
        a = _key("hello", "m", "g")
        b = _key("world", "m", "g")
        assert a != b


class TestGetPut:
    def test_miss_on_empty_cache(self, cache_path):
        assert get("p", "m", path=cache_path) is None

    def test_round_trip(self, cache_path):
        put("p", "m", "expanded text", path=cache_path)
        assert get("p", "m", path=cache_path) == "expanded text"

    def test_miss_on_different_model(self, cache_path):
        put("p", "m1", "exp1", path=cache_path)
        assert get("p", "m2", path=cache_path) is None

    def test_grounding_isolation(self, cache_path):
        put("p", "m", "exp-a", grounding="ga", path=cache_path)
        put("p", "m", "exp-b", grounding="gb", path=cache_path)
        assert get("p", "m", grounding="ga", path=cache_path) == "exp-a"
        assert get("p", "m", grounding="gb", path=cache_path) == "exp-b"

    def test_empty_expansion_not_stored(self, cache_path):
        put("p", "m", "", path=cache_path)
        assert get("p", "m", path=cache_path) is None
        assert not cache_path.exists()

    def test_corrupt_cache_is_treated_as_empty(self, cache_path):
        cache_path.write_text("not json {{{")
        assert get("p", "m", path=cache_path) is None

    def test_non_dict_cache_is_treated_as_empty(self, cache_path):
        cache_path.write_text(json.dumps([1, 2, 3]))
        assert get("p", "m", path=cache_path) is None


class TestTTL:
    def test_expired_entry_returns_none(self, cache_path):
        # Store with a fake "now" 2 days ago
        old = time.time() - 2 * 86400
        put("p", "m", "old", path=cache_path, now=old)
        assert get("p", "m", path=cache_path, ttl_seconds=86400) is None

    def test_fresh_entry_returns_value(self, cache_path):
        recent = time.time() - 60
        put("p", "m", "recent", path=cache_path, now=recent)
        assert get("p", "m", path=cache_path, ttl_seconds=86400) == "recent"


class TestEviction:
    def test_lru_evicts_oldest(self, cache_path):
        # 3 entries with monotonic timestamps; cap at 2.
        base = time.time()
        put("p1", "m", "exp1", path=cache_path, max_entries=2, now=base)
        put("p2", "m", "exp2", path=cache_path, max_entries=2, now=base + 1)
        put("p3", "m", "exp3", path=cache_path, max_entries=2, now=base + 2)
        # exp1 should be evicted
        assert get("p1", "m", path=cache_path) is None
        assert get("p2", "m", path=cache_path) == "exp2"
        assert get("p3", "m", path=cache_path) == "exp3"

    def test_below_cap_keeps_all(self, cache_path):
        for i in range(5):
            put(f"p{i}", "m", f"exp{i}", path=cache_path, max_entries=10)
        for i in range(5):
            assert get(f"p{i}", "m", path=cache_path) == f"exp{i}"


class TestClear:
    def test_clear_removes_file(self, cache_path):
        put("p", "m", "exp", path=cache_path)
        assert cache_path.exists()
        clear(cache_path)
        assert not cache_path.exists()

    def test_clear_missing_file_is_noop(self, cache_path):
        clear(cache_path)  # must not raise


class TestExpandQueryUsesCache:
    """Integration: hyde.expand_query should hit the cache on second call."""

    def test_expand_query_cache_hit_skips_ollama(self, cache_path, monkeypatch):
        from claude_hooks import hyde, hyde_cache

        # Pre-seed the cache with the same hash hyde would compute
        monkeypatch.setattr(hyde_cache, "DEFAULT_CACHE_PATH", cache_path)
        # Patch the path the hyde module looks up via fresh import
        import claude_hooks.hyde_cache as hc
        hc.DEFAULT_CACHE_PATH = cache_path

        put("the prompt", "gemma4:e2b", "cached expansion", path=cache_path)

        calls = []

        def fake_ollama(**kw):
            calls.append(kw["model"])
            return "should not be reached"

        monkeypatch.setattr(hyde, "_call_ollama", fake_ollama)

        # Patch the cache_get lookup default path to point at our tmp file
        original_get = hyde_cache.get

        def patched_get(prompt, model, grounding="", **kw):
            kw.setdefault("path", cache_path)
            return original_get(prompt, model, grounding, **kw)

        monkeypatch.setattr(hyde_cache, "get", patched_get)

        out = hyde.expand_query("the prompt")
        assert out == "cached expansion"
        assert calls == [], "Ollama should not have been called on cache hit"

    def test_expand_query_cache_miss_calls_ollama_and_stores(
        self, cache_path, monkeypatch,
    ):
        from claude_hooks import hyde, hyde_cache

        calls = []

        def fake_ollama(**kw):
            calls.append(kw["model"])
            return "fresh expansion"

        monkeypatch.setattr(hyde, "_call_ollama", fake_ollama)

        original_get = hyde_cache.get
        original_put = hyde_cache.put

        def patched_get(prompt, model, grounding="", **kw):
            kw.setdefault("path", cache_path)
            return original_get(prompt, model, grounding, **kw)

        def patched_put(prompt, model, expansion, **kw):
            kw.setdefault("path", cache_path)
            return original_put(prompt, model, expansion, **kw)

        monkeypatch.setattr(hyde_cache, "get", patched_get)
        monkeypatch.setattr(hyde_cache, "put", patched_put)

        out = hyde.expand_query("new prompt")
        assert out == "fresh expansion"
        assert calls, "Ollama should have been called on cache miss"
        # Stored back
        assert get("new prompt", calls[0], path=cache_path) == "fresh expansion"

    def test_expand_query_cache_disabled_always_calls_ollama(
        self, cache_path, monkeypatch,
    ):
        from claude_hooks import hyde

        calls = []

        def fake_ollama(**kw):
            calls.append(kw["model"])
            return "fresh"

        monkeypatch.setattr(hyde, "_call_ollama", fake_ollama)
        # cache_enabled=False — cache must not be touched
        out = hyde.expand_query("p", cache_enabled=False)
        assert out == "fresh"
        assert calls == ["gemma4:e2b"]
