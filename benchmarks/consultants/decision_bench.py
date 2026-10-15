"""Decision-model bench: Jev (TypeSafe) and local decision models (xOllama).

Runs ``questions/decision/cases.json`` — council-shaped questions with a
known answer (review accept, citation check, struggle, compaction review,
next action, critic severity) — through every target's ``/v1/systemone``
and scores accuracy, calibration (Brier), confident-but-wrong answers and
latency. One request per case.

    python benchmarks/consultants/decision_bench.py \\
        --targets typesafe:jev-latest,typesafe:jev-preview,\\
xollama:tev1:latest,xollama:nimble:latest

``typesafe`` reads its key from ``--key-file`` (default
``~/jev-claude-hooks.key``); the key is never printed or written out.
Outputs under ``results/<date>/decision/``: ``trials.jsonl`` (local),
``metadata.json`` and ``report.md`` (committed).

``--cases`` runs another case file, such as the H6 set mined from real
councils by ``mine_decision_cases.py``; the report then adds accuracy by
label class and a floor sweep (how many answers each floor lets through,
and how many of those are right), which is what tuning a per-server floor
reads.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import statistics
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[2]
CASES = ROOT / "benchmarks/consultants/questions/decision/cases.json"
SERVERS = {"typesafe": "https://api.typesafe.ai",
           "xollama": "http://192.168.178.2:11433"}
#: The fork's floor (jev.ts): a noul counts at |2p-1| >= 0.6, i.e. p >= 0.8.
CONFIDENCE_FLOOR = 0.6
#: Floors the report sweeps.
SWEEP = (0.0, 0.2, 0.4, 0.6, 0.7, 0.8, 0.9)


def question(case: dict) -> dict:
    q = {"type": case["kind"], "instructions": case["instructions"]}
    if "criteria" in case:
        q["criteria"] = case["criteria"]
    return q


def ask(base: str, model: str, case: dict, key: Optional[str],
        timeout: float) -> tuple[Optional[dict], Optional[str], int]:
    body = {"model": model, "state": case["state"],
            "questions": {"q": question(case)}}
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(base + "/v1/systemone",
                                 data=json.dumps(body).encode(),
                                 headers=headers)
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
        ms = int((time.monotonic() - t0) * 1000)
        return (data.get("answers") or {}).get("q"), None, ms
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}: {e.read()[:300].decode(errors='replace')}", \
            int((time.monotonic() - t0) * 1000)
    except Exception as e:  # a failed call is a data point
        return None, f"{type(e).__name__}: {e}", int((time.monotonic() - t0) * 1000)


def ask_production(base: str, model: str, case: dict,
                   key_file: Optional[Path], timeout: float
                   ) -> tuple[Optional[dict], Optional[str], int]:
    """As :func:`ask`, through ``claude_hooks.decision_client.evaluate`` —
    the call every live seam makes, with its state cut-and-retry to a
    local model's loaded window. The key stays in the file."""
    from claude_hooks import decision_client as dc
    ep = dc.Endpoint(base_url=base, model=model, timeout_s=timeout,
                     key_file=str(key_file) if key_file else None)
    t0 = time.monotonic()
    try:
        resp = dc.evaluate(ep, case["state"], {"q": question(case)})
    except Exception as e:  # a failed call is a data point
        return None, f"{type(e).__name__}: {e}", \
            int((time.monotonic() - t0) * 1000)
    return (resp.answers or {}).get("q"), None, \
        int((time.monotonic() - t0) * 1000)


def grade(case: dict, ans: Optional[dict]) -> dict:
    if not ans:
        return {"correct": False, "brier": None, "confidence": None,
                "confident_wrong": False}
    kind, truth = case["kind"], case["truth"]
    if kind == "noul":
        p = float(ans.get("noul"))
        y = 1.0 if truth else 0.0
        conf = abs(2 * p - 1)
        correct = (p >= 0.5) == bool(truth)
        return {"correct": correct, "brier": round((p - y) ** 2, 4),
                "p": round(p, 4), "confidence": round(conf, 4),
                "confident_wrong": (not correct) and conf >= CONFIDENCE_FLOOR}
    probs = {str(k): float(v) for k, v in (ans.get("probabilities") or {}).items()}
    conf = ans.get("confidence")
    conf = None if conf is None else float(conf)
    if kind == "choice":
        correct = ans.get("choice") == truth
        brier = sum((p - (1.0 if k == truth else 0.0)) ** 2
                    for k, p in probs.items())
        return {"correct": correct, "brier": round(brier, 4),
                "picked": ans.get("choice"),
                "p_truth": round(probs.get(truth, 0.0), 4),
                "confidence": None if conf is None else round(conf, 4),
                "confident_wrong": (not correct) and (conf or 0) >= CONFIDENCE_FLOOR}
    score = float(ans.get("score"))
    correct = round(score) == int(truth)
    brier = sum((p - (1.0 if int(k) == int(truth) else 0.0)) ** 2
                for k, p in probs.items())
    return {"correct": correct, "brier": round(brier, 4),
            "score": round(score, 3), "abs_error": round(abs(score - truth), 3),
            "confidence": None if conf is None else round(conf, 4),
            "confident_wrong": (not correct) and (conf or 0) >= CONFIDENCE_FLOOR}


#: What a seam does with a confident noul, as ``consultants/engine/
#: decision.py`` reads it: the direction that acts and the floor it
#: acts at (``high_stakes_floor`` 0.85 for a struggle nudge, ``floor``
#: 0.6 for an unsupported-citation note).
SEAM_ACTIONS = {"struggle": ("nudge (stuck)", True, 0.85),
                "citations": ("note (unsupported)", False, 0.6)}


def _seam_actions(trials: list[dict], targets: list[str]) -> list[str]:
    """Per seam that acts on a noul: at each floor, how often it would act
    where it should (hits) and where it should not (false alarms)."""
    out: list[str] = []
    for seam, (what, direction, live) in SEAM_ACTIONS.items():
        rows = [t for t in trials if t["seam"] == seam and t["kind"] == "noul"]
        if not rows:
            continue
        should = sum(1 for t in rows if bool(t["truth"]) == direction) \
            // max(1, len(targets))
        shouldnt = len(rows) // max(1, len(targets)) - should
        out += ["", f"## {seam}: {what}", "",
                f"Acts when p points {'yes' if direction else 'no'} with "
                f"confidence ≥ the floor; the live floor is {live}. Cells: "
                f"hits / {should} that should act · false alarms / "
                f"{shouldnt} that should not.", "",
                "| floor | " + " | ".join(f"`{t}`" for t in targets) + " |",
                "|---|" + "---|" * len(targets)]
        for floor in sorted(set(SWEEP[2:]) | {live}):
            cells = []
            for tg in targets:
                acts = [t for t in rows if t["target"] == tg
                        and t.get("p") is not None
                        and (t["p"] >= 0.5) == direction
                        and (t.get("confidence") or 0) >= floor]
                hits = sum(1 for t in acts if bool(t["truth"]) == direction)
                cells.append(f"{hits} · {len(acts) - hits}")
            mark = " (live)" if floor == live else ""
            out.append(f"| {floor}{mark} | " + " | ".join(cells) + " |")
    return out


def render(trials: list[dict], meta: dict) -> str:
    targets = list(dict.fromkeys(t["target"] for t in trials))
    seams = list(dict.fromkeys(t["seam"] for t in trials))
    out = [f"# Decision-model bench — {meta['date']}", "",
           f"{meta['cases']} council-shaped cases × {meta['repeats']} "
           f"repeat(s), one request per case. Confidence floor "
           f"{CONFIDENCE_FLOOR} (a noul at |2p−1|, as the fork reads it).",
           "", "## By target", "",
           "| target | accuracy | mean Brier | confident & wrong | "
           "median ms | p90 ms | errors |", "|---|---|---|---|---|---|---|"]
    for tg in targets:
        rows = [t for t in trials if t["target"] == tg]
        ok = [t for t in rows if not t["error"]]
        lat = sorted(t["ms"] for t in ok) or [0]
        briers = [t["brier"] for t in ok if t["brier"] is not None]
        out.append(
            f"| `{tg}` | {sum(t['correct'] for t in rows)}/{len(rows)} | "
            f"{statistics.mean(briers):.3f} | "
            f"{sum(t['confident_wrong'] for t in rows)} | "
            f"{statistics.median(lat):.0f} | "
            f"{lat[min(len(lat) - 1, int(0.9 * len(lat)))]:.0f} | "
            f"{sum(1 for t in rows if t['error'])} |" if briers else
            f"| `{tg}` | 0/{len(rows)} | — | — | — | — | {len(rows)} |")
    out += ["", "## Accuracy by seam", "",
            "| seam | " + " | ".join(f"`{t}`" for t in targets) + " |",
            "|---|" + "---|" * len(targets)]
    for s in seams:
        cells = []
        for tg in targets:
            rows = [t for t in trials if t["target"] == tg and t["seam"] == s]
            cells.append(f"{sum(t['correct'] for t in rows)}/{len(rows)}")
        out.append(f"| {s} | " + " | ".join(cells) + " |")
    if any(t.get("candidate") for t in trials):
        cands = list(dict.fromkeys((t["seam"], t.get("candidate") or str(t["truth"]))
                                   for t in trials))
        out += ["", "## Accuracy by label class", "",
                "| seam | class | " + " | ".join(f"`{t}`" for t in targets) + " |",
                "|---|---|" + "---|" * len(targets)]
        for s, k in cands:
            cells = []
            for tg in targets:
                rows = [t for t in trials if t["target"] == tg and t["seam"] == s
                        and (t.get("candidate") or str(t["truth"])) == k]
                cells.append(f"{sum(t['correct'] for t in rows)}/{len(rows)}")
            out.append(f"| {s} | {k} | " + " | ".join(cells) + " |")
    out += ["", "## Floor sweep (noul answers)", "",
            "A floor lets an answer through when its confidence |2p−1| is at "
            "least the floor; below it the seam reads \"unsure\" and keeps "
            "today's behaviour. Cells: right / let through (of all).", "",
            "| floor | " + " | ".join(f"`{t}`" for t in targets) + " |",
            "|---|" + "---|" * len(targets)]
    for floor in SWEEP:
        cells = []
        for tg in targets:
            rows = [t for t in trials if t["target"] == tg and t["kind"] == "noul"]
            kept = [t for t in rows if (t.get("confidence") or 0) >= floor]
            cells.append(f"{sum(t['correct'] for t in kept)} / {len(kept)} "
                         f"({len(rows)})")
        out.append(f"| {floor} | " + " | ".join(cells) + " |")
    out += _seam_actions(trials, targets)
    out += ["", "## Misses", ""]
    for t in trials:
        if not t["correct"]:
            detail = t["error"] or ", ".join(
                f"{k}={t[k]}" for k in ("p", "picked", "p_truth", "score",
                                        "confidence") if t.get(k) is not None)
            out.append(f"- `{t['target']}` {t['id']} (truth {t['truth']}): "
                       f"{detail}")
    return "\n".join(out) + "\n"


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--targets", default="typesafe:jev-latest,"
                   "typesafe:jev-preview,xollama:tev1:latest,"
                   "xollama:nimble:latest")
    p.add_argument("--key-file", type=Path,
                   default=Path.home() / "jev-claude-hooks.key")
    p.add_argument("--xollama", default=SERVERS["xollama"])
    p.add_argument("--repeats", type=int, default=1)
    p.add_argument("--timeout", type=float, default=120.0)
    p.add_argument("--out", type=Path, default=None)
    p.add_argument("--cases", type=Path, default=CASES)
    p.add_argument("--client", choices=("raw", "production"), default="raw",
                   help="raw: one POST as written (the H4 runs); production: "
                   "decision_client.evaluate, what a live seam sends")
    args = p.parse_args(argv)
    SERVERS["xollama"] = args.xollama.rstrip("/")

    cases = json.loads(args.cases.read_text())["cases"]
    date = dt.date.today().isoformat()
    out = args.out or ROOT / "benchmarks/consultants/results" / date / "decision"
    out.mkdir(parents=True, exist_ok=True)
    key = args.key_file.read_text().strip() if args.key_file.exists() else None

    trials = []
    for target in args.targets.split(","):
        server, model = target.split(":", 1)
        base = SERVERS[server]
        tkey = key if server == "typesafe" else None
        if server == "typesafe" and not tkey:
            print(f"skip {target}: no key at {args.key_file}")
            continue
        for rep in range(args.repeats):
            for case in cases:
                if args.client == "production":
                    ans, err, ms = ask_production(
                        base, model, case,
                        args.key_file if server == "typesafe" else None,
                        args.timeout)
                else:
                    ans, err, ms = ask(base, model, case, tkey, args.timeout)
                row = {"target": target, "repeat": rep, "id": case["id"],
                       "seam": case["seam"], "kind": case["kind"],
                       "truth": case["truth"], "ms": ms, "error": err,
                       "candidate": case.get("candidate"),
                       "project": case.get("project"),
                       "answer": ans, **grade(case, ans)}
                trials.append(row)
                print(f"{target:28s} {case['id']:10s} "
                      f"{'ok ' if row['correct'] else 'MISS'} {ms:6d}ms"
                      f"{' ' + err[:80] if err else ''}", flush=True)
    with (out / "trials.jsonl").open("w") as f:
        for t in trials:
            f.write(json.dumps(t) + "\n")
    meta = {"date": date, "targets": args.targets.split(","),
            "cases": len(cases), "repeats": args.repeats,
            "xollama": SERVERS["xollama"], "floor": CONFIDENCE_FLOOR,
            "client": args.client,
            "cases_file": str(args.cases.resolve().relative_to(ROOT))}
    (out / "metadata.json").write_text(json.dumps(meta, indent=2) + "\n")
    (out / "report.md").write_text(render(trials, meta))
    print(out / "report.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
