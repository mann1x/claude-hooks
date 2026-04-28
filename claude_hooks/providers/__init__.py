"""Memory backend providers for claude-hooks.

Lazy-loading registry — concrete provider classes are NOT imported at
package load time. Each hook subprocess pays only for the providers it
actually instantiates.

Empirical: eagerly importing all four providers (qdrant + memory_kg +
pgvector + sqlite_vec) costs ~92ms (the DB-backed scaffolds drag in
``claude_hooks.embedders`` + optional psycopg / sqlite_vec probes).
Importing just qdrant + memory_kg lazily is ~3ms — a ~90ms saving on
every hook invocation.

Public API stays stable:

- ``REGISTRY`` is iterable and yields lazy-resolved classes
- ``QdrantProvider`` / ``MemoryKgProvider`` / ``PgvectorProvider`` /
  ``SqliteVecProvider`` resolve via module ``__getattr__``
- ``get_provider_class(name)`` does the import on demand
- ``provider_names()`` returns names without importing
"""

from claude_hooks.providers.base import Memory, Provider, ServerCandidate

# name → (module_path, class_name). Pure data — no imports yet.
#
# Order matters: it's the registration order seen by detect / install /
# dispatcher when iterating REGISTRY. MCP-backed providers (qdrant,
# memory_kg) come first because they're the default supported set.
# DB-backed scaffolds (pgvector, sqlite_vec) come after.
_PROVIDER_REGISTRY: dict[str, tuple[str, str]] = {
    "qdrant":     ("claude_hooks.providers.qdrant", "QdrantProvider"),
    "memory_kg":  ("claude_hooks.providers.memory_kg", "MemoryKgProvider"),
    "pgvector":   ("claude_hooks.providers.pgvector", "PgvectorProvider"),
    "sqlite_vec": ("claude_hooks.providers.sqlite_vec", "SqliteVecProvider"),
}


def provider_names() -> list[str]:
    """Return the list of registered provider names. No imports."""
    return list(_PROVIDER_REGISTRY.keys())


def get_provider_class(name: str) -> type[Provider]:
    """Look up a provider class by its ``name`` attribute.

    Imports the provider module on demand — first call for a given name
    pays the import cost, subsequent calls hit the import cache.
    """
    if name not in _PROVIDER_REGISTRY:
        raise KeyError(f"unknown provider: {name}")
    mod_path, cls_name = _PROVIDER_REGISTRY[name]
    import importlib
    return getattr(importlib.import_module(mod_path), cls_name)


class _LazyRegistry:
    """Backward-compatible ``REGISTRY`` — looks like a list of provider
    classes, but each class is resolved on access rather than at import
    time. Iterating this WILL import every provider; for hot paths
    that only need names, prefer ``provider_names()`` and resolve
    individual classes via ``get_provider_class`` after filtering.
    """

    def __iter__(self):
        for name in _PROVIDER_REGISTRY:
            yield get_provider_class(name)

    def __len__(self):
        return len(_PROVIDER_REGISTRY)

    def __contains__(self, item):
        if isinstance(item, str):
            return item in _PROVIDER_REGISTRY
        try:
            return any(item is get_provider_class(n) for n in _PROVIDER_REGISTRY)
        except Exception:
            return False

    def __getitem__(self, key):
        if isinstance(key, int):
            names = list(_PROVIDER_REGISTRY.keys())
            return get_provider_class(names[key])
        if isinstance(key, str):
            return get_provider_class(key)
        if isinstance(key, slice):
            names = list(_PROVIDER_REGISTRY.keys())[key]
            return [get_provider_class(n) for n in names]
        raise TypeError(f"REGISTRY indices must be int, str, or slice, not {type(key).__name__}")

    def __repr__(self):
        return f"<LazyRegistry {provider_names()}>"


REGISTRY = _LazyRegistry()


# Lazy module-level attributes: ``from claude_hooks.providers import
# QdrantProvider`` works without eagerly importing the other three.
_LAZY_ATTRS = {
    "QdrantProvider": "qdrant",
    "MemoryKgProvider": "memory_kg",
    "PgvectorProvider": "pgvector",
    "SqliteVecProvider": "sqlite_vec",
}


def __getattr__(name: str):
    """PEP 562 module-level lazy attribute resolution."""
    if name in _LAZY_ATTRS:
        return get_provider_class(_LAZY_ATTRS[name])
    raise AttributeError(f"module 'claude_hooks.providers' has no attribute {name!r}")


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
    "provider_names",
]
