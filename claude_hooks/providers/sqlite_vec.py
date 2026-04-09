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
from claude_hooks.providers.base import (
    Memory,
    Provider,
    ServerCandidate,
)

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
        try:
            with self._conn:  # type: ignore[union-attr]
                cur = self._conn.execute(  # type: ignore[union-attr]
                    f"INSERT INTO {table} (content, metadata) VALUES (?, ?)",
                    (content, json.dumps(metadata or {})),
                )
                rowid = cur.lastrowid
                self._conn.execute(  # type: ignore[union-attr]
                    f"INSERT INTO {table}_vec (rowid, embedding) VALUES (?, ?)",
                    (rowid, vec_blob),
                )
        except sqlite3.Error as e:
            log.warning("sqlite_vec insert failed: %s", e)
            raise

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
        """Create the content + vec tables if they don't exist."""
        table = _safe_table(self.options.get("table") or "memory")
        dim = self._embedder.dim if self._embedder and self._embedder.dim else 0  # type: ignore[union-attr]

        # Check if tables already exist.
        cur = self._conn.execute(  # type: ignore[union-attr]
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        )
        if cur.fetchone():
            self._tables_created = True
            return

        # Need the embedding dimension. If the embedder hasn't set it yet,
        # do a probe embed to discover it.
        if dim == 0:
            try:
                probe = self._embedder.embed("dimension probe")  # type: ignore[union-attr]
                dim = len(probe)
            except EmbedderError as e:
                raise RuntimeError(
                    f"cannot create tables: need embedding dimension but embedder failed: {e}"
                )

        self._conn.execute(  # type: ignore[union-attr]
            f"""CREATE TABLE IF NOT EXISTS {table} (
                rowid       INTEGER PRIMARY KEY,
                content     TEXT NOT NULL,
                metadata    TEXT,
                created_at  TEXT DEFAULT (datetime('now'))
            )"""
        )
        self._conn.execute(  # type: ignore[union-attr]
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {table}_vec USING vec0(embedding float[{dim}])"
        )
        self._conn.commit()  # type: ignore[union-attr]
        self._tables_created = True
        log.info("created sqlite_vec tables: %s, %s_vec (dim=%d)", table, table, dim)


def _safe_table(name: str) -> str:
    """Validate a SQL identifier to prevent injection via config values."""
    if not _SAFE_IDENT_RE.match(name):
        raise ValueError(f"unsafe table name: {name!r}")
    return name


def _pack_vec(vec: list[float]) -> bytes:
    """sqlite-vec accepts float32 little-endian blobs."""
    return struct.pack(f"{len(vec)}f", *vec)
