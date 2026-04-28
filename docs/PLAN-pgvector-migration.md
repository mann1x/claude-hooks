# Plan — Pgvector migration evaluation

> Status: **complete** (2026-04-28). Pgvector backend is functional,
> migrated, benchmarked, and documented. Default embedder picked is
> `nomic-embed-text` (768 dim). See `docs/pgvector-runbook.md` for the
> walkthrough; this doc captures the design rationale + decisions taken.

## Goal

Add Postgres + pgvector as an alternative memory backend that can:

1. Replace **Qdrant** as the semantic memory store, AND
2. Replace **Memory KG** as the typed-graph + observations store,

with a single docker stack, an idempotent migration script (also handles
delta sync), and benchmark numbers good enough to inform the default
embedder pick.

## Why pgvector specifically

- Single backend instead of two (one DB to back up, one auth surface,
  one set of running containers).
- Hybrid search out of the box (vector + tsvector keyword + JSONB
  metadata filtering in one SQL query).
- Postgres operational maturity vs Qdrant's smaller ecosystem.
- Local; no cloud lock-in. The whole stack stays on the LAN.

## Non-goals

- **Not** retiring Qdrant + Memory KG immediately. Pgvector ships
  alongside, opt-in. Cutover is a config-only change later if the
  user wants it.
- Not building a relational query layer over the KG side; Memory-KG's
  graph queries are name-based + observation keyword search, both
  trivially served by GIN indexes (trigram + tsvector).

## Decisions taken

### D1. Postgres 16 + pgvector 0.8

Image: `pgvector/pgvector:pg16`. Stable, current, official.
Installed extensions: `vector`, `pg_trgm` (entity-name fuzzy match),
`btree_gin` (composite indexes).

### D2. Per-model namespaced tables

Embedding dimension is part of the column type. To bench multiple
models cleanly we suffix tables by model:

```
memories_minilm        vector(384)
memories_nomic         vector(768)
memories_arctic        vector(1024)
kg_observations_minilm vector(384)
kg_observations_nomic  vector(768)
kg_observations_arctic vector(1024)
```

Model-agnostic tables (`kg_entities`, `kg_relations`,
`migration_state`) are shared.

### D3. Re-embed during migration (no vector reuse from Qdrant)

Even when picking minilm (same model Qdrant uses), we re-embed the
content in Python. Reasons:

- Qdrant's stored vectors are produced by `fastembed` server-side; our
  pipeline goes through Ollama. Tokenizer/preprocessing are subtly
  different (verified empirically: `recall@5` for same-model minilm
  vs Qdrant baseline was 0.32, not 1.0).
- Fetching vectors from Qdrant via `/points/scroll` with
  `with_vector=true` works but couples us to Qdrant's payload schema.
- Re-embedding takes seconds for a small corpus; not worth the
  complexity of a vector-passthrough path.

### D4. Default embedder = `nomic-embed-text` (768 dim)

Bench-driven (see runbook). `nomic` lands at 35 ms p50 vs Qdrant's
86 ms baseline, with full 8k-token context. `arctic-embed2` is higher
quality but 6× slower per embed; `minilm-l6-v2` is fast but truncates
at 256 tokens which loses information for typical memory entries.

### D5. Idempotent migration via stable content hashes

Every memory/observation gets `SHA-256(normalised_content)` as the
idempotency key. ON CONFLICT DO NOTHING for memories/observations,
ON CONFLICT DO UPDATE for entities (so renamed entity_types take
effect on re-run). Same script handles initial migration AND delta
sync — re-running is cheap.

### D6. Truncate inputs server-side, not source-side

Some Qdrant memories are 6k+ chars. Embedders have native context
limits (256 tokens for minilm, 8192 for nomic/arctic). The migration
script applies a per-model `max_chars` cap — but only when sending
to the embedder. The full text stays in the `content` column, so:

- Recall on truncated embeddings still returns the full content.
- Switching to a larger-context model later just needs a re-embed,
  not a re-import.

### D7. Batch API as a real performance win, not a stub

PgvectorProvider overrides `batch_recall` (single Ollama batch call
+ N parallel SQL queries) and `batch_store` (single Ollama batch +
`executemany`). Measured 2.39× speedup on 8 queries vs single
sequential. Same shape used by the migration script.

## Trade-offs accepted

| Trade-off | Why |
|---|---|
| 3× storage during bench (3 models in parallel) | Tiny corpus; cost is irrelevant. Lets us A/B without ALTER COLUMN gymnastics. |
| Qdrant + Memory-KG containers stay running | No flag day. Reversible cutover. Costs a few hundred MB. |
| Bench's `recall@5 vs Qdrant` is similarity, not quality | True quality eval would need labelled relevance. The number is informative (0 == bug, > 0 == aligned-ish), not authoritative. |
| Per-model tables vs schema-per-model | Tables are simpler. Schema-per-model would mean cross-schema queries; not worth the complexity for 3 candidates. |

## Out-of-scope follow-ups (sequenced)

1. **Hybrid query**: when both vector and keyword indexes exist, an
   `AND` SQL query that combines them with weighted ranking would
   likely beat pure vector recall on factual queries. Try once we
   have real usage data.
2. **HNSW tuning**: `m=16, ef_construction=64` are pgvector defaults
   and have not been swept. With <1000 vectors the difference is
   negligible — re-tune if the corpus grows past ~50k entries.
3. **Quantisation (`halfvec`)**: pgvector 0.8 supports `halfvec` (16-bit)
   for ~50% storage savings. Negligible win at our scale; revisit if
   the corpus grows past ~1M vectors.
4. **MCP server for pgvector**: the existing pattern (Qdrant, Memory-KG)
   exposes the store as an HTTP MCP server so other tools (not just
   claude-hooks) can recall from it. We did not build that layer —
   PgvectorProvider talks SQL directly. Trivial to add later.
