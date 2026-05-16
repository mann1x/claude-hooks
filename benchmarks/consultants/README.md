# benchmarks/consultants/

Bench scripts + question banks that implement the **Consultancy
Skill-Eval Protocol**. For the methodology, decision rubric, and
when-to-re-run rules, read
[`docs/consultants-skill-eval-protocol.md`](../../docs/consultants-skill-eval-protocol.md)
FIRST.

This directory is the implementation; the protocol document is
the contract.

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

## Status (2026-05-16)

- ✅ **coder** suite v1.0 (M11b): 8 questions, 4 candidate models,
  pipeline validated end-to-end on dry-run.
- ⏳ **stall** suite (M11a): not yet shipped. Will measure
  inter-token cadence on hard GPQA-style questions.
- ⏳ **tool_executor** suite (M11c): not yet shipped. Will measure
  multi-tool research task success rate.
