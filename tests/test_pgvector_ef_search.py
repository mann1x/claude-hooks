"""``hnsw.ef_search`` tuning on the pgvector provider.

pgvector defaults ``ef_search`` to 40. We raise it per-connection because
the accuracy/latency trade is priced unusually here: the HNSW scan costs
~1 ms against a multi-second embed, and a dedup search that misses a
near-duplicate writes a permanently duplicated memory.

Swept on solidpc 2026-07-25 (memories_qwen3, 5835 rows, 200 perturbed
queries, recall@5 measured against an exact seq scan):

    ef=40   99.90%  p50 0.65 ms       ef=150  100.00%  p50 1.46 ms
    ef=64  100.00%  p50 0.83 ms       ef=200  100.00%  p50 1.45 ms
    ef=100 100.00%  p50 1.18 ms       ef=400  100.00%  p50 16.53 ms

The ef=400 row is the one to remember: past ~200 the planner's cost
estimate for the index scan exceeds a seq scan and it stops using the
HNSW index at all. Higher is not monotonically better-and-slower -- it
falls off a cliff -- which is why the default is a measured 100 rather
than "as high as we can afford".

The setting is applied per connection rather than by ALTER DATABASE so
it travels with the code to every host instead of living in one
machine's server config.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any, Optional

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from claude_hooks.providers.base import ServerCandidate  # noqa: E402
from claude_hooks.providers.pgvector import (  # noqa: E402
    DEFAULT_EF_SEARCH,
    PgvectorProvider,
)


class _FakeCursor:
    def __init__(self, conn: "_FakeConn"):
        self.conn = conn

    def execute(self, sql: str, params: Optional[Any] = None) -> None:
        if self.conn.raise_on_execute:
            raise RuntimeError("simulated: unrecognized configuration parameter")
        self.conn.statements.append((sql, params))

    def fetchall(self):
        return []

    def fetchone(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, raise_on_execute: bool = False):
        self.statements: list[tuple] = []
        self.commits = 0
        self.rollbacks = 0
        self.raise_on_execute = raise_on_execute

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        pass


class _FakeEmbedder:
    dim = 3

    def embed(self, text: str) -> list[float]:
        return [0.1, 0.2, 0.3]

    def embed_batch(self, texts):
        return [self.embed(t) for t in texts]


def _provider(options: Optional[dict] = None, *, conn: Optional[_FakeConn] = None):
    server = ServerCandidate(server_key="pgvector",
                             url="postgresql://stub@stub/stub", source="test")
    p = PgvectorProvider(server, options={"table": "test_mem", **(options or {})})
    c = conn if conn is not None else _FakeConn()
    p._conn = c            # type: ignore[attr-defined]
    p._embedder = _FakeEmbedder()   # type: ignore[attr-defined]
    p._table_created = True         # type: ignore[attr-defined]
    return p, c


def _ef_stmts(conn: _FakeConn) -> list[str]:
    return [s for s, _ in conn.statements if "ef_search" in s]


class TestEfSearchApplied(unittest.TestCase):
    def test_default_is_the_measured_value(self):
        self.assertEqual(DEFAULT_EF_SEARCH, 100)

    def test_emits_set_with_default(self):
        p, conn = _provider()
        p._apply_ef_search()
        self.assertEqual(_ef_stmts(conn), [f"SET hnsw.ef_search = {DEFAULT_EF_SEARCH}"])
        self.assertEqual(conn.commits, 1)

    def test_option_overrides_default(self):
        p, conn = _provider({"ef_search": 250})
        p._apply_ef_search()
        self.assertEqual(_ef_stmts(conn), ["SET hnsw.ef_search = 250"])

    def test_string_option_is_coerced(self):
        p, conn = _provider({"ef_search": "64"})
        p._apply_ef_search()
        self.assertEqual(_ef_stmts(conn), ["SET hnsw.ef_search = 64"])

    def test_zero_leaves_the_server_default_alone(self):
        p, conn = _provider({"ef_search": 0})
        p._apply_ef_search()
        self.assertEqual(_ef_stmts(conn), [])
        self.assertEqual(conn.commits, 0)

    def test_negative_leaves_the_server_default_alone(self):
        p, conn = _provider({"ef_search": -1})
        p._apply_ef_search()
        self.assertEqual(_ef_stmts(conn), [])

    def test_garbage_falls_back_to_default(self):
        p, conn = _provider({"ef_search": "not-a-number"})
        p._apply_ef_search()
        self.assertEqual(_ef_stmts(conn), [f"SET hnsw.ef_search = {DEFAULT_EF_SEARCH}"])

    def test_value_is_interpolated_as_an_int_only(self):
        """The GUC name cannot be parameterised, so the value is
        formatted into the SQL. It must go through ``%d`` after an
        ``int()`` -- never a raw string -- or this is an injection
        point reachable from config."""
        p, conn = _provider({"ef_search": "100; DROP TABLE memories_qwen3"})
        p._apply_ef_search()
        self.assertEqual(_ef_stmts(conn), [f"SET hnsw.ef_search = {DEFAULT_EF_SEARCH}"])


class TestEfSearchFailureIsSoft(unittest.TestCase):
    def test_unknown_guc_does_not_raise(self):
        """An older pgvector without the GUC must not break the
        provider -- recall and store still work, just at the server
        default."""
        p, conn = _provider(conn=_FakeConn(raise_on_execute=True))
        p._apply_ef_search()   # must not raise
        self.assertEqual(conn.rollbacks, 1)

    def test_failure_rolls_back_so_the_connection_stays_usable(self):
        """Postgres leaves a connection aborted until rollback; without
        this the next caller sees 'current transaction is aborted'."""
        p, conn = _provider(conn=_FakeConn(raise_on_execute=True))
        p._apply_ef_search()
        self.assertGreaterEqual(conn.rollbacks, 1)


class TestAppliedOnConnect(unittest.TestCase):
    def test_ensure_ready_applies_it_for_a_fresh_connection(self):
        """Regression guard: the SET has to ride on connection setup.
        A per-connection GUC set anywhere else is silently lost every
        time the provider reconnects."""
        from unittest import mock

        import psycopg

        conn = _FakeConn()
        p, _ = _provider()
        p._conn = None            # type: ignore[attr-defined]
        p._table_created = True   # type: ignore[attr-defined]
        with mock.patch.object(psycopg, "connect", return_value=conn):
            p._ensure_ready()
        self.assertEqual(_ef_stmts(conn),
                         [f"SET hnsw.ef_search = {DEFAULT_EF_SEARCH}"])


if __name__ == "__main__":
    unittest.main()
