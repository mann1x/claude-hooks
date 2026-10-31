"""Which tools each council role is offered and which it calls (plan D12).

Reads every ``transcript.db`` under the given project roots
(``<root>/.claude-hooks/consultants/csl-*/transcript.db``) and prints, per
role: tool-bearing requests, the schema size those requests carried, and
per tool how often it was offered and called. Names a role called that
were never offered (a provider mangling the name, a hallucinated tool)
are listed too.

    python benchmarks/consultants/tool_use_by_role.py ~/dev/* [--json]
"""
from __future__ import annotations

import argparse
import collections
import json
import sqlite3
from pathlib import Path


def measure(roots: list[Path]) -> dict:
    calls = collections.defaultdict(collections.Counter)
    offered = collections.defaultdict(collections.Counter)
    schema = collections.defaultdict(list)
    sessions = 0
    for root in roots:
        for db in sorted(root.glob(".claude-hooks/consultants/csl-*/transcript.db")):
            try:
                con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            except sqlite3.Error:
                continue
            try:
                for role, tool in con.execute(
                        "SELECT role, tool FROM events WHERE kind='tool_call'"):
                    calls[(role or "").split(":")[0]][tool] += 1
                for role, req in con.execute(
                        "SELECT role, request_json FROM events WHERE "
                        "kind='llm_call' AND request_json IS NOT NULL"):
                    try:
                        tools = (json.loads(req) or {}).get("tools") or []
                    except ValueError:
                        continue
                    if not tools:
                        continue
                    base = (role or "").split(":")[0]
                    schema[base].append(len(json.dumps(tools)))
                    for t in tools:
                        offered[base][(t.get("function") or {}).get("name")] += 1
                sessions += 1
            except sqlite3.Error:
                pass
            finally:
                con.close()
    out = {"sessions": sessions, "roles": {}}
    for role in sorted(set(calls) | set(offered)):
        sizes = sorted(schema.get(role) or [0])
        out["roles"][role] = {
            "tool_requests": len(schema.get(role) or []),
            "schema_chars_median": sizes[len(sizes) // 2],
            "tools": {n: {"offered": offered[role][n], "called": calls[role][n]}
                      for n in sorted(set(offered[role]) | set(calls[role]),
                                      key=lambda n: -calls[role][n])},
        }
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("roots", nargs="+", type=Path)
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    m = measure([r.expanduser() for r in a.roots])
    if a.json:
        print(json.dumps(m, indent=2))
        return 0
    print(f"{m['sessions']} sessions")
    for role, r in m["roles"].items():
        print(f"\n{role}: {r['tool_requests']} tool-bearing requests, "
              f"schema median {r['schema_chars_median']} chars")
        for n, c in r["tools"].items():
            flag = "" if c["offered"] else "  (never offered)"
            print(f"  {n[:40]:40s} offered {c['offered']:5d} called {c['called']:5d}{flag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
