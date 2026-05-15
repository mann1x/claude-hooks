"""SQLite schema + in-place migration for the sqlite_vec provider.

v1.7.0 brings sqlite_vec to full parity with pgvector — content_hash
idempotency, BM25-via-FTS5 hybrid recall, and a knowledge graph
(entities + relations + observations). All of that lives on top of
the v1.6.x schema (a regular memory table + a vec0 virtual table).
Existing DBs migrate in-place, idempotently, on first
``_ensure_ready()`` after upgrade.

Schema versions
---------------

- **v0** — legacy v1.6.x: ``<table>`` (rowid/content/metadata) +
  ``<table>_vec`` (vec0 embedding). No content_hash, no FTS, no KG.
- **v1** — v1.7.0 full parity: v0 plus content_hash + UNIQUE
  partial index + ``<table>_fts`` FTS5 mirror + ``kg_entities`` +
  ``kg_relations`` + ``kg_observations`` (+ vec + fts mirrors) +
  bookkeeping table.

Bookkeeping
-----------

``claude_hooks_schema(version, migrated_at, metadata)`` records the
applied schema version. ``metadata`` is a JSON string used to cache
probe results (e.g. whether the FTS5 ``trigram`` tokenizer is
available on this SQLite build) so the read-path doesn't re-probe
on every call.

Migration shape
---------------

One public function: ``migrate_schema(conn, *, embedding_dim,
table)``. Reads the current version (defaulting to 0 when the
bookkeeping table doesn't exist yet — that's a legacy v1.6.x DB or
a fresh empty DB), applies each step from current+1 up to v1 in a
single transaction, returns the new version. Re-runs are no-ops.

Why a separate module
---------------------

DDL strings get long. Keeping them out of ``sqlite_vec.py`` makes
the provider readable and gives migration tests an obvious import
target. The provider's ``_ensure_ready`` calls ``migrate_schema``
once per connection lifecycle.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from typing import Optional

from claude_hooks.providers._content_hash import content_hash

log = logging.getLogger("claude_hooks.providers.sqlite_vec_schema")

LATEST_VERSION = 1

# Identifier validation for the configurable memory table name. Same
# pattern sqlite_vec.py uses; duplicated here so the schema module
# doesn't have to import the provider.
_SAFE_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _safe_table(name: str) -> str:
    if not _SAFE_IDENT_RE.match(name):
        raise ValueError(f"unsafe table name: {name!r}")
    return name


# ---------------------------------------------------------------- #
# Public entry point
# ---------------------------------------------------------------- #


def migrate_schema(conn: sqlite3.Connection, *, embedding_dim: int,
                   table: str) -> int:
    """Bring the on-disk schema up to ``LATEST_VERSION``.

    Idempotent: re-runs that find an already-current schema return
    the version without touching the DB.

    Args:
        conn: an open ``sqlite3.Connection`` with the sqlite_vec
            extension already loaded (the caller does that).
        embedding_dim: the embedder's output dimension. Used to
            CREATE the vec0 virtual tables for memories and KG
            observations. Must match what the embedder actually
            produces; mismatch surfaces as a vec0 dimension error
            at insert time.
        table: the memories table name from
            ``cfg.providers.sqlite_vec.table``. Validated against
            ``_SAFE_IDENT_RE`` to prevent SQL injection via config.

    Returns:
        The version number after migration completes (1 in v1.7.0).
    """
    table = _safe_table(table)
    # Foreign-key enforcement is connection-scoped in SQLite and
    # ``ON DELETE CASCADE`` (KG relations + observations) is inert
    # without it. The provider re-runs this in _ensure_ready too so
    # any future connections get it, but pinning here protects the
    # migration itself.
    conn.execute("PRAGMA foreign_keys = ON")

    _ensure_bookkeeping(conn)
    current = _read_version(conn)
    if current >= LATEST_VERSION:
        return current

    # Apply steps in order. Each step is its own savepoint so a crash
    # mid-migration leaves the DB consistent up to the last completed
    # step; the next run picks up where it stopped.
    if current < 1:
        _migrate_v0_to_v1(conn, embedding_dim=embedding_dim, table=table)
        _write_version(conn, 1, _build_v1_metadata(conn))
        log.info("sqlite_vec schema migrated to v1 (table=%s, dim=%d)",
                 table, embedding_dim)
    return LATEST_VERSION


def read_schema_metadata(conn: sqlite3.Connection) -> dict:
    """Return the cached metadata JSON from ``claude_hooks_schema``.

    Empty dict if the bookkeeping table doesn't exist yet (caller is
    on a v0 DB that hasn't been migrated).
    """
    try:
        row = conn.execute(
            "SELECT metadata FROM claude_hooks_schema "
            "ORDER BY version DESC LIMIT 1"
        ).fetchone()
    except sqlite3.OperationalError:
        return {}
    if not row or not row[0]:
        return {}
    try:
        return json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        return {}


# ---------------------------------------------------------------- #
# Bookkeeping
# ---------------------------------------------------------------- #


def _ensure_bookkeeping(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS claude_hooks_schema (
            version      INTEGER PRIMARY KEY,
            migrated_at  TEXT NOT NULL DEFAULT (datetime('now')),
            metadata     TEXT NOT NULL DEFAULT '{}'
        )
        """
    )


def _read_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT version FROM claude_hooks_schema ORDER BY version DESC LIMIT 1"
    ).fetchone()
    return int(row[0]) if row else 0


def _write_version(conn: sqlite3.Connection, version: int, metadata: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO claude_hooks_schema(version, metadata) "
        "VALUES (?, ?)",
        (version, json.dumps(metadata, separators=(",", ":"))),
    )


def _build_v1_metadata(conn: sqlite3.Connection) -> dict:
    """Probe optional FTS5 features and cache the result.

    The trigram tokenizer ships with stock SQLite ≥3.34 (Nov 2020).
    Both target hosts run far newer, but the runbook documents a
    LIKE fallback for older builds — that fallback is driven off
    this metadata blob.
    """
    return {
        "name_fts_tokenizer": _probe_trigram_tokenizer(conn),
    }


def _probe_trigram_tokenizer(conn: sqlite3.Connection) -> str:
    """Return ``"trigram"`` if FTS5 supports it on this build,
    else ``"unicode61"`` (with ``LIKE`` fallback at read time)."""
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE _claude_hooks_trigram_probe "
            "USING fts5(x, tokenize='trigram case_sensitive 0')"
        )
        conn.execute("DROP TABLE _claude_hooks_trigram_probe")
        return "trigram"
    except sqlite3.OperationalError as e:
        log.info("FTS5 trigram tokenizer unavailable (%s); "
                 "kg_search will use LIKE fallback for name match", e)
        try:
            conn.execute("DROP TABLE IF EXISTS _claude_hooks_trigram_probe")
        except sqlite3.OperationalError:
            pass
        return "unicode61"


# ---------------------------------------------------------------- #
# v0 → v1
# ---------------------------------------------------------------- #


def _migrate_v0_to_v1(conn: sqlite3.Connection, *, embedding_dim: int,
                      table: str) -> None:
    """Bring a legacy v1.6.x DB (or a fresh empty DB) up to v1.7.0.

    Steps run in dependency order. Each is idempotent in isolation
    so partial-failure re-runs converge to the right state.
    """
    # Step 0: ensure the v0 baseline exists. A fresh DB has nothing;
    # a legacy DB has ``<table>`` + ``<table>_vec``. Either way we
    # CREATE IF NOT EXISTS so the rest of the migration has the
    # base tables to bolt onto.
    _create_v0_baseline(conn, embedding_dim=embedding_dim, table=table)

    # Step 1: add content_hash to the memory table if missing.
    _add_content_hash_column(conn, table=table)

    # Step 2: backfill content_hash for existing rows (NULL → hash(content)).
    _backfill_content_hash(conn, table=table)

    # Step 3: partial UNIQUE index on content_hash (skipping NULLs).
    # NULL-skip lets the backfill survive duplicate-content rows
    # without rolling back; the lost-duplicates stay with NULL hash
    # so they're visible to future queries but ineligible for
    # ``INSERT … ON CONFLICT(content_hash)`` idempotency.
    conn.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS {table}_content_hash_unique "
        f"ON {table}(content_hash) WHERE content_hash IS NOT NULL"
    )

    # Step 4: BM25 over content via FTS5 external-content virtual table.
    conn.execute(
        f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS {table}_fts USING fts5(
            content,
            content={table}, content_rowid=rowid,
            tokenize='unicode61 remove_diacritics 2'
        )
        """
    )
    conn.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS {table}_fts_ai
        AFTER INSERT ON {table} BEGIN
            INSERT INTO {table}_fts(rowid, content)
                VALUES (new.rowid, new.content);
        END
        """
    )
    conn.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS {table}_fts_ad
        AFTER DELETE ON {table} BEGIN
            INSERT INTO {table}_fts({table}_fts, rowid, content)
                VALUES('delete', old.rowid, old.content);
        END
        """
    )

    # Step 5: populate FTS5 from existing rows that aren't there yet.
    # We use ``INSERT INTO … SELECT … WHERE NOT EXISTS`` so re-runs
    # don't double-insert (the trigger catches new rows; this one-shot
    # backfill catches everything that existed before the migration).
    conn.execute(
        f"""
        INSERT INTO {table}_fts(rowid, content)
        SELECT m.rowid, m.content FROM {table} m
        WHERE NOT EXISTS (
            SELECT 1 FROM {table}_fts WHERE rowid = m.rowid
        )
        """
    )

    # Step 6: KG tables + indexes.
    _create_kg_entities(conn)
    _create_kg_relations(conn)
    _create_kg_observations(conn, embedding_dim=embedding_dim)

    # Step 7: name-fuzzy FTS5 for kg_entities. Tokenizer is whatever
    # the probe found (trigram preferred; unicode61 fallback).
    tokenizer = _probe_trigram_tokenizer(conn)
    conn.execute(
        f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS kg_entities_name_fts USING fts5(
            name,
            content=kg_entities, content_rowid=id,
            tokenize='{tokenizer} case_sensitive 0'
        )
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS kg_entities_name_fts_ai
        AFTER INSERT ON kg_entities BEGIN
            INSERT INTO kg_entities_name_fts(rowid, name)
                VALUES (new.id, new.name);
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS kg_entities_name_fts_ad
        AFTER DELETE ON kg_entities BEGIN
            INSERT INTO kg_entities_name_fts(kg_entities_name_fts, rowid, name)
                VALUES('delete', old.id, old.name);
        END
        """
    )


# ---------------------------------------------------------------- #
# Step helpers
# ---------------------------------------------------------------- #


def _create_v0_baseline(conn: sqlite3.Connection, *, embedding_dim: int,
                        table: str) -> None:
    """Create the v1.6.x base tables if absent. Idempotent."""
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {table} (
            rowid       INTEGER PRIMARY KEY,
            content     TEXT NOT NULL,
            metadata    TEXT,
            created_at  TEXT DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS {table}_vec "
        f"USING vec0(embedding float[{embedding_dim}])"
    )


def _add_content_hash_column(conn: sqlite3.Connection, *, table: str) -> None:
    """Add ``content_hash BLOB`` to ``<table>`` if missing.

    Idempotent via ``PRAGMA table_info`` instead of ``ALTER TABLE
    … ADD COLUMN IF NOT EXISTS`` because the IF-NOT-EXISTS clause
    on ADD COLUMN is SQLite ≥3.35 only and we want to work on
    older builds too.
    """
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if "content_hash" not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN content_hash BLOB")


def _backfill_content_hash(conn: sqlite3.Connection, *, table: str,
                           batch: int = 500) -> int:
    """Populate ``content_hash`` for any rows where it's NULL.

    Returns the number of rows updated. Runs in batches so a huge
    legacy DB doesn't load every row at once.

    Duplicate-content rows: collapsing them under a UNIQUE
    constraint would be destructive (which row wins?). Instead the
    backfill assigns the hash to whichever row it sees first;
    subsequent duplicates of the same content collide on the
    partial UNIQUE index we create next, and the migration steps
    skip them — they keep their existing rowid and content but get
    a NULL ``content_hash``. They're still queryable; they just
    won't participate in future ON-CONFLICT idempotency.
    """
    seen: set[bytes] = set()
    total = 0
    while True:
        rows = conn.execute(
            f"SELECT rowid, content FROM {table} "
            f"WHERE content_hash IS NULL LIMIT ?",
            (batch,),
        ).fetchall()
        if not rows:
            break
        for rowid, content in rows:
            if not content:
                continue
            h = content_hash(content)
            if h in seen:
                # Will collide on UNIQUE; skip this row's update
                # so it stays NULL (intentional — see docstring).
                continue
            try:
                conn.execute(
                    f"UPDATE {table} SET content_hash = ? WHERE rowid = ?",
                    (h, rowid),
                )
                seen.add(h)
                total += 1
            except sqlite3.IntegrityError:
                # UNIQUE collision against a hash that was already
                # written in an earlier migration attempt. Skip.
                continue
    if total:
        log.info("backfilled content_hash for %d row(s) in %s", total, table)
    return total


def _create_kg_entities(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS kg_entities (
            id           INTEGER PRIMARY KEY,
            name         TEXT NOT NULL UNIQUE,
            entity_type  TEXT NOT NULL,
            metadata     TEXT NOT NULL DEFAULT '{}',
            created_at   TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS kg_entities_type_idx "
        "ON kg_entities(entity_type)"
    )
    # SQLite analogue of pgvector's touch_updated_at() trigger.
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS kg_entities_touch
        AFTER UPDATE OF name, entity_type, metadata ON kg_entities
        BEGIN
            UPDATE kg_entities SET updated_at = datetime('now')
                WHERE id = old.id;
        END
        """
    )


def _create_kg_relations(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS kg_relations (
            id              INTEGER PRIMARY KEY,
            from_entity_id  INTEGER NOT NULL
                REFERENCES kg_entities(id) ON DELETE CASCADE,
            to_entity_id    INTEGER NOT NULL
                REFERENCES kg_entities(id) ON DELETE CASCADE,
            relation_type   TEXT NOT NULL,
            metadata        TEXT NOT NULL DEFAULT '{}',
            created_at      TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (from_entity_id, to_entity_id, relation_type)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS kg_relations_from_idx "
        "ON kg_relations(from_entity_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS kg_relations_to_idx "
        "ON kg_relations(to_entity_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS kg_relations_type_idx "
        "ON kg_relations(relation_type)"
    )


def _create_kg_observations(conn: sqlite3.Connection, *,
                            embedding_dim: int) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS kg_observations (
            id            INTEGER PRIMARY KEY,
            entity_id     INTEGER NOT NULL
                REFERENCES kg_entities(id) ON DELETE CASCADE,
            content       TEXT NOT NULL,
            content_hash  BLOB NOT NULL,
            created_at    TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (entity_id, content_hash)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS kg_observations_entity_idx "
        "ON kg_observations(entity_id)"
    )
    conn.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS kg_observations_vec "
        f"USING vec0(embedding float[{embedding_dim}])"
    )
    conn.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS kg_observations_fts USING fts5(
            content,
            content=kg_observations, content_rowid=id,
            tokenize='unicode61 remove_diacritics 2'
        )
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS kg_observations_fts_ai
        AFTER INSERT ON kg_observations BEGIN
            INSERT INTO kg_observations_fts(rowid, content)
                VALUES (new.id, new.content);
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS kg_observations_fts_ad
        AFTER DELETE ON kg_observations BEGIN
            INSERT INTO kg_observations_fts(kg_observations_fts, rowid, content)
                VALUES('delete', old.id, old.content);
        END
        """
    )
    # vec0 isn't FK-cascade aware; mirror the kg_observations delete.
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS kg_observations_vec_ad
        AFTER DELETE ON kg_observations BEGIN
            DELETE FROM kg_observations_vec WHERE rowid = old.id;
        END
        """
    )
