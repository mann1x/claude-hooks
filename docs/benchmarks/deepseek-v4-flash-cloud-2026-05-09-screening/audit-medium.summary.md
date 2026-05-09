---
session_id: csl-2026-05-09-0654-e2ce
created: "2026-05-09T06:54:53"
question: "Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python."
models:
  planner: "deepseek-v4-flash:cloud"
  researcher: "deepseek-v4-flash:cloud"
  synthesizer: "deepseek-v4-flash:cloud"
topology: council
effort: medium
duration_seconds: 157.19
status: completed
cwd: /tmp/claude-hooks-bench-bench-baseline-2026-05-07-Uaj8
---

- `claude_hooks/providers/pgvector.py:123` — **EXERCISABLE** (inside `verify()`, called from `install.py` which runs under system Python, no shim).  
- `claude_hooks/providers/pgvector.py:328` — **NOT EXERCISABLE** (inside `_ensure_ready()`; production call chain `bin/` → `_resolve_python.sh` forces conda-env Python, so non-conda interpreter never reaches it).  
- `scripts/migrate_to_pgvector.py:624` — **EXERCISABLE** (bare `import psycopg` in `main()`, invoked directly via `python scripts/migrate_to_pgvector.py`; crashes with `ModuleNotFoundError` outside conda).  
- `scripts/bench_recall.py:107` — **EXERCISABLE** (same pattern, inside `PgvectorBenchProvider.__init__()`, invoked via `python scripts/bench_recall.py`; crashes with `ModuleNotFoundError`).  
- `tests/test_pgvector_integration.py:56` — **EXERCISABLE** (inside `_skip_if_no_deps()`, called from `setUpClass`; pytest collection from any interpreter hits it, gracefully raises `SkipTest`).  
- `tests/test_pgvector_integration.py:285` — **NOT EXERCISABLE** (inside `tearDownClass()`; runs only if `setUpClass` succeeded, which already imported psycopg at line 56 — if psycopg is missing the class is skipped, so line 285 never executes under non-conda Python).  

**Remediation priority**: `scripts/migrate_to_pgvector.py:624` and `scripts/bench_recall.py:107` are the highest-risk — they crash hard with no fallback or helpful error. `pgvector.py:123` silently returns `False` but never probes the conda env, causing degraded functionality without alerting the user.
