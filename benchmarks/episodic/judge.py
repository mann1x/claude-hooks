#!/usr/bin/env python3
"""Pooled relevance judging for the episodic search eval.

Marker relevance (a conversation containing a string) is lexical, so it
favours BM25 and scores an on-topic semantic hit as a miss. This pools the
top-10 of every method in the given eval files per query, asks a model to
rate each pooled exchange once (cached by query + exchange id), and scores
every method on the same judgments.

    python benchmarks/episodic/judge.py --db <live db.sqlite> \\
        --cache <judgments.json> --model glm-5.3 <eval.json> [<eval.json> ...]

Ratings: 2 = exactly what the query asks about, 1 = related / partly useful,
0 = not relevant. Metrics per method: P@10 (r>=1), nDCG@10, exact@10 (any
r=2 in the top 10), MRR (first r>=1).
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from claude_hooks.get_advice.chat_client import ChatClient  # noqa: E402

HARNESS = re.compile(r"^\s*(<task-notification>|This session is being continued|Stop hook feedback|"
                     r"\[SYSTEM NOTIFICATION|<local-command|Caveat:|<command-(name|message)>)")
SYSTEM = (
    "You judge search results over one developer's past Claude Code conversations. "
    "For the query, rate every numbered result: 2 = it is about exactly what the query asks "
    "(the work, bug, fix or answer the query refers to is discussed here); 1 = related topic, "
    "partly useful; 0 = not relevant. Judge the content, not the wording. Reply with JSON only: "
    '{"ratings": [{"i": 1, "r": 2}, ...]} with one entry per result.')


def clean(text: str, n: int) -> str:
    text = re.sub(r"<system-reminder>.*?</system-reminder>", " ", text or "", flags=re.S)
    return re.sub(r"\s+", " ", text).strip()[:n]


def judge_batch(client, model, query, items):
    lines = [f"Query: {query}", ""]
    for i, (_, proj, u, a) in enumerate(items, 1):
        user = "[harness message]" if HARNESS.search(u[:300]) else clean(u, 500)
        lines.append(f"[{i}] project {proj}\nUser: {user}\nAssistant: {clean(a, 1500)}\n")
    payload = {"model": model, "messages": [{"role": "system", "content": SYSTEM},
                                            {"role": "user", "content": "\n".join(lines)}],
               "format": "json", "options": {"num_predict": 2000}, "think": False}
    out = client.chat(payload)
    text = out["choices"][0]["message"]["content"]
    data = json.loads(text[text.find("{"): text.rfind("}") + 1])
    got = {int(x["i"]): int(x["r"]) for x in data.get("ratings", [])}
    return {items[i - 1][0]: max(0, min(2, got.get(i, 0))) for i in range(1, len(items) + 1)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--cache", required=True)
    ap.add_argument("--model", default="glm-5.3")
    ap.add_argument("--base-url", default="https://ollama.com")
    ap.add_argument("evals", nargs="+")
    a = ap.parse_args()

    db = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    cache = json.loads(Path(a.cache).read_text()) if Path(a.cache).exists() else {}
    runs = {}
    for f in a.evals:
        rep = json.loads(Path(f).read_text())
        for m, v in rep.items():
            if isinstance(v, dict) and isinstance(v.get("rows"), list):
                runs[f"{Path(f).stem}:{m}"] = [r.get("ids") or r.get("top") or [] for r in v["rows"]]
                queries = [r["q"] for r in v["rows"]]
    client = ChatClient(a.base_url)
    for qi, q in enumerate(queries):
        pool = sorted({x for tops in runs.values() for x in tops[qi]})
        todo = [x for x in pool if f"{qi}:{x}" not in cache]
        for b in range(0, len(todo), 10):
            ids = todo[b:b + 10]
            items = []
            for x in ids:
                row = db.execute("select project, user_message, assistant_message from exchanges where id=?", (x,)).fetchone()
                if row:
                    items.append((x, *row))
            for x, r in judge_batch(client, a.model, q, items).items():
                cache[f"{qi}:{x}"] = r
            Path(a.cache).write_text(json.dumps(cache))
        print(f"judged q{qi}: pool {len(pool)}, new {len(todo)}", file=sys.stderr)

    print(f"{'run':40s} {'P@10':>5s} {'nDCG':>5s} {'exact':>5s} {'MRR':>5s}")
    for name, tops in runs.items():
        p10 = ndcg = exact = mrr = 0.0
        for qi, top in enumerate(tops):
            rs = [cache.get(f"{qi}:{x}", 0) for x in top[:10]]
            p10 += sum(1 for r in rs if r >= 1) / 10
            dcg = sum(r / math.log2(i + 2) for i, r in enumerate(rs))
            ideal = sorted((cache[k] for k in cache if k.startswith(f"{qi}:")), reverse=True)[:10]
            idcg = sum(r / math.log2(i + 2) for i, r in enumerate(ideal)) or 1
            ndcg += dcg / idcg
            exact += 1 if any(r == 2 for r in rs) else 0
            first = next((i for i, r in enumerate(rs) if r >= 1), None)
            mrr += 1 / (first + 1) if first is not None else 0
        n = len(tops) or 1
        print(f"{name:40s} {p10/n:5.2f} {ndcg/n:5.2f} {exact/n:5.2f} {mrr/n:5.2f}")


if __name__ == "__main__":
    main()
