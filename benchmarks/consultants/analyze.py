#!/usr/bin/env python3
"""Render the coder skill-eval markdown report from a trials.jsonl
file. Also applies the suite's decision rubric to recommend a
default model.

CLI:

    analyze.py path/to/trials.jsonl [--output report.md]

The script reads ``metadata.json`` next to the trials file for the
suite + harness version provenance; without it the report lands but
flags missing metadata at the top.

Output sections:

1. **Provenance** — harness version, suite version, suite hash, git
   commit, models, run timestamp.
2. **Per-model summary** — pass rate, compile rate, median wall,
   median tokens, avg quality score, qualifies-per-rubric.
3. **Recommended default** — applies rubric; states the winning
   model + the rationale, or "no model qualifies" with the gap.
4. **Per-question detail** — for each question, a small table
   showing each model's outcome (PASS / tests-fail / no-compile /
   error) + tokens.
5. **Reproducibility note** — how to re-run + how to update
   ``docs/consultants-skill-eval-baselines.md``.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Repo-root-aware import.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from benchmarks.consultants.harness import (  # noqa: E402
    CoderTrial, HARNESS_VERSION, load_trials,
)


@dataclass
class ModelStats:
    model: str
    n_trials: int
    n_compiled: int
    n_passed: int
    n_errored: int
    median_wall_s: float
    median_tokens: int
    quality_scores: list[float]

    @property
    def pass_rate(self) -> float:
        return self.n_passed / self.n_trials if self.n_trials else 0.0

    @property
    def compile_rate(self) -> float:
        return self.n_compiled / self.n_trials if self.n_trials else 0.0

    @property
    def avg_quality(self) -> Optional[float]:
        if not self.quality_scores:
            return None
        return statistics.mean(self.quality_scores)

    def qualifies(self, *, pass_floor: float, quality_floor: float) -> bool:
        if self.pass_rate < pass_floor:
            return False
        q = self.avg_quality
        if q is None:
            return False
        return q >= quality_floor


def aggregate_by_model(trials: list[CoderTrial]) -> dict[str, ModelStats]:
    by_model: dict[str, list[CoderTrial]] = {}
    for t in trials:
        by_model.setdefault(t.model, []).append(t)
    out: dict[str, ModelStats] = {}
    for model, ts in by_model.items():
        walls = [t.wall_s for t in ts if t.wall_s > 0]
        toks = [t.tokens_prompt + t.tokens_completion for t in ts
                if (t.tokens_prompt + t.tokens_completion) > 0]
        q = [float(t.quality_score) for t in ts
             if t.quality_score is not None]
        out[model] = ModelStats(
            model=model,
            n_trials=len(ts),
            n_compiled=sum(1 for t in ts if t.compiles),
            n_passed=sum(1 for t in ts if t.passes_tests),
            n_errored=sum(1 for t in ts if t.error),
            median_wall_s=statistics.median(walls) if walls else 0.0,
            median_tokens=int(statistics.median(toks)) if toks else 0,
            quality_scores=q,
        )
    return out


def recommend_default(stats: dict[str, ModelStats], *,
                      rubric: dict) -> tuple[Optional[str], str]:
    """Apply the rubric. Returns (winning_model_or_None, rationale)."""
    pass_floor = float(rubric.get("pass_rate_floor", 0.70))
    quality_floor = float(rubric.get("quality_score_floor", 3.5))
    tie_breaker = str(rubric.get("tie_breaker", "median_tokens"))

    qualifying = [
        s for s in stats.values()
        if s.qualifies(pass_floor=pass_floor, quality_floor=quality_floor)
    ]
    if not qualifying:
        # Find the closest miss for the rationale.
        if not stats:
            return None, "no trials"
        best = max(stats.values(), key=lambda s: s.pass_rate)
        return None, (
            f"no model meets the rubric "
            f"(pass_rate ≥ {pass_floor:.0%}, quality ≥ {quality_floor:.1f}). "
            f"Best so far: **{best.model}** with "
            f"pass_rate={best.pass_rate:.0%}, "
            f"avg_quality="
            + (
                f"{best.avg_quality:.2f}"
                if best.avg_quality is not None else "n/a"
            )
            + ". The role's default stays at the project-global "
              "DEFAULT_MODEL until a follow-up run produces a "
              "qualifying candidate."
        )
    # Pick highest pass_rate, then tie-break by median_tokens (lower=better).
    qualifying.sort(
        key=lambda s: (-s.pass_rate, s.median_tokens),
    )
    winner = qualifying[0]
    runner_ups = qualifying[1:]
    rationale_parts = [
        f"**{winner.model}** wins the rubric: "
        f"pass_rate={winner.pass_rate:.0%}, "
    ]
    if winner.avg_quality is not None:
        rationale_parts.append(
            f"avg_quality={winner.avg_quality:.2f}, "
        )
    rationale_parts.append(
        f"median_tokens={winner.median_tokens}."
    )
    if runner_ups:
        rationale_parts.append(
            " Also qualifying: "
            + ", ".join(
                f"{s.model} ({s.pass_rate:.0%}/"
                + (
                    f"{s.avg_quality:.2f}"
                    if s.avg_quality is not None else "n/a"
                )
                + ")"
                for s in runner_ups
            )
            + "."
        )
    return winner.model, "".join(rationale_parts)


def _trial_status(t: CoderTrial) -> str:
    if t.error:
        return "ERROR"
    if not t.compiles:
        return "no-compile"
    if not t.passes_tests:
        return "tests-fail"
    return "PASS"


def render_report(trials: list[CoderTrial],
                  metadata: Optional[dict] = None) -> str:
    """Produce the markdown report. Pure function; the CLI writes
    the result to disk."""
    lines: list[str] = []
    md = metadata or {}
    suite = md.get("suite") or "coder"
    suite_version = md.get("suite_version") or "(unknown)"
    suite_hash = md.get("suite_hash") or ""
    rubric = md.get("rubric") or {
        "pass_rate_floor": 0.70,
        "quality_score_floor": 3.5,
        "tie_breaker": "median_tokens",
    }

    # ---- Provenance ----
    lines.append(f"# Skill-Eval Report — {suite} suite v{suite_version}")
    lines.append("")
    lines.append("## Provenance")
    lines.append("")
    lines.append("| Field | Value |")
    lines.append("|---|---|")
    lines.append(f"| harness_version | `{md.get('harness_version', HARNESS_VERSION)}` |")
    lines.append(f"| suite | `{suite}` |")
    lines.append(f"| suite_version | `{suite_version}` |")
    lines.append(f"| suite_hash | `{suite_hash[:16]}...` |" if suite_hash
                 else "| suite_hash | _(missing — metadata.json absent)_ |")
    lines.append(f"| released | `{md.get('suite_released', '?')}` |")
    lines.append(f"| run_started_at | `{md.get('run_started_at', '?')}` |")
    lines.append(f"| mode | `{md.get('mode', '?')}` |")
    lines.append(f"| ollama_base | `{md.get('ollama_base') or '(dry-run)'}` |")
    lines.append(f"| judge_model | `{md.get('judge_model') or '(no judge)'}` |")
    lines.append(f"| git_commit | `{md.get('git_commit', '?')}` |")
    lines.append(f"| host | `{md.get('host', '?')}` |")
    lines.append("")

    if not trials:
        lines.append("## No trials recorded\n")
        return "\n".join(lines)

    # ---- Per-model summary ----
    stats = aggregate_by_model(trials)
    lines.append("## Per-model summary")
    lines.append("")
    pass_floor = float(rubric.get("pass_rate_floor", 0.70))
    quality_floor = float(rubric.get("quality_score_floor", 3.5))
    lines.append(
        "Rubric: `pass_rate ≥ "
        f"{pass_floor:.0%}` **AND** "
        f"`avg_quality ≥ {quality_floor:.1f}`. "
        "Tie-broken by `median_tokens` (lower wins)."
    )
    lines.append("")
    lines.append("| Model | Trials | Pass rate | Compile rate | "
                  "Median wall | Median tokens | Avg quality | Qualifies? |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for model in sorted(stats, key=lambda m: (-stats[m].pass_rate, m)):
        s = stats[model]
        q_cell = (f"{s.avg_quality:.2f}" if s.avg_quality is not None
                  else "_n/a_")
        qual = "✅" if s.qualifies(
            pass_floor=pass_floor, quality_floor=quality_floor,
        ) else "❌"
        lines.append(
            f"| `{model}` | {s.n_trials} | "
            f"{s.pass_rate:.0%} ({s.n_passed}/{s.n_trials}) | "
            f"{s.compile_rate:.0%} ({s.n_compiled}/{s.n_trials}) | "
            f"{s.median_wall_s:.1f}s | {s.median_tokens} | "
            f"{q_cell} | {qual} |"
        )
    lines.append("")

    # ---- Recommendation ----
    winner, rationale = recommend_default(stats, rubric=rubric)
    lines.append("## Recommended default for `cfg.roles.coder.model`")
    lines.append("")
    if winner:
        lines.append(rationale)
        lines.append("")
        lines.append(
            "To adopt this default, update "
            "`consultants/engine/coder_defaults.py` "
            "(or `cfg.roles.coder.model` in the config TOML) and "
            "record this score in "
            "`docs/consultants-skill-eval-baselines.md`."
        )
    else:
        lines.append(rationale)
    lines.append("")

    # ---- Per-question detail ----
    lines.append("## Per-question detail")
    lines.append("")
    # Group by tier (sorted) → question (sorted).
    by_tier: dict[str, dict[str, list[CoderTrial]]] = {}
    for t in trials:
        by_tier.setdefault(t.tier, {}).setdefault(t.question_id, []).append(t)
    tier_order = ["trivial", "easy", "medium", "hard"]
    for tier in [t for t in tier_order if t in by_tier]:
        lines.append(f"### {tier}")
        lines.append("")
        for qid in sorted(by_tier[tier]):
            qts = by_tier[tier][qid]
            lines.append(f"**{qid}**")
            lines.append("")
            lines.append(
                "| Model | Status | Iters | Tokens | Quality | Code lines |"
            )
            lines.append("|---|---|---|---|---|---|")
            for t in sorted(qts, key=lambda x: x.model):
                status = _trial_status(t)
                q_cell = (
                    f"{t.quality_score:.1f}"
                    if t.quality_score is not None else "—"
                )
                lines.append(
                    f"| `{t.model}` | {status} | {t.iterations} | "
                    f"{t.tokens_prompt + t.tokens_completion} | "
                    f"{q_cell} | {t.code_lines} |"
                )
            lines.append("")

    # ---- Reproducibility ----
    lines.append("## Reproducibility")
    lines.append("")
    models_csv = ",".join(md.get("models", ["<MODELS>"]))
    lines.append("Re-run this exact suite version:")
    lines.append("")
    lines.append("```")
    lines.append(
        "python benchmarks/consultants/coder_bench.py \\\n"
        f"    --live --accept-cost \\\n"
        f"    --models {models_csv} \\\n"
        f"    --ollama-base {md.get('ollama_base') or 'http://192.168.178.2:11433'} \\\n"
        f"    --judge-model {md.get('judge_model') or 'kimi-k2.6:cloud'}"
    )
    lines.append("```")
    lines.append("")
    lines.append(
        "If `suite_hash` differs from this run's "
        f"(`{suite_hash[:12]}` if recorded), the question content drifted "
        "without a SUITE.md version bump — investigate before "
        "comparing baselines."
    )
    lines.append("")
    lines.append(
        "Add the line for this run to "
        "`docs/consultants-skill-eval-baselines.md` so future-you "
        "can compare new candidates against today's numbers."
    )
    lines.append("")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="analyze",
        description="Render coder-bench trials.jsonl as a markdown report.",
    )
    p.add_argument(
        "trials_path", type=Path,
        help="Path to trials.jsonl produced by coder_bench.py",
    )
    p.add_argument(
        "--output", type=Path, default=None,
        help="Where to write report.md. Default: next to trials.jsonl",
    )
    args = p.parse_args(argv)
    if not args.trials_path.is_file():
        print(f"error: trials file not found: {args.trials_path}",
              file=sys.stderr)
        return 2
    metadata_path = args.trials_path.parent / "metadata.json"
    metadata = None
    if metadata_path.is_file():
        try:
            metadata = json.loads(metadata_path.read_text())
        except json.JSONDecodeError:
            print(f"warning: metadata.json corrupt at {metadata_path}",
                  file=sys.stderr)
    trials = load_trials(args.trials_path)
    if not trials:
        print(f"error: no trials in {args.trials_path}", file=sys.stderr)
        return 1
    report = render_report(trials, metadata=metadata)
    out_path = args.output or (args.trials_path.parent / "report.md")
    out_path.write_text(report, encoding="utf-8")
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
