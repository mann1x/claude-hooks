"""M14 — pgvector ``expires_at`` schema + provider methods.

Live-Postgres integration coverage lives in ``test_pgvector_integration.py``
(skipped when PG isn't reachable). These tests stay machine-portable
by injecting a **fake psycopg connection** that records every SQL
statement and its bind parameters. We assert against the recorded
SQL strings rather than against query results.

Coverage:

1. ``_create_table`` emits the ``ALTER TABLE … ADD COLUMN IF NOT
   EXISTS expires_at TIMESTAMPTZ`` statement.
2. ``_create_table`` also emits the partial index DDL with the
   correct ``WHERE expires_at IS NOT NULL`` clause.
3. ``store()`` includes the ``expires_at`` column + parameter when
   metadata carries one; passes ``None`` otherwise.
4. ``expire_before`` builds the SELECT with the right shape +
   bind params, and converts result rows to ``ExpiringRow``.
5. ``refresh_expires_at`` builds the UPDATE with the right shape.
6. ``delete_by_hashes`` uses ``= ANY(%s)`` (Postgres array form),
   not the SQLite IN-list form.
7. Empty inputs are no-ops (don't emit SQL).
"""
from __future__ import annotations

import unittest
from datetime import datetime, timezone
from typing import Any, Optional


class _FakeCursor:
    """psycopg Cursor double — records every execute() call.

    Each statement is captured as ``(sql, params)`` so tests can
    assert on the SQL shape without driving Postgres. ``fetchall``
    returns whatever was queued via ``set_rows``; ``fetchone``
    returns the first row of the queue.
    """

    def __init__(self, conn: "_FakeConn"):
        self.conn = conn
        self._rows: list[tuple] = []
        self.rowcount = 0

    def execute(self, sql: str, params: Optional[Any] = None) -> None:
        self.conn.statements.append((sql, params))
        # ``rowcount`` mirrors psycopg semantics: after UPDATE/DELETE
        # it reflects the rows affected. Tests that need a specific
        # value set ``self.conn.next_rowcount`` ahead of the call.
        self.rowcount = self.conn.next_rowcount

    def fetchall(self) -> list[tuple]:
        rows, self._rows = self.conn.queued_rows, []
        return rows

    def fetchone(self) -> Optional[tuple]:
        if self.conn.queued_rows:
            return self.conn.queued_rows[0]
        return None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self):
        self.statements: list[tuple[str, Any]] = []
        self.committed = 0
        self.queued_rows: list[tuple] = []
        self.next_rowcount = 0

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def commit(self) -> None:
        self.committed += 1

    def rollback(self) -> None:
        # Bumped on any failed transactional unit so tests can verify
        # the provider doesn't leave the connection aborted.
        self.committed -= 1

    def close(self) -> None:
        pass


class _FakeEmbedder:
    def embed(self, text: str) -> list[float]:
        return [0.1, 0.2, 0.3]

    @property
    def dim(self) -> int:
        return 3


def _make_provider() -> "tuple":
    """Build a PgvectorProvider with the fake connection wired in."""
    from claude_hooks.providers.base import ServerCandidate
    from claude_hooks.providers.pgvector import PgvectorProvider
    server = ServerCandidate(
        server_key="pgvector",
        url="postgresql://stub@stub/stub",
        source="test",
        confidence="high",
    )
    p = PgvectorProvider(server, options={"table": "test_mem"})
    conn = _FakeConn()
    p._conn = conn  # type: ignore[attr-defined]
    p._embedder = _FakeEmbedder()  # type: ignore[attr-defined]
    p._table_created = True  # type: ignore[attr-defined]
    return p, conn


def _stmts(conn: _FakeConn) -> list[str]:
    """Convenience: flatten conn.statements to just the SQL strings."""
    return [sql for sql, _ in conn.statements]


def _stmt_matching(conn: _FakeConn, *needles: str) -> Optional[tuple]:
    """Return the first (sql, params) whose SQL contains ALL the needles."""
    for sql, params in conn.statements:
        if all(n in sql for n in needles):
            return (sql, params)
    return None


class TestCreateTableEmitsAlter(unittest.TestCase):
    """``_create_table`` must add the M14 column + index even on
    pre-M14 tables (PG's ADD COLUMN IF NOT EXISTS handles re-runs)."""

    def test_alter_table_add_column_emitted(self):
        p, conn = _make_provider()
        # _create_table normally runs inside _ensure_ready; call it
        # directly so we don't have to drive the full connection
        # bring-up path through the fake.
        p._table_created = False  # type: ignore[attr-defined]
        # _create_table reads dim from self._embedder; the fake
        # already returns 3. No positional/keyword args needed.
        p._create_table()
        match = _stmt_matching(
            conn, "ALTER TABLE", "ADD COLUMN IF NOT EXISTS",
            "expires_at",
        )
        self.assertIsNotNone(
            match,
            f"ALTER TABLE ADD COLUMN expires_at not emitted; "
            f"statements: {_stmts(conn)}",
        )
        # Type must be TIMESTAMPTZ — the daemon depends on TZ-aware
        # comparisons.
        self.assertIn("TIMESTAMPTZ", match[0])

    def test_partial_index_emitted(self):
        p, conn = _make_provider()
        p._table_created = False  # type: ignore[attr-defined]
        # _create_table reads dim from self._embedder; the fake
        # already returns 3. No positional/keyword args needed.
        p._create_table()
        match = _stmt_matching(
            conn, "CREATE INDEX",
            "test_mem_expires_at_idx",
            "expires_at IS NOT NULL",
        )
        self.assertIsNotNone(
            match,
            f"partial index on expires_at not emitted; "
            f"statements: {_stmts(conn)}",
        )

    def test_alter_is_idempotent_via_if_not_exists(self):
        # PG ≥ 9.6 supports ADD COLUMN IF NOT EXISTS — confirm the
        # DDL string uses it so re-running on a v1.7 deploy doesn't
        # raise ``duplicate column``.
        p, conn = _make_provider()
        p._table_created = False  # type: ignore[attr-defined]
        # _create_table reads dim from self._embedder; the fake
        # already returns 3. No positional/keyword args needed.
        p._create_table()
        match = _stmt_matching(
            conn, "ALTER TABLE", "expires_at",
        )
        self.assertIsNotNone(match)
        self.assertIn("IF NOT EXISTS", match[0])


class TestStoreThreadsExpiresAt(unittest.TestCase):
    """``store()`` must pull ``expires_at`` from metadata and bind
    it to the INSERT — pass ``None`` when absent."""

    def test_expires_at_from_metadata_is_bound(self):
        p, conn = _make_provider()
        iso = "2026-12-01T00:00:00+00:00"
        p.store(
            "hello",
            metadata={
                "_consultants_store": True,
                "namespace": ["csl-x", "research"],
                "expires_at": iso,
            },
        )
        match = _stmt_matching(conn, "INSERT INTO", "expires_at")
        self.assertIsNotNone(
            match,
            f"INSERT including expires_at column missing; "
            f"statements: {_stmts(conn)}",
        )
        sql, params = match
        # expires_at is the 5th positional parameter (content,
        # content_hash, metadata, embedding, expires_at).
        self.assertEqual(len(params), 5)
        self.assertEqual(params[-1], iso)

    def test_store_without_metadata_expires_at_passes_none(self):
        p, conn = _make_provider()
        p.store("hello", metadata={"_consultants_store": True})
        match = _stmt_matching(conn, "INSERT INTO", "expires_at")
        self.assertIsNotNone(match)
        self.assertIsNone(match[1][-1])

    def test_non_string_expires_at_is_treated_as_none(self):
        # Defensive: caller passes something silly. Don't crash.
        p, conn = _make_provider()
        p.store("hello", metadata={"expires_at": 12345})
        match = _stmt_matching(conn, "INSERT INTO", "expires_at")
        self.assertIsNotNone(match)
        self.assertIsNone(match[1][-1])


class TestExpireBefore(unittest.TestCase):
    """``expire_before`` SQL shape + ExpiringRow conversion."""

    def test_sql_shape(self):
        p, conn = _make_provider()
        p.expire_before(before_iso="2026-06-01T00:00:00+00:00", limit=500)
        match = _stmt_matching(
            conn, "SELECT", "FROM test_mem",
            "expires_at IS NOT NULL",
            "ORDER BY expires_at ASC",
            "LIMIT",
        )
        self.assertIsNotNone(match)
        # The WHERE clause must compare against a TIMESTAMPTZ cast.
        self.assertIn("timestamptz", match[0].lower())
        # Two bind params: before_iso + limit.
        self.assertEqual(
            match[1],
            ("2026-06-01T00:00:00+00:00", 500),
        )

    def test_empty_before_iso_is_noop(self):
        p, conn = _make_provider()
        result = p.expire_before(before_iso="", limit=10)
        self.assertEqual(result, [])
        # No SQL fired.
        self.assertEqual(
            [s for s in _stmts(conn) if "SELECT" in s and "expires_at" in s],
            [],
        )

    def test_rows_converted_to_expiring_row(self):
        from claude_hooks.providers._content_hash import ExpiringRow
        p, conn = _make_provider()
        # Stage a row the way psycopg would return it:
        # BYTEA → memoryview / bytes, JSONB → dict, TIMESTAMPTZ → datetime.
        conn.queued_rows = [(
            b"\x01" * 32,
            "finding text",
            {"namespace": ["csl-x", "research"], "lane_idx": 3},
            datetime(2026, 1, 1, tzinfo=timezone.utc),
        )]
        out = p.expire_before(
            before_iso="2026-06-01T00:00:00+00:00", limit=10,
        )
        self.assertEqual(len(out), 1)
        self.assertIsInstance(out[0], ExpiringRow)
        self.assertEqual(out[0].content_hash, b"\x01" * 32)
        self.assertEqual(out[0].content, "finding text")
        self.assertEqual(
            out[0].metadata["namespace"], ["csl-x", "research"],
        )
        # Datetime → ISO string.
        self.assertEqual(out[0].expires_at, "2026-01-01T00:00:00+00:00")

    def test_expire_before_closes_readonly_transaction(self):
        """#218 (2026-05-18): a successful read-only SELECT must close
        the connection's transaction before returning. Without this,
        psycopg3 leaves the connection ``idle in transaction``
        holding AccessShareLock on the table, blocking any concurrent
        ALTER TABLE from another connection. The #214 cell-2 14-minute
        deadlock surfaced this — the M14 reaper sweep's expire_before
        held the lock while a researcher session's M14 lazy migration
        (ADD COLUMN IF NOT EXISTS expires_at) sat waiting.

        Verified via the fake conn's rollback counter (our fake
        decrements ``committed`` on every rollback). After a happy-
        path expire_before, ``committed`` must be lower than zero.
        """
        p, conn = _make_provider()
        # No queued rows — empty happy path.
        out = p.expire_before(
            before_iso="2026-06-01T00:00:00+00:00", limit=10,
        )
        self.assertEqual(out, [])
        # The fake's rollback decrements ``committed``; one rollback
        # call after the SELECT means committed == -1 (no prior commits
        # in this test).
        self.assertEqual(
            conn.committed, -1,
            "expire_before must rollback the read-only transaction "
            "to release AccessShareLock — see #218 forensic.",
        )


class TestRefreshExpiresAt(unittest.TestCase):
    """``refresh_expires_at`` is the refresh-on-read primitive."""

    def test_sql_shape(self):
        p, conn = _make_provider()
        new_iso = "2026-12-31T00:00:00+00:00"
        ch = b"\x07" * 32
        p.refresh_expires_at(ch, new_iso)
        match = _stmt_matching(
            conn, "UPDATE test_mem", "SET expires_at",
            "WHERE content_hash",
        )
        self.assertIsNotNone(match)
        self.assertIn("timestamptz", match[0].lower())
        self.assertEqual(match[1], (new_iso, ch))
        # Refresh commits — Postgres needs explicit commit to make
        # the UPDATE visible to other connections (e.g. the daemon).
        self.assertGreaterEqual(conn.committed, 1)

    def test_empty_hash_is_noop(self):
        p, conn = _make_provider()
        p.refresh_expires_at(b"", "2026-12-31T00:00:00+00:00")
        # No UPDATE fired.
        self.assertEqual(
            [s for s in _stmts(conn) if "UPDATE" in s],
            [],
        )

    def test_empty_new_iso_is_noop(self):
        p, conn = _make_provider()
        p.refresh_expires_at(b"\x01" * 32, "")
        self.assertEqual(
            [s for s in _stmts(conn) if "UPDATE" in s],
            [],
        )


class TestDeleteByHashes(unittest.TestCase):
    """``delete_by_hashes`` uses PG's ANY(%s) array form."""

    def test_delete_uses_any_array_form(self):
        p, conn = _make_provider()
        conn.next_rowcount = 3
        hashes = [b"\x01" * 32, b"\x02" * 32, b"\x03" * 32]
        n = p.delete_by_hashes(hashes)
        match = _stmt_matching(
            conn, "DELETE FROM test_mem",
            "content_hash = ANY(",
        )
        self.assertIsNotNone(match)
        self.assertEqual(match[1], (hashes,))
        self.assertEqual(n, 3)
        self.assertGreaterEqual(conn.committed, 1)

    def test_empty_input_is_noop(self):
        p, conn = _make_provider()
        n = p.delete_by_hashes([])
        self.assertEqual(n, 0)
        self.assertEqual(
            [s for s in _stmts(conn) if "DELETE" in s],
            [],
        )

    def test_all_empty_hashes_filtered_out(self):
        p, conn = _make_provider()
        n = p.delete_by_hashes([b"", None, b""])  # type: ignore[list-item]
        self.assertEqual(n, 0)
        self.assertEqual(
            [s for s in _stmts(conn) if "DELETE" in s],
            [],
        )


class TestCreateTableMigratesExistingTables(unittest.TestCase):
    """Regression gate for the M14 bug surfaced by the live
    distillation smoke (2026-05-18): when the table already exists
    (pre-M14 deployment), ``_create_table`` early-returned BEFORE
    the ALTER TABLE / CREATE INDEX, so the user's live
    ``memories_qwen3`` would never grow the ``expires_at`` column
    and ``expire_before`` would fail with ``column does not exist``.

    The fix splits the create branch from the migration branch:
    create is skipped when the table exists, but the additive M14
    migration (ALTER + CREATE INDEX, both IF NOT EXISTS) runs on
    every connection.
    """

    def test_existing_table_still_gets_alter_and_index(self):
        p, conn = _make_provider()
        # Simulate "table already exists" — the
        # ``SELECT 1 FROM information_schema.tables`` fetchone returns
        # a row.
        conn.queued_rows = [(1,)]
        p._table_created = False  # type: ignore[attr-defined]
        p._create_table()
        # CREATE TABLE must NOT be emitted on the existing-table path.
        self.assertIsNone(
            _stmt_matching(conn, "CREATE TABLE IF NOT EXISTS test_mem"),
            f"CREATE TABLE wrongly emitted on existing table; "
            f"statements: {_stmts(conn)}",
        )
        # ALTER TABLE + partial index MUST still be emitted so live
        # pre-M14 tables migrate in place.
        self.assertIsNotNone(
            _stmt_matching(
                conn, "ALTER TABLE", "ADD COLUMN IF NOT EXISTS",
                "expires_at",
            ),
            f"ALTER TABLE not emitted on existing-table path "
            f"(M14 regression); statements: {_stmts(conn)}",
        )
        self.assertIsNotNone(
            _stmt_matching(
                conn, "CREATE INDEX",
                "test_mem_expires_at_idx",
                "expires_at IS NOT NULL",
            ),
            f"CREATE INDEX not emitted on existing-table path; "
            f"statements: {_stmts(conn)}",
        )
        # The migration block commits — required so the next query on
        # this connection doesn't see "current transaction is
        # aborted".
        self.assertGreaterEqual(conn.committed, 1)

    def test_migration_rolls_back_on_failure(self):
        """If the ALTER TABLE blows up (e.g. permissions, race), the
        connection must be rolled back before the exception
        propagates — otherwise subsequent queries fail with
        ``current transaction is aborted, commands ignored``."""

        class _RaisingCursor(_FakeCursor):
            def execute(self, sql, params=None):
                if "ADD COLUMN IF NOT EXISTS expires_at" in sql:
                    raise RuntimeError("simulated DDL failure")
                super().execute(sql, params)

        class _RaisingConn(_FakeConn):
            def cursor(self):
                return _RaisingCursor(self)

        p, _ = _make_provider()
        bad = _RaisingConn()
        bad.queued_rows = [(1,)]  # table already exists
        p._conn = bad  # type: ignore[attr-defined]
        p._table_created = False  # type: ignore[attr-defined]
        with self.assertRaises(RuntimeError):
            p._create_table()
        # rollback() in our fake decrements ``committed`` — confirm
        # it ran so a real psycopg connection would clear the
        # aborted-transaction state.
        self.assertLess(bad.committed, 0)


if __name__ == "__main__":
    unittest.main()
