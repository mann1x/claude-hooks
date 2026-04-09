"""Memory backend providers for claude-hooks."""

from claude_hooks.providers.base import Memory, Provider, ServerCandidate
from claude_hooks.providers.memory_kg import MemoryKgProvider
from claude_hooks.providers.pgvector import PgvectorProvider
from claude_hooks.providers.qdrant import QdrantProvider
from claude_hooks.providers.sqlite_vec import SqliteVecProvider

# Registry of provider classes available for detection.
#
# To add a new provider:
# 1. Implement the Provider ABC in claude_hooks/providers/<name>.py
# 2. Import it above
# 3. Append the class to REGISTRY (order = display order in install/recall)
#
# The MCP-backed providers (qdrant, memory_kg) are listed first because
# they're the default supported set. The DB-backed scaffolds are listed
# after — they're disabled by default in DEFAULT_CONFIG and require extra
# setup (Postgres or sqlite-vec installed, embedder configured).
REGISTRY: list[type[Provider]] = [
    QdrantProvider,
    MemoryKgProvider,
    PgvectorProvider,
    SqliteVecProvider,
]


def get_provider_class(name: str) -> type[Provider]:
    """Look up a provider class by its ``name`` attribute."""
    for cls in REGISTRY:
        if cls.name == name:
            return cls
    raise KeyError(f"unknown provider: {name}")


__all__ = [
    "Memory",
    "Provider",
    "ServerCandidate",
    "QdrantProvider",
    "MemoryKgProvider",
    "PgvectorProvider",
    "SqliteVecProvider",
    "REGISTRY",
    "get_provider_class",
]
