---
session_id: csl-2026-05-09-1020-7cd4
created: "2026-05-09T10:20:30"
question: "Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python."
models:
  planner: "gemini-3-flash-preview:cloud"
  researcher: "gemini-3-flash-preview:cloud"
  synthesizer: "gemma4:31b-cloud"
topology: council
effort: medium
duration_seconds: 60.97
status: completed
cwd: /tmp/claude-hooks-bench-bench-baseline-2026-05-07-CvAB
---

Six import sites lack the conda-env subprocess fallback implemented in commit 4e67dc2.

* `claude_hooks/providers/pgvector.py:123`: Exercisable; `verify()` returns `False` on `ImportError` without attempting fallback.
* `claude_hooks/providers/pgvector.py:328`: Exercisable; `_ensure_ready()` re-raises `ImportError` during `recall`, `store`, or `count` operations (reachable via `pyproject.toml:34`).
* `scripts/migrate_to_pgvector.py:624`: Exercisable; bare import in `main()` causes `ModuleNotFoundError` upon direct script invocation.
* `scripts/bench_recall.py:107`: Exercisable; bare import in `PgvectorBenchProvider.__init__` causes `ModuleNotFoundError` during benchmarking.
* `tests/test_pgvector_integration.py:56`: Exercisable; `_skip_if_no_deps()` triggers `unittest.SkipTest` without attempting fallback.
* `tests/test_pgvector_integration.py:285`: Exercisable; `tearDownClass` performs a direct import and silently swallows the failure via a broad exception block.
