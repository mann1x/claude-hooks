# PLAN — Proxy stats SQLite rollup + metric expansion

**Status:** researched, not implemented. Come back end of April 2026.

**Context:** the proxy writes per-request JSONL (`~/.claude/claude-hooks-proxy/YYYY-MM-DD.jsonl`).
We want persistent, queryable stats — not ad-hoc `proxy_stats.py` runs.
Phased so each step is independently useful.

Research inputs synthesized here:
- **stellaraccident's report** on issue #42796 (6,852 session JSONLs, 234,760
  tool calls — the richest single proxy-analysis writeup the community has
  produced).
- **Research-agent mining** of ~20 other claude-code issues (open + closed)
  for metric demand.
- **ccusage** (`npx ccusage@latest`) — de-facto community CLI, dedups by
  `message.id + model + requestId`.

---

## Phases

| Phase | Scope | Estimate | Depends on |
|---|---|---|---|
| **S1** | SQLite schema + daily rollup job that ingests existing JSONL. | S | — |
| **S2** | Extend proxy body-parser: `isSidechain` / `agentId` / beta headers / request classification. Backfill S1 once live. | S | S1 |
| **S3** | SSE thinking-block metrics (signature byte length + delta counts) as new JSONL fields. | S | — |
| **S4** | Dashboard (single-file Flask or static HTML over SQLite) — daily, weekly, per-agent, per-model, burn projection, regression canaries. | M | S1–S3 |
| **S5** | Cross-host stats (if we ever point pandorum at solidPC's proxy AND want a combined view — today this is effectively already true because pandorum routes through solidPC's proxy). | S | S4 |

Each phase ships on its own; no grand migration.

---

## S1 — SQLite rollup (core schema)

Single file: `~/.claude/claude-hooks-proxy/stats.db` (configurable via
`proxy.stats_db_path`). Rollup script under `scripts/proxy_rollup.py`,
run via systemd timer or proxy internal tick.

### Core tables

- `requests` — one row per JSONL line (idempotent upsert, natural key:
  `ts + session_id + request_hash`). Holds everything the JSONL already
  carries plus S2/S3 additions.
- `daily_rollup` — date-keyed aggregates.
- `session_rollup` — session-keyed aggregates.
- `model_rollup` — (date, model) grid.
- `agent_rollup` — (date, `agentId` or "main") grid. Filled by S2.
- `ratelimit_windows` — time-series of 5h / 7d utilization + representative-
  claim transitions (`allowed` → `allowed_warning` → `throttled`).
- `beta_headers` — (date, header_name) count of how many requests carried
  each beta header. Filled by S2.

### Rollup job

Idempotent. Reads all JSONL files still on disk (proxy retains 14 days by
default). Computes + upserts all rollup tables. Safe to run hourly or
daily. Drop SQLite rows older than a configurable retention (default
365 days) to cap database growth.

### Dedup rule (borrow from ccusage)

Natural key for `requests`: `sha256(message_id + model + request_id)` —
defends against the proxy seeing the same request twice on retry.

---

## S2 — Request body parser extensions

Parse the outgoing request body for metadata Claude Code ships but the
proxy doesn't yet capture:

- `isSidechain` — boolean, marks subagent traffic.
- `agentId` — subagent type name (`code-analysis`, `general-purpose`,
  `ultrathink-detective`, etc.).
- `parent_session_id` — if present, lets us build the main-agent →
  subagent tree.
- `isMeta` — synthetic messages (Bash rewrites, `tool_result` stubs).
- Beta headers from the request: `anthropic-beta`, `redact-thinking-*`,
  `context-management-*`, `agent-teams`, `showThinkingSummaries`, etc.
- Request classification heuristic → one of:
  `main | subagent | warmup | sideQuery | compact | classifier | unknown`.
  `sideQuery` / `classifier` detectable by short prompts + specific system
  prompts; `compact` by the CC-generated compaction prompt signature.

All go straight into `requests` columns and propagate to the rollups.

---

## S3 — Thinking-token proxy metrics

stellaraccident's key finding: the SSE `signature` field on thinking
blocks has **0.971 Pearson correlation** with thinking content length,
so even when content is redacted we can still estimate depth. Adds to
each JSONL line:

- `thinking_delta_count` — number of SSE `thinking_delta` events in the
  stream.
- `thinking_signature_bytes` — length of the `signature` blob on
  `content_block_start type=thinking` events.
- `thinking_output_tokens` — if Anthropic ever exposes this (stellar-
  accident's explicit ask), capture it; else fall back to the estimate.

Purely additive. Old rows keep NULL; rollups handle NULL as 0.

---

## S4 — Dashboard

Single-binary Python, read-only over SQLite. Views:

- **Daily** — tokens in/out, cache hit rate, Warmups blocked, model
  divergence count, top N sessions by burn.
- **Weekly** — 7d burn curve with 5h sub-windows overlaid, ETA to
  exhaustion, week-over-week delta.
- **Per-agent** — which subagents cost what; which ones ran Warmup most.
- **Per-model** — requested vs delivered split.
- **Canary panel** — the regression signals from stellaraccident's
  methodology: stop-reason distribution shift, thinking-signature-median
  trend, user-interrupt rate (if transcript data is joined in S5).
- **Beta-header timeline** — new headers appearing in the stream, for
  spotting feature rollouts that correlate with behavior changes.

No auth (localhost only). Optional `/api/daily.json` for external
tooling.

---

## S5 — Cross-host aggregation (optional)

Already effectively covered: pandorum routes through solidPC's proxy,
so solidPC's SQLite is the source of truth. If we ever decentralize,
options are: (a) each host writes to its own DB and we rsync nightly,
or (b) stats DB lives on NAS with SQLite over NFS (safe for append-only,
not under concurrent writers).

---

## Metric catalog

### Tier A — multiple issues, high demand (implement in S1/S2 as soon as the body parser lands)

| # | Metric | Issues | JSONL gap today |
|---|---|---|---|
| A1 | Sidechain/subagent token split | #33945, #22625, #32617, #23254, #47922, #43945, #45958 | No `isSidechain` / `agentId` / `parent_session_id` |
| A2 | Warmup per-agent breakdown (cache-creation burn) | #47922, #17457, #16752, #16961, #25138 | `is_warmup` captured; no agent tag |
| A3 | Cache hit rate (`cache_read / (cache_read + cache_creation)`) | #47098, #41284, #39803, #43893, #38029 | Usage captured; rollup needed |
| A4 | Model requested vs delivered divergence | #45312, #43005, #40269, #30350, #30353, #31480 | Both fields captured; counter needed |
| A5 | Weekly-window burn projection (5h + 7d) | #43271, #35672, #15366, #20636, #25041 | Headers captured; projection math needed |
| A6 | Thinking-token proxy (delta count + signature bytes) | #42796 (stellaraccident's explicit ask) | Not captured — needs S3 |

### Tier B — interesting, single advocate or niche but useful

| # | Metric | Issue | Notes |
|---|---|---|---|
| B1 | Stop-reason distribution per day | #42796 | Canary for premature-stop regressions |
| B2 | Latency percentiles by hour-of-day (PST+UTC) | #42796 | stellaraccident's post-redaction load-sensitivity finding |
| B3 | Auto-backgrounded Bash poll-storm detector (cache_read/min spikes > threshold) | #45958 | Detects stalled `cargo test` watchers eating 15M cache_read |
| B4 | Session-resume output-token spikes | #38029 | 652k output tokens on resume, no user input |
| B5 | Compaction events (auto + manual) | #30276 | Tokens compacted-from / compacted-to |
| B6 | HTTP 401 rate during parallel agent dispatch | #37520 | Simple counter |
| B7 | `isMeta` synthetic-rewind count per session | #44596 | Bash-rewind / orphaned `tool_use` detector |
| B8 | Beta-header capture timeline | #42796 | `redact-thinking-*`, `context-management-*` — feature rollouts |
| B9 | Model routing share (Opus / Sonnet / Haiku % of tokens) | #27665 | "93.8% of Max tokens to Opus" |
| B10 | Account/OAuth-email tag (multi-account split) | #43271 | Parse auth header if feasible |
| B11 | CC version / user-agent parsing | #42796, #43893, #47922 | Correlate metric regressions with CC upgrades |
| B12 | `service_tier` from usage block (standard/batch/priority) | — | Already in SSE; just surface |

### Already covered (sanity check)

- `anthropic-ratelimit-unified-*` headers verbatim — ✓
- Full `usage` block merged from `message_start` + `message_delta` — ✓
  (`cache_creation_input_tokens`, `cache_read_input_tokens`,
   `ephemeral_5m_input_tokens`, `ephemeral_1h_input_tokens`, `service_tier`)
- `synthetic` flag (the `<synthetic>` rate-limiter ghost model) — ✓
- `warmup_blocked` / `is_warmup` — ✓
- `stop_reason` — ✓
- `req_bytes` / `resp_bytes` / `duration_ms` — ✓

### Gaps to fill in the JSONL collector first (pre-schema)

1. `isSidechain` / `agentId` / parent-session linkage — blocks most Tier A
2. `thinking_tokens` count / signature bytes — blocks A6 (stellaraccident's ask)
3. Beta-header capture — blocks B8
4. Request classification (main / warmup / sideQuery / compact / classifier) — prereq for clean rollups

---

## stellaraccident-inspired quality canaries (future S4+ feature)

These come from stellaraccident's methodology. Most require transcript
data (not proxy data), so they're S5 / transcript-joining territory.
Noted here so we don't lose them:

- **Read:Edit ratio** — 6.6 (good) → 2.0 (degraded). Tool-call level,
  needs transcript.
- **Edits without prior Read %** — 6.2% → 33.7%. Transcript.
- **Reasoning loops per 1K tool calls** — 8.2 → 26.6. Text-pattern in
  assistant output.
- **"simplest" word frequency** — 2.7 → 6.3 per 1K tool calls.
  Text-pattern.
- **Stop-hook violation rate** — 0 → 10/day during the March regression.
  claude-hooks could fire its own canary counter from `stop_guard.py`.
- **User-interrupt rate per 1K tool calls** — 0.9 → 11.4. Detectable if
  we capture session resume patterns.
- **Time-of-day thinking-signature variance** — 2.6x (good) → 8.8x
  (degraded). A2 signature-bytes metric lets us reproduce this chart.
- **Convention drift** — CLAUDE.md-rule violations in generated code.
  Out of proxy scope, noted for reference.

---

## Sources

- **#42796** stellaraccident — thinking-redaction analysis (the goldmine)
- **#47922** our own Warmup drain issue
- **#17457 / #16752 / #16961 / #25138** — earlier Warmup reports (closed NOT_PLANNED)
- **#43945** — cost tracking understates spend (sideQuery, subagent, unknown-model)
- **#43271** — community status-line proposal (dashboard blueprint)
- **#45958** — parallel-agent 90-min stall, 15M cache_read burn
- **#33945 / #22625 / #32617 / #23254** — subagent token tracking requests
- **#30276** — auto-compact triggers on cumulative tokens
- **#35672 / #15366 / #20636 / #25041** — plan quota / reset exposure
- **#47098** — new sessions never hit cache
- **#41284** — forking session recreates cache
- **#38029** — 652k output tokens on resume with no user input
- **#43893** — 143x token-usage drop theory (cache-read-weight hypothesis)
- **#45312 / #43005 / #40269 / #30350 / #30353** — silent Opus → Sonnet / Haiku
- **#44596** — long-Bash `isMeta` synthetic rewind, orphaned `tool_use`
- **#27665** — "93.8% of Max tokens to Opus without optimization"
- **#37520** — 401 during parallel agents
- **#41591** — auto-update wipes session JSONL (motivates persistent SQLite)
- **ccusage** — `npx ccusage@latest`, baseline to replicate (dedup + breakdowns)

---

## Non-goals for S1–S3

- Cross-transcript joining (stellaraccident-level analysis). That's S5+
  territory and probably belongs in a separate tool or downstream of the
  existing `episodic-memory` server.
- Anthropic-account reverse engineering. We only read public / stable
  response fields and headers.
- Live streaming dashboards. HTTP refresh every 10 s is plenty for one
  user's own usage.
- Pricing calculator. ccusage does that well and we should link to it
  rather than duplicate; Anthropic plan pricing is too opaque for
  accurate internal math anyway.
