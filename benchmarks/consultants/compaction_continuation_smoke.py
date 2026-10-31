#!/usr/bin/env python3
"""Compaction as a continuation of the lane, live (plan F3).

One researcher lane built as the fan-out builds it — this repo's
grounding, the researcher system prompt, one plan item — with a scripted
tool loop of real file reads after it. The lane's previous turn is sent
first through :class:`PlacedChat` (so the engine holds its stream, as it
would mid-loop), then the history is compacted with the full flow, once
per arm:

- ``continuation``: the writer and the half-critics continue the lane;
- ``text``: the transcript pasted as text (the path before F3).

Per compaction call it prints what the engine prefilled (prompt tokens
less ``cache_n``), what the layer shared, and the wall time; per arm the
replay and the release audit.

    python benchmarks/consultants/compaction_continuation_smoke.py \\
        --xollama http://HOST:22434 --model M [--arms continuation,text]
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

from claude_hooks.caliber_proxy.prompt import build_grounding_messages  # noqa: E402
from claude_hooks.get_advice import chat_client as cc  # noqa: E402
from consultants.engine import budget, compaction, council, polykv  # noqa: E402
from consultants.engine import compaction_prompts as P  # noqa: E402

QUESTION = ("Where does the consultants engine decide which chat client a "
            "role uses, and how does PolyKV placement wrap it? Cite file:line.")
ITEM = "Read consultants/server/runner.py and consultants/engine/polykv.py."
FILES = ["consultants/server/runner.py", "consultants/engine/polykv.py",
         "consultants/engine/graph.py", "consultants/engine/trace.py",
         "claude_hooks/get_advice/chat_client.py"]
TOOLS = [{"type": "function", "function": {
    "name": "read_file", "description": "Read a file of the project.",
    "parameters": {"type": "object", "properties": {
        "path": {"type": "string"}}, "required": ["path"]}}}]
STEPS = {P.WRITER_SYSTEM_PROMPT: "writer", P.FULL_SYSTEM_PROMPT: "writer",
         P.CRITIC_SYSTEM_PROMPT: "critic", P.REVIEWER_SYSTEM_PROMPT: "reviewer",
         P.RETROSPECTIVE_SYSTEM_PROMPT: "retrospective",
         P.SYNTHESIZER_SYSTEM_PROMPT: "synthesizer"}


def step_of(payload: dict) -> str:
    msgs = payload.get("messages") or []
    if msgs and msgs[0].get("content") in STEPS:
        return STEPS[msgs[0]["content"]]
    last = str((msgs[-1] if msgs else {}).get("content") or "")
    return next((v + "+cont" for k, v in STEPS.items()
                 if f"[SYSTEM MESSAGE] {k}" in last), "lane")


class Recording(cc.ChatClient):
    """A ChatClient that keeps what the engine said about each call."""

    calls: list

    def chat(self, payload, *a, **kw):
        t0 = time.monotonic()
        try:
            out = super().chat(payload, *a, **kw)
        except Exception as e:  # noqa: BLE001 — recorded, then re-raised
            self.calls.append({"step": step_of(payload), "error": str(e)[:300],
                               "num_ctx": (payload.get("placement") or {}).get("num_ctx"),
                               "s": round(time.monotonic() - t0, 1)})
            raise
        rec = ((out.get("timings") or {}).get("polykv")) or {}
        usage = out.get("usage") or {}
        pt = int(usage.get("prompt_tokens") or 0)
        cache = int(rec.get("cache_n") or 0)
        self.calls.append({
            "step": step_of(payload), "prompt_tokens": pt, "cache_n": cache,
            "prefilled": max(0, pt - cache),
            "pool_shared": rec.get("n_pool_shared"),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "s": round(time.monotonic() - t0, 1),
            "session": (payload.get("placement") or {}).get("session_id")})
        return out


def history(grounding: list, files: int = len(FILES)) -> list:
    msgs = council.build_researcher_messages(QUESTION, ITEM, [], grounding)
    for i, f in enumerate(FILES[:files]):
        body = (ROOT / f).read_text(encoding="utf-8", errors="replace")[:4000]
        msgs.append({"role": "assistant", "content": "",
                     "tool_calls": [{"id": f"c{i}", "type": "function",
                                     "function": {"name": "read_file",
                                                  "arguments": {"path": f}}}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": body})
    return msgs


def arm(name: str, args, grounding: list) -> dict:
    run = f"ccsmoke-{name}-{uuid.uuid4().hex[:6]}"
    mgr = polykv.register(polykv.KvRunManager(run, mode=args.mode))
    client = Recording(args.xollama, timeout_s=1800, max_retries=2)
    client.calls = []
    placed = polykv.PlacedChat(client, mgr, role="researcher")
    msgs = history(grounding, args.files)
    # The lane's previous turn: the engine now holds its stream.
    placed.chat({"model": args.model, "messages": msgs[:-2], "tools": TOOLS,
                 "stream": False, "think": False,
                 "options": {"num_predict": 24}})
    settings = compaction.Settings(
        flow="full", continuation="on" if name == "continuation" else "off")
    plan = budget.plan(placed, args.model, msgs, tools=TOOLS)
    scope = compaction.Scope(chat_client=placed, model=args.model,
                             role="researcher", settings=settings, tools=TOOLS)
    t0 = time.monotonic()
    out = compaction.compact(scope, msgs, plan=plan, tools=TOOLS,
                             reason="forced", session=run)
    wall = round(time.monotonic() - t0, 1)
    marker = next((m for m in out.messages if m.get("_compaction_marker")), {})
    report = polykv.settle_run(run, cwd=args.cwd, status="completed") or {}
    comp = [c for c in client.calls if c["step"] != "lane"]
    return {"arm": name, "wall_s": wall, "changed": out.changed,
            "continuation": out.report.get("continuation"),
            "digest": out.report.get("digest"),
            "prefilled": sum(c.get("prefilled", 0) for c in comp),
            "prompt_tokens": sum(c.get("prompt_tokens", 0) for c in comp),
            "widened": [e for e in mgr.events if e["kind"] == "widened"],
            "calls": client.calls,
            "erased": [e for e in mgr.events if e["kind"] == "erased"],
            "leaked": [report.get("leaked_allocations"),
                       report.get("leaked_pools")],
            "replay": str(marker.get("content") or "")[:args.show]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--xollama", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--mode", default="pooled", choices=polykv.MODES)
    ap.add_argument("--arms", default="continuation,text")
    ap.add_argument("--cwd", default=str(ROOT))
    ap.add_argument("--show", type=int, default=3000)
    ap.add_argument("--files", type=int, default=len(FILES),
                    help="file reads in the scripted loop (fewer: the "
                         "lane's history fits the writer's room check)")
    args = ap.parse_args(argv)
    grounding = build_grounding_messages(args.cwd, tools_available=True)
    results = [arm(a, args, grounding) for a in args.arms.split(",")]
    print(json.dumps(results, indent=1, default=str))
    return 0 if all(r["changed"] and not any(r["leaked"]) for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
