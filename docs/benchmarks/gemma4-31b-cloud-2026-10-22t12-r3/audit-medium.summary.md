---
session_id: csl-2026-10-22-1903-3cb5
created: "2026-10-22T19:03:43"
question: "Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python."
models:
  planner: "gemma4:31b-cloud"
  researcher: "gemma4:31b-cloud"
  synthesizer: "gemma4:31b-cloud"
topology: council
effort: medium
duration_seconds: 17.54
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-10-22/tmp/claude-hooks-bench-bench-baseline-2026-05-07-6CJQ
---

*   `claude_hooks/providers/pgvector.py:123`: Exercisable. Called during provider detection/verification in the main hook loop.
*   `claude_hooks/providers/pgvector.py:328`: Exercisable. Primary connection path for recall/store operations.
*   `scripts/migrate_to_pgvector.py:624`: Exercisable. Standalone migration script; crashes on system Python.
*   `scripts/bench_recall.py:107`: Exercisable. Standalone benchmarking script; crashes on system Python.
