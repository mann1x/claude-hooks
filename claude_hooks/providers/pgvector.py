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

import hashlib
import json
import logging
import re
from typing import Optional


def _content_hash(text: str) -> bytes:
    """SHA-256 of normalised text — matches scripts/migrate_to_pgvector.py
    so production stores collide on the same content_hash key as migrated rows."""
    normalised = " ".join(text.split())
    return hashlib.sha256(normalised.encode("utf-8")).digest()

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

        return self._search_tables(qvec, k)

    def _resolve_tables(self) -> list[str]:
        """Return the validated list of tables to search.

        Always includes the primary ``table``. ``additional_tables`` (config)
        adds further per-table searches whose results merge with the primary
        — useful when one pgvector instance should serve both ``memories_*``
        and ``kg_observations_*`` (hybrid Qdrant + Memory-KG replacement).
        Tables in ``additional_tables`` MUST share the embedding dimension
        of the primary; pgvector enforces this at query time.
        """
        primary = _safe_table(self.options.get("table") or "claude_hooks_memory")
        extras = self.options.get("additional_tables") or []
        if not isinstance(extras, list):
            extras = []
        validated = [primary]
        for t in extras:
            if isinstance(t, str) and t and _SAFE_IDENT_RE.match(t):
                if t != primary and t not in validated:
                    validated.append(t)
        return validated

    def _search_tables(self, qvec: list[float], k: int) -> list[Memory]:
        """Search every resolved table for top-k, merge by distance, return top-k."""
        tables = self._resolve_tables()
        vec_literal = str(qvec)
        rows: list[tuple] = []
        for t in tables:
            try:
                with self._conn.cursor() as cur:  # type: ignore[union-attr]
                    # kg_observations tables don't carry a metadata column;
                    # synthesise one so the result row shape stays uniform.
                    metadata_expr = "metadata" if "kg_observations" not in t else "'{}'::jsonb AS metadata"
                    cur.execute(
                        f"SELECT content, {metadata_expr}, embedding <=> %s AS distance, %s AS _src "
                        f"FROM {t} ORDER BY distance LIMIT %s",
                        (vec_literal, t, k),
                    )
                    rows.extend(cur.fetchall())
            except Exception as e:
                log.warning("pgvector query on %s failed: %s", t, e)
                continue
        rows.sort(key=lambda r: r[2])
        result: list[Memory] = []
        for content, meta, distance, src in rows[:k]:
            meta = dict(meta) if meta else {}
            meta["_distance"] = distance
            meta["_table"] = src
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
                # ``content_hash`` matches the migration-script schema —
                # without it inserts into ``memories_<model>`` (which has
                # NOT NULL on content_hash) fail. ON CONFLICT DO NOTHING
                # makes repeat stores of identical content a silent no-op
                # rather than a unique-constraint error.
                cur.execute(
                    f"INSERT INTO {table} (content, content_hash, metadata, embedding) "
                    f"VALUES (%s, %s, %s, %s) "
                    f"ON CONFLICT (content_hash) DO NOTHING",
                    (content, _content_hash(content), json.dumps(metadata or {}), str(vec)),
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
    # Batch overrides
    # ------------------------------------------------------------------ #
    def batch_recall(self, queries: list[str], k: int = 5) -> list[list[Memory]]:
        """Embed all queries in one Ollama batch, then issue SQL queries
        in parallel via the shared connection. Each per-query SQL is fast
        (sub-2 ms), so the win comes from amortising the embed model load
        and HTTP round-trip — not from SQL parallelism."""
        queries = [q for q in queries if isinstance(q, str)]
        non_empty = [(i, q) for i, q in enumerate(queries) if q.strip()]
        if not non_empty:
            return [[] for _ in queries]
        try:
            self._ensure_ready()
        except (ImportError, EmbedderError) as e:
            log.warning("pgvector unavailable: %s", e)
            return [[] for _ in queries]
        try:
            vectors = self._embedder.embed_batch([q for _, q in non_empty])  # type: ignore[union-attr]
        except EmbedderError as e:
            log.warning("pgvector batch_embed failed: %s", e)
            return [[] for _ in queries]

        results: list[list[Memory]] = [[] for _ in queries]
        for (idx, _), vec in zip(non_empty, vectors):
            try:
                results[idx] = self._search_tables(vec, k)
            except Exception as e:
                log.warning("pgvector batch_recall query %d failed: %s", idx, e)
                continue
        return results

    def batch_store(self, items: list[tuple[str, Optional[dict]]]) -> None:
        """One Ollama batch + one ``executemany`` per chunk. Idempotent on
        unique (content). For large bulk loads use scripts/migrate_to_pgvector.py
        which uses the same shape."""
        items = [(c, m) for (c, m) in items if isinstance(c, str) and c.strip()]
        if not items:
            return
        try:
            self._ensure_ready()
        except (ImportError, EmbedderError) as e:
            raise RuntimeError(f"pgvector batch_store failed: {e}")
        try:
            vectors = self._embedder.embed_batch([c for c, _ in items])  # type: ignore[union-attr]
        except EmbedderError as e:
            raise RuntimeError(f"pgvector batch embed failed: {e}")
        table = _safe_table(self.options.get("table") or "claude_hooks_memory")
        params = [
            (c, _content_hash(c), json.dumps(m or {}), str(v))
            for (c, m), v in zip(items, vectors)
        ]
        try:
            with self._conn.cursor() as cur:  # type: ignore[union-attr]
                cur.executemany(
                    f"INSERT INTO {table} (content, content_hash, metadata, embedding) "
                    f"VALUES (%s, %s, %s, %s) "
                    f"ON CONFLICT (content_hash) DO NOTHING",
                    params,
                )
                self._conn.commit()  # type: ignore[union-attr]
        except Exception as e:
            log.warning("pgvector batch insert failed: %s", e)
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
            # Schema mirrors scripts/migrate_to_pgvector.py:schema_sql_for_model
            # so production stores from the live hook collide on the same
            # content_hash key as migration-loaded rows.
            cur.execute(
                f"""CREATE TABLE IF NOT EXISTS {table} (
                    id              BIGSERIAL PRIMARY KEY,
                    content         TEXT NOT NULL,
                    content_hash    BYTEA NOT NULL,
                    metadata        JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                    embedding       vector({dim}) NOT NULL,
                    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
                    CONSTRAINT {table}_content_hash_unique UNIQUE (content_hash)
                )"""
            )
            cur.execute(
                f"""CREATE INDEX IF NOT EXISTS {table}_embedding_hnsw
                    ON {table} USING hnsw (embedding vector_cosine_ops)
                    WITH (m = 16, ef_construction = 64)"""
            )
        self._conn.commit()  # type: ignore[union-attr]
        self._table_created = True
        log.info("created pgvector table: %s (dim=%d)", table, dim)


def _safe_table(name: str) -> str:
    """Validate a SQL identifier to prevent injection via config values."""
    if not _SAFE_IDENT_RE.match(name):
        raise ValueError(f"unsafe table name: {name!r}")
    return name
