#!/usr/bin/env python3
"""M14 follow-up — retroactive TTL backfill for pre-M14 rows.

Pre-M14 rows on pgvector / sqlite_vec tables have ``expires_at =
NULL`` and live forever (see ``feedback_pgvector_rollback_hygiene``
and the M14 design notes — NULL means "the writer never opted this
row into a TTL regime"). That's the right default for upgrades —
silent data loss from a schema migration is the worst kind of
regression — but operators sometimes WANT a retroactive sweep
("age out anything older than 6 months").

This script does that, exactly once, opt-in, with a dry-run mode:

    # Show what would happen, no writes:
    python scripts/backfill_expires_at.py \\
        --dsn "postgresql://user:pass@host/db" \\
        --table memories_qwen3 \\
        --days 180 \\
        --dry-run

    # Apply: stamp expires_at = created_at + 180 days on every
    # row where expires_at IS NULL (legacy / never-opted-in).
    python scripts/backfill_expires_at.py \\
        --dsn "postgresql://user:pass@host/db" \\
        --table memories_qwen3 \\
        --days 180

    # sqlite_vec variant:
    python scripts/backfill_expires_at.py \\
        --sqlite-vec-path ~/.claude/consultants-store.db \\
        --table memory \\
        --days 30

Safety rails baked in:

- Only touches rows where ``expires_at IS NULL`` (legacy / never-
  opted-in). M14 writes (which already have expires_at) are
  untouched.
- ``--dry-run`` mode reports counts + the SQL that would run,
  exits non-zero if nothing would change.
- ``--max-rows N`` cap (default 1_000_000) prevents accidental
  multi-hour migrations on giant tables.
- Per-tx commit so a Ctrl-C mid-run leaves a partial backfill
  that re-running picks up cleanly.
- Refuses to run when ``--days`` is unset or non-positive — TTL
  of 0 / negative would expire everything immediately.

The script is stdlib-only beyond psycopg / sqlite3 (sqlite3 ships
with Python); psycopg is only imported when --dsn is supplied so a
sqlite_vec backfill on a host without psycopg works fine.

Exit codes:
    0 — applied (or dry-run that would have applied)
    1 — usage error / no NULL rows to update
    2 — backend / connection error
    3 — interrupted (SIGINT)
"""
from __future__ import annotations

import argparse
import datetime as _dt
import os
import re
import signal
import sys
from pathlib import Path
from typing import Optional, Tuple


# Same regex as ``claude_hooks/providers/pgvector.py:_safe_table``
# so a backfill against an injected table name is impossible.
_SAFE_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _safe_ident(name: str) -> str:
    if not _SAFE_IDENT_RE.match(name):
        raise SystemExit(
            f"error: unsafe table/column name: {name!r}. "
            f"Allowed: [A-Za-z_][A-Za-z0-9_]*"
        )
    return name


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="backfill_expires_at",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    backend = p.add_mutually_exclusive_group(required=True)
    backend.add_argument(
        "--dsn",
        help="Postgres DSN, e.g. postgresql://user:pass@host:5432/db",
    )
    backend.add_argument(
        "--sqlite-vec-path",
        help="Path to sqlite_vec database file.",
    )
    p.add_argument(
        "--table", required=True,
        help="Table name to backfill. Must match "
             "[A-Za-z_][A-Za-z0-9_]*.",
    )
    p.add_argument(
        "--days", type=float, required=True,
        help="TTL in days from each row's created_at. Must be > 0.",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be updated; no writes.",
    )
    p.add_argument(
        "--max-rows", type=int, default=1_000_000,
        help="Safety cap (default 1_000_000). Refuses to run if "
             "the NULL-row count would exceed this.",
    )
    p.add_argument(
        "--created-at-column", default="created_at",
        help="Column name carrying the row's birth time. Default "
             "'created_at' (matches pgvector + sqlite_vec schemas).",
    )
    args = p.parse_args()
    if args.days <= 0:
        p.error("--days must be positive (negative / zero would "
                "expire everything immediately).")
    return args


def _install_sigint_handler() -> None:
    """Exit cleanly on Ctrl-C — leaves a partial backfill that
    re-running picks up cleanly thanks to the `expires_at IS NULL`
    filter."""
    def _handler(_signum, _frame):
        print("\n[backfill] interrupted; partial commit may be in place. "
              "Re-run with the same --days to finish.", file=sys.stderr)
        sys.exit(3)
    signal.signal(signal.SIGINT, _handler)


# ============================================================== #
# pgvector backend
# ============================================================== #


def _run_pgvector(
    dsn: str, table: str, days: float, *,
    dry_run: bool, max_rows: int, created_at_col: str,
) -> int:
    try:
        import psycopg  # type: ignore
    except ImportError:
        print("error: psycopg not installed. "
              "pip install 'psycopg[binary]>=3.2'", file=sys.stderr)
        return 2

    table = _safe_ident(table)
    created_at_col = _safe_ident(created_at_col)

    print(f"[backfill] backend=pgvector table={table} "
          f"days={days} dry_run={dry_run}")
    try:
        conn = psycopg.connect(dsn)
    except Exception as e:
        print(f"error: pgvector connect failed: {e}", file=sys.stderr)
        return 2

    try:
        # First — has expires_at? If the column doesn't exist, the
        # backfill is a no-op (no M14 migration ran yet). Surface a
        # clear error rather than letting the UPDATE fail
        # mysteriously.
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = %s AND column_name = 'expires_at'",
                (table,),
            )
            if not cur.fetchone():
                print(f"error: table {table!r} has no expires_at "
                      f"column. Restart the consultants daemon (or "
                      f"any process that uses the pgvector provider) "
                      f"so _create_table runs the M14 ALTER TABLE "
                      f"first.", file=sys.stderr)
                return 2

        # Count NULL rows.
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT COUNT(*) FROM {table} "
                f"WHERE expires_at IS NULL"
            )
            row = cur.fetchone()
            null_count = int(row[0]) if row else 0
        print(f"[backfill] rows with expires_at IS NULL: {null_count}")
        if null_count == 0:
            print("[backfill] nothing to do.")
            return 1
        if null_count > max_rows:
            print(f"error: {null_count} > --max-rows={max_rows}. "
                  f"Bump --max-rows after reviewing.", file=sys.stderr)
            return 2

        # The UPDATE: ``expires_at = <created_at_col> + interval``.
        # Postgres ``interval`` is constructed from the days literal
        # so the value lands as TIMESTAMPTZ. We bind ``days`` as a
        # parameter — Postgres accepts it in the ``make_interval``
        # signature for safety.
        sql = (
            f"UPDATE {table} SET expires_at = "
            f"{created_at_col} + make_interval(days => %s::int, "
            f"secs => %s::float) "
            f"WHERE expires_at IS NULL"
        )
        days_int = int(days)
        secs_remainder = float(days - days_int) * 86400.0
        params: Tuple = (days_int, secs_remainder)
        print(f"[backfill] SQL: {sql}")
        print(f"[backfill] params: days={days_int} "
              f"secs_remainder={secs_remainder:.2f}")

        if dry_run:
            print("[backfill] --dry-run: not committing.")
            return 0

        with conn.cursor() as cur:
            cur.execute(sql, params)
            updated = cur.rowcount
        conn.commit()
        print(f"[backfill] committed: {updated} rows updated.")
        return 0
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ============================================================== #
# sqlite_vec backend
# ============================================================== #


def _run_sqlite_vec(
    db_path: str, table: str, days: float, *,
    dry_run: bool, max_rows: int, created_at_col: str,
) -> int:
    import sqlite3

    table = _safe_ident(table)
    created_at_col = _safe_ident(created_at_col)

    db_full = Path(os.path.expanduser(db_path))
    if not db_full.exists():
        print(f"error: sqlite_vec db not found: {db_full}",
              file=sys.stderr)
        return 2

    print(f"[backfill] backend=sqlite_vec db={db_full} table={table} "
          f"days={days} dry_run={dry_run}")
    try:
        conn = sqlite3.connect(str(db_full))
    except Exception as e:
        print(f"error: sqlite_vec connect failed: {e}", file=sys.stderr)
        return 2

    try:
        # Confirm the column exists.
        cols = {
            row[1] for row in conn.execute(
                f"PRAGMA table_info({table})"
            )
        }
        if "expires_at" not in cols:
            print(f"error: table {table!r} has no expires_at "
                  f"column. Open the db with the v2 schema (any "
                  f"consultants store call against this file will "
                  f"trigger the lazy migration) and re-run.",
                  file=sys.stderr)
            return 2

        # Count NULL rows.
        cur = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE expires_at IS NULL"
        )
        null_count = int(cur.fetchone()[0])
        print(f"[backfill] rows with expires_at IS NULL: {null_count}")
        if null_count == 0:
            print("[backfill] nothing to do.")
            return 1
        if null_count > max_rows:
            print(f"error: {null_count} > --max-rows={max_rows}. "
                  f"Bump --max-rows after reviewing.", file=sys.stderr)
            return 2

        # sqlite_vec stores expires_at as TEXT (ISO-8601). Compute
        # the new value as ``datetime(created_at, '+N seconds')``
        # using SQLite's date functions so the arithmetic stays in
        # the DB and we don't have to scan + rewrite client-side.
        seconds = int(round(days * 86400.0))
        sql = (
            f"UPDATE {table} SET expires_at = "
            f"datetime({created_at_col}, '+{seconds} seconds') "
            f"WHERE expires_at IS NULL"
        )
        print(f"[backfill] SQL: {sql}")

        if dry_run:
            print("[backfill] --dry-run: not committing.")
            return 0

        cur = conn.execute(sql)
        updated = cur.rowcount
        conn.commit()
        print(f"[backfill] committed: {updated} rows updated.")
        return 0
    finally:
        try:
            conn.close()
        except Exception:
            pass


def main() -> int:
    _install_sigint_handler()
    args = _parse_args()
    started = _dt.datetime.now(_dt.timezone.utc)
    print(f"[backfill] started at {started.isoformat()}")
    if args.dsn:
        rc = _run_pgvector(
            args.dsn, args.table, args.days,
            dry_run=args.dry_run, max_rows=args.max_rows,
            created_at_col=args.created_at_column,
        )
    else:
        rc = _run_sqlite_vec(
            args.sqlite_vec_path, args.table, args.days,
            dry_run=args.dry_run, max_rows=args.max_rows,
            created_at_col=args.created_at_column,
        )
    elapsed = _dt.datetime.now(_dt.timezone.utc) - started
    print(f"[backfill] finished in {elapsed.total_seconds():.2f}s "
          f"(rc={rc})")
    return rc


if __name__ == "__main__":
    sys.exit(main())
