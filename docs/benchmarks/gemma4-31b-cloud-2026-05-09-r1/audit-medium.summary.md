---
session_id: csl-2026-05-09-0859-c570
created: "2026-05-09T08:59:03"
question: "Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python."
models:
  planner: "gemma4:31b-cloud"
  researcher: "gemma4:31b-cloud"
  synthesizer: "gemma4:31b-cloud"
topology: council
effort: medium
duration_seconds: 62.24
status: completed
cwd: /tmp/claude-hooks-bench-bench-baseline-2026-05-07-c512
---

Six sites lack the conda-env subprocess fallback, all of which are exercisable from a non-conda-env Python.

* `scripts/migrate_to_pgvector.py:624`: Yes (Bare import; crashes with `ModuleNotFoundError`).
* `scripts/bench_recall.py:107`: Yes (Bare import; crashes with `ModuleNotFoundError`).
* `claude_hooks/providers/pgvector.py:123`: Yes (Catches `ImportError` and returns `False` without attempting fallback).
* `claude_hooks/providers/pgvector.py:328`: Yes (Catches `ImportError` and re-raises without attempting fallback).
* `tests/test_pgvector_integration.py:56`: Yes (Catches `ImportError` to trigger `unittest.SkipTest`).
* `tests/test_pgvector_integration.py:285`: Yes (Import failure swallowed by broad exception handler).
