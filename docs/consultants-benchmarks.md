# `/consultants` benchmark queries

Three locked-in queries used to evaluate the council pipeline and
compare cloud models. **Do not edit the query text** in this file
without re-baselining everyone — the whole point is reproducibility.

These run via `scripts/consultants_benchmark.sh <label>` which:
1. Reads the canonical query texts from this file (search for the
   `<!-- BENCH-Q: ... -->` anchors below).
2. Sets every role's model to the value the script was invoked with
   (or leaves the existing config alone with `--no-set-model`).
3. Issues `claude-consultants consult` for each query.
4. Saves answer + structured event log to `docs/benchmarks/<label>/`.
   Each query lands `<slug>.summary.md`, `<slug>.transcript.md`,
   `<slug>.metadata.json`, `<slug>.transcript.db` (SQLite, v1.1),
   and `<slug>.waterfall.txt`. The `.db` files contain full LLM
   payloads (system prompts, tool results) — committing benchmark
   labels publishes those, which is fine for internal audit data
   but worth flagging if the host-level repo ever goes public.
5. Writes `docs/benchmarks/<label>/results.md` with wall times,
   token totals, and pass/fail-on-quality verdicts.

## Q1 — Smoke (`effort=medium`)

Cheapest end-to-end exercise of all four roles. Used to:

- Detect catastrophic regressions (engine down, all roles failing).
- Measure per-role wall floor on a model — how slow is the model
  before any real work happens?
- Validate the empty-output fallback (small prompts often hit cap).

**The query is code-only.** It must be answerable from the frozen
worktree alone, with no dependence on live filesystem state
(running services, recent session files, etc.). The original
liveness-check smoke gave different answers depending on whether
the bench ran from the live repo or a worktree (different ambient
filesystem state), which made it useless as a reproducibility
anchor — fixed in the 2026-05-07 baseline by re-framing as
code-only.

Pass criteria: completes; answer names all four roles
(`planner`, `researcher`, `critic`, `synthesizer`); ≤ 3
sentences; no hedging.

<!-- BENCH-Q: smoke -->
```text
From the code in this repository, name the four roles of the consultants council in a single short sentence.
```

Baseline expectation (kimi-k2.6:cloud, 2026-05-07): ~200-400 s
wall depending on cloud variability.

## Q2 — Medium audit (`effort=medium`)

A real codebase audit that requires multi-file grep + read +
synthesis. The ground truth exists; we manually verified it. The
council should produce a list of `path:line` entries with
exercisability verdicts.

Pass criteria: completes; answer cites at least three of
`claude_hooks/providers/pgvector.py:123`,
`claude_hooks/providers/pgvector.py:328`,
`scripts/migrate_to_pgvector.py:624`,
`scripts/bench_recall.py:107`; distinguishes protected vs
unprotected sites.

<!-- BENCH-Q: audit-medium -->
```text
Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python.
```

Baseline expectation (kimi-k2.6:cloud, 2026-05-07, post day-1
optimizations): ~500 s wall on a 4-min-class model; answer should
list 4-10 file:line entries.

## Q3 — High-effort frontier (`effort=high`)

A failure-mode reasoning task. Cannot be answered by grep alone;
requires the model to (a) understand LangGraph's `Send` semantics,
(b) read the additive reducers in `CouncilState`, (c) trace the
runner's stream loop, (d) reason about a hypothetical exception
that the existing test suite doesn't exercise. Frontier models
should be able to combine the three; mid-tier models will hand-wave
the part they can't grep their way to.

Pass criteria:
- Names the specific exception path (the runner's `try/except` in
  the `for mode, payload in compiled.stream(...)` loop).
- Notes that LangGraph's parallel-branch exception propagation
  cancels sibling lanes.
- Identifies that `final_state` accumulated up to that point gets
  written to artifacts via `_write_failed_artifacts` OR the partial
  is preserved.
- Recommends ONE concrete hardening change with `path:line`.

<!-- BENCH-Q: audit-high -->
```text
In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:

1. CouncilState's additive reducers (consultants/engine/graph.py)
2. consultants/server/runner.py's compiled.stream loop
3. The synthesizer's input messages — what does it actually see?
4. The storage.py artifact writer

Answer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line.
```

Baseline expectation (kimi-k2.6:cloud, 2026-05-07, effort=high
with dedicated critic): ~10-15 min wall, answer should cite at
least 6 distinct path:line locations and produce one concrete
recommendation.

## Adding a new benchmark query

Open a PR. Don't edit the existing three. Add a new section
`## Q4 — ...` with its own `<!-- BENCH-Q: <slug> -->` anchor and
its own pass criteria. Re-run the baseline and update the table
at `docs/benchmarks/index.md`.

The point of this file is to be **append-only**: queries already
locked in are reproducibility anchors across model sweeps. Bumping
their text invalidates every prior measurement.
