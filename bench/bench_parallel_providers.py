"""Benchmark sequential vs parallel provider fan-out.

Runs N rounds of recall + store against the configured live providers,
once with the new parallel_map path (active in-tree) and once with a
forced-sequential simulation (max_workers=1 wraps the same code so we
isolate just the threading effect, not the code path).

Usage:
    python3 bench/bench_parallel_providers.py [--rounds 20] [--query "..."]

Output: per-call timings + median / p95 summary, sequential vs parallel.
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from claude_hooks.config import load_config  # noqa: E402
from claude_hooks.providers import get_provider_class  # noqa: E402


def _load_providers():
    cfg = load_config()
    providers = []
    for name, pcfg in (cfg.get("providers") or {}).items():
        if not pcfg.get("enabled"):
            continue
        try:
            cls = get_provider_class(name)
        except (KeyError, ValueError):
            continue
        from claude_hooks.providers.base import ServerCandidate
        srv = ServerCandidate(
            server_key=name, url=pcfg.get("mcp_url", ""),
            headers=pcfg.get("headers") or {},
        )
        try:
            providers.append(cls(srv, pcfg))
        except Exception as e:
            print(f"  skip {name}: {e}", file=sys.stderr)
    return providers, cfg


def _sequential_recall(providers, query, k=5):
    """Manual sequential recall — control."""
    out = {}
    for p in providers:
        try:
            t0 = time.perf_counter()
            mems = p.recall(query, k=k)
            dt = time.perf_counter() - t0
            out[p.name] = (mems or [], dt)
        except Exception as e:
            out[p.name] = (None, -1)
            print(f"    seq {p.name}: ERROR {e}", file=sys.stderr)
    return out


def _parallel_recall(providers, query, k=5):
    """Use the new parallel_map helper."""
    from claude_hooks._parallel import parallel_map

    def _call(p):
        t0 = time.perf_counter()
        mems = p.recall(query, k=k)
        return (p.name, mems or [], time.perf_counter() - t0)

    results = parallel_map(_call, providers)
    out = {}
    for r in results:
        if r is None:
            continue
        name, mems, dt = r
        out[name] = (mems, dt)
    return out


def _bench(label, fn, providers, query, rounds):
    print(f"\n--- {label} ({rounds} rounds, {len(providers)} provider(s)) ---")
    durations = []
    for i in range(rounds):
        t0 = time.perf_counter()
        out = fn(providers, query)
        wall = (time.perf_counter() - t0) * 1000  # ms
        durations.append(wall)
        per_provider = " | ".join(
            f"{n}={dt*1000:.0f}ms" for n, (_, dt) in out.items() if dt > 0
        )
        print(f"  round {i+1:2d}: {wall:6.1f}ms  [ {per_provider} ]")
    if durations:
        print(f"  median: {statistics.median(durations):6.1f}ms")
        print(f"  mean:   {statistics.mean(durations):6.1f}ms")
        print(f"  p95:    {sorted(durations)[int(len(durations)*0.95)]:6.1f}ms")
        print(f"  min:    {min(durations):6.1f}ms")
        print(f"  max:    {max(durations):6.1f}ms")
    return durations


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=20)
    ap.add_argument("--query", default="claude-hooks proxy drain forwarder fix")
    ap.add_argument("--warmup", type=int, default=3,
                    help="warm-up rounds to prime HTTP keep-alive (excluded from stats)")
    args = ap.parse_args()

    providers, cfg = _load_providers()
    if not providers:
        print("no enabled providers found in config — aborting", file=sys.stderr)
        return 1

    print(f"loaded providers: {[p.name for p in providers]}")
    print(f"query: {args.query!r}")

    # Warm up — first calls pay TLS handshake. Excluded from stats.
    print(f"\nwarming up ({args.warmup} rounds, discarded)...")
    for _ in range(args.warmup):
        _parallel_recall(providers, args.query)

    seq_d = _bench("SEQUENTIAL", _sequential_recall, providers, args.query, args.rounds)
    par_d = _bench("PARALLEL", _parallel_recall, providers, args.query, args.rounds)

    if seq_d and par_d:
        seq_med = statistics.median(seq_d)
        par_med = statistics.median(par_d)
        speedup = seq_med / par_med if par_med > 0 else float("inf")
        print(f"\n=== summary ===")
        print(f"  sequential median: {seq_med:6.1f}ms")
        print(f"  parallel   median: {par_med:6.1f}ms")
        print(f"  speedup:           {speedup:.2f}x")
        print(f"  saved per call:    {seq_med - par_med:6.1f}ms")
    return 0


if __name__ == "__main__":
    sys.exit(main())
