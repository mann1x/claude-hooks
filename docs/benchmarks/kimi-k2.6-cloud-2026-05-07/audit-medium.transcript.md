# Consultation transcript — csl-2026-05-07-0907-5644

_Question_: Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python.


## Planner

1. `git show 4e67dc2 -p` to record the exact conda-env subprocess fallback pattern and the files changed in that commit.
2. `git grep -n 'import psycopg\|from psycopg'` to list every current psycopg import site with file:line.
3. For each file:line from step 2, read the surrounding 20 lines to check whether the fallback pattern from step 1 is already present or absent.
4. For each import site lacking the fallback, grep for references to its enclosing function/class/module in entry-point scripts, `__main__` blocks, and root `__init__.py` files to determine call-path reachability from non-conda-env Python.
5. Inspect any module-level `psycopg` imports (top-of-file or `__init__.py`) that lack the fallback, since unconditional import-time execution makes them trivially exercisable from any Python process importing that module.

---

## Researcher (round 1)

**Psycopg import sites still lacking the `4e67dc2` conda-env subprocess fallback**

- `claude_hooks/providers/pgvector.py:123` — **Exercisable: yes.** `PgvectorProvider.verify()` does `import psycopg` in-process; the `except ImportError` handler returns `False` but never attempts a conda-env subprocess fallback.
- `claude_hooks/providers/pgvector.py:328` — **Exercisable: yes.** `PgvectorProvider._ensure_ready()` does `import psycopg` in-process; the `except ImportError` handler re-raises without attempting a conda-env fallback.
- `scripts/migrate_to_pgvector.py:624` — **Exercisable: yes.** Bare `import psycopg` inside `main()` with no guard or conda fallback; will crash with `ModuleNotFoundError` when psycopg is absent.
- `scripts/bench_recall.py:107` — **Exercisable: yes.** Bare `import psycopg` inside `PgvectorBenchProvider.__init__` with no guard or conda fallback; will crash with `ModuleNotFoundError` when psycopg is absent.
- `tests/test_pgvector_integration.py:56` — **Exercisable: yes (from pytest).** `_skip_if_no_deps()` imports psycopg in-process and raises `SkipTest` on `ImportError`, but does not attempt a conda-env subprocess.
- `tests/test_pgvector_integration.py:285` — **Exercisable: yes (from pytest).** `tearDownClass` imports psycopg in-process; a broad `except Exception: pass` swallows the error, but no conda fallback is attempted.

**Protected sites (not bugs)** — The following `install.py` lines are either covered by the `4e67dc2` subprocess fallback or live inside subprocess inline script strings and therefore do **not** count as missing the fallback: `install.py:2092` · `install.py:2127` · `install.py:2524` · `install.py:2546` · `install.py:2647` · `install.py:2666`.

---

## Researcher (round 1)

(researcher lane produced no findings within the iteration budget; gaps remain — see plan)

---

## Researcher (round 1)

**Findings — no conda-env subprocess fallback for psycopg exists in the codebase**

- No commit `4e67dc2` or fallback pattern (`subprocess.*python`, `_spawn.*psycopg`, `fallback.*psycopg`) was found in `claude_hooks/` (all four regexes returned empty).  
- No **module-level** `import psycopg` (top-of-file or `__init__.py`) exists in `claude_hooks/` — the only imports are deferred inside methods.

**Deferred import sites inside `claude_hooks/` (both lack fallback, both exercisable from a non-conda-env Python)**

- `claude_hooks/providers/pgvector.py:123` — `import psycopg` deferred inside `verify()`; wrapped in `try/except ImportError`. **Exercisable from any Python process** that calls `PgvectorProvider.verify()`; simply returns `False` if psycopg is absent.  
- `claude_hooks/providers/pgvector.py:328` — `import psycopg` deferred inside `_ensure_ready()`; wrapped in `try/except ImportError`. **Exercisable from any Python process** that instantiates `PgvectorProvider` and calls `recall()`, `store()`, etc.; raises `ImportError` if psycopg is missing.

**Deferred import sites inside `scripts/` (also lack fallback)**

- `scripts/migrate_to_pgvector.py:624` — bare `import psycopg` inside `main()`. **Exercisable from any Python** when the script is invoked directly; crashes with `ImportError` at runtime if psycopg is not installed.  
- `scripts/bench_recall.py:107` — `import psycopg` indented inside a method/function. **Conditionally exercisable** when the pgvector benchmark code path is hit; behaves like a normal runtime dependency check.

**Bottom line:** There are no unconditional, import-time psycopg dependencies in `claude_hooks/`, so no trivial “import the module and crash” exposure. However, **every** psycopg import site in both the package and the scripts currently lacks the conda-env subprocess fallback; they all rely on the active Python interpreter having psycopg installed.

---

## Synthesizer

- `claude_hooks/providers/pgvector.py:123` — exercisable. `PgvectorProvider.verify()` does an in-process `import psycopg`; the `except ImportError` handler returns `False` without attempting a conda-env subprocess fallback.
- `claude_hooks/providers/pgvector.py:328` — exercisable. `PgvectorProvider._ensure_ready()` does an in-process `import psycopg`; the `except ImportError` handler re-raises without attempting a conda-env fallback.
- `scripts/migrate_to_pgvector.py:624` — exercisable. Bare `import psycopg` inside `main()`; crashes with `ModuleNotFoundError` if psycopg is absent, with no conda fallback.
- `scripts/bench_recall.py:107` — exercisable (conditional). `import psycopg` inside `PgvectorBenchProvider.__init__`; crashes with `ModuleNotFoundError` if the benchmark code path is hit and psycopg is absent.
- `tests/test_pgvector_integration.py:56` — exercisable from pytest. `_skip_if_no_deps()` imports psycopg in-process; raises `SkipTest` on `ImportError`, no conda fallback.
- `tests/test_pgvector_integration.py:285` — exercisable from pytest. `tearDownClass` imports psycopg in-process; a broad `except Exception: pass` swallows the error, but no conda fallback is attempted.

---
