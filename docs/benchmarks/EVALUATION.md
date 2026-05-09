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

- `cost_proxy = prompt_tok * 1.0 + completion_tok * 4.0` — a
  unit-free proxy for cloud cost (Anthropic-style 1:4 ratio; only
  useful for cross-model relative ordering, not absolute USD).
- `tokens_per_second = (prompt_tok + completion_tok) / wall_s` —
  rough throughput; inflated by reasoning tokens.

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
| **A** | All 4 claims present and correctly cited with `path:line`; recommendation includes a code-shaped change (not just prose) |
| **B** | 3 of 4 claims correct; recommendation cites a real file but is vague about the change |
| **C** | 1-2 of 4 claims correct; recommendation is generic ("add try/except") with no path:line |
| **F** | Status `failed`, OR no recommendation, OR claims hand-waved without citations, OR fabricated `path:line` references |

A grade of **A** on Q3 is the bar a frontier model should hit. Mid-tier
models will typically land at **B** or **C**.

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
- If no model gets A on a role, use the highest-grading model
  available; if multiple tie, prefer the one with lower wall.

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

- Cost-per-run in actual currency once proxy logging is reliable
  enough to derive (requires hooking into the caliber proxy's
  rollup DB).
- A/B significance test (Mann-Whitney U) when comparing labels
  with N ≥ 3 runs. Skipped for now because the small N makes any
  test underpowered; trust the median/MAD eyeball.
- Automated grading of Q3 via a frontier-model judge. Currently
  manual to avoid model-judges-model echo chambers.

---

**Protocol version:** 1.1 (2026-05-09)
**Authoritative file:** `docs/benchmarks/EVALUATION.md`
**Last reviewed:** 2026-05-09

### Changelog

- **1.1 (2026-05-09)** — Q3 claim #3 corrected to reflect current code:
  the synthesizer **does** see the failure tombstone via the additive
  `research` reducer + `build_synthesizer_messages`, not the inverse.
  The pre-fix wording invalidated Q3 grades for the 2026-05-09 cloud
  sweep where every strong model correctly read the tombstone-visibility
  fix at `consultants/engine/council.py:555-563`. No regrading of pre-1.1
  labels: Q3 was not the dominant signal in the 2026-05-07 sweep
  (only kimi/glm-5.1 hit A and their answers fit either reading).
- **1.0 (2026-05-07)** — initial protocol.
