"""
SQLite + sqlite-vec provider — *experimental scaffold*.

Disabled in DEFAULT_CONFIG. To use:

1. Install the optional deps: ``pip install sqlite-vec``
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

4. Run ``python install.py --init-sqlite-vec`` to create the schema.

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

Detection: there is no MCP server here either — :meth:`detect` returns
empty. The installer prompts for the db_path if the user wants to enable.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import struct
from typing import Optional

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

        table = self.options.get("table") or "memory"
        vec_blob = _pack_vec(qvec)
        try:
            cur = self._conn.execute(  # type: ignore[union-attr]
                f"""
                SELECT m.content, m.metadata
                FROM {table}_vec v
                JOIN {table} m ON m.rowid = v.rowid
                WHERE v.embedding MATCH ?
                ORDER BY v.distance
                LIMIT ?
                """,
                (vec_blob, k),
            )
            rows = cur.fetchall()
        except sqlite3.Error as e:
            log.warning("sqlite_vec query failed: %s", e)
            return []
        result: list[Memory] = []
        for content, meta_json in rows:
            try:
                meta = json.loads(meta_json) if meta_json else {}
            except json.JSONDecodeError:
                meta = {}
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

        table = self.options.get("table") or "memory"
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


def _pack_vec(vec: list[float]) -> bytes:
    """sqlite-vec accepts float32 little-endian blobs."""
    return struct.pack(f"{len(vec)}f", *vec)
