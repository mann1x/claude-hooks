---
session_id: csl-2026-05-09-1009-ef89
created: "2026-05-09T10:09:48"
question: "Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python."
models:
  planner: "gemini-3-flash-preview:cloud"
  researcher: "gemini-3-flash-preview:cloud"
  synthesizer: "gemma4:31b-cloud"
topology: council
effort: medium
duration_seconds: 56.83
status: completed
cwd: /tmp/claude-hooks-bench-bench-baseline-2026-05-07-KaZP
---

* `claude_hooks/providers/pgvector.py:123`: Exercisable. `PgvectorProvider.verify()` returns `False` on `ImportError` without fallback.
* `claude_hooks/providers/pgvector.py:328`: Exercisable. `PgvectorProvider._ensure_ready()` re-raises `ImportError` as an installation instruction.
* `scripts/migrate_to_pgvector.py:624`: Exercisable. Bare `import psycopg` in `main()` results in `ModuleNotFoundError`.
* `scripts/bench_recall.py:107`: Exercisable. Bare `import psycopg` in `PgvectorBenchProvider.__init__` results in `ModuleNotFoundError`.
* `tests/test_pgvector_integration.py:56`: Exercisable. `_skip_if_no_deps()` catches `ImportError` to trigger `unittest.SkipTest`.
* `tests/test_pgvector_integration.py:285`: Exercisable. `tearDownClass()` imports `psycopg` within a broad `except Exception` block.
