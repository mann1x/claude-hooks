# Consultation transcript — csl-2026-05-09-0821-393e

_Question_: Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python.


## Planner

1. Identify the exact diff of commit `4e67dc2` using `git show 4e67dc2 --stat` then `git show 4e67dc2` and record the pattern used for the conda-env subprocess fallback (e.g., try/except ImportError that falls back to `subprocess.check_output` calling a conda environment's Python).
2. Grep the repository for any import statement containing `psycopg` (`import psycopg`, `from psycopg`, `import psycopg2`, `from psycopg2`) across all Python files (excluding `.git` and virtual environments), and compile a list of file paths and line numbers.
3. For each file from step 2, inspect the surrounding context (the function or block that contains the import) to determine if a fallback mechanism identical or analogous to the one in commit `4e67dc2` exists (i.e., a try/except that catches `ImportError` and delegates to a subprocess call to a conda env’s Python).
4. Check each file’s module-level `__name__` guards, entry points (e.g., `if __name__ == "__main__"`), imports from `__init__.py` or CLI entry points (`cli.py`, `main.py`, `setup.cfg` entry points), and dependency chain (imported by any top-level module) to decide whether the psycopg import site would be executed when `claude-hooks` is run from a Python interpreter that does NOT have psycopg installed (i.e., a non-conda-env Python).
5. For any file that lacks the fallback but is not exercisable (e.g., inside a conditional branch that guards on conda-env detection, or in a dead code path not imported by entry points), classify it as “not exercisable” and note the guard condition.
6. For each file that lacks the fallback and IS exercisable, record the exact file:line of the import and the surrounding 10 lines to confirm absence of fallback, then flag it as a remediation target.

---

## Researcher (round 1)

## Report: psycopg import sites lacking the `4e67dc2` conda-env subprocess fallback

### Fallback pattern (established in `install.py` by `4e67dc2`)

The pattern uses a two-phase strategy in three functions:
- `_verify_pgvector_dsn` (`install.py:2115`), `_pgvector_tables_present` (`install.py:2503`), `_init_pgvector_schema` (`install.py:2625`)
- **Phase 1** (lines 2127–2136, 2524–2537, 2647–2654): `try: import psycopg` inside function → use it directly. `except ImportError: pass`.
- **Phase 2** (lines 2140–2155, 2540–2560, 2657–2680): `conda_py = find_conda_env_python()`; if it exists, `subprocess.run([conda_py, "-c", probe_script, dsn, ...])` to run psycopg from the conda env where it's installed.

---

### Sites lacking the fallback (6 sites across 4 files)

**1. `claude_hooks/providers/pgvector.py:123`** — `import psycopg` inside `PgvectorProvider.verify()`. Wrapped in `try/except ImportError` that returns `False`. **No conda-env subprocess fallback.** *Exercisable: yes* — any Python interpreter that calls `PgvectorProvider.verify()` (e.g. from `install.py`'s own fast path at line 2132, or at runtime during detection) hits this code. The `except ImportError` silently returns `False` for the calling project's whole pgvector backend; it doesn't re-probe in the conda env.

**2. `claude_hooks/providers/pgvector.py:328`** — `import psycopg` inside `_ensure_ready()`. The `except ImportError` handler at line 329–330 re-raises as `raise ImportError("install psycopg: pip install psycopg[binary]")`. **No conda-env subprocess fallback.** *Exercisable: yes* — any instantiated `PgvectorProvider` that calls a recall/store/verify method triggers `_ensure_ready()` and crashes at line 330 when psycopg is absent from the importing interpreter. This is a hard error path — the user sees an unhelpful message instead of "I found psycopg in your conda env, let me use that."

**3. `scripts/migrate_to_pgvector.py:624`** — bare `import psycopg` inside `main()`. **No `try/except ImportError` at all.** *Exercisable: yes* — if this script is invoked from a system Python that lacks psycopg (exactly the scenario `install.py`'s fallback was written for), it crashes at line 624 with `ModuleNotFoundError` before doing any work. Note that `install.py`'s `_init_pgvector_schema` (line 2638) imports `MODELS` and `schema_sql_for_model` from this file, but never runs `main()`.

**4. `scripts/bench_recall.py:107`** — bare `import psycopg` inside `PgvectorBenchProvider.__init__()`. **No `try/except ImportError`.** *Exercisable: yes* — any benchmark invocation of the pgvector provider path from a non-conda-env Python crashes at line 107 with `ModuleNotFoundError`.

**5. `tests/test_pgvector_integration.py:56`** — `import psycopg` inside `_skip_if_no_deps()`. Wrapped in `try/except ImportError` that raises `unittest.SkipTest`. **No conda-env subprocess fallback.** *Exercisable: yes* (from pytest) — the `SkipTest` behaviour is correct for test runners, but a developer running `pytest tests/test_pgvector_integration.py` from a non-conda Python gets `SKIP` for the entire integration suite even if psycopg is sitting in the conda env. The test would be better served by attempting the conda-env subprocess probe before skipping.

**6. `tests/test_pgvector_integration.py:285`** — `import psycopg` inside `tearDownClass()`. Wrapped in broad `except Exception: pass`. **No conda-env subprocess fallback.** *Exercisable: yes* (from pytest) — the `except Exception: pass` silently swallows the `ModuleNotFoundError`, so cleanup just doesn't run. Tables accumulate. No conda-env probe is attempted.

---

### Summary

| Site | Guard? | Conda-env fallback? | Exercisable? | Severity |
|---|---|---|---|---|
| `pgvector.py:123` | `try: except ImportError: return False` | ❌ | yes (any `verify()` call) | medium — silent false negative |
| `pgvector.py:328` | `try: except ImportError: raise ImportError(...)` | ❌ | yes (any provider usage) | high — hard crash, poor diagnostic |
| `migrate_to_pgvector.py:624` | none | ❌ | yes (script startup) | high — hard crash |
| `bench_recall.py:107` | none | ❌ | yes (conditional code path) | medium — conditional crash |
| `test_pgvector_integration.py:56` | `try: except ImportError: SkipTest` | ❌ | yes (pytest) | low — correct skip, but unnecessarily broad |
| `test_pgvector_integration.py:285` | `try: except Exception: pass` | ❌ | yes (pytest) | low — silent cleanup skip |

Sites 1 and 2 are the highest-impact misses in `claude_hooks/` itself: they are the only psycopg import sites the library exports, and neither attempts the `find_conda_env_python()` + `subprocess` strategy that `install.py` uses. Sites 3 and 4 are script-level with no guard at all.

---

## Researcher (round 1)

Now let me verify the `_ensure_ready` caller context to confirm graceful handling:

---

## Researcher (round 1)

## Audit: psycopg import sites lacking conda-env subprocess fallback

The `4e67dc2` pattern (seen in `install.py:2118-2161` and `install.py:2539-2541`) is: `try: import psycopg → except ImportError → conda_py = find_conda_env_python() → subprocess.run([conda_py, ...])`. No `claude_hooks/` file implements this pattern; it exists only in the installer.

---

### Sites LACKING the fallback — exercisable from non-conda-env Python

- **`claude_hooks/providers/pgvector.py:328`** — `import psycopg` inside `_ensure_ready()`. This is the **runtime hot path**: every `recall()`, `store()`, `count()`, `batch_recall()`, `batch_store()`, plus all KG methods (`_kg_create_entities`, `_kg_add_observations`, `_kg_create_relations`, `_kg_search_nodes`) call `_ensure_ready()` first (lines 148, 214, 241, 265, 292, 439, 555, 592, 645, 676). When the hook shim (`bin/claude-hook`) resolves to a Python without psycopg, the `except ImportError` at line 329 catches and re-raises as `RuntimeError("install psycopg: pip install psycopg[binary]")` — no subprocess fallback. **REMEDIATION TARGET.** The `_ensure_ready()` method should shell out to the conda env's python for the psycopg `connect()` call when the current interpreter lacks psycopg.

- **`scripts/migrate_to_pgvector.py:624`** — top-level `import psycopg` in the `main()` CLI body. No try/except, no fallback. If invoked via `python scripts/migrate_to_pgvector.py` from a system python without psycopg, it crashes at line 624 with `ModuleNotFoundError`. **REMEDIATION TARGET.**

- **`scripts/bench_recall.py:107`** — `import psycopg` inside `PgvectorBenchProvider.__init__()`. Same pattern: bare import, no fallback. Crash if the benchmarking python lacks psycopg. **REMEDIATION TARGET.**

---

### Sites LACKING the fallback — NOT exercisable from non-conda-env Python in normal operation

- **`claude_hooks/providers/pgvector.py:123`** — `import psycopg` inside `PgvectorProvider.verify()`. The sole caller during normal operation is `install.py:_verify_pgvector_dsn()` at `install.py:2132`. That function (lines 2115-2161) has its own fast-path/fallback: it tries `import psycopg` at line 2127 first, and **only** enters the `PgvectorProvider.verify()` branch when that import succeeds. When psycopg is unavailable, the installer's fast path catches at `install.py:2135` and falls through to the `find_conda_env_python()` subprocess at `install.py:2140`. **Guarded by the installer wrapper — not a remediation target.** (A user calling `PgvectorProvider.verify()` from a REPL could trigger it, but that's not a normal operational path.)

- **`tests/test_pgvector_integration.py:56`** — `import psycopg` inside `_skip_if_no_deps()`. Raises `unittest.SkipTest` on failure. This is correct pytest behavior — skip when deps are absent. No fallback needed; the test contract is "don't run if psycopg is missing."

- **`tests/test_pgvector_integration.py:285`** — `import psycopg` inside a test helper (teardown or fixture). Same context — test-only, SkipTest on failure.

---

### Sites that ARE protected (not bugs)

- **`install.py:2127`** — fast-path `import psycopg` inside `_verify_pgvector_dsn()`. The `except ImportError` at line 2135 drops into the subprocess fallback at line 2140 (`conda_py = find_conda_env_python()`, then `subprocess.run([str(conda_py), "-c", _PGVECTOR_VERIFY_SCRIPT, dsn])`). **Protected.**

- **`install.py:2524` / `install.py:2539-2541`** — fast-path `import psycopg` inside `_table_exists_in_pgvector()`. The `except ImportError` at line 2536 falls through to `conda_py = find_conda_env_python()` at line 2540, then a subprocess inline script. **Protected.**

- **`install.py:2647`** (per `scripts/bench_recall.py` path from grep context) — same pattern. **Protected.**

---

### Summary: real remediation targets

| File:Line | Caller chain | What breaks |
|---|---|---|
| `claude_hooks/providers/pgvector.py:328` | Hook runtime → `dispatcher` → `provider.recall/store` → `_ensure_ready()` → `import psycopg` | Every recall/store/KG op fails with `RuntimeError` when the resolved python lacks psycopg |
| `scripts/migrate_to_pgvector.py:624` | CLI invocation (no shim) | `ModuleNotFoundError` at line 624 |
| `scripts/bench_recall.py:107` | CLI invocation (no shim) | `ModuleNotFoundError` at line 107 |

The two `scripts/` sites are low-severity (operator scripts, not hook runtime). The `pgvector.py:328` site is the high-impact target: it's on the hot path of every pgvector recall/store when the hook's Python lacks psycopg. A subprocess fallback there (similar to `install.py:2140`) would shell out to the conda env's python for the `connect()` call, so the provider works even when the hook shim resolves to system python3.

---

## Synthesizer

Three psycopg import sites lack the `4e67dc2` conda-env subprocess fallback and are **exercisable** from a non-conda-env Python:

- **`claude_hooks/providers/pgvector.py:328`** — `import psycopg` inside `_ensure_ready()`. Every `recall()`, `store()`, etc. calls this. On failure raises `RuntimeError("install psycopg: pip install psycopg[binary]")`. No attempt to shell out to conda env. **Remediation target** (high impact).
- **`scripts/migrate_to_pgvector.py:624`** — bare `import psycopg` in `main()`. No try/except. Crashes with `ModuleNotFoundError`. **Remediation target** (medium; operator script).
- **`scripts/bench_recall.py:107`** — bare `import psycopg` inside `PgvectorBenchProvider.__init__()`. No guard. Same crash. **Remediation target** (medium; benchmark script).

Three other sites lack the fallback but are **not exercisable** from a non-conda-env Python in normal use:

- `claude_hooks/providers/pgvector.py:123` — guarded by installer’s own fast-path: `install.py:2118-2161` tries `import psycopg` first and only calls `PgvectorProvider.verify()` when import succeeds. Non‑conda‑env paths are handled by the subprocess fallback in `install.py:2140`.
- `tests/test_pgvector_integration.py:56` — inside `_skip_if_no_deps()` raising `SkipTest`. Correct test behaviour.
- `tests/test_pgvector_integration.py:285` — inside a test tear-down; silence via `except Exception: pass`. Test-only low-impact.

All protected sites (`install.py:2127`, `install.py:2524`, `install.py:2647`) already use the `try: import psycopg` / `except ImportError` / `find_conda_env_python()` + `subprocess` pattern and are not bugs.

---
