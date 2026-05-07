---
session_id: csl-2026-05-07-0951-fe97
created: "2026-05-07T09:51:34"
question: "Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python."
models:
  planner: "gemma4:31b-cloud"
  researcher: "gemma4:31b-cloud"
  synthesizer: "gemma4:31b-cloud"
topology: council
effort: medium
duration_seconds: 71.53
status: completed
cwd: /tmp/claude-hooks-bench-bench-baseline-2026-05-07-s7IY
---

Six `psycopg` import sites lack the conda-env subprocess fallback; all are exercisable from a non-conda-env Python.

*   `claude_hooks/providers/pgvector.py:123`: Exercisable. Returns `False` on `ImportError` in `verify()`.
*   `claude_hooks/providers/pgvector.py:328`: Exercisable. Re-raises `ImportError` in `_ensure_ready()`.
*   `scripts/migrate_to_pgvector.py:624`: Exercisable. Bare import in `main()`; triggers `ModuleNotFoundError`.
*   `scripts/bench_recall.py:107`: Exercisable. Bare import in `PgvectorBenchProvider.__init__`; triggers `ModuleNotFoundError`.
*   `tests/test_pgvector_integration.py:56`: Exercisable. Raises `unittest.SkipTest` in `_skip_if_no_deps()`.
*   `tests/test_pgvector_integration.py:285`: Exercisable. Import in `tearDownClass()` is swallowed by a broad `except Exception` block.
