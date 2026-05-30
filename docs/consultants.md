# `/consultants` — multi-agent council consultation

> **Status:** engine **v2** (post-v1.7.0, on `dev`) · opt-in
> install via `python install.py` · dedicated
> `claude-hooks-consultants` conda env (Py 3.11) with LangGraph +
> LangServe · per-OS service unit (or smart-start spawn-on-demand)
> · permanent on-disk session artifacts under
> `.claude-hooks/consultants/<sid>/`
>
> **v2 over v1.1 in one paragraph:** six roles instead of four
> (opt-in `tool_executor` + `coder` join the four base roles),
> proper composition under tool_executor + x-tier multi-model
> fan-out (M11c-3 `parent_lane_idx`-routed `Send` dispatch), an
> opt-in BaseStore-backed cross-session memory (M8) with
> per-namespace TTL + Caliber-style distillation-on-expiry (M14),
> and a `CitationLinter` that verifies every `path:line` claim
> before the answer leaves the council (#204 / #205 / #207).
> The role-by-role reference lives at
> [`docs/consultants-roles.md`](consultants-roles.md) — read it
> before flipping opt-ins on.

`/consultants` runs a **council** of LLM specialist agents on a
single deep question:

```
question
   │
   ▼
[planner]       ── decomposes the question, picks files / paths to
   │                ground in; optional JSON blocks request
   │                tool_executor or coder lanes
   ▼
[researcher×N]  ── one of two modes (planner picks):
   │                  Mode A (inline): tool-call loop directly over
   │                    read_file / grep / glob / list_files /
   │                    recall_memory; produces grounded report
   │                  Mode B (PLAN-REPORT split, opt-in):
   │                    researcher emits a tool_plan (PLAN),
   │                    tool_executor lanes run the tools,
   │                    researcher fans back in REPORT mode
   │                    against routed tool_results (M11c-3
   │                    parent_lane_idx)
   ▼  ── CitationLinter verifies every path:line before peer_findings
[tool_executor×K]   ── (opt-in role, default OFF as of 2026-05-18)
   │                    structured tool-call dispatcher feeding
   │                    routed evidence back to its parent
   │                    researcher lane. See the role doc for
   │                    when to enable.
   ▼
[critic×C]      ── reads researcher reports + the question; returns
   │                DECISION: ready | needs_more_research with gaps
   │                (xmax: meta-critic combines C critic verdicts)
   ▼
[synthesizer]   ── composes the final answer from research + critic
   │                verdict; CitationLinter runs again on output
   │  ── (if planner emitted coder_tasks) ────────────┐
   │                                                  ▼
   │                                              [coder]
   │                                              sandbox-bounded
   │                                              write_file role
   │                                              (50 KB/file,
   │                                              1 MB/lane,
   │                                              16 files/lane)
   ▼
final answer (lives on disk + returned to the user, store-distilled
into the project namespace at TTL expiry if the M8 store is enabled)
```

The council runs **in the background** while you keep working in
Claude Code. Sessions are permanent — they survive engine restarts,
OS reboots, and Claude Code updates — and live under
`<your-project>/.claude-hooks/consultants/<sid>/` as four files:

- `summary.md` — the synthesizer's final answer with YAML
  front-matter (sid, models, effort, duration, status, error if
  any)
- `transcript.md` — full role-by-role transcript with tool calls +
  results inline
- `metadata.json` — structured per-role token usage, retry counts,
  exit reason
- `transcript.db` — SQLite sidecar with the full per-role LLM
  message threads (system + user + tool-result messages
  accumulated across iters + the final assistant message). This is
  what makes follow-ups continuous with the parent: the engine
  reloads message threads from this file when reopening a session
  from disk. Schema:
  [`docs/consultants-transcript-db-schema.md`](consultants-transcript-db-schema.md).

> **v1.3 migration note:** The pre-v1.3 form had five separate
> slash commands (`/consultants`, `/consultants--config`,
> `/consultants--list`, `/consultants--show`,
> `/consultants--followup`). Those were collapsed into a single
> dispatcher with subverbs (`ask` default-implicit, `config`,
> `list`, `show`, `followup`) to cut the upfront slash-command
> menu cost. The installer removes the legacy
> `~/.claude/skills/consultants--*` dirs on first v1.3 upgrade run.

---

## When to use it

| Use `/consultants` when… | Use `/get-advice` when… |
|---|---|
| The question benefits from a **specialist split** — a researcher actively grounding in code while a critic challenges the evidence | One model is plenty |
| You want **iterative refinement** — the critic can re-route back to the researcher for more rounds | One conversation is plenty |
| You want the answer to **survive restarts** and be re-readable later via `/consultants show <sid>` | Single-shot is fine |
| You want **multi-model perspective** at `xmedium` / `xhigh` / `xmax` effort tiers — fan out the researcher across 2-N different cloud models and have a meta-critic combine multiple critic verdicts | One model's perspective is enough |
| Wall-clock budget: 1-15 min depending on effort tier | Wall-clock budget: seconds to a minute |
| Question types: architecture audits, refactor risk analysis, release-notes-vs-diff cross-check, design review on a decision that affects multiple files | Validation, sanity-check, recipe review, code review of one function |

`/consultants` is the heavier path. Use [`/get-advice`](get-advice.md)
for one-shot questions; reach for the council when the question
deserves a planner pass and grounded evidence-gathering.

---

## v2 engine — what changed since v1.1

The v1.1 council had four roles wired in a fixed
planner → researcher → critic → synthesizer topology. The v2
engine (post-v1.7.0, all on `dev`) keeps that as the default path
and adds three orthogonal pieces:

**1. Two opt-in roles (default OFF as of 2026-05-18).**

- **`tool_executor`** — splits researcher into PLAN-REPORT mode:
  researcher emits a structured `tool_plan` block, the
  tool_executor lane runs the calls, the researcher fans back in
  REPORT mode against routed `tool_results`. Default OFF because
  the M14 first-real-ask A/B benchmark showed 3× wall time, +43%
  tokens, and **fewer** edge cases caught on grep-shaped questions
  vs the synthesizer-direct path. Keep enabled only for
  slow-tool-budget questions where the synthesizer would otherwise
  re-issue the same five grep calls across nine x-tier lanes — the
  role's actual win condition. Record:
  [`benchmarks/consultants/results/2026-05-18/tool-executor-ab/report.md`](../benchmarks/consultants/results/2026-05-18/tool-executor-ab/report.md).
- **`coder`** — sandbox-bounded `write_file` role for "change
  these files, here's what they should look like after"
  questions. Planner emits a `coder_tasks` JSON block when
  `requires_code_generation=true`. Caps: 50 KB/file, 1 MB/lane,
  16 files/lane. Model picks matter — see the M11b skill-eval
  rubric at
  [`docs/consultants-skill-eval-protocol.md`](consultants-skill-eval-protocol.md).

Full per-role detail (responsibilities, prompts, planner JSON
contracts, model evidence, shortcomings, when to enable):
[`docs/consultants-roles.md`](consultants-roles.md).

**2. Optional BaseStore-backed cross-session memory (M8 + M14).**

When `[store].enabled = true`, the engine threads a LangGraph
BaseStore (pgvector or sqlite_vec backend) through every
researcher and synthesizer turn. Four canonical namespaces:

| Namespace                | Lifetime         | Default TTL |
|--------------------------|------------------|-------------|
| `(sid, "research")`      | per-session lane | 30 days     |
| `(sid, "tool_results")`  | per-session tool | 24 hours    |
| `("project", project_id)`| per-project     | never       |
| `("user", user_id)`      | user-global      | never       |

M14 ships a hourly daemon sweep that, for expiring research
entries, runs a **Caliber-style distillation pass** (primary
`gemma4:31b-cloud` → fallback `glm-5.1:cloud`) that summarizes
the session's findings into the durable
`("project", project_id)` namespace **before** deleting the
originals. Episodic short-term → semantic long-term. The
research originals are only deleted after a successful
project-namespace write — failed distillation keeps the
originals in place for the next sweep tick.

Defaults flipped on 2026-05-18 (commit `48f1c4e`):
`store.enabled=True`, `backend=sqlite_vec`, TTL+distillation on.
M12 parity holds because `medium` (the default effort) is not in
the M8 `enable_at_efforts` set — the store stays inactive until
you ask for `high` / `max` / x-tier.

**3. `CitationLinter` at the researcher boundary (#204 / #205 / #207).**

Every researcher REPORT (and synthesizer output) passes through
a three-layer verifier before flowing downstream:

1. **Path resolution** — the file must exist under one of the
   discovered allowed roots.
2. **Line bounds** — the cited line range must be within the file.
3. **Symbol match** — if the cite names a function / class /
   method, the symbol text must actually appear at the cited
   line (±1 slack). Backed by the in-process `code_graph`
   `enclosing_symbol_at` query for fast lookup; ast-parse
   fallback for files outside the graph.

Failures are annotated inline as
`[no <claimed> at this line; line is in <actual>]` so the
synthesizer (and the user) can see exactly which cites the
council fabricated. The linter runs at researcher-boundary
(annotations propagate via `peer_findings`) rather than only at
synthesizer-output, so downstream lanes see only verified cites.

**4. May-18 hardening pass (#212 / #215 / #216 / #218).**

A live regression run uncovered four issues in the M14 store
path; all four are fixed and the fixes ship as additive
defaults. None of these change role behavior — they fix the
machinery beneath the store.

- **#212 — silent durable-write hole.** `ProviderBackedStore._do_put`
  used to swallow provider failures from `provider.store(...)`.
  The M14 reaper's critical invariant ("research originals
  only deleted after a successful distillation write to the
  project namespace") was therefore conditional on the
  fallible provider call surfacing. Fix: failures re-raise to
  the caller; reaper catches `DistillationFailed` and skips
  the delete step, originals stay for the next sweep tick.
- **#215 — M14 reaper pacing.** Three configurable knobs so a
  backlog can't fan out into one big synchronous batch that
  saturates the embedder:
  - `store.ttl.jitter_pct = 0.1` — at write time,
    `expires_at = now + ttl * (1 + uniform(-jitter, +jitter))`.
    Spreads cohorts across ±10 % of the nominal TTL so the
    reaper doesn't see N sessions expire on one tick. Critical
    after the default-on flip stamped every existing session
    with the same 30 d expiry within one minute.
  - `store.distillation.max_groups_per_sweep = 5` — caps
    **successful** distillations per tick. Cost-gate skips
    (< `min_entries_per_distillation`) and tool_results
    deletes don't burn the budget; only LLM-driven
    distillations do. Remaining groups roll over to the next
    sweep, originals stay in place.
  - `store.distillation.pace_seconds_between_distillations = 5.0`
    — sleeps between consecutive distillations within one
    tick. Sliced 0.5 s so reaper shutdown stays responsive.
- **#216 — `tool_executor` write amplification.** With
  `tool_executor` enabled, a researcher lane that loops
  PLAN → tool → REPORT N times wrote N different rows to the
  store (one per round, different content_hash). The fix:
  `record_research` now uses a stable per-lane key (`L{lane_idx}`,
  no content hash), and `ProviderBackedStore._do_put` deletes
  the prior provider row on overwrite. One row per
  `(namespace, key)` pair regardless of how many rounds run.
- **#218 — pgvector read-only transaction leak.**
  `expire_before` / `count` / `_search_tables` /
  `_recall_hybrid_unlocked` / `_kg_search_nodes` used to
  return rows without closing the psycopg3 implicit
  transaction. The connection sat ``idle in transaction``
  holding `AccessShareLock`, blocking any concurrent
  `ALTER TABLE` from another connection — which is exactly
  what M14's lazy `ADD COLUMN IF NOT EXISTS expires_at`
  migration is on every researcher session's first store
  call. Fix: `_read_only_finish()` helper called at every
  read-only happy-path exit.

---

## Prerequisites

- An **Ollama backend** reachable from the install host. The
  engine reads its endpoint from
  `~/.claude/consultants-config.toml` (which install.py seeds from
  `CALIBER_GROUNDING_UPSTREAM` if present).
- The `claude-consultants` CLI on PATH. install.py drops it as a
  POSIX-shell wrapper at `~/.local/bin/claude-consultants` (Linux
  / macOS) or `%LOCALAPPDATA%\claude-hooks\bin\claude-consultants[.cmd]`
  (Windows, with User PATH auto-prepended).
- The `claude-hooks-consultants` conda env (Py 3.11). install.py
  creates this on opt-in and pip-installs the LangGraph + LangServe
  dependencies into it. **Conda is required — there is no bare-venv
  fallback.** install.py aborts with a clear message if conda isn't
  installed at the system level.
- A per-OS service unit OR the daemon's smart-start mode (see
  below). install.py walks you through both choices on opt-in.

The five `/consultants*` skills register automatically when the
`claude-hooks-consultants` env is present. On hosts where the env
is missing, the skills are skipped silently — you can still install
the rest of claude-hooks without /consultants.

---

## Service modes

`/consultants` needs a long-lived Python process to drive the
LangGraph state machine and hold warm ChatClients between turns.
You pick how that process is managed at install time:

### Always-on (default, recommended)

A per-OS service unit keeps the engine resident:

- **Linux**: `systemd/claude-hooks-consultants.service`,
  `Type=notify`, `Restart=on-failure`. Bound to `127.0.0.1:38095`.
- **macOS**: `~/Library/LaunchAgents/com.claude-hooks.consultants.plist`.
- **Windows**: scheduled task `claude-hooks-consultants` running
  `python -m consultants.server` on logon.

Steady-state RAM cost: ~250 MB. First-turn latency is sub-second
because Python + LangGraph are already paged in.

### Smart-start (opt-in)

No service unit. The `claude-hooks-daemon` lazily spawns the engine
as a child process on the first `/consultants` request, tracks
idle time, and `SIGTERM`s it after `idle_timeout_seconds` (default
30 min). Auto-respawn on the next request.

Trade-off: zero RAM idle, but you pay a 5-10 s cold start on each
first consultation after a quiet period. Choose this if you
consult rarely or run on a memory-constrained host.

Switch modes any time:

```
claude-consultants config set-service-mode always-on
# or
claude-consultants config set-service-mode smart-start
```

The CLI prints an exact follow-up command (re-run install.py to
install/uninstall the service unit, then restart the daemon).

---

## Effort tiers

Effort caps the budget — researcher rounds, critic re-routes,
multi-model fan-out — that the council is allowed to spend. Eight
tiers, four "base" and four "x-prefix":

| Tier | Topology | Researcher rounds | Critic re-routes | Multi-model fan-out |
|---|---|---|---|---|
| `low` | planner → researcher → synthesizer | ≤1 | _no critic_ | none |
| `medium` *(default)* | full council | ≤1 | ≤1 | none |
| `high` | full council | ≤3 | ≤2 | none |
| `max` | full council | uncapped | uncapped | none |
| `xmedium` | full council | ≤1 | ≤1 | researcher across primary + extras |
| `xhigh` | full council | ≤3 | ≤2 | researcher across primary + extras |
| `xmax` | full council | uncapped | uncapped | researcher AND critic across primary + extras + meta-critic combine |

The **x-prefix tiers** activate multi-model fan-out:

- **researcher fan-out** (xmedium / xhigh / xmax): the planner
  emits N×M Sends — N plan-items × M models (primary + extras) —
  so each researcher lane runs a different model in parallel. The
  synthesizer sees the union of their reports.
- **critic fan-out + meta-critic combine** (xmax only): the
  council also runs C parallel critics across `critic.extra_models`,
  then a meta-critic synthesizes the C verdicts into one consensus
  decision. Critic identities are anonymized in the meta-critic
  prompt ("Critic 1", "Critic 2", …) — the audit map back to
  models lives in `transcript.db`'s `events.model` column.

To activate fan-out you must (a) set the effort tier to an
x-prefix tier AND (b) configure `extra_models` for the researcher
(or critic at xmax). Either alone is silent.

```
claude-consultants config set-role researcher --add-model glm-5.1:cloud
claude-consultants config set-effort xhigh
```

Every base tier silently ignores `extra_models` — the foot-gun
guard. So an `xhigh` config with no extras runs a single-model
researcher; a `medium` config with extras runs a single-model
researcher anyway.

---

## Running a consultation

In a Claude Code prompt:

```
/consultants validate the v1.1.0 release notes against the
v1.0.3..HEAD diff. I want concrete entries we forgot to mention,
plus a yes/no on whether the SemVer bump is appropriate.
```

What happens:

1. The skill reads config via `claude-consultants config show`.
2. It builds a framing message (your question + project context +
   what "good" looks like + where to look) and starts the council:

   ```
   claude-consultants consult --message "<framing>" --cwd "$(pwd)"
   ```

   Returns `{"sid": "csl-2026-05-08-...", "status": "running",
   "status_url": "..."}`.

3. Claude polls status every ~10 s and surfaces per-role
   transitions to you ("planner: done; researcher: 2 tool calls so
   far; critic: ready; synthesizer: drafting"). You can keep
   working on other things — the council runs in the background.

4. On `status: completed`, Claude fetches `result <sid>` and
   prints the synthesizer's `summary_markdown` verbatim with a
   footer:

   ```
   sid: csl-2026-... · effort: medium · duration: 4m 12s ·
   models: planner=gemma4:31b-cloud researcher=gemma4:31b-cloud
   critic=glm-5.1:cloud synthesizer=gemma4:31b-cloud
   ```

5. Re-read any past consultation later with
   `/consultants show <sid>` (no engine call — reads `summary.md`
   from disk).

To override effort for a single consultation (e.g. force xhigh on
a particularly cross-cutting question without changing the default):

```
/consultants --effort xhigh <your question>
```

---

## Follow-ups — the v1.1 headline feature

`/consultants followup` runs a **continuous** consultation that
reuses the parent's per-role LLM message history. The planner /
researcher / critic / synthesizer each pick up exactly where they
left off — the follow-up's answer is continuous with the parent's,
the way turn N+1 of a warm chat would be. This is materially
different from running `/consultants` again with a related
question (which builds a fresh thread from scratch).

How it works:

1. The engine looks up the parent_sid's `_role_messages` — either
   warm in memory (if the engine still has the session loaded) or
   reconstructed from `transcript.db` by `load_role_messages` (if
   closed / evicted).
2. Each role's `prior_messages` is fed in as the context base. The
   follow-up question is appended as the next `user` message.
3. The follow-up runs through a **shortened graph** (researcher +
   synthesizer; critic optional based on the follow-up's effort)
   and emits its own session under
   `.claude-hooks/consultants/<new_sid>/` with `parent_sid` recorded
   in metadata.

Usage:

```
/consultants followup <question>
/consultants followup csl-2026-... <question>
```

Without an explicit sid, the skill defaults to the **most recent
session of any status** in the project. When the most recent is
`failed` (cloud flap mid-synthesizer), the skill surfaces that
explicitly and offers two paths:

- **Chain off the failed sid** *(recommended)* — researcher +
  critic threads inherit from the failed session's `transcript.db`,
  so the synthesizer composes with prior research warm. Cost is
  one synthesizer call, not a council re-run. Pairs with the
  v1.1 synthesizer fallback chain (below) for the cheapest
  recovery.
- **Chain off the failed sid's parent_sid** — start over from the
  known-good consultation upstream of the failure. Use this when
  the failed session's research was thin / wrong and you want a
  fresh angle on the original question.

Chains are fine — a follow-up's sid can be the parent of another
follow-up, and so on. Each link inherits all prior turns from the
same role thread.

---

## Review loop — consultancy status + bounded auto-followup

Mirroring `/get-advice`'s discuss-until-satisfied flow, after a council
answer the skill **reviews** it rather than accepting the first result
blindly. The mechanism is an engine-owned status machine layered above
the per-run `status` (`running | completed | failed`):

- A *council run* is one LangGraph invocation (one sid).
- A *consultancy* is the whole chain rooted at the first `ask`
  (`root_sid`); followups are children. Its **`consultancy_status`**
  advances:

  ```
  in_progress ──(council done)──► ready_to_review ──accept──► accepted (terminal)
       ▲                                │
       └──── followup (under cap) ──────┤
                                        └── followup at cap ──► awaiting_approval
                                               (user yes + --allow-extra) ──► in_progress
  ```

The status is persisted to `consultancy.json` in the **root** session
dir (`.claude-hooks/consultants/<root_sid>/consultancy.json`):

```json
{"root_sid": "csl-…", "status": "ready_to_review", "followup_count": 1,
 "max_followups": 4, "extra_granted": 0, "effective_cap": 4,
 "child_sids": ["csl-…"], "updated_at": 1748352000.0}
```

It is engine-owned, queryable (the `consultancy` block rides every
`status` / `result` / `state` / `follow-up` response), and survives idle
reap, daemon restart, and context compaction — so the skill resumes the
loop correctly after a compact by reading `status <root_sid>` first.

**The loop** (driven by the skill, like `/get-advice`): when the council
finishes (`ready_to_review`), Claude critiques the answer and either

- **accepts** it — `claude-consultants accept <sid>` → `accepted`
  (terminal); or
- **auto-issues a focused follow-up** (with a one-line rationale) and
  loops, **up to `max_followups`** (default 4).

**The cap.** The engine enforces `max_followups` server-side: a follow-up
past the cap is refused with a structured
`{"ok": false, "reason": "followup_limit_reached", …}` (HTTP 200) and the
consultancy flips to `awaiting_approval`. The skill then stops and asks
the user, presenting the remaining concern + why another round helps. On
approval the skill re-issues the follow-up with `--allow-extra N`
(`--force` = the configured default), which raises the cap by `N` for
**this consultancy only** — no persisted config change. `--allow-extra`
is also the escape hatch for non-Claude-Code/scripted callers.

**Configuration** (both flat / effort-independent; skill menu + CLI):

```
claude-consultants config set-max-followups 4   # auto-followup cap (>= 0)
claude-consultants config set-allow-extra 1      # per-approval grant (>= 1)
```

or via `/consultants config` → **Followup limit**. Both surface in
`config show` (`max_followups`, `allow_extra`).

---

## Recovery from cloud flaps

Three layers of recovery, all v1.1, all automatic:

### 1. ChatClient retry budget — 15 attempts, ~15 min ceiling

Every LLM call (every role) goes through `ChatClient.chat` which
retries on `{408, 429, 500, 502, 503, 504}` plus known transient
4xx body shapes. Default budget at v1.1 is **15 attempts** with
exponential backoff capped at 90 s — worst-case ~905 s ≈ 15.1 min
total. This absorbs every Ollama Cloud flap shorter than the
budget. The motivating session
(`csl-2026-05-07-2158-7c75`, xhigh effort, audit-followup) burned
the pre-bump 8-attempt / 136 s budget on a 2+ minute flap; the new
budget would have absorbed it cleanly.

You can still tune this per-call:

```python
from claude_hooks.get_advice.chat_client import ChatClient
c = ChatClient(url, max_retries=8, retry_max_delay_s=30.0)
```

Default applies to both `/consultants` and `/get-advice`. Trade-off:
a real permanent outage takes ~15 min to surface as a user-visible
error.

### 2. Synthesizer fallback model chain

When the synthesizer's primary model exhausts its retry budget,
the engine walks `synthesizer.extra_models` in order before giving
up. **Same chat_client** (so the same proxy + connection pool);
only the `model` field of the payload changes per attempt. First
success wins. Each attempt records an `llm_call` event in
`transcript.db` with the actual model used, so post-hoc audit via
`/consultants show <sid> --raw` reveals which model produced the
final answer.

Configure:

```
claude-consultants config set-role synthesizer --add-model glm-5.1:cloud
```

`synthesizer.extra_models` is **NOT a fan-out** — the synthesizer
never fans out, even at xmax. It's strictly a serial failure
chain. Active at every effort tier (not gated by the x-prefix —
cloud flaps don't care about effort).

### 3. Degraded-answer composer

When every model in the fallback chain fails, the engine writes a
`summary.md` whose `final_answer` field surfaces:

- the researcher's full reports (often 3-5k tokens of analysis at
  xhigh effort)
- the critic's verdict (usually 1k of structured decision text)
- a banner explaining it's a degraded answer (not a synthesized
  one)
- a recovery hint pointing at:

  ```
  claude-consultants follow-up <THIS_SID> --message "compose a
  final answer from the prior research and critic"
  ```

The next attempt inherits research + critic threads warm from
`transcript.db` and only pays one synthesizer call. So the worst
case (cloud down for >15 min, every fallback model also down) still
gives you the raw expensive work — uncombined but readable.

---

## Configuration

`/consultants config` walks you through every config knob via
AskUserQuestion. No file editing. The on-disk source of truth is
`~/.claude/consultants-config.toml` (user-global) or
`<project>/.claude-hooks/consultants.toml` (per-project override),
hand-editable if you prefer.

### What you can configure

| Knob | Tier | What it does |
|---|---|---|
| Per-role `enabled` | role | Toggle planner / researcher / critic / tool_executor / coder on or off (synthesizer is mandatory). Disabling planner skips decomposition; disabling researcher gives the synthesizer only the bare question (rarely useful); disabling critic skips the verdict step; tool_executor + coder are opt-in (default OFF as of 2026-05-18). |
| Per-role `model` | role | Primary Ollama model for that role. Roles can run different models. |
| Per-role `ctx_max` | role | Pin context length explicitly, or `auto` to probe via `/api/show` on first use. |
| Per-role `extra_models` | role | At x-tier effort: fan-out lanes (researcher, critic, tool_executor). At any tier on synthesizer: failure-fallback chain. |
| Coder language routes | role | Per-language primary + fallback model for the `coder` role. Default map covers c / cpp / csharp / go / python / rust. |
| `effort` | global | Default effort tier — `low`/`medium`/`high`/`max`/`xmedium`/`xhigh`/`xmax`. |
| `service.mode` | global | `always-on` or `smart-start`. |
| `smart_start.idle_timeout_seconds` | global | Idle timeout before reaping the engine in smart-start mode. |
| `topology` | global | Currently only `council`. Future: roundtable, freeform. |
| `store.enabled` | store | Master switch for the cross-session memory adapter. M14 default = `true`. |
| `store.backend` | store | `memory` (no durability) / `sqlite_vec` (default; file at `~/.claude/consultants-store.db`) / `pgvector` (shared Postgres). |
| `store.enable_at_efforts` | store | Effort tiers at which the store wires into the graph. Default `["high", "max", "xmedium", "xhigh", "xmax", "xauto"]` — lower tiers stay zero-cost. |
| `store.recall_limit` | store | Top-K results for the peer-findings recall block. Default 5. |
| `store.sqlite_vec_path` / `pgvector_dsn` / `pgvector_table` | store | Backend-specific endpoints. The pgvector table defaults to `consultants_store` so the M14 reaper never scans the recall pipeline's `memories_<model>` rows. |
| `store.embedder` + `embedder_options` | store | Embedder identifier (e.g. `llamafile`, `ollama`) and its connection options. `install.py` auto-copies these from the matching `providers.<name>` block in `claude-hooks.json`. |
| `store.ttl.enabled` | store | Master TTL switch. M14 default = `true`. |
| `store.ttl.research_days` / `tool_results_hours` / `project_days` / `user_days` | store | Per-namespace TTL. `0` or negative = never expire. Defaults: 30 d / 24 h / never / never. |
| `store.ttl.refresh_on_read` | store | Bump `expires_at` forward on every successful recall hit ("if it's still useful, keep it"). Default `true`. |
| `store.ttl.jitter_pct` | store | #215 cohort spread — at write time `expires_at += ttl * uniform(-jitter, +jitter)`. Stops N sessions from expiring on the same reaper tick. Default 0.1 (±10 %). |
| `store.distillation.enabled` | store | Master switch for Caliber-style summarization at expiry. M14 default = `true`. |
| `store.distillation.model` | store | Primary distiller LLM. Default `gemma4:31b-cloud` (M11c-2 tool_executor winner). |
| `store.distillation.fallback_models` | store | Tried in order on primary failure. Default `["glm-5.1:cloud"]`. |
| `store.distillation.sweep_interval_seconds` | store | Reaper cadence. Minimum 30 s; default 3600 (1 h). |
| `store.distillation.min_entries_per_distillation` | store | Cost gate — research groups below this delete without an LLM call. Default 3. |
| `store.distillation.max_session_entries` | store | Per-prompt truncation cap. Default 50 (~30 k tokens at `gemma4:31b-cloud`'s 32 k ctx). |
| `store.distillation.max_groups_per_sweep` | store | #215 cap on **successful** distillations per tick. Cost-gate skips + tool_results deletes don't burn the budget. Default 5; `0` = uncapped. |
| `store.distillation.pace_seconds_between_distillations` | store | #215 inter-call sleep (0.5 s sliced for shutdown). Default 5 s. |
| `coder_limits.max_file_bytes` / `max_total_bytes` / `max_files` | role | Sandbox caps for the opt-in `coder` role. Defaults 50 KB / 1 MB / 16. |

### CLI

If you don't want the AskUserQuestion walk-through (e.g. scripting,
ssh, CI), every change has a direct CLI:

```bash
# Inspect
claude-consultants config show

# Roles
claude-consultants config set-role planner --model gemma4:31b-cloud
claude-consultants config set-role researcher --enabled true
claude-consultants config set-role critic --ctx 32768
claude-consultants config set-role researcher --add-model glm-5.1:cloud
claude-consultants config set-role researcher --remove-model glm-5.1:cloud
claude-consultants config set-role researcher --clear-extras
claude-consultants config set-role synthesizer --add-model glm-5.1:cloud  # failure fallback

# Globals
claude-consultants config set-effort xhigh
claude-consultants config set-service-mode always-on
claude-consultants config set-idle-timeout 1800

# Review loop (consultancy followup cap + per-approval grant size)
claude-consultants config set-max-followups 4   # auto-followup cap (>= 0)
claude-consultants config set-allow-extra 1      # grant per approval (>= 1)

# Cross-session memory store (M8 + M14)
claude-consultants config set-store --enabled true --backend sqlite_vec
claude-consultants config set-store --recall-limit 7
claude-consultants config set-store --add-effort medium     # enable at the medium tier too
claude-consultants config set-store --pgvector-dsn 'postgresql://u:p@host:5432/db' \
    --pgvector-table consultants_store

# TTL knobs (per namespace; 0 / negative = never expire)
claude-consultants config set-store-ttl --enabled true
claude-consultants config set-store-ttl --research-days 14 --tool-results-hours 6
claude-consultants config set-store-ttl --project-days 0     # explicit "never"
claude-consultants config set-store-ttl --refresh-on-read false --jitter-pct 0.0

# Distillation knobs (M14 sweep + #215 pacing)
claude-consultants config set-store-distillation --enabled true \
    --model gemma4:31b-cloud --add-fallback-model glm-5.1:cloud
claude-consultants config set-store-distillation --sweep-interval-seconds 1800 \
    --min-entries-per-distillation 5
claude-consultants config set-store-distillation --max-groups-per-sweep 3 \
    --pace-seconds-between-distillations 10

# Coder language routes (opt-in coder role; M11b skill-eval)
claude-consultants config coder set-route python --primary glm-5.1:cloud \
    --fallback kimi-k2.6:cloud
claude-consultants config coder set-default --primary glm-5.1:cloud

# Discover what's available
claude-consultants config list-models
```

Every `set-*` accepts `--project` + `--cwd` to write a per-project
override file (`.claude-hooks/consultants.toml`) instead of the
user-global `~/.claude/consultants-config.toml`. Both files are
hand-editable if you prefer; the CLI is a typed wrapper around the
same dataclass + TOML round-trip.

`config list-models` calls Ollama's `/api/tags` and filters to
tools-capable models — what the council can actually use as a role
model.

### Interactive installer

`install.py` calls `_setup_consultants_store()` after the engine is
wired. The default flow auto-detects an embedder from the main
recall pipeline's `providers.pgvector` or `providers.sqlite_vec`
block in `claude-hooks.json` and copies it into the consultants
config — no questions asked, M14 defaults stand. If you want to
tune TTL / distillation knobs at install time instead of discovering
the CLI afterwards, answer **yes** to the
"Customize TTL + distillation knobs now?" prompt and it walks
through every knob with the current default as the fallback.

---

## Cross-session memory (M8 store + M14 distillation)

The optional `[store]` block wires a LangGraph
[`BaseStore`](https://langchain-ai.github.io/langgraph/concepts/persistence/#stores)
into every researcher + synthesizer turn. Two questions it answers
across sessions: *"did anyone in the same project already research
this?"* (peer-findings recall before researcher draft) and *"what
durable knowledge can we keep when the per-session transcript
expires?"* (distillation on TTL).

### The four namespaces

| Namespace                  | Lifetime         | Default TTL | Written by                              | Read by                                                                       |
|----------------------------|------------------|-------------|-----------------------------------------|-------------------------------------------------------------------------------|
| `(sid, "research")`        | per-session lane | 30 days     | researcher REPORT (verified citations)  | sibling researcher lanes via `peer_findings`; reaper as the distillation source |
| `(sid, "tool_results")`    | per-session tool | 24 hours    | tool_executor (when role enabled)       | researcher REPORT prompt (#216 stable per-lane key)                            |
| `("project", project_id)`  | per-project     | never       | M14 reaper at distillation              | future sessions in the same project (`project_id` = `sha256(cwd)[:12]`)        |
| `("user", user_id)`        | user-global      | never       | (no writer yet — reserved for future)   | (no reader yet — reserved for future)                                          |

The store is **effort-gated** via `enable_at_efforts`. The default
`["high", "max", "xmedium", "xhigh", "xmax", "xauto"]` keeps the
lower tiers (`low`, `medium`) on the zero-cost no-store path. M12
parity stays green because `medium` (the default effort) is not in
the gate.

### Episodic → semantic via M14 distillation

Per-session research is **episodic** memory — high-detail, tied to
the session, expires fast. Cross-project knowledge is **semantic**
memory — distilled, durable, useful across sessions. M14 wires the
consolidation between them:

```
                            ┌─ daemon thread sleeps in 0.5 s slices ─┐
                            │     (interval default = 3600 s)        │
sweep tick ─────────────────┴────────────────────────────────────────┘
   │
   ▼
provider.expire_before(before_iso=now - 5 min, limit=1000)
   │
   ├─ Group rows by (sid, kind):
   │
   ├─ For "research" group with N ≥ min_entries_per_distillation:
   │    1. Build distillation prompt from rows (≤ max_session_entries)
   │    2. Call distiller LLM: primary → fallback chain
   │    3. project_id = sha256(cwd_from_meta)[:12]
   │    4. Write summary → ("project", project_id) with provenance
   │    5. provider.delete_by_hashes(originals)
   │
   └─ For "tool_results" group: delete unconditionally (no distillation)
```

**Critical invariant**: research originals are deleted **only after
a successful distillation write to the project namespace**. Failed
distillation (every model in the chain raised) keeps the originals
in place — the next sweep tick retries. This survives transient
cloud flaps, OOM kills, and signal-interrupted writes (#212).

`tool_results` is dropped at TTL without distillation — cheap to
recompute and nothing worth keeping beyond the session window.

The distillation prompt is in `consultants/engine/distillation.py`;
borrows the rubric shape from
[`claude_hooks/reflect.py`](../claude_hooks/reflect.py) (retain
citations + decisions, drop process narration / dead ends).

### Recall — what the researcher sees

When the store is wired in, every researcher draft sees a
`## peer_findings` block in its prompt — top-K (default 5) results
from a hybrid recall across the project's research namespace. The
block lands **above** the question so the model treats it as
context, not as a finding to re-derive. Recall results passing the
TTL filter trigger a `refresh_expires_at` UPDATE when
`refresh_on_read = true` — "if it's still useful, keep it".

### #215 reaper pacing

The default-on flip stamped every existing session with the same
30 d expiry within one minute. Without pacing knobs, the reaper
would have fanned a hundred sessions into a single sweep tick and
saturated the embedder. Three knobs space the work out:

- **`store.ttl.jitter_pct`** — spreads cohorts at write time.
  `expires_at = now + ttl * (1 + uniform(-jitter, +jitter))`.
  Default 0.1 (±10 %); set to 0.0 for deterministic expiry
  windows.
- **`store.distillation.max_groups_per_sweep`** — caps
  *successful* distillations per tick. Cost-gated skips and
  tool_results deletes don't count. Default 5; rolls overflow to
  the next sweep with originals intact.
- **`store.distillation.pace_seconds_between_distillations`** —
  inter-call sleep within one tick, sliced 0.5 s for shutdown
  responsiveness. Default 5 s.

### Backend choice

- **`sqlite_vec` (default)** — single file at
  `~/.claude/consultants-store.db`, no daemon dependency, FTS5 +
  vec extension for hybrid recall. Lowest-friction.
- **`pgvector`** — shares the Postgres your main recall pipeline
  already uses. Dedicated table (default `consultants_store`)
  keeps consultants writes separate from `memories_<model>`.
  Higher throughput, KG relations available.
- **`memory`** — LangGraph's bundled in-process store. No
  durability, no cross-session memory. M14 reaper short-circuits
  on this backend (nothing to sweep). Use it for debugging.

### Operational knobs at a glance

| Verb                      | Question it answers                                                        |
|---------------------------|----------------------------------------------------------------------------|
| `set-store`               | Should the store run? What backend? At which effort tiers? Which embedder? |
| `set-store-ttl`           | How long does each namespace's data live? Refresh on hit? Cohort spread?  |
| `set-store-distillation`  | When is the reaper sweep? Which model distills? How much load per tick?    |

All three are also exposed via the `/consultants config` skill
walkthrough — `claude-consultants config show` dumps the current
state so the skill knows what to default each prompt to.

---

## Reading a session

Each session lives at `<project>/.claude-hooks/consultants/<sid>/`:

```
csl-2026-05-08-1520-3f9a/
├── summary.md          ← synthesizer's final answer + YAML front-matter
├── transcript.md       ← full role transcript with tool calls inline
├── metadata.json       ← per-role token + timing + retry data
└── transcript.db       ← SQLite sidecar (LLM message threads)
```

`summary.md` is what `/consultants show <sid>` prints. It's
markdown with YAML front-matter; tools that already parse YAML
(Caliber's `score-refine.ts`, OpenWolf's `cerebrum.md` parsers,
etc.) round-trip it cleanly.

`transcript.db` is **not** committed by default — `.gitignore`
covers `.claude-hooks/consultants/` because the db carries full
LLM payloads (system prompts, tool results, intermediate model
reasoning) that are sensitive to the project. The
`docs/benchmarks/<label>/` exception is opt-in committed as
public-audit data; per-session live artifacts under
`.claude-hooks/` are not.

To inspect the events table directly:

```
claude-consultants show <sid> --raw
```

Dumps each `llm_call` / `node_enter` / `node_exit` / `tool_call`
event as one JSON object per line. Schema:
[`docs/consultants-transcript-db-schema.md`](consultants-transcript-db-schema.md).

---

## Picking models

Empirical model evaluation lives in:

- [`docs/benchmarks/EVALUATION.md`](benchmarks/EVALUATION.md) —
  the protocol (run + grade + compare across models)
- [`docs/benchmarks/`](benchmarks/) — the per-model results.
  Seven labels as of 2026-05-07: `kimi-k2.6-cloud`,
  `gemma4-31b-cloud`, `glm-5-1-cloud`, `qwen3-5-cloud`,
  `qwen3-5-397b-cloud`, `minimax-m2-7-cloud`, plus the
  `kimi-k2.6-cloud-pre-harden` retroactive baseline. Each label
  carries `smoke`, `audit-medium`, and `audit-high` runs with
  `summary.md` + `transcript.md` + `metadata.json` + `results.md`.
- [`docs/consultants-benchmarks.md`](consultants-benchmarks.md) —
  the canonical query set (3 locked queries) used by the
  benchmark runner.

The two configurations recommended on the dev hosts as of v1.1.0:

```toml
# Mixed config (used as the v1.1 default after 2026-05-07 audits)
[roles.planner]      model = "gemma4:31b-cloud"
[roles.researcher]   model = "gemma4:31b-cloud"
                     extra_models = ["glm-5.1:cloud"]   # x-tier fan-out
[roles.critic]       model = "glm-5.1:cloud"
                     extra_models = ["gemma4:31b-cloud"] # xmax fan-out
[roles.synthesizer]  model = "gemma4:31b-cloud"
                     extra_models = ["glm-5.1:cloud"]   # failure fallback

# Homogeneous kimi config (used in the original v1.1 baseline)
[roles.planner]      model = "kimi-k2.6:cloud"
[roles.researcher]   model = "kimi-k2.6:cloud"
[roles.critic]       model = "kimi-k2.6:cloud"
[roles.synthesizer]  model = "kimi-k2.6:cloud"
```

---

## Lifecycle commands

Most of the time you don't think about session lifecycle — the
engine warms automatically on each consult, reaps on smart-start
idle timeout, and reopens transparently from disk on follow-up.
But when you do need to manage it:

```bash
claude-consultants list-open       # warm sessions in engine memory
claude-consultants reopen <sid>    # restore a closed/evicted session
claude-consultants close <sid>     # release engine memory (reversible — next follow-up auto-reopens)
```

`close` is **not** a "lock the session" — closed sessions auto-
reopen on the next follow-up. It's purely a memory-pressure tool.

---

## Troubleshooting

### "claude-consultants: command not found"

Same root cause as `/get-advice` — the wrapper isn't on PATH for
Claude Code's bash subprocess. See the [troubleshooting section
in get-advice.md](get-advice.md#claude-advisor-command-not-found)
for the fix; replace `claude-advisor` with `claude-consultants`
throughout.

### Engine endpoint unreachable

```
curl http://127.0.0.1:38095/v1/health
```

If this 200's, the engine's up. If it doesn't:

- **Always-on mode**: check the service unit
  (`systemctl --user status claude-hooks-consultants` on Linux,
  `launchctl list | grep consultants` on macOS,
  `schtasks /query /TN claude-hooks-consultants` on Windows).
- **Smart-start mode**: check the daemon
  (`claude-hooks-daemon-ctl status`); the engine spawns lazily on
  the first request and won't be running until then.

If the service is supposedly up but the health check 503's, look
at the engine log:
`~/.claude/claude-hooks-consultants.log` (rotated by the existing
log_rotator).

### "synthesizer failed: ollama chat HTTP 500"

Three possibilities, in order of likelihood:

1. **Cloud flap on the upstream** — the most common cause. v1.1's
   retry budget absorbs flaps up to ~15 min; if the budget is
   exhausted, the synthesizer fallback chain kicks in. If both
   primary and fallback exhaust their budgets, you get a degraded
   answer with researcher + critic content surfaced. To recover:

   ```
   /consultants followup csl-... compose a final answer from
   the prior research and critic
   ```

   This re-runs only the synthesizer with researcher + critic
   threads warm from `transcript.db`.

2. **Model context overflow** — the synthesizer payload at xhigh
   effort can be 50 KB+ (researcher reports + critic verdict +
   prior history). If the model's `ctx_max` is too small, Ollama
   may 400 / 500. Bump the model's pin via
   `set-role synthesizer --ctx 65536` or switch to a larger model.

3. **Genuine upstream outage** — Ollama Cloud actually down. Check
   the upstream's status page; switch to a different model
   provider via the synthesizer fallback chain or
   `set-role synthesizer --model <other>`.

### Researcher tool-spinning

If the researcher fires many tool calls but never produces a
report (waterfall.txt shows researcher round running to its
iter cap), the question's grounding hint isn't tight enough.
Either:

- re-fire with a tighter `<query>` that names specific path:line
  pointers
- bump effort to `high` so the researcher gets more iters per
  round
- check if the project's pgvector is indexed — `recall_memory`
  with no useful hits leaves the researcher only with grep / read

### Follow-up doesn't carry forward the previous follow-up's question

This is **correct behavior, by design**. Each follow-up has
exactly one `parent_sid`. The follow-up inherits **only that
parent's** role threads — it does NOT inherit any sibling
follow-up's. To chain off a sibling follow-up, point at it
explicitly:

```
/consultants followup csl-<sibling_sid> <next question>
```

The `/consultants followup` skill picks the most recent session
of any status by default, which usually does the right thing.

### Store calls failing with `EmbedderError` / `NullEmbedder`

The store needs an embedder to vectorize content at write + recall
time. The M14 defaults turn the store on without forcing one to be
configured — if the install detected one, it was copied over from
`providers.pgvector` / `providers.sqlite_vec`. If you see
`EmbedderError: NullEmbedder cannot embed` in the engine log:

```bash
# Confirm what's wired
claude-consultants config show | jq '.store'

# Wire one explicitly
claude-consultants config set-store --embedder ollama
# then hand-edit ~/.claude/consultants-config.toml's
# [store.embedder_options] block (url / model / timeout)
```

Or re-run `install.py` and let it auto-detect again from
`config/claude-hooks.json`. The store stays effort-gated, so
disabling it via `set-store --enabled false` is also a valid
escape hatch.

### Distillation backlog never drains

Symptoms: `expire_before` returns the same rows tick after tick,
`max_groups_per_sweep` is doing its job (5 / tick), but a 200-
group backlog will still take 40 ticks (≈ 40 hours) at the
default cadence. To accelerate:

```bash
# Drain faster (shorter cadence, more groups per tick)
claude-consultants config set-store-distillation \
    --sweep-interval-seconds 600 \
    --max-groups-per-sweep 20 \
    --pace-seconds-between-distillations 2
```

Restore the defaults once the backlog clears. If the originals
are *not* worth distilling (e.g. a forgotten benchmark dump from
months ago), set `distillation.enabled = false` for a single
sweep cycle and the reaper deletes them straight away.

### Engine log shows `current transaction is aborted` after store hit

This is the **#218 read-only transaction leak** symptom. Pre-#218,
`PgvectorProvider.expire_before` / `count` / search would leave
the psycopg3 implicit transaction open, blocking concurrent
`ALTER TABLE` from another connection. Upgrade to a build that
includes #218 (commit `40eef61` and later); every read-only
happy-path exit now calls `_read_only_finish()`. Manual recovery:
restart `claude-hooks-consultants.service`.

---

## Implementation pointers

If you want to read the code:

- Skill: [`.claude/skills/consultants/SKILL.md`](../.claude/skills/consultants/SKILL.md) — orchestration logic
- Engine: `consultants/engine/council.py` — graph nodes and
  per-role logic (planner, researcher, critic, meta-critic,
  synthesizer)
- Engine: `consultants/engine/graph.py` — LangGraph state machine
  wiring (Send fan-out, conditional edges, barrier nodes,
  M11c-3 `parent_lane_idx`-routed `_fanout_after_tool_executor`)
- Engine: `consultants/engine/tool_executor.py` — opt-in
  PLAN-REPORT role (default OFF since 2026-05-18; see
  [`docs/consultants-roles.md`](consultants-roles.md))
- Engine: `consultants/engine/tool_executor_defaults.py` —
  `RECOMMENDED_DEFAULT_ON` + flip-history comment block
- Engine: `consultants/engine/coder.py` — opt-in sandboxed
  `write_file` role
- Engine: `consultants/engine/citation_linter.py` — three-layer
  `path:line` verifier (#204 / #205 / #207) with `code_graph`
  fast path
- Engine: `consultants/engine/store.py` — M8 BaseStore adapter
  (researcher peer_findings recall + record)
- Engine: `consultants/engine/distillation.py` — M14 Caliber-style
  distillation; primary + fallback chain
- Engine: `consultants/engine/store_reaper.py` — M14 daemon-side
  hourly sweep (TTL expiry → distill → delete)
- Engine: `consultants/engine/recorder.py` — `transcript.db`
  writer and `load_role_messages` reader
- Engine: `consultants/engine/storage.py` — `summary.md` /
  `transcript.md` / `metadata.json` writers
- Server: `consultants/server/runner.py` — primary + follow-up
  session runners
- Server: `consultants/server/app.py` — FastAPI HTTP surface
- CLI: `consultants/cli.py`
- Config: `consultants/config.py`
- Code-graph query: `claude_hooks/code_graph/enclosing.py` —
  process-global mtime-cached `enclosing_symbol_at` used by the
  CitationLinter
- Tests: `tests/test_consultants_*.py` + `tests/test_citation_linter.py`
  + `tests/test_code_graph_enclosing.py` + `tests/test_tool_executor_defaults.py`
  (~1200 collected; full sweep target stays green)

---

## See also

- [`docs/consultants-roles.md`](consultants-roles.md) — **the
  role-by-role reference** (read first when considering enabling
  `tool_executor` or `coder`)
- [`docs/get-advice.md`](get-advice.md) — when one model is plenty
- [`docs/benchmarks/EVALUATION.md`](benchmarks/EVALUATION.md) —
  evaluation protocol (run + grade + compare)
- [`docs/benchmarks/index.md`](benchmarks/index.md) — per-label
  benchmark sweeps
- [`docs/consultants-benchmarks.md`](consultants-benchmarks.md) —
  canonical query set
- [`docs/consultants-skill-eval-protocol.md`](consultants-skill-eval-protocol.md)
  — Consultancy Skill-Eval Protocol v1.0 (coder + stall +
  tool_executor sub-protocols)
- [`docs/consultants-skill-eval-baselines.md`](consultants-skill-eval-baselines.md)
  — model-pick evidence ledger (M11b coder, M11c tool_executor)
- [`benchmarks/consultants/results/2026-05-18/tool-executor-ab/report.md`](../benchmarks/consultants/results/2026-05-18/tool-executor-ab/report.md)
  — the A/B record behind the 2026-05-18 `tool_executor`
  default-flip back to OFF
- [`docs/consultants-transcript-db-schema.md`](consultants-transcript-db-schema.md)
  — `transcript.db` schema + privacy posture
- [`docs/PLAN-consultants-v1.1-message-history.md`](PLAN-consultants-v1.1-message-history.md)
  — the v1.1 design doc (reference, not a runbook)
- [`docs/caliber-proxy.md`](caliber-proxy.md) — the agent-loop
  runner that backs both `/consultants` and `/get-advice`
- [`docs/RELEASING.md`](RELEASING.md) — when to expect changes to
  `/consultants`
- [`docs/whats-new.md`](whats-new.md) — latest release
  highlights (v1.4); prior releases archived alongside
