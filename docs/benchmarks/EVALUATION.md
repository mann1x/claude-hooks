# `/consultants` evaluation protocol

This document defines **how** to run and grade benchmark sweeps so
two sessions on different days produce comparable numbers. If you
change anything in this file, bump the version stamp at the bottom
and re-baseline existing labels — the protocol is the contract.

> Read this end-to-end before kicking off a sweep. Cloud LLM calls
> cost real money and the numbers are only useful when the protocol
> matches across sweeps.

---

## 1. Purpose & scope

We compare:

- **Cloud models** as drop-in role models
  (`kimi-k2.6:cloud`, `deepseek-v4-pro:cloud`, `qwen3.5:cloud`, …)
- **Engine configurations** (effort tier defaults, fan-out cap,
  iter budgets, prompt variants)

We do NOT compare:

- Local models against cloud models (latency profile too different;
  use a separate suite for those).
- Prompts authored by the user mid-session (no ground truth).

The canonical query set is in
[`../consultants-benchmarks.md`](../consultants-benchmarks.md). Do
not edit those queries when running a sweep — they are the
reproducibility anchors.

---

## 2. KPIs (raw measurements, per run)

Pulled directly from the trace JSONL + status files. Recorded
automatically by `scripts/consultants_benchmark.sh`.

| KPI | Source | Unit | Notes |
|---|---|---|---|
| `wall_s` | `<slug>.status` | seconds | End-to-end wall, CLI POST → terminal status |
| `prompt_tok` | `<slug>.metadata.json::total_prompt_tokens` | tokens | Sum across all roles + lanes |
| `completion_tok` | `<slug>.metadata.json::total_completion_tokens` | tokens | Includes reasoning tokens for thinking models |
| `llm_calls` | trace JSONL `event=llm_call` count | int | Useful for retry diagnosis |
| `tool_calls` | trace JSONL `event=tool_call` count | int | Sanity-check researcher behavior |
| `tools_wall_ms` | trace JSONL sum of `tool_call.duration_ms` | ms | Floor on filesystem cost |
| `synthesizer_wall_ms` | trace JSONL sum of synthesizer `node_exit.duration_ms` | ms | Often the dominant role-cost |
| `retries` | metadata.json::retries_by_role | dict | Cloud flap indicator |
| `effort` | `<slug>.status` | enum | low / medium / high / max |
| `status` | `<slug>.status` | enum | completed / failed |

Derived KPIs:

- **`cost_usd`** — dollars, per query and per run, priced **per role**:
  each turn's `prompt_tokens` / `completion_tokens` from
  `<slug>.metadata.json::turns`, times the price of the model that role
  ran on, at the time the query ran (peak or off-peak). See §2.1.
- `tokens_per_second = (prompt_tok + completion_tok) / wall_s` —
  rough throughput; inflated by reasoning tokens.

(`cost_proxy = prompt_tok + 4 × completion_tok`, the unit-free proxy
used through v1.2, is retired: Ollama now bills each model at its own
input/output price, and the 1:4 ratio it assumed ranges from 1:3 to
1:40 across the models we run.)

### 2.1 Cost — what a run costs in dollars

The Ollama account is a monthly **dollar budget**, and every call draws
on it at the model's own per-token price (Appendix A). Cost is therefore
a first-class comparison axis beside quality and wall time, and it
follows three rules:

1. **Every LLM call is counted, by role.** The planner, every researcher
   lane, the critic, the synthesizer, and — in the skill-eval benches —
   every judge, rejudge and retry. A figure that leaves a role out is
   not a cost. The council metadata records every turn; the skill-eval
   trials record `usage` by role (`harness.record_usage`).
2. **Priced at a dated snapshot.** `benchmarks/consultants/pricing.py`
   holds the table, its source and its date. A report states the
   snapshot it used, so a later price change never silently rewrites an
   old comparison.
3. **Upper bound.** Prompt tokens are priced uncached, because the traces
   do not record cache hits.

`scripts/bench_costs.py` prices every recorded run, council and
skill-eval alike; the result is committed at
[`costs.md`](costs.md). When choosing between models of equal grade,
**cost decides before wall time** (see §3.5 "Composing the role
grades").

Off-peak pricing applies outside 12:00–18:00 UTC on weekdays and all
weekend, and currently halves the deepseek models only. Schedule sweeps
that lean on them off-peak and record the window (§6.4).

### 2.2 The skill-eval judge

A coder_bench quality score is only as good as its judge, and a judge
is only trustworthy once it has been checked against the one thing
that is not an opinion: whether the code passes its tests.
`benchmarks/consultants/judge_eval.py` does that. It measures a
candidate's separation (AUC of its score against *tests pass*),
repeat agreement, self-bias, style affinity, speed and cost on solutions
whose correctness is already known. Findings:
[`judge-and-sampling.md`](judge-and-sampling.md).

1. **Default judge (2026-09-23): the panel.** glm-5.3-flash and
   deepseek-v4.1-flash score every compiled trial independently on the
   same blind rubric. When both return a score, deepseek-v4.1-flash
   settles it: it verifies each review's claim against the code, sees
   the reviews as A/B in a hashed order, and never sees model names
   (`judge_panel.py`). One score alone stands; a failed synthesizer
   leaves an agreed score, or the mean, flagged.
2. **kimi-k2.6 remains the reference** for comparisons with runs judged
   before 2026-09-23. A run that is compared with an older one names the
   older run's judge (`--judge-model kimi-k2.6:cloud`). Scores from
   different judges are never compared directly: calibrate on shared
   models (`scripts/bench_ladders.py`) or re-judge.
3. **A new judge is admitted by `judge_eval`**, not by reputation: its
   AUC interval must overlap the reference's, and its self-bias must be
   reported.

### 2.3 Sampling is part of the subject

A cloud model runs at the provider default unless the request says
otherwise, and the default is not always one the model does well at.
The sampling a run used is therefore recorded with the run:

- Templates travel in the request, from `config/model-sampling.json`
  plus user overrides (`claude_hooks/model_sampling.py`), never from an
  Ollama Modelfile overlay.
- A judge's label carries its sampling
  (`glm-5.3-flash:cloud@temperature=0.7`), and a coder arm's directory
  carries a `sampling.json`. Results under different sampling never
  pool.
- A sampling comparison changes only the subject's sampling. The judge
  must be one without a template, or be pinned with `--sampling none`.
- A shipped template changes the model in **every** role. Measure it in
  each role the model is routed to before shipping it.

### 2.4 Outages and repair

A network outage does not invalidate a run, but it does leave holes:
judge calls that returned no score. Two rules keep them from becoming
results.

1. **Wait it out.** Benchmark clients use `harness.bench_client`, which
   waits out a dead line for up to 30 min
   (`ChatClient.outage_wait_s`) instead of failing within seconds.
2. **Repair, never re-run.** Every benchmark chain ends with
   `benchmarks/consultants/repair.py`. It re-asks exactly the verdicts
   and coder judgments left without a score, using the same judge,
   sampling and prompt, append-only, over a few rounds. A trial scored
   after the run is marked `quality_filled`. A run with holes left is
   not reported as complete.

---

## 3. Quality criteria — per query

### Q1 (smoke — code-only as of 2026-05-07 baseline)

The smoke asks the council to name the four roles of the
consultants council from the codebase. Ground truth: `planner`,
`researcher`, `critic`, `synthesizer`.

| Grade | Criterion |
|---|---|
| **PASS** | Status `completed`, answer names all four roles, ≤ 3 sentences, no hedging language |
| **WEAK** | Status `completed`, answer names 3 of 4 roles, OR is verbose (> 3 sentences) but otherwise correct |
| **FAIL** | Status `failed`, OR fewer than 3 roles named, OR fabricates roles ("synthesizer / critic / orchestrator / executor"), OR refuses to answer |

### Q2 (audit-medium, psycopg ground truth)

Ground-truth set of unprotected sites (manually verified
2026-05-07): the model gets credit for citing each one with a
correct exercisability verdict.

```
claude_hooks/providers/pgvector.py:123          -- exercisable
claude_hooks/providers/pgvector.py:328          -- exercisable
scripts/migrate_to_pgvector.py:624              -- exercisable
scripts/bench_recall.py:107                     -- exercisable
tests/test_pgvector_integration.py:56           -- exercisable from pytest
tests/test_pgvector_integration.py:285          -- exercisable from pytest
```

Optional credit for distinguishing protected sites in `install.py`
(2092, 2127, 2524, 2546, 2647, 2666 are EITHER protected by the
`4e67dc2` subprocess fallback OR live inside subprocess inline
script strings — a careful answer flags them as not-a-bug).

| Grade | Criterion |
|---|---|
| **A** | Cites ≥ 5 of the 6 ground-truth sites with correct verdicts; correctly flags or omits the protected `install.py` sites |
| **B** | Cites 3-4 of 6 ground-truth sites with correct verdicts; OR cites all 6 but mis-classifies ≤ 2 protected `install.py` sites as bugs |
| **C** | Cites ≤ 2 ground-truth sites; OR fabricates non-existent file paths / line numbers |
| **F** | Status `failed`, OR claims insufficient information (silent failure in old pipeline), OR answer is a refusal |

### Q3 (audit-high, frontier reasoning)

No grep-able ground truth. Graded against four required claims.

1. Names that `error` / `_role_failed` are NOT reducer-augmented
   in `CouncilState` (i.e. last-write-wins behavior).
2. Names that `researcher_node` swallows exceptions internally and
   returns a tombstone state update (`error`, `_role_failed`, plus
   a placeholder string appended to `research`) so the stream does
   NOT abort.
3. Names that the synthesizer **does** see the failure tombstone —
   `research` uses an additive `operator.add` reducer
   (`consultants/engine/graph.py`), so the placeholder string from
   a failed lane lands in the merged `research` list, and
   `build_synthesizer_messages` at `consultants/engine/council.py:245+`
   embeds every entry of `research` in the user prompt as a
   "RESEARCHER REPORT (round N): …". The synthesizer therefore
   produces a degraded answer that is aware of the lane failure
   text — but it does NOT see the `state.error` / `state._role_failed`
   fields directly. (This is by design — the in-code comment at
   `council.py:555-563` notes the tombstone-visibility was the fix
   to a previous audit-high finding where crashed lanes silently
   contributed nothing and the synthesizer wrote a confidently-
   degraded answer over the surviving lanes.)
4. Recommends exactly ONE hardening change with concrete
   `path:line` (not a vague "add error handling").

| Grade | Criterion |
|---|---|
| **A** | All 4 claims present and correctly cited with `path:line`; recommendation is code-shaped (not just prose) AND **passes the actionability sub-rubric below** |
| **B** | 3 of 4 claims correct; recommendation cites a real file but is vague, OR recommendation is concrete but only partially addresses the failure mode (per actionability sub-rubric) |
| **C** | 1-2 of 4 claims correct, OR recommendation is generic ("add try/except") with no `path:line`, OR recommendation compiles but is REDUNDANT / off-topic / a contentious UX choice rather than a bug fix |
| **F** | Status `failed`, OR no recommendation, OR claims hand-waved without citations, OR fabricated `path:line` references, OR recommendation references a parameter/symbol that does not exist (e.g. fabricated function args), OR recommendation is REGRESSIVE (would revert a deliberate prior fix that an in-code comment documents as the cure for a previous audit-high finding) |

A grade of **A** on Q3 is the bar a frontier model should hit. Mid-tier
models will typically land at **B** or **C**.

#### Actionability sub-rubric (added v1.2)

The shape-only Q3 grade (v1.0/v1.1) gave A to recommendations that were
redundant, off-topic, or regressive. The 2026-05-09 audit at
`docs/benchmarks/Q3-actionability-audit-2026-05-09.md` re-graded every
Q3 across both prior sweeps and reshuffled the leaderboard.

To be A-graded a Q3 recommendation must satisfy BOTH:

1. **Compiles cleanly against live code.** The suggested edit applies to
   the current `consultants/engine/*.py` and `consultants/server/runner.py`
   without invoking a fabricated function/parameter. Stale `path:line`
   references are tolerated if the *intent* is unambiguous.

2. **Addresses the failure mode the answer described.** The Q3 question
   asks the model to trace a specific failure path and propose a fix.
   A recommendation that compiles but fixes a different bug fails this
   dimension.

Common patterns that DO NOT pass the sub-rubric:

- **Redundant** — proposes a guard that already exists at another layer
  (e.g. wrapping a function whose body already has the same try/except).
- **Regressive** — proposes reverting a deliberate prior fix. Look for
  in-code comments that document the current behavior as the response to
  a previous audit-high finding; the model probably didn't read them.
- **Off-topic** — picks a different reducer / state field / function to
  modify than the one whose behavior the answer just described.
- **Contentious UX flip** — proposes changing the meaning of a
  user-facing field (e.g. flipping `status=failed` semantics). The
  change is technically correct but reshapes the contract for every
  downstream consumer; it's a design discussion, not a bug fix. These
  drop to C+ even when shape-A.

---

## 3.5 Per-role quality grading (the key to building a model mix)

The per-query grade tells you whether the council answered the
question. The per-role grade tells you which role each model is
GOOD at — that's what lets us compose heterogeneous configs like
"cheap fast model as planner + frontier as synthesizer".

For each label, after running the three queries, read each role's
output across all three transcripts and assign **one role grade
per role** (A / B / C / F). The grader is **Claude (the LLM
driving the consultation work)** reading the on-disk transcripts.
This is the natural fit for a `/consultants`-style evaluation:
the conversation is LLM-to-LLM throughout, including the grading.
The human operator's job is the lighter one — verify model
selection in real-world skill usage on whichever model gets
picked, not grade transcripts.

Record the grade plus a one-sentence justification in
`results.md` § Per-role grades.

### Planner (input: question; output: numbered plan)

Read the planner output in each `<query>.transcript.md`'s
`## Planner` section.

| Grade | Criterion |
|---|---|
| **A** | 3-7 numbered items; each one cites a concrete file/path/symbol target OR a specific verification step ("verify that X handles Y"); no items of the form "understand the code" or "look at how X works" |
| **B** | Right item count; some items concrete but 1-2 are vague ("review the architecture") |
| **C** | Wrong item count (1, or > 10); items are mostly vague; researcher would not be able to execute against this plan |
| **F** | Plan is missing entirely (planner crashed → tombstone); OR plan is a single paragraph with no structure |

### Researcher (input: plan + tools; output: per-lane reports)

Read every `## Researcher (round N)` section.

| Grade | Criterion |
|---|---|
| **A** | Every claim cites `path:line`; tool calls are batched (multiple in one assistant turn whenever possible); coverage hits the plan items; no hallucinated paths |
| **B** | Most claims cited but some bare prose; mostly batched tool calls but some sequential; covers most plan items |
| **C** | Few `path:line` citations; sequential single-tool calls dominate; misses plan items |
| **F** | Empty output (the fallback fired); OR fabricated `path:line` references; OR claims findings without any tool calls run |

### Critic (only at `effort=high`; input: plan + research; output: DECISION line)

Read each `## Critic` section.

| Grade | Criterion |
|---|---|
| **A** | First line is `DECISION: ready` or `DECISION: needs_more_research`; reasoning is concise (≤ 5 lines); when re-routing, names 1-3 specific gaps with `path:line` |
| **B** | Decision line present and parseable; reasoning is sound but verbose; gaps named without `path:line` |
| **C** | Decision line missing or non-parseable (parser falls back to "ready"); reasoning hedges or restates the question |
| **F** | Crashed (tombstone fired); OR routed to needs_more_research with no gap content |

(For non-high efforts the critic isn't fired; mark "n/a".)

### Synthesizer (input: plan + research [+ critique at high]; output: final answer)

Read each `<query>.summary.md` body.

| Grade | Criterion |
|---|---|
| **A** | Lead sentence is the bottom line (no preamble); evidence-grounded with `path:line` citations; output shape matches question (list-shaped → bullets, yes/no → one sentence + justification); no hedging when evidence is concrete; explicit hedging when evidence is sparse |
| **B** | Bottom line within first paragraph; mostly cited; output shape mostly right but some unnecessary preamble or markdown ceremony |
| **C** | Bottom line buried below preamble; few `path:line` cites; output shape mismatched (paragraphs for a list question) |
| **F** | Tombstone-only ("consultation incomplete: synthesizer error"); OR fabricates `path:line` cites; OR refuses to answer despite available evidence |

### Composing the role grades

In a heterogeneous config (different model per role), each role's
grade is the mode of that role's per-query grades — same
aggregation as §3 query grades. A label's "model mix verdict" is:

```
P:<grade> R:<grade> C:<grade> S:<grade>
```

e.g. `P:B R:A C:A S:A` means kimi is excellent at researcher /
critic / synthesizer but fine-but-not-great at planner — a
candidate for a mix where the planner runs on a cheaper model.

To find a viable mix, scan the cross-label `index.md`'s per-role
columns:

- **Cheapest A grade per role** → use that model for that role.
  "Cheapest" means **dollars per fire** for that role (§2.1), not
  tokens: a model that uses 2× the tokens at a fifth of the price is
  the cheaper one.
- If no model gets A on a role, use the highest-grading model
  available; if multiple tie, prefer the one with lower cost, then
  lower wall.

A composed mix should be re-baselined as its own label
(`mix-2026-05-XX-PA-RA-CA-SA`) before being declared usable.

---

## 4. Pass/fail thresholds for a label

A label is **PROD-READY** when:

- All 3 queries `status=completed`
- Q1 grade = PASS
- Q2 grade ≥ B
- Q3 grade ≥ B
- No query exceeds 25 minutes wall (operational ceiling — anything
  longer is unusable in interactive workflows even if correct)
- No retries-by-role entry exceeds 5 (sustained cloud instability
  invalidates the run)

A label is **EVALUATED-ONLY** otherwise — the numbers go in the
table but the model is flagged unsuitable for the role(s) where it
underperformed.

---

## 5. Multi-run requirement

Cloud is flaky. A single run is **not** a measurement. Required:

- **N = 3 runs minimum per label** for a publishable comparison.
- Runs spaced ≥ 10 minutes apart (cloud rate-limit cooldown +
  KV-cache invalidation between runs).
- Report **median wall** and **median absolute deviation (MAD)** —
  not mean. Cloud outliers are common and skew means heavily.
- Quality grade is the **mode** across runs (the most common
  grade); ties resolve to the WORSE grade.

Naming convention for repeated runs:
`<model>-<date>-r1`, `-r2`, `-r3` under
`docs/benchmarks/<model>-<date>/r{1,2,3}/`. The aggregate
`results.md` lives at `<model>-<date>/results.md`.

For exploratory or screening sweeps, single runs are OK but **must**
be labeled with `-screening` suffix and excluded from the
comparison table at `index.md`.

---

## 6. Comparison protocol — what to control

There are **three independent drift sources** every comparable run
has to pin. The runner captures all three automatically; the
verifier checks them before r2/r3.

### 6.1 Subject codebase (what the consultants audit)

Q2's ground-truth `path:line` set and Q3's reducer trace lines
both reference the actual claude-hooks repo. As the repo evolves
those lines move, breaking historical comparisons.

**Mechanism:** a git tag listed in
`docs/benchmarks/CURRENT_BASELINE`. The runner creates a worktree
at that tag and passes its path as `--cwd` to the consultants. The
worktree is auto-removed at the end. The current baseline is
`bench-baseline-2026-05-07`.

When to bump the baseline tag (and re-run every label against the
new tag): when the dev branch moves enough that Q2/Q3 stop being
useful audits — e.g. files renamed, the line numbers Q3 cites
shift more than ~50 lines, or new psycopg sites land that flip the
ground truth. Bumping is a deliberate act with full re-baseline
cost; treat it as a sweep-suite version bump.

`results.md` records `Subject baseline: <tag> (commit <sha>)` so
you can tell at a glance which tree the run audited.

**The audited tree must not contain the answers.** The baseline tag
is a snapshot of this repo, so it carries this file (the Q1 role list
and the Q2 ground-truth sites), every earlier label's transcripts and
answers under `docs/benchmarks/`, and `docs/consultants-benchmarks.md`,
which restates the Q2 sites. The runner deletes those paths from the
worktree before the first query and `results.md` records that it did
(`Answer key removed from the worktree: …`). Runs before 2026-09-23
audited a tree that still held them: on 2026-09-23 `glm-5.3` read the
key for Q1 and Q2 and `deepseek-v4.1-flash` found it, so an earlier
label's Q1/Q2 grade cannot rule out the same.

### 6.2 Engine code (the consultants engine itself)

The engine's prompts / caps / fan-out / reducers all influence
results from the same model. We pin this via the engine's git HEAD,
recorded in `results.md` as `Engine HEAD: <sha>`. If the engine
changes — even a one-character prompt tweak — every prior run is
invalidated.

When to re-baseline: any commit touching `consultants/`,
`claude_hooks/agent_loop/`, `claude_hooks/get_advice/chat_client.py`,
or `claude_hooks/caliber_proxy/` invalidates open sweeps. Either:
- finish the current sweep BEFORE merging the engine change, or
- re-run all open labels at the new engine HEAD.

### 6.3 Cloud model snapshot

The cloud upstream (`192.168.178.2:11433` proxy fronting Ollama
Cloud or similar) can rotate the snapshot behind a tag like
`kimi-k2.6:cloud` without notice — quantisation tweak,
fine-tune drop, etc. Tracked via `/api/show`'s `modified_at` +
`capabilities` + `parameter_size`.

**Mechanism:** at r1, the runner writes `<label>/models.json`
containing a `/api/show` snapshot for every distinct role model.
Before r2/r3, run:

```bash
scripts/consultants_model_snapshot.py verify <label-dir>
```

Exit codes:
- `0` — snapshot matches; safe to add the next run.
- `2` — drift detected. Per protocol, **re-execute every prior
  rN run at the current snapshot before adding the next one**.
- `3` — probe failed (network / proxy). Don't add a run until
  you can verify; the cloud may be in a partial state.

### 6.4 Other things to control

- **Effort tiers** — same effort per query across labels (the
  canonical assignment is in `consultants-benchmarks.md`; don't
  override at the runner unless you're testing effort itself).
- **Service config** — `claude-consultants config show` snapshot
  is embedded in `results.md` automatically.
- **Network path** — same proxy URL (default `192.168.178.2:11433`).
  If the proxy changes, treat as a new label suffix
  (`<existing>-via-<new-proxy-tag>`).
- **Time of day** — cloud-model latency varies by US business
  hours. Run all labels in the same UTC window (e.g. all between
  20:00–06:00 UTC, or all between 14:00–18:00 UTC). Note the
  window in `results.md`.

What you can vary:

- Model assignment per role (the point of model sweeps).
- Engine config knobs (the point of config sweeps) — but ONLY one
  knob at a time; otherwise you can't attribute the delta.

---

## 7. Cloud variability handling

Outlier classification is part of the protocol. Each run is
classified after-the-fact:

- **CLEAN** — `wall_s` within median ± 2 × MAD across the label's 3
  runs; `retries_by_role` total ≤ 2.
- **FLAP** — `retries_by_role` total ≥ 5 OR a single role hit `_role_failed`. Note in the run's row but include in the median.
- **OUTLIER** — `wall_s` > median + 5 × MAD. Re-run that single
  query and substitute. Do NOT silently drop; document the
  substitution in `results.md`.

If 2 of 3 runs are FLAP/OUTLIER, the label is **UNSTABLE** — flag it
and run an additional 3 runs. If still unstable, the model is
unsuitable for use in `/consultants` and the label is published
with that verdict.

---

## 8. Pre-run checklist

Before kicking off `consultants_benchmark.sh`:

- [ ] `git status` clean (or pending changes don't touch `consultants/`,
      `claude_hooks/agent_loop/`, or `claude_hooks/get_advice/chat_client.py`).
      If they do, commit or stash before running.
- [ ] `git rev-parse HEAD` recorded for the label.
- [ ] `systemctl --user is-active claude-hooks-consultants` reports
      `active`. If you just changed engine code, restart it:
      `systemctl --user restart claude-hooks-consultants`.
- [ ] `curl -s http://127.0.0.1:38095/v1/health` returns
      `{"status":"ok"}`.
- [ ] `claude-consultants config show` matches the intended config;
      diff against the previous label's `<label>/config.toml` if
      one exists.
- [ ] No other consultations running (`active_sessions` field in
      `/v1/health` should be 0). Concurrent consultations contend
      for cloud bandwidth and skew measurements.
- [ ] Network reachable: `curl -fsS http://192.168.178.2:11433/api/tags`
      returns 200. If the proxy is down, abort.
- [ ] Time window matches your prior labels (see §6).

---

## 9. Post-run checklist

After `consultants_benchmark.sh` completes:

- [ ] `results.md` written and the wall numbers are sane (no 0s).
- [ ] All three `<slug>.summary.md` exist and are non-empty.
- [ ] Skim the answers — does Q1 cite "live"/"active"? Does Q2 list
      `path:line` entries? Does Q3 cite reducers + reroute path?
- [ ] Grade each query manually using §3's rubric. Record grades
      in `results.md` under a `## Grades` section.
- [ ] If grades are **F** anywhere, classify as FLAP and decide
      whether to re-run that single query or call the label
      EVALUATED-ONLY.
- [ ] Append a one-line entry to `index.md` with the label, model,
      median wall (post-3-run aggregation), and grade summary.
- [ ] Commit the `<label>/` directory + `index.md` change in one
      commit titled `bench(consultants): <label>`.

---

## 10. Exclusion criteria — when to discard a run

A run is invalid (do NOT use its numbers) if:

- Engine restarted mid-run.
- Repo HEAD changed mid-run.
- The proxy returned 5xx for ≥ 50% of `/api/chat` calls in the
  trace (cloud was burning).
- Another `/consultants consult` was running concurrently from the
  same engine.
- Manual `Ctrl-C` mid-script.

Re-run from the START of the affected query (do NOT splice
partial results across runs).

---

## 11. Reporting template

The runner-generated `results.md` is raw data. After grading, append
these sections manually:

```markdown
## Per-query grades

| Query | Grade | Notes |
|---|---|---|
| smoke        | PASS / WEAK / FAIL | one-line note |
| audit-medium | A / B / C / F      | one-line note |
| audit-high   | A / B / C / F      | one-line note |

## Per-role grades

(See §3.5 for criteria; one grade per role aggregated across all
three queries. Critic grade is `n/a` unless audit-high ran.)

| Role | Grade | One-sentence justification |
|---|---|---|
| planner     | A / B / C / F      | …  |
| researcher  | A / B / C / F      | …  |
| critic      | A / B / C / F / n/a| …  |
| synthesizer | A / B / C / F      | …  |

Mix string: `P:<g> R:<g> C:<g> S:<g>`

## Verdict

PROD-READY / EVALUATED-ONLY / UNSTABLE

## One-paragraph commentary

What surprised you. Which role(s) this model wins at vs prior
labels, which role(s) it should NOT be used for. Whether you'd
build a heterogeneous mix around it.
```

The per-role grades are what makes a heterogeneous mix possible —
without them you only know "this label is OK overall", not "it's
great at researcher but mediocre at planner".

The commentary is the most useful artifact when a future session
picks the model — quick verdict scannable from `index.md`'s table
of pointers.

---

## 12. Future protocol extensions (not yet enforced)

- Cached-input pricing, once traces record cache hits (the current
  figures are uncached upper bounds).
- A/B significance test (Mann-Whitney U) when comparing labels
  with N ≥ 3 runs. Skipped for now because the small N makes any
  test underpowered; trust the median/MAD eyeball.
- Automated grading of Q3 via a frontier-model judge. Currently
  manual to avoid model-judges-model echo chambers.

---

## Appendix A — Price snapshot 2026-09-23

Read from <https://ollama.com/pricing> on **2026-09-23**; the same table
is `benchmarks/consultants/pricing.py`. US$ per million tokens.

| Model | Input | Cached input | Output | Off-peak input | Off-peak output |
|---|---|---|---|---|---|
| deepseek-v4.1-flash | 0.30 | 0.006 | 1.20 | 0.15 | 0.60 |
| deepseek-v4-flash | 0.44 | 0.014 | 1.32 | 0.22 | 0.66 |
| deepseek-v4-pro | 1.32 | 0.044 | 3.96 | 0.66 | 1.98 |
| gemma4 | 0.14 | 0.05 | 0.40 | — | — |
| glm-5.3 | 1.40 | 0.26 | 4.40 | — | — |
| glm-5.3-flash | 0.15 | 0.03 | 0.50 | — | — |
| glm-5.2 | 1.40 | 0.26 | 4.40 | — | — |
| glm-5.1 | 1.00 | 0.20 | 3.20 | — | — |
| gpt-oss:120b | 0.15 | 0.014 | 0.60 | — | — |
| gpt-oss:20b | 0.07 | 0.035 | 0.30 | — | — |
| kimi-k3 | 3.00 | 0.30 | 15.00 | — | — |
| kimi-k2.7-code | 0.95 | 0.19 | 4.00 | — | — |
| kimi-k2.6 | 0.95 | 0.16 | 4.00 | — | — |
| minimax-m3 | 0.60 | 0.12 | 2.40 | — | — |
| minimax-m2.7 | 0.30 | 0.06 | 1.20 | — | — |
| mistral-large-3 | 0.50 | — | 1.50 | — | — |
| nemotron-3-nano | 0.06 | — | 0.24 | — | — |
| nemotron-3-super | 0.015 | 0.015 | 0.60 | — | — |
| nemotron-3-ultra | 0.10 | 0.10 | 3.00 | — | — |
| qwen3.5:397b | 0.60 | — | 3.60 | — | — |

Not on the page, and therefore **unpriced** in every report:
`gemini-3-flash-preview`, `qwen3.5` (bare), `qwen3-coder-next`.

Off-peak: outside 12:00–18:00 UTC on weekdays, and all weekend.
Plans: Free ($0 + starter credits, 1 concurrent), Pro ($60/month, 3
concurrent), Max ($300/month, 10 concurrent), Team ($1,000/month shared,
10 concurrent). Unused monthly credit does not carry forward; overage
draws on purchased balance.

When the prices change, add a new appendix with its date and keep this
one: costs already published were computed against it.

---

**Protocol version:** 1.5 (2026-09-24)
**Authoritative file:** `docs/benchmarks/EVALUATION.md`
**Last reviewed:** 2026-09-24

### Changelog

- **1.5 (2026-09-24)** — Three rules for the skill-eval benches. The
  judge is checked against tests passing, and coder_bench's default
  becomes the glm-5.3-flash + deepseek-v4.1-flash panel; kimi-k2.6
  stays the reference for comparisons with older runs (§2.2). Sampling
  is recorded as part of the subject and never pools across settings
  (§2.3). Outages are waited out, and every chain ends with
  `repair.py` (§2.4). Council grading is unchanged, and no label needs
  re-grading.

- **1.4 (2026-09-23)** — The answer key is removed from the audited
  worktree (§6.1): `docs/benchmarks/` and `docs/consultants-benchmarks.md`
  are deleted from it before the first query. Grading criteria are
  unchanged; Q1/Q2 grades from earlier runs carry the caveat that the
  key was reachable.

- **1.3 (2026-09-23)** — Cost in dollars becomes a first-class KPI
  (§2.1). Every LLM call is counted by role and priced per model at a
  dated snapshot (Appendix A, `benchmarks/consultants/pricing.py`);
  `cost_proxy` is retired. "Cheapest A per role" now means dollars per
  fire, and ties break on cost before wall. Grading criteria are
  unchanged, so no existing label needs re-grading; every prior run was
  re-priced at the 2026-09-23 snapshot in [`costs.md`](costs.md).

- **1.2 (2026-05-09)** — Q3 grading gains a correctness sub-rubric.
  The v1.0/v1.1 shape-only grade gave A to several recommendations
  that turn out to be redundant, regressive, off-topic, or
  contentious UX flips when checked against the live code. The new
  sub-rubric requires both (a) compiles cleanly against live code
  and (b) addresses the failure mode the answer described. See the
  audit at `docs/benchmarks/Q3-actionability-audit-2026-05-09.md`
  which re-grades every Q3 across both prior sweeps and reshuffles
  the leaderboard. Headline finding: only `gemma4:31b-cloud`
  consistently produces actionable Q3 recommendations across both
  sweeps and three runs at the v1.1.0 engine HEAD.
- **1.1 (2026-05-09)** — Q3 claim #3 corrected to reflect current code:
  the synthesizer **does** see the failure tombstone via the additive
  `research` reducer + `build_synthesizer_messages`, not the inverse.
  The pre-fix wording invalidated Q3 grades for the 2026-05-09 cloud
  sweep where every strong model correctly read the tombstone-visibility
  fix at `consultants/engine/council.py:555-563`. No regrading of pre-1.1
  labels: Q3 was not the dominant signal in the 2026-05-07 sweep
  (only kimi/glm-5.1 hit A and their answers fit either reading).
- **1.0 (2026-05-07)** — initial protocol.
