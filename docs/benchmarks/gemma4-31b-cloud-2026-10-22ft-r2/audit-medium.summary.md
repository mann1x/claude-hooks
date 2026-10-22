---
session_id: csl-2026-10-22-2004-abd1
created: "2026-10-22T20:04:02"
question: "Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python."
models:
  planner: "gemma4:31b-cloud"
  researcher: "gemma4:31b-cloud"
  synthesizer: "gemma4:31b-cloud"
topology: council
effort: medium
duration_seconds: 33.73
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-10-22/tmp/claude-hooks-bench-bench-baseline-2026-05-07-FXar
---

Several critical sites in `claude_hooks/providers/pgvector.py` and utility scripts still lack the conda-env subprocess fallback, leading to potential runtime crashes when executed from a non-conda Python environment.

* `claude_hooks/providers/pgvector.py:123`: Yes (fails provider verification)
* `claude_hooks/providers/pgvector.py:328`: Yes (crashes during `recall` or `store` operations)
* `scripts/migrate_to_pgvector.py:624 [no store at this line; line is in main]`: Yes (crashes during CLI execution)
* `scripts/bench_recall.py:107`: Yes (crashes during provider instantiation)
* `tests/test_pgvector_integration.py:56`: No (handled via `unittest.SkipTest` at `tests/test_pgvector_integration.py:57`)
* `tests/test_pgvector_integration.py:285 [no SkipTest at this line; line is in tearDownClass]`: No (handled via broad exception block at `tests/test_pgvector_integration.py:293`)
