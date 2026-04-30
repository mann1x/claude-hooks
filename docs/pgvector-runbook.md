# pgvector runbook

End-to-end guide for running claude-hooks against a Postgres + pgvector
backend instead of (or alongside) Qdrant + Memory KG. Covers install,
data migration with delta-sync, the bench harness, and the practical
embedder pick.

> Status: pgvector backend is **optional** and disabled by default. Qdrant
> + Memory KG remain the supported defaults. This runbook documents the
> migration path for users who want a single Postgres-backed store.

> ⚠ **Vector-space mismatch is a silent footgun.** If you change
> `embedder_options.model`, you MUST re-embed the corpus before flipping
> the active config. Embeddings from different models live in
> incompatible vector spaces — recall against a `memories_X` table
> populated with model A but queried with model B's embeddings returns
> garbage with no error. Use the **[Swapping the embedding
> model](#5-swapping-the-embedding-model)** recipe.

---

## TL;DR

The fast path — `install.py` handles everything from step 2 onward
(probes the DSN, pulls the embedder, creates the qwen3 + KG schema,
drops the system-wide `pgvector-mcp` launcher, registers it in
`~/.claude.json`):

```bash
# 0. Bring up the pgvector docker stack
cd /shared/config/mcp-pgvector
cp .env.example .env && ${EDITOR} .env       # set POSTGRES_PASSWORD
docker compose up -d
docker exec mcp-pgvector psql -U claude -d memory -c "\dx"   # vector 0.8.x present?

# 1. Run the claude-hooks installer — it'll prompt for the DSN,
#    everything else uses sensible defaults (qwen3-embedding:0.6b,
#    memories_qwen3, kg_observations_qwen3, local Ollama).
cd /srv/dev-disk-by-label-opt/dev/claude-hooks
python install.py
```

That's it for fresh installs. The runbook below covers the
manual / advanced paths: bulk-migrating existing Qdrant + Memory-KG
data, swapping the embedder, and the hard cutover playbook.

```bash
# Manual / advanced — only when install.py's auto-flow doesn't fit.
pip install -r requirements.txt -r requirements-pgvector.txt
python scripts/migrate_to_pgvector.py --embedder qwen3 --source all
python scripts/bench_recall.py --warm 3 --repeat 8     # benchmark before swapping models
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
| `qwen3-embedding:0.6b` | 16384 | 30000 | default since 2026-04-28; runs at 16k (half its native 32k) so the daemon and the caliber proxy share one KV cache |

Truncation only affects the embedding — the full text is stored intact
in the `content` column.

When changing the embedder via the active config, mirror these in
`embedder_options.num_ctx` and `embedder_options.max_chars` —
`OllamaEmbedder` defaults are `num_ctx=8192` / `max_chars=16000`,
which silently caps a 32k-ctx model at 8k. The example config ships
the qwen3 values; nomic/arctic do not need overrides.

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
| pgvector + qwen3 (1024d) | 87 ms | 104 ms | 85 ms | 1.6 ms | — |

`qwen3` numbers are from a re-bench on 2026-04-28 against the same
17-query set, after the swap to `qwen3-embedding:0.6b`. Embed latency
sits between nomic and arctic; absolute total stays under 100 ms.
Quality wins on niche queries — see the side-by-side block below.

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
| Default for the recall hook (every UserPromptSubmit) | **`qwen3-embedding:0.6b`** — 32k native ctx, tight cosine on niche queries, ~85 ms p50 embed |
| Speed-first if your corpus is short turns and you can live with 8k ctx | `nomic-embed-text` |
| Quality-first overnight backfills | `snowflake-arctic-embed2` (slowest embed, but good ranking) |
| Don't pick | `all-MiniLM-L6-v2` — 256-token cap is a recall ceiling |

The default flipped from `nomic-embed-text` to `qwen3-embedding:0.6b`
on 2026-04-28 after a side-by-side bench:

| Query | nomic top-1 distance | qwen3 top-1 distance | Winner |
|---|---|---|---|
| "how to suppress windows console window when spawning subprocess" | 0.414 | **0.328** | qwen3 — same row, tighter signal |
| "pandorum training on RTX 5080 GPU" | 0.360 (off-topic user profile) | **0.284** (GPU monitoring rule) | qwen3 — actually relevant |
| "OllamaEmbedder default num_ctx and max_chars values" | 0.396 (off-topic) | **0.309** (embedder config) | qwen3 |
| "pgvector content_hash dedup ON CONFLICT" | **0.304** | 0.372 | nomic — exact lexical match |

Qwen3's edge is consistent on intent/architecture queries; nomic still
wins on lexical-match queries where the literal string is in the
corpus. The 32k ctx is the tie-breaker for long Stop summaries.

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
  "table": "memories_qwen3",
  "additional_tables": ["kg_observations_qwen3"],
  "embedder": "ollama",
  "embedder_options": {
    "url": "http://YOUR-OLLAMA-HOST:11434/api/embeddings",
    "model": "qwen3-embedding:0.6b",
    "num_ctx": 16384,
    "max_chars": 30000,
    "timeout": 30.0
  },
  "recall_k": 5,
  "store_mode": "auto",
  "timeout": 10.0
}
```

The provider auto-creates the table on first store/recall using the
embedder's vector dimension. If you migrated with the script above,
the table already exists and the auto-create is a no-op.

The `table` and `additional_tables[*]` suffixes must agree with the
`embedder_options.model` you pick. Pgvector enforces the column's
declared dim, so a model→table mismatch where the dims differ
(e.g. `model=nomic-embed-text` against `table=memories_qwen3`,
768 vs 1024) fails fast at SQL. The silent-garbage case is when two
models *share* a dim — qwen3 and arctic are both 1024 — so a
`model=snowflake-arctic-embed2` query against `memories_qwen3` returns
rows without error, but cosine distances are meaningless because the
vector spaces don't align. Re-embed before swapping.

### MCP server (recommended)

The repo ships a stdio JSON-RPC MCP server at
`claude_hooks.pgvector_mcp` that wraps `PgvectorProvider` and exposes
recall + store + KG ops as first-class MCP tools. After `install.py`
runs, the server is reachable from any MCP-aware client (Claude Code,
Cursor, Codex, OpenWebUI, …) without going through the hook
injection path.

What `install.py` does in `_setup_pgvector_mcp`:

1. Probes Postgres + the `vector` extension via the existing
   `PgvectorProvider.verify` (DSN already in your config, or prompts
   for one).
2. Probes Ollama for the configured embedder model (`qwen3-embedding:0.6b`
   by default); offers to `/api/pull` it if missing.
3. Initializes the qwen3 + KG schema if `memories_qwen3` doesn't
   exist (CREATE EXTENSION vector + pg_trgm; create kg_entities,
   kg_relations, memories_qwen3, kg_observations_qwen3 — all
   idempotent, re-running is a no-op).
4. Drops a launcher at `~/.local/bin/pgvector-mcp` (POSIX) or
   `%LOCALAPPDATA%/claude-hooks/bin/pgvector-mcp.cmd` (Windows). The
   launcher bakes in PYTHONPATH + the resolved Python interpreter so
   the server works from any cwd, no pip install of claude-hooks
   required.
5. Registers `mcpServers.pgvector` at the root of `~/.claude.json`
   pointing at the launcher (root-level so every project sees it).
   Backs up the existing config first.

Tools exposed (visible after Claude Code restart as
`mcp__pgvector__<name>`):

| Tool | Purpose |
|---|---|
| `pgvector-find` | Pure cosine-distance vector recall |
| `pgvector-find-hybrid` | RRF blend of vector + BM25 keyword (best for factual queries) |
| `pgvector-store` | Insert one memory; idempotent on content_hash |
| `pgvector-count` | Row count of the configured primary table |
| `pgvector-kg-search` | Search KG entities by name (trigram) + observations (hybrid) |
| `pgvector-kg-create` | Bulk-create entities; idempotent on name |
| `pgvector-kg-observe` | Add observations to existing entities |
| `pgvector-kg-relate` | Create relations between entities |

#### Manual install / pip path

If you `pip install claude-hooks`, the `pgvector-mcp` console script
is registered automatically (via `[project.scripts]` in
`pyproject.toml`). Then add it to your client's MCP config:

```jsonc
// ~/.claude.json (or ~/.cursor/settings.json, ~/.codex/config.json, …)
{
  "mcpServers": {
    "pgvector": {
      "type": "stdio",
      "command": "pgvector-mcp",
      "args": [],
      "env": {}
    }
  }
}
```

Without a pip install, point `command` at the launcher path the
installer wrote — `~/.local/bin/pgvector-mcp` or the equivalent
Windows `.cmd`.

#### Verifying the server is alive

```bash
printf '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}}\n' | pgvector-mcp
# -> {"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2024-11-05",
#     "capabilities":{"tools":{}},"serverInfo":{"name":"claude-hooks-pgvector","version":"0.1.0"}}}
```

A clean handshake means the launcher resolved Python correctly,
`claude_hooks.pgvector_mcp` imports, and the provider opens against
your DSN. Any failure prints to stderr.

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

## 5. Swapping the embedding model

This is the recipe a new user needs when they want a different
embedder than the one in the example config — e.g. dropping back to
`nomic-embed-text` for speed, switching to `snowflake-arctic-embed2`
for quality-first backfills, or trying a model not yet in the
registry.

> ⚠ **Re-embed before flipping the active config.** Embeddings from
> different models are NOT interchangeable. If two models share a
> dim (qwen3 and arctic are both 1024) the SQL won't error — cosine
> distances will silently be meaningless. The model→table suffix
> agreement is what protects you.

### A. Pick (or add) the model in the registry

The migration + bench scripts read from `MODELS` in
`scripts/migrate_to_pgvector.py`. To add a new candidate:

```python
MODELS: dict[str, ModelSpec] = {
    "minilm": ModelSpec("minilm", "locusai/all-minilm-l6-v2:latest", 384,  "minilm", 400,    256),
    "nomic":  ModelSpec("nomic",  "nomic-embed-text",                768,  "nomic",  5000,   8192),
    "arctic": ModelSpec("arctic", "snowflake-arctic-embed2",         1024, "arctic", 5000,   8192),
    "qwen3":  ModelSpec("qwen3",  "qwen3-embedding:0.6b",            1024, "qwen3",  30000,  32768),
    # add yours here
}
```

`short` is the table suffix. Pick one that won't collide with an
existing `memories_<short>` table. `max_chars` should be ≈ `num_ctx × 1`
in the worst case (1 char/token for dense paths/code) and ≈
`num_ctx × 3` for normal prose.

### B. Pull the model on Ollama (or your embedder host)

```bash
ollama pull qwen3-embedding:0.6b
```

### C. Populate the per-model tables

If you already have data in another `memories_<short>` table (initial
migration or another model's tables), re-embed from there into the
new tables:

```bash
# Source from Qdrant + Memory KG (fresh install only — has the side
# effect of also populating kg_entities and kg_relations).
python scripts/migrate_to_pgvector.py --embedder qwen3 --source all

# Source from an existing pgvector model — best path when Qdrant is
# already retired. There's no first-class flag for this yet; the
# inline pattern below works for any (src_short, dst_short):
python <<'PY'
import sys; sys.path.insert(0, ".")
from scripts.migrate_to_pgvector import (
    OllamaBatchEmbedder, MODELS, _vec_to_pg_literal, _load_pgvector_env_dotfile,
)
import json, psycopg
env = _load_pgvector_env_dotfile()
dsn = f"postgresql://{env['POSTGRES_USER']}:{env['POSTGRES_PASSWORD']}@127.0.0.1:5432/{env['POSTGRES_DB']}"
spec, src_short = MODELS["qwen3"], "nomic"
emb = OllamaBatchEmbedder("http://YOUR-OLLAMA-HOST:11434", spec.ollama_model,
                          max_chars=spec.max_chars, num_ctx=spec.num_ctx, timeout=180.0)
with psycopg.connect(dsn) as conn:
    src = conn.execute(
        f"SELECT content, COALESCE(metadata,'{{}}'::jsonb), source_id, created_at, content_hash "
        f"FROM memories_{src_short} ORDER BY id"
    ).fetchall()
    for i in range(0, len(src), 16):
        chunk = src[i:i+16]
        vecs = emb.embed([r[0] for r in chunk])
        with conn.cursor() as cur:
            cur.executemany(
                f"INSERT INTO memories_{spec.short} "
                "(content, metadata, source_id, created_at, content_hash, embedding) "
                "VALUES (%s,%s,%s,%s,%s,%s::vector) "
                "ON CONFLICT (content_hash) DO NOTHING",
                [(r[0], json.dumps(r[1]), r[2], r[3], r[4], _vec_to_pg_literal(v))
                 for r, v in zip(chunk, vecs)],
            )
        conn.commit()
    # Same loop for kg_observations_{src_short} → kg_observations_{spec.short}.
PY
```

### D. (Optional) Bench the new model

```bash
OLLAMA_URL=http://YOUR-OLLAMA-HOST:11434 \
    python scripts/bench_recall.py --skip-qdrant --models nomic,qwen3 --warm 2 --repeat 5
```

If you have a local query set you care about more than the curated
17, pass `--queries-file my_queries.txt` (one query per line).

### E. Flip the active config — atomic 4-line edit

```diff
 "pgvector": {
   "enabled": true,
   "dsn": "postgresql://claude:PASS@127.0.0.1:5432/memory",
-  "table": "memories_nomic",
-  "additional_tables": ["kg_observations_nomic"],
+  "table": "memories_qwen3",
+  "additional_tables": ["kg_observations_qwen3"],
   "embedder": "ollama",
   "embedder_options": {
     "url": "http://YOUR-OLLAMA-HOST:11434/api/embeddings",
-    "model": "nomic-embed-text"
+    "model": "qwen3-embedding:0.6b",
+    "num_ctx": 32768,
+    "max_chars": 30000
   }
 }
```

The four fields that must agree: `table` suffix, every entry in
`additional_tables`, `embedder_options.model`, and the `num_ctx` /
`max_chars` overrides if the new model differs from the
`OllamaEmbedder` defaults (8192 / 16000). Forgetting any one of these
is the most common foot-gun.

### F. Restart the daemon and verify

```bash
bin/claude-hooks-daemon-ctl restart           # POSIX
bin\claude-hooks-daemon-ctl.cmd restart       # Windows

# Smoke test: live recall via the running provider
python -c "
from claude_hooks.config import load_config
from claude_hooks.dispatcher import build_providers
cfg = load_config()
pg = next(p for p in build_providers(cfg) if p.name == 'pgvector')
hits = pg.recall('a query that should have stored matches', k=3)
print(f'recall: {len(hits)} hits'); [print(f'  {h[:120]}') for h in hits]
"
```

If recall returns 0 hits *and the corpus is non-empty*, you've hit
the silent same-dim mismatch — re-check that the table suffix matches
the model you re-embedded with. If recall returns hits but they look
unrelated, you may have skipped step C (re-embed) entirely.

### G. Rollback

The old tables are still on disk untouched. Revert step E (just put
the previous `table` / `additional_tables` / `embedder_options.model`
back, drop the `num_ctx` / `max_chars` override if it doesn't apply
to the old model), restart the daemon. No data loss; the new tables
linger as additional storage you can drop with `TRUNCATE` (or
`DROP TABLE` if you're certain).

---

## 6. Batch API

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

## 7. Operational notes

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
  TRUNCATE memories_minilm, memories_nomic, memories_arctic, memories_qwen3,
           kg_observations_minilm, kg_observations_nomic, kg_observations_arctic, kg_observations_qwen3,
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

## 8. Hard cutover walkthrough — replacing Qdrant + Memory KG

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
