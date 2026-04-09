"""
Provider abstract base class.

A provider is a memory backend (Qdrant, Memory KG, ...) that claude-hooks
recalls from before each prompt and stores into at end-of-turn.

Each provider implements four methods:

- ``detect``    — find candidate MCP servers from a parsed ~/.claude.json
- ``verify``    — confirm a candidate actually exposes the expected tools
- ``recall``    — fetch top-k snippets relevant to a query string
- ``store``     — persist a new memory

Providers are deliberately tiny so adding a new one (pgvector, sqlite-vec,
Weaviate, ...) is one file under ``claude_hooks/providers/`` plus a line
in the ``REGISTRY`` list in ``__init__.py``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class ServerCandidate:
    """A potential MCP server detected in the user's Claude Code config."""

    server_key: str                       # the key in ~/.claude.json's mcpServers
    url: str                              # the http(s) URL
    headers: dict = field(default_factory=dict)  # auth headers, if any
    source: str = "user"                  # "user" (root mcpServers) or "project:<path>"
    confidence: str = "name"              # "name" | "tool_probe" | "manual"
    notes: str = ""                       # human-readable detail


@dataclass
class Memory:
    """A single recalled memory item."""

    text: str                             # the content shown to the model
    metadata: dict = field(default_factory=dict)
    source_provider: str = ""             # filled in by dispatcher


class Provider(ABC):
    """Abstract base class for memory providers."""

    #: Short, lowercase identifier — must match the key under ``providers``
    #: in claude-hooks.json.
    name: str = ""

    #: Human-readable label for prompts and logs.
    display_name: str = ""

    def __init__(self, server: ServerCandidate, options: Optional[dict] = None):
        self.server = server
        self.options = dict(options or {})

    # ------------------------------------------------------------------ #
    # Class-level: detection & verification (no instance needed)
    # ------------------------------------------------------------------ #
    @classmethod
    @abstractmethod
    def detect(cls, claude_config: dict) -> list[ServerCandidate]:
        """
        Walk a parsed ~/.claude.json and return MCP servers that look like
        this provider. May return an empty list, one match, or many — the
        installer disambiguates with the user.
        """

    @classmethod
    @abstractmethod
    def signature_tools(cls) -> set[str]:
        """
        Tool names that, if found on a server, mean it is this kind of
        provider. Used by the tool-probe detection fallback when name
        matching is ambiguous.
        """

    @classmethod
    def verify(cls, server: ServerCandidate, *, timeout: float = 5.0) -> bool:
        """
        Probe ``server`` and confirm it exposes our signature tools.
        Returns True if the server is reachable and has the right tools.
        Default implementation calls ``tools/list`` and checks names.
        """
        from claude_hooks.mcp_client import McpClient, McpError

        client = McpClient(server.url, timeout=timeout, headers=server.headers)
        try:
            tools = client.list_tools()
        except McpError:
            return False
        names = {t.get("name") for t in tools if isinstance(t, dict)}
        sig = cls.signature_tools()
        return sig.issubset(names)

    # ------------------------------------------------------------------ #
    # Instance-level: actual recall & store
    # ------------------------------------------------------------------ #
    @abstractmethod
    def recall(self, query: str, k: int = 5) -> list[Memory]:
        """Return up to ``k`` memories relevant to ``query``."""

    @abstractmethod
    def store(self, content: str, metadata: Optional[dict] = None) -> None:
        """Persist a new memory. Idempotency is the backend's responsibility."""

    # ------------------------------------------------------------------ #
    # Helpers shared by all providers
    # ------------------------------------------------------------------ #
    def _client(self, timeout: float = 5.0):
        from claude_hooks.mcp_client import McpClient
        return McpClient(self.server.url, timeout=timeout, headers=self.server.headers)


# Helper used by detect() implementations across providers.
def iter_mcp_servers(claude_config: dict) -> list[tuple[str, dict, str]]:
    """
    Yield ``(server_key, server_config, source)`` triples covering both the
    root ``mcpServers`` map and per-project ones.
    """
    out: list[tuple[str, dict, str]] = []
    root = (claude_config or {}).get("mcpServers") or {}
    if isinstance(root, dict):
        for k, v in root.items():
            if isinstance(v, dict):
                out.append((k, v, "user"))
    projects = (claude_config or {}).get("projects") or {}
    if isinstance(projects, dict):
        for proj_path, proj_cfg in projects.items():
            pmcp = (proj_cfg or {}).get("mcpServers") or {}
            if isinstance(pmcp, dict):
                for k, v in pmcp.items():
                    if isinstance(v, dict):
                        out.append((k, v, f"project:{proj_path}"))
    return out


def is_http_server(server_config: dict) -> bool:
    """A server is usable by claude-hooks only if it speaks HTTP transport."""
    return server_config.get("type") in ("http", "sse", "streamable-http") and "url" in server_config
