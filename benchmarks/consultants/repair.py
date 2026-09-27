#!/usr/bin/env python3
"""Repair benchmark output that a network outage left with holes.

An outage turns judge calls into results without a score: a verdict
with ``score: null`` in a judge_eval ``verdicts.jsonl``, a coder_bench
trial that compiled but has ``quality_score: null``. The rest of the run
is sound, so re-running it would waste the spend; this asks again for
exactly the holes, with the same judge, sampling and prompt, and loops a
few rounds (with a pause, for a line that is still down) until none are
left. It is the last step of a benchmark chain.

    repair.py --questions-dir questions/coder_med \\
        --judge-eval results/2026-09-23/judge_eval=results/2026-09-23/coder_med \\
        --judge-eval results/2026-09-23/judge_eval_june=results/2026-06-04/coder_med \\
        --coder-run results/2026-09-23/coder_med-sampling/glm-5.3-flash-t07

- ``--judge-eval OUT=TRIALS``: the verdicts dir and the trials its
  prompts were built from. Every key whose latest verdict has no score
  is re-judged (grouped by judge label); panel verdicts are re-settled
  after their members are repaired.
- ``--coder-run DIR``: ``fill_judge`` for the trials without a judge
  score. A run with fewer trials than its metadata lists is skipped: it
  is still appending, or it died, and neither may be rewritten.

Exit status 0 when no hole is left, 1 otherwise. Appends only; nothing
that succeeded is re-asked.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO))

from benchmarks.consultants import fill_judge, judge_eval as je  # noqa: E402
from claude_hooks import model_sampling  # noqa: E402

log = logging.getLogger("repair")


def latest(verdicts: list[dict]) -> dict:
    """The last record per key: a repaired verdict supersedes its hole."""
    out = {}
    for v in verdicts:
        out[v["key"]] = v
    return out


def holes(out_dir: Path) -> list[dict]:
    return [v for v in latest(je._load_verdicts(out_dir / "verdicts.jsonl")).values()
            if v.get("score") is None]


def repair_verdicts(out_dir: Path, trials: Path, qdir: Path, *,
                    base: str, timeout_s: float) -> int:
    """Re-judge the member holes, then re-settle panels. Returns the
    number of holes left."""
    todo = [v for v in holes(out_dir) if "#panel=" not in v["judge"]]
    groups: dict[str, list[dict]] = {}
    for v in todo:
        groups.setdefault(v["judge"], []).append(v)
    rows = je.load_raw_trials(trials)
    qmap = je._question_map(qdir)
    vpath = out_dir / "verdicts.jsonl"
    for label, vs in groups.items():
        judge = vs[0].get("judge_model") or model_sampling.model_of(label)
        options = vs[0].get("options") or {}
        want = {v["key"] for v in vs}
        kinds = {v["kind"] for v in vs}
        reps = max([v.get("rep", 0) for v in vs] + [1])
        work = [w for w in je.build_work(
            rows, qmap, judge=judge, kinds=kinds, models=None,
            retest_n=10 ** 9, variant_n=10 ** 9, options=options,
            retest_repeats=reps) if w["key"] in want]
        wire = {**{k: None for k in model_sampling.sampling_for(judge)},
                **options}
        log.info("%s: %d holes, %d rebuildable", label, len(want), len(work))
        client = je._make_client(judge, base, timeout_s)
        for w in work:
            w["wire_options"] = wire
            rec = je._judge_one(client, w)
            rec["repaired"] = True
            with open(vpath, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
            log.info("  %s %s × %s -> %s %s", rec["kind"], rec["question_id"],
                     rec["subject"], rec["score"], rec["error"] or "")
    # Panels: drop their holes' keys from the settled set and settle again.
    for label in sorted({v["judge"] for v in holes(out_dir) if "#panel=" in v["judge"]}
                        | _panel_labels(out_dir)):
        synth, members = label.split("#panel=", 1)
        je.cmd_synth(argparse.Namespace(
            out=str(out_dir), members=members.replace("+", ","), synth=synth,
            trials=str(trials), questions_dir=str(qdir), kinds="base,retest",
            ollama_base=base, timeout_s=timeout_s, concurrency=1))
    return len(holes(out_dir))


def _panel_labels(out_dir: Path) -> set:
    return {v["judge"] for v in latest(je._load_verdicts(out_dir / "verdicts.jsonl")).values()
            if "#panel=" in v["judge"]}


def repair_coder(run: Path, qdir: Path, *, base: str, timeout_s: float) -> int:
    if not fill_judge.is_complete(run):
        log.warning("%s: incomplete (still running or died); skipped", run)
        return -1
    fill_judge.main(["--run-dir", str(run), "--questions-dir", str(qdir),
                     "--ollama-base", base, "--timeout-s", str(timeout_s)])
    trials = [json.loads(l) for l in (run / "trials.jsonl").read_text(
        encoding="utf-8").splitlines() if l.strip()]
    return sum(fill_judge.needs_judge(t) for t in trials)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--questions-dir", required=True)
    ap.add_argument("--judge-eval", action="append", default=[],
                    metavar="OUT=TRIALS")
    ap.add_argument("--coder-run", action="append", default=[])
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--pause-s", type=float, default=300.0,
                    help="between rounds, for a line that is still down")
    ap.add_argument("--ollama-base", default="http://192.168.178.161:11434")
    ap.add_argument("--timeout-s", type=float, default=300.0)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    qdir = Path(args.questions_dir)
    pairs = []
    for spec in args.judge_eval:
        out, sep, trials = spec.partition("=")
        if not sep:
            ap.error(f"--judge-eval wants OUT=TRIALS, got {spec!r}")
        pairs.append((Path(out), Path(trials)))
    left = 0
    for rnd in range(1, max(1, args.rounds) + 1):
        left = 0
        for out, trials in pairs:
            n = repair_verdicts(out, trials, qdir, base=args.ollama_base,
                                timeout_s=args.timeout_s)
            log.info("round %d: %s: %d holes left", rnd, out, n)
            left += n
        for run in map(Path, args.coder_run):
            n = repair_coder(run, qdir, base=args.ollama_base,
                             timeout_s=args.timeout_s)
            log.info("round %d: %s: %s unjudged left", rnd, run,
                     "skipped" if n < 0 else n)
            left += max(0, n)
        if left == 0:
            log.info("REPAIR_CLEAN after round %d", rnd)
            return 0
        if rnd < args.rounds:
            log.info("round %d left %d holes; pausing %.0fs", rnd, left,
                     args.pause_s)
            time.sleep(args.pause_s)
    log.error("%d holes left after %d rounds", left, args.rounds)
    return 1


if __name__ == "__main__":
    sys.exit(main())
