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

    def store(self, content: str, metadata: Optional[dict] = None) -> None:
        if not content.strip():
            return
        try:
            self._ensure_ready()
            vec = self._embedder.embed(content)  # type: ignore[union-attr]
        except (ImportError, EmbedderError) as e:
            raise RuntimeError(f"sqlite_vec store failed: {e}")

        table = _safe_table(self.options.get("table") or "memory")
        vec_blob = _pack_vec(vec)
        ch = content_hash(content)
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
                    f"INSERT INTO {table}(content, content_hash, metadata) "
                    f"VALUES (?, ?, ?) "
                    f"ON CONFLICT(content_hash) "
                    f"  WHERE content_hash IS NOT NULL "
                    f"  DO NOTHING "
                    f"RETURNING rowid",
                    (content, ch, json.dumps(metadata or {})),
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
