---
session_id: csl-2026-05-09-0754-e412
created: "2026-05-09T07:54:19"
question: "Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python."
models:
  planner: "deepseek-v4-flash:cloud"
  researcher: "deepseek-v4-flash:cloud"
  synthesizer: "deepseek-v4-flash:cloud"
topology: council
effort: medium
duration_seconds: 96.54
status: completed
cwd: /tmp/claude-hooks-bench-bench-baseline-2026-05-07-Nvir
---

4 production-relevant import sites lack the conda‑env subprocess fallback introduced in 4e67dc2; all are exercisable from a non‑conda‑env Python. Two test‑only sites also lack the fallback but are benign (graceful skip or no‑op).

- `claude_hooks/providers/pgvector.py:123` – `import psycopg` inside `PgvectorProvider.verify()`; wrapped in `try/except ImportError` that returns `False`. Exercisable: yes. Called from `install.py:2132` (fast‑path `_verify_pgvector_dsn`) and from `detect.py`; no conda guard in call chain.
- `claude_hooks/providers/pgvector.py:328` – `import psycopg` inside `PgvectorProvider._ensure_ready()`; wrapped in `try/except ImportError` that re‑raises with a pip message. Exercisable: yes. Called by `store()`/`recall()` (via dispatcher); no conda fallback, so non‑conda Python immediately crashes with `ImportError`.
- `scripts/migrate_to_pgvector.py:624` – bare `import psycopg` inside `main()`; no error handling. Exercisable: yes. Crashes with `ModuleNotFoundError` when run from system Python.
- `scripts/bench_recall.py:107` – bare `import psycopg` inside `PgvectorBenchProvider.__init__()`; no error handling. Exercisable: yes (conditional on bench path). Crashes with `ModuleNotFoundError` when hit from non‑conda Python.
- `tests/test_pgvector_integration.py:56` – `import psycopg` inside `_skip_if_no_deps()`; catches `ImportError` and raises `unittest.SkipTest`. Exercisable: yes but benign – test skipped gracefully.
- `tests/test_pgvector_integration.py:285` – `import psycopg` inside `tearDownClass()`; blanket `except Exception: pass`. Exercisable: yes but harmless – any failure silently swallowed.
