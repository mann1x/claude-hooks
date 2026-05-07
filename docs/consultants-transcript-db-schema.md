# `transcript.db` schema (v1.1)

Every consultation produces a SQLite sidecar at
`<cwd>/.claude-hooks/consultants/<sid>/transcript.db`, alongside
the existing `summary.md`, `transcript.md`, and `metadata.json`.
The file holds the structured event log — every LLM call, every
tool execution, and every node enter/exit boundary the council
fired. It's the canonical machine-readable view of a session;
`summary.md` and `transcript.md` are derived markdown.

This file documents the schema for tooling that wants to query
the database directly. The reader of record is
`consultants/engine/recorder.py:load_role_messages` (used by the
reopen path); the engineer-facing surface is
`bin/claude-consultants show --raw <sid>` (Phase 7) and
`scripts/consultants_trace_summary.py <sid>` (Phase 6).

## Why SQLite

Three properties drove the choice over JSONL:

1. **Privacy from text indexers.** `transcript.db` is a binary
   file. `claudemem reindex`, ripgrep, RAG ingestors, and
   file-watch crawlers walk text — they don't pierce SQLite. Full
   LLM payloads (system prompts, tool results that may carry
   sensitive code excerpts) don't accidentally surface in
   unrelated tools that walk the project tree.
2. **Concurrent fan-out writes** are first-class via WAL mode.
   The recorder hands each researcher fan-out lane its own
   connection from a `threading.local` pool; SQLite serializes
   commits internally. JSONL would have needed our own per-line
   lock.
3. **Queries are SQL.** "Give me every llm_call for the
   researcher in lane 1" is one `SELECT … WHERE … ORDER BY ts`
   instead of streaming jq filters. The reopen path leans on
   this for thread reconstruction.

## File layout on disk

```
<cwd>/.claude-hooks/consultants/<sid>/
├── metadata.json       — top-level structured metadata
├── summary.md          — synthesizer's final answer
├── transcript.md       — sectioned by role (human-readable)
└── transcript.db       — this file (event log + meta header)
```

`.claude-hooks/consultants/` is `.gitignore`d project-wide —
session artifacts carry full LLM payloads and aren't intended for
commit. Benchmark labels under `docs/benchmarks/<host>/` are the
exception: those are committed deliberately as audit data and
contain copies of the per-session artifacts (including
`transcript.db`).

## Schema (v1)

```sql
-- One row per consultation. Mirrors metadata.json's header so the
-- .db file is independently readable. schema_version pins the
-- format; future bumps either ALTER TABLE backward-compatibly or
-- write a sidecar transcript_v2.db.
CREATE TABLE meta (
    schema_version       INTEGER NOT NULL DEFAULT 1,
    sid                  TEXT    NOT NULL,
    cwd                  TEXT    NOT NULL,
    question             TEXT    NOT NULL,
    effort               TEXT    NOT NULL,        -- low|medium|high|max
    topology             TEXT    NOT NULL,        -- council
    parent_sid           TEXT,                    -- for follow-ups
    started_at           REAL    NOT NULL,
    finished_at          REAL,                    -- NULL while running
    status               TEXT    NOT NULL,        -- running|completed|failed
    error                TEXT,                    -- terminal error if any
    subject_baseline_tag TEXT,                    -- git tag, optional
    models_json          TEXT    NOT NULL         -- {"role":"tag",...}
);

-- One row per recordable event. INSERTs are append-only.
CREATE TABLE events (
    event_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                REAL NOT NULL,            -- wallclock epoch seconds
    kind              TEXT NOT NULL,            -- llm_call|tool_call|node_enter|node_exit
    role              TEXT NOT NULL,            -- planner|researcher|critic|synthesizer
    round             INTEGER NOT NULL DEFAULT 1,
    lane_idx          INTEGER,                  -- NULL except researcher fan-out

    -- llm_call columns. NULL for non-llm rows.
    model             TEXT,
    request_json      TEXT,                     -- full request dict, JSON-encoded
    response_json     TEXT,                     -- full response dict, JSON-encoded
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,

    -- tool_call columns. NULL for non-tool rows.
    tool              TEXT,
    args              TEXT,                     -- raw arguments string
    output            TEXT,                     -- tool result string
    output_chars      INTEGER,                  -- len(output) at write time

    -- Both kinds: optional duration + error.
    duration_ms       INTEGER,
    error             TEXT
);

CREATE INDEX idx_events_role_kind ON events(role, kind, round);
CREATE INDEX idx_events_ts        ON events(ts);
```

### Pragmas applied at open

```sql
PRAGMA journal_mode = WAL;       -- concurrent fan-out writes
PRAGMA synchronous = NORMAL;     -- fsync on commit, not page write
PRAGMA foreign_keys = ON;        -- belt-and-braces; future-proofing
PRAGMA temp_store = MEMORY;
```

WAL is sticky: the first connection to open the file sets it; all
subsequent connections inherit. Connections in the recorder use
`check_same_thread=False` so the finalizer thread can close peer
connections at teardown (otherwise `wal_checkpoint(TRUNCATE)`
silently downgrades to PASSIVE and the WAL never shrinks — see
the 2026-05-07 fix in `9a7c659`).

### Finalize order

The recorder's `finalize()` does:

1. `UPDATE meta SET status, finished_at, error WHERE sid = ?`
2. Close every peer thread's connection.
3. `VACUUM;` — rewrites the main DB compactly. **In WAL mode VACUUM
   writes its compaction output through the WAL**, so this step
   inflates the WAL.
4. `PRAGMA wal_checkpoint(TRUNCATE);` — folds the WAL back into the
   main DB and shrinks it to zero bytes.

Step order matters. A pre-VACUUM TRUNCATE is immediately re-filled
by VACUUM; reverse order leaves a fat WAL on disk. Both calls cost
~50 ms total.

## Canonical queries

### Per-role thread reconstruction (used by `_load_session_from_artifacts`)

```sql
SELECT request_json, response_json, role, round, lane_idx
  FROM events
 WHERE kind = 'llm_call'
 ORDER BY ts;
```

For each `(role, lane_idx)` group, the FINAL row's
`request_json.messages` is the complete conversation up to that
LLM call (the request's messages list at iter N+1 already
contains everything from iter N plus that round's tool results).
Concatenate the response's `choices[0].message` to get the full
thread.

```python
# pseudo-code (real reader: consultants/engine/recorder.py:load_role_messages)
last_by_role = {}
last_by_lane = {}
for req_j, resp_j, role, _round, lane_idx in rows:
    req = json.loads(req_j)
    resp = json.loads(resp_j)
    last_by_role[role] = (req, resp)
    if lane_idx is not None:
        last_by_lane.setdefault(role, {})[int(lane_idx)] = (req, resp)

def thread(req, resp):
    return req["messages"] + [resp["choices"][0]["message"]]
```

### Per-role wall + token sums (the waterfall)

```sql
SELECT
    role,
    SUM(CASE WHEN kind='node_exit' THEN duration_ms ELSE 0 END) AS wall_ms,
    SUM(CASE WHEN kind='llm_call'  THEN 1 ELSE 0 END) AS llm_count,
    SUM(CASE WHEN kind='llm_call'  THEN prompt_tokens ELSE 0 END) AS prompt_tok,
    SUM(CASE WHEN kind='llm_call'  THEN completion_tokens ELSE 0 END) AS completion_tok,
    SUM(CASE WHEN kind='tool_call' THEN 1 ELSE 0 END) AS tool_count
  FROM events
 GROUP BY role;
```

`scripts/consultants_trace_summary.py` runs this shape against
the .db to render the waterfall.

### Tool calls grouped by tool (debug "what files did we read?")

```sql
SELECT tool, COUNT(*) AS n, SUM(duration_ms) AS total_ms,
       SUM(output_chars) AS total_chars
  FROM events
 WHERE kind = 'tool_call' AND role = 'researcher'
 GROUP BY tool
 ORDER BY total_ms DESC;
```

### Find which LLM call retried after which tool calls

```sql
SELECT ts, role, kind, model, tool
  FROM events
 WHERE role = 'researcher' AND lane_idx = 0
 ORDER BY ts;
```

Reads the lane-0 timeline as a flat audit log.

## Disk size

From real runs on solidpc:

- Smoke (`--effort low`, no critic): ~70–330 KB.
- Audit-medium: ~500 KB.
- Audit-high (worst seen, csl-...faa2): ~700 KB raw → ~330 KB
  post-VACUUM.

A project that runs ~100 consultations/year sits at well under
100 MB. A `claude-consultants prune --older-than 30d` maintenance
command is planned for v1.2 if growth becomes a concern; for now
the per-session disk cost is negligible compared to the audit
value.

## Backward compatibility

- A v1.0-shape session (no `transcript.db` in the artifact dir)
  is still readable: `_load_session_from_artifacts` falls back to
  reconstructing plan / research / critique from
  `metadata.json`'s `turns` field. `_role_messages` and
  `_role_lane_messages` stay `None` and the follow-up runner
  branches on their presence to use the legacy build_*_messages
  path.
- A v1.0 engine reading a v1.1-written session ignores the
  `transcript.db` file (no new keys were added to
  `metadata.json`).
- Schema v1 is the only schema as of this commit. Future bumps
  either ALTER TABLE within v1 or write a sidecar
  `transcript_v2.db` and leave the v1 file in place; readers
  branch on `SELECT schema_version FROM meta`.

## Schema versioning

- v1.1 ships **schema_version = 1**.
- Adding a column: ALTER TABLE backward-compatibly within v1.
  Readers must tolerate the column being absent on older files
  (use `SELECT col FROM events LIMIT 1` defensively or check
  `PRAGMA table_info(events)`).
- Renaming or removing a column: bump to schema_version = 2,
  write to `transcript_v2.db`, leave the v1 file in place.
- Document the change in this file under a "## Schema v2" header.

## Privacy posture

`transcript.db` contains **full LLM payloads** — system prompts,
user messages, assistant turns including reasoning where the model
emits it, and complete tool results. Treat it with the same
caution as `transcript.md`:

- Project-level `.gitignore` excludes `.claude-hooks/consultants/`.
- Benchmark labels under `docs/benchmarks/` may contain copies of
  these files for audit reproducibility — those are committed
  deliberately. Avoid putting credentials, tokens, or untrusted
  user input into a benchmark question if you intend to publish
  the results.
- The CLI's `show --raw <sid>` prints raw JSON-encoded payloads to
  stdout. Pipe it through redaction tooling if you intend to share
  the output.

The whole reason we picked SQLite over JSONL is to keep the data
opaque to incidental indexers (claudemem, RAG, ripgrep). It is
not a security boundary — anyone with read access to the file has
the data.
