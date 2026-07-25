#!/usr/bin/env python3
"""Time embeds against a llamafile endpoint, or saturate one as a load source.

Companion to ``bench_store_gaps.py``. Point it at an *isolated* llamafile
instance, not the production embedder -- spawn one on a spare port so the
numbers are neither disturbed by nor conflated with live traffic:

    vendor/llamafile/dist/qwen3-embedding-0.6b-16k.llamafile \\
        --server --host 127.0.0.1 --port 38093 --embedding \\
        --pooling last --ctx-size 16384 --gpu disable

Every measurement uses unique content. llama.cpp's prompt cache serves
repeated payloads from cache, which once produced a 14,511 tok/s reading
against a true ~76 tok/s -- if a throughput figure looks impossibly good,
this is why.

Usage:
    scripts/bench_embed_latency.py time --chars 5000 --reps 5
    scripts/bench_embed_latency.py load --workers 3 --seconds 150
"""
from __future__ import annotations

import argparse
import json
import random
import threading
import time
import urllib.request

DEFAULT_URL = "http://127.0.0.1:38093/embedding"

WORDS = ("the proxy retry layer honours retry-after and applies full jitter "
         "exponential backoff while a wall clock deadline bounds the attempt "
         "budget so the breaker engages before sibling sessions are harmed "
         "by a pool wide eviction that would close healthy connections").split()


def prose(n_chars: int, seed: int) -> str:
    rnd = random.Random(seed)
    out: list[str] = []
    total = 0
    while total < n_chars:
        w = rnd.choice(WORDS)
        out.append(w)
        total += len(w) + 1
    return " ".join(out)[:n_chars]


def embed(url: str, text: str, timeout: float = 300.0) -> float:
    req = urllib.request.Request(
        url, data=json.dumps({"content": text}).encode(),
        headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        r.read()
    return (time.perf_counter() - t0) * 1000


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["time", "load"])
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--chars", type=int, default=5000)
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--seconds", type=float, default=60.0)
    a = ap.parse_args()

    if a.mode == "time":
        samples = sorted(embed(a.url, prose(a.chars, seed=10_000 + i))
                         for i in range(a.reps))
        print(json.dumps({
            "url": a.url, "chars": a.chars, "reps": a.reps,
            "ms": [round(s, 1) for s in samples],
            "median_ms": round(samples[len(samples) // 2], 1),
        }))
        return 0

    stop = threading.Event()
    counts = [0] * a.workers

    def worker(idx: int) -> None:
        n = 0
        while not stop.is_set():
            try:
                embed(a.url, prose(a.chars, seed=idx * 1_000_000 + n))
            except Exception:
                pass
            n += 1
            counts[idx] = n

    for i in range(a.workers):
        threading.Thread(target=worker, args=(i,), daemon=True).start()
    time.sleep(a.seconds)
    stop.set()
    print(json.dumps({"url": a.url, "workers": a.workers, "chars": a.chars,
                      "seconds": a.seconds, "embeds": sum(counts)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
