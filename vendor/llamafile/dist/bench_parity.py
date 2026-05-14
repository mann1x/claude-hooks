#!/usr/bin/env python3
"""Parity bench: Ollama qwen3-embedding:0.6b vs vendor llamafile.

Embeds N varied prompts via both backends, computes per-pair cosine
similarity, dimensionality match, mean / min similarity, and wall-time
per request. Same weights (Ollama blob is the GGUF the llamafile was
built from), so cosine should be effectively 1.0 — anything below
0.998 indicates pooling / tokenizer / scaling drift.

Run with the llamafile listening on :38092 (the canonical
claude-hooks embedding port) and Ollama on the proxy at :11433.
"""
from __future__ import annotations

import json
import math
import time
import urllib.request

OLLAMA_URL = "http://localhost:11433/api/embeddings"
OLLAMA_MODEL = "qwen3-embedding:0.6b"
LLAMAFILE_URL = "http://localhost:38092/embedding"

PROMPTS = [
    # short / common
    "hello world",
    "what is the meaning of life?",
    "the quick brown fox jumps over the lazy dog",
    # code / technical
    "def fibonacci(n):\n    return n if n < 2 else fibonacci(n-1) + fibonacci(n-2)",
    "SELECT id, name FROM users WHERE active = true LIMIT 10;",
    "git rebase -i HEAD~3 --autosquash",
    # paths / refs
    "claude_hooks/dispatcher.py:136",
    "v1.3.2 fixes the __version__ drift bug",
    # multilingual
    "le chat est sur la table",
    "il gatto è sul tavolo",
    "der Hund läuft im Park",
    "猫が机の上にいる",
    # longer / structured
    (
        "Yesterday we cut v1.3.1 to fix the sqlite_vec dispatcher bug "
        "reported in issue #2. The fix is a single line in "
        "claude_hooks/dispatcher.py — the URL extractor now accepts "
        "db_path alongside mcp_url and dsn."
    ),
    (
        "The HyDE pipeline generates a hallucinated answer to the user "
        "prompt, then embeds that answer to use as the query vector for "
        "recall. This typically improves recall@k for short prompts by "
        "20-40% because the model fills in vocabulary the user omitted."
    ),
    # questions
    "how do I fix bcache cache-set superblock corruption?",
    "what context size does qwen3-embedding support natively?",
    # nonsense / edge
    "asdf qwer zxcv",
    "",  # empty (some servers reject; skip if so)
    "🎉 emoji test 🚀",
    # near-duplicates (should embed very close to each other)
    "the cat is on the table",
    "a cat sits on the table",
    "the cat is on the mat",
    # claude-hooks specific
    "store a new memory in pgvector with the kg-create tool",
    "the consultants engine runs planner researcher critic synthesizer",
]


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


def _post(url: str, payload: dict) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def embed_ollama(text: str) -> tuple[list[float], float]:
    t0 = time.perf_counter()
    d = _post(OLLAMA_URL, {"model": OLLAMA_MODEL, "prompt": text})
    return d["embedding"], time.perf_counter() - t0


def embed_llamafile(text: str) -> tuple[list[float], float]:
    t0 = time.perf_counter()
    d = _post(LLAMAFILE_URL, {"content": text})
    if isinstance(d, list):
        d = d[0]
    e = d["embedding"]
    if isinstance(e[0], list):
        e = e[0]
    return e, time.perf_counter() - t0


def main():
    print(f"{'idx':>3}  {'dim_o':>5}  {'dim_l':>5}  {'cos':>7}  "
          f"{'t_ollama_ms':>11}  {'t_llamafile_ms':>14}  prompt")
    print("-" * 100)
    rows = []
    for i, p in enumerate(PROMPTS):
        if not p:
            continue
        try:
            e_o, t_o = embed_ollama(p)
            e_l, t_l = embed_llamafile(p)
        except Exception as ex:
            print(f"{i:>3}  ERR  {ex}")
            continue
        c = cosine(e_o, e_l)
        rows.append((i, len(e_o), len(e_l), c, t_o * 1000, t_l * 1000, p))
        snippet = p.replace("\n", "\\n")
        if len(snippet) > 40:
            snippet = snippet[:37] + "..."
        print(f"{i:>3}  {len(e_o):>5}  {len(e_l):>5}  "
              f"{c:>7.4f}  {t_o*1000:>11.1f}  {t_l*1000:>14.1f}  {snippet}")

    if not rows:
        print("no successful rows")
        return

    cosines = [r[3] for r in rows]
    t_o = [r[4] for r in rows]
    t_l = [r[5] for r in rows]
    print("-" * 100)
    print(f"N={len(rows)}  mean_cos={sum(cosines)/len(cosines):.5f}  "
          f"min_cos={min(cosines):.5f}  max_cos={max(cosines):.5f}")
    print(f"mean t_ollama={sum(t_o)/len(t_o):.1f} ms  "
          f"mean t_llamafile={sum(t_l)/len(t_l):.1f} ms")
    print(f"dim consistency: "
          f"ollama={set(r[1] for r in rows)}  "
          f"llamafile={set(r[2] for r in rows)}")
    # JSON dump for the parity doc.
    with open("parity_results.json", "w", encoding="utf-8") as f:
        json.dump([{
            "idx": r[0], "prompt": r[6][:120],
            "dim_o": r[1], "dim_l": r[2],
            "cos": r[3], "t_o_ms": r[4], "t_l_ms": r[5],
        } for r in rows], f, indent=2)
    print("wrote parity_results.json")


if __name__ == "__main__":
    main()
