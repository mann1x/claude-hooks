"""Providers must recover when their backend restarts underneath them.

The bug this locks down, observed live on solidpc 2026-07-30: after the
Postgres container restarted, every long-lived process kept using a dead
connection **forever**, and turned each failure into a legitimate-looking
empty answer.

Two independent faults combined:

1. ``_ensure_ready`` rebuilt the connection only ``if self._conn is
   None``. A killed connection is not None — it is an object whose
   server is gone — so the rebuild never fired and the process kept
   handing the corpse to every subsequent call.
2. The failure paths return ``[]`` / ``0``, which is indistinguishable
   from "nothing stored". ``count()`` returned 0 with no log at all.

The asymmetry that made it confusing: SessionStart/UserPromptSubmit hooks
kept working, because each hook is a fresh short-lived process that gets
a fresh connection. Only the long-lived MCP servers and daemons were
broken, and they reported an empty memory rather than an error.

``qdrant`` and ``memory_kg`` are deliberately not covered here: they
build a fresh ``McpClient`` per call over stateless HTTP, so they hold
no connection that can go stale.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Optional

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from claude_hooks.providers.base import ServerCandidate  # noqa: E402
from claude_hooks.providers.pgvector import PgvectorProvider  # noqa: E402


# ===================================================================== #
# pgvector
# ===================================================================== #
class _Cur:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, params=None):
        self.conn.statements.append(sql)
        if self.conn.raise_on_execute:
            raise RuntimeError("server closed the connection unexpectedly")

    def fetchone(self):
        return (7,)

    def fetchall(self):
        return []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    """psycopg double. No ``fileno`` — the socket probe is skipped and
    the ``closed`` flag is the only signal, which is the shape every
    existing provider test double already has."""

    def __init__(self, closed: bool = False, raise_on_execute: bool = False):
        self.closed = closed
        self.raise_on_execute = raise_on_execute
        self.statements: list[str] = []
        self.commits = 0
        self.rollbacks = 0
        self.closed_calls = 0

    def cursor(self):
        return _Cur(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed_calls += 1
        self.closed = True


class _Emb:
    dim = 3

    def embed(self, text):
        return [0.1, 0.2, 0.3]

    def embed_batch(self, texts):
        return [self.embed(t) for t in texts]


def _pg(conn: Optional[_Conn], **options: Any) -> PgvectorProvider:
    p = PgvectorProvider(
        ServerCandidate(server_key="pgvector",
                        url="postgresql://stub@stub/stub", source="test"),
        options={"table": "test_mem", **options},
    )
    p._conn = conn              # type: ignore[attr-defined]
    p._embedder = _Emb()        # type: ignore[attr-defined]
    p._table_created = True     # type: ignore[attr-defined]
    return p


class TestPgvectorDeadConnectionDetection(unittest.TestCase):
    def test_none_is_dead(self):
        self.assertTrue(_pg(None)._connection_is_dead())

    def test_closed_flag_is_dead(self):
        self.assertTrue(_pg(_Conn(closed=True))._connection_is_dead())

    def test_healthy_connection_is_not_dead(self):
        self.assertFalse(_pg(_Conn())._connection_is_dead())

    def test_probe_does_not_open_a_transaction(self):
        """The probe must not perturb transaction state. An earlier
        implementation issued ``SELECT 1`` on every call, which opened a
        transaction and forced a rollback — visible to callers and to
        every test that counts commits."""
        conn = _Conn()
        p = _pg(conn)
        p._connection_is_dead()
        self.assertEqual(conn.statements, [])
        self.assertEqual(conn.rollbacks, 0)
        self.assertEqual(conn.commits, 0)


class TestPgvectorReconnects(unittest.TestCase):
    def test_dead_connection_is_replaced(self):
        """THE regression. Old code gated the rebuild on ``is None``, so
        a dead-but-present connection was reused forever."""
        from unittest import mock

        import psycopg

        dead, fresh = _Conn(closed=True), _Conn()
        p = _pg(dead)
        with mock.patch.object(psycopg, "connect", return_value=fresh):
            p._ensure_ready()
        self.assertIs(p._conn, fresh, "must reconnect, not reuse the corpse")
        self.assertEqual(dead.closed_calls, 1, "dead connection must be closed")

    def test_healthy_connection_is_kept(self):
        from unittest import mock

        import psycopg

        alive = _Conn()
        p = _pg(alive)
        with mock.patch.object(psycopg, "connect",
                               side_effect=AssertionError("must not reconnect")):
            p._ensure_ready()
        self.assertIs(p._conn, alive)

    def test_discard_resets_schema_flag(self):
        """A reconnect may land on a different database (restored
        volume, recreated container), so the DDL check must re-run."""
        conn = _Conn()
        p = _pg(conn)
        p._discard_connection()
        self.assertIsNone(p._conn)
        self.assertFalse(p._table_created)

    def test_safe_rollback_discards_an_unrollbackable_connection(self):
        class _NoRollback(_Conn):
            def rollback(self):
                raise RuntimeError("connection already gone")

        p = _pg(_NoRollback())
        p._safe_rollback()
        self.assertIsNone(p._conn, "unusable connection must be dropped")


class TestPgvectorCountIsHonest(unittest.TestCase):
    def test_count_runs_ensure_ready_even_with_a_connection(self):
        """``count`` used to call ``_ensure_ready`` only when ``_conn``
        was None, skipping recovery in exactly the case needing it."""
        from unittest import mock

        import psycopg

        dead, fresh = _Conn(closed=True), _Conn()
        p = _pg(dead)
        with mock.patch.object(psycopg, "connect", return_value=fresh):
            self.assertEqual(p.count(), 7)
        self.assertIs(p._conn, fresh)

    def test_count_logs_when_it_reports_zero(self):
        """A silent 0 is what made "backend down" look like "no
        memories". Every zero-returning failure must say so."""
        p = _pg(_Conn(raise_on_execute=True))
        with self.assertLogs("claude_hooks.providers.pgvector", "WARNING") as cm:
            self.assertEqual(p.count(), 0)
        self.assertTrue(any("NOT an empty corpus" in m for m in cm.output))


# ===================================================================== #
# sqlite_vec — same class of bug, different trigger
# ===================================================================== #
class _SqliteEmb:
    dim = 3

    def embed(self, text):
        return [0.1, 0.2, 0.3]

    def embed_batch(self, texts):
        return [self.embed(t) for t in texts]


@unittest.skipIf(
    __import__("importlib").util.find_spec("sqlite_vec") is None,
    "sqlite_vec not installed",
)
class TestSqliteVecRecovery(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = os.path.join(self.dir, "m.db")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _p(self):
        from claude_hooks.providers.sqlite_vec import SqliteVecProvider
        p = SqliteVecProvider(
            ServerCandidate(server_key="sqlite_vec", url=self.db, source="test"),
            {"table": "memory", "embedder": "null"},
        )
        p._embedder = _SqliteEmb()   # type: ignore[attr-defined]
        return p

    def test_count_connects_instead_of_reporting_zero(self):
        """``count`` returned 0 whenever ``_conn`` was None — i.e. on a
        freshly built provider it always claimed an empty corpus,
        which is what the SessionStart status line renders."""
        writer = self._p()
        writer.store("alpha", metadata={})
        writer._conn.close()

        fresh = self._p()
        self.assertEqual(fresh.count(), 1)
        fresh._conn.close()

    def test_replaced_database_file_is_reopened(self):
        """SQLite's analogue of a server restart. The old handle keeps
        serving the old inode happily — no error is ever raised — so
        this is the silent-wrong-answer case."""
        p = self._p()
        p.store("alpha", metadata={})
        p.store("beta", metadata={})
        self.assertEqual(p.count(), 2)

        shutil.move(self.db, self.db + ".old")
        other = self._p()
        other.store("gamma", metadata={})
        other._conn.close()

        self.assertEqual(p.count(), 1, "must see the NEW file, not the stale inode")
        self.assertTrue(any("gamma" in m.text for m in p.recall("gamma", k=5)))
        p._conn.close()

    def test_closed_handle_is_reopened(self):
        p = self._p()
        p.store("alpha", metadata={})
        p._conn.close()
        self.assertEqual(p.count(), 1)
        p._conn.close()

    def test_healthy_handle_is_not_reopened(self):
        p = self._p()
        p.store("alpha", metadata={})
        before = p._conn
        p.count()
        self.assertIs(p._conn, before, "must not churn a healthy connection")
        p._conn.close()

    def test_discard_resets_schema_flag(self):
        p = self._p()
        p.store("alpha", metadata={})
        p._discard_connection()
        self.assertIsNone(p._conn)
        self.assertFalse(p._tables_created)
        self.assertIsNone(p._db_identity)

    def test_dead_handle_detected(self):
        p = self._p()
        p.store("alpha", metadata={})
        conn = p._conn
        conn.close()
        self.assertTrue(p._connection_is_dead())
        with self.assertRaises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")


class TestStatelessProvidersUnaffected(unittest.TestCase):
    def test_qdrant_and_memory_kg_hold_no_connection(self):
        """Documents why they are exempt: a fresh McpClient per call
        means there is no long-lived handle to go stale. If either ever
        grows one, this test should start failing and the recovery
        logic above must be extended to it."""
        import inspect

        from claude_hooks.providers import memory_kg, qdrant
        for mod, cls in ((qdrant, "QdrantProvider"),
                         (memory_kg, "MemoryKgProvider")):
            src = inspect.getsource(getattr(mod, cls))
            self.assertNotIn("self._conn", src,
                             f"{cls} now holds a connection — extend recovery")


if __name__ == "__main__":
    unittest.main()
