"""M14 — sqlite_vec schema v2 migration tests.

v2 adds an ``expires_at TEXT NULL`` column to ``<table>`` plus a
partial index ``<table>_expires_at_idx`` (skipping NULL rows). The
column lets the consultants-daemon's store-reaper expire rows
without scanning the whole memory table.

These tests cover:

- **Fresh-DB path** — a brand-new DB lands at v2 in one ``migrate_schema``
  call (v0 → v1 → v2 in one migration session).
- **Upgrade path** — a v1 DB (the v1.7.0 release shape) bumps to v2
  on first re-open without touching pre-existing rows.
- **Idempotency** — re-running on a v2 DB is a no-op.
- **Index shape** — the index is partial (``WHERE expires_at IS
  NOT NULL``) so non-TTL rows stay out of it.
- **Bookkeeping** — the version row at v2 carries metadata
  including ``has_expires_at: true`` so future code can probe the
  schema without re-running ``PRAGMA table_info``.

Most assertions mirror the v1 test file's structure for blame
locality.
"""
from __future__ import annotations

import json
import sqlite3
import unittest


def _skip_if_no_sqlite_vec():
    try:
        import sqlite_vec  # noqa: F401
    except ImportError:
        raise unittest.SkipTest("sqlite-vec not installed")


def _conn(path: str = ":memory:") -> sqlite3.Connection:
    import sqlite_vec
    c = sqlite3.connect(path)
    c.enable_load_extension(True)
    sqlite_vec.load(c)
    return c


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def _indexes_on(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='index' AND tbl_name = ? ",
        (table,),
    ).fetchall()
    return [r[0] for r in rows]


def _index_sql(conn: sqlite3.Connection, name: str) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = ?",
        (name,),
    ).fetchone()
    return row[0] if row else ""


def _migrate_to_v1_only(conn: sqlite3.Connection, *, table: str) -> None:
    """Manually walk the migration up to v1 ONLY, simulating an
    existing v1.7.0 .db that hasn't seen the M14 column yet.

    We can't just call ``migrate_schema`` because it now goes all
    the way to ``LATEST_VERSION`` (2). So we replay the v0→v1 steps
    directly and stamp the version row at 1 by hand.
    """
    from claude_hooks.providers.sqlite_vec_schema import (
        _ensure_bookkeeping,
        _migrate_v0_to_v1,
        _write_version,
        _build_v1_metadata,
    )
    conn.execute("PRAGMA foreign_keys = ON")
    _ensure_bookkeeping(conn)
    _migrate_v0_to_v1(conn, embedding_dim=4, table=table)
    _write_version(conn, 1, _build_v1_metadata(conn))
    conn.commit()


class TestFreshMigrationV2(unittest.TestCase):
    """A brand-new DB migrates v0 → v1 → v2 in one call."""

    def setUp(self):
        _skip_if_no_sqlite_vec()
        self.conn = _conn()

    def tearDown(self):
        self.conn.close()

    def test_fresh_migration_returns_v2(self):
        from claude_hooks.providers.sqlite_vec_schema import (
            migrate_schema, LATEST_VERSION,
        )
        v = migrate_schema(self.conn, embedding_dim=4, table="memory")
        self.assertEqual(v, LATEST_VERSION)
        self.assertEqual(v, 2)

    def test_fresh_creates_expires_at_column(self):
        from claude_hooks.providers.sqlite_vec_schema import migrate_schema
        migrate_schema(self.conn, embedding_dim=4, table="memory")
        cols = _table_columns(self.conn, "memory")
        self.assertIn("expires_at", cols)

    def test_fresh_creates_partial_index(self):
        from claude_hooks.providers.sqlite_vec_schema import migrate_schema
        migrate_schema(self.conn, embedding_dim=4, table="memory")
        indexes = _indexes_on(self.conn, "memory")
        self.assertIn("memory_expires_at_idx", indexes)
        sql = _index_sql(self.conn, "memory_expires_at_idx")
        # Partial index: must include the WHERE clause excluding NULL.
        self.assertIn("expires_at", sql)
        self.assertIn("NOT NULL", sql.upper())


class TestUpgradeFromV1ToV2(unittest.TestCase):
    """A v1 DB (the v1.7.0 release shape) bumps to v2 on re-open."""

    def setUp(self):
        _skip_if_no_sqlite_vec()
        self.conn = _conn()
        _migrate_to_v1_only(self.conn, table="memory")
        # Seed a v1 row that lacks expires_at semantics.
        self.conn.execute(
            "INSERT INTO memory (content, content_hash, metadata) "
            "VALUES ('legacy', ?, '{}')",
            (b"x" * 32,),
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_v1_db_upgrades_to_v2_on_migrate(self):
        from claude_hooks.providers.sqlite_vec_schema import (
            migrate_schema, _read_version,
        )
        # Pre-migrate: confirm we're really at v1 with no expires_at.
        self.assertEqual(_read_version(self.conn), 1)
        self.assertNotIn(
            "expires_at", _table_columns(self.conn, "memory"),
        )
        v = migrate_schema(
            self.conn, embedding_dim=4, table="memory",
        )
        self.assertEqual(v, 2)
        self.assertEqual(_read_version(self.conn), 2)
        self.assertIn(
            "expires_at", _table_columns(self.conn, "memory"),
        )

    def test_v1_row_survives_upgrade_with_null_expires_at(self):
        from claude_hooks.providers.sqlite_vec_schema import migrate_schema
        migrate_schema(self.conn, embedding_dim=4, table="memory")
        # The legacy row must still be there, with expires_at = NULL
        # so it stays out of the partial index ("live forever").
        row = self.conn.execute(
            "SELECT content, expires_at FROM memory WHERE content='legacy'"
        ).fetchone()
        self.assertEqual(row[0], "legacy")
        self.assertIsNone(row[1])

    def test_upgrade_writes_v2_bookkeeping_metadata(self):
        from claude_hooks.providers.sqlite_vec_schema import migrate_schema
        migrate_schema(self.conn, embedding_dim=4, table="memory")
        row = self.conn.execute(
            "SELECT version, metadata FROM claude_hooks_schema "
            "WHERE version = 2"
        ).fetchone()
        self.assertIsNotNone(row, "v2 bookkeeping row missing")
        meta = json.loads(row[1])
        self.assertTrue(meta.get("has_expires_at"))


class TestIdempotencyV2(unittest.TestCase):
    """Re-running migrate_schema on a v2 DB is a no-op."""

    def setUp(self):
        _skip_if_no_sqlite_vec()
        self.conn = _conn()

    def tearDown(self):
        self.conn.close()

    def test_rerun_on_v2_is_noop(self):
        from claude_hooks.providers.sqlite_vec_schema import (
            migrate_schema, _read_version,
        )
        migrate_schema(self.conn, embedding_dim=4, table="memory")
        before_v = _read_version(self.conn)
        before_cols = _table_columns(self.conn, "memory")
        before_rows = self.conn.execute(
            "SELECT COUNT(*) FROM claude_hooks_schema"
        ).fetchone()[0]
        migrate_schema(self.conn, embedding_dim=4, table="memory")
        self.assertEqual(_read_version(self.conn), before_v)
        self.assertEqual(_table_columns(self.conn, "memory"), before_cols)
        # Row count is stable (one row per migration step; with M14
        # a fresh-to-v2 DB has 2 rows: version=1 and version=2).
        after_rows = self.conn.execute(
            "SELECT COUNT(*) FROM claude_hooks_schema"
        ).fetchone()[0]
        self.assertEqual(after_rows, before_rows)

    def test_expires_at_column_only_added_once(self):
        """If ``_migrate_v1_to_v2`` ran twice naively, the second
        ALTER TABLE would raise ``duplicate column name`` because
        SQLite's ALTER has no IF NOT EXISTS. The PRAGMA table_info
        guard in the migration prevents that."""
        from claude_hooks.providers.sqlite_vec_schema import (
            _migrate_v1_to_v2, migrate_schema,
        )
        migrate_schema(self.conn, embedding_dim=4, table="memory")
        # Re-invoke the v1→v2 step directly — must not raise.
        _migrate_v1_to_v2(self.conn, table="memory")
        _migrate_v1_to_v2(self.conn, table="memory")
        # Still exactly one expires_at column.
        cols = _table_columns(self.conn, "memory")
        self.assertEqual(cols.count("expires_at"), 1)


class TestWriteAndReadExpiresAt(unittest.TestCase):
    """After v2 migration, ``expires_at`` writes round-trip cleanly."""

    def setUp(self):
        _skip_if_no_sqlite_vec()
        self.conn = _conn()
        from claude_hooks.providers.sqlite_vec_schema import migrate_schema
        migrate_schema(self.conn, embedding_dim=4, table="memory")

    def tearDown(self):
        self.conn.close()

    def test_can_insert_row_with_expires_at(self):
        self.conn.execute(
            "INSERT INTO memory (content, content_hash, expires_at) "
            "VALUES ('hello', ?, '2026-06-20T00:00:00+00:00')",
            (b"x" * 32,),
        )
        row = self.conn.execute(
            "SELECT expires_at FROM memory WHERE content='hello'"
        ).fetchone()
        self.assertEqual(row[0], "2026-06-20T00:00:00+00:00")

    def test_null_expires_at_stays_out_of_partial_index(self):
        # Partial index: WHERE expires_at IS NOT NULL. Two rows, one
        # with a value, one without. The index scan should find the
        # first but not the second.
        self.conn.execute(
            "INSERT INTO memory (content, content_hash, expires_at) "
            "VALUES ('past', ?, '2024-01-01T00:00:00+00:00')",
            (b"a" * 32,),
        )
        self.conn.execute(
            "INSERT INTO memory (content, content_hash) "
            "VALUES ('forever', ?)",
            (b"b" * 32,),
        )
        # Range query — equivalent to the daemon's expire-before
        # filter. Should return only the past row.
        rows = self.conn.execute(
            "SELECT content FROM memory "
            "WHERE expires_at IS NOT NULL "
            "AND expires_at < '2026-01-01T00:00:00+00:00'"
        ).fetchall()
        names = {r[0] for r in rows}
        self.assertEqual(names, {"past"})


if __name__ == "__main__":
    unittest.main()
