"""
Postgres + pgvector provider — *experimental scaffold*.

Disabled in DEFAULT_CONFIG. To use:

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

5. Run ``python install.py --init-pgvector`` to create the table + index.

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
from typing import Optional

from claude_hooks.embedders import Embedder, EmbedderError, make_embedder
from claude_hooks.providers.base import (
    Memory,
    Provider,
    ServerCandidate,
)

log = logging.getLogger("claude_hooks.providers.pgvector")


class PgvectorProvider(Provider):
    name = "pgvector"
    display_name = "Postgres pgvector"

    def __init__(self, server: ServerCandidate, options: Optional[dict] = None):
        super().__init__(server, options)
        self._embedder: Optional[Embedder] = None
        self._conn = None

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

        table = self.options.get("table") or "claude_hooks_memory"
        try:
            with self._conn.cursor() as cur:  # type: ignore[union-attr]
                cur.execute(
                    f"SELECT content, metadata FROM {table} "
                    f"ORDER BY embedding <=> %s LIMIT %s",
                    (qvec, k),
                )
                rows = cur.fetchall()
        except Exception as e:
            log.warning("pgvector query failed: %s", e)
            return []
        return [Memory(text=row[0], metadata=row[1] or {}) for row in rows]

    def store(self, content: str, metadata: Optional[dict] = None) -> None:
        if not content.strip():
            return
        try:
            self._ensure_ready()
            vec = self._embedder.embed(content)  # type: ignore[union-attr]
        except (ImportError, EmbedderError) as e:
            raise RuntimeError(f"pgvector store failed: {e}")
        table = self.options.get("table") or "claude_hooks_memory"
        try:
            with self._conn.cursor() as cur:  # type: ignore[union-attr]
                cur.execute(
                    f"INSERT INTO {table} (content, metadata, embedding) VALUES (%s, %s, %s)",
                    (content, json.dumps(metadata or {}), vec),
                )
                self._conn.commit()  # type: ignore[union-attr]
        except Exception as e:
            log.warning("pgvector insert failed: %s", e)
            raise

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


CREATE_TABLE_SQL = """
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS {table} (
    id          BIGSERIAL PRIMARY KEY,
    content     TEXT NOT NULL,
    metadata    JSONB,
    embedding   vector({dim}),
    created_at  TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS {table}_embedding_hnsw
    ON {table} USING hnsw (embedding vector_cosine_ops);
"""
