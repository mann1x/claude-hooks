---
session_id: csl-2026-10-22-1851-4be3
created: "2026-10-22T18:51:24"
question: "Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python."
models:
  planner: "gemma4:31b-cloud"
  researcher: "gemma4:31b-cloud"
  synthesizer: "gemma4:31b-cloud"
topology: council
effort: medium
duration_seconds: 26.63
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-10-22/tmp/claude-hooks-bench-bench-baseline-2026-05-07-JR50
---

The following sites lack the conda-env subprocess fallback:

*   `claude_hooks/providers/pgvector.py:123`: **Exercisable**. Returns `False` upon `ImportError` during verification.
*   `claude_hooks/providers/pgvector.py:328`: **Exercisable**. Raises a user-facing `ImportError` requesting `pip install` during provider readiness checks.
*   `scripts/migrate_to_pgvector.py:624`: **Exercisable**. Direct import will cause a `ModuleNotFoundError` crash if run from a non-conda Python.
*   `scripts/bench_recall.py:107`: **Exercisable**. Direct import will cause a `ModuleNotFoundError` crash if run from a non-conda Python.
*   `tests/test_pgvector_integration.py:56`: **Not exercisable**. The import is part of `_skip_if_no_deps()`, which catches the error to skip the test.
*   `tests/test_pgvector_integration.py:285 [no _skip_if_no_deps at this line; line is in tearDownClass]`: **Not exercisable**. The import in `tearDownClass()` is wrapped in a broad `try...except Exception` block.
