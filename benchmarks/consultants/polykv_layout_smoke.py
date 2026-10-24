#!/usr/bin/env python3
"""The shared prompt layout and its layer chain, live (plan F4).

A critic and then a synthesizer built as the council builds them — the
same question, plan and researcher reports (excerpts of this repo's
files) — sent through :class:`PlacedChat` under ``pooled``. Per call it
prints the pool attached, what the engine read from cache and the wall
time; then the layers the run made (parent → child) and the release
audit. Once with ``layout = shared`` and once ``as-built``, so the two
can be compared on the same engine.

    python benchmarks/consultants/polykv_layout_smoke.py \\
        --xollama http://HOST:22434 --model M [--layouts shared,as-built]

Exit 0 when the synthesizer attached a deeper layer than the critic's
under ``shared`` and nothing leaked.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from claude_hooks.get_advice import chat_client as cc  # noqa: E402
from consultants.engine import council, polykv  # noqa: E402

QUESTION = ("How does the PolyKV manager decide which pool a council call "
            "attaches? Cite file:line.")
PLAN = ("1. Read consultants/engine/polykv.py place().\n"
        "2. Read consultants/engine/prompt_layout.py block_cuts().\n"
        "3. Check how PlacedChat sends the placement.")
SOURCES = ["consultants/engine/polykv.py", "consultants/engine/prompt_layout.py",
           "claude_hooks/polykv/wire.py"]


def reports(chars: int) -> list[str]:
    out = []
    for i, f in enumerate(SOURCES, start=1):
        body = (ROOT / f).read_text(encoding="utf-8", errors="replace")
        out.append(f"Lane {i} read {f}.\n\n" + body[:chars])
    return out


def run(layout: str, args, reps: list[str]) -> dict:
    sid = f"f4smoke-{layout}-{uuid.uuid4().hex[:6]}"
    mgr = polykv.register(polykv.KvRunManager(sid, mode="pooled",
                                              layout=layout))
    client = cc.ChatClient(args.xollama, timeout_s=900, max_retries=2)
    calls = []
    for role, msgs in (
            ("critic", council.build_critic_messages(QUESTION, PLAN, reps)),
            ("synthesizer", council.build_synthesizer_messages(
                QUESTION, PLAN, reps, "DECISION: ready\nCONFIDENCE: 0.8")),
            # A second judge after both: it attaches what the first two
            # left, the chain's deepest fork.
            ("meta_critic", council.build_critic_messages(
                QUESTION, PLAN, reps, strictness="strict"))):
        placed = polykv.PlacedChat(client, mgr, role=role)
        t0 = time.monotonic()
        out = placed.chat({"model": args.model, "messages": msgs,
                           "stream": False, "think": False,
                           "options": {"num_predict": 48}})
        rec = ((out.get("timings") or {}).get("polykv")) or {}
        pt = int((out.get("usage") or {}).get("prompt_tokens") or 0)
        calls.append({"role": role, "prompt_tokens": pt,
                      "cache_n": rec.get("cache_n"),
                      "prefilled": pt - int(rec.get("cache_n") or 0),
                      "pool_id": (placed.last_placement() or {}).get("pool_id"),
                      "s": round(time.monotonic() - t0, 1)})
    layers = [{k: e.get(k) for k in ("pool_id", "parent", "chars", "covered")}
              for e in mgr.events if e["kind"] == "layer"]
    refused = [e.get("why") for e in mgr.events if e["kind"] == "layer_refused"]
    roots = [{k: e.get(k) for k in ("root", "mode", "reason")}
             for e in mgr.events if e["kind"] == "root"]
    report = polykv.settle_run(sid, cwd=args.cwd, status="completed") or {}
    return {"layout": layout, "roots": roots, "calls": calls, "layers": layers,
            "refused": refused,
            "leaked": [report.get("leaked_allocations"),
                       report.get("leaked_pools")],
            "close_errors": report.get("close_errors")}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--xollama", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--layouts", default="shared,as-built")
    ap.add_argument("--report-chars", type=int, default=6000)
    ap.add_argument("--cwd", default=str(ROOT))
    args = ap.parse_args(argv)
    reps = reports(args.report_chars)
    # Load the model first: behind xOllama the engine answers only once
    # it is loaded, and until then the manager places by affinity alone.
    cc.ChatClient(args.xollama, timeout_s=900).chat({
        "model": args.model, "stream": False, "think": False,
        "messages": [{"role": "user", "content": "ready"}],
        "options": {"num_predict": 1}})
    results = [run(lay, args, reps) for lay in args.layouts.split(",")]
    print(json.dumps(results, indent=1, default=str))
    shared = next((r for r in results if r["layout"] == "shared"), None)
    ok = all(not any(r["leaked"]) for r in results)
    if shared:
        c, s = shared["calls"][:2]
        ok = ok and s["pool_id"] is not None and s["pool_id"] != c["pool_id"]
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
