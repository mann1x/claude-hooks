---
session_id: csl-2026-09-23-1823-b6c2
created: "2026-09-23T18:23:12"
question: "Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python."
models:
  planner: "glm-5.3:cloud"
  researcher: "glm-5.3:cloud"
  synthesizer: "glm-5.3:cloud"
topology: council
effort: medium
duration_seconds: 92.18
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-09-23/tmp/claude-hooks-bench-bench-baseline-2026-05-07-7w8z
---

**Six import sites still lack the 4e67dc2 conda-env subprocess fallback; the three sites in `install.py` are the only protected ones.** The fallback (`find_conda_env_python` at `install.py:101`) has zero call sites outside `install.py`, so everything below degrades whenever it runs under a Python without psycopg.

**Exercisable in production from a non-conda Python (the real gaps):**

- `claude_hooks/providers/pgvector.py:123` — `import psycopg` in `verify()`; `except ImportError` at `claude_hooks/providers/pgvector.py:124-126` logs a warning and returns `False`, no subprocess attempt. **Yes, exercisable**: the MCP server imports the provider at `claude_hooks/pgvector_mcp/server.py:37`, and the launcher chain (`bin/_resolve_python.sh:53-58`, `bin/claude-hook-pgvector-mcp.cmd:43-47`) falls back to bare system `python3`/`python` when no conda env/venv exists — so a psycopg-less interpreter silently reports pgvector verification as "FAILED". This is exactly the failure mode 4e67dc2 fixed in `install.py` (`tests/test_install_pgvector_verify.py:11`).
- `claude_hooks/providers/pgvector.py:328` — `import psycopg` in `_ensure_ready()`; re-raised as `ImportError("install psycopg: pip install psycopg[binary]")` at `claude_hooks/providers/pgvector.py:329-330`. **Yes, exercisable**: `claude_hooks/pgvector_mcp/server.py:271-280` dispatches `pgvector-find`/`pgvector-store` to `recall`/`store`, both gated by `_ensure_ready`. The production systemd unit (`systemd/claude-hooks-pgvector-mcp.service:16`) uses the same resolver chain, so a host without psycopg sees every MCP request crash with ImportError.
- `scripts/migrate_to_pgvector.py:624` — bare `import psycopg` in `main()`, no guard at all. **Yes, unconditionally exercisable**: the runbook invokes it as bare `python scripts/migrate_to_pgvector.py` (`docs/pgvector-runbook.md:50`), no conda pin — `ModuleNotFoundError` under any psycopg-less interpreter.
- `scripts/bench_recall.py:107` — bare `import psycopg` inside `PgvectorBenchProvider.__init__` (`scripts/bench_recall.py:105-108`). **Conditionally exercisable**: only when the pgvector bench provider is selected; runbook also uses bare `python` (`docs/pgvector-runbook.md:51`). Crashes with `ModuleNotFoundError`, no fallback.

**Test-file sites — reachable but low-severity by design:**

- `tests/test_pgvector_integration.py:56` — `import psycopg` in `_skip_if_no_deps()`; raises `unittest.SkipTest` at `tests/test_pgvector_integration.py:58`. Exercisable from any interpreter, but degrades to a clean skip, not a crash. (`tests/conftest.py:33-54` only warns when pytest runs outside the conda env; nothing is enforced.)
- `tests/test_pgvector_integration.py:285` — `import psycopg` in `tearDownClass()`, wrapped in `except Exception: pass` at `tests/test_pgvector_integration.py:293-294`. Exercisable but fully swallowed — cleanup silently skipped; no failure mode.

**Protected (contrast, per the 4e67dc2 pattern):** `install.py:2127` (fallback at `install.py:2140-2148`), `install.py:2524` (at `install.py:2540-2559`), and `install.py:2647` (at `install.py:2657-2676`). `install.py:2092` is not a missing-fallback site — it's an import inside the `_PGVECTOR_VERIFY_SCRIPT` string payload, executed in the conda env's child process by design (as are `install.py:2546` and `install.py:2666`). `claude_hooks/providers/__init__.py:33` and `claude_hooks/pgvector_mcp/server.py:37` are registry/import-chain references, not import sites.

One nuance for remediation: the resolver shims *prefer* the conda env (`bin/_resolve_python.sh:35-44`), and the runbook assumes psycopg lives there (`docs/pgvector-runbook.md:733-737`). The non-conda path materializes only when (a) no env exists, or (b) an env exists but lacks psycopg — the resolver picks the interpreter by existence, not by package presence. Line numbers match the `docs/benchmarks/EVALUATION.md:85-90` ground truth with zero drift.
