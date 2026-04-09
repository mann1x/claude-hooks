"""
Minimal MCP (Model Context Protocol) client for Streamable HTTP transport.

Stdlib only — no external dependencies. Designed for stateless servers
(mcp-proxy --stateless), where each request is independent and we can call
``tools/call`` directly without an ``initialize`` handshake.

Both ``application/json`` and ``text/event-stream`` response framing are
handled, so this client also works against streaming-mode servers.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from typing import Any, Optional


class McpError(Exception):
    """Raised when an MCP call fails (transport, protocol, or tool error)."""

    def __init__(self, message: str, *, code: Optional[int] = None, data: Any = None):
        super().__init__(message)
        self.code = code
        self.data = data


class McpClient:
    """
    Tiny JSON-RPC 2.0 client over MCP Streamable HTTP.

    Usage:
        client = McpClient("http://192.168.178.2:32775/mcp")
        tools = client.list_tools()
        result = client.call_tool("qdrant-find", {"query": "bcache", "collection_name": "memory"})
    """

    def __init__(
        self,
        url: str,
        *,
        timeout: float = 5.0,
        headers: Optional[dict] = None,
    ):
        self.url = url
        self.timeout = timeout
        self.headers = dict(headers or {})
        self._next_id = 0

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def list_tools(self) -> list[dict]:
        """Return the server's tools list."""
        resp = self._call("tools/list", {})
        return resp.get("tools", [])

    def call_tool(self, name: str, arguments: dict) -> dict:
        """
        Invoke a tool by name. Returns the raw ``result`` object from the
        JSON-RPC response (i.e. ``{"content": [...], "isError": bool, ...}``).
        """
        return self._call("tools/call", {"name": name, "arguments": arguments})

    def initialize(self) -> dict:
        """
        Optional handshake. Most stateless servers don't require this — we
        only call it from the installer's verification step to confirm the
        server is alive and identify itself.
        """
        return self._call(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "claude-hooks", "version": "0.1"},
            },
        )

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    def _call(self, method: str, params: dict) -> dict:
        self._next_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id,
            "method": method,
            "params": params,
        }
        body = json.dumps(payload).encode("utf-8")

        req_headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **self.headers,
        }
        req = urllib.request.Request(
            self.url, data=body, headers=req_headers, method="POST"
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                content_type = resp.headers.get("Content-Type", "")
                raw = resp.read()
        except urllib.error.HTTPError as e:
            try:
                err_body = e.read().decode("utf-8", errors="replace")
            except Exception:
                err_body = ""
            raise McpError(
                f"HTTP {e.code} from {self.url}: {err_body[:200]}"
            ) from e
        except (urllib.error.URLError, socket.timeout) as e:
            raise McpError(f"network error talking to {self.url}: {e}") from e

        # Parse the response — could be JSON or SSE-framed JSON.
        try:
            if "text/event-stream" in content_type:
                msg = self._parse_sse(raw)
            else:
                msg = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            raise McpError(f"invalid response from {self.url}: {e}") from e

        if not isinstance(msg, dict):
            raise McpError(f"unexpected response shape from {self.url}: {type(msg).__name__}")

        if "error" in msg:
            err = msg["error"] or {}
            raise McpError(
                err.get("message", "MCP error"),
                code=err.get("code"),
                data=err.get("data"),
            )

        result = msg.get("result")
        if result is None:
            raise McpError(f"missing 'result' in response from {self.url}")
        return result

    @staticmethod
    def _parse_sse(raw: bytes) -> dict:
        """
        Parse a single Server-Sent-Events frame and return the data payload
        as a dict. We only care about the most recent ``data:`` line.
        """
        text = raw.decode("utf-8", errors="replace")
        last_data: Optional[str] = None
        for line in text.splitlines():
            if line.startswith("data:"):
                last_data = line[5:].lstrip()
        if last_data is None:
            raise ValueError("no SSE data line in response")
        return json.loads(last_data)


def extract_text_content(result: dict) -> str:
    """
    Convenience: pull the concatenated text from an MCP tool ``result``'s
    ``content`` field. Returns empty string if no text content present.
    """
    parts: list[str] = []
    for item in result.get("content", []) or []:
        if isinstance(item, dict) and item.get("type") == "text":
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts)
