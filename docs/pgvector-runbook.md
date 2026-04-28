# pgvector runbook

End-to-end guide for running claude-hooks against a Postgres + pgvector
backend instead of (or alongside) Qdrant + Memory KG. Covers install,
data migration with delta-sync, the bench harness, and the practical
embedder pick.

> Status: pgvector backend is **optional** and disabled by default. Qdrant
> + Memory KG remain the supported defaults. This runbook documents the
> migration path for users who want a single Postgres-backed store.

---

## TL;DR

```bash
# 0. Install the pgvector Python deps
pip install -r requirements.txt -r requirements-pgvector.txt

# 1. Bring up the docker stack
cd /shared/config/mcp-pgvector
cp .env.example .env && ${EDITOR} .env       # set POSTGRES_PASSWORD
docker compose up -d
docker exec mcp-pgvector psql -U claude -d memory -c "\dx"   # vector 0.8.x present?

# 2. Migrate Qdrant + Memory-KG into pgvector
cd /srv/dev-disk-by-label-opt/dev/claude-hooks
python scripts/migrate_to_pgvector.py --embedder nomic --source all

# 3. Benchmark (optional — only needed when changing embedder)
python scripts/bench_recall.py --warm 3 --repeat 8

# 4. Wire it up: copy the pgvector block from
#    config/claude-hooks.example.json into your config/claude-hooks.json,
#    set the dsn + ollama url, set "enabled": true.
```

---

## 1. Install (docker compose)

The compose file at `/shared/config/mcp-pgvector/docker-compose.yaml`
boots the official `pgvector/pgvector:pg16` image with a tuned
`postgresql.conf`. It uses `network_mode: host` for parity with the
existing `mcp-qdrant` and `mcp-memory` services.

Key files (all live under `/shared/config/mcp-pgvector/`):

| Path | Purpose |
|---|---|
| `docker-compose.yaml` | Image, env, volumes, healthcheck |
| `.env.example` | Copy to `.env`; supplies POSTGRES_USER/PASSWORD/DB |
| `postgresql.conf` | Tuned for vector workload — see "Tuning" below |
| `init/01_extensions.sql` | `CREATE EXTENSION vector, pg_trgm, btree_gin` |
| `init/02_schema.sql` | Model-agnostic tables (kg_entities, kg_relations, migration_state) |

The vector tables (`memories_<model>`, `kg_observations_<model>`) are
**not** created by the init scripts — their dimension depends on the
chosen embedder and they're created on-demand by the migration script.
This lets you keep multiple model variants side-by-side for benchmarking.

### Tuning summary (postgresql.conf)

| Knob | Value | Why |
|---|---|---|
| `shared_buffers` | 4 GB | Bounded slice on a shared host |
| `effective_cache_size` | 12 GB | Lets the planner trust the OS page cache |
| `maintenance_work_mem` | 2 GB | Speeds up HNSW index builds |
| `work_mem` | 64 MB | Per-op cap; small by design |
| `random_page_cost` | 1.1 | Assumes SSD/NVMe |
| `effective_io_concurrency` | 200 | NVMe-friendly |
| `jit` | on | Postgres 16 JIT helps pgvector modestly |

These are sized for solidPC's hardware (125 GB RAM, NVMe-backed bcache).
For other hosts, scale `shared_buffers` to ~25% of available RAM if the
DB is dedicated, lower if it's sharing.

---

## 2. Migration

`scripts/migrate_to_pgvector.py` is idempotent on every level — the same
script handles initial migration **and** ongoing delta sync. Key flags:

```bash
# Initial import — all sources, default embedder (nomic).
python scripts/migrate_to_pgvector.py

# Bench mode — populate every embedder's tables in one pass.
python scripts/migrate_to_pgvector.py --embedder all --source all

# Delta sync — re-run; existing rows are skipped via content_hash.
python scripts/migrate_to_pgvector.py --embedder nomic

# Just count, no writes.
python scripts/migrate_to_pgvector.py --embedder nomic --dry-run
```

### What goes where

| Source | Pgvector table | Idempotency key |
|---|---|---|
| Qdrant collection `memory` | `memories_<model>` | `content_hash` (SHA-256 of normalised content) |
| Memory-KG entities | `kg_entities` (model-agnostic) | `name` UNIQUE |
| Memory-KG observations | `kg_observations_<model>` | `(entity_id, content_hash)` UNIQUE |
| Memory-KG relations | `kg_relations` (model-agnostic) | `(from, to, type)` UNIQUE |

Re-runs are cheap: `ON CONFLICT DO NOTHING` skips existing content,
`ON CONFLICT DO UPDATE` for entities lets you correct types in place.
The `migration_state` table records `(source, model, last_synced_at,
rows_synced)` so you can audit what's been ingested.

### Why per-model tables?

Embedding dimension is a column type in pgvector — you can't change it
without rebuilding the index. Suffixing tables by model (`memories_nomic`,
`memories_arctic`, …) lets you A/B test cleanly. The runbook for the
final pick is to alias one variant to the canonical name (or just point
the provider config at `memories_nomic` directly — that's what the
default config does).

### Per-model context limits

| Model | Native ctx (tokens) | Script-side max_chars cap | Why |
|---|---|---|---|
| `locusai/all-minilm-l6-v2` | 256 | 400 | dense text tokenises tight; truncation **does** lose information |
| `nomic-embed-text` | 8192 | 5000 | safe headroom for code/path-heavy memories |
| `snowflake-arctic-embed2` | 8192 | 5000 | same |

Truncation only affects the embedding — the full text is stored intact
in the `content` column.

---

## 3. Benchmark results (2026-04-28)

Run on solidPC. 17 representative queries × 8 timed runs, k=5, after
3 warm-up runs per (provider, query):

| Provider | total p50 | total p95 | embed p50 | DB recall p50 | recall@5 vs Qdrant |
|---|---|---|---|---|---|
| Qdrant baseline (MCP) | 86 ms | 126 ms | — | — | — |
| pgvector + minilm (384d) | **18 ms** | 34 ms | 17 ms | 0.9 ms | 0.32 |
| pgvector + nomic (768d) | 35 ms | 44 ms | 33 ms | 2.1 ms | 0.22 |
| pgvector + arctic (1024d) | 208 ms | 251 ms | 206 ms | 1.4 ms | 0.23 |

### Reading the numbers

- **DB recall is sub-3 ms** for every model on this dataset (148 memories,
  526 observations). Postgres + HNSW is not the bottleneck — the budget
  is spent in the embedder.
- **`recall@5 vs Qdrant`** measures *similarity of result sets*, **not
  quality**. Qdrant isn't the gold standard — it's a different model
  with a 0.40 score-threshold filter that drops borderline results.
  The 0.22-0.32 spread tells you the result sets diverge, not which
  is better.

### Qualitative quality (top-1 inspection on real queries)

| Query | nomic top-1 | arctic top-1 |
|---|---|---|
| "claudemem reindex windows administrator cmd window" | bug-167 ✓ | bug-167 ✓ |
| "solidPC docker MCP services and their ports" | "How to add a new MCP server…" (procedure) | "MCP servers architecture on solidPC…" (canonical) ✓ |
| "how does claude-hooks recall flow work?" | recent turn dumps | recent turn dumps |

Arctic shows a visible quality edge on broad architecture queries.
Nomic is competitive on specific bug/fact queries.

### Pick

| Use case | Model |
|---|---|
| Default for the recall hook (every UserPromptSubmit) | **`nomic-embed-text`** |
| Migrations / overnight backfills / quality-first | `snowflake-arctic-embed2` |
| Don't pick | `all-MiniLM-L6-v2` — 256-token cap is a recall ceiling |

The bench harness lives at `scripts/bench_recall.py` — re-run it any
time you want to retest with different embedders, query sets, or HNSW
parameters. JSON output (default `bench-results.json`) has full raw
timings for percentile recomputation.

---

## 4. Wiring into claude-hooks

Open `config/claude-hooks.json` (or copy from `config/claude-hooks.example.json`)
and set the pgvector block:

```json
"pgvector": {
  "enabled": true,
  "dsn": "postgresql://claude:YOUR_PASSWORD@127.0.0.1:5432/memory",
  "table": "memories_nomic",
  "embedder": "ollama",
  "embedder_options": {
    "url": "http://YOUR-OLLAMA-HOST:11434/api/embeddings",
    "model": "nomic-embed-text"
  },
  "recall_k": 5,
  "store_mode": "auto",
  "timeout": 10.0
}
```

The provider auto-creates the table on first store/recall using the
embedder's vector dimension. If you migrated with the script above,
the table already exists and the auto-create is a no-op.

### Running pgvector alongside Qdrant + Memory KG

All three providers can be enabled simultaneously. The dispatcher
fans out recall in parallel and merges the result blocks into the
prompt. There's no need for an exclusive cutover — you can run
pgvector in shadow mode (just enabled, no production reliance)
until you're confident.

When you're ready to retire Qdrant / Memory-KG, set their
`"enabled": false` and remove their entries from `~/.claude.json`.
The data stays in their containers — re-enabling them is a one-line
config change away.

---

## 5. Batch API

`PgvectorProvider` overrides the default `batch_recall` and
`batch_store` methods to use real Ollama batch (`/api/embed` with
`input: [text]*N`) and `executemany`-based DB inserts. The win is
amortising the embedder model load + HTTP round-trip — sequential
N×single recall hits the per-call overhead N times.

Measured on the same 8 queries × pgvector_nomic on solidPC:

| Mode | total | per query | speedup |
|---|---|---|---|
| N×single recall | 98.1 ms | 12.3 ms | 1.0× |
| `batch_recall(N)` | 41.1 ms | 5.1 ms | **2.39×** |

Top-1 results are identical — batching is a pure latency optimisation,
not a quality tradeoff.

---

## 6. Operational notes

### Schema migrations

The schema in `init/02_schema.sql` and the per-model SQL in
`scripts/migrate_to_pgvector.py:schema_sql_for_model` are both
idempotent (`CREATE TABLE IF NOT EXISTS` everywhere). To pick up a
schema change, drop the affected tables and re-run the migration.

### Backups

`data/` is a host-mounted volume. Standard Postgres backup applies:

```bash
docker exec mcp-pgvector pg_dump -U claude memory > /backup/pgvector_$(date +%F).sql
```

### Resetting

```bash
# Truncate everything but keep the tables.
docker exec -e PGPASSWORD=$POSTGRES_PASSWORD mcp-pgvector psql -U claude -d memory -c "
  TRUNCATE memories_minilm, memories_nomic, memories_arctic,
           kg_observations_minilm, kg_observations_nomic, kg_observations_arctic,
           kg_relations, kg_entities, migration_state RESTART IDENTITY CASCADE;
"

# Or, nuke the data dir and let init/ recreate the base schema:
docker compose down
sudo rm -rf data/* data/.*
docker compose up -d
```

### Connecting from pandorum / other hosts

The DSN in `claude-hooks.json` can point at solidPC's LAN IP
(`postgresql://claude:PASS@192.168.178.2:5432/memory`). The compose
binds Postgres to all interfaces on the host network — locked at the
firewall/router boundary, not at Postgres itself. If you want a
tighter listen scope, set `listen_addresses = '192.168.178.2,127.0.0.1'`
in `postgresql.conf` and restart the container.

---

## 7. Hard cutover walkthrough — replacing Qdrant + Memory KG

This is the procedure used on solidpc + pandorum on 2026-04-28 to retire
the two MCP-backed memory services. Keep both `mcp-qdrant` and
`mcp-memory` containers **stopped, not removed** — rollback is a single
`docker compose start` away.

### A. Pre-flight (each host)

```bash
# Solidpc-style hosts: psycopg already in conda env
/root/anaconda3/envs/claude-hooks/bin/python -c "import psycopg; print(psycopg.__version__)"

# Pandorum-style hosts: install if missing
C:\Users\you\miniconda3\envs\claude-hooks\python.exe -m pip install "psycopg[binary]"

# Network: the host needs reachability to the pgvector instance
# (default port 5432 on the LAN IP).
nc -zv 192.168.178.2 5432            # POSIX
Test-NetConnection -ComputerName 192.168.178.2 -Port 5432   # PowerShell
```

### B. Edit `config/claude-hooks.json` (per host)

Disable the two old providers, enable pgvector with hybrid recall:

```json
"providers": {
  "qdrant":    { "enabled": false, ... },
  "memory_kg": { "enabled": false, ... },
  "pgvector": {
    "enabled": true,
    "dsn": "postgresql://claude:PASS@192.168.178.2:5432/memory",
    "table": "memories_nomic",
    "additional_tables": ["kg_observations_nomic"],
    "embedder": "ollama",
    "embedder_options": {
      "url": "http://192.168.178.2:11434/api/embeddings",
      "model": "nomic-embed-text"
    },
    "recall_k": 5,
    "store_mode": "auto",
    "timeout": 10.0
  }
}
```

The `additional_tables` list makes the single pgvector instance hybrid-search both
the Qdrant-equivalent (`memories_nomic`) and the Memory-KG-equivalent
(`kg_observations_nomic`) and merge results by cosine distance.

Update `hooks.user_prompt_submit.include_providers`:
```json
"include_providers": ["pgvector"]
```

### C. Edit `~/.claude.json` — remove the dead MCP entries

Claude Code probes every entry in `mcpServers` at startup; dead URLs
slow startup and add log noise. Remove the `qdrant` and `memory` keys
(back up `~/.claude.json` first). On Linux:

```bash
cp ~/.claude.json ~/.claude.json.bak-$(date +%F)
python3 -c "
import json, os
p = os.path.expanduser('~/.claude.json')
cfg = json.load(open(p))
for k in ('qdrant', 'memory'):
    cfg.get('mcpServers', {}).pop(k, None)
json.dump(cfg, open(p,'w'), indent=2)
"
```

### D. Stop the old MCP containers (data preserved)

```bash
cd /shared/config/mcp-qdrant && docker compose stop
cd /shared/config/mcp-memory && docker compose stop
```

**Do not run `docker compose down -v`** — that would delete the data
volumes and break the rollback path. Plain `stop` keeps everything.

### E. Restart the claude-hooks daemon

The daemon caches modules in memory. Without a restart it keeps using
the pre-cutover dispatcher and config:

```bash
# Linux/macOS
bin/claude-hooks-daemon-ctl restart

# Windows
bin\claude-hooks-daemon-ctl.cmd restart
# If that reports "already responding" without a clean stop:
#   wmic process where "ProcessId=<pid>" get CommandLine
#   taskkill /F /PID <pid>
#   bin\claude-hooks-daemon-ctl.cmd start
```

### F. Smoke-test the live hook

```bash
echo '{"hook_event_name":"UserPromptSubmit","prompt":"verify pgvector hybrid recall","cwd":"'$(pwd)'","session_id":"smoke","transcript_path":"/tmp/x.jsonl"}' \
  | bin/claude-hook UserPromptSubmit
```

The output should contain a single recall block whose summary line names
the active backend explicitly:

```
## Recalled memory

_5 hit(s) from Postgres pgvector — claude-hooks_

### Postgres pgvector (5)
- ...
```

If you see `_N hit(s) from 0 providers_` or get an empty body, the
daemon didn't restart cleanly — kill the process and re-run step E.

### Rollback

If anything goes wrong, the cutover is reversible in seconds:

```bash
# 1. Restart the old containers (data still on disk)
cd /shared/config/mcp-qdrant && docker compose start
cd /shared/config/mcp-memory && docker compose start

# 2. Restore the backed-up configs
cp config/claude-hooks.json.bak-<TS> config/claude-hooks.json
cp ~/.claude.json.bak-<TS> ~/.claude.json

# 3. Restart the daemon
bin/claude-hooks-daemon-ctl restart
```

The pgvector container can stay running during rollback — it's idle
when no provider points at it. Drop it later with `docker compose down`
under `/shared/config/mcp-pgvector/` if you decide pgvector wasn't the
right call.
