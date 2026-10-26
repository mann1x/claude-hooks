#!/usr/bin/env python3
"""Bench write-scope views (plan E3) on a real tree.

Makes ``--lanes`` lane views of ``--scope`` under ``--root`` through
:class:`claude_hooks.sandbox.clone_views.CloneViews` — the path a council
lane takes, the hand-over to the sandbox user included — then edits one
file per lane, freezes and discards every lane. Reports per lane the
view-creation time, how it was made (reflink / copy) and the disk used,
measured as the filesystem's free-space delta.

The views live in ``<root>/.claude-hooks/consultants/<sid>/`` like a
council's, and are removed at the end; the tree itself is only read::

    python scripts/bench_clone_views.py --root /path/to/backup_models \\
        --scope scripts --lanes 4 [--json out.json]

The exit bar of plan E3 is a view in under 2 s with no data-proportional
disk use.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from claude_hooks.sandbox.clone_views import (  # noqa: E402
    CloneViews, _scan, prewarm)
from claude_hooks.sandbox.linux_bwrap import NOBODY, is_root  # noqa: E402


def _free(path: str) -> int:
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", required=True)
    ap.add_argument("--scope", action="append", required=True)
    ap.add_argument("--lanes", type=int, default=3)
    ap.add_argument("--json", help="write the report here")
    ap.add_argument("--prewarm-lead", type=float, default=None,
                    help="start the read-ahead a run starts at submission, "
                         "this many seconds before the first lane (the "
                         "planner's time); the bar then covers every lane")
    a = ap.parse_args(argv)
    root = os.path.realpath(a.root)
    sid = f"csl-bench-{int(time.time())}"
    sdir = os.path.join(root, ".claude-hooks", "consultants", sid)
    uid = NOBODY if is_root() else None
    v = CloneViews(sid=sid, root=root, session_dir=sdir, scopes=a.scope,
                   uid=uid)
    t_sub = time.monotonic()
    if a.prewarm_lead is not None:
        prewarm(root, v.scopes)
    entries = size = 0
    for s in v.scopes:
        n, b = _scan(s, (os.path.join(root, ".claude-hooks", "consultants"),),
                     10**9)
        entries, size = entries + n, size + b
    print(f"root {root}\nscopes {v.scopes}: {entries} entries, "
          f"{size / 2**30:.1f} GiB")
    if a.prewarm_lead is not None:
        time.sleep(max(0.0, a.prewarm_lead - (time.monotonic() - t_sub)))
    rows = []
    try:
        for i in range(a.lanes):
            lane = f"researcher-{i}"
            free0 = _free(root)
            t0 = time.monotonic()
            v.ensure(lane)
            dt = time.monotonic() - t0
            rec = v.lanes()[lane]
            used = max(0, free0 - _free(root))
            # One edit, so the freeze has something to hold.
            clone = next(iter(rec["clones"].values()))
            Path(clone, f"bench-{i}.txt").write_text("edited by the bench\n")
            t1 = time.monotonic()
            fr = v.freeze(lane)
            ft = time.monotonic() - t1
            rows.append({"lane": lane, "create_s": round(dt, 3),
                         "how": sorted(set(rec["how"].values())),
                         "disk_used_mib": round(used / 2**20, 1),
                         "freeze_s": round(ft, 3), "state": fr["state"],
                         "files": len(fr.get("files") or [])})
            print(f"{lane}: view {dt:.3f}s ({'/'.join(rows[-1]['how'])}), "
                  f"disk +{used / 2**20:.1f} MiB, freeze {ft:.3f}s "
                  f"-> {fr['state']} ({rows[-1]['files']} file)")
    finally:
        for lane in list(v.lanes()):
            v.discard(lane)
        shutil.rmtree(sdir, ignore_errors=True)
    report = {"root": root, "scopes": v.scopes, "entries": entries,
              "bytes": size, "uid": uid, "lanes": rows,
              "prewarm_lead_s": a.prewarm_lead,
              "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    if a.json:
        Path(a.json).parent.mkdir(parents=True, exist_ok=True)
        Path(a.json).write_text(json.dumps(report, indent=2) + "\n")
    # Without the read-ahead the first lane pays the filesystem's cold
    # metadata (once per council) and the bar is on the lanes after it;
    # with it, on every lane.
    every = a.prewarm_lead is not None
    warm = [r["create_s"] for r in (rows if every else rows[1:])] or \
        [r["create_s"] for r in rows]
    ok = bool(warm) and max(warm) < 2.0
    print(f"{'PASS' if ok else 'FAIL'}: {'every' if every else 'warm'} view "
          f"under 2 s (worst {max(warm, default=0):.3f}s; first lane "
          f"{rows[0]['create_s'] if rows else 0:.3f}s)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
