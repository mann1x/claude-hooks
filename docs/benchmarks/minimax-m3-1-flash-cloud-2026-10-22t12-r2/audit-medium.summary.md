---
session_id: csl-2026-10-22-1749-23e7
created: "2026-10-22T17:49:27"
question: "Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python."
models:
  planner: "minimax-m3.1-flash:cloud"
  researcher: "minimax-m3.1-flash:cloud"
  synthesizer: "minimax-m3.1-flash:cloud"
topology: council
effort: medium
duration_seconds: 169.80
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-10-22/tmp/claude-hooks-bench-bench-baseline-2026-05-07-vcrw
---

Three production import sites still lack the conda subprocess fallback and are reachable from a Python without `psycopg`: `claude_hooks/providers/pgvector.py:328`, `scripts/migrate_to_pgvector.py:624`, and `scripts/bench_recall.py:107`. A fourth site, `claude_hooks/providers/pgvector.py:123`, lacks the fallback but is only directly exercisable through the public provider API—not through the normal pgvector installer path.

| file:line | Purpose | Conda subprocess fallback? | Exercisable from non-conda Python? | Assessment / reachability |
|---|---|---:|---:|---|
| `claude_hooks/providers/pgvector.py:123` | `PgvectorProvider.verify()` imports `psycopg`; missing dependency is caught and the method returns `False`. `claude_hooks/providers/pgvector.py:123` `claude_hooks/providers/pgvector.py:124` `claude_hooks/providers/pgvector.py:126` | **No** | **Yes, but not through the normal installer path** | The site is directly reachable by external code calling the public `PgvectorProvider.verify()` method. However, `_verify_pgvector_dsn()` first imports `psycopg` itself and catches `ImportError` before calling `PgvectorProvider.verify()`, then uses the conda subprocess. `install.py:2127` `install.py:2132` `install.py:2135` `install.py:2146` Thus a non-conda installer does **not** reach line 123: its own import fails first. The generic provider-verification loop also explicitly skips pgvector. `install.py:3540` `install.py:3547` |
| `claude_hooks/providers/pgvector.py:328` | `PgvectorProvider._ensure_ready()` imports `psycopg` and opens the configured DSN. `claude_hooks/providers/pgvector.py:320 [no psycopg at this line; line is in _ensure_ready]` `claude_hooks/providers/pgvector.py:328` `claude_hooks/providers/pgvector.py:330` `claude_hooks/providers/pgvector.py:334` | **No** | **Yes — genuine production gap** | `_ensure_ready()` is called by provider operations including `recall()`, `store()`, `count()`, and batch operations. `claude_hooks/providers/pgvector.py:148 [no count at this line; line is in recall]` `claude_hooks/providers/pgvector.py:214 [no count at this line; line is in store]` `claude_hooks/providers/pgvector.py:241` `claude_hooks/providers/pgvector.py:265` `claude_hooks/providers/pgvector.py:292` The MCP server dispatches directly to `recall()`, `store()`, and `count()`. `claude_hooks/pgvector_mcp/server.py:271 [no count at this line; line is in _dispatch_tool]` `claude_hooks/pgvector_mcp/server.py:280 [no count at this line; line is in _dispatch_tool]` `claude_hooks/pgvector_mcp/server.py:283` The launcher selects a repo `.venv` before the conda environment, so a `.venv` lacking `psycopg` can run this code even while the separate conda environment contains it. `bin/_resolve_python.sh:31` `bin/_resolve_python.sh:32` `bin/_resolve_python.sh:35` `bin/claude-hook-pgvector-mcp:10` `bin/claude-hook-pgvector-mcp:17` Missing `psycopg` is converted into an `ImportError`; no conda retry occurs. |
| `scripts/migrate_to_pgvector.py:624` | `main()` imports `psycopg`, then immediately opens the migration connection. `scripts/migrate_to_pgvector.py:610 [no psycopg at this line; line is in main]` `scripts/migrate_to_pgvector.py:624` `scripts/migrate_to_pgvector.py:626` | **No** | **Yes — documented CLI path** | The script has a direct `__main__` entry point. `scripts/migrate_to_pgvector.py:653` The runbook documents invoking it as `python scripts/migrate_to_pgvector.py`. `docs/pgvector-runbook.md:50` `docs/pgvector-runbook.md:103` Therefore a non-conda Python without `psycopg` reaches the unguarded import and fails there. |
| `scripts/bench_recall.py:107` | `PgvectorBenchProvider.__init__()` imports `psycopg` and immediately connects to PostgreSQL. `scripts/bench_recall.py:105 [no psycopg at this line; line is in PgvectorBenchProvider]` `scripts/bench_recall.py:107` `scripts/bench_recall.py:108` | **No** | **Yes — documented CLI path** | The benchmark CLI constructs `PgvectorBenchProvider` for each selected model. `scripts/bench_recall.py:214 [no PgvectorBenchProvider at this line; line is in main]` `scripts/bench_recall.py:238` `scripts/bench_recall.py:239` It has a direct `__main__` entry point, and the runbook documents `python scripts/bench_recall.py`. `scripts/bench_recall.py:322` `docs/pgvector-runbook.md:51` `docs/pgvector-runbook.md:531` A non-conda Python without `psycopg` reaches the import when the provider is constructed. |

Excluded import occurrences:

- `install.py:2127` already has the fallback: the current interpreter is tried first, and an `ImportError` leads to `find_conda_env_python()` and a `subprocess.run()` retry. `install.py:2115` `install.py:2127` `install.py:2135` `install.py:2140` `install.py:2146`
- `install.py:2524` already has the equivalent fallback. `install.py:2515` `install.py:2524` `install.py:2536` `install.py:2540` `install.py:2557`
- `install.py:2647` already has the equivalent fallback. `install.py:2631` `install.py:2647` `install.py:2653` `install.py:2657` `install.py:2673`
- `install.py:2092`, `install.py:2546`, and `install.py:2666` are imports embedded in subprocess script strings, not additional parent-process import sites. `install.py:2089` `install.py:2092` `install.py:2543` `install.py:2546` `install.py:2664` `install.py:2666`
- `tests/test_pgvector_integration.py:56` is a dependency probe that converts missing `psycopg` into a test skip. `tests/test_pgvector_integration.py:54 [no psycopg at this line; line is in _skip_if_no_deps]` `tests/test_pgvector_integration.py:56` `tests/test_pgvector_integration.py:58`
- `tests/test_pgvector_integration.py:285` is test cleanup whose import is inside exception-swallowing teardown, not a production runtime path. `tests/test_pgvector_integration.py:285` `tests/test_pgvector_integration.py:293`

Reference limitation: the available evidence verifies the current-source fallback pattern, but commit `4e67dc2` itself could not be inspected directly, so attribution to that exact commit is unverified.
