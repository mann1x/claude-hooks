---
session_id: csl-2026-10-27-1447-a47b
created: "2026-10-27T14:47:13"
question: "Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python."
models:
  planner: "gemma4:31b-cloud"
  researcher: "glm-5.3-flash:cloud"
  synthesizer: "gemma4:31b-cloud"
topology: council
effort: medium
duration_seconds: 72.72
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-10-27/tmp/claude-hooks-bench-bench-baseline-2026-05-07-yIQd
---

The following `psycopg` import sites lack the `conda-env` subprocess fallback implemented in `install.py` (e.g., `install.py:2138-2143`, `install.py:2539-2555`, `install.py:2656-2676`).

*   **`claude_hooks/providers/pgvector.py:123`**
    *   **Exercisable:** Yes. Called during hook configuration loading and installation detection.
    *   **Behavior:** On `ImportError`, it logs a warning and returns `False` (`pgvector.py:124-126`), resulting in a silent failure to verify the provider.
*   **`claude_hooks/providers/pgvector.py:328`**
    *   **Exercisable:** Yes. This is the primary runtime gate (`_ensure_ready`) for all provider operations (`recall` at `pgvector.py:148`, `store` at `pgvector.py:214`, and others at `pgvector.py:241, 265, 292, 439, 555, 592, 645, 676`).
    *   **Behavior:** Raises `ImportError` with a pip-install hint (`pgvector.py:329-330`). In the dispatcher path, this is caught and returns an empty result (`dispatcher.py:74, 87, 110`); in the `pgvector-mcp` server path, it causes the tool call to fail (`server.py:271, 276, 280`).
*   **`scripts/migrate_to_pgvector.py:624`**
    *   **Exercisable:** Yes. Standalone operator script.
    *   **Behavior:** Raw `ImportError` traceback.
*   **`scripts/bench_recall.py:107`**
    *   **Exercisable:** Yes. Standalone benchmark script.
    *   **Behavior:** Raw `ImportError` traceback.
