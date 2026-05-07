# Plan: /consultants v1.1 — full message-history persistence

> **Status:** drafted 2026-05-07, revised same day to swap JSONL
> for SQLite per user direction. Ready for implementation.
> **Owner:** next session picks this up after context compaction.
> **Authoritative target branch:** `dev`. Engine HEAD at draft time:
> commit `17e8c2c`, `bench-baseline-2026-05-07` baseline tag.

## TL;DR

Today, closing or evicting a `/consultants` session preserves enough
disk state to **answer follow-ups correctly** but loses the
per-role LLM message threads (system / user / assistant / tool_call /
tool_result sequences). v1.1 captures those threads in a new
**SQLite sidecar (`transcript.db`)** so that:

1. **Disk-reopened sessions are indistinguishable from warm ones** —
   the follow-up's researcher and synthesizer see the same
   conversation context regardless of whether the parent stayed in
   memory or was loaded from artifacts after a service restart.
2. **Follow-ups can extend the parent's researcher thread** instead
   of starting a fresh single-lane round, so iterative questions
   that depend on tool results from the parent (e.g. "in the file
   you already read, what does line 200 do?") don't re-execute
   `read_file`.
3. **Sessions are auditable / replayable** — every LLM call and
   tool execution is on disk in a structured form, not just the
   rendered markdown transcript.

A binary SQLite file is opaque to line-oriented indexers
(claudemem reindex, ripgrep, RAG ingestion pipelines, file
watchers), so internal LLM conversations — system prompts, tool
results that may include sensitive code excerpts, model reasoning
chains — don't accidentally surface in the user's other tools.
JSONL was the original proposal but loses this property.

## Why now

A user-visible quality gap was observed after the v1.0 reopen
landing (commit `17e8c2c`):

- A consultation completes; we get answer A.
- The user closes / engine restarts.
- The same question's follow-up against the disk-reopened parent
  produces answer B.

For most queries, A and B are equivalent because the parent's
plan / research / critique / final_answer are reconstructable from
`metadata.json`'s `turns` field. For queries whose follow-up
depends on **tool-result content the original researcher saw but
didn't quote in its turn-content** (e.g. "what's the value at
foo.py:200" — the parent might have read foo.py but not echoed
line 200 verbatim), the disk-reopened follow-up has to re-run the
tool, which can fail or pick up a different value if the file
changed.

The v1.1 design closes that gap and adds replayability as a side
effect.

## Current state (what survives close today)

Per `consultants/engine/storage.py:render_summary` /
`render_transcript` and `metadata.json` schema:

| Field | Source | Survives close |
|---|---|---|
| `final_answer` | top of metadata.json + body of summary.md | ✅ |
| `models` (role → tag) | metadata.json | ✅ |
| `topology`, `effort`, `question`, `cwd`, `parent_sid` | metadata.json | ✅ |
| `turns[]` — list of `{role, round, content, prompt_tokens, completion_tokens, tool_calls?, duration_seconds}` | metadata.json | ✅ |
| `plan` text (planner's content) | derived from `turns[?role=='planner'].content` | ✅ via `_load_session_from_artifacts` |
| `research[]` (researcher turn contents) | derived from `turns[?role=='researcher']` | ✅ |
| `critique` text | derived from `turns[?role=='critic']` | ✅ |
| **Full LLM message thread per role** (system / user / assistant w/ tool_calls / tool messages) | only in process memory while engine runs; never written to disk | ❌ |
| **Tool results** (e.g. the actual contents of `read_file` invocations) | only in process memory inside `agent_loop.runner.run_loop._loop_messages` | ❌ |
| `_chat_clients` warm caches (`_probed_think`, `_unsupported_think`) | runtime-only, dropped on close | ❌ — by design |

The `_loop_messages` field that the runner already attaches to each
researcher's response (added during the empty-output fallback work,
see `claude_hooks/agent_loop/runner.py`) is the upstream of the
data we want to persist.

## Schema design

### New artifact: `transcript.db` (SQLite)

Lives alongside the existing artifacts at
`<cwd>/.claude-hooks/consultants/<sid>/transcript.db`. Single
SQLite file per consultation. Why SQLite over JSONL:

- **Privacy:** binary file, not picked up by claudemem reindex,
  ripgrep, RAG ingestors, file-watch indexers. The user
  explicitly called this out — JSONL would expose system prompts
  + tool results to anything that walks the project tree looking
  for text.
- **Schema evolution:** versioned via a `meta` table; future
  additions are ALTER TABLE migrations rather than ad-hoc field
  additions.
- **Concurrent writes:** WAL mode handles simultaneous appends
  from fan-out lanes natively. With JSONL we'd need our own lock.
- **Queries:** "give me every llm_call for the researcher in
  round 2" is one SELECT instead of a streaming jq filter.
- **Stdlib only:** Python's `sqlite3` is in the stdlib, no extra
  dep on the consultants conda env (which already has many).

Cost over JSONL: ~10–20% larger files (SQLite page overhead, but
WAL compresses well at close-time `VACUUM`). Acceptable.

### Schema (v1)

Single file, two tables:

```sql
-- Meta table — one row per consultation. Mirrors the
-- header fields in metadata.json so both files are
-- independently parseable. Schema version pins the format
-- so future readers can branch on it.
CREATE TABLE meta (
    schema_version  INTEGER NOT NULL DEFAULT 1,
    sid             TEXT NOT NULL,
    cwd             TEXT NOT NULL,
    question        TEXT NOT NULL,
    effort          TEXT NOT NULL,
    topology        TEXT NOT NULL,
    parent_sid      TEXT,
    started_at      REAL NOT NULL,
    finished_at     REAL,
    status          TEXT NOT NULL,   -- 'running' | 'completed' | 'failed'
    error           TEXT,
    -- 'subject_baseline_tag' lets follow-ups detect when the
    -- frozen worktree the parent ran against has been bumped;
    -- relevant for the "trust prior tool results" decision in
    -- §"Open questions" #3.
    subject_baseline_tag  TEXT,
    -- Models snapshot (JSON-encoded role->tag dict). Read by
    -- the follow-up runner to know which model handled each
    -- role in the parent.
    models_json     TEXT NOT NULL
);

-- Events table — one row per recordable event. Every LLM call,
-- tool execution, and node enter/exit landing here. Indexes on
-- (role, round, kind) make the per-role thread reconstruction a
-- single SELECT.
CREATE TABLE events (
    event_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts               REAL NOT NULL,
    kind             TEXT NOT NULL,    -- 'llm_call' | 'tool_call' | 'node_enter' | 'node_exit'
    role             TEXT NOT NULL,    -- 'planner' | 'researcher' | 'critic' | 'synthesizer'
    round            INTEGER NOT NULL DEFAULT 1,
    lane_idx         INTEGER,          -- NULL except for researcher fan-out events

    -- llm_call columns. NULL for non-llm rows.
    model            TEXT,
    request_json     TEXT,             -- full request dict (messages, tools, think, options) as JSON
    response_json    TEXT,             -- full response dict (choices, usage) as JSON
    prompt_tokens    INTEGER,
    completion_tokens INTEGER,

    -- tool_call columns. NULL for non-tool rows.
    tool             TEXT,
    args             TEXT,             -- raw arguments string
    output           TEXT,             -- tool result string
    output_chars     INTEGER,

    -- Both kinds: optional duration + error.
    duration_ms      INTEGER,
    error            TEXT
);

CREATE INDEX idx_events_role_kind ON events(role, kind, round);
CREATE INDEX idx_events_ts        ON events(ts);
```

### Pragmas at open time

```sql
PRAGMA journal_mode = WAL;       -- concurrent fan-out writes
PRAGMA synchronous = NORMAL;     -- fsync on commit only, not every page write
PRAGMA foreign_keys = ON;        -- belt-and-braces; we have no FKs yet, future-proofing
PRAGMA temp_store = MEMORY;
```

WAL means we can have multiple researcher lanes inserting events
concurrently without `database is locked` errors. The recorder
opens one connection per lane (sqlite3 connections are NOT
thread-safe by default).

### Per-role message-thread reconstruction

The follow-up runner reads the parent's threads via:

```sql
SELECT request_json, response_json, round, lane_idx
FROM events
WHERE role = ? AND kind = 'llm_call'
ORDER BY ts;
```

For each row, the request's `messages` list at iter N+1 contains
all messages from iter N plus the new tool results, so the FINAL
row's `request_json.messages` IS the complete thread up to that
LLM call, and the FINAL row's `response_json.choices[0].message`
is the last assistant turn. Concatenate to get the full
conversation.

(Researcher's per-lane threads diverge at iter 1 of fan-out and
re-merge logically only at the synthesizer; we store them as
separate `lane_idx` chains so a follow-up can pick which lane to
extend or just take the union.)

### Decision: fold `transcript.db` and the existing trace into one?

Same question as the JSONL draft, same recommendation. The
existing `consultants/engine/trace.py` writes JSONL to
`~/.claude/consultants-traces/<sid>.jsonl`. v1.1 deprecates that
path:

- `CONSULTANTS_TRACE` env var becomes a no-op (logged once at
  startup if set).
- `--trace` / `--no-trace` CLI flags become no-ops with a
  one-line deprecation warning. Remove in v1.2.
- `scripts/consultants_trace_summary.py` is rewritten to query
  `<cwd>/.claude-hooks/consultants/<sid>/transcript.db` via SQL.
- `scripts/consultants_bench_row.py` ditto.
- `scripts/consultants_benchmark.sh` copies `transcript.db`
  into label dirs (replacing the JSONL trace copy).

### Disk-size estimate (revised for SQLite)

From the kimi audit-high run (`csl-...-faa2`, the costliest seen):

- 27 LLM calls × ~5–50 KB per request/response pair = ~500 KB raw
- 46 tool calls × ~2 KB each = ~100 KB raw
- ~10–20% SQLite overhead: ~700 KB total worst case
- Smoke runs land closer to ~70 KB

Per-project with ~100 sessions/year: ~70 MB. Same order as the
JSONL estimate. Document a `claude-consultants prune --older-than
30d` cron in v1.2 if needed.

### Optional VACUUM at close

`PRAGMA wal_checkpoint(TRUNCATE); VACUUM;` at consultation
completion shrinks WAL files and reclaims any deleted-row pages.
Adds ~50 ms per consultation; recommended for the always-on
case.

## Implementation phases

Each phase is independently testable + commitable. Stop after any
phase if scope creeps.

### Phase 1 — Recorder primitive (SQLite)

**New file:** `consultants/engine/recorder.py`

```python
import sqlite3, threading, time, json
from pathlib import Path
from typing import Optional

SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS meta (...);
CREATE TABLE IF NOT EXISTS events (...);
CREATE INDEX IF NOT EXISTS idx_events_role_kind ON events(role, kind, round);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
"""

class MessageRecorder:
    """Thread-safe SQLite-backed recorder for one consultation.

    Each fan-out lane MUST get its own connection (sqlite3
    connections are not thread-safe). The recorder owns a
    connection pool keyed by current thread.

    Writes go to <db_path> in WAL mode with foreign_keys ON. The
    database file is created if missing; meta row inserted once
    on first event; events table appends.
    """
    def __init__(self, sid: str, db_path: Path,
                 *, meta: dict): ...

    # Per-thread connection helper. Caller does:
    #   conn = self._conn()
    #   conn.execute("INSERT INTO events ...", ...)
    #   conn.commit()
    def _conn(self) -> sqlite3.Connection: ...

    def record_llm(self, *, role, round, lane_idx, model,
                   request: dict, response: dict,
                   duration_ms: int): ...
    def record_tool(self, *, role, round, lane_idx,
                    tool, args, output, duration_ms,
                    error: Optional[str] = None): ...
    def record_node(self, *, role, kind, duration_ms=None): ...

    def finalize(self, *, status, finished_at, error=None): ...
    """Update meta with terminal status + run VACUUM."""

    def close(self) -> None: ...
    """Close all per-thread connections."""
```

**Implementation notes:**

- `db_path.parent.mkdir(parents=True, exist_ok=True)` so the
  recorder doesn't blow up if the consultation dir doesn't
  exist yet. (Storage layer normally creates it; this is
  belt-and-braces.)
- `request` / `response` dicts get `json.dumps(... ,
  default=str)` so any non-JSON-serializable values (datetimes
  etc.) get stringified gracefully.
- Per-thread connections via `threading.local()`. The recorder
  closes them all in `close()`.
- WAL mode set in the FIRST connection that opens the file;
  subsequent connections inherit.
- `finalize()` runs `PRAGMA wal_checkpoint(TRUNCATE); VACUUM;`
  to shrink the WAL log.

**Tests:** unit-only, no engine dependencies. Verify schema is
created, events insert correctly, multiple threads can write
concurrently without locking errors, ordering by ts is preserved,
finalize VACUUMs, schema_version is 1.

### Phase 2 — Plumb recorder into role nodes

Modify `consultants/engine/council.py`:

- `_single_shot(...)` gains an optional `recorder` + `role` +
  `round` arg; wraps `chat_client.chat(payload)` to call
  `recorder.record_llm(...)` after each call.
- `planner_node`, `critic_node`, `synthesizer_node` accept
  `recorder` and pass through.
- `researcher_node` is more involved: it needs to capture the
  loop's per-iteration messages. The simplest hook is to extend
  `agent_loop.runner.run_loop` with an optional `on_iter` callback
  that fires per iteration with `(payload, response, duration_ms)`,
  and per-tool-call.

Modify `claude_hooks/agent_loop/runner.py`:

- Add `on_iter: Optional[Callable]` kwarg.
- Fires with `(iter_idx, payload, response, duration_ms)` after
  each `chat_fn(payload)` returns.
- Tool execution already has its own logging in
  `execute_tool_calls`; mirror that to a `on_tool: Optional[Callable]`
  kwarg that fires with `(name, args_str, output, duration_ms,
  error)`.
- Both default to no-op for backward compat (caliber proxy + advisor
  don't need them).

Modify `consultants/server/runner.py:make_runner`:

- Instantiate a `MessageRecorder` per consultation.
- Pass it through `GraphDeps` to every role's wrapper closure.
- Researcher's `run_loop` call binds the recorder via `on_iter` /
  `on_tool` kwargs.

**Tests:** integration test with the in-process stub — assert that
a complete consultation produces a recorder buffer with N
`llm_call` events matching the role pattern, M `tool_call` events,
and `node_enter`/`node_exit` for each role.

### Phase 3 — Storage path + finalize

Modify `consultants/engine/storage.py`:

- Add `TRANSCRIPT_DB_FILENAME = "transcript.db"`.
- Extend `write_consultation` signature to accept an optional
  `recorder: Optional[MessageRecorder] = None`.
- When provided, the recorder has been writing to
  `<cwd>/.claude-hooks/consultants/<sid>/transcript.db` throughout
  the run. Storage's job is to call `recorder.finalize(status=...,
  finished_at=...)` so the meta row reflects the terminal state.

Modify `consultants/server/runner.py`:

- Construct the recorder BEFORE the graph stream loop so events
  land in real time (not batched at completion).
- Path: `Path(cwd) / ".claude-hooks" / "consultants" / sid /
  "transcript.db"`.
- Pass the recorder into `GraphDeps` so role nodes can call
  `record_llm` / `record_tool` directly.
- After the stream drains: `recorder.finalize(status=...,
  finished_at=time.time(), error=...)` then `recorder.close()`.

**Note:** real-time writes (vs batched) protect against the
"engine crashed mid-flight" case — the partial transcript.db is
inspectable post-mortem. SQLite WAL handles the in-flight rows
correctly even if the process is killed.

**Tests:** assert the .db file exists post-consultation, has the
right meta row (status='completed' or 'failed'), and the events
table count matches the number of LLM + tool calls the stub
runner made.

### Phase 4 — Reopen reads SQLite

Modify `consultants/server/app.py:_load_session_from_artifacts`:

- After the `metadata.json` load, also probe for
  `transcript.db`. When present, open it (read-only —
  `sqlite3.connect("file:...?mode=ro", uri=True)`) and run:

  ```sql
  SELECT request_json, response_json, role, round, lane_idx
  FROM events
  WHERE kind = 'llm_call'
  ORDER BY ts;
  ```

  Reconstruct two new SessionState fields:

  ```python
  _role_messages: dict[str, list[dict]]
  _role_lane_messages: dict[str, dict[int, list[dict]]]
  ```

  `_role_messages[role]` = the LAST llm_call's
  `request.messages` for that role + that response's assistant
  message. This is the "complete final thread" view, suitable for
  feeding the synthesizer or a re-engaged critic.

  `_role_lane_messages[role][lane_idx]` = same but per-lane,
  for the researcher fan-out case. Use this when the follow-up
  wants to extend a SPECIFIC lane.

- When `transcript.db` is missing (older v1.0 sessions),
  `_role_messages = None` and we fall back to today's
  turn-content reconstruction. Backward compatible.

Add to SessionState:

```python
_role_messages: Optional[dict[str, list[dict]]] = field(
    default=None, repr=False)
_role_lane_messages: Optional[dict[str, dict[int, list[dict]]]] = field(
    default=None, repr=False)
```

Both excluded from `public_dict()`.

**Tests:** seed a tiny `transcript.db` (write three llm_call rows
across 2 roles); call `_load_session_from_artifacts`; assert the
reconstructed `_role_messages` matches expectations. Test the
backward-compat path too: `metadata.json` present, no
`transcript.db` — `_role_messages` should be None and the loader
should still succeed.

### Phase 5 — Follow-up uses message threads

This is the user-visible quality fix.

Modify `consultants/server/runner.py:make_follow_up_runner`:

- When `parent_state._role_messages` is populated:
  - For the synthesizer node, build messages as
    `parent.synthesizer_messages + [{"role": "user", "content":
    follow_up_question}]` instead of
    `build_synthesizer_messages(...)` from scratch. The synthesizer
    sees the original conversation as a multi-turn chat.
  - For the researcher node (when it fires — only at high effort or
    when the follow-up question requires more evidence), build
    messages as `parent.researcher_messages + [{"role": "user",
    "content": follow_up_question}]`. Tools available, but the
    researcher can lean on the prior tool results in context
    instead of re-fetching.
- When `parent_state._role_messages` is None (older artifact, or
  follow-up of an in-memory parent that ran with v1.0 engine), fall
  back to today's behavior: `prior_research = list(parent.research)
  + [parent.final_answer block]` injected as plan_item.

Modify `consultants/engine/council.py`:

- Add `prior_messages: Optional[list[dict]] = None` arg to
  `synthesizer_node` and `researcher_node`. When set, use it as the
  message base instead of calling `build_*_messages(...)`.

The follow-up's planner is gated out at low/medium and uses
parent.plan as-is at high (no change).

**Tests:**
- Synthesizer follow-up with prior_messages: assert the LLM call
  payload is `[parent's messages... follow_up_user_msg]` shape.
- Researcher follow-up with prior_messages: same.
- Backward-compat: follow-up against an artifact without
  transcript.db falls back cleanly.
- E2E (live engine): warm follow-up vs cold-reopen follow-up
  produce equivalent answers (both within the warm path's wall
  ± 30%).

### Phase 6 — Trace consolidation

Decommission `~/.claude/consultants-traces/<sid>.jsonl`:

- `consultants/engine/trace.py:Tracer` is removed (or reduced to a
  no-op shim that logs a deprecation warning). The `MessageRecorder`
  in `transcript.db` is the single source of truth for the structured
  event log; the existing per-home-dir JSONL stream goes away.
- `CONSULTANTS_TRACE` env var becomes a no-op with a deprecation
  log.
- `--trace` / `--no-trace` CLI flags become no-ops with a
  deprecation log; remove in v1.2.
- Rewrite `scripts/consultants_trace_summary.py` to query
  `<cwd>/.claude-hooks/consultants/<sid>/transcript.db` via SQL
  (canonical waterfall query: `SELECT ts, role, kind, model, tool,
  duration_ms FROM events ORDER BY ts`). CLI signature can take
  either a sid (resolved via consultants config) or a direct path
  to a `.db` file.
- Rewrite `scripts/consultants_bench_row.py` similarly — the
  per-row aggregates (token sums, tool counts) become a SUM/COUNT
  group-by query.
- Update `scripts/consultants_benchmark.sh` to copy
  `transcript.db` into label dirs in place of the previous trace
  file. (Note: SQLite copying mid-flight needs `.backup` API to be
  safe, but post-completion it's a plain `cp`.)

**Tests:** trace_summary continues to print sensible waterfalls
when reading `transcript.db` via SQL.

### Phase 7 — CLI surface

Add `claude-consultants show --raw <sid>` (alongside the existing
`show`):

- Default `show` continues to print `summary.md` body.
- `--raw` dumps every row of `transcript.db`'s `events` table as
  pretty JSON (one event per line, ordered by ts) for inspection.
- `--raw --filter role=researcher` adds `WHERE role = ?` to the
  query.
- `--raw --filter kind=tool_call` adds `WHERE kind = ?`.
- `--raw --sql "<select>"` for power users who want arbitrary
  read-only SQL against the schema (open the db with
  `mode=ro` URI).

Useful for debugging engine behavior. Optional; can land in v1.2 if
schedule slips.

### Phase 8 — Documentation + privacy notes

Update:

- `CLAUDE.md` — note that `transcript.db` contains full LLM
  payloads (system prompts, tool results) and inherits the same
  privacy posture as the existing `transcript.md`. Confirm the
  consultants artifact dir is `.gitignore`d so the binary file
  doesn't accidentally land in version control.
- `docs/consultants-benchmarks.md` — note that benchmark sweeps
  copy `transcript.db` into the label dir, so committed
  benchmark labels include the full conversation in opaque form.
  Worth flagging that even though SQLite is opaque to text
  indexers, the file content is still the conversation — treat it
  as sensitive.
- `docs/benchmarks/EVALUATION.md` §10 (exclusion criteria) — no
  change needed; protocol stays.
- New: `docs/consultants-transcript-db-schema.md` — pin the SQL
  schema with examples. Schema bumps either add columns
  backward-compatibly within `schema_version=1` or bump the
  version and document the migration.

## Backward compatibility

All phases must keep existing artifacts working:

- A `metadata.json` written by v1.0 has no `transcript.db`
  sibling. Phase 4's reopen path detects missing `transcript.db`
  and falls back to today's turn-content reconstruction.
- A v1.0 engine reading a v1.1-written artifact ignores the new
  `.db` file (we don't add new keys to `metadata.json`).
- The `_role_messages` field on SessionState is opt-in (defaults to
  None); follow-up runner branches on its presence.

Schema versioning: the `meta` table carries a `schema_version`
column (default `1`). Future schema bumps either ALTER TABLE
backward-compatibly within v1 OR write a sidecar
`transcript_v2.db` and leave the v1 file in place. Readers
inspect `SELECT schema_version FROM meta` and branch.

## Risks & mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Disk bloat on long-lived projects | medium | low | document; ship `prune` in v1.2 |
| Token cost of feeding full prior thread to follow-up's LLM is too high | medium | medium | start by feeding only synthesizer's thread (smaller); measure; add `--compact-history` flag that summarizes prior thread to a fixed budget |
| Concurrent fan-out lanes lock the SQLite db | medium | medium | WAL mode + per-thread connections + `synchronous=NORMAL` — the recorder enforces both at open time, no caller can opt out |
| SQLite file corruption on engine kill mid-write | low | medium | WAL guarantees rollback to last commit; `finalize()` runs `wal_checkpoint(TRUNCATE)` so completed sessions are crash-clean. Partial dbs from killed runs are still openable read-only |
| `agent_loop.runner.run_loop` callback API change breaks caliber proxy / advisor | low | medium | new kwargs default to None; existing callers pass nothing |
| Schema drift across v1.1 / v1.2 / etc. | high (over time) | low | `schema_version` column + always-additive ALTER TABLE within a major version |
| Tool results in transcript.db leak sensitive code into committed benchmark labels | low | medium | doc note; SQLite opacity to grep/RAG indexers is the primary defense (the user's stated motivation for this format); consider an opt-in `--redact-tool-outputs` flag for the benchmark copy step |
| `sqlite3.connect()` from a thread that opened in another thread errors out | medium (during dev) | low | per-thread connections via `threading.local()` — covered in Phase 1 unit tests |

## Test plan

### Unit (per phase)

Phase 1: `tests/test_consultants_recorder.py` — schema, ordering,
thread-safety.

Phase 2: extend `tests/test_consultants_council.py` —
`record_llm` fired N times for N stub chat calls; `record_tool`
fired M times for M tool executions.

Phase 3: extend `tests/test_consultants_storage.py` — write +
re-read SQLite round-trips. Verify `meta.status` flips to
`completed`/`failed` correctly; `events` row counts match expected
LLM + tool call counts; `wal_checkpoint` shrinks the WAL log.

Phase 4: extend `tests/test_consultants_server.py:TestReopen` —
new test `test_reopen_loads_role_messages_from_transcript_db`.
Seed a tiny `transcript.db` in a temp dir, call
`_load_session_from_artifacts`, assert `_role_messages` /
`_role_lane_messages` are correctly populated. Also test the
backward-compat path (no .db file → fields are None, loader still
succeeds).

Phase 5: extend `tests/test_consultants_server.py:TestFollowUp` —
new test `test_follow_up_uses_prior_messages_when_available`.

### E2E (live engine, post-deploy)

After all phases land, run on solidpc:

1. Issue a consultation, capture the synthesizer's answer A.
2. Note the parent sid; close the session; restart the engine.
3. Issue an identical follow-up against the disk-reopened parent.
4. Capture the synthesizer's follow-up answer B_cold.
5. Issue a fresh consultation with the same question; close;
   without restart, follow up. Capture B_warm.
6. Assert: `B_cold` and `B_warm` produce equivalent answers under
   the EVALUATION.md §3 grading rubric (both same letter grade,
   both cite the same `path:line` references for at least 80% of
   claims).

If `B_cold ≠ B_warm`, the message-history reconstruction has a
gap; iterate until they converge.

### Regression

After all phases:

```
/root/anaconda3/envs/claude-hooks/bin/python -m pytest \
  tests/test_consultants_*.py -q
```

Expect 115 passing (Phase 0 baseline) → 130+ passing after Phase 5.

Also: full r1 sweep of one model (gemma4:31b-cloud) at the new
engine HEAD as a sanity check that Q1/Q2/Q3 grades hold.

## Open questions / decision log

These should be resolved by the implementer in the early phases.

1. **Should the recorder write incrementally (per-event fsync) or
   buffer + write-once at completion?** Recommendation: buffer +
   write-once. Per-event fsync would catch crashes mid-flight at
   ~10× the disk cost. We already lose mid-flight state on graph
   crashes; preserving it requires LangGraph checkpointer wiring
   (out of scope).

2. **Should `_role_messages` retain lane separation, or flatten?**
   Recommendation: store BOTH — `_role_messages[role]` flat for
   the simple "feed to follow-up" case, and
   `_role_lane_messages[role][lane_idx]` for advanced cases (e.g.
   "re-run lane 2 with a different model"). Costs nothing extra
   in memory.

3. **Follow-up's researcher: re-execute tools or trust prior
   results?** Recommendation: trust prior tool results when the
   subject codebase tag matches (i.e. the worktree commit is the
   same). Re-execute when the codebase has moved. Implement via a
   "subject_baseline_tag" field in the parent's metadata.json that
   the follow-up checks against current cwd.

4. **Does the synthesizer get the FULL parent thread or a
   summarized version?** Recommendation: full thread by default;
   add `--compact-history` flag that prepends a summary instead
   when the full thread would exceed N tokens (start with N=32k).

5. **Should v1.1 also touch the `transcript.md` rendered output,
   or leave it as-is?** Recommendation: leave `transcript.md`
   as-is. It's the human-readable view; `transcript.db` is the
   machine view. Two artifacts is fine.

## Out of scope for v1.1

- LangGraph checkpointer integration (would let us also recover
  mid-flight state on crashes — orthogonal feature).
- Compression of `transcript.db` (SQLite is already reasonably
  compact post-VACUUM; gzip would only buy ~30%).
- A "fork" command (`claude-consultants fork <sid> --at synthesizer
  --model X` to re-run the synthesizer with a different model on
  preserved upstream evidence). Builds on v1.1 but isn't part of
  it.
- Cross-cwd reopen (today and in v1.1: reopen requires `--cwd`
  pointing at the project; we don't have a global session
  registry).
- An external SQL viewer / web UI for browsing transcript.db
  files. CLI `show --raw` covers the engineering need; a real UI
  is post-v1.1.

## Phase ordering for the next session

Recommended commit cadence:

1. **Phase 1** — recorder + unit tests. Small, mergeable. Commit.
2. **Phase 2** — plumb through nodes + agent_loop callbacks. The
   caliber/advisor regression test must pass unchanged. Commit.
3. **Phase 3** — storage write. Verify `transcript.db` appears
   next to `metadata.json` after a stub consultation, with WAL
   mode set and `meta.status='completed'`. Commit.
4. **Phase 4** — reopen reads SQLite. Backward-compat test with a
   v1.0-shape artifact (no .db) is critical. Commit.
5. **Phase 5** — follow-up uses prior messages. This is the user-
   visible quality fix; E2E test on solidpc here. Commit.
6. **Phase 6** — trace consolidation + script updates. Commit.
7. **Phase 7** — `show --raw`. Commit if time, defer to v1.2 if
   not.
8. **Phase 8** — docs. Commit before pushing.

After phase 5, the user-visible enhancement is done. Phases 6-8
are cleanup + ergonomics; can ship as a follow-on commit.

## Pre-implementation checklist for the next session

Before writing any code:

1. [ ] `git checkout dev && git pull` to confirm at or beyond
   commit `17e8c2c`.
2. [ ] `systemctl --user is-active claude-hooks-consultants` should
   report `active`. Restart after each phase that changes engine
   code.
3. [ ] `/root/anaconda3/envs/claude-hooks/bin/python -m pytest
   tests/test_consultants_*.py -q` should print 115 passed (the
   v1.0 baseline). If it doesn't, fix the regression before
   starting v1.1 work.
4. [ ] Read this whole plan top to bottom. Resolve any ambiguity
   in §"Open questions" before phase 1.
5. [ ] Check `docs/benchmarks/CURRENT_BASELINE`; if it points at
   `bench-baseline-2026-05-07`, you're aligned. Engine code
   changes during v1.1 don't require a new baseline tag (subject
   codebase doesn't change).

## Reference: where the relevant code lives

- Recorder will go: `consultants/engine/recorder.py` (new)
- Council nodes: `consultants/engine/council.py` (planner_node,
  researcher_node, critic_node, synthesizer_node, _single_shot)
- Loop callback hook: `claude_hooks/agent_loop/runner.py:run_loop`
- Storage: `consultants/engine/storage.py` (write_consultation,
  `TRANSCRIPT_DB_FILENAME = "transcript.db"` constant to add)
- Runner: `consultants/server/runner.py` (make_runner,
  make_follow_up_runner)
- Reopen path: `consultants/server/app.py`
  (_load_session_from_artifacts, _resolve_parent_for_follow_up)
- Existing tracer to merge: `consultants/engine/trace.py`
- CLI: `consultants/cli.py`
- Tests: `tests/test_consultants_*.py`

## How to know v1.1 is done

When all of these are true:

- `transcript.db` is written next to `summary.md` /
  `transcript.md` / `metadata.json` for every consultation
  (including follow-ups), with WAL mode and `meta.status` set to
  `completed` or `failed` at termination.
- The schema is documented in
  `docs/consultants-transcript-db-schema.md` with the canonical
  reconstruction queries (per-role thread, per-lane thread).
- A consultation closed and reopened from disk produces a
  follow-up answer that grades equivalently to a follow-up
  against the warm parent (per EVALUATION.md §3).
- 130+ tests pass.
- The deprecation messages on `CONSULTANTS_TRACE` /
  `--trace` / `--no-trace` are wired (or those flags are
  removed entirely if we decide v1.1 is allowed to break that
  contract — flag this as a decision in §"Open questions").
- E2E run on solidpc confirms a follow-up with `transcript.db`
  reuses prior tool results visibly: the follow-up's `events`
  table has fewer `tool_call` rows than a comparable fresh
  consultation against the same question
  (`SELECT COUNT(*) FROM events WHERE kind='tool_call'`).
- `find <project>/.claude-hooks/consultants -name '*.db' | xargs
  -I{} ripgrep -l 'pattern' {}` produces no matches — confirming
  text indexers don't pierce the SQLite container (the privacy
  property the user asked for).
