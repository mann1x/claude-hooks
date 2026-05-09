# `gemma4-31b-cloud` — caliber-eval summary (2026-05-09)

**Headline:** First cloud-Ollama caliber init that **runs end-to-end without
crashing, scores A grade, and beats the claude-cli baseline on wall clock.**
9-point quality gap remains vs claude-cli (85 vs 94), concentrated in the
"Project grounding" rubric category.

## Run setup

| Field | Value |
|---|---|
| Workspace | `/srv/dev-disk-by-label-opt/dev/caliber-eval/gemma4-31b-cloud/` |
| Source | `claude-hooks` @ commit `3d574c1` (2026-05-09 dev), pre-resilience-port |
| Caliber | `1.49.6` |
| Provider | `openai` (via grounding proxy on `127.0.0.1:38091`) |
| Model | `gemma4:31b-cloud` (planner + fast model) |
| Proxy CWD | `$DEST` (workspace, not live repo) |
| Proxy upstream | `http://192.168.178.2:11433/v1` (cloud auth) |
| HOME | per-bench fake-home (provider config isolated) |
| Wall clock | **28m 41s** (vs claude-cli 37m 14s — **23% faster**) |
| Caliber score | **85/100 (Grade A)** (vs claude-cli 94/100 Grade A) |

## Phase breakdown

| Phase | Wall | Notes |
|---|---|---|
| Detecting project stack | 3m 29s | ✓ Python, Bash, JSON, YAML, TOML — 5 languages, 4 frameworks, 1063 files |
| Generating configs      | 7m 29s | ✓ Mapping `caliber_proxy` |
| Generating skills       | 15m 2s | ✓ 2 project skills (vs baseline's 5) |
| Validating & refining   | 2m 40s | ✓ Refined; score 35 → 85 (+50 pts) |
| **Total**               | **28m 41s** | |

## Headline metrics — vs `claude-cli-baseline`

| Metric | claude-cli | gemma4:31b-cloud | Delta |
|---|---|---|---|
| Caliber score        | 94/100  | **85/100** | **−9** |
| Wall clock           | 37m 14s | **28m 41s** | **−23%** |
| Total skills         | 8       | 5          | −3 |
| Project skills       | 5       | 2          | −3 |
| `paths:` fm coverage | 5/5 (100%) | 2/2 (100%) | **parity in ratio** |
| `paths:` fm entries  | 25      | 4          | −21 |
| Bare file refs in skill bodies | 48 | 8 | −40 |
| `file:line` refs     | 0       | 0          | tie (rubric doesn't require) |
| Skill-body chars total | 56,031 | 18,316    | −37,715 (33% of baseline) |
| `CLAUDE.md` chars    | 9,109   | 6,104      | 67% of baseline |
| `CLAUDE.md` bare file refs | 54 | 25       | 46% of baseline |

## Token usage (from caliber's own report)

`gemma4:31b-cloud: 364,636 in / 11,870 out  (8 calls)`

For a 28m run that's an average of 47s per call inclusive of network + tool
loops. Way below the 5-minute claude-cli per-call ceiling.

## What gemma4:31b-cloud got right

1. **It ran end-to-end.** Past gemma-eval runs (gemma4-98e:tools v1..v5)
   either crashed at "Generating configs" with "Model produced no output
   for 12m4s" or produced 3-skill outputs with empty `paths:` frontmatter.
   This run completed all four caliber phases cleanly.
2. **`paths:` frontmatter coverage is at baseline parity (100%).** Both
   `consultants-management` and `setup-compile-aware` had populated
   `paths:` blocks (vs every past gemma run getting 0/3 or 0/N here).
   The gap on `paths:` count is purely "fewer skills produced", not
   "skills produced without paths".
3. **Score-refine loop worked.** 35 → 85 (+50 pts) is the largest
   refinement delta of any past gemma run. The loop converged in
   2m 40s — comparable to claude-cli's refinement phase.
4. **Wall clock beats claude-cli by 23%.** The cloud-tagged model is
   genuinely fast, and the proxy's tool dispatch + grounding was not
   the bottleneck.

## Where the 9-point gap comes from (caliber's verbose breakdown)

Still-failing rubric categories at 85/100:

| Category | Score | Detail |
|---|---|---|
| Project grounding | 3/12 | Config doesn't mention `.claude-hooks`, `bench`, `bin`, `claude_hooks`, `consultants` (+6 more) — i.e. CLAUDE.md is sparse on the actual directory structure |
| MCP servers configured | 0/0 | N/A on this project |
| AGENTS.md exists | 0/1 | Caliber didn't generate AGENTS.md |
| Learned content present | 0/2 | No `caliber learn install` artefacts |
| External sources configured | 0/0 | N/A |

The headline gap is **Project grounding (3/12)**: gemma's CLAUDE.md
mentions only a fraction of the project's top-level directories. The
baseline claude-cli CLAUDE.md mentions all of them densely. This is
the specific axis to target if we want to close the next ~6 points.

## Skills produced

| Skill | Body chars | `paths:` | Bare refs | `file:line` | Built-in? |
|---|---|---|---|---|---|
| `consultants-management` | 3,886 | 2 | 3 | 0 | no (project-specific) |
| `setup-compile-aware`    | 2,892 | 2 | 4 | 0 | no (project-specific) |
| `find-skills`            | 1,963 | 0 | 0 | 0 | yes (caliber) |
| `save-learning`          | 2,129 | 0 | 0 | 0 | yes (caliber) |
| `setup-caliber`          | 7,446 | 0 | 1 | 0 | yes (caliber) |

**The two project-specific skills gemma chose are reasonable picks:**
- `consultants-management` is the v1.1.0 marquee feature.
- `setup-compile-aware` covers the LSP engine's compile-aware diagnostics
  work shipped in v0.7.

But it **missed** the 5 skills the baseline produced (`add-provider`,
`add-hook-handler`, `code-graph-rebuild`, `proxy-metadata-field`,
`run-tests`). Those are still the architecturally important targets
for an agent navigating this codebase. A future gemma run could be
nudged toward them via a richer `survey_project` tool result or a
caliber config seed that names target skill subjects.

## Cloud weather during the run

84 chat completions to upstream, **zero 5xx**. The proxy's existing
`force_first` retry fired ~10 times (model occasionally skipped tool
calls on iter 0 — the proxy injected a corrective user message and
retried, the existing 1.0.x mechanism). No new resilience-layer
events were observed because cloud was stable.

This run was on **pre-resilience-port** code (proxy started 14:02,
before commit `aeca4ae`). Future runs after a proxy restart will
benefit from the new retry budget if cloud weather worsens.

## Pitfalls hit during the run (and folded into PROTOCOL.md)

1. **Initial rsync excluded `.git`** → caliber saw "no languages" in
   23s, ran for 5+ min generating configs against an empty stack
   before we caught it. Killed and re-rsynced. (PROTOCOL §1, pitfall 1)
2. **`/root/.caliber/config.json` had `provider: claude-cli` from a
   prior baseline run** → caliber's first launch silently routed
   through claude-cli ignoring `OPENAI_BASE_URL`/`CALIBER_MODEL` env.
   The proxy log showed zero chat hits while caliber stalled at
   "Generating configs ... Model is taking longer than expected".
   Fixed by per-bench fake-HOME (`fake-home-gemma4-31b-cloud/.caliber/config.json`
   with `provider: openai`). (PROTOCOL §3, pitfall 2)
3. **`pkill -f caliber init` killed our launching shell** (rc=144).
   Switched to `kill <PID>` by exact pid lookup. (PROTOCOL pitfall 7)

## Verdict

`gemma4:31b-cloud` is **the strongest non-claude-cli caliber init
backend** we've measured. It's the first to clear the 80/100
threshold, the first to produce `paths:`-correct skills cleanly,
and it does it 23% faster than claude-cli.

The remaining 9-point gap is concentrated in **CLAUDE.md project
grounding density** — fixable with prompt engineering on caliber's
config-generation step rather than model swaps. That's the next
experiment.

Recommended use today:

- For projects where claude-cli is unavailable / too expensive,
  `gemma4:31b-cloud` via the grounding proxy is now a **PROD-screen**
  backend at A grade.
- The 2-vs-5 skill gap means the operator should expect to manually
  add 1-3 missing skills via `caliber refresh` after init — but that's
  a known gap, not a regression.

## Reproduce

`run-bench.sh` in this workspace's directory; PROTOCOL.md §1-§5 for the
full recipe.
