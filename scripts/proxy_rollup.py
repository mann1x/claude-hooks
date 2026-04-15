#!/usr/bin/env python3
"""
Ingest proxy JSONL logs into ``stats.db`` and rebuild the daily /
session / model / ratelimit rollups. Idempotent — safe to run from
cron, a systemd timer, or manually.

Usage:

    scripts/proxy_rollup.py                         # default log dir + db
    scripts/proxy_rollup.py --since 2026-04-14
    scripts/proxy_rollup.py --log-dir /custom/path
    scripts/proxy_rollup.py --db /custom/stats.db
    scripts/proxy_rollup.py --dry-run               # report, no writes
    scripts/proxy_rollup.py --json                  # structured output

Reads ``config/claude-hooks.json`` for defaults; CLI flags override.
Never reads network, never mutates the JSONL files — the cursor lives
in the DB itself (``ingestion_state`` table).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Make the repo importable when invoked directly from a checkout.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from claude_hooks.config import expand_user_path, load_config
from claude_hooks.proxy.stats_db import ingest_dir, connect


def _default_log_dir(cfg: dict) -> Path:
    pcfg = (cfg.get("proxy") or {})
    return Path(expand_user_path(pcfg.get("log_dir", "~/.claude/claude-hooks-proxy")))


def _default_db_path(cfg: dict, log_dir: Path) -> Path:
    pcfg = (cfg.get("proxy") or {})
    p = pcfg.get("stats_db_path")
    if p:
        return Path(expand_user_path(p))
    return log_dir / "stats.db"


def main() -> int:
    ap = argparse.ArgumentParser(prog="proxy_rollup.py",
                                 description=__doc__.splitlines()[2])
    ap.add_argument("--log-dir", type=str, default=None)
    ap.add_argument("--db", type=str, default=None)
    ap.add_argument("--since", type=str, default=None,
                    help="YYYY-MM-DD — skip files older than this")
    ap.add_argument("--dry-run", action="store_true",
                    help="Report what would change, write nothing")
    ap.add_argument("--json", action="store_true",
                    help="Emit a JSON report instead of human text")
    args = ap.parse_args()

    cfg = load_config()
    log_dir = Path(args.log_dir) if args.log_dir else _default_log_dir(cfg)
    db_path = Path(args.db) if args.db else _default_db_path(cfg, log_dir)

    if not log_dir.exists():
        msg = f"log dir not found: {log_dir}"
        print(msg, file=sys.stderr)
        return 1

    if args.dry_run:
        # Dry-run: open the DB read-only-ish (we'll rollback) and
        # count what *would* be ingested per file.
        files = sorted(log_dir.glob("*.jsonl"))
        conn = connect(db_path)
        try:
            per_file = []
            for f in files:
                if args.since and f.stem < args.since:
                    continue
                row = conn.execute(
                    "SELECT lines_processed FROM ingestion_state WHERE source_file=?",
                    (f.name,),
                ).fetchone()
                cursor = row[0] if row else 0
                total = sum(1 for _ in open(f, "r", encoding="utf-8"))
                pending = max(0, total - cursor)
                per_file.append({
                    "file": f.name,
                    "total_lines": total,
                    "already_processed": cursor,
                    "new_lines": pending,
                })
        finally:
            conn.close()
        if args.json:
            print(json.dumps({"dry_run": True, "files": per_file}, indent=2))
        else:
            print(f"[dry-run] db: {db_path}")
            print(f"[dry-run] log dir: {log_dir}")
            for entry in per_file:
                print(f"  {entry['file']}: "
                      f"{entry['new_lines']} new / {entry['total_lines']} total "
                      f"(cursor at {entry['already_processed']})")
        return 0

    results = ingest_dir(db_path, log_dir, since=args.since)

    if args.json:
        payload = {
            "db": str(db_path),
            "log_dir": str(log_dir),
            "results": [
                {
                    "file": r.source_file,
                    "lines_read": r.lines_read,
                    "lines_inserted": r.lines_inserted,
                    "lines_skipped": r.lines_skipped,
                    "parse_errors": r.parse_errors,
                } for r in results
            ],
        }
        print(json.dumps(payload, indent=2))
        return 0

    print(f"db:      {db_path}")
    print(f"log dir: {log_dir}")
    total_new = sum(r.lines_inserted for r in results)
    total_read = sum(r.lines_read for r in results)
    total_err = sum(r.parse_errors for r in results)
    for r in results:
        new_marker = f"+{r.lines_inserted}" if r.lines_inserted else " ·"
        print(f"  {r.source_file:20}  {new_marker:>6} new  "
              f"({r.lines_read} total, {r.lines_skipped} skipped, "
              f"{r.parse_errors} parse-errs)")
    print(f"total:  {total_new} new / {total_read} total / {total_err} errors")
    return 0


if __name__ == "__main__":
    sys.exit(main())
