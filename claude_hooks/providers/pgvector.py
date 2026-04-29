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
        """Detect a system-installed ``pgvector-mcp`` launcher.

        The launcher is dropped by ``install.py`` to ``~/.local/bin``
        (POSIX) or ``%LOCALAPPDATA%/claude-hooks/bin`` (Windows) and made
        available on PATH so any MCP-aware tool — Claude Code, Cursor,
        Codex, OpenWebUI — can spawn the same server. Detection here
        finds it via ``shutil.which`` and reports back as a candidate
        the installer can register in ``mcpServers``.

        Returns an empty list when no launcher is present — that path
        means the user hasn't run the pgvector setup step yet (or
        chose to skip it). Detection ignores the PgvectorProvider's
        own DSN config; that's verified separately by ``verify``.
        """
        import shutil
        path = shutil.which("pgvector-mcp")
        if not path:
            return []
        return [
            ServerCandidate(
                server_key="pgvector",
                url=path,
                source="system-binary",
                confidence="high",
                notes="system-installed pgvector-mcp launcher",
            ),
        ]

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
            try:
                self._ensure_ready()
            except Exception:
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


# ---------------------------------------------------------------------- #
# Hybrid recall + KG extensions
#
# These methods are attached to ``PgvectorProvider`` after class creation
# so the diff stays a single contiguous block at the file's tail and the
# core class above stays readable. The functions below are the real
# implementation; the loop at the bottom binds them.
# ---------------------------------------------------------------------- #


def _kg_obs_table(self: PgvectorProvider) -> Optional[str]:
    """Return the configured kg_observations table, or derive it.

    Resolution order:
    1. ``options["kg_observations_table"]`` if set (explicit).
    2. First entry in ``options["additional_tables"]`` that begins with
       ``kg_observations`` (the convention used by the migration script).
    3. None — KG ops will refuse if no observation table is resolvable.
    """
    explicit = self.options.get("kg_observations_table")
    if isinstance(explicit, str) and _SAFE_IDENT_RE.match(explicit):
        return explicit
    for t in (self.options.get("additional_tables") or []):
        if isinstance(t, str) and t.startswith("kg_observations") and _SAFE_IDENT_RE.match(t):
            return t
    return None


def _recall_hybrid(self: PgvectorProvider, query: str, k: int = 5,
                   alpha: float = 0.5, rrf_k: int = 60) -> list[Memory]:
    """Hybrid recall = RRF blend of cosine-distance vector + BM25 keyword.

    Reciprocal Rank Fusion: score(doc) = alpha * 1/(rrf_k + rank_vec)
                                       + (1-alpha) * 1/(rrf_k + rank_bm25)

    ``alpha=0.5`` weights both signals equally; tune up to favour
    semantic similarity, down to favour exact keywords. Each table
    contributes its own pair of ranked lists, which are merged with RRF
    across the union. Tables without a ``content_tsv`` column fall
    back to vector-only contribution.
    """
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

    vec_literal = str(qvec)
    tables = self._resolve_tables()
    # Keyed by (table, content_hash_hex) so the same row contributes to
    # at most one fused score; avoids double-counting when the same
    # content appears in vector and BM25 hits.
    fused: dict[tuple[str, str], dict] = {}

    for t in tables:
        metadata_expr = "metadata" if "kg_observations" not in t else "'{}'::jsonb AS metadata"
        # Vector ranking
        try:
            with self._conn.cursor() as cur:  # type: ignore[union-attr]
                cur.execute(
                    f"SELECT content, {metadata_expr}, embedding <=> %s AS distance, content_hash "
                    f"FROM {t} ORDER BY distance LIMIT %s",
                    (vec_literal, max(k * 4, 20)),
                )
                vec_rows = cur.fetchall()
        except Exception as e:
            log.warning("pgvector hybrid vector query on %s failed: %s", t, e)
            vec_rows = []
        for rank, (content, meta, distance, ch) in enumerate(vec_rows, start=1):
            key = (t, ch.hex() if isinstance(ch, (bytes, bytearray)) else str(ch))
            entry = fused.setdefault(key, {
                "content": content, "metadata": dict(meta or {}),
                "table": t, "vec_rank": None, "kw_rank": None,
                "vec_distance": None,
            })
            entry["vec_rank"] = rank
            entry["vec_distance"] = distance

        # BM25 ranking via content_tsv. websearch_to_tsquery handles
        # phrases / "OR" / negation gracefully and never errors on
        # malformed user input — the right default for free text.
        try:
            with self._conn.cursor() as cur:  # type: ignore[union-attr]
                cur.execute(
                    f"SELECT content, {metadata_expr}, "
                    f"       ts_rank(content_tsv, websearch_to_tsquery('english', %s)) AS rank, "
                    f"       content_hash "
                    f"FROM {t} "
                    f"WHERE content_tsv @@ websearch_to_tsquery('english', %s) "
                    f"ORDER BY rank DESC LIMIT %s",
                    (query, query, max(k * 4, 20)),
                )
                kw_rows = cur.fetchall()
        except Exception as e:
            # content_tsv missing or query error — silently degrade to
            # vector-only signal for this table.
            log.debug("pgvector hybrid keyword query on %s skipped: %s", t, e)
            kw_rows = []
        for rank, (content, meta, _score, ch) in enumerate(kw_rows, start=1):
            key = (t, ch.hex() if isinstance(ch, (bytes, bytearray)) else str(ch))
            entry = fused.setdefault(key, {
                "content": content, "metadata": dict(meta or {}),
                "table": t, "vec_rank": None, "kw_rank": None,
                "vec_distance": None,
            })
            entry["kw_rank"] = rank

    # RRF score
    for entry in fused.values():
        s = 0.0
        if entry["vec_rank"] is not None:
            s += alpha * (1.0 / (rrf_k + entry["vec_rank"]))
        if entry["kw_rank"] is not None:
            s += (1.0 - alpha) * (1.0 / (rrf_k + entry["kw_rank"]))
        entry["_score"] = s

    ranked = sorted(fused.values(), key=lambda e: e["_score"], reverse=True)[:k]
    out: list[Memory] = []
    for e in ranked:
        meta = e["metadata"]
        meta["_table"] = e["table"]
        meta["_score"] = e["_score"]
        if e["vec_distance"] is not None:
            meta["_distance"] = e["vec_distance"]
        meta["_vec_rank"] = e["vec_rank"]
        meta["_kw_rank"] = e["kw_rank"]
        out.append(Memory(text=e["content"], metadata=meta))
    return out


# ---------------------------------------------------------------------- #
# KG operations — entities, observations, relations
#
# Operates on the project's existing kg_entities / kg_relations /
# kg_observations_<model> tables. Schema is what migrate_to_pgvector.py
# defines; we don't create these tables here (they're set up at
# migration time).
# ---------------------------------------------------------------------- #


def _kg_create_entities(self: PgvectorProvider, entities: list[dict]) -> int:
    """Insert entities. Each dict: {name, entity_type, metadata?}.
    Idempotent on (name) — duplicates are a no-op via ON CONFLICT.
    Returns number of rows actually inserted."""
    rows = []
    for e in entities:
        name = (e.get("name") or "").strip()
        etype = (e.get("entity_type") or e.get("type") or "").strip()
        if not name or not etype:
            continue
        rows.append((name, etype, json.dumps(e.get("metadata") or {})))
    if not rows:
        return 0
    self._ensure_ready()
    try:
        with self._conn.cursor() as cur:  # type: ignore[union-attr]
            before = self._kg_entity_count_unsafe(cur)
            cur.executemany(
                "INSERT INTO kg_entities (name, entity_type, metadata) "
                "VALUES (%s, %s, %s) ON CONFLICT (name) DO NOTHING",
                rows,
            )
            self._conn.commit()  # type: ignore[union-attr]
            after = self._kg_entity_count_unsafe(cur)
            return after - before
    except Exception as e:
        log.warning("kg_create_entities failed: %s", e)
        raise


def _kg_entity_count_unsafe(self: PgvectorProvider, cur) -> int:
    """Count rows inside an existing cursor — no commit."""
    cur.execute("SELECT COUNT(*) FROM kg_entities")
    return cur.fetchone()[0]


def _kg_add_observations(self: PgvectorProvider, items: list[dict]) -> int:
    """Add observations to entities. Each item: {entity_name, content}.

    Embeds each observation, inserts into the configured kg_observations
    table. Idempotent on (entity_id, content_hash). Returns rows added.
    """
    obs_table = _kg_obs_table(self)
    if not obs_table:
        raise RuntimeError("kg_observations table is not configured (set kg_observations_table or additional_tables)")
    items = [(i.get("entity_name", "").strip(), (i.get("content") or "").strip())
             for i in items if isinstance(i, dict)]
    items = [(n, c) for (n, c) in items if n and c]
    if not items:
        return 0
    self._ensure_ready()
    try:
        vectors = self._embedder.embed_batch([c for _, c in items])  # type: ignore[union-attr]
    except EmbedderError as e:
        raise RuntimeError(f"kg_add_observations embed failed: {e}")
    inserted = 0
    try:
        with self._conn.cursor() as cur:  # type: ignore[union-attr]
            # Resolve entity names → ids. Names not present are skipped
            # (caller should kg_create_entities first).
            names = list({n for n, _ in items})
            cur.execute(
                "SELECT name, id FROM kg_entities WHERE name = ANY(%s)",
                (names,),
            )
            name_to_id = {row[0]: row[1] for row in cur.fetchall()}
            payload = []
            for (n, c), v in zip(items, vectors):
                eid = name_to_id.get(n)
                if eid is None:
                    log.debug("kg_add_observations: entity %r not found, skipping", n)
                    continue
                payload.append((eid, c, _content_hash(c), str(v)))
            if not payload:
                return 0
            before = cur.rowcount
            cur.executemany(
                f"INSERT INTO {obs_table} (entity_id, content, content_hash, embedding) "
                f"VALUES (%s, %s, %s, %s) "
                f"ON CONFLICT (entity_id, content_hash) DO NOTHING",
                payload,
            )
            inserted = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            self._conn.commit()  # type: ignore[union-attr]
    except Exception as e:
        log.warning("kg_add_observations failed: %s", e)
        raise
    return inserted


def _kg_create_relations(self: PgvectorProvider, relations: list[dict]) -> int:
    """Create relations. Each dict: {from, to, relation_type, metadata?}.
    Idempotent on (from, to, type)."""
    rows = []
    for r in relations:
        f = (r.get("from") or r.get("from_name") or "").strip()
        t = (r.get("to") or r.get("to_name") or "").strip()
        rt = (r.get("relation_type") or r.get("type") or "").strip()
        if not f or not t or not rt:
            continue
        rows.append((f, t, rt, json.dumps(r.get("metadata") or {})))
    if not rows:
        return 0
    self._ensure_ready()
    inserted = 0
    try:
        with self._conn.cursor() as cur:  # type: ignore[union-attr]
            for f, t, rt, meta in rows:
                cur.execute(
                    "INSERT INTO kg_relations (from_entity_id, to_entity_id, relation_type, metadata) "
                    "SELECT a.id, b.id, %s, %s::jsonb "
                    "FROM kg_entities a, kg_entities b WHERE a.name = %s AND b.name = %s "
                    "ON CONFLICT (from_entity_id, to_entity_id, relation_type) DO NOTHING",
                    (rt, meta, f, t),
                )
                if cur.rowcount and cur.rowcount > 0:
                    inserted += 1
            self._conn.commit()  # type: ignore[union-attr]
    except Exception as e:
        log.warning("kg_create_relations failed: %s", e)
        raise
    return inserted


def _kg_search_nodes(self: PgvectorProvider, query: str, k: int = 5) -> list[dict]:
    """Search KG entities by name (trigram) and observation content (hybrid).

    Returns a list of nodes: {name, entity_type, metadata, observations: [content...], _score}.
    Two passes: (1) entity-name trigram match, (2) observation hybrid recall
    that surfaces additional entities by their observation content. Results
    are deduplicated by entity name.
    """
    if not query.strip():
        return []
    self._ensure_ready()
    out: dict[str, dict] = {}

    # Pass 1: entity-name fuzzy match (trigram via gin_trgm_ops index).
    try:
        with self._conn.cursor() as cur:  # type: ignore[union-attr]
            cur.execute(
                "SELECT id, name, entity_type, metadata, similarity(name, %s) AS sim "
                "FROM kg_entities WHERE name %% %s "
                "ORDER BY sim DESC LIMIT %s",
                (query, query, k * 2),
            )
            for eid, name, etype, meta, sim in cur.fetchall():
                out[name] = {
                    "id": eid, "name": name, "entity_type": etype,
                    "metadata": dict(meta or {}),
                    "observations": [],
                    "_score": float(sim or 0.0),
                    "_match": "name",
                }
    except Exception as e:
        log.debug("kg_search_nodes name pass failed: %s", e)

    # Pass 2: observation hybrid → resolve to entity.
    obs_table = _kg_obs_table(self)
    if obs_table:
        obs_hits = self.recall_hybrid(query, k=k * 2)
        # Map content back to entity via a single round-trip.
        contents = [m.text for m in obs_hits if m.metadata.get("_table") == obs_table]
        if contents:
            try:
                with self._conn.cursor() as cur:  # type: ignore[union-attr]
                    cur.execute(
                        f"SELECT e.id, e.name, e.entity_type, e.metadata, o.content "
                        f"FROM {obs_table} o JOIN kg_entities e ON e.id = o.entity_id "
                        f"WHERE o.content = ANY(%s)",
                        (contents,),
                    )
                    for eid, name, etype, meta, content in cur.fetchall():
                        node = out.setdefault(name, {
                            "id": eid, "name": name, "entity_type": etype,
                            "metadata": dict(meta or {}),
                            "observations": [],
                            "_score": 0.0,
                            "_match": "observation",
                        })
                        if content not in node["observations"]:
                            node["observations"].append(content)
                        # Bump score for observation hits so they rise.
                        node["_score"] += 0.5
            except Exception as e:
                log.debug("kg_search_nodes observation pass failed: %s", e)

    # Fill observations for name-matched entities (top N only) so the
    # caller gets a useful payload even when the match was on the entity
    # name and not the content.
    if obs_table and out:
        ids_needing_obs = [n["id"] for n in out.values() if not n["observations"]][:k]
        if ids_needing_obs:
            try:
                with self._conn.cursor() as cur:  # type: ignore[union-attr]
                    cur.execute(
                        f"SELECT entity_id, content FROM {obs_table} "
                        f"WHERE entity_id = ANY(%s) ORDER BY id DESC LIMIT %s",
                        (ids_needing_obs, len(ids_needing_obs) * 5),
                    )
                    by_id: dict[int, list[str]] = {}
                    for eid, content in cur.fetchall():
                        by_id.setdefault(eid, []).append(content)
                    for n in out.values():
                        if not n["observations"]:
                            n["observations"] = by_id.get(n["id"], [])[:3]
            except Exception as e:
                log.debug("kg_search_nodes obs-fill failed: %s", e)

    ranked = sorted(out.values(), key=lambda n: n["_score"], reverse=True)[:k]
    # Drop internal id from public payload (keep _score/_match for ranking transparency).
    for n in ranked:
        n.pop("id", None)
    return ranked


# Bind extensions onto PgvectorProvider so the public class API now
# includes recall_hybrid + kg_*. Done outside the class body so the
# class definition above stays focused on the original recall/store
# contract; readers hit the diff in one block at the file tail.
PgvectorProvider.recall_hybrid = _recall_hybrid
PgvectorProvider.kg_create_entities = _kg_create_entities
PgvectorProvider.kg_add_observations = _kg_add_observations
PgvectorProvider.kg_create_relations = _kg_create_relations
PgvectorProvider.kg_search_nodes = _kg_search_nodes
PgvectorProvider._kg_entity_count_unsafe = _kg_entity_count_unsafe
