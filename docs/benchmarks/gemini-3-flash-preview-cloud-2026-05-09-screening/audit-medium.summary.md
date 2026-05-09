---
session_id: csl-2026-05-09-0615-2fb1
created: "2026-05-09T06:15:07"
question: "Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python."
models:
  planner: "gemini-3-flash-preview:cloud"
  researcher: "gemini-3-flash-preview:cloud"
  synthesizer: "gemini-3-flash-preview:cloud"
topology: council
effort: medium
duration_seconds: 36.80
status: completed
cwd: /tmp/claude-hooks-bench-bench-baseline-2026-05-07-jYrZ
---

Six sites in `claude-hooks` lack the 4e67dc2 conda-env fallback, causing crashes or functional failures in non-conda environments.

*   `claude_hooks/providers/pgvector.py:123`: **Exercisable.** Returns `False` on `ImportError` without attempting a fallback. Affects provider discovery and health checks.
*   `claude_hooks/providers/pgvector.py:328`: **Exercisable.** Re-raises `ImportError` with a manual installation hint. Affects all `store` and `recall` operations using the pgvector provider.
*   `scripts/migrate_to_pgvector.py:624`: **Exercisable.** Bare import inside `main()`. Direct execution will crash with `ModuleNotFoundError` if the package is missing.
*   `scripts/bench_recall.py:107`: **Exercisable.** Bare import in `PgvectorBenchProvider.__init__`. Benchmarking CLI will crash if run outside the primary environment.
*   `tests/test_pgvector_integration.py:56`: **Exercisable.** Triggers `unittest.SkipTest` on failure. Correctly prevents crashes during discovery but ignores the conda interpreter fallback.
*   `tests/test_pgvector_integration.py:285`: **Exercisable.** Import in `tearDownClass` is caught by a broad `except Exception`. Silently fails to clean up database resources if the package is missing.
