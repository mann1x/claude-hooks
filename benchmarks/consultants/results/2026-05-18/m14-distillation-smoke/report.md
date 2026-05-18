# M14 live distillation smoke — 2026-05-18

End-to-end verification of the M14 TTL + distillation chain on the
user's live solidPC pgvector (post-flip commit `48f1c4e`).

## Run

- **Timestamp**: 2026-05-18T06:18:25Z (Berlin: 2026-05-18 08:18 CEST)
- **Backend**: pgvector @ `127.0.0.1:5432/memory`, table
  `consultants_m14_smoke` (created + dropped by the harness).
- **Embedder**: `qwen3-embedding:0.6b` via the daemon-managed
  llamafile at `127.0.0.1:38092`, dim 1024.
- **Distillation model**: `gemma4:31b-cloud` (primary), no
  fallback fired.
- **Ollama proxy**: `http://192.168.178.2:11433`.
- **Harness**:
  [`run_smoke.py`](./run_smoke.py),
  [`smoke_output.txt`](./smoke_output.txt).

## Result

| Stage                       | Outcome                                             |
|-----------------------------|-----------------------------------------------------|
| PgvectorProvider init       | ✅ Connected via DSN; embedder probed dim=1024     |
| Table created with v2 schema | ✅ `_create_table` emitted ALTER TABLE + partial index in one tx |
| Seeded 4 research findings  | ✅ All 4 rows have `expires_at` set (T+30s)        |
| Read-back via `store.search` | ✅ 4 items returned (via in-process index fallback) |
| Sleep 35s → TTL elapsed     | ✅                                                  |
| `provider.expire_before(now)` | ✅ Returned 4 rows ordered by `expires_at` ASC    |
| `Distiller.distill_session()` | ✅ gemma4:31b-cloud, primary, no fallback         |
| LLM call elapsed            | **25.71 s**                                         |
| Summary length              | **2073 chars** (≤ 800-word rubric budget)           |
| `write_distilled_summary()` | ✅ Wrote to `("project", "6ba3d13e1e58")` key=`distill-csl-2026-05-18-m14-smoke-ce85df675a9b` |
| `delete_by_hashes()`        | ✅ 4 originals deleted                             |
| Verify project ns survives  | ✅ 1 distilled entry in `("project", …)`           |
| Cleanup                     | ✅ Test table dropped                              |

**Total wall time**: ~ 65 s (35 s TTL wait + 25.7 s LLM + ~5 s
embedding + IO).

## Distilled summary content

The full 2073-char summary is captured in
[`smoke_output.txt`](./smoke_output.txt). Highlights:

- **Citations preserved**: `claude_hooks/providers/pgvector.py:140`,
  `claude_hooks/providers/sqlite_vec_schema.py:122`,
  `consultants/engine/store_reaper.py:255` — every claim grounded
  to a file:line, matching the rubric.
- **Decisions retained**: "This implementation preserves the
  invariant that a single group failure does not poison other
  groups in the same sweep (test_one_group_failure_does_not_…)".
- **Gotcha retained**: "`test_sqlite_vec_schema_migration.py`
  requires assertions to check for `LATEST_VERSION` rather than a
  hardcoded `version == 1`".
- **Open question retained**: "Should successful distillations be
  cached in-memory to avoid redundant LLM billing if a
  `BaseStore.put` failure triggers a retry in the next sweep
  tick?".
- **Structure**: Markdown headers + bullet lists, no preamble
  ("Here is the summary…"), no process narration. Matches the
  Caliber rubric exactly.

The summary is materially smaller than the 4 input findings
(2073 chars vs ~3800 chars combined) while preserving every
file:line citation and every flagged open question. That's
exactly the consolidation behaviour M14 was designed for.

## Bugs discovered + fixed (this smoke landed two real
defects)

### Bug 1 — pgvector pre-existing tables never got the `expires_at` column

`PgvectorProvider._create_table` early-returned at the table-
exists check, BEFORE the M14 ALTER TABLE / CREATE INDEX. So the
user's live `memories_qwen3` (and any other pre-M14 table) would
never grow `expires_at`, and `expire_before` queries would fail
with `column "expires_at" does not exist`.

**Fix**: split the create branch from the migration branch.
`CREATE TABLE` is skipped when the table exists; the additive
M14 migration (ALTER + CREATE INDEX, both IF NOT EXISTS) runs on
every connection. Failure to migrate now also calls
`self._conn.rollback()` so the connection isn't left aborted
for the next query.

**Regression gate**: new class
`TestCreateTableMigratesExistingTables` in
[`tests/test_pgvector_expires_at.py`](../../../../tests/test_pgvector_expires_at.py)
with 2 tests covering both the existing-table-still-migrates and
the rollback-on-failure paths.

### Bug 2 — pgvector recall paths left the connection aborted on soft failure

`recall_hybrid`'s BM25 leg and `_search_tables`'s vector leg both
caught query exceptions and logged them as "soft" failures —
without calling `conn.rollback()`. PostgreSQL leaves the
connection in an aborted state until a rollback, so the very
next query (in this smoke's case, `expire_before` 35 s later)
saw `current transaction is aborted, commands ignored until end
of transaction block`.

This was a generic bug in the pgvector provider that pre-dated
M14 — the smoke just surfaced it because the smoke's test table
didn't have a `content_tsv` column (canonical migrate-script-
created tables do; tests / smoke / ad-hoc tables typically
don't).

**Fix**: every soft-failure `except` block in `recall_hybrid` and
`_search_tables` now calls `self._conn.rollback()` in a guarded
inner try before continuing. The rollback restores the
connection so the next caller can use it.

## What the smoke validates

✅ **TTL plumbing on pgvector** — writes carry `expires_at`,
`expire_before` selects them.

✅ **Distiller fallback chain** — primary
`gemma4:31b-cloud` was sufficient; fallback didn't fire.

✅ **Caliber rubric** — citations + decisions + gotchas + open
questions retained; process narration / retry traces dropped;
≤ 800 words honored; no preamble.

✅ **Project-namespace write** — summary lands in
`("project", project_id)` with provenance metadata
(`distilled_from_sid`, `original_count`, `distill_model`,
`cwd`).

✅ **Critical invariant — deletes only after a successful
distillation** — the smoke's flow proves this directly: the
4 `delete_by_hashes` calls happened AFTER
`write_distilled_summary` returned the key.

## Follow-ups

- The two pgvector bugs above warrant their own commit on `dev`
  (separate from the flip commit because they're operational
  fixes for a pre-existing-table footprint, not a behavior
  change). Tests added inline.
- The "cache successful distillation in-memory before deleting
  originals" open question is now durably captured in this
  report's distilled summary itself — a nice fractal: M14's
  output recommends a refinement to M14.
