# benchmarks/consultants/

Bench scripts + question banks that implement the **Consultancy
Skill-Eval Protocol**. For the methodology, decision rubric, and
when-to-re-run rules, read
[`docs/consultants-skill-eval-protocol.md`](../../docs/consultants-skill-eval-protocol.md)
FIRST.

This directory is the implementation; the protocol document is
the contract. For where to find **results** (and all other benchmark
families), start at the
[benchmark index](../../docs/benchmarks/index.md).

## Layout

```
benchmarks/consultants/
├── README.md                    (this file)
├── harness.py                   shared library: question loader, trial
│                                schema, oracle grader, judge helper,
│                                cost estimator, suite manifest parser
├── coder_bench.py               M11b runner CLI
├── analyze.py                   markdown report renderer + rubric applier
├── questions/
│   └── coder/
│       ├── SUITE.md             coder suite v1.0 manifest + rubric
│       ├── <id>.md              one per question — model-facing task
│       └── <id>-oracle.py       one per question — harness-facing
│                                pytest oracle
└── results/                     per-run outputs, git-ignored
    └── <YYYY-MM-DD>/
        └── <suite>/
            ├── metadata.json    run header (suite ver, models, git
            │                    commit, etc.)
            ├── trials.jsonl     one JSON line per (question × model)
            ├── trials/<NN>-<model>-<qid>/   per-trial sandbox dir
            │                    with the produced code on disk
            └── report.md        rendered by analyze.py
```

## Quick start (dry-run)

```bash
python benchmarks/consultants/coder_bench.py --dry-run \
    --models kimi-k2.6:cloud,qwen3-next:cloud,glm-5.1:cloud,gemma4:31b-cloud
```

Should print `32 PASS / 32 trials` in ~10 s, no cloud calls.

## Live run (with cost-gate)

```bash
# Step 1 — see the cost estimate without running
python benchmarks/consultants/coder_bench.py --live \
    --models kimi-k2.6:cloud,qwen3-next:cloud,glm-5.1:cloud,gemma4:31b-cloud

# Step 2 — run for real
python benchmarks/consultants/coder_bench.py --live --accept-cost \
    --models kimi-k2.6:cloud,qwen3-next:cloud,glm-5.1:cloud,gemma4:31b-cloud \
    --ollama-base http://192.168.178.2:11433 \
    --judge-model kimi-k2.6:cloud

# Step 3 — render the report
python benchmarks/consultants/analyze.py \
    benchmarks/consultants/results/$(date -u +%Y-%m-%d)/coder/trials.jsonl
```

The report.md applies the rubric and recommends a default model
(or explains why none qualified). If a winner is named, **append**
the score to
[`docs/consultants-skill-eval-baselines.md`](../../docs/consultants-skill-eval-baselines.md).

## Smoke / partial runs

- `--smoke` — only trivial-tier questions (2 × N_models = 8 trials,
  ~5 min live). Useful for harness-change validation, not for
  rubric-grade conclusions.
- `--tier easy --tier medium` — pick tiers; the `--tier` flag is
  repeatable.
- `--id easy-02-fib --id hard-01-matrix-path` — pick specific
  questions; also repeatable.
- `--judge-model ''` — skip the judge LLM (quality_score will be
  None on every trial, rubric will report "no qualifying model").
  Cheap when you only want the pass/fail signal.

## Adding a question

Per the protocol document — short version:

1. Create `<tier>-<NN>-<id>.md` with frontmatter (see existing
   questions for the schema).
2. Create `<tier>-<NN>-<id>-oracle.py` — pytest file importing
   from `$CODER_SANDBOX`.
3. Append the id to `manifest:` in `SUITE.md`; bump
   `suite_version`.
4. Re-baseline every previously-scored model on the new suite
   version. The dry-run should still pass.

## Status

All sub-protocols have shipped and have a live baseline:

- ✅ **coder** v1.0 (M11b, 2026-05-16) — 8 Python questions × 4 models.
  Winner: `glm-5.1:cloud`. Re-scored 2026-06-03 with `minimax-m3` +
  `nemotron-3-super` (both 8/8).
- ✅ **coder_mlang** v1.0.1 (M11b-mlang, 2026-05-17) — 13 questions × 6
  languages × 5 models. Per-language routing; **no model qualifies on the
  strict axis** (very_hard tier), so picks are normalized over the answerable
  questions — see
  [`docs/benchmarks/coder-mlang-results.md`](../../docs/benchmarks/coder-mlang-results.md).
  (Suite has since grown to 18 questions; the baseline used the original 13.)
- ✅ **coder_easy** v1.0 (2026-06-03) — 30 easy problems × 6 languages × 7
  models = 1260 trials. The *floor-fixing* counterpart to mlang: every
  question discriminates, but the top saturates — only **go**/**rust** separate
  on pass-rate. `glm-5.1:cloud` is the standout all-rounder. See
  [`docs/benchmarks/coder-easy-results.md`](../../docs/benchmarks/coder-easy-results.md).
- ✅ **coder_med** v1.0 (2026-06-04) — 10 algorithmic problems × 6 languages × 7
  models = 420 trials. The **mid-band** that separates the field on correctness
  (pass-rate 100% → 77%). Carries the **cross-judge study**: a second judge
  (`gemini-3-flash-preview`) re-scores all solutions + ranks them head-to-head,
  showing the `kimi` self-judge bias is *easy-only* (−0.27 easy / +0.03 med).
  Regenerated by `gen_coder_med.py`; re-judged offline by `rejudge.py`. See
  [`docs/benchmarks/coder-med-results.md`](../../docs/benchmarks/coder-med-results.md).
- ✅ **stall** v1.0 (M11a, 2026-05-17) — Tier-1, 7 models × 4 questions × 3
  trials. Derives per-model `(stall_threshold_s, hard_cap_s)`.
- ✅ **tool_executor** v1.0 (M11c, 2026-05-17) — 8 questions × 6 models.
  Winner: `gemma4:31b-cloud` (role still default-off pending task #103).

## Where the results live

- **Summary ledger** (one row per run, every suite):
  [`docs/consultants-skill-eval-baselines.md`](../../docs/consultants-skill-eval-baselines.md).
- **Per-run detailed reports** (gitignored `trials.jsonl` + committed
  `report.md`): `results/<date>/<suite>/report.md`, e.g.
  [`results/2026-05-17/tool_executor/report.md`](results/2026-05-17/tool_executor/report.md).
- **Per-language coder deep-dives** (full + normalized):
  [`coder-mlang-results.md`](../../docs/benchmarks/coder-mlang-results.md) (hard tier) ·
  [`coder-easy-results.md`](../../docs/benchmarks/coder-easy-results.md) (easy tier, 7 models) ·
  [`coder-med-results.md`](../../docs/benchmarks/coder-med-results.md) (medium + cross-judge).
  Render the per-language scoreboard from any multi-language `trials.jsonl` with
  `analyze.py --by-language`. The easy/medium suites are regenerated by
  `gen_coder_easy.py` / `gen_coder_med.py` (spec-driven). A prior run's persisted
  sandboxes can be **re-judged offline** by a second judge + ranked head-to-head
  with `rejudge.py --rescore` / `--ladder` (no coder re-run).
- **Adopted defaults in code:**
  [`coder_defaults.py`](../../consultants/engine/coder_defaults.py),
  [`stall_defaults.py`](../../consultants/engine/stall_defaults.py),
  [`tool_executor_defaults.py`](../../consultants/engine/tool_executor_defaults.py).
