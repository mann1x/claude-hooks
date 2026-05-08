---
session_id: csl-2026-05-07-1023-c9ca
created: "2026-05-07T10:23:55"
question: "Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python."
models:
  planner: "qwen3.5:397b-cloud"
  researcher: "qwen3.5:397b-cloud"
  synthesizer: "qwen3.5:397b-cloud"
topology: council
effort: medium
duration_seconds: 78.61
status: completed
cwd: /tmp/claude-hooks-bench-bench-baseline-2026-05-07-NsBk
---

Commit `4e67dc2` does not exist in this repository; no conda-env subprocess fallback for `psycopg` is implemented in `claude_hooks/`. Two `psycopg` import sites lack environment guards and depend entirely on the current interpreter's installed packages.

*   `claude_hooks/providers/pgvector.py:123` — Inside `PgvectorProvider.verify()`. **Reachable:** Yes. **Behavior:** Returns `False` on `ImportError`. No conda fallback.
*   `claude_hooks/providers/pgvector.py:328` — Inside `PgvectorProvider._ensure_ready()`. **Reachable:** Yes. **Behavior:** Raises `ImportError` on failure. No conda fallback.

Both sites are exercisable from non-conda Python but will fail or degrade if `psycopg` is not installed in the active environment. Conda-env subprocess patterns exist for other components (e.g., `claude_hooks/consultants_forwarder.py:347-358` for consultant binaries, `claude_hooks/store_async.py:78` for detached workers) but are not applied to `psycopg` imports.
