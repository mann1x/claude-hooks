---
session_id: csl-2026-05-09-0634-76f0
created: "2026-05-09T06:34:18"
question: "Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python."
models:
  planner: "mistral-large-3:675b-cloud"
  researcher: "mistral-large-3:675b-cloud"
  synthesizer: "mistral-large-3:675b-cloud"
topology: council
effort: medium
duration_seconds: 717.90
status: completed
cwd: /tmp/claude-hooks-bench-bench-baseline-2026-05-07-5PKY
---

- `claude_hooks/providers/pgvector.py:123` – ❌ Not exercisable outside conda env (import in `verify()` called only during conda-env setup, no subprocess fallback).
- `claude_hooks/providers/pgvector.py:328` – ❌ Not exercisable outside conda env (import in `_ensure_ready()` called by core provider methods, no subprocess fallback).
- `scripts/migrate_to_pgvector.py:624` – ✅ Exercisable (bare import in `main()`, no guards or fallback).
- `scripts/bench_recall.py:107` – ✅ Exercisable (bare import in `PgvectorBenchProvider.__init__()`, no guards or fallback).
