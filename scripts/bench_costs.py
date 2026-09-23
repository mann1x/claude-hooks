#!/usr/bin/env python3
"""Price every recorded benchmark run at a dated Ollama price snapshot.

Two sources:

- **Council sweeps** — ``docs/benchmarks/<label>/<query>.metadata.json``.
  Each carries ``turns`` with per-turn role + tokens and ``models`` with
  the role → model map, so a heterogeneous mix is priced per role.
- **Skill-eval suites** — ``benchmarks/consultants/results/<date>/
  <suite>/**/trials.jsonl``. Trials written since 2026-09-23 carry
  ``usage`` by role (coder and every judge). Older trials carry only the
  subject's ``tokens_prompt`` / ``tokens_completion``: their judge spend
  was never recorded, and the report says so instead of pricing it at 0.

Usage::

    scripts/bench_costs.py > docs/benchmarks/costs.md
    scripts/bench_costs.py --suite-dir benchmarks/consultants/results/2026-09-23/coder_med
"""
from __future__ import annotations

import argparse
import ast
import json
import statistics
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from benchmarks.consultants import pricing  # noqa: E402

LOCAL = ZoneInfo("Europe/Berlin")


def _when(raw) -> datetime | None:
    """Timestamps: trials are UTC (``Z``); council ``created`` is naive
    local time, written by the engine's ``datetime.now()``."""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=LOCAL)


def _money(x: float | None, missing: str = "—") -> str:
    return missing if x is None else f"${x:.4f}" if x < 1 else f"${x:.2f}"


# ---------------------------------------------------------------- #
# Council
# ---------------------------------------------------------------- #

def council_runs(root: Path) -> list[dict]:
    """One row per label run directory (``<label>`` or ``<label>-rN``)."""
    rows = []
    for label_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        metas = sorted(label_dir.glob("*.metadata.json"))
        if not metas:
            continue
        run = {"label": label_dir.name, "queries": {}, "by_model": defaultdict(
            lambda: [0, 0]), "unpriced": set(), "total": 0.0}
        for m in metas:
            d = json.loads(m.read_text())
            turns = d.get("turns") or []
            if isinstance(turns, str):
                turns = ast.literal_eval(turns)
            models = d.get("models") or {}
            at = _when(d.get("created"))
            q_cost, q_unpriced = 0.0, False
            for t in turns:
                model = models.get(t.get("role"), "")
                p, c = int(t.get("prompt_tokens") or 0), int(
                    t.get("completion_tokens") or 0)
                run["by_model"][model][0] += p
                run["by_model"][model][1] += c
                cost = pricing.cost_usd(model, p, c, at)
                if cost is None:
                    run["unpriced"].add(model)
                    q_unpriced = True
                else:
                    q_cost += cost
            # None when nothing in the query could be priced: "$0" would
            # read as free.
            run["queries"][m.name.split(".")[0]] = (
                None if q_unpriced and q_cost == 0 else q_cost)
            run["total"] += q_cost
        rows.append(run)
    return rows


def _cell(run: dict, query: str) -> str:
    if query not in run["queries"]:
        return "—"
    return _money(run["queries"][query], "unpriced")


def render_council(rows: list[dict]) -> str:
    out = ["## Council-role sweeps", "",
           "Per run directory: every role's tokens from `turns`, priced "
           "per role's model at the time the query ran (peak/off-peak).",
           "",
           "| Label | smoke | audit-medium | audit-high | Total | Prompt tok | "
           "Completion tok | Unpriced models |",
           "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        p = sum(v[0] for v in r["by_model"].values())
        c = sum(v[1] for v in r["by_model"].values())
        unp = ", ".join(sorted(m for m in r["unpriced"] if m)) or ""
        total = ("unpriced" if unp and r["total"] == 0
                 else _money(r["total"]) + (" (floor)" if unp else ""))
        out.append(
            f"| `{r['label']}` | {_cell(r, 'smoke')} | "
            f"{_cell(r, 'audit-medium')} | "
            f"{_cell(r, 'audit-high')} | {total} | "
            f"{p:,} | {c:,} | {unp} |")
    return "\n".join(out)


# ---------------------------------------------------------------- #
# Skill-eval suites
# ---------------------------------------------------------------- #

def _suite_trials(suite_dir: Path) -> list[dict]:
    """The run's trials, once each: the merged top-level ``trials.jsonl``
    when present, else the per-model subdirectories'."""
    top = suite_dir / "trials.jsonl"
    files = [top] if top.is_file() else sorted(suite_dir.glob("*/trials.jsonl"))
    trials = []
    for f in files:
        for line in f.read_text().splitlines():
            if line.strip():
                trials.append(json.loads(line))
    return trials


def suite_costs(suite_dir: Path) -> dict:
    per_model: dict[str, dict] = {}
    for t in _suite_trials(suite_dir):
        m = t.get("model", "?")
        row = per_model.setdefault(m, {"n": 0, "subject": 0.0, "judges": 0.0,
                                       "judge_recorded": True, "pass": 0,
                                       "unpriced": set(), "costs": []})
        row["n"] += 1
        row["pass"] += bool(t.get("passes_algorithm", t.get("passes_tests")))
        at = _when(t.get("timestamp"))
        usage = t.get("usage")
        if not usage:
            usage = {"subject": {"model": m, "prompt": t.get("tokens_prompt", 0),
                                 "completion": t.get("tokens_completion", 0)}}
            if t.get("quality_score") is not None or t.get("quality_rationale"):
                row["judge_recorded"] = False
        priced = pricing.usage_cost(usage, at)
        subj = sum(v for k, v in priced["by_role"].items()
                   if k in ("subject", "coder", "tool_executor"))
        row["subject"] += subj
        if any(k in ("subject", "coder", "tool_executor") for k in priced["unpriced"]):
            row["subject_unpriced"] = True
        row["judges"] += priced["total"] - subj
        row["costs"].append(priced["total"])
        row["unpriced"].update(usage[r].get("model", "") for r in priced["unpriced"])
    return per_model


def render_suite(suite_dir: Path) -> str:
    rows = suite_costs(suite_dir)
    if not rows:
        return ""
    rel = suite_dir.relative_to(REPO) if suite_dir.is_relative_to(REPO) else suite_dir
    out = [f"### `{rel}`", "",
           "| Model | Trials | Pass | Subject $ | Judge $ | Total $ | $/trial "
           "(median) | $/pass |",
           "|---|---|---|---|---|---|---|---|"]
    for m, r in sorted(rows.items(), key=lambda kv: kv[1]["subject"]):
        judge = _money(r["judges"]) if r["judge_recorded"] else "not recorded"
        total = r["subject"] + r["judges"]
        per_pass = total / r["pass"] if r["pass"] else None
        flag = " (floor)" if (not r["judge_recorded"] or r["unpriced"]) else ""
        if r.get("subject_unpriced"):
            out.append(f"| `{m}` | {r['n']} | {r['pass']} | unpriced | {judge} | "
                       "unpriced | unpriced | unpriced |")
            continue
        out.append(
            f"| `{m}` | {r['n']} | {r['pass']} | "
            f"{'unpriced' if r.get('subject_unpriced') else _money(r['subject'])} | "
            f"{judge} | {_money(total)}{flag} | "
            f"{_money(statistics.median(r['costs']))} | {_money(per_pass)} |")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--suite-dir", action="append", default=[],
                    help="price only these suite run dirs (repeatable)")
    ap.add_argument("--no-council", action="store_true")
    ap.add_argument("--exclude", action="append", default=[],
                    help="skip suite dirs whose path contains this (repeatable)")
    args = ap.parse_args(argv)

    parts = ["# Benchmark costs", "", "> ↟ [Benchmark index](index.md)", "",
             f"Prices: [{pricing.PRICING_SOURCE}]({pricing.PRICING_SOURCE}) "
             f"snapshot of **{pricing.PRICING_DATE}** "
             "(`benchmarks/consultants/pricing.py`). Prompt tokens are priced "
             "uncached, because the traces do not record cache hits, so every "
             "figure is an upper bound. A model with no row on the pricing "
             "page is listed as unpriced and the total is marked *(floor)*.",
             ""]
    if not args.no_council and not args.suite_dir:
        parts += [render_council(council_runs(REPO / "docs" / "benchmarks")), ""]
    suite_dirs = [Path(s).resolve() for s in args.suite_dir] or sorted(
        {p.parent if p.parent.name not in ("trials",) else p.parent.parent
         for p in (REPO / "benchmarks/consultants/results").glob("*/*/trials.jsonl")}
        | {p.parent.parent for p in (REPO / "benchmarks/consultants/results").glob(
            "*/*/*/trials.jsonl")})
    parts += ["## Skill-eval suites", "",
              "Judge spend before 2026-09-23 was not recorded by the harness "
              "(bug-925): those totals are the coder's spend only.", ""]
    suite_dirs = [d for d in suite_dirs
                  if not any(x in str(d) for x in args.exclude)]
    aborted = [d for d in suite_dirs if "aborted" in d.name]
    if aborted:
        parts += [f"Skipped {len(aborted)} `*-aborted-*` run dir(s): partial "
                  "runs that were never published.", ""]
    for d in suite_dirs:
        if d in aborted:
            continue
        block = render_suite(d)
        if block:
            parts += [block, ""]
    print("\n".join(parts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
