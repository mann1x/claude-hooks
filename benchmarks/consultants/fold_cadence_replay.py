"""ch-25: why a 32k researcher folds every 2–3 calls, replayed on its folds.

Every fold of a recorded council is rebuilt: the history the lane held when
it folded is the previous researcher request plus the newest turn (the
assistant call matching the previous reply's tool-call ids, and every
message after it in the folded request). ``budget.plan_cut`` is run on it
with the target and trigger the engine computes, after the token
calibration the live run had at that point (each researcher request's
count fed to ``token_calib.observe_request_tokens`` in recorded order, as
``council._observe_prompt_tokens`` does).

The baseline must reproduce the recorded tails before a variant means
anything; the report says how many it does. Each variant is then scored
by the tail it keeps, where the fold lands (head + the recorded digest +
that tail), the headroom left under the trigger, and the folds each lane's
own recorded growth would cost landing there (:func:`simulate`, checked
against the recorded fold count on the baseline).

  python benchmarks/consultants/fold_cadence_replay.py \\
      --db <session>/transcript.db --window 32768 \\
      --out benchmarks/consultants/results/<date>/fold-cadence

``report.md`` and ``metadata.json`` hold counts only.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics as st
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from claude_hooks import token_calib  # noqa: E402
from consultants.engine import budget, compaction  # noqa: E402


def _ids(msg: dict) -> set:
    return {t.get("id") for t in msg.get("tool_calls") or []}


def _reply(resp: dict) -> dict:
    return ((resp.get("choices") or [{}])[0].get("message")
            or resp.get("message") or {})


def _marker(messages: list) -> int:
    return next((i for i, m in enumerate(messages)
                 if m.get("_compaction_marker")), -1)


def folds(db: Path, window: int) -> tuple[list[dict], dict]:
    """The rebuilt folds, and per-lane growth between them."""
    token_calib.reset_calibration()
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = c.execute(
        "select lane_idx, model, prompt_tokens, request_json, response_json "
        "from events where kind='llm_call' and role='researcher' "
        "order by rowid").fetchall()
    c.close()
    last: dict = {}
    out: list[dict] = []
    growth: dict = {}
    first: dict = {}
    lane_calls: dict = {}
    unmatched = 0
    for lane, model, pt, rq, rs in rows:
        req, resp = json.loads(rq), json.loads(rs or "{}")
        msgs, tools = req.get("messages") or [], req.get("tools")
        model = req.get("model") or model or ""
        mi = _marker(msgs)
        gen = msgs[mi].get("_compaction_generation") if mi >= 0 else 0
        prev = last.get(lane)
        first.setdefault(lane, int(pt or 0))
        lane_calls[lane] = lane_calls.get(lane, 0) + 1
        if prev and prev["gen"] == gen:
            growth.setdefault(lane, []).append(pt - prev["pt"])
        if prev and gen != prev["gen"] and mi >= 0:
            tail = msgs[mi + 1:]
            ids = _ids(_reply(prev["resp"]))
            j = next((k for k, m in enumerate(tail)
                      if m.get("role") == "assistant" and ids
                      and _ids(m) == ids), None)
            if j is None:
                unmatched += 1
            else:
                pre = prev["msgs"] + tail[j:]
                oh = compaction.overhead_tokens(pre, tools, model)
                trig = budget.compaction_trigger_tokens(
                    context_length=window,
                    observed_output=budget.observed_output_tokens(pre, model=model),
                    reserve_share=budget.RESERVE_SHARE,
                    reserve_min=budget.RESERVE_MIN_TOKENS)
                est = lambda ms: budget.estimate_messages_tokens(ms, model)  # noqa: E731
                head = budget._head_end(pre)
                out.append({
                    "lane": lane, "gen": gen, "pre": pre, "model": model,
                    "overhead": oh, "trigger": trig,
                    "head_tokens": est(msgs[:mi]),
                    "digest_tokens": est([msgs[mi]]),
                    "recorded_tail": len(tail), "head_end": head,
                    "landing_recorded": pt})
        chars, reasoning = token_calib.measure_request_chars(msgs, tools=tools)
        token_calib.observe_request_tokens(chars, int(pt or 0), reasoning,
                                           model, context_window=window)
        last[lane] = {"gen": gen, "pt": int(pt or 0), "msgs": msgs,
                      "resp": resp}
    return out, {"growth": growth, "unmatched": unmatched,
                 "calls": len(rows), "first": first,
                 "lane_calls": lane_calls}


def _fork_shape(messages: list) -> list:
    """The history as the fork holds it: a turn's tool results in one
    message after its assistant call (cline puts them in one user turn)."""
    out: list = []
    for m in messages:
        if m.get("role") == "tool" and out and out[-1].get("_results"):
            out[-1]["content"] += "\n" + (m.get("content") or "")
            out[-1]["_n"] += 1
            continue
        if m.get("role") == "tool":
            out.append({"role": "tool", "content": m.get("content") or "",
                        "_results": True, "_n": 1})
            continue
        out.append(m)
    return out


def tail_of(f: dict, variant: str, window: int) -> tuple[int, int]:
    """(kept messages, kept tokens) of ``variant``'s cut."""
    pre, model = f["pre"], f["model"]
    target = budget.compaction_target_tokens(
        window, overhead_tokens=f["overhead"], trigger_tokens=f["trigger"])
    keep = target
    if variant in ("total", "total-floor"):
        # The fork's recovery bar on every fold: digest + tail <= target.
        keep = max(1, target - f["digest_tokens"])
        if variant == "total-floor":
            keep = max(keep, budget.preserve_recent_tokens(
                context_length=window, target_tokens=target))
    kw = dict(keep_tokens=keep, context_length=window, model=model,
              overhead_tokens=f["overhead"])
    if variant == "fork-shape":
        shaped = _fork_shape(pre)
        cp = budget.plan_cut(shaped, keep_recent=0, **kw)
        kept = sum(m.get("_n", 1) for m in shaped[cp.cut_end:])
        tail = pre[len(pre) - kept:] if kept else []
    else:
        keep_recent = 0 if variant == "no-keep-recent" else 4
        cp = budget.plan_cut(pre, keep_recent=keep_recent, **kw)
        tail = pre[cp.cut_end:]
    return len(tail), budget.estimate_messages_tokens(tail, model)


VARIANTS = (
    ("baseline", "baseline"),
    ("no keep_recent floor (fork count rule)", "no-keep-recent"),
    ("fork message shape (one results message per turn)", "fork-shape"),
    ("A: digest + tail <= target (fork recovery bar)", "total"),
    ("A2: as A, tail >= recency floor", "total-floor"),
)


def simulate(info: dict, trigger: int, landing: float) -> int:
    """Folds each lane's recorded growth costs at ``landing``: the lane
    adds its measured growth call by call (in order, repeated when the
    lane ran longer than its fold-free stretches), and folds back to
    ``landing`` each time the prompt passes ``trigger``."""
    n = 0
    for lane, calls in info["lane_calls"].items():
        seq = info["growth"].get(lane) or [0]
        prompt = info["first"][lane]
        for k in range(calls - 1):
            prompt += seq[k % len(seq)]
            if prompt > trigger:
                n += 1
                prompt = landing
    return n


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", required=True, type=Path)
    ap.add_argument("--window", type=int, default=32_768)
    ap.add_argument("--out", required=True, type=Path)
    a = ap.parse_args(argv)
    fs, info = folds(a.db, a.window)
    growth = [g for gs in info["growth"].values() for g in gs]
    recorded_folds = len(fs)
    trigger = fs[0]["trigger"]
    lines = ["# Fold cadence at a 32k window (ch-25)", "",
             f"- council: `{a.db.parent.name}`, window {a.window:,}",
             f"- researcher calls {info['calls']}, folds rebuilt "
             f"{recorded_folds} ({info['unmatched']} not rebuilt)",
             f"- growth per call within a generation: mean "
             f"{st.mean(growth):,.0f}, median {st.median(growth):,.0f} tokens",
             f"- trigger {trigger:,}, overhead (system + tools) "
             f"{fs[0]['overhead']:,}, head {st.mean(f['head_tokens'] for f in fs):,.0f}, "
             f"digest {st.mean(f['digest_tokens'] for f in fs):,.0f} (mean)", "",
             "| variant | tail = recorded | tail msgs | tail tokens | lands at "
             "| headroom | folds (sim.) |", "|---|---|---|---|---|---|---|"]
    meta: dict = {"window": a.window, "folds": recorded_folds,
                  "calls": info["calls"], "trigger": trigger, "variants": {}}
    for label, variant in VARIANTS:
        tails = [tail_of(f, variant, a.window) for f in fs]
        same = sum(1 for f, (n, _t) in zip(fs, tails)
                   if n == f["recorded_tail"])
        land = [f["head_tokens"] + f["digest_tokens"] + t
                for f, (_n, t) in zip(fs, tails)]
        mean_land = st.mean(land)
        sim = simulate(info, trigger, mean_land)
        row = {"tail_equals_recorded": same,
               "tail_msgs": st.mean(n for n, _ in tails),
               "tail_tokens": st.mean(t for _, t in tails),
               "tail_tokens_min": min(t for _, t in tails),
               "landing": mean_land, "headroom": trigger - mean_land,
               "folds_sim": sim}
        meta["variants"][label] = row
        lines.append(
            f"| {label} | {same}/{recorded_folds} | {row['tail_msgs']:.1f} "
            f"| {row['tail_tokens']:,.0f} (min {row['tail_tokens_min']:,}) "
            f"| {mean_land:,.0f} | {trigger - mean_land:,.0f} | {sim} |")
    lines += ["", f"Recorded folds: {recorded_folds}. The simulation replays "
              "each lane's own growth; its baseline row is the check."]
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / "metadata.json").write_text(json.dumps(meta, indent=1))
    (a.out / "report.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
