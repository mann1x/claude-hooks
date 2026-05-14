# `/consultants` — multi-agent council consultation

> **Status:** v1.1.0 · opt-in install via `python install.py` ·
> dedicated `claude-hooks-consultants` conda env (Py 3.11) with
> LangGraph + LangServe · per-OS service unit (or smart-start
> spawn-on-demand) · permanent on-disk session artifacts under
> `.claude-hooks/consultants/<sid>/`

`/consultants` runs a **council** of LLM specialist agents on a
single deep question:

```
question
   │
   ▼
[planner]   ── decomposes the question, picks files / paths to ground in
   │
   ▼
[researcher×N]  ── tool-call loop over read_file / grep / glob /
   │                list_files / recall_memory; produces grounded
   │                evidence reports with path:line citations
   ▼
[critic×C]      ── reads researcher reports + the question; returns
   │                DECISION: ready | needs_more_research with gaps
   ▼
[synthesizer]   ── composes the final answer from research + critic verdict
   │
   ▼
synthesizer's final answer (lives on disk + returned to the user)
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
| Per-role `enabled` | role | Toggle planner / researcher / critic on or off (synthesizer is mandatory). Disabling planner skips decomposition; disabling researcher gives the synthesizer only the bare question (rarely useful); disabling critic skips the verdict step. |
| Per-role `model` | role | Primary Ollama model for that role. Roles can run different models. |
| Per-role `ctx_max` | role | Pin context length explicitly, or `auto` to probe via `/api/show` on first use. |
| Per-role `extra_models` | role | At x-tier effort: fan-out lanes (researcher, critic). At any tier on synthesizer: failure-fallback chain. |
| `effort` | global | Default effort tier — `low`/`medium`/`high`/`max`/`xmedium`/`xhigh`/`xmax`. |
| `service.mode` | global | `always-on` or `smart-start`. |
| `smart_start.idle_timeout_seconds` | global | Idle timeout before reaping the engine in smart-start mode. |
| `topology` | global | Currently only `council`. Future: roundtable, freeform. |

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

# Discover what's available
claude-consultants config list-models
```

`config list-models` calls Ollama's `/api/tags` and filters to
tools-capable models — what the council can actually use as a role
model.

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

---

## Implementation pointers

If you want to read the code:

- Skill: [`.claude/skills/consultants/SKILL.md`](../.claude/skills/consultants/SKILL.md) — orchestration logic
- Engine: `consultants/engine/council.py` — graph nodes and
  per-role logic (planner, researcher, critic, meta-critic,
  synthesizer)
- Engine: `consultants/engine/graph.py` — LangGraph state machine
  wiring (Send fan-out, conditional edges, barrier nodes)
- Engine: `consultants/engine/recorder.py` — `transcript.db`
  writer and `load_role_messages` reader
- Engine: `consultants/engine/storage.py` — `summary.md` /
  `transcript.md` / `metadata.json` writers
- Server: `consultants/server/runner.py` — primary + follow-up
  session runners
- Server: `consultants/server/app.py` — FastAPI HTTP surface
- CLI: `consultants/cli.py`
- Config: `consultants/config.py`
- Tests: `tests/test_consultants_*.py` (369 passing as of v1.1.0)

---

## See also

- [`docs/get-advice.md`](get-advice.md) — when one model is plenty
- [`docs/benchmarks/EVALUATION.md`](benchmarks/EVALUATION.md) —
  evaluation protocol (run + grade + compare)
- [`docs/benchmarks/index.md`](benchmarks/index.md) — per-label
  benchmark sweeps
- [`docs/consultants-benchmarks.md`](consultants-benchmarks.md) —
  canonical query set
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
