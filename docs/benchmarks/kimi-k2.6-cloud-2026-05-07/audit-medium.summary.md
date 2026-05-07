---
session_id: csl-2026-05-07-0907-5644
created: "2026-05-07T09:07:08"
question: "Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python."
models:
  planner: "kimi-k2.6:cloud"
  researcher: "kimi-k2.6:cloud"
  synthesizer: "kimi-k2.6:cloud"
topology: council
effort: medium
duration_seconds: 197.82
status: completed
cwd: /tmp/claude-hooks-bench-bench-baseline-2026-05-07-ce1r
---

- `claude_hooks/providers/pgvector.py:123` — exercisable. `PgvectorProvider.verify()` does an in-process `import psycopg`; the `except ImportError` handler returns `False` without attempting a conda-env subprocess fallback.
- `claude_hooks/providers/pgvector.py:328` — exercisable. `PgvectorProvider._ensure_ready()` does an in-process `import psycopg`; the `except ImportError` handler re-raises without attempting a conda-env fallback.
- `scripts/migrate_to_pgvector.py:624` — exercisable. Bare `import psycopg` inside `main()`; crashes with `ModuleNotFoundError` if psycopg is absent, with no conda fallback.
- `scripts/bench_recall.py:107` — exercisable (conditional). `import psycopg` inside `PgvectorBenchProvider.__init__`; crashes with `ModuleNotFoundError` if the benchmark code path is hit and psycopg is absent.
- `tests/test_pgvector_integration.py:56` — exercisable from pytest. `_skip_if_no_deps()` imports psycopg in-process; raises `SkipTest` on `ImportError`, no conda fallback.
- `tests/test_pgvector_integration.py:285` — exercisable from pytest. `tearDownClass` imports psycopg in-process; a broad `except Exception: pass` swallows the error, but no conda fallback is attempted.
