"""Scan a coder-bench trials.jsonl and identify trials that hit a
timeout / empty-content / retry-storm pathology on any of:
  - the coder itself          (trial.error, or wall_s >> inference_s)
  - the read-only judge       (quality_score=None with timeout/empty rationale)
  - the audit judge           (quality_audit_score=None ...)
  - the meta judge            (quality_meta_score=None ...)
  - pytest session-level      (test_results empty + stderr "session timed out")

The default action is to **print** the (question_id, model) tuples and
the matching CLI invocation. With ``--fire`` the helper re-runs only
the matching subset and writes the new rows to a sibling
``trials.rerun.jsonl`` next to the original (no mutation of the
historical data).

Usage:
  # Identify and print
  python -m benchmarks.consultants.rerun_timeouts \\
    --trials benchmarks/consultants/results/2026-05-17/coder_mlang-v1.0.1/trials.jsonl

  # Also fire the rerun with the tightened budgets baked in
  python -m benchmarks.consultants.rerun_timeouts \\
    --trials ...trials.jsonl --fire --accept-cost

The rerun reads its judge models + ollama base from the original
metadata.json so we don't have to repeat them on the CLI.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# Phrases that signal "this judge call timed out / retried to death /
# returned empty content twice / network-errored". Match against the
# rationale field. Lower-case substring match.
TIMEOUT_SIGNALS = (
    "empty content twice",
    "empty then raised",
    "timed out",
    "timeout",
    "raised:",
    "judge raised",
    "audit judge raised",
    "meta judge raised",
    "could not read produced file",
)


def _rationale_indicates_timeout(rat: Optional[str]) -> bool:
    if not rat:
        return False
    lowered = rat.lower()
    return any(sig in lowered for sig in TIMEOUT_SIGNALS)


def _judge_timed_out(score: Optional[float], rationale: str) -> bool:
    """A judge is "timed out" iff it produced no score AND the
    rationale matches one of the timeout signals. This distinguishes
    a real "judge said no score" failure from a parse failure (which
    we keep as-is)."""
    return score is None and _rationale_indicates_timeout(rationale)


def _pytest_session_timed_out(trial: dict) -> bool:
    """pytest session timed out (the OUTER safety net fired). With
    the pytest-timeout per-test plugin this should be rare — but
    catch it for completeness."""
    test_results = trial.get("test_results") or {}
    stderr = (trial.get("test_output") or "").lower()
    return (
        not test_results
        and trial.get("compiles")
        and "session timed out" in stderr
    )


def _coder_timed_out(trial: dict) -> bool:
    """The coder hit the per-attempt timeout enough times that the
    final ChatClient retry-budget fired, OR the agent loop raised.

    Signals:
      - ``trial.error`` non-empty (coder_node raised an exception)
      - large delta between wall_s and inference_s on the trial
        itself (retries burned wall but didn't accumulate inference)
    """
    if trial.get("error"):
        return True
    wall = float(trial.get("wall_s") or 0.0)
    inf = float(trial.get("inference_s") or 0.0)
    # >= 60s of pure retry overhead on the coder LLM is suspicious.
    # Normal coder calls have wall_s ≈ inference_s within float
    # epsilon; a 60s+ gap means at least one retry sleep + timeout
    # cycle fired.
    return (wall - inf) >= 60.0


def classify_trial(trial: dict) -> dict:
    """Return a dict of timeout flags. ``timed_out`` is True if ANY
    flag is set. Pure function — does not mutate ``trial``."""
    coder = _coder_timed_out(trial)
    pytest_outer = _pytest_session_timed_out(trial)
    judge_a = _judge_timed_out(
        trial.get("quality_score"), trial.get("quality_rationale", ""),
    )
    judge_b = _judge_timed_out(
        trial.get("quality_audit_score"),
        trial.get("quality_audit_rationale", ""),
    )
    judge_c = _judge_timed_out(
        trial.get("quality_meta_score"),
        trial.get("quality_meta_rationale", ""),
    )
    return {
        "coder": coder,
        "pytest_session": pytest_outer,
        "judge_a": judge_a,
        "judge_b": judge_b,
        "judge_c": judge_c,
        "timed_out": (coder or pytest_outer or judge_a or judge_b or judge_c),
    }


def load_trials(trials_path: Path) -> list[dict]:
    out: list[dict] = []
    with trials_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def find_timeouts(trials: list[dict]) -> list[dict]:
    """Return the list of trials that hit any timeout. Adds a
    ``_flags`` key with the classification dict for the printer."""
    matches = []
    for t in trials:
        flags = classify_trial(t)
        if flags["timed_out"]:
            t = dict(t)
            t["_flags"] = flags
            matches.append(t)
    return matches


def render_summary(matches: list[dict], total: int) -> str:
    if not matches:
        return f"No timeout-tagged trials in {total} total. No rerun needed."
    lines = [
        f"Found {len(matches)} timeout-tagged trials (of {total} total):",
        "",
    ]
    by_q: dict[str, list[dict]] = {}
    by_m: dict[str, int] = {}
    for t in matches:
        by_q.setdefault(t["question_id"], []).append(t)
        by_m[t["model"]] = by_m.get(t["model"], 0) + 1
    for q in sorted(by_q):
        lines.append(f"  {q}")
        for t in by_q[q]:
            flags = t["_flags"]
            tagged = []
            if flags["coder"]: tagged.append("coder")
            if flags["pytest_session"]: tagged.append("pytest-session")
            if flags["judge_a"]: tagged.append("judge-A")
            if flags["judge_b"]: tagged.append("judge-B")
            if flags["judge_c"]: tagged.append("judge-C")
            lines.append(
                f"    × {t['model']:30s}  [{','.join(tagged)}]"
                f"  wall_s={t.get('wall_s', 0):.0f}"
            )
    lines.append("")
    lines.append("Affected models (count of timeout trials):")
    for m in sorted(by_m):
        lines.append(f"  {m}: {by_m[m]}")
    return "\n".join(lines)


def build_rerun_invocation(matches: list[dict], metadata: dict,
                           output_dir: Path) -> list[str]:
    """Build a shell-quoted argv for a coder-bench rerun on the
    matching subset. Reads judge models + ollama base from the
    parent run's metadata.json (so the rerun is reproducible without
    repeating the config)."""
    cmd = [
        sys.executable, "-m", "benchmarks.consultants.coder_bench",
        "--live", "--accept-cost",
        "--questions-dir",
        # Reconstruct from suite name — assume default layout.
        str(_REPO_ROOT / "benchmarks" / "consultants" / "questions"
            / metadata.get("suite", "coder_mlang")),
        "--output-dir", str(output_dir),
        "--ollama-base", metadata.get(
            "ollama_base", "http://192.168.178.2:11433",
        ),
        "--judge-model", metadata.get("judge_model") or "",
    ]
    if metadata.get("audit_judge_model"):
        cmd += ["--audit-judge-model", metadata["audit_judge_model"]]
    if metadata.get("meta_judge_model"):
        cmd += ["--meta-judge-model", metadata["meta_judge_model"]]
    # Tighter judge budget on the rerun — that's the whole point.
    cmd += ["--judge-timeout-s", "60", "--judge-max-retries", "3"]
    # Models = the unique set in the matches.
    models = sorted({t["model"] for t in matches})
    cmd += ["--models", ",".join(models)]
    # Questions = unique ids; one --id per question.
    for q in sorted({t["question_id"] for t in matches}):
        cmd += ["--id", q]
    return cmd


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="rerun_timeouts",
        description=__doc__,
    )
    p.add_argument(
        "--trials", type=Path, required=True,
        help="Path to the coder-bench trials.jsonl to scan.",
    )
    p.add_argument(
        "--output-dir", type=Path, default=None,
        help=("Where the rerun writes its trials.rerun.jsonl + "
              "metadata.json. Defaults to <parent>/rerun-<utc-stamp>/"),
    )
    p.add_argument(
        "--fire", action="store_true",
        help="Actually invoke the rerun (default: just print the plan).",
    )
    p.add_argument(
        "--accept-cost", action="store_true",
        help="Required with --fire (passed through to coder_bench).",
    )
    args = p.parse_args(argv)

    if not args.trials.is_file():
        print(f"ERROR: {args.trials} not found", file=sys.stderr)
        return 1
    trials = load_trials(args.trials)
    matches = find_timeouts(trials)
    print(render_summary(matches, len(trials)))
    if not matches:
        return 0
    # Load parent metadata for reusable config.
    parent_meta_path = args.trials.parent / "metadata.json"
    if not parent_meta_path.is_file():
        print(
            f"\nERROR: metadata.json not found next to "
            f"{args.trials}; cannot build rerun invocation.",
            file=sys.stderr,
        )
        return 1
    metadata = json.loads(parent_meta_path.read_text(encoding="utf-8"))
    # Output dir: sibling rerun-<stamp>/
    if args.output_dir is None:
        import datetime
        stamp = datetime.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        args.output_dir = args.trials.parent / f"rerun-{stamp}"
    cmd = build_rerun_invocation(matches, metadata, args.output_dir)
    print("\nRerun invocation:")
    print("  " + " \\\n    ".join(cmd))
    if not args.fire:
        print("\n(dry-run; pass --fire --accept-cost to execute)")
        return 0
    if not args.accept_cost:
        print(
            "\nERROR: --fire requires --accept-cost (passed through "
            "to coder_bench).",
            file=sys.stderr,
        )
        return 1
    import subprocess
    args.output_dir.mkdir(parents=True, exist_ok=True)
    log_path = args.output_dir / "rerun-run.log"
    print(f"\nFiring rerun. Streaming log to {log_path}.")
    with log_path.open("w", encoding="utf-8") as logf:
        proc = subprocess.run(cmd, stdout=logf, stderr=subprocess.STDOUT)
    print(f"Rerun exited {proc.returncode}. See {log_path}.")
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
