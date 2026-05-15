# sqlite_vec runbook (v1.7+)

This is the operator + developer reference for the `sqlite_vec`
provider after the v1.7.0 full-parity work. The sibling
[`pgvector-runbook.md`](pgvector-runbook.md) covers the same shape
on Postgres — pick whichever fits your infra.

## What you get

* **`recall(query, k)`** — pure vector cosine via the sqlite-vec
  `vec0` virtual table.
* **`recall_hybrid(query, k, alpha=0.5, rrf_k=60)`** — Reciprocal
  Rank Fusion over vector cosine + BM25 (FTS5). Same defaults as
  pgvector.
* **`store(content, metadata)`** — idempotent on whitespace-normalised
  `content_hash`. Re-storing the same content is a silent no-op.
* **`count()`** — row count in the memory table.
* **`kg_create_entities` / `kg_add_observations` / `kg_create_relations`
  / `kg_search_nodes`** — full KG surface; same return shapes as
  pgvector.

The bundled `sqlite-vec-mcp` server exposes all eight as MCP tools
named `sqlite-vec-find / -find-hybrid / -store / -count / -kg-search
/ -kg-create / -kg-observe / -kg-relate`.

## On-disk schema (v1)

Single `.db` file, schema version pinned in `claude_hooks_schema`:

```
<table>                       content + content_hash + metadata + rowid
<table>_vec                   vec0 virtual, embedding float[dim]
<table>_fts                   fts5 virtual, content (BM25)
                              AFTER-INSERT / AFTER-DELETE triggers

kg_entities                   id PK, name UNIQUE, type, metadata, ts
kg_entities_name_fts          fts5 virtual, name (tokenize=trigram or unicode61)
kg_relations                  id PK, (from_id, to_id, type) UNIQUE, FKs CASCADE
kg_observations               id PK, entity_id, content, content_hash,
                              UNIQUE(entity_id, content_hash), FK CASCADE
kg_observations_vec           vec0 virtual, embedding float[dim]
kg_observations_fts           fts5 virtual, content (BM25)

claude_hooks_schema           version INT PK, migrated_at TEXT, metadata TEXT
```

The connection is opened with `PRAGMA foreign_keys = ON;` so the
ON DELETE CASCADE chains work — without that PRAGMA the cascade
clauses are inert and observations + relations leak when an entity
is deleted.

`<table>_fts` is **external-content** (`content=<table>,
content_rowid=rowid`) so FTS5 reads the source text from `<table>`
on demand instead of duplicating storage. The AFTER-INSERT trigger
keeps it synced; on backfill (existing rows during migration) we
run `INSERT INTO <table>_fts(<table>_fts) VALUES('rebuild')`, the
canonical external-content rebuild idiom.

## Migration (v0 → v1)

A user upgrading from v1.6.x has a legacy `.db` with `<table>` +
`<table>_vec` only. First call to `_ensure_ready()` after upgrade
(typically the first `recall` / `store` / `count`) runs
`migrate_schema(conn, embedding_dim=dim, table=table)`:

1. ALTER TABLE add `content_hash BLOB` if missing.
2. Backfill `content_hash` for existing rows in batches of 500.
   Duplicates that lose the partial-unique race survive with NULL
   hash (logged); they remain searchable, just won't participate
   in future `ON CONFLICT(content_hash) DO NOTHING`.
3. Create UNIQUE INDEX on `content_hash WHERE content_hash IS NOT NULL`
   (partial — NULLs are exempt).
4. Create `<table>_fts`, the AFTER triggers, and run the FTS5
   `rebuild` to populate it from existing rows.
5. Create the KG cluster (entities, relations, observations, vec
   + fts mirrors, triggers).
6. Insert the `(version=1, migrated_at=now)` row.

Migration is one transaction. Idempotent: re-runs that find
`version >= 1` are no-ops. Non-destructive: the original `<table>`
and `<table>_vec` are never dropped or rewritten — a user who
downgrades back to v1.6.x can still recall + store on the legacy
surface; the new tables sit unused.

If the host's stdlib SQLite lacks the FTS5 trigram tokenizer
(`tokenize='trigram'`, SQLite ≥3.34), the migration falls back to
`unicode61` for `kg_entities_name_fts` and writes
`{"name_fts": "unicode61"}` into `claude_hooks_schema.metadata`.
`kg_search_nodes` reads the metadata once per provider instance and
switches to a `LIKE %query%` fallback in that case — slower and
case-sensitive but functionally complete.

## Hybrid recall — tuning

`recall_hybrid(query, k=5, alpha=0.5, rrf_k=60)`:

* **`alpha`** — weight on the vector signal in [0, 1]. `0.5` blends
  evenly. `1.0` collapses to pure vector recall (`recall`).
  `0.0` collapses to pure BM25.
* **`rrf_k`** — RRF smoothing constant. Higher values flatten the
  rank distribution; lower values are more winner-take-all. `60`
  is the value Cormack/Clarke/Büttcher (2009) used in the original
  RRF paper and matches what pgvector ships.
* **`k`** — top-K to return. Each signal pass fetches `max(k*4, 20)`
  candidates before fusion so the runner-ups from one signal can
  rescue documents missed by the other.

Empty / whitespace-only query short-circuits to `[]`. Either signal
pass can fail (vector backend down, FTS5 parse error) and the
surviving signal still drives the ranking — `kg_search_nodes` and
`recall_hybrid` log the skip at DEBUG and continue.

## FTS5 input sanitisation

User input flows through `_fts5_query(text)` which wraps the whole
string in `"..."` quotes (doubling any embedded `"`). This makes
FTS5 take operators (`OR`, `NEAR/N`, `*`, `-`) literally instead of
as syntax. It's the SQLite analogue of pgvector's
`websearch_to_tsquery('english', %s)` robust-input contract.

## KG usage

```python
provider.kg_create_entities([
    {"name": "solidpc", "entity_type": "server"},
    {"name": "ollama", "entity_type": "service",
     "metadata": {"port": 11434}},
])
provider.kg_add_observations([
    {"entity_name": "solidpc", "content": "RTX 3090 + Ollama"},
])
provider.kg_create_relations([
    {"from": "ollama", "to": "solidpc", "relation_type": "runs_on"},
])

# Three-pass search: name fuzzy → observation hybrid → observation fill
nodes = provider.kg_search_nodes("solidpc")
# [{"name": "solidpc", "entity_type": "server", "metadata": {},
#   "observations": ["RTX 3090 + Ollama"],
#   "_score": 1.0, "_match": "name"}]
```

Idempotency:

* `kg_create_entities` — `ON CONFLICT(name) DO NOTHING`
* `kg_add_observations` — `ON CONFLICT(entity_id, content_hash) DO NOTHING`
* `kg_create_relations` — `ON CONFLICT(from_entity_id, to_entity_id, relation_type) DO NOTHING`

`content_hash` is computed by the shared
`claude_hooks/providers/_content_hash.py` — same algorithm pgvector
uses, so the cross-store dedup key is identical. A future shovel
script that imports a `.db` into pgvector (or vice-versa) can rely
on hash collisions to find duplicates.

## Cross-store compatibility

The shared `content_hash` means a memory stored on pgvector and the
same memory stored on sqlite_vec hash to the same 32-byte blob.
This is the cornerstone for any future migration / replication
tool — the two stores agree on identity.

## Failure modes

| Symptom | Cause | Fix |
|---|---|---|
| `ON CONFLICT clause does not match any PRIMARY KEY or UNIQUE constraint` | Partial unique index needs predicate on the conflict target | Already handled in `store()` — `ON CONFLICT(content_hash) WHERE content_hash IS NOT NULL DO NOTHING` |
| `vec0: dimension mismatch` | Embedding dim drift after upgrading embedder | Restore previous embedder OR drop `<table>_vec` (loses embeddings, regen on next store) |
| `no such tokenizer: trigram` | SQLite <3.34 | Migration falls back to `unicode61`; KG name search uses `LIKE` |
| Observations + relations leak after entity DELETE | `PRAGMA foreign_keys` not ON | `_ensure_ready` sets it; any external connection on the same `.db` must also set it |

## Concurrency

SQLite serialises writers on the `.db` file. Concurrent stores from
multiple MCP clients queue; reads are concurrent with reads. The
HTTP transport uses `ThreadingHTTPServer` so each request gets its
own thread, but they all share the one provider connection — writes
still serialise, which is the right shape for our workload (low
write rate, high read rate).
