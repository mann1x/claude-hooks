# Consultation transcript — csl-2026-10-27-1504-5b80

_Question_: Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python.


## Planner

1. Identify the exact implementation of the "conda-env subprocess fallback" in commit `4e67dc2` (via researcher's access to git history) to establish the required pattern for `psycopg` imports.
2. Analyze `claude_hooks/providers/pgvector.py:123` and `claude_hooks/providers/pgvector.py:328` to determine if they use the fallback or simply raise `ImportError`.
3. Audit `scripts/migrate_to_pgvector.py:624` and `scripts/bench_recall.py:107` for the presence of the fallback logic around their `import psycopg` calls.
4. Inspect `install.py` (specifically lines 2092, 2127, 2524, 2546, 2647, 2666) to see if `psycopg` imports are wrapped in the fallback or are intended only for installation-time checks.
5. Examine `tests/test_pgvector_integration.py` (lines 56, 285) to check for fallback usage, though these are likely excluded from "exercisable" results if they only run in CI.
6. For every file identified as lacking the fallback, evaluate if the code path is reachable from a non-conda Python environment (e.g., check if `claude_hooks/providers/pgvector.py` is instantiated during normal `claude-hook` execution without a conda environment).

---

## Researcher (round 1)

## Findings — psycopg import sites vs. conda-env subprocess fallback

**Caveat on step 1:** I have no git tool available in this environment, so I could not read commit `4e67dc2` directly. The fallback pattern is instead established from its current implementation and tests in the working tree: `find_conda_env_python()` at `install.py:101` (probes conda layouts + `conda env list --json`, cached per env — cache-bug note at `install.py:109-114`), with three subprocess-fallback call sites at `install.py:2140`, `install.py:2540`, `install.py:2657`, and regression tests in `tests/test_install_pgvector_verify.py:63-155` (`TestSubprocessFallback`). The pattern: try `import psycopg` in-process; on `ImportError`, shell out to the conda env's python with an embedded probe script.

**Sites WITHOUT the fallback:**

- `claude_hooks/providers/pgvector.py:123` — `PgvectorProvider.verify()` does bare `import psycopg`, catches `ImportError`, logs "psycopg not installed — cannot verify" and returns `False`. No conda-subprocess attempt. **Exercisable from non-conda python: yes** — `verify` is a classmethod invoked by installer detection and any caller with the provider loaded; returning False from a system-python run is exactly the "misleading FAILED" behavior the fallback was added to fix elsewhere.
- `claude_hooks/providers/pgvector.py:328` — `_ensure_conn` (connection setup) imports psycopg and re-raises `ImportError("install psycopg: pip install psycopg[binary]")` at `:330`. No fallback. **Exercisable: yes** — any `recall()`/`store()` on an enabled pgvector provider hits this; consumers include `claude_hooks/pgvector_mcp/server.py:37,344,492` (the MCP server wraps `PgvectorProvider`) and the dispatcher via `build_providers`. Mitigation in practice: the `bin/claude-hook-pgvector-mcp` shim prefers the conda env python, and the `pgvector-mcp` pip entry point (`pyproject.toml:29-31`) is normally installed into the conda env — but a pip install into a non-conda interpreter would hit the raise.
- `scripts/migrate_to_pgvector.py:624` — bare `import psycopg` inside the run path (`psycopg.connect` at `:626`), no try/except, no fallback. **Exercisable: yes** — operator-run script; invoking it with system python yields an uncaught `ImportError` traceback.
- `scripts/bench_recall.py:107` — bare `import psycopg` in `PgvectorBenchProvider.__init__` (`psycopg.connect` at `:108`), no fallback. **Exercisable: yes** — bench script inserts repo root into `sys.path` (`scripts/bench_recall.py:41`) so it runs from any checkout, but psycopg must exist in the invoking interpreter.
- `tests/test_pgvector_integration.py:285` — teardown does bare `import psycopg` with no guard (the setup guard at `:56` raises `unittest.SkipTest`). CI-only; not production-exercisable, but the teardown itself would crash rather than skip if setup somehow passed.

**Sites WITH the fallback (for contrast):** `install.py:2125-2143` (`_verify_pgvector_dsn`: fast path `:2127`, fallback `:2140`), `install.py:2522-2542` (`_pgvector_tables_present`: fast path `:2524`, fallback `:2540`), `install.py:2645-2663` (schema init: fast path `:2647`, fallback `:2657`, embedded-DDL-via-stdin script at `:2664-2670`). The `import psycopg` at `install.py:2092` and `:2546`/`:2666` are embedded probe-script *strings* executed via the conda python subprocess — they are part of the fallback machinery, not gaps.

**Evidence gaps:** commit `4e67dc2` contents unreadable with available tools (no git access); behavior inferred from current code + `tests/test_install_pgvector_verify.py` docstring (`:11-13`), which describes the fallback as fixing the "FAILED (psycopg not installed)" message when `install.py` runs under system python — consistent with the three fallback sites above being the commit's scope.

---

## Researcher (round 1)

## Findings — psycopg import sites vs conda-env subprocess fallback (4e67dc2)

**Reference pattern (has fallback)** — `install.py` is the only place the fallback exists, in three functions: `_verify_pgvector_dsn` (fast path `install.py:2127`, fallback `install.py:2138-2149`), `_pgvector_tables_present` (fast path `install.py:2524`, fallback `install.py:2539-2558`), and `_pgvector_init_schema` (fast path `install.py:2647`, fallback `install.py:2656-2674`, DDL piped via stdin). All three shell out to `find_conda_env_python()` (`install.py:101`). The bare `import psycopg` at `install.py:2092` and `install.py:2546`/`install.py:2666` are inside embedded subprocess probe scripts (`_PGVECTOR_VERIFY_SCRIPT` string, `sys.argv`-driven) — they run *in the conda env by design*, not fallback candidates.

**Lacking the fallback:**

- `claude_hooks/providers/pgvector.py:123` — `verify()`: no fallback, logs warning and returns `False` on ImportError. Exercisable from non-conda Python: yes, when install.py's fast path succeeds it's only because psycopg is present, so in practice `verify` is reached via `install.py:2132` with psycopg available; but a direct caller (e.g. daemon or tests) on system python gets a misleading `False`.
- `claude_hooks/providers/pgvector.py:328` — `_connect()`: no fallback, raises `ImportError("install psycopg: pip install psycopg[binary]")`. Exercisable from non-conda Python: **yes, the hot path**. `claude_hooks/dispatcher.py:134` loads DB-backed providers via `dsn` config; if `pgvector` is enabled in `config/claude-hooks.json`, every `UserPromptSubmit`/`Stop` hook in a system-python execution (conda env missing → `bin/claude-hook` shim fallback to `python3`) hits this raise. Hook survives (dispatcher catches, exits 0 per CLAUDE.md failure-mode policy) but recall silently no-ops.
- `scripts/migrate_to_pgvector.py:624` — bare import in `main()`, no fallback. Exercisable: yes — operator tooling, invoked as `python scripts/migrate_to_pgvector.py` per README, exactly the non-conda invocation the fallback exists for. Crashes with raw ImportError instead of delegating to conda env.
- `scripts/bench_recall.py:107` — bare import in `PgvectorBenchProvider.__init__`, no fallback. Exercisable: yes, operator bench script; same non-conda failure mode.
- `tests/test_pgvector_integration.py:56` and `tests/test_pgvector_integration.py:285` — no fallback, but `:56` is `_skip_if_no_deps()` which converts ImportError to `unittest.SkipTest`; `:285` is `tearDownClass` cleanup, only reached if setup already imported psycopg. Not exercisable-failure sites; agree with planner's exclusion.

**Not audit hits:** `claude_hooks/pgvector_mcp/` has zero direct `import psycopg` (grep, 0 matches) — it reaches psycopg only through the provider, so its exposure is inherited from `pgvector.py:328`. `CONTRIBUTING.md:44` codifies the lazy-import convention but says nothing about the conda fallback, so there's no documented requirement the scripts violate beyond the 4e67dc2 precedent.

**Note:** commit 4e67dc2 itself not verifiable here (no git log tool available); the fallback's presence/absence is assessed against current code only.

---

## Synthesizer

* `claude_hooks/providers/pgvector.py:123`: Yes. `PgvectorProvider.verify()` catches `ImportError` and returns `False` without attempting conda fallback, leading to misleading "FAILED" status during detection.
* `claude_hooks/providers/pgvector.py:328`: Yes. `_ensure_conn` raises `ImportError` when `psycopg` is missing. This is a hot path for hooks executed via the `bin/claude-hook` shim when the conda environment is missing and it falls back to system Python.
* `scripts/migrate_to_pgvector.py:624`: Yes. Bare import in `main()` causes an uncaught `ImportError` traceback when the migration script is invoked using system Python.
* `scripts/bench_recall.py:107`: Yes. Bare import in `PgvectorBenchProvider.__init__` causes an uncaught `ImportError` traceback when the benchmarking script is invoked using system Python.

---
