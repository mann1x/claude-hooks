---
session_id: csl-2026-10-22-1248-478e
created: "2026-10-22T12:48:31"
question: "Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python."
models:
  planner: "gemma4:31b-cloud"
  researcher: "gemma4:31b-cloud"
  synthesizer: "gemma4:31b-cloud"
topology: council
effort: medium
duration_seconds: 15.17
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-10-22/tmp/claude-hooks-bench-bench-baseline-2026-05-07-xFLz
---

* `claude_hooks/providers/pgvector.py:123`: Exercisable. Called by `PgvectorProvider.verify()`; fails if run in system Python without `psycopg`.
* `claude_hooks/providers/pgvector.py:328`: Exercisable. Called by `_ensure_ready()`; raises `ImportError` if the provider is enabled but the current interpreter lacks `psycopg`.
* `scripts/migrate_to_pgvector.py:624`: Exercisable. Direct import in `main()`; results in immediate crash if run in a non-conda environment.
* `scripts/bench_recall.py:107 [no main at this line; line is in __init__]`: Exercisable. Direct import in `PgvectorBenchProvider.__init__`; results in `ImportError` crash if run in a non-conda environment.
