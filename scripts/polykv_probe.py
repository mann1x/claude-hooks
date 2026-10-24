#!/usr/bin/env python3
"""Live gate for the PolyKV client (plan F1): one owner, a two-layer pool
tree, two workers, release, audit — through xOllama or opencoti direct.

The probe of ``opencoti/docs/features/polykv_integration_guide.md`` §5,
run with our client and our chat wire, so what it shows is what the
council will send. A nonce in the knowledge layer keeps a slot from an
earlier run from deciding the attach.

    python scripts/polykv_probe.py --xollama http://HOST:22434 --model M
    python scripts/polykv_probe.py --direct http://HOST:8240 --model M

Exit 0 when every worker shared the tree, the release found everything
and the audit is clean; 1 otherwise. Prints one JSON summary.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from claude_hooks.get_advice import chat_client as cc  # noqa: E402
from claude_hooks.polykv.client import (  # noqa: E402
    OpencotiError, PolykvClient, pool_ref, wire_id,
)

SENTINEL = "⁣SENTINEL⁣"


def cut(pk: PolykvClient, messages: list) -> str:
    full = pk.apply_template(messages + [{"role": "user", "content": SENTINEL}])
    at = full.find(SENTINEL)
    if at < 0:
        raise SystemExit("the template dropped the sentinel")
    return full[:at]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--xollama")
    g.add_argument("--direct")
    ap.add_argument("--model", required=True)
    ap.add_argument("--owner-ctx", type=int, default=32768)
    ap.add_argument("--workers", type=int, default=2)
    args = ap.parse_args(argv)

    if args.xollama:
        pk = PolykvClient.via_xollama(args.xollama, args.model)
        chat = cc.ChatClient(args.xollama, timeout_s=600, max_retries=2)
    else:
        pk = PolykvClient.direct(args.direct)
        chat = cc.OpenAICompatChatClient(args.direct, timeout_s=600,
                                         max_retries=2)
    nonce = uuid.uuid4().hex[:8]
    run = wire_id(f"probe-{nonce}")
    owner = f"{run}~own-1"
    system = [{"role": "system", "content":
               "You are a terse assistant. Answer in one short sentence."}]
    knowledge = {"role": "user", "content":
                 f"<knowledge nonce={nonce}>\n" + "\n".join(
                     f"Fact {i}: module m{i}.py writes field f{i}a and f{i}b."
                     for i in range(120)) + "\n</knowledge>"}
    out: dict = {"run": run, "model": args.model,
                 "transport": pk.transport.kind}
    pools: list[str] = []
    sessions: list[str] = []
    ok = True
    try:
        # Load the model and open the owner: a 1-token request that books.
        t0 = time.monotonic()
        first = chat.chat({"model": args.model, "messages": system + [
            {"role": "user", "content": "ready"}], "think": False,
            "options": {"num_predict": 1},
            "placement": {"session_id": owner, "num_ctx": args.owner_ctx,
                          "num_ctx_min": 8192}})
        sessions.append(owner)
        out["owner"] = first["timings"].get("polykv")
        out["load_s"] = round(time.monotonic() - t0, 1)
        out["boot_id"] = pk.boot_id()
        props = pk.props()
        out["features_n"] = len(props.get("features") or [])
        kv = pk.kv()
        out["session_ctx_max"] = kv.get("session_ctx_max")

        p0_text = cut(pk, system)
        p1_text = cut(pk, system + [knowledge])
        full = pk.apply_template(system + [knowledge, {"role": "user",
                                                       "content": "task"}])
        out["prefix_ok"] = full.startswith(p1_text) and p1_text.startswith(p0_text)
        p0 = pool_ref(pk.create_pool(prompt=p0_text, session_id=owner, pin=True))
        pools.append(p0)
        p1 = pool_ref(pk.fork_pool(p0, prompt=p1_text, session_id=owner,
                                   pin=True))
        pools.append(p1)
        out["pools"] = [p0, p1]

        workers = []
        for i in range(args.workers):
            sid = f"{run}~r{i}"
            sessions.append(sid)
            got = chat.chat({"model": args.model, "think": False,
                             "messages": system + [knowledge, {
                                 "role": "user",
                                 "content": f"Which fields does m{i * 7}.py write?"}],
                             "options": {"num_predict": 48},
                             "placement": {"session_id": sid, "pool_id": p1}})
            rec = got["timings"].get("polykv") or {}
            msg = ((got.get("choices") or [{}])[0].get("message") or {})
            workers.append({"session": sid, **rec,
                            "answer": (msg.get("content") or "")[:120]})
            # opencoti direct reports n_pool_shared; xOllama's native chat
            # only the cached prompt count, which a fresh session gets
            # from nothing but the pool.
            if not (rec.get("n_pool_shared") or rec.get("cache_n")) \
                    or rec.get("warnings"):
                ok = False
        out["workers"] = workers
        out["kv_mid"] = [{k: a.get(k) for k in ("session_id", "window", "used",
                                                 "pools")}
                         for a in pk.kv().get("allocations") or []
                         if str(a.get("session_id", "")).startswith(run)]
    except (OpencotiError, cc.ChatFault, OSError) as e:
        out["error"] = f"{type(e).__name__}: {e}"
        ok = False
    finally:
        # Workers book nothing, so their close finds nothing: only the
        # owner's ``found`` is a check. Closing them still drops slot KV.
        closes = []
        for sid in reversed(sessions):
            try:
                r = pk.close_session(sid)
                closes.append({"session": sid, "found": r.found})
            except (OpencotiError, OSError) as e:
                closes.append({"session": sid, "error": str(e)})
        out["closes"] = closes
        try:
            left_alloc = [a.get("session_id") for a in
                          pk.kv(live=True).get("allocations") or []
                          if str(a.get("session_id", "")).startswith(run)]
            left_pools = [p.get("pool_id") for p in
                          pk.pools().get("pools") or []
                          if str(p.get("owner") or "").startswith(run)]
        except (OpencotiError, OSError) as e:
            left_alloc, left_pools = [f"audit failed: {e}"], []
        out["leaked"] = {"allocations": left_alloc, "pools": left_pools}
        if left_alloc or left_pools:
            ok = False
        if not any(c.get("found") for c in closes if c["session"] == owner):
            ok = False
    out["ok"] = ok
    print(json.dumps(out, indent=1))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
