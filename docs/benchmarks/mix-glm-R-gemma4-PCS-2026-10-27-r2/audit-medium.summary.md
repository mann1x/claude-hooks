---
session_id: csl-2026-10-27-1504-5b80
created: "2026-10-27T15:04:04"
question: "Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python."
models:
  planner: "gemma4:31b-cloud"
  researcher: "glm-5.3-flash:cloud"
  synthesizer: "gemma4:31b-cloud"
topology: council
effort: medium
duration_seconds: 624.69
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-10-27/tmp/claude-hooks-bench-bench-baseline-2026-05-07-BlRv
---

* `claude_hooks/providers/pgvector.py:123`: Yes. `PgvectorProvider.verify()` catches `ImportError` and returns `False` without attempting conda fallback, leading to misleading "FAILED" status during detection.
* `claude_hooks/providers/pgvector.py:328`: Yes. `_ensure_conn` raises `ImportError` when `psycopg` is missing. This is a hot path for hooks executed via the `bin/claude-hook` shim when the conda environment is missing and it falls back to system Python.
* `scripts/migrate_to_pgvector.py:624`: Yes. Bare import in `main()` causes an uncaught `ImportError` traceback when the migration script is invoked using system Python.
* `scripts/bench_recall.py:107`: Yes. Bare import in `PgvectorBenchProvider.__init__` causes an uncaught `ImportError` traceback when the benchmarking script is invoked using system Python.
