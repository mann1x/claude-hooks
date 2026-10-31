---
session_id: csl-2026-10-22-1236-347b
created: "2026-10-22T12:36:58"
question: "Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python."
models:
  planner: "gemma4:31b-cloud"
  researcher: "gemma4:31b-cloud"
  synthesizer: "gemma4:31b-cloud"
topology: council
effort: medium
duration_seconds: 25.33
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-10-22/tmp/claude-hooks-bench-bench-baseline-2026-05-07-izdU
---

Five `psycopg` import sites lack the conda-env subprocess fallback.

*   `install.py:2092`: Yes. Located within `_PGVECTOR_VERIFY_SCRIPT`; it catches `ImportError` and exits with a JSON failure without attempting a fallback.
*   `claude_hooks/providers/pgvector.py:123`: Yes. Located in `verify()`; returns `False` on `ImportError`. While often called by `install.py` wrappers that provide a fallback, the site itself is unprotected.
*   `claude_hooks/providers/pgvector.py:328`: Yes. Located in `_ensure_ready()`; raises a descriptive `ImportError` requiring manual installation.
*   `scripts/migrate_to_pgvector.py:624`: Yes. Direct import in `main()`; crashes on `ImportError` when run via system Python.
*   `scripts/bench_recall.py:107 [no main at this line; line is in __init__]`: Yes. Direct import in `PgvectorBenchProvider.__init__`; crashes on `ImportError` when run via system Python.
