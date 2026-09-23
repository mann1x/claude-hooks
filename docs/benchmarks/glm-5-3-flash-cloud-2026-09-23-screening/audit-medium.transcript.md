# Consultation transcript — csl-2026-09-23-1803-9e3b

_Question_: Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python.


## Planner

1. Run `git show 4e67dc2 --stat` and `git show 4e67dc2` to extract the exact fallback pattern (subprocess/conda-env wrapper code shape) and the full list of files it touched.
2. Grep the claude-hooks tree for all psycopg import sites — `rg -n "import psycopg|from psycopg|psycopg2?" --type py` plus `__import__`/`importlib` dynamic forms — and diff that set against the files patched in 4e67dc2.
3. For each unpatched site, record file:line, import style (top-level vs. function-local vs. try/except-guarded), and whether surrounding code already tolerates ImportError.
4. Identify which interpreter runs each site: check hook registration (settings.json / hook command strings, shebangs, `subprocess.run`/`Popen` call sites) to see if the invoking command is a bare `python` or a conda-env-resolved interpreter.
5. Trace call chains from each entry point (hook scripts' `main`/CLI entry) to each unpatched import to determine exercisability: top-level import in a script actually spawned by a hook = exercisable; dead code or guarded-lazy import behind a conda-only path = not.
6. Verify the fallback mechanism itself in the patched reference files — what it actually does (e.g., re-execs via `conda run -n <env> python` or spawns subprocess with env's site-packages) — so "lacks fallback" is judged against the real pattern, not an assumption.
7. Confirm exercisability empirically where cheap: for the 2-3 highest-risk sites, run the importing module with a plain `python -c "import <module>"` from outside any conda env and record whether it fails or survives.

---

## Researcher (round 1)

**Report — psycopg import-site audit vs. the 4e67dc2 conda-env subprocess fallback**

**Caveat on the commit itself:** I have no shell/git tool in this environment, so `git show 4e67dc2` was not executable. The fallback pattern is instead grounded directly in code + its test suite: `install.py:2115-2161` (`_verify_pgvector_dsn`), `install.py:2507-2569` (`_pgvector_tables_present`), `install.py:2619-2680` (`_init_pgvector_schema`) all implement the identical pattern — fast path `try: import psycopg` → on `ImportError`, `find_conda_env_python()` (`install.py:101`) → `subprocess.run([conda_py, "-c", script…])`. `tests/test_install_pgvector_verify.py:1-14` documents this as the fix for the misleading "FAILED (psycopg not installed)" when install.py runs under system py3. Note also `grep 4e67dc2` over the tree only hits benchmark docs (`docs/benchmarks/EVALUATION.md:95`), not source comments — so "which sites the commit touched" is inferred from which sites *have* the pattern, which is the operationally correct set.

**Pattern (verbatim shape, `install.py:2138-2149`):** `conda_py = find_conda_env_python()`; if missing → explicit failure with path; else `subprocess.run([str(conda_py), "-c", script, dsn], capture_output=True, text=True, timeout=15)` and parse last stdout line as JSON.

**Unpatched sites (the audit answer):**

- `claude_hooks/providers/pgvector.py:123` — inside `PgvectorProvider.verify()`, function-local `import psycopg`, `except ImportError: log.warning("psycopg not installed — cannot verify pgvector"); return False` (`pgvector.py:124-126`). **No conda fallback.** Exercisable from non-conda Python: **yes** — it's the same method install.py's fast path calls (`install.py:2132`), and detect/install flows invoke it directly; under system python it degrades to `False` + warning rather than crashing (graceful, but a false negative — exactly the symptom 4e67dc2 fixed at the installer layer, leaving the provider method itself unfixed). Under the normal hook runtime it's fine: `bin/claude-hook:17` resolves the `claude-hooks` conda env first (`bin/_resolve_python.sh:12,35-44`).
- `claude_hooks/providers/pgvector.py:328-330` — `_ensure_ready()`, function-local import; on ImportError **raises** `ImportError("install psycopg: pip install psycopg[binary]")` (`pgvector.py:330`). **No conda fallback.** Exercisable: **yes** — this is the recall/store hot path for the pgvector provider (`_ensure_ready` gates `recall`/`store`); if a user runs hooks with a non-conda interpreter (no `.claude-hooks-disable`, pgvector enabled in `config/claude-hooks.json`), recall fails with a raise rather than a graceful skip. Mitigated in practice by `bin/claude-hook` conda resolution, but not by the fallback pattern.
- `scripts/migrate_to_pgvector.py:624-626` — bare `import psycopg` + `psycopg.connect(cfg.dsn)` at the top of `main()`, **unguarded** (no try/except, no subprocess fallback). Exercisable: **yes** — it's a standalone CLI; running `python3 scripts/migrate_to_pgvector.py` under system python crashes with a raw ImportError. It's only ever safe under the conda env (or any env with `requirements-pgvector.txt` installed). Notably `install.py:2639` imports `schema_sql_for_model` from this module — safe only because the import at `:624` is function-local inside `main()`, so module import doesn't trigger it.
- `scripts/bench_recall.py:107-108` — function-local `import psycopg` / `psycopg.connect(dsn)` in `PgvectorBenchProvider.__init__`, **unguarded**. Exercisable: **yes** — bench script invoked by hand; non-conda python → immediate ImportError. No fallback, though for a bench tool this is arguably acceptable-by-design.
- `tests/test_pgvector_integration.py:56-58, 285` — imports are `try/except ImportError → unittest.SkipTest` (`:58`). **Not a gap** — self-skipping, correct pattern for tests.

**Patched / not-gap sites (for the diff against 4e67dc2's scope):**

- `install.py:2127` (`_verify_pgvector_dsn`), `install.py:2524` (`_pgvector_tables_present`), `install.py:2647` (`_init_pgvector_schema`) — all three are the fast-path halves of functions that carry the full conda-subprocess fallback (`install.py:2140-2149`, `2540-2559`, `2657-2676`). Covered.
- `install.py:2092` and `install.py:2546, 2666` — these live inside inline script strings executed by the conda subprocess itself (`_PGVECTOR_VERIFY_SCRIPT` at `install.py:2089-2112` has its own try/except JSON-status protocol). Not import sites in the calling interpreter.
- `install.py:2698-2700`, `2782-2783`, `2830-2838` — already resolve the conda interpreter (`find_conda_env_python`) before invoking anything, i.e. the fallback-by-construction variant.

**Interpreter-coverage summary (plan step 4):** every *hook-runtime* entry point resolves conda first — `bin/claude-hook` (`bin/_resolve_python.sh:35-44`, probe order: venv → conda env `claude-hooks` → system), `bin/claude-hook-pgvector-mcp` and its `.cmd` twin (`bin/claude-hook-pgvector-mcp.cmd:28`), daemon/proxy/dashboard shims likewise. So `pgvector.py:123/328` are normally reached under the conda interpreter where psycopg exists; the exposure is (a) manual/diagnostic invocation under system python, and (b) the two scripts (`migrate_to_pgvector.py:624`, `bench_recall.py:107`) which have no shim-based interpreter resolution at all and are the clearest remaining gaps if you want the 4e67dc2 pattern applied uniformly.

**Severity ranking for the synthesizer:** 1) `scripts/migrate_to_pgvector.py:624` (unguarded, operator-facing, installer depends on this module's sibling function); 2) `pgvector.py:328` (raises into the hook path, only mitigated by shim resolution); 3) `pgvector.py:123` (graceful False, false-negative risk); 4) `bench_recall.py:107` (benign by design).

---

## Researcher (round 1)

## Research report: psycopg import sites vs. the conda-env subprocess fallback

**Reference fallback pattern (what 4e67dc2-style patching looks like)** — all in `install.py` only:
- `install.py:2115-2161` — `_verify_pgvector_dsn`: fast-path `import psycopg` (2127); on `ImportError` falls back to `find_conda_env_python()` + `subprocess.run([conda_py, "-c", _PGVECTOR_VERIFY_SCRIPT, dsn], timeout=15)` (2140-2149). Probe script at `install.py:2089-2112`.
- `install.py:2507-2569` — `_pgvector_tables_present`: same two-phase pattern, inline probe script, subprocess fallback at 2539-2560.
- `install.py:2619-2681` — `_init_pgvector_schema`: same pattern; DDL piped via stdin to the conda-env python (2656-2676), raises with an actionable message if no conda env found (2658-2663).

**Unpatched psycopg import sites** (grep over repo: the only `import psycopg` sites outside `install.py` are these):

- `claude_hooks/providers/pgvector.py:123-126` — `PgvectorProvider.verify()`: lazy import, returns `False` with a warning on ImportError. **Lacks fallback**, but from a non-conda python the only consequence is a failed verify (no crash). Exercisable via the installer fast path (`install.py:2132`) — where the subprocess fallback in `_verify_pgvector_dsn` never reaches it anyway, because the fallback path replaces the whole verify call rather than re-executing `verify()`.
- `claude_hooks/providers/pgvector.py:328-334` — `_ensure_ready()`: lazy import, raises `ImportError("install psycopg: pip install psycopg[binary]")`. This raise **is caught** at `claude_hooks/providers/pgvector.py:149-150` in `recall()` (`except (ImportError, EmbedderError)` → warning, empty result). So hook-spawned calls degrade gracefully; no subprocess fallback, but not fatal. Provider is an opt-in scaffold disabled by default per `CLAUDE.md` decision 5.
- `claude_hooks/pgvector_mcp/server.py:37` — **top-level** `from claude_hooks.providers.pgvector import PgvectorProvider` in the stdio MCP server. Module import survives a psycopg-less interpreter (the psycopg import inside the provider is lazy at `pgvector.py:328`), but every recall via this server on a non-conda python will hit the `_ensure_ready` ImportError path. Exercisable only if `pgvector-mcp` is spawned with a non-conda interpreter — its launcher (`bin/claude-hook-pgvector-mcp`, per CLAUDE.md) resolves python via `_resolve_python.sh`, which prefers the conda env; **I did not read the shim to verify** — evidence insufficient on shim behavior.
- `scripts/migrate_to_pgvector.py:624-626` — bare `import psycopg` + `psycopg.connect` directly in `main()`, **no try/except, no fallback**. Exercisable: running the script from a system python fails with a raw ImportError. Not hook-spawned (operator CLI), so planner's "exercisable from a hook" test = no.
- `scripts/bench_recall.py:107-108` — `import psycopg` inside `PgvectorBenchProvider.__init__`, no fallback. Exercised only with `--provider pgvector`; raw ImportError from a non-conda python. Benchmark-only, not hook-reachable.
- `tests/test_pgvector_integration.py:56-58` — import raises `unittest.SkipTest("psycopg not installed")` — intentional skip, not a gap.
- `install.py:2092-2096` — `_PGVECTOR_VERIFY_SCRIPT`'s `import psycopg` is only ever run under the conda python by the fallback itself; not a real gap.

**Key structural finding:** the conda-env subprocess fallback exists **exclusively in `install.py`** (3 sites). Nothing in `claude_hooks/` runtime code has it — by design the runtime handles the missing-psycopg case via graceful degradation (warnings/empty results) rather than re-exec, per `CONTRIBUTING.md:44` ("Optional dependencies (psycopg, sqlite-vec) must be lazy-imported inside methods, gated by `try/except ImportError`"). So "lacks fallback" at `pgvector.py:123` / `pgvector.py:328` is accurate but matches the stated project convention; the only sites where a fallback would change behavior are the two `scripts/` CLIs.

**Empirical check:** not performed — my toolset (read/grep/glob/list/survey only) cannot execute `python -c "import ..."`. All exercisability conclusions above are from static call-chain reads; the two riskiest sites (`scripts/migrate_to_pgvector.py:624`, `scripts/bench_recall.py:107`) are straightforward top-of-`main`/`__init__` imports, so the static read is high-confidence, but the planner's step-3 empirical confirmation remains open.

---

## Synthesizer

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

---
