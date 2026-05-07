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

### Q1 (smoke)

| Grade | Criterion |
|---|---|
| **PASS** | Status `completed`, answer ≤ 3 sentences, contains "live" / "active" / "running" / "active session", no hedging language ("I cannot", "insufficient", "unable") |
| **WEAK** | Status `completed`, answer is correct but verbose (>3 sentences) or buried under preamble |
| **FAIL** | Status `failed`, OR answer claims insufficient information, OR contains hallucinated session id / file path |

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
   returns a partial-state dict (so the stream does NOT abort).
3. Names that the synthesizer's input messages do NOT include the
   error / failure tombstone (so it produces a degraded answer
   over visible lane reports).
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

When comparing labels, the following must match across all
labels in the comparison:

- **Repo HEAD** — record `git rev-parse HEAD` for each run; if it
  differs across labels, re-run the older label at the newer HEAD.
  Engine code changes invalidate prior measurements.
- **Effort tiers** — same effort per query across labels (the
  canonical assignment is in `consultants-benchmarks.md`; don't
  override at the runner unless you're testing effort itself).
- **Service config** — `consultants-config.toml` snapshot saved
  to `<label>/config.toml` at run time (the runner does this if
  `CONSULTANTS_CAPTURE_CONFIG=1`).
- **Network path** — same proxy URL (default `192.168.178.2:11433`).
  If the proxy changes, treat as a new label suffix.
- **Time of day** — cloud-model latency varies by US business
  hours. Run all labels in the same UTC window (e.g. all between
  20:00–06:00 UTC, or all between 14:00–18:00 UTC). Note the
  window in the label or `results.md` header.

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
this section manually:

```markdown
## Grades

| Query | Grade | Notes |
|---|---|---|
| smoke        | PASS / WEAK / FAIL | one-line note |
| audit-medium | A / B / C / F      | one-line note |
| audit-high   | A / B / C / F      | one-line note |

## Verdict

PROD-READY / EVALUATED-ONLY / UNSTABLE

## One-paragraph commentary

What surprised you. Where this model wins / loses vs prior labels.
Whether you'd put it in production for any role.
```

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

**Protocol version:** 1.0 (2026-05-07)
**Authoritative file:** `docs/benchmarks/EVALUATION.md`
**Last reviewed:** 2026-05-07
