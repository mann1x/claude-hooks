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
from typing import Optional


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
    def store(self, content: str, metadata: Optional[dict] = None,
              vec: Optional[list[float]] = None) -> None:
        """Persist a new memory. Idempotency is the backend's responsibility.

        ``vec`` is an optional precomputed embedding of ``content``,
        obtained from :meth:`embed_for_store`. Providers that embed
        server-side accept it and ignore it.
        """

    # ------------------------------------------------------------------ #
    # Single-embed store path (optional)
    # ------------------------------------------------------------------ #
    # A dedup-then-store cycle embeds twice by default: once to find
    # near-duplicates, once to write. On a CPU embedder the content embed
    # dominates the entire turn (5 KB = 12.7 s, measured on solidpc
    # 2026-07-25, against ~10 ms for all the surrounding DB work), so a
    # provider holding a *client-side* embedder can compute the vector
    # once and spend it twice.
    #
    # Negotiation is zero-config: the caller asks for a vector and, if it
    # gets None, takes the plain text path exactly as before. Providers
    # that embed server-side (qdrant, memory_kg) inherit these defaults
    # and are unaffected.

    def embed_for_store(self, content: str) -> Optional[list[float]]:
        """Embed ``content`` once, for reuse across dedup and store.

        Returns None when this provider has no client-side embedder.
        That is a normal answer, not an error — the caller falls back.
        """
        return None

    def recall_vec(self, vec: list[float], k: int = 5) -> Optional[list[Memory]]:
        """Recall by a precomputed embedding, skipping the query embed.

        Returns None when unsupported, so callers can fall back to
        :meth:`recall`.
        """
        return None

    # ------------------------------------------------------------------ #
    # Batch API (optional, Tier 2.6)
    # ------------------------------------------------------------------ #
    # Subclasses MAY override these when their backend supports a real
    # batch endpoint (e.g. a Postgres COPY for batch_store, or a multi-
    # query similarity call). The defaults fall back to the single-shot
    # methods running concurrently via ThreadPoolExecutor — which gives
    # the network-bound parallelism win for free, even for providers
    # that have no native batch.
    #
    # Callers should prefer batch_recall / batch_store when they have N
    # queries / N items against the SAME provider (e.g. multi-query
    # recall, end-of-session store-flush). Single-shot recall/store
    # remains the right call for one query / one item.

    def batch_recall(self, queries: list[str], k: int = 5) -> list[list[Memory]]:
        """Recall up to ``k`` memories for each query. Returns one list
        per query in the same order. Default implementation parallelises
        single-shot ``recall`` calls; override when a backend has a real
        batch endpoint."""
        if not queries:
            return []
        if len(queries) == 1:
            return [self.recall(queries[0], k=k)]
        from claude_hooks._parallel import parallel_map
        results = parallel_map(lambda q: self.recall(q, k=k), queries)
        return [r if r is not None else [] for r in results]

    def batch_store(self, items: list[tuple[str, Optional[dict]]]) -> None:
        """Persist multiple ``(content, metadata)`` pairs. Default
        implementation parallelises single-shot ``store`` calls; override
        when a backend has a real batch endpoint (e.g. Postgres COPY)."""
        if not items:
            return
        if len(items) == 1:
            content, metadata = items[0]
            self.store(content, metadata=metadata)
            return
        from claude_hooks._parallel import parallel_map
        parallel_map(lambda it: self.store(it[0], metadata=it[1]), items)

    # ------------------------------------------------------------------ #
    # Knowledge-graph surface (optional, pgvector + sqlite_vec only)
    # ------------------------------------------------------------------ #
    # Providers that don't ship KG support inherit these defaults and
    # surface a clean ``NotImplementedError`` — qdrant + memory_kg keep
    # their existing recall/store contract unchanged. pgvector and
    # sqlite_vec override all four.

    def kg_create_entities(self, entities: list[dict]) -> int:
        raise NotImplementedError(
            f"{self.name} does not implement kg_create_entities"
        )

    def kg_add_observations(self, items: list[dict]) -> int:
        raise NotImplementedError(
            f"{self.name} does not implement kg_add_observations"
        )

    def kg_create_relations(self, relations: list[dict]) -> int:
        raise NotImplementedError(
            f"{self.name} does not implement kg_create_relations"
        )

    def kg_search_nodes(self, query: str, k: int = 5) -> list[dict]:
        raise NotImplementedError(
            f"{self.name} does not implement kg_search_nodes"
        )

    def recall_hybrid(self, query: str, k: int = 5,
                       alpha: float = 0.5, rrf_k: int = 60) -> list[Memory]:
        # Default falls back to plain recall (no BM25 signal). pgvector
        # and sqlite_vec override with a real RRF fusion.
        return self.recall(query, k=k)

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
