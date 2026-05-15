"""Unit tests for the four NEW sqlite-vec-mcp tools (v1.7+).

Mirrors test_pgvector_mcp.py::TestKgTools — JSON-RPC dispatch for
find-hybrid + the three kg-* tools, plus argument validation and
the count-words-in-response sanity checks.
"""

from __future__ import annotations

import json

import pytest

from claude_hooks.providers.base import Memory
from claude_hooks.sqlite_vec_mcp.server import McpServer


class FakeProvider:
    """Captures method calls + returns canned shapes."""

    def __init__(self):
        self.calls: list[tuple] = []

    def recall(self, q, k=5):
        self.calls.append(("recall", q, k))
        return [Memory(text="vec hit", metadata={"_distance": 0.1, "_table": "memory"})]

    def recall_hybrid(self, q, k=5, alpha=0.5, rrf_k=60):
        self.calls.append(("recall_hybrid", q, k, alpha))
        return [Memory(
            text="hybrid hit",
            metadata={"_score": 0.42, "_table": "memory",
                      "_vec_rank": 1, "_kw_rank": 2},
        )]

    def store(self, c, metadata=None):
        self.calls.append(("store", c, metadata))

    def count(self):
        return 7

    def kg_create_entities(self, ents):
        self.calls.append(("kg_create", ents))
        return len(ents)

    def kg_add_observations(self, items):
        self.calls.append(("kg_observe", items))
        return len(items)

    def kg_create_relations(self, rels):
        self.calls.append(("kg_relate", rels))
        return len(rels)

    def kg_search_nodes(self, q, k=5):
        self.calls.append(("kg_search", q, k))
        return [{
            "name": "solidpc", "entity_type": "server",
            "metadata": {}, "observations": ["fact"],
            "_score": 1.5, "_match": "name",
        }]


@pytest.fixture
def server():
    s = McpServer(FakeProvider())  # type: ignore[arg-type]
    s.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    return s


def _call(server, name, args):
    return server.handle({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": name, "arguments": args},
    })


class TestFindHybrid:
    def test_dispatches_to_recall_hybrid(self, server):
        resp = _call(server, "sqlite-vec-find-hybrid",
                     {"query": "test", "k": 3, "alpha": 0.7})
        assert resp["result"]["isError"] is False
        assert "hybrid hit" in resp["result"]["content"][0]["text"]
        assert server.provider.calls[-1] == ("recall_hybrid", "test", 3, 0.7)

    def test_default_alpha_is_half(self, server):
        _call(server, "sqlite-vec-find-hybrid", {"query": "q"})
        assert server.provider.calls[-1][3] == 0.5

    def test_score_appears_in_output(self, server):
        resp = _call(server, "sqlite-vec-find-hybrid", {"query": "q"})
        text = resp["result"]["content"][0]["text"]
        assert "score=0.4200" in text


class TestKgCreate:
    def test_dispatches_and_reports_count(self, server):
        resp = _call(server, "sqlite-vec-kg-create", {
            "entities": [
                {"name": "x", "entity_type": "t"},
                {"name": "y", "entity_type": "t"},
            ],
        })
        assert resp["result"]["isError"] is False
        text = resp["result"]["content"][0]["text"]
        assert "created 2 new entities" in text

    def test_singular_for_one(self, server):
        resp = _call(server, "sqlite-vec-kg-create", {
            "entities": [{"name": "x", "entity_type": "t"}],
        })
        assert "created 1 new entity" in resp["result"]["content"][0]["text"]

    def test_empty_list_safe(self, server):
        resp = _call(server, "sqlite-vec-kg-create", {"entities": []})
        assert resp["result"]["isError"] is False


class TestKgObserve:
    def test_dispatches(self, server):
        resp = _call(server, "sqlite-vec-kg-observe", {
            "items": [{"entity_name": "x", "content": "c"}],
        })
        assert resp["result"]["isError"] is False
        assert "inserted 1 observation" in resp["result"]["content"][0]["text"]

    def test_plural(self, server):
        resp = _call(server, "sqlite-vec-kg-observe", {
            "items": [
                {"entity_name": "x", "content": "a"},
                {"entity_name": "x", "content": "b"},
            ],
        })
        assert "inserted 2 observations" in resp["result"]["content"][0]["text"]


class TestKgRelate:
    def test_dispatches(self, server):
        resp = _call(server, "sqlite-vec-kg-relate", {
            "relations": [
                {"from": "a", "to": "b", "relation_type": "links_to"},
            ],
        })
        assert resp["result"]["isError"] is False
        assert "created 1 new relation" in resp["result"]["content"][0]["text"]


class TestKgSearch:
    def test_dispatches(self, server):
        resp = _call(server, "sqlite-vec-kg-search", {"query": "solidpc"})
        text = resp["result"]["content"][0]["text"]
        assert "solidpc" in text
        assert "fact" in text

    def test_score_match_metadata_in_output(self, server):
        resp = _call(server, "sqlite-vec-kg-search", {"query": "q"})
        text = resp["result"]["content"][0]["text"]
        assert "match=name" in text
        assert "score=1.500" in text


class TestCatalogSchemas:
    """Spot-check the JSON Schemas advertised by tools/list."""

    def test_each_new_tool_has_input_schema(self, server):
        resp = server.handle({"jsonrpc": "2.0", "id": 9, "method": "tools/list"})
        tools = {t["name"]: t for t in resp["result"]["tools"]}
        for name in (
            "sqlite-vec-find-hybrid",
            "sqlite-vec-kg-search",
            "sqlite-vec-kg-create",
            "sqlite-vec-kg-observe",
            "sqlite-vec-kg-relate",
        ):
            assert name in tools, f"{name} missing"
            schema = tools[name]["inputSchema"]
            assert schema["type"] == "object"
            assert "properties" in schema

    def test_find_hybrid_advertises_alpha(self, server):
        resp = server.handle({"jsonrpc": "2.0", "id": 9, "method": "tools/list"})
        tools = {t["name"]: t for t in resp["result"]["tools"]}
        schema = tools["sqlite-vec-find-hybrid"]["inputSchema"]
        assert "alpha" in schema["properties"]
        assert schema["properties"]["alpha"]["default"] == 0.5


class TestErrorPaths:
    def test_unknown_tool_returns_iserror(self, server):
        resp = _call(server, "sqlite-vec-unknown", {})
        assert resp["result"]["isError"] is True
