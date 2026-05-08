---
session_id: csl-2026-05-07-1006-bbd1
created: "2026-05-07T10:06:59"
question: "Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python."
models:
  planner: "minimax-m2.7:cloud"
  researcher: "minimax-m2.7:cloud"
  synthesizer: "minimax-m2.7:cloud"
topology: council
effort: medium
duration_seconds: 94.79
status: completed
cwd: /tmp/claude-hooks-bench-bench-baseline-2026-05-07-NhRc
---

## psycopg Import Sites Lacking conda-env Subprocess Fallback

**Reference pattern** (4e67dc2): `try: import psycopg` fast path → on `ImportError` → `find_conda_env_python()` + `subprocess.run([conda_py, "-c", script, ...])`. Present only in `install.py:2127`, `install.py:2524`, `install.py:2647`.

---

**Missing fallback — exercisable from non-conda-env Python:**

- `claude_hooks/providers/pgvector.py:123` — bare `import psycopg` in `PgvectorProvider.verify()`; `except ImportError` returns `False` without subprocess. Callable from any Python.
- `claude_hooks/providers/pgvector.py:328` — bare `import psycopg` in `_ensure_ready()`; `except ImportError` re-raises as `"install psycopg: ..."` without fallback. Callable via any `PgvectorProvider` operation (`recall`, `store`, KG ops, etc.).
- `scripts/migrate_to_pgvector.py:624` — bare `import psycopg` at module scope inside `main()`, no `try/except`, no fallback. Crashes `ModuleNotFoundError` from any non-conda Python.
- `scripts/bench_recall.py:107` — bare `import psycopg` inside `PgvectorBenchProvider.__init__()`; no guard. Crashes on class instantiation from any Python lacking psycopg.
- `tests/test_pgvector_integration.py:56` — bare `import psycopg` in `_skip_if_no_deps()`; raises `unittest.SkipTest`. Test-only; appropriate as-is.
- `tests/test_pgvector_integration.py:285` — bare `import psycopg` in `tearDownClass()`; broad `except Exception: pass`. Test teardown; silent no-op acceptable.

---

**Verdict:** The fallback pattern from 4e67dc2 is confined to `install.py` only. The two sites in `claude_hooks/providers/pgvector.py` and the two scripts are user-facing code paths that will crash from non-conda-env Python. The two test-file sites are appropriately left unguarded.
