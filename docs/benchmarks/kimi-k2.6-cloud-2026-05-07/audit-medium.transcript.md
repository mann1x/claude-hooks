# Consultation transcript — csl-2026-05-07-0810-5c85

_Question_: Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python.


## Planner

1. Run `git show 4e67dc2` to extract the exact conda-env subprocess fallback pattern introduced for psycopg imports.
2. Run `rg -n 'import psycopg2?\b'` across the repository to enumerate every psycopg import site with file:line coordinates.
3. Read ±15 lines around each match to flag import sites that lack the fallback pattern identified in step 1.
4. For each flagged site, trace call paths up to entry points (console_scripts, `__main__`, plugin hooks) to determine if a non-conda-env Python process can reach that import at runtime.
5. Inspect `bin/`, `scripts/`, and test directories for direct psycopg imports that bypass any library-level fallback wrapper.
6. Note whether any unprotected imports sit inside `try/except ImportError` blocks or behind `CONDA_PREFIX`/`os.environ` guards that already mitigate non-conda execution.

---

## Researcher (round 1)

**Note:** `git show 4e67dc2` could not be executed (tool unavailable), so the exact subprocess-fallback pattern was not retrieved. However, every `psycopg` import site discovered in the repository is a direct in-process import with no conda-env subprocess wrapper visible in the surrounding code. Below are the six sites, all exercisable from a non-conda-env Python interpreter (i.e., they will run in the current process and raise `ImportError` / `ModuleNotFoundError` or skip when `psycopg` is absent).

- **`claude_hooks/providers/pgvector.py:123`** — Direct `import psycopg` inside `PgvectorProvider.verify()`. Catches `ImportError` and returns `False`, but still executes in the main interpreter; exercisable from a non-conda-env Python (gracefully skips verification if the package is missing).
- **`claude_hooks/providers/pgvector.py:328`** — Direct `import psycopg` inside `_ensure_ready()`. Catches `ImportError` and re-raises as `ImportError("install psycopg: ...")`; exercisable from a non-conda-env Python and will abort with an import error.
- **`tests/test_pgvector_integration.py:56`** — Direct `import psycopg` inside `_skip_if_no_deps()`. Catches `ImportError` and raises `unittest.SkipTest`; exercisable from a non-conda-env Python (causes tests to skip).
- **`tests/test_pgvector_integration.py:285`** — Direct `import psycopg` inside `tearDownClass()`. Wrapped in a broad `except Exception: pass`; exercisable from a non-conda-env Python (swallows the failure).
- **`scripts/migrate_to_pgvector.py:624`** — Bare direct `import psycopg` inside `main()`. No fallback wrapper; exercisable from a non-conda-env Python and will crash with `ModuleNotFoundError`.
- **`scripts/bench_recall.py:107`** — Direct `import psycopg` inside `PgvectorBenchProvider.__init__()`. No fallback wrapper; exercisable from a non-conda-env Python and will crash with `ModuleNotFoundError`.

**No import sites found in:** `consultants/` · `episodic_server/`.

---

## Researcher (round 1)

(researcher lane produced no findings within the iteration budget; gaps remain — see plan)

---

## Researcher (round 1)

**`psycopg` import sites still lacking the `4e67dc2` conda-env subprocess fallback**

- `claude_hooks/providers/pgvector.py:123` — **Exercisable: Yes**. Inside `PgvectorProvider.verify()`; wrapped in `try/except ImportError` (returns `False`) but has no conda-env subprocess fallback.  
- `claude_hooks/providers/pgvector.py:328` — **Exercisable: Yes**. Inside `PgvectorProvider._ensure_ready()`; wrapped in `try/except ImportError` (re-raises) but has no conda-env subprocess fallback.  
- `scripts/migrate_to_pgvector.py:624` — **Exercisable: Yes**. Bare `import psycopg` in `main()` with no `try/except ImportError` and no conda fallback; crashes if run from a Python without psycopg.  
- `scripts/bench_recall.py:107` — **Exercisable: Yes**. Bare `import psycopg` in `PgvectorBenchProvider.__init__` with no guard and no conda fallback; crashes if the benchmark script is run from a Python without psycopg.  
- `tests/test_pgvector_integration.py:56` — **Exercisable: Yes**. Inside `_skip_if_no_deps()`; caught by `except ImportError` → raises `SkipTest`, but no conda fallback is attempted.  
- `tests/test_pgvector_integration.py:285` — **Exercisable: Yes**. Inside `tearDownClass`; caught by a broad `except Exception: pass`, but no conda fallback is attempted.  

**Already covered or non-executable (for completeness)**

- `install.py:2127`, `install.py:2524`, `install.py:2647` — **Not lacking**: each is the fast-path `try: import psycopg` inside a function that falls back to a `find_conda_env_python()` subprocess on `ImportError`.  
- `install.py:2092`, `install.py:2546`, `install.py:2666` — **Not exercisable**: these sit inside triple-quoted string literals (subprocess script templates), not executed as imports by the host interpreter.

---

## Synthesizer

Six psycopg import sites across four files still lack the conda-env subprocess fallback and are all exercisable from a non-conda-env Python.

- `claude_hooks/providers/pgvector.py:123` — Exercisable: yes. `PgvectorProvider.verify()` imports psycopg in-process; `except ImportError` returns `False` but never tries a conda-env subprocess.
- `claude_hooks/providers/pgvector.py:328` — Exercisable: yes. `PgvectorProvider._ensure_ready()` imports psycopg in-process; `except ImportError` re-raises without a conda-env fallback.
- `scripts/migrate_to_pgvector.py:624` — Exercisable: yes. Bare `import psycopg` in `main()` with no guard or conda fallback; crashes with `ModuleNotFoundError` when missing.
- `scripts/bench_recall.py:107` — Exercisable: yes. Bare `import psycopg` in `PgvectorBenchProvider.__init__` with no guard or conda fallback; crashes with `ModuleNotFoundError` when missing.
- `tests/test_pgvector_integration.py:56` — Exercisable: yes. `_skip_if_no_deps()` imports psycopg in-process and raises `SkipTest` on `ImportError`, but does not attempt a conda-env subprocess.
- `tests/test_pgvector_integration.py:285` — Exercisable: yes. `tearDownClass` imports psycopg in-process; a broad `except Exception: pass` swallows the error, but no conda fallback is attempted.

---
