---
session_id: csl-2026-05-09-0706-5fb7
created: "2026-05-09T07:06:15"
question: "Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python."
models:
  planner: "deepseek-v4-pro:cloud"
  researcher: "deepseek-v4-pro:cloud"
  synthesizer: "deepseek-v4-pro:cloud"
topology: council
effort: medium
duration_seconds: 231.53
status: completed
cwd: /tmp/claude-hooks-bench-bench-baseline-2026-05-07-MszE
---

Two sites lack the conda‑env subprocess fallback and are exercisable from a non‑conda‑env Python; four additional sites lack the fallback but are not exercisable in practice.

- `scripts/migrate_to_pgvector.py:624` – bare `import psycopg`, no guard. Exercisable: yes. A direct `python scripts/migrate_to_pgvector.py` with a non‑conda Python crashes with `ModuleNotFoundError`.
- `scripts/bench_recall.py:107` – bare `import psycopg` inside `PgvectorBenchProvider.__init__()`, no guard. Exercisable: conditionally yes. When `--provider pgvector` is passed and the script is run from a non‑conda Python, instantiation crashes.
- `claude_hooks/providers/pgvector.py:123` – `try: import psycopg` / `except: return False`, no subprocess fallback. Not exercisable. All callers to `verify()` are either inside the conda‑env runtime enforced by `bin/_resolve_python.sh:35-44`, or reach the method only after `install.py`’s own psycopg‑guaranteed fast‑path.
- `claude_hooks/providers/pgvector.py:328` – `try: import psycopg` / `except: raise ImportError`, no subprocess fallback. Not exercisable. `_ensure_ready()` is only called by `recall()`/`store()` which run inside the conda‑env‑guaranteed hook framework.
- `tests/test_pgvector_integration.py:56` – `try: import psycopg` → `SkipTest`, no fallback. Not exercisable. The test suite is always launched via `make test`, which uses the conda‑env Python (`Makefile:7`).
- `tests/test_pgvector_integration.py:285` – bare `import psycopg` inside a broad `except: pass`, no fallback. Not exercisable for the same reason as line 56.
