"""
Postgres + pgvector provider.

Stores memories as embeddings in a Postgres database with the pgvector
extension for vector similarity search.

To use:

1. Install Postgres and create a database. Run ``CREATE EXTENSION vector;``
2. Install the optional Python deps: ``pip install psycopg[binary]``
3. Pull an embedding model into Ollama: ``ollama pull nomic-embed-text``
4. Edit ``config/claude-hooks.json``:

   .. code-block:: json

       "pgvector": {
         "enabled": true,
         "dsn": "postgresql://user:pass@localhost:5432/memory",
         "table": "claude_hooks_memory",
         "embedder": "ollama",
         "embedder_options": {"model": "nomic-embed-text"}
       }

5. Tables are created automatically on first use (requires the pgvector
   extension to already be installed in the database).

The schema is simple — one table per collection:

.. code-block:: sql

    CREATE TABLE claude_hooks_memory (
        id          BIGSERIAL PRIMARY KEY,
        content     TEXT NOT NULL,
        metadata    JSONB,
        embedding   vector(768),
        created_at  TIMESTAMPTZ DEFAULT now()
    );
    CREATE INDEX ON claude_hooks_memory USING hnsw (embedding vector_cosine_ops);

Detection: there is no MCP server for this provider, so :meth:`detect`
always returns an empty list. The installer treats it as "manual config
required" and prompts for the DSN if the user wants to enable it.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Optional

from claude_hooks.embedders import Embedder, EmbedderError, make_embedder
from claude_hooks.providers.base import (
    Memory,
    Provider,
    ServerCandidate,
)

_SAFE_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

log = logging.getLogger("claude_hooks.providers.pgvector")


class PgvectorProvider(Provider):
    name = "pgvector"
    display_name = "Postgres pgvector"

    def __init__(self, server: ServerCandidate, options: Optional[dict] = None):
        super().__init__(server, options)
        self._embedder: Optional[Embedder] = None
        self._conn = None
        self._table_created = False

    # ------------------------------------------------------------------ #
    # Detection — there is no MCP server, so this is always empty.
    # The installer asks the user for a DSN if they want to enable.
    # ------------------------------------------------------------------ #
    @classmethod
    def signature_tools(cls) -> set[str]:
        return set()

    @classmethod
    def detect(cls, claude_config: dict) -> list[ServerCandidate]:
        return []

    @classmethod
    def verify(cls, server: ServerCandidate, *, timeout: float = 5.0) -> bool:
        try:
            import psycopg  # type: ignore
        except ImportError:
            log.warning("psycopg not installed — cannot verify pgvector")
            return False
        try:
            with psycopg.connect(server.url, connect_timeout=int(timeout)) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    cur.fetchone()
                    cur.execute("SELECT 1 FROM pg_extension WHERE extname='vector'")
                    if cur.fetchone() is None:
                        log.warning("pgvector extension not installed in target database")
                        return False
            return True
        except Exception as e:
            log.warning("pgvector verify failed: %s", e)
            return False

    # ------------------------------------------------------------------ #
    # Recall / store
    # ------------------------------------------------------------------ #
    def recall(self, query: str, k: int = 5) -> list[Memory]:
        if not query.strip():
            return []
        try:
            self._ensure_ready()
        except (ImportError, EmbedderError) as e:
            log.warning("pgvector unavailable: %s", e)
            return []
        try:
            qvec = self._embedder.embed(query)  # type: ignore[union-attr]
        except EmbedderError as e:
            log.warning("pgvector embed failed: %s", e)
            return []

        table = _safe_table(self.options.get("table") or "claude_hooks_memory")
        try:
            with self._conn.cursor() as cur:  # type: ignore[union-attr]
                cur.execute(
                    f"SELECT content, metadata, embedding <=> %s AS distance "
                    f"FROM {table} ORDER BY distance LIMIT %s",
                    (str(qvec), k),
                )
                rows = cur.fetchall()
        except Exception as e:
            log.warning("pgvector query failed: %s", e)
            return []
        result: list[Memory] = []
        for content, meta, distance in rows:
            meta = meta or {}
            meta["_distance"] = distance
            result.append(Memory(text=content, metadata=meta))
        return result

    def store(self, content: str, metadata: Optional[dict] = None) -> None:
        if not content.strip():
            return
        try:
            self._ensure_ready()
            vec = self._embedder.embed(content)  # type: ignore[union-attr]
        except (ImportError, EmbedderError) as e:
            raise RuntimeError(f"pgvector store failed: {e}")
        table = _safe_table(self.options.get("table") or "claude_hooks_memory")
        try:
            with self._conn.cursor() as cur:  # type: ignore[union-attr]
                cur.execute(
                    f"INSERT INTO {table} (content, metadata, embedding) VALUES (%s, %s, %s)",
                    (content, json.dumps(metadata or {}), str(vec)),
                )
                self._conn.commit()  # type: ignore[union-attr]
        except Exception as e:
            log.warning("pgvector insert failed: %s", e)
            raise

    def count(self) -> int:
        """Return the number of stored memories."""
        if self._conn is None:
            return 0
        table = _safe_table(self.options.get("table") or "claude_hooks_memory")
        try:
            with self._conn.cursor() as cur:  # type: ignore[union-attr]
                cur.execute(f"SELECT COUNT(*) FROM {table}")
                return cur.fetchone()[0]
        except Exception:
            return 0

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _ensure_ready(self) -> None:
        if self._embedder is None:
            self._embedder = make_embedder(
                self.options.get("embedder") or "null",
                self.options.get("embedder_options"),
            )
        if self._conn is None:
            try:
                import psycopg  # type: ignore
            except ImportError as e:
                raise ImportError("install psycopg: pip install psycopg[binary]") from e
            dsn = self.server.url or self.options.get("dsn") or ""
            if not dsn:
                raise RuntimeError("pgvector dsn not configured")
            self._conn = psycopg.connect(dsn)
        if not self._table_created:
            self._create_table()

    def _create_table(self) -> None:
        """Create the memory table + HNSW index if they don't exist."""
        table = _safe_table(self.options.get("table") or "claude_hooks_memory")
        dim = self._embedder.dim if self._embedder and self._embedder.dim else 0  # type: ignore[union-attr]

        # Check if table already exists.
        with self._conn.cursor() as cur:  # type: ignore[union-attr]
            cur.execute(
                "SELECT 1 FROM information_schema.tables WHERE table_name = %s",
                (table,),
            )
            if cur.fetchone():
                self._table_created = True
                return

        # Need the embedding dimension. Probe if unknown.
        if dim == 0:
            try:
                probe = self._embedder.embed("dimension probe")  # type: ignore[union-attr]
                dim = len(probe)
            except EmbedderError as e:
                raise RuntimeError(
                    f"cannot create table: need embedding dimension but embedder failed: {e}"
                )

        with self._conn.cursor() as cur:  # type: ignore[union-attr]
            cur.execute(
                f"""CREATE TABLE IF NOT EXISTS {table} (
                    id          BIGSERIAL PRIMARY KEY,
                    content     TEXT NOT NULL,
                    metadata    JSONB,
                    embedding   vector({dim}),
                    created_at  TIMESTAMPTZ DEFAULT now()
                )"""
            )
            cur.execute(
                f"""CREATE INDEX IF NOT EXISTS {table}_embedding_hnsw
                    ON {table} USING hnsw (embedding vector_cosine_ops)"""
            )
        self._conn.commit()  # type: ignore[union-attr]
        self._table_created = True
        log.info("created pgvector table: %s (dim=%d)", table, dim)


def _safe_table(name: str) -> str:
    """Validate a SQL identifier to prevent injection via config values."""
    if not _SAFE_IDENT_RE.match(name):
        raise ValueError(f"unsafe table name: {name!r}")
    return name
