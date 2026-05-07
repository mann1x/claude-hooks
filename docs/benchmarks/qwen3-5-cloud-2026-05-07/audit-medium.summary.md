---
session_id: csl-2026-05-07-1032-1b25
created: "2026-05-07T10:32:15"
question: "Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python."
models:
  planner: "qwen3.5:cloud"
  researcher: "qwen3.5:cloud"
  synthesizer: "qwen3.5:cloud"
topology: council
effort: medium
duration_seconds: 87.79
status: completed
cwd: /tmp/claude-hooks-bench-bench-baseline-2026-05-07-3KOY
---

Two psycopg import sites lack the conda-env subprocess fallback; both are fully exercisable from non-conda Python environments. The referenced commit `4e67dc2` does not exist in the repository history.

*   `claude_hooks/providers/pgvector.py:123` — **Exercisable: YES** — Import inside `PgvectorProvider.verify()` lacks `CONDA_PREFIX` guards or subprocess spawn logic; returns `False` on `ImportError` without attempting fallback.
*   `claude_hooks/providers/pgvector.py:328` — **Exercisable: YES** — Import inside `PgvectorProvider._ensure_ready()` lacks fallback; raises `ImportError` directly if psycopg is missing in the current interpreter.

Grep for `subprocess.*python` and `CONDA_PREFIX` confirms zero matches in `pgvector.py`, though similar patterns exist in `install.py:101-196` (installer-only) and `claude_hooks/consultants_forwarder.py:346-363` (engine spawning).
