"""Unit tests for the sqlite-vec MCP stdio server.

Mirror of ``tests/test_pgvector_mcp.py`` trimmed to the 3-tool catalog
(no KG, no hybrid). A lightweight ``FakeSqliteVecProvider`` stands in
for the real provider so the dispatcher is exercised without touching
SQLite or an embedder.
"""

from __future__ import annotations

from typing import Any, Optional

import pytest

from claude_hooks.mcp_format import format_memories as _format_memories
from claude_hooks.providers.base import Memory
from claude_hooks.sqlite_vec_mcp.server import McpServer


class FakeSqliteVecProvider:
    """In-memory stand-in covering the methods the MCP server invokes.

    Mirrors the public surface of ``SqliteVecProvider`` for the three
    memory tools shipped in v1.6.0 (recall / store / count). Records
    every call so tests can assert on shape.
    """

    name = "sqlite_vec"
    display_name = "SQLite + sqlite-vec"

    def __init__(self):
        self.recall_calls: list[tuple[str, int]] = []
        self.stored: list[tuple[str, dict]] = []
        self.count_value = 7
        self.recall_raises: Optional[Exception] = None

    def recall(self, query: str, k: int = 5) -> list[Memory]:
        self.recall_calls.append((query, k))
        if self.recall_raises:
            raise self.recall_raises
        return [
            Memory(
                text=f"hit-sqlite for {query!r}",
                metadata={"_table": "memory", "_distance": 0.21},
            ),
        ][:k]

    def store(self, content: str, metadata: Optional[dict] = None) -> None:
        self.stored.append((content, dict(metadata or {})))

    def count(self) -> int:
        return self.count_value


@pytest.fixture
def server():
    return McpServer(FakeSqliteVecProvider())  # type: ignore[arg-type]


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
        assert result["serverInfo"]["name"] == "claude-hooks-sqlite-vec"
        assert "tools" in result["capabilities"]

    def test_notifications_initialized_returns_none(self, server):
        resp = server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        assert resp is None


# --------------------------------------------------------------------- #
# tools/list — full v1.7+ catalog (8 tools, full pgvector-mcp parity)
# --------------------------------------------------------------------- #


class TestToolsList:
    def test_returns_full_catalog_shape(self, server):
        resp = server.handle(_request("tools/list"))
        tools = resp["result"]["tools"]
        names = {t["name"] for t in tools}
        # Full parity with pgvector-mcp's eight tools.
        assert names == {
            "sqlite-vec-find",
            "sqlite-vec-find-hybrid",
            "sqlite-vec-store",
            "sqlite-vec-count",
            "sqlite-vec-kg-search",
            "sqlite-vec-kg-create",
            "sqlite-vec-kg-observe",
            "sqlite-vec-kg-relate",
        }

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


class TestRecall:
    def test_find_calls_recall_and_returns_text(self, server):
        resp = server.handle(_request("tools/call", params={
            "name": "sqlite-vec-find",
            "arguments": {"query": "bcache fix", "k": 3},
        }))
        assert resp["result"]["isError"] is False
        text = resp["result"]["content"][0]["text"]
        assert "hit-sqlite for" in text
        assert "bcache fix" in text
        assert server.provider.recall_calls == [("bcache fix", 3)]

    def test_find_default_k_is_5(self, server):
        server.handle(_request("tools/call", params={
            "name": "sqlite-vec-find",
            "arguments": {"query": "x"},
        }))
        assert server.provider.recall_calls == [("x", 5)]

    def test_recall_failure_becomes_is_error(self, server):
        server.provider.recall_raises = RuntimeError("db locked")
        resp = server.handle(_request("tools/call", params={
            "name": "sqlite-vec-find",
            "arguments": {"query": "x"},
        }))
        assert resp["result"]["isError"] is True
        assert "db locked" in resp["result"]["content"][0]["text"]


# --------------------------------------------------------------------- #
# tools/call — store / count
# --------------------------------------------------------------------- #


class TestStoreAndCount:
    def test_store_records_content_and_metadata(self, server):
        resp = server.handle(_request("tools/call", params={
            "name": "sqlite-vec-store",
            "arguments": {"content": "memo body", "metadata": {"kind": "test"}},
        }))
        assert resp["result"]["isError"] is False
        assert server.provider.stored == [("memo body", {"kind": "test"})]

    def test_store_metadata_default_is_empty(self, server):
        server.handle(_request("tools/call", params={
            "name": "sqlite-vec-store",
            "arguments": {"content": "another"},
        }))
        assert server.provider.stored == [("another", {})]

    def test_count_returns_provider_count(self, server):
        server.provider.count_value = 999
        resp = server.handle(_request("tools/call", params={
            "name": "sqlite-vec-count", "arguments": {},
        }))
        assert resp["result"]["isError"] is False
        assert "999" in resp["result"]["content"][0]["text"]


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
# Formatter — same shape as pgvector_mcp's, asserted independently
# --------------------------------------------------------------------- #


class TestFormatter:
    def test_format_memories_empty(self):
        assert _format_memories([]) == "(no results)"

    def test_format_memories_renders_table_and_distance(self):
        mems = [
            Memory(text="foo", metadata={"_table": "memory", "_distance": 0.5}),
            Memory(text="bar", metadata={"_table": "memory"}),
        ]
        out = _format_memories(mems)
        assert "memory" in out
        assert "dist=0.5000" in out
        assert "foo" in out
        assert "bar" in out


# --------------------------------------------------------------------- #
# HTTP transport
# --------------------------------------------------------------------- #


class TestHttpTransport:
    """Bring up the HTTP server on a kernel-picked free port; smoke the
    important shapes. Mirrors the pgvector HTTP fixture but against the
    sqlite-vec McpServer.
    """

    @pytest.fixture
    def http_server(self):
        import threading
        from http.server import ThreadingHTTPServer
        from claude_hooks.sqlite_vec_mcp.server import McpServer, _build_http_handler

        provider = FakeSqliteVecProvider()
        server = McpServer(provider)  # type: ignore[arg-type]
        handler_cls = _build_http_handler(server)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            yield {"port": port, "provider": provider, "server": server,
                   "url": f"http://127.0.0.1:{port}/mcp"}
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)
            assert not thread.is_alive()

    @staticmethod
    def _post(url: str, body: Any, headers: Optional[dict] = None):
        import json as _json
        from urllib import request as _req
        data = _json.dumps(body).encode("utf-8")
        req = _req.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with _req.urlopen(req, timeout=5) as resp:
                return resp.status, resp.headers, resp.read()
        except Exception as e:
            if hasattr(e, "code"):
                return e.code, e.headers, b""
            raise

    def test_initialize_round_trip(self, http_server):
        import json as _json
        status, _h, body = self._post(http_server["url"], {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
        })
        assert status == 200
        resp = _json.loads(body)
        assert resp["result"]["serverInfo"]["name"] == "claude-hooks-sqlite-vec"

    def test_tools_call_dispatches_to_provider(self, http_server):
        import json as _json
        status, _h, body = self._post(http_server["url"], {
            "jsonrpc": "2.0", "id": 7, "method": "tools/call",
            "params": {"name": "sqlite-vec-find",
                       "arguments": {"query": "bcache", "k": 2}},
        })
        assert status == 200
        resp = _json.loads(body)
        assert resp["result"]["isError"] is False
        assert "hit-sqlite for" in resp["result"]["content"][0]["text"]
        assert http_server["provider"].recall_calls == [("bcache", 2)]

    def test_notification_returns_202_no_body(self, http_server):
        status, _h, body = self._post(http_server["url"], {
            "jsonrpc": "2.0", "method": "notifications/initialized",
        })
        assert status == 202
        assert body == b""

    def test_batch_request_returns_array_dropping_notifications(self, http_server):
        import json as _json
        status, _h, body = self._post(http_server["url"], [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "sqlite-vec-count", "arguments": {}}},
        ])
        assert status == 200
        arr = _json.loads(body)
        assert isinstance(arr, list)
        assert len(arr) == 2
        assert {r["id"] for r in arr} == {1, 2}

    def test_malformed_json_returns_parse_error(self, http_server):
        from urllib import request as _req
        req = _req.Request(http_server["url"], data=b"{not valid",
                            method="POST")
        req.add_header("Content-Type", "application/json")
        try:
            _req.urlopen(req, timeout=3)
            assert False, "expected HTTPError 400"
        except Exception as e:
            assert getattr(e, "code", None) == 400

    def test_get_on_mcp_returns_405(self, http_server):
        from urllib import request as _req
        req = _req.Request(http_server["url"], method="GET")
        try:
            _req.urlopen(req, timeout=3)
            assert False, "expected HTTPError 405"
        except Exception as e:
            assert getattr(e, "code", None) == 405

    def test_post_on_unknown_path_returns_404(self, http_server):
        from urllib import request as _req
        port = http_server["port"]
        req = _req.Request(f"http://127.0.0.1:{port}/random",
                           data=b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}',
                           method="POST")
        req.add_header("Content-Type", "application/json")
        try:
            _req.urlopen(req, timeout=3)
            assert False, "expected HTTPError 404"
        except Exception as e:
            assert getattr(e, "code", None) == 404

    def test_options_preflight_returns_204_with_cors(self, http_server):
        from urllib import request as _req
        req = _req.Request(http_server["url"], method="OPTIONS")
        with _req.urlopen(req, timeout=3) as resp:
            assert resp.status == 204
            assert resp.headers.get("Access-Control-Allow-Origin") == "*"
            assert "POST" in resp.headers.get("Access-Control-Allow-Methods", "")

    def test_oversized_body_returns_413(self, http_server):
        import socket as _socket
        s = _socket.create_connection(("127.0.0.1", http_server["port"]),
                                       timeout=3)
        try:
            s.sendall(
                b"POST /mcp HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: 104857600\r\n"
                b"\r\n",
            )
            data = s.recv(4096)
        finally:
            s.close()
        assert b" 413 " in data, data[:200]
