---
session_id: csl-2026-10-22-1916-c772
created: "2026-10-22T19:16:04"
question: "Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python."
models:
  planner: "gemma4:31b-cloud"
  researcher: "gemma4:31b-cloud"
  synthesizer: "gemma4:31b-cloud"
topology: council
effort: medium
duration_seconds: 36.41
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-10-22/tmp/claude-hooks-bench-bench-baseline-2026-05-07-tQts
---

The following sites use direct `import psycopg` and lack the `conda-env` subprocess fallback pattern established in `install.py:2118-2142`:

*   `claude_hooks/providers/pgvector.py:123`: Yes. Executed during provider verification via `dispatcher.py`.
*   `claude_hooks/providers/pgvector.py:328`: Yes. This is the primary entry point for all `pgvector` store and recall operations.
*   `scripts/migrate_to_pgvector.py:624 [no pgvector at this line; line is in main]`: Yes. Exercisable when run as a standalone migration script.
*   `scripts/bench_recall.py:107`: Yes. Exercisable when run as a standalone benchmarking script.
*   `tests/test_pgvector_integration.py:56`: Yes. Executed during tests, though handled gracefully via `unittest.SkipTest`.
*   `tests/test_pgvector_integration.py:285 [no SkipTest at this line; line is in tearDownClass]`: Yes. Executed during test suite teardown.
