# Consultation transcript — csl-2026-10-27-1543-77f2

_Question_: Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python.


## Planner

1. Examine `claude_hooks/providers/pgvector.py` at lines 123 and 328 to determine if `psycopg` imports are wrapped in a conda-env subprocess fallback pattern similar to `install.py:2115-2160`.
2. Examine `scripts/migrate_to_pgvector.py` at line 624 to determine if the `psycopg` import implements the conda-env subprocess fallback.
3. Examine `scripts/bench_recall.py` at line 107 to determine if the `psycopg` import implements the conda-env subprocess fallback.
4. For each site lacking the fallback, trace the call graph (using `grep` or `read_file`) to verify if the code is reachable when the current Python interpreter lacks `psycopg` but a conda environment exists.
5. List each `file:line` missing the fallback and label it as "exercisable" or "not exercisable" based on the reachability analysis.

---

## Researcher (round 1)

## Findings

**Reference pattern (4e67dc2)** — `install.py:2115-2161` `_verify_pgvector_dsn()`: fast path `import psycopg` at `install.py:2127`, on `ImportError` shells out to `find_conda_env_python()` via `subprocess.run([conda_py, "-c", _PGVECTOR_VERIFY_SCRIPT, dsn])` at `install.py:2140-2149`, parsing last-line JSON. A second mirrored instance is at `install.py:2632-2668` (docstring says "lacks psycopg, mirroring `_verify_pgvector_dsn`"). I could not verify the commit hash itself (no git tooling available), but the pattern is present exactly as described.

**Sites lacking the fallback — all three plan targets confirmed bare:**

1. **`claude_hooks/providers/pgvector.py:123`** (`verify()` classmethod) — `try: import psycopg / except ImportError: log.warning(...); return False`. No subprocess fallback. *Exercisable but degraded*: reachable from a non-conda interpreter (it's the verify entry the installer's fast path calls at `install.py:2132`, and `PgvectorProvider.verify` is also the provider contract method), but it permanently returns `False` with "psycopg not installed — cannot verify pgvector" even when the conda env has psycopg. Graceful, not crashing.

2. **`claude_hooks/providers/pgvector.py:328`** (`_ensure_ready()`, line 320) — `try: import psycopg / except ImportError as e: raise ImportError("install psycopg: pip install psycopg[binary]")` at `:329-330`. No subprocess fallback. *Exercisable but degraded*: `recall()` catches the ImportError at `pgvector.py:149` and logs "pgvector unavailable" — the hook still exits 0, but recall/store silently no-op from a non-conda interpreter even when the conda env could serve. (In practice hooks run through the conda-preferring shim `bin/claude-hook`, so real hook traffic usually has psycopg.)

3. **`scripts/migrate_to_pgvector.py:624`** (`main()`, line 610) — bare `import psycopg`, no try/except, no conda/subprocess anywhere in the file (grep for `conda|subprocess|ImportError` returned zero matches in that file). *Not exercisable* from a non-conda Python: uncaught `ImportError` traceback at startup.

4. **`scripts/bench_recall.py:107`** (`PgvectorBenchProvider.__init__`, line 105) — bare `import psycopg` at `:107`, no fallback; called unconditionally from `main()` at `bench_recall.py:239` for every selected model. *Not exercisable* from a non-conda Python: `ImportError` traceback. Note the module-level imports at `bench_recall.py:43-48` (from `scripts.migrate_to_pgvector`) do **not** pull psycopg at import time (migrate's import is function-local), so the script fails only at provider construction, after arg parsing.

**Summary table**

| Site | Fallback? | Non-conda behavior |
|---|---|---|
| `claude_hooks/providers/pgvector.py:123` | No | returns `False` + warning (degraded, no crash) |
| `claude_hooks/providers/pgvector.py:328` | No | raises ImportError; recall/store no-op gracefully upstream (`:149`) |
| `scripts/migrate_to_pgvector.py:624` | No | hard ImportError traceback |
| `scripts/bench_recall.py:107` | No | hard ImportError traceback at `:239` |

Only `install.py` (two sites) implements the conda-env subprocess fallback; the four runtime/script sites do not.

---

## Researcher (round 1)

## Findings — psycopg import sites vs. the 4e67dc2 conda-env subprocess fallback

**Reference pattern (the fallback itself, all in `install.py`):** `_verify_pgvector_dsn()` at `install.py:2115` — fast path `import psycopg` at `install.py:2127`, on `ImportError` shells out to `find_conda_env_python()` via `subprocess.run` with the embedded `_PGVECTOR_VERIFY_SCRIPT` (`install.py:2089-2112`, subprocess call at `install.py:2146-2147`). The same pattern is replicated in `_pgvector_tables_present()` (`install.py:2507`, fallback at `install.py:2539-2540`) and `_init_pgvector_schema()` (`install.py:2619`, fallback at `install.py:2656-2657`). These three are **covered** — no gaps here.

**Sites lacking the fallback (all four plan targets verified bare):**

1. **`claude_hooks/providers/pgvector.py:123`** — `PgvectorProvider.verify()` (classmethod, `:121`). `try: import psycopg / except ImportError: log.warning("psycopg not installed — cannot verify pgvector"); return False` (`:123-126`). No subprocess fallback. **Exercisable but degraded**: called directly from `install.py:2132` (only on the fast path where psycopg *is* importable, so the bare path isn't hit from there) and from the generic provider-verify loop at `install.py:3556` (`ok = cls.verify(candidate)`), which runs under whatever interpreter `install.py` was launched with — commonly system py3 without psycopg. Also called from `tests/test_pgvector_integration.py:186` (skipped w/o psycopg per `README.md:1037`). Result: silently returns `False` / "UNREACHABLE" instead of probing via the conda env — exactly the misleading-FAILED behavior 4e67dc2 fixed for `_verify_pgvector_dsn`.

2. **`claude_hooks/providers/pgvector.py:328`** — `_ensure_ready()` (`:320`). `except ImportError as e: raise ImportError("install psycopg: pip install psycopg[binary]")` (`:329-330`). No fallback. **Exercisable but graceful**: `recall()` catches `(ImportError, EmbedderError)` and returns `[]` (`:147-151`), and hooks are non-blocking by design, so a non-conda interpreter degrades to "no recall" rather than crashing. Reachable whenever the pgvector provider is enabled in `config/claude-hooks.json` and the hook runs under system python. Also reachable via `claude_hooks/pgvector_mcp/server.py:344` / `:492` (`build_providers` → `PgvectorProvider`), though that server normally launches through the pgvector-mcp shim which prefers the conda env (`bin/_resolve_python.sh:35-44`).

3. **`scripts/migrate_to_pgvector.py:624`** — `main()` (`:610`). Bare `import psycopg` at `:624`, `psycopg.connect(cfg.dsn)` at `:626`. No try/except, no conda/subprocess anywhere in the file (grep for conda/subprocess/ImportError in that file: zero hits). **Exercisable — hard failure**: invoked as `python scripts/migrate_to_pgvector.py` per the runbook (`docs/pgvector-runbook.md:50`, `:103`, `:489`) and `config/claude-hooks.example.json:80`; run under system py3 → immediate ImportError traceback. Highest-impact gap: this is the documented first-run migration path.

4. **`scripts/bench_recall.py:107`** — `PgvectorBenchProvider.__init__` (`:105`). Bare `import psycopg` at `:107`, `psycopg.connect(dsn)` at `:108`. No fallback. **Exercisable but niche**: the class is only constructed when the pgvector benchmark is selected in that script's main flow; running `scripts/bench_recall.py` with the pgvector lane enabled under non-conda python → ImportError traceback. Low severity — it's a dev benchmark, and `docs/pgvector-runbook.md:733-734` explicitly instructs checking psycopg inside the conda env first.

**Non-code import sites (informational):** `install.py:2092` and `install.py:2546`/`:2666` are psycopg imports inside embedded subprocess probe scripts — those run *in* the conda env python by design, so they're the fallback mechanism, not gaps. `tests/test_pgvector_integration.py:56`/`:285` self-skip without psycopg. Docs snippets at `docs/pgvector-runbook.md:499` are illustrative.

**Summary table**

| Site | Has conda fallback | Behavior without psycopg |
|---|---|---|
| `claude_hooks/providers/pgvector.py:123` | No | warning + `return False` (degraded verify) |
| `claude_hooks/providers/pgvector.py:328` | No | raises ImportError; recall/store no-op upstream (`:149`) |
| `scripts/migrate_to_pgvector.py:624` | No | hard ImportError — documented first-run path |
| `scripts/bench_recall.py:107` | No | hard ImportError (pgvector bench lane only) |

**Uncertainty:** I could not locate commit `4e67dc2` itself (no git tool access; CHANGELOG grep for the hash and "subprocess fallback" returned no matches), so "added in 4e67dc2" is taken from the plan; the fallback pattern's presence in `install.py` is directly verified regardless. The `bench_recall.py:239` construction-site line number comes from a prior session's checkpoint, not a fresh read of that range — the `:107` import site itself is freshly verified.

---

## Synthesizer

Four sites lack the conda-env subprocess fallback:

* `claude_hooks/providers/pgvector.py:123` — Exercisable; returns `False` and logs a warning, causing verification to fail even if a conda environment is available.
* `claude_hooks/providers/pgvector.py:328` — Exercisable; raises `ImportError` in `_ensure_ready()`, which is caught at `claude_hooks/providers/pgvector.py:149` resulting in a graceful no-op for recall/store.
* `scripts/migrate_to_pgvector.py:624` — Exercisable; causes a hard `ImportError` traceback upon execution.
* `scripts/bench_recall.py:107` — Exercisable; causes a hard `ImportError` traceback when the pgvector benchmark lane is constructed.

---
