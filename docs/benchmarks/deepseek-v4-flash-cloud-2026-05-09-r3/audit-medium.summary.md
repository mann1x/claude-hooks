---
session_id: csl-2026-05-09-0821-393e
created: "2026-05-09T08:21:35"
question: "Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python."
models:
  planner: "deepseek-v4-flash:cloud"
  researcher: "deepseek-v4-flash:cloud"
  synthesizer: "deepseek-v4-flash:cloud"
topology: council
effort: medium
duration_seconds: 364.03
status: completed
cwd: /tmp/claude-hooks-bench-bench-baseline-2026-05-07-5MHl
---

Three psycopg import sites lack the `4e67dc2` conda-env subprocess fallback and are **exercisable** from a non-conda-env Python:

- **`claude_hooks/providers/pgvector.py:328`** — `import psycopg` inside `_ensure_ready()`. Every `recall()`, `store()`, etc. calls this. On failure raises `RuntimeError("install psycopg: pip install psycopg[binary]")`. No attempt to shell out to conda env. **Remediation target** (high impact).
- **`scripts/migrate_to_pgvector.py:624`** — bare `import psycopg` in `main()`. No try/except. Crashes with `ModuleNotFoundError`. **Remediation target** (medium; operator script).
- **`scripts/bench_recall.py:107`** — bare `import psycopg` inside `PgvectorBenchProvider.__init__()`. No guard. Same crash. **Remediation target** (medium; benchmark script).

Three other sites lack the fallback but are **not exercisable** from a non-conda-env Python in normal use:

- `claude_hooks/providers/pgvector.py:123` — guarded by installer’s own fast-path: `install.py:2118-2161` tries `import psycopg` first and only calls `PgvectorProvider.verify()` when import succeeds. Non‑conda‑env paths are handled by the subprocess fallback in `install.py:2140`.
- `tests/test_pgvector_integration.py:56` — inside `_skip_if_no_deps()` raising `SkipTest`. Correct test behaviour.
- `tests/test_pgvector_integration.py:285` — inside a test tear-down; silence via `except Exception: pass`. Test-only low-impact.

All protected sites (`install.py:2127`, `install.py:2524`, `install.py:2647`) already use the `try: import psycopg` / `except ImportError` / `find_conda_env_python()` + `subprocess` pattern and are not bugs.
