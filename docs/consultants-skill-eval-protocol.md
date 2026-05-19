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
| **coder**        | [`benchmarks/consultants/questions/coder/`](../benchmarks/consultants/questions/coder/) | `cfg.roles.coder.model` (Python; global)      | v1.0 (2026-05-16) — `glm-5.1:cloud` |
| **coder_mlang**  | [`benchmarks/consultants/questions/coder_mlang/`](../benchmarks/consultants/questions/coder_mlang/) | `cfg.roles.coder.model` (per-language + global override) | v1.0 (2026-05-16, build-out in progress) |
| **stall**        | [`benchmarks/consultants/questions/stall/`](../benchmarks/consultants/questions/stall/) | M3 stall thresholds — per-model `(stall_threshold_s, hard_cap_s)` overrides in `consultants/engine/stall_defaults.py` | v1.0 (2026-05-17, harness shipped; live data lands in M11a-2) |
| **tool_executor**| [`benchmarks/consultants/questions/tool_executor/`](../benchmarks/consultants/questions/tool_executor/) | `cfg.roles.tool_executor.model` + the role's default-on bit | v1.0 (2026-05-17, harness shipped; live data lands in M11c-2) |

Each sub-protocol has its own SUITE.md manifest, decision rubric,
and metric set; they share the harness (`benchmarks/consultants/
harness.py`), the trial schema, and the markdown report renderer
so a model's results across the three protocols are comparable.

The two coder sub-protocols answer **different decision
questions**: `coder` picks "best Python coder" against a wide
question set covering trivial→hard; `coder_mlang` picks "best
coder for Rust / Go / C / C++ / C# / Python" against a
deliberately harder question set that drops trivial+easy tiers
because the v1.0 `coder` run found those tiers stopped
discriminating between top models. The two coexist — re-baselining
one does not invalidate the other. See
[`consultants-skill-eval-mlang-suite.md`](consultants-skill-eval-mlang-suite.md)
for the multi-language design + manifest.

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

## Stall sub-protocol (v1.0)

**Decision question**: what `(stall_threshold_s, hard_cap_s)`
values should the M3 stall detector use per model?

The 2026-05-15 audit-session pathology — gemini-3-flash lanes that
held the TCP connection open for 27-31 min producing zero useful
tokens — motivated the M3 stall detector. M3 shipped with
**global** defaults (`300 s / 3600 s` in
`consultants/engine/control.py`) that are conservative guesses,
not measurements. M11a is the bench that turns those guesses into
per-model evidence.

**Manifest**: 8 questions across 2 tiers (4 standalone + 4
council), curated from researcher-style analytical prompts +
GPQA-Diamond-style external anchors. See
[`SUITE.md`](../benchmarks/consultants/questions/stall/SUITE.md)
for the full manifest and rubric block.

**Two tiers**:

1. **Tier 1 — standalone** (4 questions). One `chat_streamed` call
   per (question × model × trial_idx). Cheap; per-model baseline
   covering the full cohort.
2. **Tier 2 — fake-consultancy** (4 questions, 2 synthetic
   audit-style + 2 GPQA-Diamond). One full `build_council_graph`
   run per trial at `effort="medium"`, every role pinned to the
   same model under test. Per-lane `chat_streamed` calls captured
   via a timing-capture shim wrapping every chat client in
   `GraphDeps`. Per-trial p99 aggregated across all inner calls.

When both tiers measured a given model, the derivation prefers
**Tier 2** numbers (representative); Tier 1 is the fallback.

**Per-trial flow**:

1. The harness builds a `TimingCaptureChat` wrapping a real
   `ChatClient` for the model under test. The wrapper intercepts
   `chat_streamed` and records per-token monotonic timestamps.
2. For Tier 1: a single `chat_streamed(payload)` with the
   question body. Records one `CallTiming`; the trial's
   percentiles come directly from that call's gap distribution.
3. For Tier 2: the bench sets `runtime_control` on initial state
   (so the researcher routes chat through
   `stall_protected_chat_fn_for`), invokes the council, and lets
   every chat call land in the same wrapper. After the run, the
   bench reads `capture.calls` (5-8 entries typically) and
   aggregates inter-token + TTFT percentiles across all of them.
4. Trial result is appended to `trials.jsonl` immediately so a
   Ctrl-C mid-run loses at most the in-progress trial.

**Per-model aggregation** (`stall_bench.py:derive_thresholds`):

```
stall_threshold_s = max(p99_inter_token_ms, p99_ttft_ms) * 2.5,
                    in seconds, rounded up to the nearest 30 s,
                    floored at 30 s, ceiled at 600 s.
hard_cap_s        = p99(wall_s) * 3.0,
                    rounded up to the nearest 60 s,
                    floored at 300 s, ceiled at 3600 s.
```

The margin factors (`2.5×` for stall, `3.0×` for hard cap) plus
the floor/ceil clamps live in the rubric block of the suite
manifest, so M11a-2 tunes them without code changes if the
measured data calls for it.

**Rubric** (pinned in SUITE.md):

> Unlike coder, this is **not** a pass/fail gate — every candidate
> model gets recommended thresholds derived from its measured
> percentiles. The bench's `report.md` ranks models by
> **stall safety margin** =
> `(stall_threshold_s - p99_inter_token_s) / p99_inter_token_s` —
> higher is better. The M11a-2 closeout commit pastes the
> resulting per-model rows into
> `consultants/engine/stall_defaults.py` so the runtime picks
> a tuned threshold per model under test, falling back to the
> global `(300, 3600)` default for any unmeasured model.

---

## Tool_executor sub-protocol (v1.0)

**Decision question**: which cloud model is the best default for
`cfg.roles.tool_executor.model`, and should the role flip from
disabled-by-default to enabled-by-default?

The M6 `tool_executor` role consumes a `ToolPlanItem` emitted by
the researcher in PLAN mode, runs a full agent loop with the
shared tool stack (`survey_project`, `list_files`, `read_file`,
`glob`, `grep`, `recall_memory`), and returns a `ToolResult` with
citations the researcher folds into REPORT mode. It ships
**disabled by default** today (`DEFAULT_ENABLED_BY_ROLE
["tool_executor"]=False` in `consultants/config.py`) and the
recommended model is the M6 fallback (`gemma4:31b-cloud`).
M11c provides the empirical evidence that lets us either keep
those defaults, swap the model, or flip the default-on bit.

Unlike coder (which **writes** code) and stall (a pure
measurement bench), this suite measures **reading + reasoning
over an existing codebase via tool calls**. It uses the same
oracle pytest + LLM judge pair as coder, but each oracle reads
the captured assistant text + ordered tool-call log from env
vars rather than imported sandbox files.

**Manifest**: 8 questions across 4 tiers (trivial / easy /
medium / hard), curated for the tool-chain behaviours the role
needs: single-file grep, multi-file audit, ambiguous-intent
survey, citation precision. See
[`SUITE.md`](../benchmarks/consultants/questions/tool_executor/SUITE.md)
for the full manifest and rubric block.

The fixture corpus is **synthetic** — small hand-authored Python
+ Markdown files under
`benchmarks/consultants/questions/tool_executor/fixtures/<name>/`.
Each question's frontmatter names its `fixtures_subdir`; the
bench `cwd`s into that subdir before driving the lane so
`read_file("auth.py")` resolves relative to the cohort, not the
bench's working tree. This keeps the suite stable against
unrelated refactors of the claude-hooks repo itself.

**Per-trial flow** (one question × one model × one trial):

1. The bench builds a `ToolPlanItem` from the question's
   `task` / `why` / `suggested_tools` frontmatter, with
   `lane_idx=0` and `parent_round=1`.
2. The tool stack (`make_executor((fixture_dir,))` +
   `openai_tool_specs()` + `build_grounding_messages(cwd)`) is
   built scoped to the fixture cohort.
3. `tool_executor_node` is driven directly (NOT the full
   council) with the per-lane state slice, a ChatClient pinned
   to the model under test, and a custom
   `_ToolCallCapture` recorder that records iteration count,
   prompt/completion tokens, and the ordered tool-call log.
4. After the lane completes, the bench:
   - Extracts the final assistant text from the lane's
     `ToolResult.content`.
   - Counts `path:line`-style citations.
   - Runs the per-question oracle pytest via
     `run_pytest_against_sandbox(extra_env={...})` with three
     env vars set: `TOOL_EXEC_OUTPUT` (the final text),
     `TOOL_EXEC_CALLS` (the tool-call log as JSON), and
     `TOOL_EXEC_FIXTURE_DIR` (absolute path to the cohort).
   - Optionally calls the **judge LLM** with a strict 1-5
     rubric (`Score: <n>` + a one-sentence rationale).
5. Trial result is appended to `trials.jsonl` immediately so
   a Ctrl-C mid-run loses at most the in-progress trial.

**Per-model aggregation** (computed by `render_report`):

- `pass_rate` = #trials where the oracle passed / total trials
- `avg_quality_score` across trials with a recorded judge score
- `avg_tool_calls` (per trial) — the cost-per-answer signal
- `avg_wall_s` (per trial) — operator-facing latency

**Rubric** (pinned in SUITE.md):

> A model **qualifies for the tool_executor role default** iff
> `pass_rate ≥ 0.70` AND `avg_quality_score ≥ 3.5` — identical
> thresholds to the coder rubric so an operator who knows one
> knows both.
>
> Among qualifying models, the **recommended default** is the
> one with the highest `pass_rate`. Ties break on
> `median_tokens` (cheaper wins). If no model qualifies, the
> role stays disabled-by-default and
> `RECOMMENDED_DEFAULT_ON` in `tool_executor_defaults.py`
> remains `False`.

**Two-part gate for flipping the default-on bit** (the
**separate decision** of whether `DEFAULT_ENABLED_BY_ROLE
["tool_executor"]` flips from `False` to `True`):

1. The bench winner passes the rubric above.
2. Task #103 (x-tier proper composition) is resolved — EITHER
   the engine refactor lands (Option 2: per-lane
   `awaiting_tool_results` dict + post-barrier merge router +
   per-lane round filtering) so the role composes correctly
   under multi-model researcher fanout, OR the role is
   explicitly documented as base-tier-only (Option 1: doc
   deferral) and runtime gates apply.

Until both conditions clear, the role stays opt-in. M11c-2
populates `RECOMMENDED_TOOL_EXECUTOR_MODEL` from measured data
and decides condition 1; condition 2 is the user-facing
decision flow presented after M11c-2 data lands.

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
    --models kimi-k2.6:cloud,qwen3-coder-next:cloud,glm-5.1:cloud,gemma4:31b-cloud \
    --ollama-base http://192.168.178.2:11433 \
    --judge-model kimi-k2.6:cloud \
    --commit-report
```

The summary line at run start declares the estimated token cost.
`--accept-cost` is mandatory with `--live`; without it the script
prints the estimate and exits 2.

`--commit-report` (added 2026-05-16): after the run, `git add -f`
the rendered `report.md` + `metadata.json` (+ `quota.md` if
present) so they're staged alongside the baselines.md row in the
next commit. Raw `trials.jsonl` + per-trial sandboxes stay
gitignored (recreatable from a re-run). The flag does **not**
create a commit — the operator decides when to write history.

For the **multi-language** sub-protocol the only change is the
`--questions-dir`:

```bash
python benchmarks/consultants/coder_bench.py --live --accept-cost \
    --questions-dir benchmarks/consultants/questions/coder_mlang \
    --models glm-5.1:cloud,kimi-k2.6:cloud,deepseek-v4-flash:cloud,deepseek-v4-pro:cloud,minimax-m2.7:cloud \
    --ollama-base http://192.168.178.2:11433 \
    --judge-model kimi-k2.6:cloud \
    --commit-report
```

For the **tool_executor** sub-protocol the CLI is `skill-eval
tool_executor`. Dry-run + live commands mirror the coder shape:

```bash
# Dry-run smoke against the in-repo fixtures
claude-consultants skill-eval tool_executor --dry-run --smoke

# Full live run (M11c-2 step)
claude-consultants skill-eval tool_executor --live --accept-cost \
    --models glm-5.1:cloud,kimi-k2.6:cloud,gemma4:31b-cloud,qwen3-coder-next:cloud,deepseek-v4-pro:cloud,gemini-3-flash-preview:cloud \
    --judge-model gemma4:31b-cloud
```

The tool_executor bench has no `--commit-report` flag yet (the
M11c-2 closeout commits artifacts manually); the rest of the
surface matches the coder/stall sub-protocols.

### 3. Render the report

`coder_bench.py` now renders `report.md` automatically at the
end of every run, so step 3 is implicit. The manual command is
still supported:

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
| 2026-05-16 | 1.0 | glm-5.1:cloud | 100% | 4.88 | 1841 | 4.9s | 9aa6eaf0 | ... |
```

The baselines ledger is the running record across runs; future
sessions consult it to see the trend per model + suite version.

When `--commit-report` was passed to the live run, `report.md` +
`quota.md` + `metadata.json` are already staged. Just `git add
docs/consultants-skill-eval-baselines.md` (and the per-suite
defaults file if a new winner is being adopted) and commit.

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
| `benchmarks/consultants/harness.py` | Shared question loader, trial schema (`CoderTrial` / `StallTrial` / `ToolExecTrial`), oracle grader, judge helper, cost estimators |
| `benchmarks/consultants/coder_bench.py` | M11b runner CLI |
| `benchmarks/consultants/stall_bench.py` | M11a runner CLI |
| `benchmarks/consultants/tool_executor_bench.py` | M11c runner CLI |
| `benchmarks/consultants/analyze.py` | Markdown report renderer + rubric applier |
| `benchmarks/consultants/questions/coder/SUITE.md` | Coder suite v1.0 manifest + rubric |
| `benchmarks/consultants/questions/stall/SUITE.md` | Stall suite v1.0 manifest + rubric |
| `benchmarks/consultants/questions/tool_executor/SUITE.md` | Tool_executor suite v1.0 manifest + rubric |
| `benchmarks/consultants/questions/tool_executor/fixtures/<name>/` | Synthetic fixture cohort per question (Python + Markdown) |
| `benchmarks/consultants/questions/<suite>/<id>.md` | Per-question task description (model-facing) |
| `benchmarks/consultants/questions/<suite>/<id>-oracle.py` | Per-question pytest oracle (harness-facing) |
| `benchmarks/consultants/results/<date>/<suite>/{trials.jsonl,metadata.json,report.md}` | Per-run outputs |
| `consultants/engine/coder_defaults.py` | M11b winner + per-language routes |
| `consultants/engine/stall_defaults.py` | M11a per-model `(stall_threshold_s, hard_cap_s)` table |
| `consultants/engine/tool_executor_defaults.py` | M11c winner + default-on bit (scaffold in M11c-1; populated by M11c-2) |
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
