---
session_id: csl-2026-09-23-1815-3041
created: "2026-09-23T18:15:20"
question: "Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python."
models:
  planner: "deepseek-v4-pro:cloud"
  researcher: "deepseek-v4-pro:cloud"
  synthesizer: "deepseek-v4-pro:cloud"
topology: council
effort: medium
duration_seconds: 140.35
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-09-23/tmp/claude-hooks-bench-bench-baseline-2026-05-07-0ZSm
---

The unpatched psycopg import sites lacking the 4e67dc2 conda-env subprocess fallback are four production-reachable sites plus two test-only sites:

- `claude_hooks/providers/pgvector.py:123` — `import psycopg` in `PgvectorProvider.verify()`; catches `ImportError` and returns `False` (`pgvector.py:124-126`). Exercisable from a non-conda-env Python: yes, if `verify()` is called directly; it degrades instead of crashing, but does not use the conda-env fallback. The installer caller is guarded at `install.py:2126`, so the installer avoids this path when psycopg is absent.
- `claude_hooks/providers/pgvector.py:328` — `import psycopg` in `_ensure_ready()`; `except ImportError` re-raises (`pgvector.py:329-330`). Exercisable: yes — any `recall()`/`store()`/`batch_*` connection path reaches it, and a non-conda Python without psycopg crashes.
- `scripts/migrate_to_pgvector.py:624` — bare `import psycopg` in `main()`; no guard/fallback; CLI entry at `scripts/migrate_to_pgvector.py:653`. Exercisable: yes; crashes with `ModuleNotFoundError`.
- `scripts/bench_recall.py:107` — bare `import psycopg` in `PgvectorBenchProvider.__init__`; no guard/fallback; constructed in the benchmark branch at `scripts/bench_recall.py:239`. Exercisable: conditionally yes; crashes if psycopg is absent.
- `tests/test_pgvector_integration.py:56` — test helper `_skip_if_no_deps()`; `except ImportError` raises `SkipTest` (`:57-58`). Exercisable under pytest: yes; correct skip, not a production gap.
- `tests/test_pgvector_integration.py:285` — test `tearDownClass`; broad `except Exception: pass` (`:293-294`). Exercisable under pytest: yes; silently swallowed, not production.

Covered/excluded: `install.py:2127`, `install.py:2524`, and `install.py:2647` already have the fast-path → `find_conda_env_python()` subprocess fallback; `install.py:2092`, `install.py:2546`, and `install.py:2666` are inside the embedded subprocess scripts themselves. No `from psycopg` or `import psycopg2` sites were found.
