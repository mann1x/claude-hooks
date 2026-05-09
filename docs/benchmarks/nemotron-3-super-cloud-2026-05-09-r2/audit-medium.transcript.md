# Consultation transcript — csl-2026-05-09-0740-5dbc

_Question_: Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python.


## Planner

1. Run `grep -Rn -- "import psycopg\|from psycopg" claude-hooks/` to locate all psycopg import sites with file:line.
2. For each match, extract the surrounding 10 lines and check for the conda-env subprocess fallback pattern introduced in commit 4e67dc2 (e.g., a call to `subprocess.run` with `conda` or a check of `CONDA_PREFIX`).
3. Flag any import site where the fallback pattern is absent.
4. For each flagged site, determine if the import is executed unconditionally or guarded by a condition that would be false outside a conda environment (e.g., inspect surrounding `if` statements for `CONDA_PREFIX` or similar env vars).
5. Output a list of file:line entries for sites lacking the fallback and mark them as “exercisable from non‑conda-env Python” if the import is unconditionally reachable, otherwise mark as “conda‑guarded”.

---

## Researcher (round 1)

- claude_hooks/providers/pgvector.py:123 - import inside `verify` method, no conda-env fallback; exercisable when provider verification runs (e.g., during installer detection or manual provider use)
- claude_hooks/providers/pgvector.py:328 - import inside `_ensure_ready` method, no conda-env fallback; exercisable when provider attempts to establish a DB connection (e.g., during recall/store operations)

---

## Researcher (round 1)

- `claude_hooks/providers/pgvector.py:123` — import inside `verify()` method, guarded by `try/except ImportError` but no conda-env fallback; exercisable from non-conda-env Python (returns False on missing psycopg)
- `claude_hooks/providers/pgvector.py:328` — import inside `_ensure_ready()` method, guarded by `try/except ImportError` but no conda-env fallback; exercisable from non-conda-env Python (raises ImportError with message)
- `scripts/migrate_to_pgvector.py:624` — bare import in `main()` function, no guard or fallback; exercisable from non-conda-env Python (crashes with ModuleNotFoundError)
- `scripts/bench_recall.py:107` — import inside `PgvectorBenchProvider.__init__()`, no guard or fallback; exercisable from non-conda-env Python when benchmark code path is hit (crashes with ModuleNotFoundError)
- `tests/test_pgvector_integration.py:56` — import inside `_skip_if_no_deps()`, guarded by `try/except ImportError` but no conda-env fallback; exercisable from non-conda-env Python (raises unittest.SkipTest)
- `tests/test_pgvector_integration.py:285` — import inside `tearDownClass()`, guarded by `try/except Exception: pass` but no conda-env fallback; exercisable from non-conda-env Python (swallows ImportError)

All listed import sites lack the conda-env subprocess fallback pattern (e.g., no call to `find_conda_env_python()` or subprocess launch) and are exercisable from a non-conda-env Python environment. Sites in `install.py` (lines 2127, 2524, 2647) were verified to have the fallback pattern and are excluded. String literals (install.py:2546, 2666) are not actual imports.

---

## Researcher (round 1)

claude_hooks/providers/pgvector.py:123 — exercisable  
claude_hooks/providers/pgvector.py:328 — exercisable  
scripts/migrate_to_pgvector.py:624 — exercisable  
scripts/bench_recall.py:107 — exercisable  
tests/test_pgvector_integration.py:56 — exercisable  
tests/test_pgvector_integration.py:285 — exercisable

---

## Synthesizer

- claude_hooks/providers/pgvector.py:123 – exercisable from non‑conda-env Python  
- claude_hooks/providers/pgvector.py:328 – exercisable from non‑conda-env Python  
- scripts/migrate_to_pgvector.py:624 – exercisable from non‑conda-env Python  
- scripts/bench_recall.py:107 – exercisable from non‑conda-env Python  
- tests/test_pgvector_integration.py:56 – exercisable from non‑conda-env Python  
- tests/test_pgvector_integration.py:285 – exercisable from non‑conda-env Python

---
