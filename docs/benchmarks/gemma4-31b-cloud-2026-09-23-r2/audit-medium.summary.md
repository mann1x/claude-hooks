---
session_id: csl-2026-09-23-2036-f28a
created: "2026-09-23T20:36:50"
question: "Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python."
models:
  planner: "gemma4:31b-cloud"
  researcher: "gemma4:31b-cloud"
  synthesizer: "gemma4:31b-cloud"
topology: council
effort: medium
duration_seconds: 18.53
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-09-23/tmp/claude-hooks-bench-bench-baseline-2026-05-07-MMc4
---

The following `psycopg` import sites lack the conda-env subprocess fallback:

*   `claude_hooks/providers/pgvector.py:123`: **Yes**. Exercisable via `PgvectorProvider.verify()`; currently returns `False` upon `ImportError` rather than attempting fallback.
*   `claude_hooks/providers/pgvector.py:328`: **Yes**. Exercisable via `PgvectorProvider._ensure_ready()` during `recall()` or `store()` operations; raises `ImportError` if the provider is enabled but the package is missing.
*   `scripts/migrate_to_pgvector.py:624`: **Yes**. This is a top-level import in `main()`; running the script via a non-conda Python interpreter will cause an immediate crash.
*   `scripts/bench_recall.py:107`: **Yes**. Occurs within `PgvectorBenchProvider.__init__`; running the benchmark via a non-conda Python interpreter will cause an `ImportError`.

Note: `install.py` (lines 2127, 2524, 2647) was audited and found to correctly implement the fallback logic at `install.py:2140`, `install.py:2540`, and `install.py:2657` respectively.
