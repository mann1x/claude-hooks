---
session_id: csl-2026-09-23-2024-bf8b
created: "2026-09-23T20:24:29"
question: "Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python."
models:
  planner: "gemma4:31b-cloud"
  researcher: "gemma4:31b-cloud"
  synthesizer: "gemma4:31b-cloud"
topology: council
effort: medium
duration_seconds: 13.24
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-09-23/tmp/claude-hooks-bench-bench-baseline-2026-05-07-Zddh
---

The following sites lack the conda-env subprocess fallback and use direct imports that will fail if `psycopg` is not installed in the active Python environment:

* `scripts/migrate_to_pgvector.py:624` — **Exercisable**: Main entry point for migration; triggers `ImportError` if run from a non-conda Python.
* `scripts/bench_recall.py:107` — **Exercisable**: Provider instantiation for benchmarking; triggers `ImportError` if run from a non-conda Python.
* `tests/test_pgvector_integration.py:56` — **Exercisable**: Called by `setUpClass`; triggers `ImportError` (handled as `SkipTest`) without attempting a fallback.
* `tests/test_pgvector_integration.py:285 [no SkipTest at this line; line is in tearDownClass]` — **Exercisable**: Called in `tearDownClass`; does not attempt fallback interpreter resolution.
