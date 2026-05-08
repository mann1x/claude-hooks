# What's new in v1.1.0

> **Released:** 2026-05-08 · cut from `dev` after a 5-week
> series of phased landings · prior release was
> [v1.0.3](https://github.com/mann1x/claude-hooks/releases/tag/v1.0.3)
> on 2026-04-30
>
> **Migration:** drop-in. No config schema breaks; no hook
> contract changes. Run `python install.py` once on each host to
> pick up the new bin/ shim wrappers and (optionally) install the
> `/consultants` engine. See [`docs/RELEASING.md`](RELEASING.md)
> if you want the upgrade procedure.

This is the human-readable v1.1 highlights doc. For the full,
release-engineered, "every commit accounted for" record see
[`CHANGELOG.md`](../CHANGELOG.md).

---

## Two LLM-to-LLM advisory features, both built on the shared `agent_loop.runner`

The headline of v1.1 is two new ways to get a second opinion from
a model that isn't Claude:

### `/get-advice` — single-model second opinion

Multi-turn conversation with a configured Ollama advisor. The
advisor sees your project through six grounding tools (`read_file`,
`grep`, `glob`, `list_files`, `recall_memory`, `recall_kg`) and
gives a focused validation / sanity-check / design-review answer.

Four sub-skills configure it: `/get-advice--model`,
`/get-advice--effort`, `/get-advice--tools`, plus the driver
`/get-advice <query>`.

Full runbook: [`docs/get-advice.md`](get-advice.md).

### `/consultants` — multi-agent council

Four-role pipeline (planner → researcher → critic → synthesizer)
with iterative refinement, on-disk durable sessions, and
follow-up support that picks up exactly where the prior session
left off. Heavier than `/get-advice` — runs in the background as
a sibling service while you keep working — but produces an
audit-quality answer when the question deserves one.

Five sub-skills: `/consultants <query>`, `/consultants--config`,
`/consultants--list`, `/consultants--show`, `/consultants--followup`.

Full runbook: [`docs/consultants.md`](consultants.md).

---

## /consultants v1.1 — what got added on top of the v1.0.3 first-release engine

The `/consultants` engine itself shipped during the v1.0 series.
v1.1 added **eight phases** of follow-on work:

### 1. Full per-role LLM-message-history persistence (Phases 1-4)

Every role's complete LLM message thread (system + user + tool
results accumulated across iters + the final assistant message)
is now persisted to a `transcript.db` SQLite sidecar in each
session's directory. When a session is closed, evicted by the
idle reaper, or simply not warm anymore, the next follow-up
reconstructs the threads from disk and the response is
indistinguishable from a still-warm follow-up.

Schema: [`docs/consultants-transcript-db-schema.md`](consultants-transcript-db-schema.md).

### 2. Live-session iteration and follow-up message threads (Phases 5-7)

`claude-consultants follow-up <parent_sid>` extends a parent's
role threads with a new user question. The follow-up runs through
a shortened graph (researcher + synthesizer; critic optional) and
emits its own session under `.claude-hooks/consultants/<new_sid>/`
with `parent_sid` recorded in metadata. Chains are fine — a
follow-up's sid can be the parent of another follow-up.

The JSONL tracer that v1.0 used was decommissioned in Phase 6;
all traces now live in `transcript.db` and are queryable via
`claude-consultants show <sid> --raw`.

### 3. Multi-model x-tier fan-out (Phases 9-10)

Three new effort tiers — `xmedium`, `xhigh`, `xmax` — activate
fan-out across configured `extra_models`:

- **xmedium / xhigh**: researcher fan-out. Planner emits N×M Sends
  (N plan-items × M models) so each researcher lane runs a
  different model in parallel. Synthesizer sees the union.
- **xmax**: researcher fan-out + critic fan-out + meta-critic
  combine. C parallel critics across `critic.extra_models`, then a
  meta-critic synthesizes the C verdicts into one consensus
  decision. Critic identities are anonymized in the meta-critic
  prompt; the audit map back to models lives in
  `transcript.db.events.model`.

Phase 10a fixed a critic-fanout 6× overshoot caused by LangGraph
conditional edges firing per-Send-source-invocation; the fix is a
pass-through `research_barrier` node that restores barrier
semantics before the conditional fan-out fires.

Base tiers (`low`/`medium`/`high`/`max`) silently ignore
`extra_models` — the foot-gun guard.

### 4. Cloud-flap recovery (this commit, 2026-05-08)

Three layers of recovery from transient `Internal Server Error`
flaps on Ollama Cloud:

- **ChatClient retry budget bumped to 15 attempts / ~15 min
  ceiling** (was 8 / ~136 s). Affects both `/consultants` and
  `/get-advice`. Trade-off: a real permanent outage takes ~15 min
  to surface as a user-visible error.
- **Synthesizer fallback model chain** — when the primary
  synthesizer model exhausts its retry budget, the engine walks
  `synthesizer.extra_models` in order before giving up. Same
  ChatClient (so the same proxy + connection pool); only the
  `model` field of the payload changes per attempt. NOT a fan-out
  (synthesizer never fans out, even at xmax).
- **Degraded-answer composer** — when every model in the
  fallback chain fails, the engine writes a `summary.md` whose
  `final_answer` field surfaces the researcher's full reports +
  the critic's verdict (the most expensive work of the
  consultation, not lost) with a banner explaining it's a
  degraded answer and a recovery hint pointing at `follow-up
  <THIS_SID>` to inherit research + critic warm.

The `/consultants--followup` skill is **failed-session-aware**:
when the most recent session is `failed` (synthesizer flap), the
skill defaults to it and offers to chain off the failed sid
(researcher + critic threads inherit warm from disk; synthesizer
re-runs with the v1.1 fallback chain) or its parent (start fresh).

---

## Cross-platform install.py hardening

Two install.py improvements that make `python install.py` produce
a working install on any platform without manual PATH editing:

### bin/* shim wrappers (POSIX + Windows)

Skill CLIs invoked by bare name from a `/consultants--config` or
`/get-advice` skill failed with `command not found` before v1.1.0
because Claude Code's bash subprocess doesn't include the repo's
`bin/` on PATH on any platform. Symlinks don't fix it either —
the shims resolve `REPO` via `dirname "$0"` which through a
symlink points at the symlink dir, not the repo.

install.py now drops thin exec-wrappers in a known PATH-friendly
location for every shim:

- **POSIX (Linux + macOS)**: `~/.local/bin/<shim>` — POSIX sh
  wrapper that `exec`s the absolute repo path.
- **Windows**: `%LOCALAPPDATA%\claude-hooks\bin\<shim>` (POSIX sh
  wrapper for the MSYS bash that Claude Code uses on Windows)
  plus a `<shim>.cmd` sibling for native cmd / PowerShell users.

Wrappers carry an install-time tag in their first comment line so
the installer is fully idempotent — re-running it replaces only
its own files, hand-rolled wrappers of the same name are left
alone with a notice. `python install.py --uninstall` removes only
tagged wrappers.

### Windows User PATH auto-prepend via `reg add` (not `setx`)

For `/consultants` and `/get-advice` skills to actually resolve
on Windows, the wrapper directory needs to be on User PATH that
Claude Code's bash subprocess inherits. install.py now prepends
`%LOCALAPPDATA%\claude-hooks\bin` to `HKCU\Environment\PATH`
using `reg add` (NOT `setx` — `setx` silently truncates User
PATH to 1024 chars, which is destructive on any developer
machine), then broadcasts `WM_SETTINGCHANGE` so new processes
pick it up without a logoff. Defensive 16 KB ceiling on the
resulting PATH.

### axon-host installer hardening

The axon-unified service's installer pre-flight now verifies
(a) the dedicated dependencies (`axoniq`, `uvicorn`,
`httpx-sse`, `pydantic-settings`, `sse-starlette`) are present
before enabling the unit, and (b) `/root/.axon` registry directory
exists. Both prevent the
`status=226/NAMESPACE` boot loop encountered on solidpc
2026-05-07.

---

## Cloud-model evaluation suite

`docs/benchmarks/` now houses a **reproducible cloud-model
evaluation protocol** for `/consultants` — three locked queries
(smoke + audit-medium + audit-high) run against each model
candidate, with grading rubric, per-role grades, and per-query
verdicts:

- [`docs/benchmarks/EVALUATION.md`](benchmarks/EVALUATION.md) —
  the protocol (cloud-model fairness, multi-run discipline,
  grading rubric)
- [`docs/consultants-benchmarks.md`](consultants-benchmarks.md) —
  the canonical query set (3 locked queries; do not edit
  without re-baselining everyone)
- [`docs/benchmarks/index.md`](benchmarks/index.md) — per-label
  index with the v1.1 release tldr
- [`docs/benchmarks/<label>/`](benchmarks/) — per-model results.
  Seven labels as of 2026-05-07: `kimi-k2.6-cloud`,
  `gemma4-31b-cloud`, `glm-5-1-cloud`, `qwen3-5-cloud`,
  `qwen3-5-397b-cloud`, `minimax-m2-7-cloud`, plus the
  retroactive `kimi-k2.6-cloud-pre-harden` baseline.

All seven labels are graded by Claude (the LLM driving the
evaluation work) reading the on-disk transcripts per the
[§3.5 protocol](benchmarks/EVALUATION.md#35-per-role-quality-grading-the-key-to-building-a-model-mix);
the human operator only verifies model selection in real-world
skill usage on whichever model gets picked — they don't grade
transcripts. Headline grades:

| Verdict | Labels |
|---|---|
| **PROD-READY** (mix `P:A R:A C:A S:A`) | `kimi-k2.6-cloud`, `gemma4-31b-cloud`, `glm-5-1-cloud`, plus the retroactive `kimi-k2.6-cloud-pre-harden` baseline |
| **EVALUATED-ONLY** (usable in mixes for specific roles where the per-role grade is A) | `minimax-m2-7-cloud` (strong critic), `qwen3-5-397b-cloud` (strong planner + critic), `qwen3-5-cloud` (cheap sibling, same shape as 397b) |

Single-run, N=3 confirmation pending per
[§5](benchmarks/EVALUATION.md#5-multi-run-requirement). The
recommended on-host mixed config for v1.1.0 (planner / synthesizer
= `gemma4:31b-cloud`, researcher = gemma4 + glm-5.1 at x-tier,
critic = glm-5.1 + gemma4 at xmax, synthesizer failure-fallback
= glm-5.1) draws directly on these grades.

For "which model should I pick?" guidance see [`docs/consultants.md`
§ Picking models](consultants.md#picking-models).

---

## Smaller wins

A handful of v1.1 changes worth surfacing:

- **`/consultants--followup` skill** — the new fifth member of
  the `/consultants` skill family, exposes
  `claude-consultants follow-up` directly (previously only
  reachable via the underlying CLI or by asking Claude to
  dispatch it).
- **stop_guard stall-after-commitment check** — prevents Claude
  from stopping after declaring "I'll do X" without actually
  doing X. Off by default; opt-in via
  `hooks.stop_guard.enabled: true`.
- **pgvector backup + validity canary stack** — ships a periodic
  pg_dump + a row-count + last-write canary that surfaces
  silent corruption / index drift. See
  [`docs/pgvector-runbook.md`](pgvector-runbook.md).
- **PreCompact hook** — a new event we wire by default. Fires
  when Claude Code is about to compact context. The handler
  self-gates on the wrapup skill being installed; a wired entry
  is a cheap no-op when the skill is absent.

---

## What didn't change

- **Hook contract**: `UserPromptSubmit`, `Stop`, `SessionStart`,
  `SessionEnd`, `PreToolUse`, `PostToolUse` payload shapes
  unchanged. Existing hook handlers stay working.
- **Recall path**: `recall.py`, `hyde.py`, `dedup.py`, `decay.py`
  unchanged. Memory provider plugin contract unchanged.
- **Proxy stack**: `claude-hooks-proxy` + `dashboard` + `rollup`
  unchanged. Stop-phrase guard YAML unchanged.

---

## Acknowledgements

v1.1's `/consultants` engine ate substantial test infrastructure
(369 tests pass on the consultants suite alone, ~1.6k tests
passing repo-wide). The benchmark sweep that grounded the
multi-model x-tier work cost real cloud spend; thanks to the
tester for running the labels.

---

## See also

- [`CHANGELOG.md`](../CHANGELOG.md) — release-engineered record
- [`docs/RELEASING.md`](RELEASING.md) — cut procedure
- [`docs/consultants.md`](consultants.md) — `/consultants` runbook
- [`docs/get-advice.md`](get-advice.md) — `/get-advice` runbook
- [`docs/benchmarks/EVALUATION.md`](benchmarks/EVALUATION.md) —
  evaluation protocol
