"""
Benchmark recall latency + result quality across Qdrant and pgvector
(per embedding model).

Measures three things per query × provider:

    embed_ms        time to compute the query embedding (pgvector only;
                    Qdrant embeds server-side and we treat the round-trip
                    as one number)
    recall_ms       DB / MCP round-trip for the similarity query
    total_ms        what the caller actually waits for

…and computes top-5 overlap against the Qdrant baseline so we can compare
result *quality*, not just speed.

Usage:

    python scripts/bench_recall.py                    # all models, k=5, default queries
    python scripts/bench_recall.py --k 10 --warm 3
    python scripts/bench_recall.py --queries-file my_queries.txt
    python scripts/bench_recall.py --output bench-results.json

The script writes a JSON result file (default: bench-results.json) and
prints a markdown summary to stdout.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import statistics
import sys
import time
import urllib.request
from pathlib import Path
from typing import Optional

# Allow running from repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.migrate_to_pgvector import (
    MODELS,
    OllamaBatchEmbedder,
    _load_pgvector_env_dotfile,
    _vec_to_pg_literal,
)

log = logging.getLogger("bench_recall")


DEFAULT_QUERIES = [
    # Project-specific
    "how does claude-hooks recall flow work?",
    "what does the dispatcher do when a provider is enabled?",
    "claude-hooks-daemon control surface — start stop status",
    "stop_guard wrap-up phrase escape behavior",
    # Infrastructure
    "solidPC docker MCP services and their ports",
    "where is the qdrant collection stored and what is its dimension?",
    "memory_kg jsonl format and entity types",
    # Bug fixes / debugging
    "claudemem reindex windows administrator cmd window",
    "caliber refresh windows cmd flash root cause",
    "Caliber telemetry libuv assertion crash on exit",
    # Architecture decisions
    "why we chose stdlib-only http MCP client",
    "feedback memory format with Why and How to apply lines",
    "tier 2.6 batch API for providers — design",
    # Generic semantic
    "how to suppress a subprocess console window on windows",
    "PID-aware lock file format and rationale",
    # Short / edge
    "pgvector",
    "Ollama embeddings",
]


# ---------------------------------------------------------------------------
# Qdrant baseline (direct HTTP — measures the same path mcp-server-qdrant uses)
# ---------------------------------------------------------------------------

class QdrantBaseline:
    """Wraps the production QdrantProvider so we measure the same path the
    UserPromptSubmit hook uses (collection_name compat handling included)."""

    def __init__(self, mcp_url: str = "http://192.168.178.2:32775/mcp"):
        from claude_hooks.providers.base import ServerCandidate
        from claude_hooks.providers.qdrant import QdrantProvider
        srv = ServerCandidate(server_key="qdrant", url=mcp_url, source="user", confidence="manual")
        self._provider = QdrantProvider(srv, options={"collection": "memory", "timeout": 10.0})

    def recall(self, query: str, k: int) -> tuple[list[str], float]:
        t0 = time.perf_counter()
        memories = self._provider.recall(query, k=k)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        return [m.text for m in memories], elapsed_ms


# ---------------------------------------------------------------------------
# Pgvector benchmark — directly via psycopg
# ---------------------------------------------------------------------------

class PgvectorBenchProvider:
    def __init__(self, dsn: str, model_name: str):
        import psycopg
        self.conn = psycopg.connect(dsn)
        self.spec = MODELS[model_name]
        self.embedder = OllamaBatchEmbedder(
            os.environ.get("OLLAMA_URL", "http://192.168.178.2:11434"),
            self.spec.ollama_model,
            keep_alive="15m",
            max_chars=self.spec.max_chars,
            num_ctx=self.spec.num_ctx,
        )
        self.table = f"memories_{self.spec.short}"

    def recall(self, query: str, k: int) -> tuple[list[str], dict[str, float]]:
        """Returns (top-k contents, timing dict in ms)."""
        t0 = time.perf_counter()
        vec = self.embedder.embed([query])[0]
        embed_ms = (time.perf_counter() - t0) * 1000

        t1 = time.perf_counter()
        with self.conn.cursor() as cur:
            cur.execute(
                f"SELECT content, embedding <=> %s::vector AS distance "
                f"FROM {self.table} ORDER BY distance LIMIT %s",
                (_vec_to_pg_literal(vec), k),
            )
            rows = cur.fetchall()
        recall_ms = (time.perf_counter() - t1) * 1000

        contents = [r[0] for r in rows]
        total_ms = embed_ms + recall_ms
        return contents, {"embed_ms": embed_ms, "recall_ms": recall_ms, "total_ms": total_ms}

    def close(self):
        self.conn.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _summarise(values: list[float]) -> dict[str, float]:
    if not values:
        return {"p50": 0.0, "p95": 0.0, "min": 0.0, "max": 0.0, "n": 0}
    s = sorted(values)
    n = len(s)
    p50 = s[n // 2]
    # p95: use ceil((n*0.95)) - 1 index to get 95th percentile robustly for small n
    idx = max(0, min(n - 1, int(round(n * 0.95)) - 1))
    p95 = s[idx]
    return {
        "p50_ms": round(p50, 2),
        "p95_ms": round(p95, 2),
        "min_ms": round(min(s), 2),
        "max_ms": round(max(s), 2),
        "n": n,
    }


def _overlap_at_k(baseline: list[str], candidate: list[str], k: int) -> float:
    """Jaccard overlap of top-k contents (normalized whitespace)."""
    if not baseline or not candidate:
        return 0.0
    norm = lambda xs: {" ".join(x.split()) for x in xs[:k]}
    b, c = norm(baseline), norm(candidate)
    inter = len(b & c)
    return round(inter / max(1, k), 3)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    p.add_argument("--k", type=int, default=5)
    p.add_argument(
        "--warm", type=int, default=3,
        help="warm-up runs per (provider, query) before timing. Excluded from stats."
    )
    p.add_argument(
        "--repeat", type=int, default=5,
        help="timed runs per (provider, query)."
    )
    p.add_argument("--queries-file", type=Path,
                   help="newline-separated queries (UTF-8). Defaults to a curated list.")
    p.add_argument("--output", type=Path, default=Path("bench-results.json"))
    p.add_argument("--qdrant-mcp", default="http://192.168.178.2:32775/mcp")
    p.add_argument("--dsn", help="pgvector DSN; falls back to /shared/config/mcp-pgvector/.env")
    p.add_argument("--models", default="all",
                   help="comma-separated subset of MODELS keys, or 'all'")
    p.add_argument("--skip-qdrant", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def _resolve_dsn(args: argparse.Namespace) -> str:
    if args.dsn:
        return args.dsn
    if dsn := os.environ.get("PGVECTOR_DSN"):
        return dsn
    pg_env = _load_pgvector_env_dotfile()
    user, pw, db = pg_env.get("POSTGRES_USER"), pg_env.get("POSTGRES_PASSWORD"), pg_env.get("POSTGRES_DB")
    if user and pw and db:
        return f"postgresql://{user}:{pw}@127.0.0.1:5432/{db}"
    raise SystemExit("no DSN")


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    queries: list[str] = (
        [ln.strip() for ln in args.queries_file.read_text().splitlines() if ln.strip()]
        if args.queries_file
        else DEFAULT_QUERIES
    )
    log.info("queries=%d k=%d warm=%d repeat=%d", len(queries), args.k, args.warm, args.repeat)

    selected_models = (
        list(MODELS.keys()) if args.models == "all"
        else [m.strip() for m in args.models.split(",") if m.strip()]
    )

    # Build providers.
    providers: dict[str, object] = {}
    if not args.skip_qdrant:
        providers["qdrant"] = QdrantBaseline(args.qdrant_mcp)
    dsn = _resolve_dsn(args)
    for m in selected_models:
        providers[f"pgvector_{m}"] = PgvectorBenchProvider(dsn, m)

    # Run.
    raw_results: dict = {p: {"timings": {}, "results": {}} for p in providers}
    for q in queries:
        for pname, prov in providers.items():
            # warm-up
            for _ in range(args.warm):
                _ = prov.recall(q, args.k)
            # timed
            timings = []
            embed_timings = []
            recall_timings = []
            last_results: list[str] = []
            for _ in range(args.repeat):
                if pname == "qdrant":
                    contents, total_ms = prov.recall(q, args.k)
                    timings.append(total_ms)
                    last_results = contents
                else:
                    contents, t = prov.recall(q, args.k)
                    timings.append(t["total_ms"])
                    embed_timings.append(t["embed_ms"])
                    recall_timings.append(t["recall_ms"])
                    last_results = contents
            raw_results[pname]["timings"].setdefault("total_ms", []).extend(timings)
            if pname != "qdrant":
                raw_results[pname]["timings"].setdefault("embed_ms", []).extend(embed_timings)
                raw_results[pname]["timings"].setdefault("recall_ms", []).extend(recall_timings)
            raw_results[pname]["results"][q] = last_results

    # Compute overlaps vs qdrant baseline.
    overlaps: dict[str, list[float]] = {}
    if "qdrant" in providers:
        for pname in providers:
            if pname == "qdrant":
                continue
            scores = []
            for q in queries:
                base = raw_results["qdrant"]["results"].get(q, [])
                cand = raw_results[pname]["results"].get(q, [])
                scores.append(_overlap_at_k(base, cand, args.k))
            overlaps[pname] = scores

    # Aggregate.
    summary: dict = {"k": args.k, "warm": args.warm, "repeat": args.repeat,
                     "queries": len(queries), "providers": {}}
    for pname in providers:
        agg = {kind: _summarise(vals) for kind, vals in raw_results[pname]["timings"].items()}
        if pname in overlaps:
            agg["recall_at_k_vs_qdrant"] = {
                "mean": round(statistics.mean(overlaps[pname]), 3),
                "median": round(statistics.median(overlaps[pname]), 3),
                "min": round(min(overlaps[pname]), 3),
            }
        summary["providers"][pname] = agg

    # Persist + print.
    args.output.write_text(json.dumps({
        "summary": summary,
        "raw_timings": {p: raw_results[p]["timings"] for p in raw_results},
        "raw_results": {p: raw_results[p]["results"] for p in raw_results},
    }, indent=2))
    log.info("wrote %s", args.output)

    print()
    print(f"# Bench — k={args.k}, queries={len(queries)}, warm={args.warm}, repeat={args.repeat}")
    print()
    print("| Provider | total p50 | total p95 | embed p50 | recall p50 | recall@5 vs Qdrant |")
    print("|---|---|---|---|---|---|")
    for pname, agg in summary["providers"].items():
        tot = agg.get("total_ms", {})
        emb = agg.get("embed_ms", {})
        rec = agg.get("recall_ms", {})
        rk = agg.get("recall_at_k_vs_qdrant")
        rk_s = "—" if not rk else f'{rk["mean"]:.2f}'
        print(
            f"| `{pname}` | {tot.get('p50_ms', '—')} ms | {tot.get('p95_ms', '—')} ms |"
            f" {emb.get('p50_ms', '—')} ms | {rec.get('p50_ms', '—')} ms | {rk_s} |"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
