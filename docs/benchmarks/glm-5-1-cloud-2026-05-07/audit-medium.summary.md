---
session_id: csl-2026-05-07-0957-ccac
created: "2026-05-07T09:57:38"
question: "Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python."
models:
  planner: "glm-5.1:cloud"
  researcher: "glm-5.1:cloud"
  synthesizer: "glm-5.1:cloud"
topology: council
effort: medium
duration_seconds: 55.67
status: completed
cwd: /tmp/claude-hooks-bench-bench-baseline-2026-05-07-e4FI
---

Four psycopg import sites lack the conda-env subprocess fallback that `install.py:2127`, `install.py:2524`, and `install.py:2647` already have:

- **`claude_hooks/providers/pgvector.py:123`** — bare `import psycopg` in `verify()`, wrapped in try/except that returns `False` on `ImportError`. **Exercisable from non-conda Python**: fires whenever the pgvector provider is enabled and `verify()` is called (e.g. by the installer or daemon). Won't crash, but silently degrades — no fallback to conda env's Python.

- **`claude_hooks/providers/pgvector.py:328`** — bare `import psycopg` in `_ensure_ready()`, wrapped in try/except that re-raises `ImportError` with an install hint. **Exercisable from non-conda Python**: called on every `recall()`/`store()` when pgvector provider is enabled. Hard failure for that provider's operations (caught by dispatcher, so hook still exits 0).

- **`scripts/migrate_to_pgvector.py:624`** — bare `import psycopg` at top of `main()`, no try/except, no fallback. **Exercisable from non-conda Python**: runs via `__main__` or CLI invocation; crashes immediately on `ImportError`. Lower risk (operator script, typically run with conda env active), but has zero guard.

- **`scripts/bench_recall.py:107`** — bare `import psycopg` in `PgvectorBenchProvider.__init__()`, no try/except, no fallback. **Exercisable from non-conda Python**: crashes on instantiation if psycopg absent. Same low-risk operator-script caveat as above.

Note: `psycopg[binary]` is an optional dependency (`pyproject.toml:38` — `pgvector = ["psycopg[binary]"]`), so a default install won't have it, making these paths reachable precisely when a user enables the pgvector provider but the hook shim resolves to a Python outside the conda env. The two `pgvector.py` sites are the highest priority to fix since they sit on the live hook dispatch path; the `scripts/` sites are lower priority but still unprotected.
