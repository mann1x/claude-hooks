---
session_id: csl-2026-05-09-0805-017a
created: "2026-05-09T08:05:56"
question: "Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python."
models:
  planner: "gemini-3-flash-preview:cloud"
  researcher: "gemini-3-flash-preview:cloud"
  synthesizer: "gemini-3-flash-preview:cloud"
topology: council
effort: medium
duration_seconds: 60.59
status: completed
cwd: /tmp/claude-hooks-bench-bench-baseline-2026-05-07-qba3
---

Six `psycopg` import sites lack the conda-env subprocess fallback (as seen in `install.py:2127`, `install.py:2524`, and `install.py:2647`):

*   **`claude_hooks/providers/pgvector.py:123`**: Lacks fallback. Catches `ImportError` and returns `False` inside `verify()`. **Exercisable** during provider discovery/validation; prevents use even if the conda environment is configured correctly.
*   **`claude_hooks/providers/pgvector.py:328`**: Lacks fallback. Catches `ImportError` and re-raises it with manual install instructions inside `_ensure_ready()`. **Exercisable** during any database recall or store operation; results in a fatal error instead of falling back.
*   **`scripts/migrate_to_pgvector.py:624`**: Lacks fallback. Bare `import psycopg` in `main()`. **Exercisable** via direct script execution; will crash with `ModuleNotFoundError` if run outside the specific conda environment.
*   **`scripts/bench_recall.py:107`**: Lacks fallback. Bare `import psycopg` in `PgvectorBenchProvider.__init__`. **Exercisable** during benchmarking; will crash with `ModuleNotFoundError`.
*   **`tests/test_pgvector_integration.py:56`**: Lacks fallback. Catches `ImportError` to raise `unittest.SkipTest`. **Exercisable** during test execution; prevents the suite from verifying functionality via the conda-env bridge.
*   **`tests/test_pgvector_integration.py:285`**: Lacks fallback. Wrapped in a broad `try/except` that swallows the error in `tearDownClass()`. **Exercisable** during test cleanup; silently fails to drop test tables if the local interpreter lacks the package.
