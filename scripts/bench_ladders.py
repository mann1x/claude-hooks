#!/usr/bin/env python3
"""Cost / quality and cost / quality / speed ladders for coder_bench runs.

Every model that was measured, can still be used (priced in the current
snapshot and not announced for retirement — ``pricing.is_current``) and
has a price gets a rung. The first run given is the **reference**; a
model measured in several runs is taken from the first run that has it.

Runs are not on one scale. The same suite and judge scored the same
models ~0.15 Q higher on 2026-09-23 than on 2026-06-04, and the June
run's heavy concurrency made every trial ~9x slower. So each older run
is **calibrated** on the models it shares with the reference: its Q and
its seconds are multiplied by the geometric mean of the
reference/older ratios over those shared models. A calibrated row is
marked †, the factors and the anchors they rest on are printed, and a
run that shares no model with the reference is not calibrated and not
ranked. Cost needs no calibration: it is the run's own tokens at the
one price snapshot.

Two ladders:

- **Value ladder (cost / quality)** ranks by **$ per quality point**:
  the subject's mean $ per trial divided by its quality Q. Lower is
  better. It is what one unit of delivered quality costs.
- **Throughput ladder (cost / quality / speed)** ranks by
  ``Q / sqrt(cost_rel × time_rel)``, where cost_rel and time_rel are the
  model's $ per trial and median seconds per trial divided by the
  field's median. Cost and speed weigh equally; the result is indexed so
  the field median is 100. Higher is better.

**Quality Q** is the mean over trials of ``quality_score / 5`` when the
solution passes its tests and 0 when it does not: working code, weighted
by how good the judge found it. A model below the skill-eval bar
(pass rate ≥ 70 %, mean judge score ≥ 3.5) is ranked but marked, since a
cheap model that fails is not a bargain.

Cost is the **subject's** spend only — the judge does not run in
production — priced at the list (peak) rate of the dated snapshot in
``benchmarks/consultants/pricing.py``, so a run that happened to cross
the off-peak window does not flatter a deepseek model. The off-peak
column shows what the off-peak discount would make of it.

Usage::

    scripts/bench_ladders.py benchmarks/consultants/results/2026-09-23/coder_med \\
        benchmarks/consultants/results/2026-06-04/coder_med
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from benchmarks.consultants import pricing  # noqa: E402

PASS_BAR, QUALITY_BAR = 0.70, 3.5
# A weekday noon, inside the peak window: list price.
_PEAK = datetime(2026, 9, 23, 14, 0, tzinfo=timezone.utc)
_OFF_PEAK = datetime(2026, 9, 26, 14, 0, tzinfo=timezone.utc)  # a Saturday


def _trials(run_dir: Path) -> list[dict]:
    out = []
    for f in sorted(run_dir.glob("*/trials.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


def _subject_cost(t: dict, at: datetime) -> float | None:
    u = (t.get("usage") or {}).get("coder")
    if u:
        return pricing.cost_usd(u["model"], int(u["prompt"]),
                                int(u["completion"]), at)
    return pricing.cost_usd(t["model"], int(t.get("tokens_prompt") or 0),
                            int(t.get("tokens_completion") or 0), at)


def _raw_stats(trials: list[dict]) -> list[dict]:
    by: dict[str, list[dict]] = {}
    for t in trials:
        by.setdefault(t["model"], []).append(t)
    rows = []
    for model, ts in by.items():
        costs = [_subject_cost(t, _PEAK) for t in ts]
        if any(c is None for c in costs):
            continue  # unpriced: no ladder position without a price
        off = [_subject_cost(t, _OFF_PEAK) for t in ts]
        judged = [t["quality_score"] for t in ts
                  if isinstance(t.get("quality_score"), (int, float))]
        q = statistics.fmean(
            (t["quality_score"] / 5 if t.get("passes_tests")
             and isinstance(t.get("quality_score"), (int, float)) else 0.0)
            for t in ts)
        pass_rate = sum(bool(t.get("passes_tests")) for t in ts) / len(ts)
        avg_q = statistics.fmean(judged) if judged else 0.0
        walls = sorted(float(t.get("wall_s") or 0) for t in ts)
        rows.append({
            "model": model, "n": len(ts), "pass_rate": pass_rate,
            "avg_quality": avg_q, "Q": q,
            "usd_trial": statistics.fmean(costs),
            "usd_trial_off": statistics.fmean(off),
            "median_s": statistics.median(walls),
            "p90_s": walls[int(0.9 * (len(walls) - 1))],
            "meets_bar": pass_rate >= PASS_BAR and avg_q >= QUALITY_BAR,
        })
    return rows


def _rank(rows: list[dict]) -> list[dict]:
    """Add the two ladder scores; relative terms use the field median."""
    if not rows:
        return rows
    med_c = statistics.median(r["usd_trial"] for r in rows)
    med_t = statistics.median(r["median_s"] for r in rows)
    for r in rows:
        r["usd_per_q"] = r["usd_trial"] / r["Q"] if r["Q"] else float("inf")
        r["throughput_raw"] = r["Q"] / ((r["usd_trial"] / med_c)
                                        * (r["median_s"] / med_t)) ** 0.5
    med_raw = statistics.median(r["throughput_raw"] for r in rows)
    for r in rows:
        # A field whose median delivers nothing has no scale to index on.
        r["throughput"] = (100 * r["throughput_raw"] / med_raw
                           if med_raw else 0.0)
    return rows


def _name(r: dict) -> str:
    return (f"`{r['model']}`" + (" †" if r.get("calibrated") else "")
            + ("" if r["meets_bar"] else " ⚠ below bar"))


def _rel(p: Path) -> str:
    try:
        return str(p.resolve().relative_to(REPO))
    except ValueError:
        return str(p)


def render(rows: list[dict], cals: list[dict]) -> str:
    ref = next((r["run"] for r in rows if not r["calibrated"]), None)
    L = ["## Ladders — what quality costs", "",
         f"Every coder_med model that was measured, is still offered and "
         f"has a price: {len(rows)} models, 60 trials each, all judged by "
         f"kimi-k2.6. Reference run `{_rel(ref) if ref else '—'}`. Prices: "
         f"snapshot of **{pricing.PRICING_DATE}**, list (peak) rate, "
         "subject spend only (the judge does not run in production). "
         "Retiring models are left out: "
         + ", ".join(f"`{m}` ({d})" for m, d in pricing.RETIRING.items())
         + ". Method: `scripts/bench_ladders.py`.", ""]
    for c in cals:
        if c["q"] is None:
            L.append(f"`{_rel(c['run'])}` shares no model with the reference, "
                     "so its rows are not calibrated and not ranked.")
        else:
            L.append(f"† from `{_rel(c['run'])}`, calibrated on "
                     f"{len(c['anchors'])} models measured in both runs ("
                     + ", ".join(f"`{m}`" for m in c["anchors"])
                     + f"): Q × {c['q']:.3f}, seconds × {c['t']:.3f}. "
                     f"{len(c['anchors'])} anchor(s) make this an estimate: "
                     "a † rung says where to re-run, not what the model "
                     "measures today.")
    L += ["",
         "**Q** = mean of *judge score ÷ 5* over trials whose tests pass "
         "(failing trials count 0) — working code, weighted by how good it "
         f"is. ⚠ = below the skill-eval bar (pass ≥ {PASS_BAR:.0%}, "
         f"mean judge score ≥ {QUALITY_BAR}).", ""]

    L += ["### Value ladder — cost / quality", "",
          "Ranked by **$ per quality point** (mean $ per trial ÷ Q): what "
          "one unit of delivered quality costs. Lower is better.", "",
          "| # | Model | Q | Pass | Judge | $ / trial | **$ / Q point** "
          "| off-peak $ / Q |", "|---|---|---|---|---|---|---|---|"]
    for i, r in enumerate(sorted(rows, key=lambda r: r["usd_per_q"]), 1):
        L.append(f"| {i} | {_name(r)} | {r['Q']:.3f} | {r['pass_rate']:.0%} "
                 f"| {r['avg_quality']:.2f} | ${r['usd_trial']:.4f} "
                 f"| **${r['usd_per_q']:.4f}** "
                 f"| ${r['usd_trial_off'] / r['Q']:.4f} |" if r["Q"] else
                 f"| {i} | {_name(r)} | 0 | {r['pass_rate']:.0%} | — | — | — | — |")

    L += ["", "### Throughput ladder — cost / quality / speed", "",
          "Ranked by **Q ÷ √(cost_rel × time_rel)**, cost and time each "
          "relative to the field median; indexed so the median model is "
          "100. Cost and speed weigh equally. Higher is better.", "",
          "| # | Model | Q | $ / trial | cost_rel | median s | p90 s "
          "| time_rel | **index** |", "|---|---|---|---|---|---|---|---|---|"]
    med_c = statistics.median(r["usd_trial"] for r in rows)
    med_t = statistics.median(r["median_s"] for r in rows)
    for i, r in enumerate(sorted(rows, key=lambda r: -r["throughput"]), 1):
        L.append(f"| {i} | {_name(r)} | {r['Q']:.3f} | ${r['usd_trial']:.4f} "
                 f"| {r['usd_trial'] / med_c:.2f} | {r['median_s']:.1f} "
                 f"| {r['p90_s']:.1f} | {r['median_s'] / med_t:.2f} "
                 f"| **{r['throughput']:.0f}** |")
    return "\n".join(L) + "\n"


def _geo(xs: list[float]) -> float:
    return statistics.geometric_mean(xs)


def combine(run_dirs: list[Path]) -> tuple[list[dict], list[dict]]:
    """Rungs from every run, older runs calibrated onto the first.
    Returns ``(rows, calibrations)``."""
    ref_rows = {r["model"]: r for r in _raw_stats(_current(_trials(run_dirs[0])))}
    for r in ref_rows.values():
        r["run"], r["calibrated"] = run_dirs[0], False
    rows = dict(ref_rows)
    cals = []
    for run in run_dirs[1:]:
        old = {r["model"]: r for r in _raw_stats(_current(_trials(run)))}
        shared = sorted(set(old) & set(ref_rows))
        cal = {"run": run, "anchors": shared, "q": None, "t": None}
        cals.append(cal)
        if not shared:
            continue
        cal["q"] = _geo([ref_rows[m]["Q"] / old[m]["Q"] for m in shared])
        cal["t"] = _geo([ref_rows[m]["median_s"] / old[m]["median_s"]
                         for m in shared])
        for m, r in old.items():
            if m in rows:
                continue
            r["Q"] = min(1.0, r["Q"] * cal["q"])
            r["median_s"] *= cal["t"]
            r["p90_s"] *= cal["t"]
            r["run"], r["calibrated"] = run, True
            rows[m] = r
    return _rank(list(rows.values())), cals


def _current(trials: list[dict]) -> list[dict]:
    return [t for t in trials if pricing.is_current(t["model"])]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("run_dirs", nargs="+",
                    help="reference run first, then older runs to calibrate onto it")
    args = ap.parse_args(argv)
    rows, cals = combine([Path(d) for d in args.run_dirs])
    print(render(rows, cals))
    return 0


if __name__ == "__main__":
    sys.exit(main())
