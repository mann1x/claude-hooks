#!/usr/bin/env python3
"""PolyKV placement on the council's real prompt path (plan F3 live check).

N researcher lanes built as the fan-out builds them — this repo's
grounding, the researcher system prompt, one plan item each — sent
concurrently through ``_single_shot`` (budget plan, compaction, the
output cap) with each role client wrapped in :class:`PlacedChat`. Shows
what a council would get: the layer the grounding makes, what each lane
shared, and the release audit.

    python benchmarks/consultants/polykv_smoke.py \\
        --xollama http://HOST:22434 --model M [--lanes 3] [--mode pooled]

Exit 0 when every lane attached the layer and shared tokens, and the
release left nothing behind.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from claude_hooks.caliber_proxy.prompt import build_grounding_messages  # noqa: E402
from claude_hooks.get_advice import chat_client as cc  # noqa: E402
from consultants.engine import council, polykv  # noqa: E402

QUESTION = ("Where does the consultants engine decide which chat client a "
            "role uses? Answer in two sentences, citing file:line.")
ITEMS = ["Read consultants/server/runner.py and find _role_client.",
         "Read claude_hooks/get_advice/chat_client.py make_agent_chat_client.",
         "Read consultants/engine/graph.py GraphDeps.chat_clients.",
         "Read consultants/engine/trace.py TracedChat.",
         "Read consultants/config.py RoleConfig.model."]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--xollama", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--lanes", type=int, default=3)
    ap.add_argument("--mode", default="pooled", choices=polykv.MODES)
    ap.add_argument("--cwd", default=str(ROOT))
    ap.add_argument("--follow-up", action="store_true",
                    help="settle as held, adopt it from a second run, run "
                         "the lanes again, then release as on accept")
    ap.add_argument("--sequential", action="store_true",
                    help="one lane at a time (an engine with room for one "
                         "lane's window next to the layer)")
    args = ap.parse_args(argv)

    grounding = build_grounding_messages(args.cwd, tools_available=False)
    mgr = polykv.register(polykv.KvRunManager(
        f"smoke-{uuid.uuid4().hex[:8]}", mode=args.mode))
    client = cc.ChatClient(args.xollama, timeout_s=900, max_retries=2)
    placed = polykv.PlacedChat(client, mgr, role="researcher")
    results: dict[int, dict] = {}

    def lane(i: int) -> None:
        msgs = council.build_researcher_messages(
            QUESTION, f"Sub-research lane {i}: {ITEMS[i % len(ITEMS)]}", [],
            grounding)
        t0 = time.monotonic()
        try:
            text, pt, ct = council._single_shot(
                placed, args.model, msgs, think=False, role="researcher",
                lane_idx=i)
            results[i] = {"ok": True, "prompt_tokens": pt,
                          "completion_tokens": ct,
                          "polykv": (client.last_timings or {}).get("polykv"),
                          "s": round(time.monotonic() - t0, 1),
                          "answer": text[:160]}
        except Exception as e:  # noqa: BLE001 — report, never hide
            results[i] = {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # Lane 0 first, alone: it makes the layer. The rest attach concurrently
    # unless --sequential.
    def run_lanes():
        lane(0)
        if args.sequential:
            for i in range(1, args.lanes):
                lane(i)
        else:
            threads = [threading.Thread(target=lane, args=(i,))
                       for i in range(1, args.lanes)]
            [t.start() for t in threads]
            [t.join() for t in threads]

    run_lanes()
    follow = None
    if args.follow_up:
        first = dict(results)
        polykv.settle_run(mgr.run_id, cwd=args.cwd, status="completed",
                          hold_root=mgr.run_id)
        child = polykv.start_run(mgr.run_id + "-fu", mode=args.mode,
                                 cwd=args.cwd, adopt_root=mgr.run_id)
        placed._placed_manager = child
        creates = sum(1 for e in child.events if e["kind"] == "layer")
        results.clear()
        run_lanes()
        follow = {"adopted": child is mgr, "first": first,
                  "new_layers": sum(1 for e in child.events
                                    if e["kind"] == "layer") - creates}
        polykv.drop(mgr.run_id + "-fu")
        polykv._hold(mgr.run_id, child,
                     polykv.snapshot_path(args.cwd, mgr.run_id))
        report = polykv.release_held(mgr.run_id, reason="accepted")
    else:
        report = polykv.settle_run(mgr.run_id, cwd=args.cwd, status="completed")
    st = (report or {}).get("stats", {}).get("researcher", {})
    layers = list(mgr.events)
    want = args.lanes * (2 if follow else 1)
    ok = (all(r.get("ok") for r in results.values())
          and (follow is None or (follow["adopted"] and follow["new_layers"] == 0))
          and st.get("pooled") == want and st.get("shared_tokens", 0) > 0
          and not (report or {}).get("leaked_allocations")
          and not (report or {}).get("leaked_pools"))
    print(json.dumps({"run": mgr.run_id, "grounding_chars": sum(
        len(m.get("content") or "") for m in grounding),
        "events": layers, "stats": st,
        "lanes": {str(k): v for k, v in sorted(results.items())},
        "closed": (report or {}).get("closed"),
        "settle": {k: (report or {}).get(k) for k in (
            "released_pools", "unpinned_pools", "owners_closed",
            "close_errors")},
        "leaked": [(report or {}).get("leaked_allocations"),
                   (report or {}).get("leaked_pools")],
        "follow_up": follow, "ok": ok}, indent=1, default=str))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
