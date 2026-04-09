"""Tests for the stdlib-only MCP HTTP client."""

from __future__ import annotations

import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# Allow `import claude_hooks` from a checkout without pip-install.
HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from claude_hooks.mcp_client import McpClient, McpError, extract_text_content


class MockMcpHandler(BaseHTTPRequestHandler):
    """Tiny in-process MCP server for tests. Configured per-test via class attrs."""

    response_body: bytes = b'{"jsonrpc":"2.0","id":1,"result":{"tools":[]}}'
    response_status: int = 200
    response_content_type: str = "application/json"

    def log_message(self, format, *args):
        pass  # silence

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        _ = self.rfile.read(length)
        self.send_response(self.response_status)
        self.send_header("Content-Type", self.response_content_type)
        self.send_header("Content-Length", str(len(self.response_body)))
        self.end_headers()
        self.wfile.write(self.response_body)


def start_server(handler_cls):
    server = HTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


class TestMcpClient(unittest.TestCase):
    def setUp(self):
        # Reset shared state so tests don't poison each other.
        MockMcpHandler.response_body = b'{"jsonrpc":"2.0","id":1,"result":{"tools":[]}}'
        MockMcpHandler.response_status = 200
        MockMcpHandler.response_content_type = "application/json"
        self.server, self.thread = start_server(MockMcpHandler)
        host, port = self.server.server_address
        self.url = f"http://{host}:{port}/mcp"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_list_tools_basic(self):
        MockMcpHandler.response_body = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "result": {"tools": [{"name": "qdrant-find"}]}}
        ).encode()
        client = McpClient(self.url, timeout=2.0)
        tools = client.list_tools()
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["name"], "qdrant-find")

    def test_call_tool(self):
        MockMcpHandler.response_body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"content": [{"type": "text", "text": "hello"}], "isError": False},
            }
        ).encode()
        client = McpClient(self.url, timeout=2.0)
        result = client.call_tool("qdrant-find", {"query": "x", "collection_name": "memory"})
        self.assertEqual(extract_text_content(result), "hello")

    def test_jsonrpc_error_raises(self):
        MockMcpHandler.response_body = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "Method not found"}}
        ).encode()
        client = McpClient(self.url, timeout=2.0)
        with self.assertRaises(McpError) as ctx:
            client.list_tools()
        self.assertEqual(ctx.exception.code, -32601)

    def test_sse_response_parsing(self):
        sse = b"event: message\ndata: " + json.dumps(
            {"jsonrpc": "2.0", "id": 1, "result": {"tools": [{"name": "x"}]}}
        ).encode() + b"\n\n"
        MockMcpHandler.response_body = sse
        MockMcpHandler.response_content_type = "text/event-stream"
        client = McpClient(self.url, timeout=2.0)
        tools = client.list_tools()
        self.assertEqual(tools[0]["name"], "x")

    def test_http_error_raises_mcperror(self):
        MockMcpHandler.response_body = b"server exploded"
        MockMcpHandler.response_status = 500
        client = McpClient(self.url, timeout=2.0)
        with self.assertRaises(McpError):
            client.list_tools()

    def test_extract_text_content_handles_missing(self):
        self.assertEqual(extract_text_content({}), "")
        self.assertEqual(extract_text_content({"content": []}), "")
        self.assertEqual(
            extract_text_content({"content": [{"type": "image"}, {"type": "text", "text": "x"}]}),
            "x",
        )


if __name__ == "__main__":
    unittest.main()
