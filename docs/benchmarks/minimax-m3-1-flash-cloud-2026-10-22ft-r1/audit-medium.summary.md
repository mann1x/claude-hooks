---
session_id: csl-2026-10-22-2029-db1d
created: "2026-10-22T20:29:07"
question: "Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python."
models:
  planner: "minimax-m3.1-flash:cloud"
  researcher: "minimax-m3.1-flash:cloud"
  synthesizer: "minimax-m3.1-flash:cloud"
topology: council
effort: medium
duration_seconds: 158.36
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-10-22/tmp/claude-hooks-bench-bench-baseline-2026-05-07-zEA5
---

Four executable `psycopg` import sites still lack the conda-environment subprocess fallback: `claude_hooks/providers/pgvector.py:123`, `claude_hooks/providers/pgvector.py:328`, `scripts/migrate_to_pgvector.py:624`, and `scripts/bench_recall.py:107`.

### Sites still lacking the fallback

- **`claude_hooks/providers/pgvector.py:123` — directly exercisable, but no normal non-conda production path was found.**  
  `PgvectorProvider.verify()` directly imports `psycopg` and converts `ImportError` into `False`; it does not attempt the conda environment (`claude_hooks/providers/pgvector.py:123`, `claude_hooks/providers/pgvector.py:124`). The dedicated installer verifier never reaches this method without `psycopg`: its own import fails first and triggers the conda subprocess (`install.py:2127`, `install.py:2135`, `install.py:2140`, `install.py:2146`). The generic installer verification loop explicitly skips the `pgvector` provider, so its generic `cls.verify(candidate)` call is not a pgvector route (`install.py:3540`, `install.py:3541`, `install.py:3547`, `install.py:3548`, `install.py:3556`). Thus:
  - **Directly callable from non-conda Python:** yes.
  - **Reachable through an audited normal production path:** no demonstrated route.

- **`claude_hooks/providers/pgvector.py:328` — yes, conditionally exercisable from a non-conda Python.**  
  `_ensure_ready()` directly imports `psycopg` and raises an “install psycopg” error instead of using the conda environment (`claude_hooks/providers/pgvector.py:328`, `claude_hooks/providers/pgvector.py:329`, `claude_hooks/providers/pgvector.py:334`). Recall reaches that method (`claude_hooks/providers/pgvector.py:144`, `claude_hooks/providers/pgvector.py:148`). The normal dispatch path constructs configured providers (`claude_hooks/dispatcher.py:129`, `claude_hooks/dispatcher.py:131`, `claude_hooks/dispatcher.py:136`, `claude_hooks/dispatcher.py:148`), and the launcher can select system Python rather than conda Python (`bin/_resolve_python.sh:53`, `bin/claude-hook.cmd:43`). Pgvector is disabled by default (`claude_hooks/config.py:48`, `claude_hooks/config.py:49`), so the defect is reached when pgvector is **enabled, has a DSN, and the selected non-conda interpreter lacks `psycopg`**. The MCP path can also reach provider recall/store (`claude_hooks/pgvector_mcp/server.py:271 [no psycopg at this line; line is in _dispatch_tool]`, `claude_hooks/pgvector_mcp/server.py:280`, `claude_hooks/pgvector_mcp/server.py:344`).

- **`scripts/migrate_to_pgvector.py:624` — yes, directly exercisable from a non-conda Python.**  
  `main()` unconditionally imports `psycopg` and immediately connects (`scripts/migrate_to_pgvector.py:624`, `scripts/migrate_to_pgvector.py:626`). The script is a directly runnable CLI (`scripts/migrate_to_pgvector.py:653`, `scripts/migrate_to_pgvector.py:654`), and its documented invocation uses plain `python`, so a system/non-conda interpreter without `psycopg` reaches the unguarded import (`config/claude-hooks.example.json:80`).

- **`scripts/bench_recall.py:107` — yes, directly exercisable when a pgvector benchmark model is selected.**  
  The provider constructor directly imports `psycopg` and connects (`scripts/bench_recall.py:107`, `scripts/bench_recall.py:108`). The CLI instantiates the provider for selected pgvector models (`scripts/bench_recall.py:239`) and is directly runnable (`scripts/bench_recall.py:322`, `scripts/bench_recall.py:323`). Therefore a non-conda interpreter lacking `psycopg` reaches the import when the benchmark selects pgvector.

### Installer import sites that are not missing the fallback

- `install.py:2092` — inside the embedded verification script; that script is launched with the resolved conda interpreter, so it is not executed by the original non-conda Python (`install.py:2092`, `install.py:2146`).
- `install.py:2127` — fast-path import; it is reached by non-conda Python but has the `ImportError` → `find_conda_env_python()` → `subprocess.run()` fallback (`install.py:2127`, `install.py:2135`, `install.py:2140`, `install.py:2146`).
- `install.py:2524` — fast-path import with a conda subprocess fallback (`install.py:2524`, `install.py:2536`, `install.py:2540`, `install.py:2557`).
- `install.py:2546` — inside the embedded subprocess script, which is run by the conda interpreter (`install.py:2546`, `install.py:2557`).
- `install.py:2647` — fast-path import with a conda subprocess fallback (`install.py:2647`, `install.py:2653`, `install.py:2657`, `install.py:2673`).
- `install.py:2666` — inside the embedded subprocess script, which is run by the conda interpreter (`install.py:2666`, `install.py:2673`).

The integration-test import is intentionally dependency-gated (`tests/test_pgvector_integration.py:56`), its teardown import is inside that gated suite (`tests/test_pgvector_integration.py:285`), and the runbook occurrence is documentation rather than executable code (`docs/pgvector-runbook.md:499`).

Historical attribution to commit `4e67dc2` could not be verified because the commit diff was unavailable; the classification above is against the fallback pattern present in the current tree (`install.py:2125`, `install.py:2135`, `install.py:2140`, `install.py:2146`).
