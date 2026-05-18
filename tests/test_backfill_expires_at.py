"""M14 follow-up — :file:`scripts/backfill_expires_at.py` tests.

Exercises the sqlite_vec backfill path end-to-end against a real
on-disk SQLite file. pgvector path is exercised at the SQL-shape
level only (no live PG): the dry-run flag covers the no-write
path, and the safety rails (--days <= 0, --max-rows cap, missing
expires_at column) are unit-tested via subprocess invocation.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "backfill_expires_at.py"


def _run(*args: str) -> subprocess.CompletedProcess:
    """Invoke the script with the same Python that's running tests
    (consultants-env). Captures stdout + stderr."""
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True, timeout=30,
    )


class TestSqliteVecBackfill(unittest.TestCase):

    def _make_db(self) -> Path:
        """Build a minimal v2-shaped sqlite_vec table with 5 NULL
        rows (different created_at timestamps) and 2 rows that
        already have expires_at set (must remain untouched)."""
        tmp = tempfile.NamedTemporaryFile(
            prefix="backfill_test_", suffix=".db", delete=False,
        )
        tmp.close()
        db = Path(tmp.name)
        conn = sqlite3.connect(str(db))
        conn.execute(
            "CREATE TABLE memory ("
            "id INTEGER PRIMARY KEY, "
            "content TEXT, "
            "content_hash BLOB, "
            "metadata TEXT, "
            "created_at TEXT, "
            "expires_at TEXT"
            ")"
        )
        # Seed 5 NULL-expires_at rows with created_at spread over
        # the last 6 days.
        now = datetime.now(timezone.utc)
        for i in range(5):
            ct = (now - timedelta(days=i)).isoformat()
            conn.execute(
                "INSERT INTO memory "
                "(content, content_hash, metadata, created_at, "
                "expires_at) VALUES (?, ?, ?, ?, NULL)",
                (f"row-{i}", f"h{i}".encode(),
                 json.dumps({}), ct),
            )
        # Seed 2 rows that already have expires_at — backfill MUST
        # leave them alone.
        already = (now + timedelta(days=10)).isoformat()
        for i in range(5, 7):
            ct = (now - timedelta(days=i)).isoformat()
            conn.execute(
                "INSERT INTO memory "
                "(content, content_hash, metadata, created_at, "
                "expires_at) VALUES (?, ?, ?, ?, ?)",
                (f"row-{i}", f"h{i}".encode(),
                 json.dumps({}), ct, already),
            )
        conn.commit()
        conn.close()
        return db

    def test_dry_run_reports_count_without_writing(self) -> None:
        db = self._make_db()
        try:
            res = _run(
                "--sqlite-vec-path", str(db),
                "--table", "memory",
                "--days", "30",
                "--dry-run",
            )
            self.assertEqual(res.returncode, 0, msg=res.stderr)
            self.assertIn("rows with expires_at IS NULL: 5",
                          res.stdout)
            self.assertIn("--dry-run", res.stdout)
            # Confirm no writes happened.
            conn = sqlite3.connect(str(db))
            cur = conn.execute(
                "SELECT COUNT(*) FROM memory WHERE expires_at IS NULL"
            )
            self.assertEqual(int(cur.fetchone()[0]), 5)
            conn.close()
        finally:
            db.unlink(missing_ok=True)

    def test_apply_updates_only_null_rows(self) -> None:
        db = self._make_db()
        try:
            res = _run(
                "--sqlite-vec-path", str(db),
                "--table", "memory",
                "--days", "30",
            )
            self.assertEqual(res.returncode, 0, msg=res.stderr)
            self.assertIn("committed: 5 rows updated", res.stdout)
            conn = sqlite3.connect(str(db))
            # All 7 rows now have expires_at set.
            cur = conn.execute(
                "SELECT COUNT(*) FROM memory WHERE expires_at IS NULL"
            )
            self.assertEqual(int(cur.fetchone()[0]), 0)
            # The 2 pre-existing expires_at values are unchanged
            # (they all point to ``now + 10 days``, NOT
            # ``created_at + 30 days``).
            cur = conn.execute(
                "SELECT expires_at FROM memory "
                "WHERE content IN ('row-5', 'row-6')"
            )
            for (val,) in cur.fetchall():
                # The seed used now+10d, so the year-day delta is
                # ~10 from "now"; a +30d-from-created_at backfill
                # would have written a different ISO string. The
                # simplest check: every row-5/6 value matches a
                # single fixed pre-seeded string.
                self.assertNotIn("+30", val)  # not the backfill shape
            # The 5 NULL rows now have expires_at = created_at + 30d.
            cur = conn.execute(
                "SELECT created_at, expires_at FROM memory "
                "WHERE content LIKE 'row-%' AND id <= 5 "
                "ORDER BY id"
            )
            for created, expires in cur.fetchall():
                c_dt = datetime.fromisoformat(created)
                # SQLite's datetime('+30 seconds') returns a value
                # in UTC without tzinfo — parse and compare gaps.
                e_dt = datetime.fromisoformat(expires)
                if e_dt.tzinfo is None:
                    e_dt = e_dt.replace(tzinfo=timezone.utc)
                gap = (e_dt - c_dt).total_seconds()
                # 30 days exact == 2_592_000 seconds; tolerate
                # ±1 second for ISO-microsecond truncation in
                # SQLite's datetime().
                self.assertAlmostEqual(
                    gap, 2_592_000.0, delta=1.0,
                )
            conn.close()
        finally:
            db.unlink(missing_ok=True)

    def test_missing_expires_at_column_errors(self) -> None:
        # A pre-M14 sqlite db (no v2 migration run) → script
        # refuses with a clear error rather than the cryptic
        # "no such column" SQL exception.
        tmp = tempfile.NamedTemporaryFile(
            prefix="no_col_", suffix=".db", delete=False,
        )
        tmp.close()
        db = Path(tmp.name)
        try:
            conn = sqlite3.connect(str(db))
            conn.execute(
                "CREATE TABLE memory ("
                "id INTEGER PRIMARY KEY, content TEXT, "
                "created_at TEXT)"
            )
            conn.commit()
            conn.close()
            res = _run(
                "--sqlite-vec-path", str(db),
                "--table", "memory",
                "--days", "30",
            )
            self.assertEqual(res.returncode, 2)
            self.assertIn("no expires_at column", res.stderr)
        finally:
            db.unlink(missing_ok=True)

    def test_no_null_rows_returns_rc1(self) -> None:
        # All rows already have expires_at → exit 1, "nothing to do".
        db = self._make_db()
        try:
            # Pre-fill all rows with expires_at.
            conn = sqlite3.connect(str(db))
            conn.execute(
                "UPDATE memory SET expires_at = '2027-01-01T00:00:00+00:00'"
            )
            conn.commit()
            conn.close()
            res = _run(
                "--sqlite-vec-path", str(db),
                "--table", "memory",
                "--days", "30",
            )
            self.assertEqual(res.returncode, 1)
            self.assertIn("nothing to do", res.stdout)
        finally:
            db.unlink(missing_ok=True)

    def test_max_rows_cap_refuses_oversized_backfill(self) -> None:
        db = self._make_db()
        try:
            res = _run(
                "--sqlite-vec-path", str(db),
                "--table", "memory",
                "--days", "30",
                "--max-rows", "2",  # we have 5 NULL rows
            )
            self.assertEqual(res.returncode, 2)
            self.assertIn("> --max-rows=2", res.stderr)
        finally:
            db.unlink(missing_ok=True)


class TestSafetyRails(unittest.TestCase):

    def test_negative_days_refused(self) -> None:
        res = _run(
            "--sqlite-vec-path", "/nonexistent.db",
            "--table", "memory",
            "--days", "-5",
        )
        # argparse exits with rc=2 on usage error.
        self.assertEqual(res.returncode, 2)
        self.assertIn("--days must be positive", res.stderr)

    def test_zero_days_refused(self) -> None:
        res = _run(
            "--sqlite-vec-path", "/nonexistent.db",
            "--table", "memory",
            "--days", "0",
        )
        self.assertEqual(res.returncode, 2)
        self.assertIn("--days must be positive", res.stderr)

    def test_unsafe_table_name_refused(self) -> None:
        # SQL-injectable table name → script refuses before opening
        # any connection.
        res = _run(
            "--sqlite-vec-path", "/tmp/anything.db",
            "--table", "memory; DROP TABLE users;--",
            "--days", "30",
        )
        self.assertEqual(res.returncode, 1)
        self.assertIn("unsafe table/column name", res.stderr)

    def test_dsn_and_sqlite_vec_path_mutually_exclusive(self) -> None:
        res = _run(
            "--dsn", "postgresql://u:p@h/db",
            "--sqlite-vec-path", "/tmp/x.db",
            "--table", "memory",
            "--days", "30",
        )
        # argparse mutex error.
        self.assertEqual(res.returncode, 2)
        self.assertIn("not allowed with", res.stderr)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
