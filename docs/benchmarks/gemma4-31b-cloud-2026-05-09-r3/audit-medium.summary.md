---
session_id: csl-2026-05-09-0931-a145
created: "2026-05-09T09:31:10"
question: "Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python."
models:
  planner: "gemma4:31b-cloud"
  researcher: "gemma4:31b-cloud"
  synthesizer: "gemma4:31b-cloud"
topology: council
effort: medium
duration_seconds: 152.12
status: completed
cwd: /tmp/claude-hooks-bench-bench-baseline-2026-05-07-wtV5
---

* `scripts/migrate_to_pgvector.py:624`: Yes. Bare import in `main()` causes immediate `ModuleNotFoundError`.
* `claude_hooks/providers/pgvector.py:328`: Yes. `_ensure_ready()` re-raises `ImportError` without attempting fallback; blocks `recall()` and `store()`.
* `claude_hooks/providers/pgvector.py:123`: Yes. `verify()` returns `False` on `ImportError` without attempting fallback.
* `scripts/bench_recall.py:107`: Yes. Bare import in `PgvectorBenchProvider.__init__` causes crash during instantiation.
* `tests/test_pgvector_integration.py:56`: Yes. Raises `unittest.SkipTest` on `ImportError`; expected behavior for tests.
* `tests/test_pgvector_integration.py:285`: Yes. `tearDownClass()` swallows failure via broad `except` block.
