# Consultancy Skill-Eval Protocol

The Consultancy Skill-Eval Protocol is the canonical procedure for
deciding which model should serve as the default for each
`/consultants` role (`coder`, `tool_executor`, `researcher`,
`critic`, `planner`, `synthesizer`). It is **the** evaluation
method — not a one-off benchmark — and is rerun every time:

- A new candidate model lands on the proxy (e.g., when the cloud
  upstream publishes a new tag);
- A core proxy-side change might affect tokens-per-iteration or
  inter-token latency;
- A role's prompt or topology shape changes materially (e.g.,
  PLAN-mode researcher in M6, sandboxed coder in M10);
- A new question is added to a suite (suite version bumps; every
  prior model needs re-baselining on the new manifest).

The protocol is intentionally **simple to re-run** so the
operational cost of trying a candidate model is one command + one
docs commit, not a multi-day effort. **If running the eval ever
feels expensive, we got the protocol wrong** — please file an
issue.

---

## Three sub-protocols (one per skill we test)

| Sub-protocol     | Bench dir / file                                          | Role(s) it gates                              | Manifest version |
|------------------|-----------------------------------------------------------|-----------------------------------------------|------------------|
| **coder**        | [`benchmarks/consultants/questions/coder/`](../benchmarks/consultants/questions/coder/) | `cfg.roles.coder.model`                       | v1.0 (2026-05-16)|
| **stall**        | _(M11a; lands in a later commit)_                         | M3 stall thresholds; informs every role's caps| not yet shipped   |
| **tool_executor**| _(M11c; lands in a later commit)_                         | `cfg.roles.tool_executor.model` + the role's default-on bit | not yet shipped |

Each sub-protocol has its own SUITE.md manifest, decision rubric,
and metric set; they share the harness (`benchmarks/consultants/
harness.py`), the trial schema, and the markdown report renderer
so a model's results across the three protocols are comparable.

---

## Coder sub-protocol (v1.0)

**Decision question**: which cloud model is the best default for
`cfg.roles.coder.model`?

**Manifest**: 8 questions across 4 tiers (trivial / easy / medium /
hard), curated from HumanEval-style problems with hand-written
pytest oracles. See
[`SUITE.md`](../benchmarks/consultants/questions/coder/SUITE.md) for
the full manifest and the rubric thresholds.

**Per-trial flow** (one question × one model):

1. The harness creates an isolated per-trial sandbox at
   `benchmarks/consultants/results/<date>/coder/trials/<NN>-<model>-<qid>/`.
2. `coder_node` is invoked with the question's task as a
   `CoderTaskItem`. The node creates its per-session sandbox under
   `<trial-dir>/.claude-hooks/consultants/bench/coder-out/` and
   wires the sandbox-only `write_file` tool.
3. The model (via `ChatClient`) runs its agent loop. Each
   iteration's LLM call + tool call is recorded.
4. After the lane completes, the harness:
   - Locates the produced file at the path the suite specified.
   - Runs `py_compile` (does it parse?).
   - Subprocesses pytest against the oracle file with
     `CODER_SANDBOX` env var set; the oracle imports the produced
     module by `sys.path.insert` (does it pass tests?).
   - Counts non-comment code lines.
   - Optionally measures cyclomatic complexity via `radon` (soft
     dep).
   - On a successful compile, calls the **judge LLM** with a strict
     1-5 rubric (`SCORE: <n>` + a one-sentence rationale).
5. Trial result is appended to `trials.jsonl` immediately so a
   Ctrl-C mid-run loses at most the in-progress trial.

**Per-model aggregation** (`analyze.py`):

- `pass_rate` = #trials passing `passes_tests` / total trials
- `compile_rate` = #trials passing `compiles` / total
- `median_wall_s` and `median_tokens` across trials
- `avg_quality_score` across trials with a recorded judge score

**Rubric** (pinned in SUITE.md):

> A model **qualifies as the coder default** iff
> `pass_rate ≥ 0.70` AND `avg_quality_score ≥ 3.5`.
>
> Among qualifying models, the **recommended default** is the one
> with the highest `pass_rate`. Ties break on `median_tokens`
> (cheaper wins). If no model qualifies, the role's default stays
> at the project-global `DEFAULT_MODEL` and a follow-up run
> evaluates a different candidate set — never silently flip the
> default on a sub-threshold model.

---

## How to run a sub-protocol

Two phases, mandatory order:

### 1. Dry-run validation

Always start here. Validates that:

- The suite files load cleanly (frontmatter parses, oracles exist,
  manifest matches).
- The pytest oracles pass against the canonical reference
  submissions baked into `coder_bench.py` (no model variance, just
  pipeline sanity).
- The metrics path produces sensible numbers.

```bash
python benchmarks/consultants/coder_bench.py --dry-run \
    --models kimi-k2.6:cloud,qwen3-next:cloud,glm-5.1:cloud,gemma4:31b-cloud
```

Expected: **32 PASS / 32 trials** in ~5–10 seconds, no cloud calls.

If any trial fails on dry-run, **stop**: the suite or the harness
has drifted. Investigate before spending tokens on a live run.

### 2. Live run

```bash
python benchmarks/consultants/coder_bench.py --live --accept-cost \
    --models kimi-k2.6:cloud,qwen3-next:cloud,glm-5.1:cloud,gemma4:31b-cloud \
    --ollama-base http://192.168.178.2:11433 \
    --judge-model kimi-k2.6:cloud
```

The summary line at run start declares the estimated token cost.
`--accept-cost` is mandatory with `--live`; without it the script
prints the estimate and exits 2.

### 3. Render the report

```bash
python benchmarks/consultants/analyze.py \
    benchmarks/consultants/results/<date>/coder/trials.jsonl
```

Output lands at `benchmarks/consultants/results/<date>/coder/report.md`.

### 4. Record the baseline

If the live run produced a qualifying candidate, **append the
result line** to
[`docs/consultants-skill-eval-baselines.md`](consultants-skill-eval-baselines.md):

```
| 2026-05-16 | coder | 1.0 | kimi-k2.6:cloud | 87% | 4.1 | 8240 | 9aa6eaf0... |
```

The baselines ledger is the running record across runs; future
sessions consult it to see the trend per model + suite version.

### 5. (Optional) update the consultant default

If the rubric recommended a new winner, **edit
`consultants/engine/coder_defaults.py`** (or add it in a follow-up
commit when that file doesn't yet exist) to capture the
recommendation as a code constant. The runtime config still wins
when the user has a per-session override; the default only kicks
in for fresh configs.

---

## When to re-run

Re-run the **whole sub-protocol** (full live run) when:

- A new candidate model lands and you want to evaluate it. **Run
  the full suite** for the new model and re-render the report;
  don't run only 2 questions and call it good — the rubric's
  pass_rate is meaningful only at the full denominator.
- The cloud upstream had a known disruption (model retraining,
  proxy infrastructure change, large flap event) that might shift
  baselines. Re-run every model so the ledger reflects the new
  reality.
- The suite version bumps (a new question was added). Every
  previously-baselined model needs a v(N+1) data point; the
  v(N) numbers stay valid for archival comparison but cannot be
  mixed with v(N+1) numbers when computing the rubric.

Re-run **a smoke subset** (`--smoke` → trivial tier only) when:

- The bench harness itself has changed. Compare the smoke results
  before and after the harness change; a delta indicates the
  harness is the variable, not the model.
- You're adding a new question and want to sanity-check the
  oracle's stability against a known-good reference. The smoke
  run is fast and cheap.

Do **not** re-run when:

- A model's score is "close" to the rubric threshold. The fix is
  to add questions that resolve the ambiguity (suite v(N+1)), not
  to re-roll the dice until you get a passing number.
- A single trial's quality score looked low. One judge call is
  noisy by design; the rubric averages across the suite.

---

## Adding a new question

See the "Adding a question" section in
[`SUITE.md`](../benchmarks/consultants/questions/coder/SUITE.md).
The summary:

1. Author the markdown task file + the pytest oracle, place both in
   the suite directory.
2. Append the new id to the `manifest:` list in `SUITE.md`; bump
   `suite_version` to the next MINOR.
3. Re-baseline every previously-scored model on the new suite
   version. **This is a hard rule** — without re-baselining, the
   baselines ledger mixes v(N) and v(N+1) numbers that can't be
   directly compared.

---

## Adding a new candidate model

1. **Dry-run first**, just to confirm the model name registers and
   the proxy routes it. If the dry-run fails on the new model's
   chat client construction, fix that first — the issue isn't the
   suite, it's the wiring.
2. **Full live run** including all the currently-baselined models.
   Re-baselining the existing models in the same run is what makes
   the new candidate directly comparable; running only the new
   model and comparing against an old baseline conflates model
   skill with cloud-upstream drift.
3. **Record** the run in the baselines ledger. Even if the new
   candidate doesn't qualify, the data point is useful — future-us
   will want to know when we tried this model.

---

## What the protocol explicitly does NOT do

- **It does not pick the user-default model for a fresh `claude-hooks`
  install.** That's a separate question driven by token-economy + UX
  considerations (currently `kimi-k2.6:cloud` for all consultant
  roles). The skill-eval picks the role-specific default once the
  user opts into a role; the global default stays a project-level
  decision.
- **It does not exercise the full council pipeline.** It targets
  one role node at a time so the signal is clean. End-to-end
  council behavior is verified by the M12 parity suite (a separate
  milestone) and the M13 live smoke (also separate).
- **It does not measure cost in dollars.** Ollama Pro is a
  weekly-quota subscription, not a per-call $ price. The harness
  reports token totals; you compare against your Ollama Pro plan
  separately.
- **It does not replace human judgement.** The rubric is a
  decision-support tool. A model that scored 71% pass_rate and 3.6
  quality on a 8-question suite is "qualifying" on paper, but you
  may have other reasons to prefer a different model (latency,
  stability under flap, etc.). Document those reasons in the
  baselines ledger when you choose against the rubric.

---

## Files

| Path | Purpose |
|---|---|
| `benchmarks/consultants/harness.py` | Shared question loader, trial schema, oracle grader, judge helper, cost estimator |
| `benchmarks/consultants/coder_bench.py` | M11b runner CLI |
| `benchmarks/consultants/analyze.py` | Markdown report renderer + rubric applier |
| `benchmarks/consultants/questions/coder/SUITE.md` | Coder suite v1.0 manifest + rubric |
| `benchmarks/consultants/questions/coder/<id>.md` | Per-question task description (model-facing) |
| `benchmarks/consultants/questions/coder/<id>-oracle.py` | Per-question pytest oracle (harness-facing) |
| `benchmarks/consultants/results/<date>/coder/{trials.jsonl,metadata.json,report.md}` | Per-run outputs |
| `docs/consultants-skill-eval-baselines.md` | Running ledger of every model × suite × date |
| `docs/consultants-skill-eval-protocol.md` | This file |

---

## Related reading

- [`docs/RELEASING.md`](RELEASING.md) — release-cut procedure
  (skill-eval results are referenced from CHANGELOG entries that
  adopt new defaults).
- [`docs/consultants.md`](consultants.md) (link forthcoming with M13) —
  user-facing `/consultants` documentation.
- The M11 milestone in the v2 overhaul plan
  (`/root/.claude/plans/recursive-petting-planet.md`) for the
  protocol's original design rationale.
