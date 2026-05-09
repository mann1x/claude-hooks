---
session_id: csl-2026-05-09-0730-d194
created: "2026-05-09T07:30:45"
question: "Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python."
models:
  planner: "gemini-3-flash-preview:cloud"
  researcher: "gemini-3-flash-preview:cloud"
  synthesizer: "gemini-3-flash-preview:cloud"
topology: council
effort: medium
duration_seconds: 44.89
status: completed
cwd: /tmp/claude-hooks-bench-bench-baseline-2026-05-07-VC2D
---

The following `psycopg` import sites lack the conda-env subprocess fallback introduced in `4e67dc2` and are exercisable from a non-conda-env Python:

*   **`claude_hooks/providers/pgvector.py:123`**: Exercisable via `PgvectorProvider.verify()`. It catches `ImportError` and returns `False` immediately, bypassing any attempt to locate `psycopg` in the `claude-hooks` conda environment.
*   **`claude_hooks/providers/pgvector.py:328`**: Exercisable via `PgvectorProvider._ensure_ready()`. It re-raises a bare `ImportError` if the package is missing from the current `sys.path`.
*   **`scripts/migrate_to_pgvector.py:624`**: Exercisable when running the migration script directly via a system interpreter. The `import` is inside `main()` and lacks any fallback logic.
*   **`scripts/bench_recall.py:107`**: Exercisable when executing recall benchmarks from a non-conda environment. The `import` is inside `PgvectorBenchProvider.__init__`.
*   **`tests/test_pgvector_integration.py:56`**: Exercisable during test discovery/execution. It uses `ImportError` to trigger `unittest.SkipTest` but does not check the conda environment first.
*   **`tests/test_pgvector_integration.py:285`**: Exercisable during `tearDownClass`. It performs a bare `import` inside a broad `except Exception` block which would swallow the failure but skip the intended cleanup.

Sites in `install.py` (`2127`, `2524`, `2647`) already implement the `find_conda_env_python()` fallback and are not listed here.
