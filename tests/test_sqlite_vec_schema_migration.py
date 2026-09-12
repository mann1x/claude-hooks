"""Tests for the v1.7.0 sqlite_vec schema migration.

The migration runs once per DB lifecycle, in-place, and must be:
- Idempotent: re-run is a no-op (same version, no extra writes).
- Recoverable: a partial run leaves the DB consistent so the next
  run picks up where it stopped.
- Non-destructive: legacy v1.6.x rows survive unchanged; only NEW
  columns/tables get added.
- Backfilling: existing rows that lack content_hash gain one
  derived from their content via the shared content_hash util.

These tests run against ``sqlite3.connect(':memory:')`` (fast,
isolated, no Ollama required) with the real sqlite_vec extension
loaded. They cover both the fresh-DB path (no v0 baseline yet)
and the legacy-DB path (v0 tables + rows pre-existing).
"""

from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


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


def _v0_seed(conn: sqlite3.Connection, *, dim: int = 4,
             table: str = "memory") -> None:
    """Build a v1.6.x-shape DB so the migration sees a legacy state."""
    conn.execute(
        f"CREATE TABLE {table} ("
        f"  rowid INTEGER PRIMARY KEY, content TEXT NOT NULL, "
        f"  metadata TEXT, created_at TEXT)"
    )
    conn.execute(
        f"CREATE VIRTUAL TABLE {table}_vec USING vec0(embedding float[{dim}])"
    )
    conn.commit()


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = ?", (name,)
    ).fetchone()
    return bool(row)


class TestFreshDB(unittest.TestCase):
    """Migration on an empty .db (no v0 baseline) must build v1 from
    scratch and end up identical to a legacy-then-migrated DB."""

    def setUp(self):
        _skip_if_no_sqlite_vec()
        self.conn = _conn()

    def tearDown(self):
        self.conn.close()

    def test_fresh_migration_returns_v1(self):
        from claude_hooks.providers.sqlite_vec_schema import (
            migrate_schema, LATEST_VERSION,
        )
        v = migrate_schema(self.conn, embedding_dim=4, table="memory")
        self.assertEqual(v, LATEST_VERSION)
        # M14: LATEST_VERSION bumped from 1 to 2. The migration must
        # land at the current latest, whatever that is.
        self.assertEqual(v, LATEST_VERSION)

    def test_fresh_creates_all_required_tables(self):
        from claude_hooks.providers.sqlite_vec_schema import migrate_schema
        migrate_schema(self.conn, embedding_dim=4, table="memory")
        for name in (
            "claude_hooks_schema",
            "memory", "memory_vec", "memory_fts",
            "kg_entities", "kg_entities_name_fts",
            "kg_relations",
            "kg_observations", "kg_observations_vec", "kg_observations_fts",
        ):
            self.assertTrue(
                _table_exists(self.conn, name),
                f"expected table/vtab {name!r} after migration",
            )

    def test_memory_has_content_hash_column(self):
        from claude_hooks.providers.sqlite_vec_schema import migrate_schema
        migrate_schema(self.conn, embedding_dim=4, table="memory")
        cols = _table_columns(self.conn, "memory")
        self.assertIn("content_hash", cols)

    def test_content_hash_unique_index_is_partial(self):
        """The UNIQUE on content_hash must skip NULLs so legacy
        duplicate-content rows don't block the migration."""
        from claude_hooks.providers.sqlite_vec_schema import migrate_schema
        migrate_schema(self.conn, embedding_dim=4, table="memory")
        idx = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='memory_content_hash_unique'"
        ).fetchone()
        self.assertIsNotNone(idx)
        self.assertIn("WHERE content_hash IS NOT NULL", idx[0])

    def test_metadata_records_tokenizer_probe(self):
        from claude_hooks.providers.sqlite_vec_schema import (
            migrate_schema, read_schema_metadata,
        )
        migrate_schema(self.conn, embedding_dim=4, table="memory")
        meta = read_schema_metadata(self.conn)
        self.assertIn("name_fts_tokenizer", meta)
        # Modern SQLite has trigram; older falls back. Both valid.
        self.assertIn(meta["name_fts_tokenizer"], ("trigram", "unicode61"))


class TestLegacyMigration(unittest.TestCase):
    """v0 (v1.6.x) DB with real data must migrate without losing rows."""

    def setUp(self):
        _skip_if_no_sqlite_vec()
        self.conn = _conn()
        _v0_seed(self.conn)
        # Three memos: two distinct, one duplicate of the second
        for content in (
            "alpha memo about bcache",
            "beta memo about pandorum",
            "beta memo about pandorum",  # duplicate
        ):
            self.conn.execute(
                "INSERT INTO memory(content, metadata) VALUES (?, '{}')",
                (content,),
            )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_rows_preserved(self):
        from claude_hooks.providers.sqlite_vec_schema import migrate_schema
        before = self.conn.execute("SELECT COUNT(*) FROM memory").fetchone()[0]
        migrate_schema(self.conn, embedding_dim=4, table="memory")
        after = self.conn.execute("SELECT COUNT(*) FROM memory").fetchone()[0]
        self.assertEqual(before, after)
        self.assertEqual(after, 3)

    def test_content_hash_backfilled_for_distinct_rows(self):
        from claude_hooks.providers.sqlite_vec_schema import migrate_schema
        from claude_hooks.providers._content_hash import content_hash
        migrate_schema(self.conn, embedding_dim=4, table="memory")
        rows = list(self.conn.execute(
            "SELECT rowid, content, content_hash FROM memory ORDER BY rowid"
        ))
        self.assertEqual(len(rows), 3)
        # First row: distinct content -> non-NULL hash matching algorithm.
        self.assertEqual(rows[0][2], content_hash(rows[0][1]))
        # Second row: also distinct -> non-NULL hash.
        self.assertEqual(rows[1][2], content_hash(rows[1][1]))

    def test_duplicate_content_row_kept_with_null_hash(self):
        """Documented behavior: duplicates of an already-hashed
        content stay with NULL content_hash (partial UNIQUE index
        skips NULLs). They're still searchable, just not eligible
        for future ON-CONFLICT idempotency.
        """
        from claude_hooks.providers.sqlite_vec_schema import migrate_schema
        migrate_schema(self.conn, embedding_dim=4, table="memory")
        third = self.conn.execute(
            "SELECT content_hash FROM memory WHERE rowid = 3"
        ).fetchone()
        self.assertIsNone(third[0])
        # And it survives — not deleted.
        self.assertEqual(
            self.conn.execute(
                "SELECT content FROM memory WHERE rowid = 3"
            ).fetchone()[0],
            "beta memo about pandorum",
        )

    def test_fts_populated_from_existing_rows(self):
        from claude_hooks.providers.sqlite_vec_schema import migrate_schema
        migrate_schema(self.conn, embedding_dim=4, table="memory")
        fts_count = self.conn.execute(
            "SELECT COUNT(*) FROM memory_fts"
        ).fetchone()[0]
        # All three rows present in FTS5 (the trigger handles new
        # inserts; the migration backfill handles pre-existing rows).
        self.assertEqual(fts_count, 3)

    def test_fts_finds_pre_existing_keyword(self):
        from claude_hooks.providers.sqlite_vec_schema import migrate_schema
        migrate_schema(self.conn, embedding_dim=4, table="memory")
        rows = list(self.conn.execute(
            "SELECT rowid FROM memory_fts WHERE memory_fts MATCH ? ORDER BY rowid",
            ("bcache",),
        ))
        self.assertEqual(rows, [(1,)])


class TestIdempotency(unittest.TestCase):
    """Re-runs must be no-ops; nothing should drift between runs."""

    def setUp(self):
        _skip_if_no_sqlite_vec()
        self.conn = _conn()

    def tearDown(self):
        self.conn.close()

    def test_rerun_returns_same_version(self):
        from claude_hooks.providers.sqlite_vec_schema import (
            migrate_schema, LATEST_VERSION,
        )
        v1 = migrate_schema(self.conn, embedding_dim=4, table="memory")
        v2 = migrate_schema(self.conn, embedding_dim=4, table="memory")
        v3 = migrate_schema(self.conn, embedding_dim=4, table="memory")
        self.assertEqual(v1, v2)
        self.assertEqual(v2, v3)
        # M14: track LATEST_VERSION (2) rather than hard-coding 1.
        self.assertEqual(v1, LATEST_VERSION)

    def test_rerun_does_not_duplicate_version_row(self):
        from claude_hooks.providers.sqlite_vec_schema import (
            migrate_schema, LATEST_VERSION,
        )
        migrate_schema(self.conn, embedding_dim=4, table="memory")
        # Snapshot row count after initial migration; M14 writes one
        # row per migration step, so a fresh DB landing at v2 holds
        # two rows (one for v1, one for v2). The invariant under
        # test is "re-running migrate_schema doesn't add new rows" —
        # i.e. row count is stable across no-op runs, not that it
        # equals 1.
        before = self.conn.execute(
            "SELECT COUNT(*) FROM claude_hooks_schema"
        ).fetchone()[0]
        self.assertGreaterEqual(before, 1)
        self.assertLessEqual(before, LATEST_VERSION)
        migrate_schema(self.conn, embedding_dim=4, table="memory")
        after = self.conn.execute(
            "SELECT COUNT(*) FROM claude_hooks_schema"
        ).fetchone()[0]
        self.assertEqual(before, after)

    def test_rerun_with_existing_data_doesnt_double_fts(self):
        from claude_hooks.providers.sqlite_vec_schema import migrate_schema
        # First migration on v0 with data
        _v0_seed(self.conn)
        self.conn.execute(
            "INSERT INTO memory(content, metadata) VALUES ('one', '{}')"
        )
        self.conn.commit()
        migrate_schema(self.conn, embedding_dim=4, table="memory")
        fts_after_first = self.conn.execute(
            "SELECT COUNT(*) FROM memory_fts"
        ).fetchone()[0]
        # Second migration is a no-op — version is already at the
        # current LATEST_VERSION (2 since M14; was 1 in v1.7).
        migrate_schema(self.conn, embedding_dim=4, table="memory")
        fts_after_second = self.conn.execute(
            "SELECT COUNT(*) FROM memory_fts"
        ).fetchone()[0]
        self.assertEqual(fts_after_first, fts_after_second)
        self.assertEqual(fts_after_first, 1)


class TestTriggersFireOnNewWrites(unittest.TestCase):
    """After migration, FTS5 mirrors must auto-update on INSERT/DELETE
    so the read path sees the same data the source table holds."""

    def setUp(self):
        _skip_if_no_sqlite_vec()
        self.conn = _conn()

    def tearDown(self):
        self.conn.close()

    def test_insert_propagates_to_fts(self):
        from claude_hooks.providers.sqlite_vec_schema import migrate_schema
        migrate_schema(self.conn, embedding_dim=4, table="memory")
        self.conn.execute(
            "INSERT INTO memory(content, content_hash, metadata) "
            "VALUES ('new memo', x'0123', '{}')"
        )
        self.conn.commit()
        rows = list(self.conn.execute(
            "SELECT rowid FROM memory_fts WHERE memory_fts MATCH 'memo'"
        ))
        self.assertEqual(len(rows), 1)

    def test_delete_propagates_to_fts(self):
        from claude_hooks.providers.sqlite_vec_schema import migrate_schema
        migrate_schema(self.conn, embedding_dim=4, table="memory")
        self.conn.execute(
            "INSERT INTO memory(content, content_hash, metadata) "
            "VALUES ('to delete', x'0123', '{}')"
        )
        self.conn.commit()
        rowid = self.conn.execute(
            "SELECT rowid FROM memory WHERE content='to delete'"
        ).fetchone()[0]
        self.conn.execute("DELETE FROM memory WHERE rowid = ?", (rowid,))
        self.conn.commit()
        hits = list(self.conn.execute(
            "SELECT rowid FROM memory_fts WHERE memory_fts MATCH 'delete'"
        ))
        self.assertEqual(hits, [])


class TestKGTablesAreFKEnforced(unittest.TestCase):
    """ON DELETE CASCADE on KG relations + observations requires
    PRAGMA foreign_keys = ON, which the migration sets."""

    def setUp(self):
        _skip_if_no_sqlite_vec()
        self.conn = _conn()
        from claude_hooks.providers.sqlite_vec_schema import migrate_schema
        migrate_schema(self.conn, embedding_dim=4, table="memory")

    def tearDown(self):
        self.conn.close()

    def test_foreign_keys_on(self):
        fk = self.conn.execute("PRAGMA foreign_keys").fetchone()[0]
        self.assertEqual(fk, 1)

    def test_delete_entity_cascades_observations(self):
        # Seed: entity + one observation
        self.conn.execute(
            "INSERT INTO kg_entities(name, entity_type) VALUES ('e1', 'server')"
        )
        eid = self.conn.execute(
            "SELECT id FROM kg_entities WHERE name='e1'"
        ).fetchone()[0]
        self.conn.execute(
            "INSERT INTO kg_observations(entity_id, content, content_hash) "
            "VALUES (?, 'observation x', x'aa')",
            (eid,),
        )
        self.conn.commit()
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM kg_observations"
            ).fetchone()[0], 1,
        )
        self.conn.execute("DELETE FROM kg_entities WHERE id = ?", (eid,))
        self.conn.commit()
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM kg_observations"
            ).fetchone()[0], 0,
        )

    def test_delete_entity_cascades_relations(self):
        self.conn.execute(
            "INSERT INTO kg_entities(name, entity_type) VALUES ('e1', 'x')"
        )
        self.conn.execute(
            "INSERT INTO kg_entities(name, entity_type) VALUES ('e2', 'x')"
        )
        e1, e2 = (r[0] for r in self.conn.execute(
            "SELECT id FROM kg_entities ORDER BY name"
        ).fetchall())
        self.conn.execute(
            "INSERT INTO kg_relations(from_entity_id, to_entity_id, relation_type) "
            "VALUES (?, ?, 'links_to')",
            (e1, e2),
        )
        self.conn.commit()
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM kg_relations"
            ).fetchone()[0], 1,
        )
        self.conn.execute("DELETE FROM kg_entities WHERE id = ?", (e1,))
        self.conn.commit()
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM kg_relations"
            ).fetchone()[0], 0,
        )


class TestRoundTripOnDisk(unittest.TestCase):
    """Persistence check: close + reopen the file and the migration
    state survives. Covers the real-world install scenario where
    the daemon restarts and reopens an existing .db."""

    def setUp(self):
        _skip_if_no_sqlite_vec()
        self.tmpdir = TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "memory.db"

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_reopen_sees_v1(self):
        # Name kept for blame stability; the assertion now pins
        # ``LATEST_VERSION`` (2 since M14, was 1 in v1.7) so the
        # test tracks the schema's current latest rather than a
        # hard-coded number.
        from claude_hooks.providers.sqlite_vec_schema import (
            migrate_schema, _read_version, LATEST_VERSION,
        )
        conn = _conn(str(self.db_path))
        try:
            migrate_schema(conn, embedding_dim=4, table="memory")
        finally:
            conn.close()
        # Reopen and re-run migrate; should be a no-op at LATEST_VERSION.
        conn = _conn(str(self.db_path))
        try:
            v = _read_version(conn)
            self.assertEqual(v, LATEST_VERSION)
            migrate_schema(conn, embedding_dim=4, table="memory")
            self.assertEqual(_read_version(conn), LATEST_VERSION)
        finally:
            conn.close()


class TestSafeTable(unittest.TestCase):
    """Reject SQL-injection-shaped table names at the schema boundary."""

    def setUp(self):
        _skip_if_no_sqlite_vec()
        self.conn = _conn()

    def tearDown(self):
        self.conn.close()

    def test_unsafe_table_name_rejected(self):
        from claude_hooks.providers.sqlite_vec_schema import migrate_schema
        for bad in ("memory; DROP TABLE foo", "a b", "a-b", "1bad",
                    "memory'", '"x"'):
            with self.assertRaises(ValueError):
                migrate_schema(self.conn, embedding_dim=4, table=bad)

    def test_valid_table_name_accepted(self):
        from claude_hooks.providers.sqlite_vec_schema import (
            migrate_schema, LATEST_VERSION,
        )
        for good in ("memory", "test_mem", "MyTable", "t1_2_3", "_under"):
            # Each runs against its own connection (else the second
            # call short-circuits on the version check).
            c = _conn()
            try:
                v = migrate_schema(c, embedding_dim=4, table=good)
                # M14: pin to LATEST_VERSION, not a hard-coded 1.
                self.assertEqual(v, LATEST_VERSION)
            finally:
                c.close()


if __name__ == "__main__":
    unittest.main()
