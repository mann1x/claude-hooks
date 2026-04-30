"""Tests for the caliber-grounding-proxy cross-provider recall layer.

The recall module fans out across whatever providers ``build_providers``
yields, so most tests inject a fake provider list via the module-level
cache rather than mocking the whole config + dispatcher path.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from unittest.mock import patch

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from claude_hooks.caliber_proxy import recall  # noqa: E402
from claude_hooks.providers.base import Memory  # noqa: E402


# ===================================================================== #
# Helpers
# ===================================================================== #
@dataclass
class _FakeProvider:
    name: str
    display_name: str
    hits: list[Memory]
    raise_on_recall: bool = False
    raise_on_store: bool = False
    last_stored: Optional[tuple[str, dict]] = None

    def recall(self, query: str, k: int = 5) -> list[Memory]:  # noqa: ARG002
        if self.raise_on_recall:
            raise RuntimeError(f"{self.name} recall down")
        return list(self.hits)

    def store(self, content: str, metadata: Optional[dict] = None) -> None:
        if self.raise_on_store:
            raise RuntimeError(f"{self.name} store down")
        self.last_stored = (content, dict(metadata or {}))


@pytest.fixture(autouse=True)
def _reset_recall_cache():
    """Drop any cached provider list between tests."""
    recall.reset_state_for_tests()
    yield
    recall.reset_state_for_tests()


def _inject_providers(providers: list) -> None:
    """Bypass build_providers + load_config — just stuff the cache."""
    recall._PROVIDERS = providers
    recall._LAST_FILTER = None


# ===================================================================== #
# load_config
# ===================================================================== #
class TestLoadConfig:
    def test_defaults(self, monkeypatch):
        for k in [
            "CALIBER_GROUNDING_RECALL_ENABLED",
            "CALIBER_GROUNDING_RECALL_K",
            "CALIBER_GROUNDING_RECALL_STORE",
            "CALIBER_GROUNDING_RECALL_PROVIDERS",
        ]:
            monkeypatch.delenv(k, raising=False)
        cfg = recall.load_config()
        assert cfg.enabled is True
        assert cfg.k == 5
        assert cfg.store_back is True
        assert cfg.providers_filter is None

    def test_disable_via_env(self, monkeypatch):
        monkeypatch.setenv("CALIBER_GROUNDING_RECALL_ENABLED", "0")
        cfg = recall.load_config()
        assert cfg.enabled is False

    def test_providers_filter_parsed(self, monkeypatch):
        monkeypatch.setenv("CALIBER_GROUNDING_RECALL_PROVIDERS", "qdrant, pgvector")
        cfg = recall.load_config()
        assert cfg.providers_filter == {"qdrant", "pgvector"}

    def test_store_back_off(self, monkeypatch):
        monkeypatch.setenv("CALIBER_GROUNDING_RECALL_STORE", "false")
        cfg = recall.load_config()
        assert cfg.store_back is False


# ===================================================================== #
# recall_hits — fan-out + dedup
# ===================================================================== #
class TestRecallHits:
    def test_disabled_returns_empty(self):
        cfg = recall.RecallConfig(
            enabled=False, k=5, store_back=True, providers_filter=None,
            query_max_chars=2000, store_min_chars=200, store_max_chars=4000,
        )
        assert recall.recall_hits("hello", cfg=cfg) == []

    def test_empty_query_returns_empty(self):
        _inject_providers([_FakeProvider("p", "P", [Memory(text="x")])])
        cfg = recall.load_config()
        assert recall.recall_hits("   ", cfg=cfg) == []

    def test_no_providers_returns_empty(self, monkeypatch):
        # Force build_providers to return [].
        with patch.object(recall, "_PROVIDERS", []), \
             patch.object(recall, "_LAST_FILTER", None):
            _inject_providers([])
            cfg = recall.load_config()
            assert recall.recall_hits("anything", cfg=cfg) == []

    def test_single_provider_passthrough(self):
        p = _FakeProvider("qdrant", "Qdrant",
                          [Memory(text="bcache fix from 2026", metadata={"id": 1})])
        _inject_providers([p])
        cfg = recall.load_config()
        hits = recall.recall_hits("bcache", cfg=cfg)
        assert len(hits) == 1
        assert hits[0]["text"] == "bcache fix from 2026"
        assert hits[0]["source_provider"] == "Qdrant"

    def test_fan_out_across_providers(self):
        a = _FakeProvider("qdrant", "Qdrant", [Memory(text="A1"), Memory(text="A2")])
        b = _FakeProvider("pgvector", "Postgres pgvector",
                          [Memory(text="B1")])
        _inject_providers([a, b])
        cfg = recall.load_config()
        hits = recall.recall_hits("query", cfg=cfg)
        assert {h["text"] for h in hits} == {"A1", "A2", "B1"}
        assert {h["source_provider"] for h in hits} == \
            {"Qdrant", "Postgres pgvector"}

    def test_dedup_across_providers(self):
        # Two providers return the same fact. Recall should keep one copy.
        a = _FakeProvider("qdrant", "Qdrant", [Memory(text="shared fact")])
        b = _FakeProvider("pgvector", "pgvector", [Memory(text="shared fact")])
        _inject_providers([a, b])
        cfg = recall.load_config()
        hits = recall.recall_hits("q", cfg=cfg)
        assert len(hits) == 1

    def test_one_provider_failure_doesnt_kill_others(self):
        bad = _FakeProvider("memory_kg", "Memory KG", [], raise_on_recall=True)
        good = _FakeProvider("pgvector", "pgvector", [Memory(text="survived")])
        _inject_providers([bad, good])
        cfg = recall.load_config()
        hits = recall.recall_hits("q", cfg=cfg)
        assert len(hits) == 1
        assert hits[0]["text"] == "survived"

    def test_query_truncation_passes_through(self):
        # Verify the long input is truncated before being sent to the
        # provider (so embedders never see > query_max_chars).
        captured: list[str] = []

        class _Recorder:
            name = "rec"
            display_name = "Rec"

            def recall(self, query: str, k: int = 5):  # noqa: ARG002
                captured.append(query)
                return []

            def store(self, content, metadata=None):  # pragma: no cover
                pass

        _inject_providers([_Recorder()])
        cfg = recall.RecallConfig(
            enabled=True, k=5, store_back=True, providers_filter=None,
            query_max_chars=10, store_min_chars=200, store_max_chars=4000,
        )
        recall.recall_hits("a" * 1000, cfg=cfg)
        assert captured == ["a" * 10]


# ===================================================================== #
# format_hits
# ===================================================================== #
class TestFormatHits:
    def test_empty_returns_empty_string(self):
        assert recall.format_hits([]) == ""

    def test_groups_by_provider(self):
        hits = [
            {"text": "a", "source_provider": "Qdrant"},
            {"text": "b", "source_provider": "Qdrant"},
            {"text": "c", "source_provider": "pgvector"},
        ]
        out = recall.format_hits(hits)
        # Both providers appear as h3 sections
        assert "### Qdrant (2)" in out
        assert "### pgvector (1)" in out
        assert "- a" in out
        assert "- c" in out

    def test_truncates_long_content(self):
        long = "x" * 1000
        hits = [{"text": long, "source_provider": "p"}]
        out = recall.format_hits(hits)
        assert "[…]" in out
        assert len(out) < 800  # truncated to ~600


# ===================================================================== #
# build_prepend_messages
# ===================================================================== #
class TestPrepend:
    def test_empty_when_no_hits(self):
        _inject_providers([_FakeProvider("p", "P", [])])
        cfg = recall.load_config()
        assert recall.build_prepend_messages("query", cfg=cfg) == []

    def test_single_system_message_emitted(self):
        _inject_providers([
            _FakeProvider("pgvector", "pgvector", [Memory(text="hit one")]),
        ])
        cfg = recall.load_config()
        msgs = recall.build_prepend_messages("query", cfg=cfg)
        assert len(msgs) == 1
        assert msgs[0]["role"] == "system"
        assert "Recalled memory" in msgs[0]["content"]
        assert "hit one" in msgs[0]["content"]


# ===================================================================== #
# latest_user_text
# ===================================================================== #
class TestLatestUserText:
    def test_picks_last_user_message(self):
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "..."},
            {"role": "user", "content": "latest"},
        ]
        assert recall.latest_user_text(msgs) == "latest"

    def test_empty_when_no_user(self):
        msgs = [{"role": "system", "content": "sys"}]
        assert recall.latest_user_text(msgs) == ""

    def test_handles_list_content(self):
        msgs = [{
            "role": "user",
            "content": [
                {"type": "text", "text": "part-a"},
                {"type": "text", "text": "part-b"},
            ],
        }]
        assert recall.latest_user_text(msgs) == "part-a\npart-b"


# ===================================================================== #
# store / maybe_store_assistant_turn
# ===================================================================== #
class TestStore:
    def test_too_short_skipped(self):
        p = _FakeProvider("p", "P", [])
        _inject_providers([p])
        cfg = recall.load_config()
        n = recall.store("short", cfg=cfg)
        assert n == 0
        assert p.last_stored is None

    def test_truncates_long_content(self):
        p = _FakeProvider("p", "P", [])
        _inject_providers([p])
        cfg = recall.RecallConfig(
            enabled=True, k=5, store_back=True, providers_filter=None,
            query_max_chars=2000, store_min_chars=10, store_max_chars=50,
        )
        recall.store("y" * 200, cfg=cfg)
        assert p.last_stored is not None
        text, _ = p.last_stored
        assert len(text) == 50

    def test_fan_out(self):
        a = _FakeProvider("a", "A", [])
        b = _FakeProvider("b", "B", [])
        _inject_providers([a, b])
        cfg = recall.RecallConfig(
            enabled=True, k=5, store_back=True, providers_filter=None,
            query_max_chars=2000, store_min_chars=10, store_max_chars=4000,
        )
        n = recall.store("a substantive turn summary", {"src": "x"}, cfg=cfg)
        assert n == 2
        assert a.last_stored == ("a substantive turn summary", {"src": "x"})
        assert b.last_stored == ("a substantive turn summary", {"src": "x"})

    def test_store_back_off_disables(self):
        p = _FakeProvider("p", "P", [])
        _inject_providers([p])
        cfg = recall.RecallConfig(
            enabled=True, k=5, store_back=False, providers_filter=None,
            query_max_chars=2000, store_min_chars=10, store_max_chars=4000,
        )
        n = recall.store("plenty long content here", cfg=cfg)
        assert n == 0
        assert p.last_stored is None

    def test_provider_failure_counted_zero_doesnt_stop_others(self):
        bad = _FakeProvider("bad", "Bad", [], raise_on_store=True)
        good = _FakeProvider("good", "Good", [])
        _inject_providers([bad, good])
        cfg = recall.RecallConfig(
            enabled=True, k=5, store_back=True, providers_filter=None,
            query_max_chars=2000, store_min_chars=10, store_max_chars=4000,
        )
        n = recall.store("a substantive long enough turn summary", cfg=cfg)
        assert n == 1
        assert good.last_stored is not None

    def test_maybe_store_assistant_turn(self):
        p = _FakeProvider("p", "P", [])
        _inject_providers([p])
        cfg = recall.RecallConfig(
            enabled=True, k=5, store_back=True, providers_filter=None,
            query_max_chars=2000, store_min_chars=10, store_max_chars=4000,
        )
        result = {
            "choices": [
                {"message": {
                    "role": "assistant",
                    "content": "this is a meaningful assistant turn output",
                }},
            ],
        }
        recall.maybe_store_assistant_turn(
            {"model": "gemma4-98e:tools"}, result, cfg=cfg,
        )
        assert p.last_stored is not None
        text, meta = p.last_stored
        assert "meaningful assistant turn" in text
        assert meta["source"] == "caliber-grounding-proxy"
        assert meta["model"] == "gemma4-98e:tools"

    def test_maybe_store_handles_empty_choices(self):
        p = _FakeProvider("p", "P", [])
        _inject_providers([p])
        cfg = recall.load_config()
        # Shouldn't raise even though there's no content.
        recall.maybe_store_assistant_turn({}, {"choices": []}, cfg=cfg)
        assert p.last_stored is None


# ===================================================================== #
# tools.recall_memory dispatch
# ===================================================================== #
class TestRecallMemoryTool:
    def test_dispatch_via_execute(self):
        from claude_hooks.caliber_proxy import tools
        _inject_providers([
            _FakeProvider("pg", "pgvector", [Memory(text="mem entry")]),
        ])
        out = tools.execute("recall_memory", '{"query":"any"}', cwd="/tmp")
        assert "mem entry" in out

    def test_missing_query_arg_errors(self):
        from claude_hooks.caliber_proxy import tools
        out = tools.execute("recall_memory", '{"k":3}', cwd="/tmp")
        assert out.startswith("error:")


# ===================================================================== #
# preheat_embedder — startup pre-warm
# ===================================================================== #
class _RecallFailingProvider:
    def __init__(self, name: str):
        self.name = name
        self.display_name = name
        self.recall_calls: list[tuple[str, int]] = []

    def recall(self, query: str, k: int = 5):
        self.recall_calls.append((query, k))
        raise RuntimeError("cold")

    def store(self, content: str, metadata: Optional[dict] = None) -> None:
        pass


class _RecallOkProvider:
    def __init__(self, name: str):
        self.name = name
        self.display_name = name
        self.recall_calls: list[tuple[str, int]] = []

    def recall(self, query: str, k: int = 5):
        self.recall_calls.append((query, k))
        return []

    def store(self, content: str, metadata: Optional[dict] = None) -> None:
        pass


class TestPreheatEmbedder:
    def test_disabled_via_env_short_circuits(self, monkeypatch):
        monkeypatch.setenv("CALIBER_GROUNDING_PREHEAT", "0")
        p = _RecallOkProvider("pgvector")
        _inject_providers([p])
        out = recall.preheat_embedder()
        assert out == {}
        assert p.recall_calls == []

    def test_recall_disabled_returns_empty(self, monkeypatch):
        monkeypatch.setenv("CALIBER_GROUNDING_RECALL_ENABLED", "0")
        p = _RecallOkProvider("pgvector")
        _inject_providers([p])
        out = recall.preheat_embedder()
        assert out == {}
        assert p.recall_calls == []

    def test_calls_each_providers_recall_once(self, monkeypatch):
        monkeypatch.delenv("CALIBER_GROUNDING_PREHEAT", raising=False)
        p1 = _RecallOkProvider("pgvector")
        p2 = _RecallOkProvider("sqlite_vec")
        _inject_providers([p1, p2])
        out = recall.preheat_embedder()
        assert set(out.keys()) == {"pgvector", "sqlite_vec"}
        assert all(v.startswith("ok") for v in out.values())
        assert p1.recall_calls == [("warmup", 1)]
        assert p2.recall_calls == [("warmup", 1)]

    def test_failure_is_logged_not_raised(self, monkeypatch):
        monkeypatch.delenv("CALIBER_GROUNDING_PREHEAT", raising=False)
        p = _RecallFailingProvider("pgvector")
        _inject_providers([p])
        out = recall.preheat_embedder()
        assert "failed" in out["pgvector"]
        # The exception didn't escape to the caller.
        assert p.recall_calls == [("warmup", 1)]

    def test_no_hits_returns_friendly_message(self):
        from claude_hooks.caliber_proxy import tools
        _inject_providers([_FakeProvider("p", "P", [])])
        out = tools.execute("recall_memory", '{"query":"nothing"}', cwd="/tmp")
        assert "no recalled memory" in out

    def test_tool_spec_present(self):
        from claude_hooks.caliber_proxy import tools
        names = [s["function"]["name"] for s in tools.openai_tool_specs()]
        assert "recall_memory" in names
