#!/usr/bin/env python3
"""Measure the lock-critical gaps in the claude-hooks store chain.

Sizes ``ttl_ms`` for the embedder-served coordination lock
(``docs/embedder-lock-design.md``). A lease cannot expire while the
holder has a request in flight, so the TTL only has to cover the
holder's gap *between* requests:

    acquire -> embed(content) -> [ GAP ] -> release
                                   |
                                   +- HNSW SELECT top-k, difflib compare,
                                      json encode, INSERT + commit

The gap contains no embedding, so it is measurable without touching the
embedder at all. Run with ``--nice`` to check whether deprioritising the
store subprocess (``store_async._deprioritise``) inflates it -- on
solidpc it does not, because the dominant cost is inside the Postgres
backend process, which the client's nice value does not reach.

Interleave nice levels across several rounds rather than running all of
one then all of the other: background load drifts, and a single A/B pair
will happily report a difference that is pure drift.

Usage:
    scripts/bench_store_gaps.py --n 200 --nice 0 --label baseline
    scripts/bench_store_gaps.py --n 200 --nice 10 --label niced
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import random
import statistics
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

DEFAULT_CFG = REPO / "config" / "claude-hooks.json"
SCRATCH_TABLE = "ttl_probe_scratch"

WORDS = ("the proxy retry layer honours retry-after and applies full jitter "
         "exponential backoff while a wall clock deadline bounds the attempt "
         "budget so the breaker engages before sibling sessions are harmed "
         "by a pool wide eviction that would close healthy connections").split()


def prose(n_chars: int, seed: int) -> str:
    """Deterministic filler with a realistic ~4.5 chars/token ratio.

    Random characters tokenise at ~1.9 chars/token and a repeated single
    character at ~8.0, either of which skews any downstream throughput
    figure by more than the effect being measured.
    """
    rnd = random.Random(seed)
    out: list[str] = []
    total = 0
    while total < n_chars:
        w = rnd.choice(WORDS)
        out.append(w)
        total += len(w) + 1
    return " ".join(out)[:n_chars]


def pct(xs: list[float], p: float) -> float:
    if not xs:
        return float("nan")
    s = sorted(xs)
    return s[min(len(s) - 1, int(round(p / 100.0 * (len(s) - 1))))]


def load_pgvector_cfg(cfg_path: Path) -> dict:
    cfg = json.loads(cfg_path.read_text())
    return ((cfg.get("providers") or {}).get("pgvector")) or {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path, default=DEFAULT_CFG)
    ap.add_argument("--dsn", default=None, help="override the pgvector DSN")
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--nice", type=int, default=0)
    ap.add_argument("--chars", type=int, default=5000)
    ap.add_argument("--dim", type=int, default=1024)
    ap.add_argument("--label", default="run")
    args = ap.parse_args()

    pg = load_pgvector_cfg(args.config)
    dsn = args.dsn or pg.get("dsn")
    if not dsn:
        print("no pgvector dsn (pass --dsn)", file=sys.stderr)
        return 2
    tables = [pg.get("table") or "claude_hooks_memory"]
    tables += list(pg.get("additional_tables") or [])

    if args.nice:
        try:
            os.setpriority(os.PRIO_PROCESS, 0, args.nice)
        except OSError as e:
            print(f"warn: could not set nice: {e}", file=sys.stderr)

    import psycopg

    conn = psycopg.connect(dsn)
    with conn.cursor() as cur:
        cur.execute(
            f"CREATE TABLE IF NOT EXISTS {SCRATCH_TABLE} ("
            f" id bigserial PRIMARY KEY, content text NOT NULL,"
            f" content_hash text UNIQUE, metadata jsonb,"
            f" embedding vector({args.dim}) NOT NULL,"
            f" expires_at timestamptz)"
        )
        conn.commit()

    search_ms: list[float] = []
    compare_ms: list[float] = []
    write_ms: list[float] = []
    rnd = random.Random(1234)

    try:
        for i in range(args.n):
            content = prose(args.chars, seed=i)
            v = [rnd.gauss(0, 1) for _ in range(args.dim)]
            norm = sum(x * x for x in v) ** 0.5
            vec_literal = str([x / norm for x in v])

            t0 = time.perf_counter()
            rows: list[tuple] = []
            for t in tables:
                with conn.cursor() as cur:
                    meta_expr = ("metadata" if "kg_observations" not in t
                                 else "'{}'::jsonb AS metadata")
                    cur.execute(
                        f"SELECT content, {meta_expr}, embedding <=> %s AS distance "
                        f"FROM {t} ORDER BY distance LIMIT %s",
                        (vec_literal, 3),
                    )
                    rows.extend(cur.fetchall())
            conn.rollback()
            t1 = time.perf_counter()

            rows.sort(key=lambda r: r[2])
            for row_content, _meta, _d in rows[:3]:
                difflib.SequenceMatcher(
                    None, content[:500], row_content[:500]).ratio()
            payload = json.dumps({"source": "bench", "i": i})
            t2 = time.perf_counter()

            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO {SCRATCH_TABLE} "
                    f"(content, content_hash, metadata, embedding, expires_at) "
                    f"VALUES (%s, %s, %s, %s, %s) "
                    f"ON CONFLICT (content_hash) DO NOTHING",
                    (content, f"{args.label}-{i}", payload, vec_literal, None),
                )
                conn.commit()
            t3 = time.perf_counter()

            search_ms.append((t1 - t0) * 1000)
            compare_ms.append((t2 - t1) * 1000)
            write_ms.append((t3 - t2) * 1000)
    finally:
        with conn.cursor() as cur:
            cur.execute(f"DELETE FROM {SCRATCH_TABLE} WHERE content_hash LIKE %s",
                        (f"{args.label}-%",))
            conn.commit()
        conn.close()

    gap_total = [a + b + c for a, b, c in zip(search_ms, compare_ms, write_ms)]

    def line(name: str, xs: list[float]) -> dict:
        return {"metric": name, "n": len(xs),
                "p50": round(pct(xs, 50), 3), "p95": round(pct(xs, 95), 3),
                "p99": round(pct(xs, 99), 3), "max": round(max(xs), 3),
                "mean": round(statistics.fmean(xs), 3)}

    print(json.dumps({
        "label": args.label,
        "nice": os.getpriority(os.PRIO_PROCESS, 0),
        "chars": args.chars, "n": args.n, "tables": tables,
        "loadavg": os.getloadavg(),
        "metrics": [line("search_ms", search_ms), line("compare_ms", compare_ms),
                    line("write_ms", write_ms), line("gap_total_ms", gap_total)],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
