---
session_id: csl-2026-05-07-0810-5c85
created: "2026-05-07T08:10:37"
question: "Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python."
models:
  planner: "kimi-k2.6:cloud"
  researcher: "kimi-k2.6:cloud"
  synthesizer: "kimi-k2.6:cloud"
topology: council
effort: medium
duration_seconds: 164.59
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/claude-hooks
---

Six psycopg import sites across four files still lack the conda-env subprocess fallback and are all exercisable from a non-conda-env Python.

- `claude_hooks/providers/pgvector.py:123` — Exercisable: yes. `PgvectorProvider.verify()` imports psycopg in-process; `except ImportError` returns `False` but never tries a conda-env subprocess.
- `claude_hooks/providers/pgvector.py:328` — Exercisable: yes. `PgvectorProvider._ensure_ready()` imports psycopg in-process; `except ImportError` re-raises without a conda-env fallback.
- `scripts/migrate_to_pgvector.py:624` — Exercisable: yes. Bare `import psycopg` in `main()` with no guard or conda fallback; crashes with `ModuleNotFoundError` when missing.
- `scripts/bench_recall.py:107` — Exercisable: yes. Bare `import psycopg` in `PgvectorBenchProvider.__init__` with no guard or conda fallback; crashes with `ModuleNotFoundError` when missing.
- `tests/test_pgvector_integration.py:56` — Exercisable: yes. `_skip_if_no_deps()` imports psycopg in-process and raises `SkipTest` on `ImportError`, but does not attempt a conda-env subprocess.
- `tests/test_pgvector_integration.py:285` — Exercisable: yes. `tearDownClass` imports psycopg in-process; a broad `except Exception: pass` swallows the error, but no conda fallback is attempted.
