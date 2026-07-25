"""
SQLite + sqlite-vec provider.

Stores memories as embeddings in a local SQLite database with sqlite-vec
for vector similarity search. Zero infrastructure — just a .db file.

To use:

1. Install the optional dep: ``pip install sqlite-vec``
2. Pull an embedding model into Ollama: ``ollama pull nomic-embed-text``
3. Edit ``config/claude-hooks.json``:

   .. code-block:: json

       "sqlite_vec": {
         "enabled": true,
         "db_path": "~/.claude/claude-hooks-memory.db",
         "table": "memory",
         "embedder": "ollama",
         "embedder_options": {"model": "nomic-embed-text"}
       }

4. Tables are created automatically on first use.

Schema (one virtual table per collection, plus a metadata sidecar):

.. code-block:: sql

    -- Vectors live in a sqlite-vec virtual table
    CREATE VIRTUAL TABLE memory_vec USING vec0(
        embedding float[768]
    );

    -- Content + metadata in a regular table, joined by rowid
    CREATE TABLE memory (
        rowid       INTEGER PRIMARY KEY,
        content     TEXT NOT NULL,
        metadata    TEXT,                       -- JSON
        created_at  TEXT DEFAULT (datetime('now'))
    );

This shape lets us do filtered KNN with a simple JOIN.

Detection: there is no MCP server here — :meth:`detect` returns empty.
The installer prompts for the db_path if the user wants to enable.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import struct
from typing import Optional

_SAFE_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

from claude_hooks.config import expand_user_path
from claude_hooks.embedders import Embedder, EmbedderError, make_embedder
from claude_hooks.providers._content_hash import content_hash
from claude_hooks.providers.base import (
    Memory,
    Provider,
    ServerCandidate,
)
from claude_hooks.providers.sqlite_vec_schema import migrate_schema

log = logging.getLogger("claude_hooks.providers.sqlite_vec")


class SqliteVecProvider(Provider):
    name = "sqlite_vec"
    display_name = "SQLite + sqlite-vec"

    def __init__(self, server: ServerCandidate, options: Optional[dict] = None):
        super().__init__(server, options)
        self._embedder: Optional[Embedder] = None
        self._conn: Optional[sqlite3.Connection] = None
        self._tables_created = False

    # ------------------------------------------------------------------ #
    # Detection — no MCP server.
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
            import sqlite_vec  # type: ignore
        except ImportError:
            log.warning("sqlite_vec not installed")
            return False
        db_path = server.url or "?"
        try:
            p = expand_user_path(db_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(p))
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.execute("SELECT vec_version()")
            conn.close()
            return True
        except Exception as e:
            log.warning("sqlite_vec verify failed: %s", e)
            return False

    # ------------------------------------------------------------------ #
    # Recall / store
    # ------------------------------------------------------------------ #
    def recall(self, query: str, k: int = 5) -> list[Memory]:
        if not query.strip():
            return []
        try:
            self._ensure_ready()
            qvec = self._embedder.embed(query)  # type: ignore[union-attr]
        except (ImportError, EmbedderError) as e:
            log.warning("sqlite_vec unavailable: %s", e)
            return []
        return self._search_vec(qvec, k)

    def embed_for_store(self, content: str) -> Optional[list[float]]:
        """Embed once so dedup and store can share the result.

        Soft-fails to None: the caller then embeds the old way, which
        costs time but never loses the memory.
        """
        if not content.strip():
            return None
        try:
            self._ensure_ready()
            return self._embedder.embed(content)  # type: ignore[union-attr]
        except (ImportError, EmbedderError) as e:
            log.debug("sqlite_vec embed_for_store unavailable: %s", e)
            return None

    def recall_vec(self, vec: list[float], k: int = 5) -> Optional[list[Memory]]:
        """Search by a precomputed embedding — the query-side half of the
        single-embed store path."""
        if not vec:
            return None
        try:
            self._ensure_ready()
        except (ImportError, EmbedderError) as e:
            log.warning("sqlite_vec unavailable: %s", e)
            return []
        return self._search_vec(vec, k)

    def _search_vec(self, qvec: list[float], k: int) -> list[Memory]:
        table = _safe_table(self.options.get("table") or "memory")
        vec_blob = _pack_vec(qvec)
        try:
            cur = self._conn.execute(  # type: ignore[union-attr]
                f"""
                SELECT m.content, m.metadata, v.distance
                FROM {table}_vec v
                JOIN {table} m ON m.rowid = v.rowid
                WHERE v.embedding MATCH ?
                  AND k = ?
                ORDER BY v.distance
                """,
                (vec_blob, k),
            )
            rows = cur.fetchall()
        except sqlite3.Error as e:
            log.warning("sqlite_vec query failed: %s", e)
            return []
        result: list[Memory] = []
        for content, meta_json, distance in rows:
            try:
                meta = json.loads(meta_json) if meta_json else {}
            except json.JSONDecodeError:
                meta = {}
            meta["_distance"] = distance
            # Surface the source table so MCP formatters can render
            # ``[memory dist=0.5]`` instead of the ``[? dist=0.5]``
            # placeholder. sqlite_vec only ever queries one table, so
            # this is constant per-call, but the MCP formatter is
            # shared with pgvector_mcp which DOES use ``_table`` to
            # distinguish hits across multiple tables — populating it
            # here keeps the output shape symmetric.
            meta["_table"] = table
            result.append(Memory(text=content, metadata=meta))
        return result

    def store(self, content: str, metadata: Optional[dict] = None,
              vec: Optional[list[float]] = None) -> None:
        if not content.strip():
            return
        try:
            self._ensure_ready()
            # Reuse the dedup search's embedding when the caller has one.
            if vec is None:
                vec = self._embedder.embed(content)  # type: ignore[union-attr]
        except (ImportError, EmbedderError) as e:
            raise RuntimeError(f"sqlite_vec store failed: {e}")

        table = _safe_table(self.options.get("table") or "memory")
        vec_blob = _pack_vec(vec)
        ch = content_hash(content)
        # M14: pull expires_at out of metadata if the caller provided
        # one (the ProviderBackedStore adapter computes it from
        # ``StoreTTLConfig.ttl_for_namespace``). NULL means "never
        # expire", which is the legacy v1.7.0 behavior and stays the
        # default when the metadata key is absent.
        expires_at = None
        if isinstance(metadata, dict):
            ea = metadata.get("expires_at")
            if isinstance(ea, str) and ea.strip():
                expires_at = ea
        try:
            with self._conn:  # type: ignore[union-attr]
                # v1.7.0: idempotent on content_hash. Re-storing the
                # same (whitespace-normalised) content is a silent
                # no-op — same posture as pgvector's ON CONFLICT
                # (content_hash) DO NOTHING. ``RETURNING rowid``
                # gives us the new rowid on insert and nothing on
                # conflict, so we know whether to also push the
                # embedding into the _vec table.
                cur = self._conn.execute(  # type: ignore[union-attr]
                    # Partial unique index ``WHERE content_hash IS
                    # NOT NULL`` requires the same predicate in the
                    # conflict target so SQLite recognises the
                    # constraint. See ``CREATE UNIQUE INDEX`` in
                    # sqlite_vec_schema.py.
                    f"INSERT INTO {table}"
                    f"(content, content_hash, metadata, expires_at) "
                    f"VALUES (?, ?, ?, ?) "
                    f"ON CONFLICT(content_hash) "
                    f"  WHERE content_hash IS NOT NULL "
                    f"  DO NOTHING "
                    f"RETURNING rowid",
                    (
                        content, ch, json.dumps(metadata or {}),
                        expires_at,
                    ),
                )
                row = cur.fetchone()
                if row is None:
                    # Duplicate — content already in the store, with
                    # an embedding in _vec from the original insert.
                    # Nothing to do; silent no-op.
                    return
                rowid = row[0]
                self._conn.execute(  # type: ignore[union-attr]
                    f"INSERT INTO {table}_vec (rowid, embedding) VALUES (?, ?)",
                    (rowid, vec_blob),
                )
        except sqlite3.Error as e:
            log.warning("sqlite_vec insert failed: %s", e)
            raise

    # ------------------------------------------------------------------ #
    # M14 — TTL surface (per-row ``expires_at``)
    # ------------------------------------------------------------------ #

    def expire_before(
        self, *, before_iso: str, limit: int = 1000,
    ) -> list:
        """Return rows whose ``expires_at`` is non-NULL and lexically
        less than ``before_iso``.

        The lexical comparison is correct because the column stores
        ISO-8601 strings (sortable as text). The partial index on
        ``expires_at`` lets this query skip the non-TTL rows
        entirely; on a million-row store with N TTL'd rows it scans
        O(N), not O(million).

        Args:
            before_iso: any row with ``expires_at < before_iso`` is
                included. The daemon passes ``(now - grace).isoformat()``.
            limit: cap per call. The reaper drives this in a loop
                until it gets less than ``limit`` back (the standard
                "small page until empty" cleanup pattern).

        Returns:
            List of :class:`ExpiringRow` ordered by ``expires_at``
            ascending (oldest first). Empty when nothing matches.
        """
        from claude_hooks.providers._content_hash import ExpiringRow
        if not before_iso:
            return []
        self._ensure_ready()
        table = _safe_table(self.options.get("table") or "memory")
        try:
            rows = self._conn.execute(  # type: ignore[union-attr]
                f"SELECT content_hash, content, metadata, expires_at "
                f"FROM {table} "
                f"WHERE expires_at IS NOT NULL "
                f"AND expires_at < ? "
                f"ORDER BY expires_at ASC "
                f"LIMIT ?",
                (before_iso, int(limit)),
            ).fetchall()
        except sqlite3.Error as e:
            log.warning("sqlite_vec expire_before failed: %s", e)
            return []
        out: list = []
        for ch, content, meta_json, exp in rows:
            try:
                meta = json.loads(meta_json) if meta_json else {}
            except json.JSONDecodeError:
                meta = {}
            out.append(ExpiringRow(
                content_hash=bytes(ch) if ch is not None else b"",
                content=content or "",
                metadata=meta,
                expires_at=exp or "",
            ))
        return out

    def refresh_expires_at(
        self, content_hash_bytes: bytes, new_expires_iso: str,
    ) -> None:
        """Bump one row's ``expires_at`` forward.

        Driven by the ProviderBackedStore adapter's refresh-on-read
        closure: when a hit is returned to the caller, the adapter
        rolls the expiry forward by ``ttl_for_namespace`` so still-
        useful findings stay alive.

        Silent no-op when the row doesn't exist (hash was already
        deleted, or never existed). Errors log + raise so a buggy
        caller surfaces early in tests.
        """
        if not content_hash_bytes or not new_expires_iso:
            return
        self._ensure_ready()
        table = _safe_table(self.options.get("table") or "memory")
        try:
            with self._conn:  # type: ignore[union-attr]
                self._conn.execute(  # type: ignore[union-attr]
                    f"UPDATE {table} SET expires_at = ? "
                    f"WHERE content_hash = ?",
                    (new_expires_iso, content_hash_bytes),
                )
        except sqlite3.Error as e:
            log.warning("sqlite_vec refresh_expires_at failed: %s", e)
            raise

    def delete_by_hashes(self, hashes: list) -> int:
        """Hard-delete rows by ``content_hash``. Returns count deleted.

        Cascades to the ``_vec`` virtual table because both share
        the same ``rowid`` and SQLite's INSERT/DELETE triggers on
        ``<table>_fts`` keep the FTS5 mirror in sync. Empty input
        is a no-op.

        Caller must batch — SQLite parameter limit is 999 by default.
        The reaper batches in pages of 500.
        """
        if not hashes:
            return 0
        self._ensure_ready()
        table = _safe_table(self.options.get("table") or "memory")
        # Filter to non-empty bytes; SQLite IN-list with NULLs would
        # quietly drop matches.
        hashes = [bytes(h) for h in hashes if h]
        if not hashes:
            return 0
        placeholders = ",".join("?" for _ in hashes)
        try:
            with self._conn:  # type: ignore[union-attr]
                cur = self._conn.execute(  # type: ignore[union-attr]
                    f"DELETE FROM {table} "
                    f"WHERE content_hash IN ({placeholders})",
                    hashes,
                )
                return int(cur.rowcount or 0)
        except sqlite3.Error as e:
            log.warning("sqlite_vec delete_by_hashes failed: %s", e)
            raise

    def recall_hybrid(self, query: str, k: int = 5,
                       alpha: float = 0.5, rrf_k: int = 60) -> list[Memory]:
        """Hybrid recall = RRF blend of vector cosine + BM25 (FTS5).

        Mirror of pgvector's ``recall_hybrid``:

            score(doc) = alpha * 1/(rrf_k + rank_vec)
                       + (1-alpha) * 1/(rrf_k + rank_kw)

        Single-table version — sqlite_vec stores one logical table per
        provider instance, so no multi-table merging. Empty query →
        empty list. Either signal pass can fail silently (vector
        backend down, FTS5 query parse error) and the surviving signal
        still drives the ranking.
        """
        if not query.strip():
            return []
        try:
            self._ensure_ready()
            qvec = self._embedder.embed(query)  # type: ignore[union-attr]
        except (ImportError, EmbedderError) as e:
            log.warning("sqlite_vec hybrid unavailable: %s", e)
            return []
        table = _safe_table(self.options.get("table") or "memory")
        qblob = _pack_vec(qvec)
        # keyed by content_hash (bytes) so the vector and BM25 hits
        # for the same row land on the same entry; rows with NULL
        # content_hash (legacy duplicates surviving the migration)
        # are skipped because they can't be deduped.
        fused: dict[bytes, dict] = {}

        # vector pass
        try:
            vec_rows = self._conn.execute(  # type: ignore[union-attr]
                f"SELECT m.content, m.metadata, v.distance, m.content_hash "
                f"FROM {table}_vec v JOIN {table} m ON m.rowid = v.rowid "
                f"WHERE v.embedding MATCH ? AND k = ? "
                f"ORDER BY v.distance",
                (qblob, max(k * 4, 20)),
            ).fetchall()
        except sqlite3.Error as e:
            log.warning("sqlite_vec hybrid vector pass failed: %s", e)
            vec_rows = []
        for rank, (content, meta_json, distance, ch) in enumerate(
            vec_rows, start=1,
        ):
            if ch is None:
                continue
            entry = fused.setdefault(
                bytes(ch),
                _new_fused_entry(content, meta_json, table),
            )
            entry["vec_rank"] = rank
            entry["vec_distance"] = distance

        # BM25 pass via FTS5. bm25() returns a NEGATIVE score where
        # more-negative is more-relevant — same direction as ORDER BY
        # ASC, which is what we want for the LIMIT.
        try:
            kw_rows = self._conn.execute(  # type: ignore[union-attr]
                f"SELECT m.content, m.metadata, m.content_hash "
                f"FROM {table}_fts JOIN {table} m "
                f"  ON m.rowid = {table}_fts.rowid "
                f"WHERE {table}_fts MATCH ? "
                f"ORDER BY bm25({table}_fts) "
                f"LIMIT ?",
                (_fts5_query(query), max(k * 4, 20)),
            ).fetchall()
        except sqlite3.Error as e:
            log.debug("sqlite_vec hybrid BM25 pass skipped: %s", e)
            kw_rows = []
        for rank, (content, meta_json, ch) in enumerate(kw_rows, start=1):
            if ch is None:
                continue
            entry = fused.setdefault(
                bytes(ch),
                _new_fused_entry(content, meta_json, table),
            )
            entry["kw_rank"] = rank

        # RRF fusion
        for entry in fused.values():
            s = 0.0
            if entry["vec_rank"] is not None:
                s += alpha * (1.0 / (rrf_k + entry["vec_rank"]))
            if entry["kw_rank"] is not None:
                s += (1.0 - alpha) * (1.0 / (rrf_k + entry["kw_rank"]))
            entry["_score"] = s

        ranked = sorted(
            fused.values(), key=lambda e: e["_score"], reverse=True,
        )[:k]
        out: list[Memory] = []
        for e in ranked:
            try:
                meta = json.loads(e["metadata"]) if e["metadata"] else {}
            except json.JSONDecodeError:
                meta = {}
            meta["_table"] = e["table"]
            meta["_score"] = e["_score"]
            if e["vec_distance"] is not None:
                meta["_distance"] = e["vec_distance"]
            meta["_vec_rank"] = e["vec_rank"]
            meta["_kw_rank"] = e["kw_rank"]
            out.append(Memory(text=e["content"], metadata=meta))
        return out

    # ------------------------------------------------------------------ #
    # Knowledge-graph surface (v1.7.0)
    # ------------------------------------------------------------------ #
    # Port of pgvector.py's kg_* bodies translated to SQLite idioms:
    #   * executemany over individual cursors (sqlite3's cursor API is
    #     flatter than psycopg's — we use connection.execute directly)
    #   * ON CONFLICT … DO NOTHING (SQLite ≥3.24 supports the same
    #     keyword form as Postgres; older "INSERT OR IGNORE" is the
    #     fallback but we target ≥3.35 anyway for RETURNING)
    #   * name → id resolution via ``WHERE name IN (?, ?, ...)`` instead
    #     of Postgres's ``WHERE name = ANY(%s)``
    #   * FTS5 trigram tokenizer (or LIKE fallback if the host's stdlib
    #     SQLite lacks trigram, probed once at migration time) for the
    #     name-fuzzy pass

    def kg_create_entities(self, entities: list[dict]) -> int:
        """Bulk-create entities. Each dict: ``{name, entity_type, metadata?}``.
        Idempotent on ``name`` — duplicates are a no-op via
        ``ON CONFLICT(name) DO NOTHING``. Returns rows inserted."""
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
            with self._conn:  # type: ignore[union-attr]
                before = self._conn.execute(  # type: ignore[union-attr]
                    "SELECT COUNT(*) FROM kg_entities"
                ).fetchone()[0]
                self._conn.executemany(  # type: ignore[union-attr]
                    "INSERT INTO kg_entities (name, entity_type, metadata) "
                    "VALUES (?, ?, ?) ON CONFLICT(name) DO NOTHING",
                    rows,
                )
                after = self._conn.execute(  # type: ignore[union-attr]
                    "SELECT COUNT(*) FROM kg_entities"
                ).fetchone()[0]
                return after - before
        except sqlite3.Error as e:
            log.warning("sqlite_vec kg_create_entities failed: %s", e)
            raise

    def kg_add_observations(self, items: list[dict]) -> int:
        """Add observations. Each item: ``{entity_name, content}``.
        Idempotent on ``(entity_id, content_hash)``. Skips items
        whose entity name is unknown — caller should
        :meth:`kg_create_entities` first. Returns rows inserted."""
        pairs = [(i.get("entity_name", "").strip(), (i.get("content") or "").strip())
                 for i in items if isinstance(i, dict)]
        pairs = [(n, c) for (n, c) in pairs if n and c]
        if not pairs:
            return 0
        self._ensure_ready()
        try:
            vectors = self._embedder.embed_batch([c for _, c in pairs])  # type: ignore[union-attr]
        except EmbedderError as e:
            raise RuntimeError(f"sqlite_vec kg_add_observations embed failed: {e}")
        names = list({n for n, _ in pairs})
        placeholders = ",".join("?" * len(names))
        try:
            with self._conn:  # type: ignore[union-attr]
                cur = self._conn.execute(  # type: ignore[union-attr]
                    f"SELECT name, id FROM kg_entities WHERE name IN ({placeholders})",
                    names,
                )
                name_to_id = {row[0]: row[1] for row in cur.fetchall()}
                inserted = 0
                for (n, c), v in zip(pairs, vectors):
                    eid = name_to_id.get(n)
                    if eid is None:
                        log.debug("kg_add_observations: entity %r missing", n)
                        continue
                    ch = content_hash(c)
                    res = self._conn.execute(  # type: ignore[union-attr]
                        "INSERT INTO kg_observations (entity_id, content, content_hash) "
                        "VALUES (?, ?, ?) "
                        "ON CONFLICT(entity_id, content_hash) DO NOTHING "
                        "RETURNING id",
                        (eid, c, ch),
                    )
                    row = res.fetchone()
                    if row is None:
                        continue  # duplicate
                    obs_id = row[0]
                    # vec0 is not FK-linked; populate by rowid on each insert.
                    self._conn.execute(  # type: ignore[union-attr]
                        "INSERT INTO kg_observations_vec (rowid, embedding) "
                        "VALUES (?, ?)",
                        (obs_id, _pack_vec(v)),
                    )
                    inserted += 1
                return inserted
        except sqlite3.Error as e:
            log.warning("sqlite_vec kg_add_observations failed: %s", e)
            raise

    def kg_create_relations(self, relations: list[dict]) -> int:
        """Create relations. Each dict:
        ``{from, to, relation_type, metadata?}``.
        Idempotent on ``(from_entity_id, to_entity_id, relation_type)``."""
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
            with self._conn:  # type: ignore[union-attr]
                for f, t, rt, meta in rows:
                    # SQLite has no SELECT … INTO INSERT short-form; the
                    # cleanest equivalent is an INSERT … SELECT with the
                    # name-resolution join inline. ON CONFLICT keeps it
                    # idempotent on the unique triple.
                    res = self._conn.execute(  # type: ignore[union-attr]
                        "INSERT INTO kg_relations "
                        "  (from_entity_id, to_entity_id, relation_type, metadata) "
                        "SELECT a.id, b.id, ?, ? FROM kg_entities a, kg_entities b "
                        "  WHERE a.name = ? AND b.name = ? "
                        "ON CONFLICT(from_entity_id, to_entity_id, relation_type) "
                        "  DO NOTHING "
                        "RETURNING id",
                        (rt, meta, f, t),
                    )
                    if res.fetchone() is not None:
                        inserted += 1
            return inserted
        except sqlite3.Error as e:
            log.warning("sqlite_vec kg_create_relations failed: %s", e)
            raise

    def kg_search_nodes(self, query: str, k: int = 5) -> list[dict]:
        """Search KG entities by name (FTS5 trigram fuzzy) +
        observation content (RRF hybrid). Returns
        ``[{name, entity_type, metadata, observations: [...],
        _score, _match}]``.

        Three passes — mirror of pgvector's kg_search_nodes:
          1. entity-name fuzzy via FTS5 trigram tokenizer (or LIKE
             fallback if the host's stdlib SQLite lacks trigram)
          2. observation hybrid → resolve content to entity
          3. observation-fill: for entities found by name only, fetch
             their 3 most recent observations so the caller gets a
             useful payload either way
        """
        if not query.strip():
            return []
        self._ensure_ready()
        # cache the trigram-availability flag once per provider
        # instance — read from the schema-metadata row written by
        # migrate_schema().
        if not hasattr(self, "_name_fts_tokenizer"):
            from claude_hooks.providers.sqlite_vec_schema import read_schema_metadata
            try:
                meta = read_schema_metadata(self._conn)  # type: ignore[arg-type]
            except Exception:
                meta = {}
            self._name_fts_tokenizer = meta.get("name_fts", "trigram")

        out: dict[str, dict] = {}

        # Pass 1: entity-name fuzzy.
        try:
            if self._name_fts_tokenizer == "trigram":
                cur = self._conn.execute(  # type: ignore[union-attr]
                    "SELECT e.id, e.name, e.entity_type, e.metadata "
                    "FROM kg_entities e "
                    "WHERE e.id IN ("
                    "  SELECT rowid FROM kg_entities_name_fts "
                    "  WHERE name MATCH ? LIMIT ?"
                    ")",
                    (_fts5_query(query), k * 2),
                )
            else:
                cur = self._conn.execute(  # type: ignore[union-attr]
                    "SELECT id, name, entity_type, metadata FROM kg_entities "
                    "WHERE name LIKE '%' || ? || '%' "
                    "ORDER BY length(name) LIMIT ?",
                    (query, k * 2),
                )
            for eid, name, etype, meta_json in cur.fetchall():
                try:
                    meta = json.loads(meta_json) if meta_json else {}
                except json.JSONDecodeError:
                    meta = {}
                out[name] = {
                    "id": eid, "name": name, "entity_type": etype,
                    "metadata": meta,
                    "observations": [],
                    "_score": 1.0,  # name match gets a base score
                    "_match": "name",
                }
        except sqlite3.Error as e:
            log.debug("sqlite_vec kg_search name pass failed: %s", e)

        # Pass 2: observation hybrid → entity.
        obs_hits = self._hybrid_search_observations(query, k * 2)
        if obs_hits:
            contents = [c for c, _ in obs_hits]
            placeholders = ",".join("?" * len(contents))
            try:
                cur = self._conn.execute(  # type: ignore[union-attr]
                    f"SELECT e.id, e.name, e.entity_type, e.metadata, o.content "
                    f"FROM kg_observations o "
                    f"JOIN kg_entities e ON e.id = o.entity_id "
                    f"WHERE o.content IN ({placeholders})",
                    contents,
                )
                for eid, name, etype, meta_json, content in cur.fetchall():
                    try:
                        meta = json.loads(meta_json) if meta_json else {}
                    except json.JSONDecodeError:
                        meta = {}
                    node = out.setdefault(name, {
                        "id": eid, "name": name, "entity_type": etype,
                        "metadata": meta,
                        "observations": [],
                        "_score": 0.0,
                        "_match": "observation",
                    })
                    if content not in node["observations"]:
                        node["observations"].append(content)
                    node["_score"] += 0.5  # same constant as pgvector
            except sqlite3.Error as e:
                log.debug("sqlite_vec kg_search obs pass failed: %s", e)

        # Pass 3: observation-fill for name-matched entities.
        if out:
            need_obs = [n["id"] for n in out.values() if not n["observations"]][:k]
            if need_obs:
                placeholders = ",".join("?" * len(need_obs))
                try:
                    cur = self._conn.execute(  # type: ignore[union-attr]
                        f"SELECT entity_id, content FROM ("
                        f"  SELECT entity_id, content, "
                        f"    ROW_NUMBER() OVER (PARTITION BY entity_id ORDER BY id DESC) AS rn "
                        f"  FROM kg_observations "
                        f"  WHERE entity_id IN ({placeholders})"
                        f") WHERE rn <= 3",
                        need_obs,
                    )
                    by_id: dict[int, list[str]] = {}
                    for eid, content in cur.fetchall():
                        by_id.setdefault(eid, []).append(content)
                    for n in out.values():
                        if not n["observations"]:
                            n["observations"] = by_id.get(n["id"], [])
                except sqlite3.Error as e:
                    log.debug("sqlite_vec kg_search obs-fill failed: %s", e)

        ranked = sorted(out.values(), key=lambda n: n["_score"], reverse=True)[:k]
        for n in ranked:
            n.pop("id", None)  # internal — don't leak to callers
        return ranked

    def _hybrid_search_observations(self, query: str, k: int) -> list[tuple[str, float]]:
        """Inner RRF search over ``kg_observations``. Returns
        ``[(content, score)]`` — caller resolves content→entity."""
        if not query.strip():
            return []
        try:
            qvec = self._embedder.embed(query)  # type: ignore[union-attr]
        except EmbedderError as e:
            log.debug("kg obs embed failed: %s", e)
            qvec = None
        qblob = _pack_vec(qvec) if qvec is not None else None
        fused: dict[bytes, dict] = {}

        if qblob is not None:
            try:
                vec_rows = self._conn.execute(  # type: ignore[union-attr]
                    "SELECT o.content, o.content_hash, v.distance "
                    "FROM kg_observations_vec v "
                    "JOIN kg_observations o ON o.id = v.rowid "
                    "WHERE v.embedding MATCH ? AND k = ? "
                    "ORDER BY v.distance",
                    (qblob, max(k * 4, 20)),
                ).fetchall()
            except sqlite3.Error as e:
                log.debug("kg obs vec pass failed: %s", e)
                vec_rows = []
            for rank, (content, ch, dist) in enumerate(vec_rows, start=1):
                if ch is None:
                    continue
                fused.setdefault(bytes(ch), {
                    "content": content, "vec_rank": None, "kw_rank": None,
                })["vec_rank"] = rank

        try:
            kw_rows = self._conn.execute(  # type: ignore[union-attr]
                "SELECT o.content, o.content_hash "
                "FROM kg_observations_fts JOIN kg_observations o "
                "  ON o.id = kg_observations_fts.rowid "
                "WHERE kg_observations_fts MATCH ? "
                "ORDER BY bm25(kg_observations_fts) LIMIT ?",
                (_fts5_query(query), max(k * 4, 20)),
            ).fetchall()
        except sqlite3.Error as e:
            log.debug("kg obs BM25 pass failed: %s", e)
            kw_rows = []
        for rank, (content, ch) in enumerate(kw_rows, start=1):
            if ch is None:
                continue
            fused.setdefault(bytes(ch), {
                "content": content, "vec_rank": None, "kw_rank": None,
            })["kw_rank"] = rank

        rrf_k = 60
        out: list[tuple[str, float]] = []
        for entry in fused.values():
            s = 0.0
            if entry["vec_rank"] is not None:
                s += 0.5 / (rrf_k + entry["vec_rank"])
            if entry["kw_rank"] is not None:
                s += 0.5 / (rrf_k + entry["kw_rank"])
            out.append((entry["content"], s))
        out.sort(key=lambda t: t[1], reverse=True)
        return out[:k]

    def count(self) -> int:
        """Return the number of stored memories."""
        if self._conn is None:
            return 0
        table = _safe_table(self.options.get("table") or "memory")
        try:
            cur = self._conn.execute(f"SELECT COUNT(*) FROM {table}")
            return cur.fetchone()[0]
        except sqlite3.Error:
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
                import sqlite_vec  # type: ignore
            except ImportError as e:
                raise ImportError("install sqlite_vec: pip install sqlite-vec") from e
            db_path = self.server.url or self.options.get("db_path") or ""
            if not db_path:
                raise RuntimeError("sqlite_vec db_path not configured")
            p = expand_user_path(db_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(p))
            self._conn.enable_load_extension(True)
            sqlite_vec.load(self._conn)
        if not self._tables_created:
            self._create_tables()

    def _create_tables(self) -> None:
        """Bring the on-disk schema up to v1.7.0 (idempotent).

        Delegates to :mod:`sqlite_vec_schema.migrate_schema` which
        creates or extends the memory table + ``_vec`` + ``_fts`` +
        the KG cluster + bookkeeping in a single transaction.
        Re-runs are no-ops once the version row reads ``1``.
        """
        table = _safe_table(self.options.get("table") or "memory")
        dim = self._embedder.dim if self._embedder and self._embedder.dim else 0  # type: ignore[union-attr]
        if dim == 0:
            # Need the embedding dimension before creating the vec0
            # virtual table. Probe the embedder once.
            try:
                probe = self._embedder.embed("dimension probe")  # type: ignore[union-attr]
                dim = len(probe)
            except EmbedderError as e:
                raise RuntimeError(
                    f"cannot create tables: need embedding dimension but embedder failed: {e}"
                )
        migrate_schema(self._conn, embedding_dim=dim, table=table)  # type: ignore[arg-type]
        self._tables_created = True
        log.info("sqlite_vec schema ready: %s (dim=%d)", table, dim)


def _safe_table(name: str) -> str:
    """Validate a SQL identifier to prevent injection via config values."""
    if not _SAFE_IDENT_RE.match(name):
        raise ValueError(f"unsafe table name: {name!r}")
    return name


def _pack_vec(vec: list[float]) -> bytes:
    """sqlite-vec accepts float32 little-endian blobs."""
    return struct.pack(f"{len(vec)}f", *vec)


def _new_fused_entry(content: str, meta_json: Optional[str], table: str) -> dict:
    """Per-doc accumulator used by :meth:`recall_hybrid`.

    Mirrors pgvector's fused-entry shape so the downstream RRF + sort
    + Memory[] code paths line up byte-for-byte.
    """
    return {
        "content": content,
        "metadata": meta_json,
        "table": table,
        "vec_rank": None,
        "kw_rank": None,
        "vec_distance": None,
        "_score": 0.0,
    }


# FTS5 syntax characters that would otherwise let user input mean
# something special (NEAR/N, OR, *, -, "...", :, parentheses). For
# free-text recall we want everything taken literally — equivalent
# of pgvector's ``websearch_to_tsquery('english', %s)`` posture.
_FTS5_QUOTE_RE = re.compile(r'"')


def _fts5_query(query: str) -> str:
    """Wrap a free-text query so FTS5 treats it as a literal phrase.

    Quotes are doubled (FTS5's escape) and the whole thing is wrapped
    in ``"..."``. Empty / whitespace-only input becomes the empty
    string, which FTS5 will reject — the caller must guard upstream.
    """
    q = query.strip()
    if not q:
        return ""
    return '"' + _FTS5_QUOTE_RE.sub('""', q) + '"'
