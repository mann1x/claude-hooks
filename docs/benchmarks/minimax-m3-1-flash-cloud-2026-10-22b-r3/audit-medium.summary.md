---
session_id: csl-2026-10-22-1701-a0f1
created: "2026-10-22T17:01:25"
question: "Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python."
models:
  planner: "minimax-m3.1-flash:cloud"
  researcher: "minimax-m3.1-flash:cloud"
  synthesizer: "minimax-m3.1-flash:cloud"
topology: council
effort: medium
duration_seconds: 134.13
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-10-22/tmp/claude-hooks-bench-bench-baseline-2026-05-07-YKSM
---

Four production/operator import sites still lack the conda-interpreter fallback; two are reachable in normal hook runtime or its provider APIs, and two are CLI-only. The remaining eight sites are either already covered or intentionally dependency-gated.

| Import site | Classification | Exercisable by a Python without `psycopg`? |
|---|---|---|
| `claude_hooks/providers/pgvector.py:123` | **Uncovered:** `PgvectorProvider.verify()` directly imports `psycopg`; `ImportError` is converted to `False`, with no conda subprocess (`claude_hooks/providers/pgvector.py:120 [no psycopg at this line; line is in PgvectorProvider]`). | **Yes, when `PgvectorProvider.verify()` is called directly.** It returns `False` rather than verifying through the conda environment. The generic installer loop explicitly skips `pgvector`, so `install.py:3556` is not a generic caller of this provider method (`install.py:3546`). |
| `claude_hooks/providers/pgvector.py:328` | **Uncovered and consequential:** `PgvectorProvider._ensure_ready()` directly imports `psycopg` and raises `ImportError`; it has no conda fallback (`claude_hooks/providers/pgvector.py:320 [no psycopg at this line; line is in _ensure_ready]`). | **Yes, in ordinary hook runtime.** An enabled provider with a configured DSN is instantiated by `build_providers()` (`claude_hooks/dispatcher.py:129`), and `recall()` calls `_ensure_ready()` (`claude_hooks/providers/pgvector.py:144 [no _ensure_ready at this line; line is in recall]`). The resulting `ImportError` is caught as “pgvector unavailable,” and recall returns no results. |
| `scripts/migrate_to_pgvector.py:624` | **Uncovered:** the migration CLI directly imports `psycopg` and connects without a fallback (`scripts/migrate_to_pgvector.py:610 [no psycopg at this line; line is in main]`). | **Yes**, when this migration CLI is run with a Python lacking `psycopg`; it fails at the import rather than delegating to the conda environment. It is operator-facing, not part of normal hook dispatch. |
| `scripts/bench_recall.py:107` | **Uncovered:** `PgvectorBenchProvider` directly imports `psycopg` and connects without a fallback (`scripts/bench_recall.py:105 [no psycopg at this line; line is in PgvectorBenchProvider]`). | **Yes**, when the pgvector benchmark provider is selected. It is benchmark-only, not hook runtime. |

Already covered or intentionally excluded:

- `install.py:2092` — **covered, subprocess-only:** this import is inside `_PGVECTOR_VERIFY_SCRIPT`; the script is executed with the discovered conda Python rather than the caller’s interpreter (`install.py:2089`, `install.py:2138`).
- `install.py:2127` — **covered:** the fast-path import falls back to a subprocess running with the conda Python when unavailable (`install.py:2125`, `install.py:2138`).
- `install.py:2524` — **covered:** `_pgvector_tables_present()` catches import failure and retries through the conda interpreter (`install.py:2523`, `install.py:2543`).
- `install.py:2546` — **covered, subprocess-only:** the table-check script imports `psycopg` under the selected conda interpreter (`install.py:2543`).
- `install.py:2647` — **covered:** `_init_pgvector_schema()` falls back to a conda-interpreter subprocess (`install.py:2645`).
- `install.py:2666` — **covered, subprocess-only:** the schema script imports `psycopg` under the selected conda interpreter (`install.py:2664`).
- `tests/test_pgvector_integration.py:56` — **intentionally dependency-gated:** absence of `psycopg` causes the integration suite to skip, rather than attempting a conda fallback (`tests/test_pgvector_integration.py:54`).
- `tests/test_pgvector_integration.py:285` — **test cleanup only:** this is cleanup inside the dependency-gated integration suite and is not a production path (`tests/test_pgvector_integration.py:281`).

The installer fallback tests cover the installer’s fast-path and conda-subprocess behavior, not the provider-level imports or standalone scripts (`tests/test_install_pgvector_verify.py:34`, `tests/test_install_pgvector_verify.py:63`).

The exact diff for commit `4e67dc2` could not be inspected; the classification above is based on the current source’s explicit fallback implementations. Excluding generated `.claude-hooks/` artifacts, the inventory contains 12 `import psycopg` sites and no `from psycopg` sites.
