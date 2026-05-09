---
session_id: csl-2026-05-09-0916-b65d
created: "2026-05-09T09:16:07"
question: "Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python."
models:
  planner: "gemma4:31b-cloud"
  researcher: "gemma4:31b-cloud"
  synthesizer: "gemma4:31b-cloud"
topology: council
effort: medium
duration_seconds: 107.27
status: completed
cwd: /tmp/claude-hooks-bench-bench-baseline-2026-05-07-HJf4
---

Six import sites lack the `conda-env` subprocess fallback; all are exercisable from a non-conda Python environment.

* `claude_hooks/providers/pgvector.py:123`: Yes (returns `False` via `ImportError` guard)
* `claude_hooks/providers/pgvector.py:328`: Yes (re-raises `ImportError` via guard)
* `scripts/migrate_to_pgvector.py:624`: Yes (bare import; causes `ModuleNotFoundError`)
* `scripts/bench_recall.py:107`: Yes (bare import; causes `ModuleNotFoundError`)
* `tests/test_pgvector_integration.py:56`: Yes (raises `unittest.SkipTest` via guard)
* `tests/test_pgvector_integration.py:285`: Yes (failure swallowed by `except Exception`)
