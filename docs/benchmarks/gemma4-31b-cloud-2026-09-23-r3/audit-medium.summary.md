---
session_id: csl-2026-09-23-2049-4f6a
created: "2026-09-23T20:49:58"
question: "Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python."
models:
  planner: "gemma4:31b-cloud"
  researcher: "gemma4:31b-cloud"
  synthesizer: "gemma4:31b-cloud"
topology: council
effort: medium
duration_seconds: 15.52
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-09-23/tmp/claude-hooks-bench-bench-baseline-2026-05-07-n3hj
---

The following sites perform `psycopg` imports without the conda-env subprocess fallback implemented in `install.py` (e.g., `install.py:2138`, `install.py:2660`):

*   **`claude_hooks/providers/pgvector.py:123`**: Exercisable. Used in `verify()`; returns `False` on `ImportError` (line 124) without attempting fallback.
*   **`claude_hooks/providers/pgvector.py:328`**: Exercisable. Used in `_ensure_ready()`; raises `ImportError` (line 330) without attempting fallback.
*   **`scripts/migrate_to_pgvector.py:624`**: Exercisable. Direct import in `main()`; crashes with `ImportError` if run from a non-conda Python.
*   **`scripts/bench_recall.py:107 [no main at this line; line is in __init__]`**: Exercisable. Direct import in `PgvectorBenchProvider.__init__`; crashes with `ImportError` if run from a non-conda Python.
