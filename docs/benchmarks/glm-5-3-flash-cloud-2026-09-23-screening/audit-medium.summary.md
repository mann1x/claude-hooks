---
session_id: csl-2026-09-23-1803-9e3b
created: "2026-09-23T18:03:54"
question: "Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python."
models:
  planner: "glm-5.3-flash:cloud"
  researcher: "glm-5.3-flash:cloud"
  synthesizer: "glm-5.3-flash:cloud"
topology: council
effort: medium
duration_seconds: 94.23
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-09-23/tmp/claude-hooks-bench-bench-baseline-2026-05-07-9q75
---

The 4e67dc2-style conda-env subprocess fallback exists **only in `install.py`** (three functions); every psycopg import site in `claude_hooks/` and `scripts/` lacks it. The two clearest genuinely-exercisable-from-a-bare-`python` gaps are `scripts/migrate_to_pgvector.py:624-626 [no python at this line; line is in main]` and `scripts/bench_recall.py:107-108 [no python at this line; line is in __init__]`; the runtime sites in `pgvector.py` lack the fallback but degrade gracefully by design.

**Caveat first:** no lane could execute `git show 4e67dc2`, so "the pattern" is grounded in the code that demonstrably carries it — `_verify_pgvector_dsn` (`install.py:2115-2161`), `_pgvector_tables_present` (`install.py:2507-2569`), `_init_pgvector_schema` (`install.py:2619-2681`): fast-path `import psycopg`, on ImportError → `find_conda_env_python()` (`install.py:101`) → `subprocess.run([conda_py, "-c", script…], timeout=15)`. The empirical `python -c "import …"` checks from plan step 7 were also **not performed** (no execution tooling) — all exercisability verdicts below are static call-chain reads.

**Audit results (all psycopg import sites outside install.py):**

- `scripts/migrate_to_pgvector.py:624-626 [no python at this line; line is in main]` — bare `import psycopg` + `psycopg.connect` at top of `main()`, unguarded. **Exercisable: yes.** Operator CLI; running it under system python crashes with a raw ImportError. Highest-severity gap — and `install.py:2639` imports from this module (safe only because the psycopg import is function-local inside `main()`).
- `scripts/bench_recall.py:107-108 [no python at this line; line is in __init__]` — `import psycopg` in `PgvectorBenchProvider.__init__`, unguarded. **Exercisable: yes** (`--provider pgvector` from a non-conda python → immediate ImportError), but benchmark-only, arguably acceptable by design.
- `claude_hooks/providers/pgvector.py:123-126` — lazy import in `verify()`; on ImportError logs a warning and returns `False`. **Exercisable: yes** via the installer fast path (`install.py:2132` calls `verify()` directly) — consequence is a false-negative verify, not a crash. Note the fallback path in `_verify_pgvector_dsn` never reaches `verify()` anyway; it replaces the whole call.
- `claude_hooks/providers/pgvector.py:328-334 [no verify at this line; line is in _ensure_ready]` — lazy import in `_ensure_ready()`, raises `ImportError("install psycopg: pip install psycopg[binary]")`. **Exercisable: yes** on the recall/store hot path under a non-conda interpreter, but the raise is caught in `recall()` at `pgvector.py:149-150` (`except (ImportError, EmbedderError)` → warning, empty result) — degrades gracefully, per the project convention in `CONTRIBUTING.md:44` (optional deps must be lazy-imported, gated by try/except). Provider is opt-in and disabled by default.
- `claude_hooks/pgvector_mcp/server.py:37` — top-level `from claude_hooks.providers.pgvector import PgvectorProvider`. Module import survives a psycopg-less interpreter (the psycopg import is lazy), but any recall served by this MCP server under a non-conda python hits the `_ensure_ready` ImportError path. In practice the launcher shim resolves the conda env first (`bin/claude-hook:17`, `bin/_resolve_python.sh:12,35-44` probe order: venv → conda env `claude-hooks` → system), so this is only reachable via manual/shim-bypassing invocation.
- `tests/test_pgvector_integration.py:56-58` — try/except → `unittest.SkipTest("psycopg not installed")`. **Not a gap.**
- `install.py:2092` (and the analogous inline-script imports at 2546, 2666) — these run inside the conda subprocess itself via the JSON-status protocol (`install.py:2089-2112`). Not import sites in the calling interpreter. **Not gaps.**

**Recommendation:** apply the 4e67dc2 pattern (or at minimum an actionable-error guard) to `scripts/migrate_to_pgvector.py:624` first; `bench_recall.py:107` second. The `pgvector.py` sites match the documented lazy-import convention and don't need the subprocess fallback — but `verify()`'s silent `False` at `pgvector.py:123` is worth an explicit "psycopg missing in this interpreter" message to avoid false-negative verification reports.

CONFIDENCE: 0.78 — commit scope inferred from patterned code (no git access), no empirical import test run, and shim behavior verified only in the round-1 lane.
