#!/usr/bin/env python3
"""Judge the coder_bench trials whose judge call failed, in place.

A network outage turns a judge call into ``quality_score: null`` on a
trial whose code compiled and was tested. Ladders and pass/quality
tables then count that trial as quality 0 (or drop it), so an outage
reads as a worse model. This re-asks the run's own judge for exactly
those trials — same prompt, same model — adds the calls to the trial's
``usage`` (the failed attempts were already recorded there), and
rewrites ``trials.jsonl`` atomically. No coder is re-run.

Run it only after the arm has finished: coder_bench appends to the file.

    fill_judge.py --run-dir results/2026-09-23/coder_med-sampling/glm-5.3-flash-t07 \\
        --questions-dir questions/coder_med
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO))

from benchmarks.consultants.coder_bench import (  # noqa: E402
    _judge_trial_quality, _panel_trial_quality)
from benchmarks.consultants.harness import load_questions  # noqa: E402


def needs_judge(t: dict) -> bool:
    return bool(t.get("compiles")) and t.get("quality_score") is None


def is_complete(run: Path) -> bool:
    """Every question in metadata has a trial. A shorter file is a run
    still appending (or one that died): not safe to rewrite."""
    try:
        meta = json.loads((run / "metadata.json").read_text(encoding="utf-8"))
        n = sum(1 for l in (run / "trials.jsonl").read_text(
            encoding="utf-8").splitlines() if l.strip())
    except (OSError, json.JSONDecodeError):
        return False
    want = len(meta.get("questions") or []) * len(meta.get("models") or [1])
    return n >= want > 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--questions-dir", required=True)
    ap.add_argument("--judge-model", default="",
                    help="default: the run's metadata.json judge_model")
    ap.add_argument("--ollama-base", default="http://192.168.178.161:11434")
    ap.add_argument("--timeout-s", type=float, default=300.0)
    args = ap.parse_args(argv)
    run = Path(args.run_dir)
    meta = json.loads((run / "metadata.json").read_text(encoding="utf-8"))
    panel = [] if args.judge_model else list(meta.get("judge_panel") or [])
    synth = meta.get("synth_judge_model") or ""
    judge = args.judge_model or meta.get("judge_model")
    if not judge and not panel:
        print("no judge in metadata; pass --judge-model", file=sys.stderr)
        return 2
    path = run / "trials.jsonl"
    trials = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()
              if l.strip()]
    todo = [t for t in trials if needs_judge(t)]
    who = f"panel {'+'.join(panel)} > {synth}" if panel else judge
    print(f"{len(todo)} of {len(trials)} trials need the judge ({who})")
    if not todo:
        return 0
    from benchmarks.consultants.harness import bench_client as make_agent_chat_client
    kw = dict(timeout_s=args.timeout_s, max_retries=3)
    if panel:
        members = [(m, make_agent_chat_client(m, args.ollama_base, **kw))
                   for m in panel]
        synth_c = ((synth, make_agent_chat_client(synth, args.ollama_base, **kw))
                   if synth else None)
    else:
        client = make_agent_chat_client(judge, args.ollama_base, **kw)
    qs = {q.id: q for q in load_questions(Path(args.questions_dir))}
    for t in todo:
        q = qs[t["question_id"]]
        produced = (Path(t["sandbox_dir"]) / ".claude-hooks" / "consultants"
                    / "bench" / "coder-out")
        usage = t.setdefault("usage", {})
        if panel:
            res = _panel_trial_quality(
                panel=members, synth=synth_c, usage=usage, task=q.task,
                sandbox=produced, sandbox_path=q.sandbox_path,
                key=f"{t['question_id']}|{t['model']}")
            score, rationale = res["score"], res["rationale"]
            t["quality_panel"] = res
        else:
            score, rationale = _judge_trial_quality(
                judge_chat_client=client, judge_model=judge, usage=usage,
                task=q.task, sandbox=produced, sandbox_path=q.sandbox_path)
        t["quality_score"], t["quality_rationale"] = score, rationale
        t["quality_filled"] = True  # judged after the run, see fill_judge.py
        print(f"  {t['question_id']} -> {score} {rationale[:70]}")
    tmp = path.with_suffix(".jsonl.tmp")
    tmp.write_text("".join(json.dumps(t) + "\n" for t in trials),
                   encoding="utf-8")
    os.replace(tmp, path)
    left = sum(needs_judge(t) for t in trials)
    print(f"done; {left} still unjudged")
    return 0 if left == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
