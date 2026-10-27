---
session_id: csl-2026-10-27-1543-77f2
created: "2026-10-27T15:43:25"
question: "Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python."
models:
  planner: "gemma4:31b-cloud"
  researcher: "glm-5.3-flash:cloud"
  synthesizer: "gemma4:31b-cloud"
topology: council
effort: medium
duration_seconds: 79.71
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-10-27/tmp/claude-hooks-bench-bench-baseline-2026-05-07-8iNE
---

Four sites lack the conda-env subprocess fallback:

* `claude_hooks/providers/pgvector.py:123` — Exercisable; returns `False` and logs a warning, causing verification to fail even if a conda environment is available.
* `claude_hooks/providers/pgvector.py:328` — Exercisable; raises `ImportError` in `_ensure_ready()`, which is caught at `claude_hooks/providers/pgvector.py:149` resulting in a graceful no-op for recall/store.
* `scripts/migrate_to_pgvector.py:624` — Exercisable; causes a hard `ImportError` traceback upon execution.
* `scripts/bench_recall.py:107` — Exercisable; causes a hard `ImportError` traceback when the pgvector benchmark lane is constructed.
