---
session_id: csl-2026-10-27-1114-58b8
created: "2026-10-27T11:14:45"
question: "Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python."
models:
  planner: "gemma4:31b-cloud"
  researcher: "gemma4:31b-cloud"
  synthesizer: "gemma4:31b-cloud"
topology: council
effort: medium
duration_seconds: 35.87
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-10-27/tmp/claude-hooks-bench-bench-baseline-2026-05-07-FFQy
---

The following `psycopg` import sites lack the conda-env subprocess fallback implemented in `install.py:2126-2148` and `install.py:2523-2540`:

* `claude_hooks/providers/pgvector.py:123`: Exercisable. Returns `False` and logs a warning upon `ImportError` (`claude_hooks/providers/pgvector.py:125`).
* `claude_hooks/providers/pgvector.py:328`: Exercisable. Raises `ImportError` (`claude_hooks/providers/pgvector.py:330`), causing runtime crashes if the provider is enabled but `psycopg` is missing.
* `scripts/migrate_to_pgvector.py:624`: Exercisable. Triggers an immediate `ImportError` crash when run from a non-conda environment.
* `scripts/bench_recall.py:107`: Exercisable. Triggers an immediate `ImportError` crash when run from a non-conda environment.
* `tests/test_pgvector_integration.py:56`: Exercisable. Catches `ImportError` to raise `unittest.SkipTest`, preventing a crash but bypassing the fallback.
* `tests/test_pgvector_integration.py:285 [no SkipTest at this line; line is in tearDownClass]`: Exercisable. Wrapped in a broad `try...except Exception` block, resulting in silent failure.
