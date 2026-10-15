"""Plan D acceptance: replay recorded tool-loop lanes through the always-on
guards and fit their thresholds.

Every researcher / tool_executor / coder lane in the given transcript.db
files is rebuilt turn by turn (the response of each ``llm_call`` row, the
``tool_call`` rows up to the next one) and driven through the guard classes
``run_loop`` uses:

- D1 ``reasoning_loop_guard`` (a trip per turn, 3 in a row stop) and the
  ``reasoning_repetition`` nudge (tool-calling turns only);
- D10 / D14 ``loop_detection`` (soft, a last warning, the second hard
  verdict stops; ``mistake_limit`` failed calls in a row stop);
- D2 ``struggle_supervisor`` (nudges, and the thinking-budget stop);
- D3 ``silent_turns`` (thinking-only, a call left in the text, a promise
  of work).

The replay is counterfactual: it reads what the lane did with no guard (or
an older one) in the way, so a stop is labelled by what the lane did after
it — the H6 ``lane-future`` rule: of the calls that followed, how many
brought something new (``mine_decision_cases._novelty``).

- ``stuck``: ≤ 1 in 6 of the next calls new — the stop saved spend;
- ``progressing``: ≥ half new — a false stop, it cut real work;
- ``ended``: fewer than 3 calls followed; the lane was finishing anyway;
- otherwise ``mixed``.

A D3 re-ask is false when it lands on the lane's last turn and that turn is
a report (the rule in ``silent_turns``: a report is a legitimate silent
turn). Each threshold is swept one at a time around its default.

  python benchmarks/consultants/guard_replay.py --roots DIR [DIR ...] \\
      --out benchmarks/consultants/results/<date>/guard-replay

``report.md`` and ``metadata.json`` hold counts only. ``events.jsonl`` and
``samples.md`` quote the transcripts (other repos' code): never commit them.
"""
from __future__ import annotations

import argparse
import contextlib
import dataclasses
import json
import os
import sqlite3
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from benchmarks.consultants.mine_decision_cases import _novelty, transcripts  # noqa: E402
from claude_hooks import (capped_thinking, loop_detection,  # noqa: E402
                          reasoning_loop_guard as rlg,
                          reasoning_repetition as rrep, silent_turns,
                          struggle_supervisor as ss)
from consultants.engine.council import caps_for  # noqa: E402

ROLES = ("researcher", "tool_executor", "coder")
FUTURE = 6
REPORT_MIN_CHARS = 200


# ------------------------------------------------------------------ lanes
@dataclasses.dataclass
class Turn:
    thinking: str
    content: str
    tool_calls: list            # [(name, args_str)]
    tools: list                 # [(tool, args, output, error)] as recorded
    message: dict
    usage: dict
    num_predict: Optional[int]


@dataclasses.dataclass
class Lane:
    sid: str
    role: str
    round: int
    lane: Optional[int]
    effort: str
    turns: list

    @property
    def calls(self) -> list:
        return [c for t in self.turns for c in t.tools]


def _json(text) -> dict:
    try:
        v = json.loads(text or "{}")
        return v if isinstance(v, dict) else {}
    except ValueError:
        return {}


def load_lanes(db: Path) -> list[Lane]:
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    try:
        meta = con.execute("SELECT sid, effort FROM meta LIMIT 1").fetchone() or (db.parent.name, "medium")
        marks = ",".join("?" * len(ROLES))
        rows = con.execute(
            "SELECT ts, kind, role, round, lane_idx, request_json, response_json, "
            "tool, args, output, error FROM events "
            f"WHERE kind IN ('llm_call','tool_call') AND role IN ({marks}) "
            "ORDER BY ts, event_id", ROLES).fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()
    lanes: dict = {}
    for ts, kind, role, rnd, lane, req, resp, tool, args, output, error in rows:
        key = (role, rnd, lane)
        lane_obj = lanes.get(key)
        if lane_obj is None:
            lane_obj = lanes[key] = Lane(meta[0], role, rnd, lane, meta[1] or "medium", [])
        if kind == "llm_call":
            r = _json(resp)
            ch = (r.get("choices") or [{}])[0] or {}
            msg = ch.get("message") or {}
            thinking = msg.get("thinking") or msg.get("reasoning_content") or msg.get("reasoning") or ""
            calls = []
            for tc in msg.get("tool_calls") or []:
                fn = (tc or {}).get("function") or {}
                a = fn.get("arguments")
                calls.append((fn.get("name") or "", a if isinstance(a, str) else json.dumps(a or {})))
            opts = (_json(req).get("options") or {})
            lane_obj.turns.append(Turn(str(thinking), str(msg.get("content") or ""), calls, [],
                                       msg, r.get("usage") or {}, opts.get("num_predict")))
        elif lane_obj.turns:
            lane_obj.turns[-1].tools.append((tool, args, output, error))
    return [ln for ln in lanes.values() if ln.turns and ln.calls]


# ------------------------------------------------------------------ labels
def future_label(novel: list[bool], at: int) -> str:
    """What the lane did after call ``at`` (0-based, the stopped call)."""
    nxt = novel[at + 1: at + 1 + FUTURE]
    if len(nxt) < 3:
        return "ended"
    new = sum(nxt)
    if new * 6 <= len(nxt):
        return "stuck"
    if new * 2 >= len(nxt):
        return "progressing"
    return "mixed"


def call_index_of_turn(lane: Lane, turn: int) -> int:
    """Index of the last call made before or in ``turn``."""
    return sum(len(t.tools) for t in lane.turns[: turn + 1]) - 1


# ------------------------------------------------------------------ guards
def replay_tool_guards(lane: Lane) -> list[dict]:
    """D10 / D14 exactly as ``execute_tool_calls`` + ``ToolGuards``."""
    tracker = loop_detection.LoopTracker()
    mistakes = loop_detection.MistakeLimit(limit=loop_detection.MISTAKE_LIMIT)
    hard_hits = 0
    seen: dict = {}
    out: list[dict] = []
    for i, (tool, args, output, error) in enumerate(lane.calls):
        name = tool or ""
        verdict = tracker.inspect(name, args or "{}")
        if verdict.kind == "hard":
            hard_hits += 1
            out.append({"guard": "loop_guard", "action": "stop" if hard_hits >= 2 else "last_warning",
                        "rule": verdict.rule, "at": i})
            tracker.pending = ""
            if hard_hits >= 2:
                break
            continue
        key = f"{name}:{loop_detection.call_signature(args)}"
        text = str(output or "")
        if error:
            text = f"error: {error}"
        answer = seen.get(key, text) if text.startswith("(duplicate:") else text
        if not text.startswith("(duplicate:"):
            seen[key] = text
        failed = loop_detection.is_failed_output(answer)
        tracker.note_outcome(not failed, answer)
        if verdict.kind == "soft":
            out.append({"guard": "loop_guard", "action": "warn", "rule": verdict.rule, "at": i})
        if mistakes.note(failed):
            out.append({"guard": "mistake_limit", "action": "stop", "at": i})
            break
    return out


def replay_reasoning(lane: Lane, config: rlg.GuardConfig = rlg.DEFAULT,
                     rep: rrep.RepetitionConfig = rrep.DEFAULT) -> list[dict]:
    out: list[dict] = []
    streak = 0
    nudger = rrep.RepetitionNudger(rep)
    for t, turn in enumerate(lane.turns):
        v = rlg.scan(turn.thinking, config) if turn.thinking else None
        if v is not None:
            streak += 1
            out.append({"guard": "reasoning_loop", "action": "cut", "kind": v.kind, "turn": t,
                        "chars": v.chars, "sample": v.sample})
            if streak >= config.max_consecutive_trips:
                out.append({"guard": "reasoning_loop", "action": "stop", "turn": t})
                break
        else:
            streak = 0
        if turn.tool_calls and turn.thinking and nudger.inspect(turn.thinking, t):
            out.append({"guard": "repetition", "action": "nudge", "turn": t})
    return out


def replay_struggle(lane: Lane) -> list[dict]:
    sup = ss.StruggleSupervisor(max_iterations=caps_for(lane.effort).researcher_loop_iters
                                if lane.role == "researcher" else None)
    out: list[dict] = []
    for t, turn in enumerate(lane.turns):
        capped, _ = capped_thinking.detect(
            turn.message, budget_tokens=turn.num_predict,
            completion_tokens=int(turn.usage.get("completion_tokens") or 0))
        fails = [loop_detection.is_failed_output(f"error: {e}" if e else str(o or ""))
                 for _, _, o, e in turn.tools]
        calls = [(f"{tool}:{loop_detection.call_signature(args)}", str(o or ""), bad)
                 for (tool, args, o, _), bad in zip(turn.tools, fails)]
        r = sup.close_iteration(t, budget_spent=bool(capped), failures=fails, calls=calls)
        if r is not None:
            out.append({"guard": "struggle", "action": r[0], "reason": r[1], "turn": t})
            if r[0] == "stop":
                break
    return out


def replay_silent(lane: Lane) -> list[dict]:
    out: list[dict] = []
    last = len(lane.turns) - 1
    for t, turn in enumerate(lane.turns):
        if turn.tool_calls:
            continue
        what = None
        if silent_turns.thinking_only(turn.content, turn.thinking):
            what = "thinking_only"
        elif silent_turns.unparsed_tool_call(turn.content) is not None:
            what = "unparsed_call"
        elif silent_turns.announced_intent(turn.content):
            what = "announced_intent"
        if what:
            # Call markup is never a report, however long.
            report = (what == "announced_intent" and t == last
                      and len(turn.content.strip()) >= REPORT_MIN_CHARS)
            out.append({"guard": "silent_turn", "action": "reask", "what": what, "turn": t,
                        "false": report})
    return out


# ------------------------------------------------------------------ runs
def label_stops(lane: Lane, events: list[dict]) -> None:
    novel = _novelty(lane.calls)
    for e in events:
        if e["action"] not in ("stop", "nudge"):
            continue
        at = e["at"] if "at" in e else call_index_of_turn(lane, e["turn"])
        e["label"] = future_label(novel, at)
        e["lost_new_calls"] = sum(novel[at + 1:])


def run_all(lanes: list[Lane]) -> list[dict]:
    rows: list[dict] = []
    for ln in lanes:
        evs = (replay_tool_guards(ln) + replay_reasoning(ln) + replay_struggle(ln)
               + replay_silent(ln))
        label_stops(ln, evs)
        for e in evs:
            rows.append({"sid": ln.sid, "role": ln.role, "round": ln.round, "lane": ln.lane,
                         "effort": ln.effort, **e})
    return rows


@contextlib.contextmanager
def patched(mod, **values):
    old = {k: getattr(mod, k) for k in values}
    try:
        for k, v in values.items():
            setattr(mod, k, v)
        yield
    finally:
        for k, v in old.items():
            setattr(mod, k, v)


def stop_table(lanes: list[Lane], fn, guard: str) -> Counter:
    c: Counter = Counter()
    for ln in lanes:
        evs = [e for e in fn(ln) if e["guard"] == guard]
        label_stops(ln, evs)
        for e in evs:
            if e["action"] == "stop":
                c[e["label"]] += 1
                c["lost_new_calls"] += e["lost_new_calls"]
            else:
                c[e["action"]] += 1
    return c


def sweeps(lanes: list[Lane]) -> dict:
    out: dict = {}
    tool_lanes = lanes
    for name, values in (("HARD_THRESHOLD", (3, 4, 5, 6, 8)),
                         ("STRIKE_LIMIT", (3, 4, 6, 8)),
                         ("CYCLE_REPEAT_LIMIT", (2, 3, 4, 5))):
        for v in values:
            with patched(loop_detection, **{name: v}):
                out[f"loop_guard {name}={v}"] = stop_table(tool_lanes, replay_tool_guards, "loop_guard")
    for v in (3, 4, 5, 6, 8, 10):
        with patched(loop_detection, MISTAKE_LIMIT=v):
            out[f"mistake_limit={v}"] = stop_table(tool_lanes, replay_tool_guards, "mistake_limit")
    for field, values in (("min_cycles", (10, 15, 20, 30)),
                          ("max_unique_in_window", (2, 4, 6, 8)),
                          ("phrase_min_cycles", (6, 8, 12, 16)),
                          ("min_chars", (2000, 4000, 8000))):
        for v in values:
            cfg = dataclasses.replace(rlg.DEFAULT, **{field: v})
            out[f"reasoning_loop {field}={v}"] = stop_table(
                lanes, lambda ln, cfg=cfg: replay_reasoning(ln, cfg), "reasoning_loop")
    for name, values in (("BUDGET_TURNS_TO_NUDGE", (2, 3, 4)),
                         ("FAILURES_TO_NUDGE", (2, 3, 4, 5)),
                         ("MIN_ITERATION", (4, 8, 12))):
        for v in values:
            with patched(ss, **{name: v}):
                out[f"struggle {name}={v}"] = stop_table(lanes, replay_struggle, "struggle")
    return out


# ------------------------------------------------------------------ report
def _fmt(c: Counter, keys=("warn", "last_warning", "cut", "nudge", "stuck", "ended",
                           "mixed", "progressing", "lost_new_calls")) -> str:
    return " | ".join(str(c.get(k, 0)) for k in keys)


def write_report(out: Path, lanes: list[Lane], rows: list[dict], sw: dict, meta: dict) -> None:
    by = defaultdict(Counter)
    for r in rows:
        key = r["guard"]
        if r["action"] == "stop":
            by[key][f"stop:{r['label']}"] += 1
        elif r["guard"] == "struggle":
            by[key][f"nudge:{r['reason']}:{r['label']}"] += 1
        elif r["guard"] == "silent_turn":
            by[key][f"{r['what']}{' (on a report)' if r['false'] else ''}"] += 1
        elif r["guard"] == "struggle":
            by[key][f"nudge:{r['reason']}"] += 1
        else:
            by[key][r["action"] + (f":{r['rule']}" if r.get("rule") else "")
                    + (f":{r['kind']}" if r.get("kind") else "")] += 1
    turns = sum(len(ln.turns) for ln in lanes)
    calls = sum(len(ln.calls) for ln in lanes)
    reasoned = sum(1 for ln in lanes for t in ln.turns if t.thinking)
    L = [f"# Guard replay ({meta['date']})", "",
         f"{meta['transcripts']} transcripts, {len(lanes)} tool-loop lanes "
         f"({dict(Counter(ln.role for ln in lanes))}), {turns} turns "
         f"({reasoned} with reasoning text), {calls} tool calls. "
         f"Commit {meta['commit']}. Labels: the lane's next {FUTURE} calls after a stop "
         "(stuck ≤ 1/6 new, progressing ≥ 1/2 new, ended < 3 calls left).", "",
         "## At the shipped thresholds", "", "| guard | events |", "|---|---|"]
    for g in sorted(by):
        L.append(f"| {g} | " + ", ".join(f"{k} {v}" for k, v in sorted(by[g].items())) + " |")
    L += ["", "## Sweeps (one threshold at a time; others at their defaults)", "",
          "| setting | warn | last warning | cut | nudge | stop: stuck | stop: ended | "
          "stop: mixed | stop: progressing | new calls lost |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for k, c in sw.items():
        L.append(f"| {k} | {_fmt(c)} |")
    (out / "report.md").write_text("\n".join(L) + "\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--roots", nargs="+", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    dbs = transcripts(a.roots)
    lanes = [ln for db in dbs for ln in load_lanes(db)]
    rows = run_all(lanes)
    sw = sweeps(lanes)
    commit = subprocess.run(["git", "-C", str(REPO), "describe", "--always", "--dirty"],
                            capture_output=True, text=True).stdout.strip()
    meta = {"date": time.strftime("%Y-%m-%d"), "commit": commit, "transcripts": len(dbs),
            "lanes": len(lanes), "roots": [os.path.basename(r.rstrip("/")) for r in a.roots]}
    (out / "metadata.json").write_text(json.dumps(meta, indent=2) + "\n")
    with open(out / "events.jsonl", "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    write_report(out, lanes, rows, sw, meta)
    print((out / "report.md").read_text())
    return 0


if __name__ == "__main__":
    sys.exit(main())
