# Consultation transcript — csl-2026-05-09-0754-e412

_Question_: Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python.


## Planner

1. `git show 4e67dc2 --stat` to get changed files and `git show 4e67dc2` to extract the exact fallback pattern (likely a try/except around `import psycopg` with a subprocess call to conda-env Python).
2. Recursively grep all `.py` files under the claude-hooks root for lines matching `^import psycopg` or `^from psycopg`; also check `psycopg2` if relevant. Collect file:line.
3. For each candidate, examine from that line upward for a `try:` block that catches `ImportError` and contains a subprocess invocation (e.g., `subprocess.run([...'conda'...])`) — mark missing if absent.
4. For each missing site, determine if the import is at module top-level (always executed on import) or inside a function/method. If inside a function, trace callers upward (grep for function name usage) to see if any call path reaches it from a non-conda environment (e.g., is it guarded by `if 'conda' in sys.executable`?).
5. If the import is inside a conditional that checks for conda presence (e.g., `if 'conda' in sys.prefix`), consider it not exercisable from non-conda-env Python; otherwise assume exercisable unless proven otherwise by call-chain analysis.
6. For each missing site, output `file:line -- exercisable: yes/no/reason`.

---

## Researcher (round 1)

Now let me verify whether `_ensure_ready()` and `verify()` are actually called from production paths.

---

## Researcher (round 1)

## Report: psycopg import sites lacking the `4e67dc2` conda-env subprocess fallback

The `4e67dc2` pattern (fast-path `try: import psycopg` → `except ImportError:` → `subprocess.run([conda_py, "-c", ...])`) exists only in three functions in `install.py`:
- `install.py:2115` · `_verify_pgvector_dsn`
- `install.py:~2506` · `_pgvector_tables_present`
- `install.py:~2620` · `_init_pgvector_schema`

The helper `find_conda_env_python()` (`install.py:101`) is not imported or duplicated anywhere outside `install.py` — confirmed by grep (`claude_hooks/` has zero matches). Every site below lacks any conda-env subprocess fallback.

### Sites that lack the fallback

| # | File:Line | Context | Try/Except? | Conda fallback? | Exercisable from non-conda Python? |
|---|-----------|---------|-------------|-----------------|------------------------------------|
| 1 | `claude_hooks/providers/pgvector.py:123` | `PgvectorProvider.verify()` classmethod | Yes — catches `ImportError`, returns `False` | **No** | **Yes** — called from `detect.py` during auto-detection and from `install.py:2132` fast path; any Python can invoke it, and missing psycopg yields `False` silently |
| 2 | `claude_hooks/providers/pgvector.py:328` | `PgvectorProvider._ensure_ready()` instance method | Yes — catches `ImportError`, re-raises as `ImportError("install psycopg: ...")` | **No** | **Yes** — called from `store()`/`recall()` via the hook dispatcher (`claude_hooks/dispatcher.py`); dispatcher catches and exits 0, but never attempts conda |
| 3 | `tests/test_pgvector_integration.py:56` | `_skip_if_no_deps()` | Yes — catches `ImportError`, raises `unittest.SkipTest` | **No** | **Yes (pytest)** — any `pytest` collection from any Python triggers this |
| 4 | `tests/test_pgvector_integration.py:285` | `tearDownClass()` | Yes — blanket `except Exception: pass` | **No** | **Yes (pytest)** — swallows failure but no fallback |
| 5 | `scripts/migrate_to_pgvector.py:624` | `main()` | **No** — bare `import psycopg` | **No** | **Yes** — crashes with `ModuleNotFoundError` when run from non-conda Python |
| 6 | `scripts/bench_recall.py:107` | `PgvectorBenchProvider.__init__()` | **No** — bare `import psycopg` | **No** | **Yes (conditional)** — crashes with `ModuleNotFoundError` when pgvector bench path is hit from non-conda Python |

### Call-path analysis (step 2 of the plan)

For each function-scoped site, I traced callers upward:

- **#1** (`pgvector.py:123`): `PgvectorProvider.verify()` is a `@classmethod` — no instance gate. Called from `install.py:2132` (fast path, inside `_verify_pgvector_dsn`) and from `claude_hooks/detect.py` during MCP server probe. No `if 'conda' in sys.executable` guard anywhere in the call chain.
- **#2** (`pgvector.py:328`): `_ensure_ready()` is called by `PgvectorProvider.store()` and `PgvectorProvider.recall()` (both methods). These are called from `claude_hooks/dispatcher.py` → `claude_hooks/hooks/` handlers. The dispatcher runs inside whatever Python the `bin/claude-hook` shim resolves — which is *preferentially* the conda env (`$HOME/anaconda3/envs/claude-hooks/bin/python` per CLAUDE.md) but falls back to system `python3`. On the fallback path, neither the dispatcher nor `_ensure_ready()` attempts a subprocess to the conda env's Python. No conda guard.
- **#5** (`migrate_to_pgvector.py:624`): `import psycopg` inside `main()`. Script is invoked directly by user. No shim, no conda preference, no guard.
- **#6** (`bench_recall.py:107`): Same as #5 — direct user invocation, no guard.

### Bottom line

All 6 sites are exercisable from a non-conda-env Python. Sites #1-#4 at least `try/except` the `ImportError` so they degrade gracefully (return `False` / skip test / re-raise with message), but **none** attempt a subprocess fallback to the conda env's Python like the three `install.py` functions do. Sites #5-#6 (`scripts/migrate_to_pgvector.py:624`, `scripts/bench_recall.py:107`) have zero error handling — bare imports that crash with `ModuleNotFoundError` when psycopg is absent from the calling interpreter.

---

## Researcher (round 1)

## Psycopg import sites lacking the `4e67dc2` conda-env subprocess fallback

Reference pattern (present in `install.py`): fast-path `try: import psycopg` → on `ImportError` → `subprocess.run([find_conda_env_python(), "-c", script, dsn])` with psycopg expected in the conda env. Three `install.py` functions have it: `_verify_pgvector_dsn` (line 2127), `_pgvector_tables_present` (line 2524), `_pgvector_init_schema` (line 2647). None of the six sites below has this fallback.

---

- **`claude_hooks/providers/pgvector.py:123`** — `import psycopg` inside `PgvectorProvider.verify()`. Wrapped in `try/except ImportError` that returns `False`. **Exercisable: yes** (no conda check). The `verify()` classmethod is called from `install.py:_verify_pgvector_dsn` (line 2132) and from the provider discovery path in `detect.py`. When run from non-conda-env Python, the import fails, `verify()` returns `False` — safe, but silently skips a provider that might actually work via the conda env. The caller (`_verify_pgvector_dsn`) *does* have the conda fallback on its own code path, but only after `verify()` returns `False`; the `verify()` method itself has no subprocess escape hatch.

- **`claude_hooks/providers/pgvector.py:328`** — `import psycopg` inside `PgvectorProvider._ensure_ready()`. Wrapped in `try/except ImportError` that re-raises as `ImportError("install psycopg: pip install psycopg[binary]")`. **Exercisable: yes** (no conda check). This is called by `recall()`, `store()`, and every other public method of `PgvectorProvider`. When the provider is loaded via `bin/claude-hook` (which prefers the conda env's Python per `CLAUDE.md:272`), the import usually succeeds. But if `pgvector` is enabled in `config/claude-hooks.json` and the hook shim falls through to system Python (conda env absent), `_ensure_ready()` raises immediately — no attempt to re-spawn under the conda env's python. Call chain: `hook dispatcher` (line 134 mentions pgvector) → `get_provider` → `PgvectorProvider.__init__` → `_ensure_ready` (line 320).

- **`scripts/migrate_to_pgvector.py:624`** — bare `import psycopg` inside `main()`. **No guard at all**, no `try/except`, no conda check. **Exercisable: yes**. When invoked from system Python without `psycopg[binary]` installed, crashes with `ModuleNotFoundError`. The `pgvector-runbook.md` documents running a conda-env path at line 734 (`/root/anaconda3/envs/claude-hooks/bin/python -c "import psycopg; print(psycopg.__version__)"`), but the script itself has no subprocess fallback.

- **`scripts/bench_recall.py:107`** — bare `import psycopg` inside `PgvectorBenchProvider.__init__()`. **No guard**, no conda check. **Exercisable: yes** (conditional on the bench code path being exercised). When the benchmark script is run from non-conda-env Python and hits the pgvector provider, crashes with `ModuleNotFoundError`.

- **`tests/test_pgvector_integration.py:56`** — `import psycopg` inside `_skip_if_no_deps()`. Wrapped in `try/except ImportError` that raises `unittest.SkipTest`. **Exercisable: yes** but benign — `ImportError` produces a graceful test skip, not a crash. No conda fallback is appropriate here (tests should be straightforward about their deps).

- **`tests/test_pgvector_integration.py:285`** — `import psycopg` inside `tearDownClass()`. Wrapped in `try/except Exception: pass`. **Exercisable: yes** but harmless — any failure including `ImportError` is silently swallowed.

**Summary:** 4 production-relevant sites lack the conda-env subprocess fallback — the two in `claude_hooks/providers/pgvector.py` (lines 123, 328) and both scripts (`scripts/migrate_to_pgvector.py:624`, `scripts/bench_recall.py:107`). The two test sites (lines 56, 285) are intentionally bare and benign. The `install.py` triple (lines 2127, 2524, 2647) already has the pattern and is NOT a finding. No psycopg imports exist in `claude_hooks/pgvector_mcp/` (the MCP server imports `PgvectorProvider` transitively via the class, not `psycopg` directly).

---

## Synthesizer

4 production-relevant import sites lack the conda‑env subprocess fallback introduced in 4e67dc2; all are exercisable from a non‑conda‑env Python. Two test‑only sites also lack the fallback but are benign (graceful skip or no‑op).

- `claude_hooks/providers/pgvector.py:123` – `import psycopg` inside `PgvectorProvider.verify()`; wrapped in `try/except ImportError` that returns `False`. Exercisable: yes. Called from `install.py:2132` (fast‑path `_verify_pgvector_dsn`) and from `detect.py`; no conda guard in call chain.
- `claude_hooks/providers/pgvector.py:328` – `import psycopg` inside `PgvectorProvider._ensure_ready()`; wrapped in `try/except ImportError` that re‑raises with a pip message. Exercisable: yes. Called by `store()`/`recall()` (via dispatcher); no conda fallback, so non‑conda Python immediately crashes with `ImportError`.
- `scripts/migrate_to_pgvector.py:624` – bare `import psycopg` inside `main()`; no error handling. Exercisable: yes. Crashes with `ModuleNotFoundError` when run from system Python.
- `scripts/bench_recall.py:107` – bare `import psycopg` inside `PgvectorBenchProvider.__init__()`; no error handling. Exercisable: yes (conditional on bench path). Crashes with `ModuleNotFoundError` when hit from non‑conda Python.
- `tests/test_pgvector_integration.py:56` – `import psycopg` inside `_skip_if_no_deps()`; catches `ImportError` and raises `unittest.SkipTest`. Exercisable: yes but benign – test skipped gracefully.
- `tests/test_pgvector_integration.py:285` – `import psycopg` inside `tearDownClass()`; blanket `except Exception: pass`. Exercisable: yes but harmless – any failure silently swallowed.

---
