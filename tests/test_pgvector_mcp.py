"""Unit tests for the pgvector MCP stdio server.

Tests target the JSON-RPC dispatch surface (``McpServer.handle``) — they
don't spawn a subprocess or touch Postgres. A lightweight
``FakePgvectorProvider`` stands in for the real provider so we can
assert on call shape and error propagation without Docker.
"""

from __future__ import annotations

from typing import Any, Optional

import pytest

from claude_hooks.pgvector_mcp.server import McpServer, _format_kg_nodes, _format_memories
from claude_hooks.providers.base import Memory


class FakePgvectorProvider:
    """In-memory stand-in covering the methods the MCP server invokes.

    Mirrors the public surface of ``PgvectorProvider`` plus the
    extensions (``recall_hybrid``, ``kg_*``). Records every call so
    tests can assert on shape.
    """

    name = "pgvector"
    display_name = "Postgres pgvector"

    def __init__(self):
        self.recall_calls: list[tuple[str, int]] = []
        self.recall_hybrid_calls: list[tuple[str, int, float]] = []
        self.stored: list[tuple[str, dict]] = []
        self.count_value = 42
        self.kg_search_calls: list[tuple[str, int]] = []
        self.kg_search_returns: list[dict] = []
        self.kg_create_calls: list[list[dict]] = []
        self.kg_observe_calls: list[list[dict]] = []
        self.kg_relate_calls: list[list[dict]] = []
        self.recall_raises: Optional[Exception] = None

    def recall(self, query: str, k: int = 5) -> list[Memory]:
        self.recall_calls.append((query, k))
        if self.recall_raises:
            raise self.recall_raises
        return [
            Memory(text=f"hit-vec for {query!r}", metadata={"_table": "memories_qwen3", "_distance": 0.12}),
        ][:k]

    def recall_hybrid(self, query: str, k: int = 5, alpha: float = 0.5) -> list[Memory]:
        self.recall_hybrid_calls.append((query, k, alpha))
        return [
            Memory(text=f"hit-hybrid for {query!r}", metadata={"_table": "memories_qwen3", "_score": 0.018, "_vec_rank": 1, "_kw_rank": 2}),
        ][:k]

    def store(self, content: str, metadata: Optional[dict] = None) -> None:
        self.stored.append((content, dict(metadata or {})))

    def count(self) -> int:
        return self.count_value

    def kg_search_nodes(self, query: str, k: int = 5) -> list[dict]:
        self.kg_search_calls.append((query, k))
        return list(self.kg_search_returns)

    def kg_create_entities(self, entities: list[dict]) -> int:
        self.kg_create_calls.append(list(entities))
        return len([e for e in entities if e.get("name") and e.get("entity_type")])

    def kg_add_observations(self, items: list[dict]) -> int:
        self.kg_observe_calls.append(list(items))
        return len([i for i in items if i.get("entity_name") and i.get("content")])

    def kg_create_relations(self, relations: list[dict]) -> int:
        self.kg_relate_calls.append(list(relations))
        return len([
            r for r in relations
            if r.get("from") and r.get("to") and r.get("relation_type")
        ])


@pytest.fixture
def server():
    return McpServer(FakePgvectorProvider())  # type: ignore[arg-type]


def _request(method: str, *, rpc_id: Any = 1, params: Optional[dict] = None) -> dict:
    msg = {"jsonrpc": "2.0", "id": rpc_id, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


# --------------------------------------------------------------------- #
# Protocol handshake
# --------------------------------------------------------------------- #


class TestHandshake:
    def test_initialize_returns_protocol_and_server_info(self, server):
        resp = server.handle(_request("initialize", params={
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "smoke", "version": "0"},
        }))
        assert resp["jsonrpc"] == "2.0"
        assert resp["id"] == 1
        result = resp["result"]
        assert result["protocolVersion"] == "2024-11-05"
        assert "serverInfo" in result
        assert result["serverInfo"]["name"] == "claude-hooks-pgvector"
        assert "tools" in result["capabilities"]

    def test_notifications_initialized_returns_none(self, server):
        # Notifications have no id and no response.
        resp = server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        assert resp is None


# --------------------------------------------------------------------- #
# tools/list
# --------------------------------------------------------------------- #


class TestToolsList:
    def test_returns_full_catalog_shape(self, server):
        resp = server.handle(_request("tools/list"))
        tools = resp["result"]["tools"]
        names = {t["name"] for t in tools}
        # All eight expected tools are present.
        expected = {
            "pgvector-find",
            "pgvector-find-hybrid",
            "pgvector-store",
            "pgvector-count",
            "pgvector-kg-search",
            "pgvector-kg-create",
            "pgvector-kg-observe",
            "pgvector-kg-relate",
        }
        assert expected <= names

    def test_each_tool_has_required_fields(self, server):
        tools = server.handle(_request("tools/list"))["result"]["tools"]
        for t in tools:
            assert "name" in t
            assert "description" in t
            assert "inputSchema" in t
            assert t["inputSchema"]["type"] == "object"


# --------------------------------------------------------------------- #
# tools/call — recall
# --------------------------------------------------------------------- #


class TestRecallTools:
    def test_find_calls_recall_and_returns_text(self, server):
        resp = server.handle(_request("tools/call", params={
            "name": "pgvector-find",
            "arguments": {"query": "bcache fix", "k": 3},
        }))
        assert resp["result"]["isError"] is False
        text = resp["result"]["content"][0]["text"]
        assert "hit-vec for" in text
        assert "bcache fix" in text
        assert server.provider.recall_calls == [("bcache fix", 3)]

    def test_find_hybrid_calls_recall_hybrid(self, server):
        server.handle(_request("tools/call", params={
            "name": "pgvector-find-hybrid",
            "arguments": {"query": "timer cron", "k": 4, "alpha": 0.7},
        }))
        assert server.provider.recall_hybrid_calls == [("timer cron", 4, 0.7)]

    def test_find_hybrid_alpha_default(self, server):
        server.handle(_request("tools/call", params={
            "name": "pgvector-find-hybrid",
            "arguments": {"query": "x"},
        }))
        # k default 5, alpha default 0.5
        assert server.provider.recall_hybrid_calls == [("x", 5, 0.5)]

    def test_find_default_k_is_5(self, server):
        server.handle(_request("tools/call", params={
            "name": "pgvector-find",
            "arguments": {"query": "x"},
        }))
        assert server.provider.recall_calls == [("x", 5)]

    def test_recall_failure_becomes_is_error(self, server):
        server.provider.recall_raises = RuntimeError("connection refused")
        resp = server.handle(_request("tools/call", params={
            "name": "pgvector-find",
            "arguments": {"query": "x"},
        }))
        assert resp["result"]["isError"] is True
        assert "connection refused" in resp["result"]["content"][0]["text"]


# --------------------------------------------------------------------- #
# tools/call — store / count
# --------------------------------------------------------------------- #


class TestStoreAndCount:
    def test_store_records_content_and_metadata(self, server):
        resp = server.handle(_request("tools/call", params={
            "name": "pgvector-store",
            "arguments": {"content": "memo body", "metadata": {"kind": "test"}},
        }))
        assert resp["result"]["isError"] is False
        assert server.provider.stored == [("memo body", {"kind": "test"})]

    def test_store_metadata_default_is_empty(self, server):
        server.handle(_request("tools/call", params={
            "name": "pgvector-store",
            "arguments": {"content": "another"},
        }))
        assert server.provider.stored == [("another", {})]

    def test_count_returns_provider_count(self, server):
        server.provider.count_value = 12345
        resp = server.handle(_request("tools/call", params={
            "name": "pgvector-count", "arguments": {},
        }))
        assert resp["result"]["isError"] is False
        assert "12345" in resp["result"]["content"][0]["text"]


# --------------------------------------------------------------------- #
# tools/call — KG operations
# --------------------------------------------------------------------- #


class TestKgTools:
    def test_kg_search_routes_query(self, server):
        server.provider.kg_search_returns = [
            {"name": "solidPC", "entity_type": "server", "_score": 0.9,
             "_match": "name", "observations": ["o1", "o2"]},
        ]
        resp = server.handle(_request("tools/call", params={
            "name": "pgvector-kg-search",
            "arguments": {"query": "solidpc nginx", "k": 3},
        }))
        assert resp["result"]["isError"] is False
        text = resp["result"]["content"][0]["text"]
        assert "solidPC" in text
        assert "server" in text
        assert "o1" in text
        assert server.provider.kg_search_calls == [("solidpc nginx", 3)]

    def test_kg_create_passes_entities(self, server):
        entities = [
            {"name": "claude-hooks", "entity_type": "service", "metadata": {"port": 47018}},
            {"name": "pgvector",     "entity_type": "service"},
        ]
        resp = server.handle(_request("tools/call", params={
            "name": "pgvector-kg-create",
            "arguments": {"entities": entities},
        }))
        assert resp["result"]["isError"] is False
        assert server.provider.kg_create_calls == [entities]
        # Both have name + entity_type → "2 new entities"
        assert "2 new entit" in resp["result"]["content"][0]["text"]

    def test_kg_observe_passes_items(self, server):
        items = [{"entity_name": "solidPC", "content": "runs ollama"}]
        resp = server.handle(_request("tools/call", params={
            "name": "pgvector-kg-observe",
            "arguments": {"items": items},
        }))
        assert resp["result"]["isError"] is False
        assert server.provider.kg_observe_calls == [items]
        assert "1 observation" in resp["result"]["content"][0]["text"]

    def test_kg_relate_passes_relations(self, server):
        relations = [
            {"from": "claude-hooks", "to": "pgvector", "relation_type": "depends_on"},
        ]
        resp = server.handle(_request("tools/call", params={
            "name": "pgvector-kg-relate",
            "arguments": {"relations": relations},
        }))
        assert resp["result"]["isError"] is False
        assert server.provider.kg_relate_calls == [relations]
        assert "1 new relation" in resp["result"]["content"][0]["text"]


# --------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------- #


class TestErrors:
    def test_unknown_tool_returns_is_error(self, server):
        resp = server.handle(_request("tools/call", params={
            "name": "does-not-exist",
            "arguments": {},
        }))
        assert resp["result"]["isError"] is True
        assert "unknown tool" in resp["result"]["content"][0]["text"].lower()

    def test_unknown_method_returns_jsonrpc_error(self, server):
        resp = server.handle(_request("does/not/exist"))
        assert "error" in resp
        assert resp["error"]["code"] == -32601


# --------------------------------------------------------------------- #
# Formatters — pure functions, easy to assert on shape
# --------------------------------------------------------------------- #


class TestFormatters:
    def test_format_memories_empty(self):
        assert _format_memories([]) == "(no results)"

    def test_format_memories_includes_score_and_distance(self):
        mems = [
            Memory(text="foo", metadata={"_table": "memories_qwen3", "_score": 0.12, "_distance": 0.5}),
            Memory(text="bar", metadata={"_table": "memories_qwen3"}),
        ]
        out = _format_memories(mems)
        assert "memories_qwen3" in out
        assert "score=0.1200" in out
        assert "dist=0.5000" in out
        assert "foo" in out
        assert "bar" in out

    def test_format_kg_nodes_empty(self):
        assert _format_kg_nodes([]) == "(no results)"

    def test_format_kg_nodes_renders_entity_and_observations(self):
        nodes = [
            {"name": "solidPC", "entity_type": "server", "_score": 0.9,
             "_match": "name", "observations": ["o1", "o2"]},
            {"name": "swag", "entity_type": "service", "_score": 0.5,
             "_match": "observation", "observations": []},
        ]
        out = _format_kg_nodes(nodes)
        assert "# solidPC (server)" in out
        assert "  - o1" in out
        assert "  - o2" in out
        assert "# swag (service)" in out
        assert "(no observations)" in out
