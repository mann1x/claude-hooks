---
description: Persistent task tracking — task files are the record, the store's tasks table is a derived index
globs: claude_hooks/tasks/**,tests/test_tasks.py,docs/PLAN-task-tracking.md
---

- One Markdown file per task under `<project>/.claude-hooks/tasks/` is the record. The `tasks` table on the host's store (pgvector or sqlite_vec) only indexes the files, for listing, cross-project views and recall. A host without a store still has working tasks: build the service with `service_for(provider=None)` from `claude_hooks/tasks/__init__.py`.
- `TaskService` (`claude_hooks/tasks/service.py`) writes the task file before the row. A failed index write is logged and repaired by the next `reconcile()`; never write the row first.
- `reconcile()` lets the files win. Unchanged files are recognised by `(mtime_ns, size)` from `.index-state.json` in the task folder, changed ones are re-read and upserted, and rows whose file is gone are deleted.
- Layout (`claude_hooks/tasks/files.py`): `config.toml` holds `prefix` and `project`, open tasks are `<prefix>-<n>.md`, done and cancelled ones live in `archive/`. The prefix is derived once by `derive_prefix()` and written down, never re-derived, so renaming the directory doesn't renumber old references.
- `TaskDir.create()` allocates ids with an `O_EXCL` create, not a lock file. A number already used in `archive/` is skipped.
- File format (`claude_hooks/tasks/model.py`): front matter in a restricted YAML subset parsed with the stdlib (bare or double-quoted scalars, flow lists, flow maps), then `## Description` and `## Acceptance` (rewritten in place) and `## Log` (append-only, newest first). Sections a person adds are kept. A value the parser doesn't understand is kept as a string, never refused. Don't add a YAML dependency.
- Statuses are `pending` / `active` / `waiting` (open) and `done` / `cancelled` (closed); priorities are `H` / `M` / `L`.
- `TaskIndex` (`claude_hooks/tasks/store.py`) borrows the provider's connection and its lock, the same rule as `claude_hooks/mailbox/store.py`. It never opens a second connection, and every failure path rolls back.
- `claude_hooks/tasks/schema.py` serves both dialects with one set of queries: times are ISO-8601 UTC text, list and map fields are JSON text. The embedding is a base64 float32 blob in a TEXT column, not `vector(n)`, and similarity is computed in Python. `embed_model` tags each vector, and rows from another model are re-embedded (`embed_pending()`), never compared across spaces.
- `service_for(..., embed=False)` keeps the write path free of embedder round-trips; `embed_pending()` catches up later.
- Tests: `tests/test_tasks.py`. Plan: `docs/PLAN-task-tracking.md`.
