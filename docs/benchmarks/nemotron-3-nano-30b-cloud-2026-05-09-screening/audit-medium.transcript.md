# Consultation transcript — csl-2026-05-09-0611-88e9

_Question_: Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python.


## Planner

1. Checkout commit 4e67dc2 and diff it to locate all files that received the conda‑env subprocess fallback.  
2. Search each of those files for `import psycopg` statements that are not accompanied by the fallback subprocess invocation.  
3. For each uncovered import site, examine the surrounding code to confirm the fallback is absent or incomplete.  
4. Test the identified import statements in a plain Python virtual environment (no conda) to see if they raise an executable error.  
5. Record each file:line location and annotate whether it is exercisable from a non‑conda‑env Python interpreter.

---

## Researcher (round 1)

(researcher lane produced no findings within the iteration budget; gaps remain — see plan)

---

## Researcher (round 1)

- `claude_hooks/providers/pgvector.py:123` — direct `import psycopg` at module level, no `try/except` or subprocess fallback; **exercisable** (will raise `ImportError` in a plain Python env).  
- `claude_hooks/providers/pgvector.py:328` — direct `import psycopg` inside `_ensure_ready()`, no fallback guard; **exercisable** (raises `ImportError` when `PgvectorProvider` is instantiated).  
- `scripts/migrate_to_pgvector.py:624` — bare `import psycopg` inside `main()`; no error handling or conda‑env spawn; **exercisable** (fails with `ModuleNotFoundError` if `psycopg` is absent).  
- `scripts/bench_recall.py:107` — `import psycopg` inside `PgvectorBenchProvider.__init__()` with no guard; **exercisable** (fails at runtime when the benchmark code path is triggered).  
- `tests/test_pgvector_integration.py:56` — top‑level `import psycopg` in the test file; no fallback; **exercisable** (import error aborts the test suite).  
- `tests/test_pgvector_integration.py:285` — `import psycopg` inside `tearDownClass()` without error handling; **exercisable** (will raise `ImportError` if `psycopg` missing).  

*All listed sites lack the conda‑env subprocess fallback introduced in commit `4e67dc2`. Each import can be triggered from a standard Python virtual environment (no `conda` activation) and will raise an import‑time error, confirming the fallback is missing.*

---

## Researcher (round 1)

- `claude_hooks/providers/pgvector.py:123` — Import inside `PgvectorProvider.verify()` is wrapped in `try/except ImportError`; **not exercisable** from a non‑conda‑env Python (graceful fallback).  
- `claude_hooks/providers/pgvector.py:328` — Import inside `PgvectorProvider._ensure_ready()` re‑raises `ImportError`; **exercisable** and will crash if `psycopg` is absent outside a conda env.  
- `install.py:2127` — Fast‑path import of `psycopg` with no guard or fallback; **exercisable** and will raise `ModuleNotFoundError` when run from a non‑conda‑env Python.

---

## Synthesizer

- claude_hooks/providers/pgvector.py:328 — exercisable  
- scripts/migrate_to_pgvector.py:624 — exercisable  
- scripts/bench_recall.py:107 — exercisable  
- tests/test_pgvector_integration.py:56 — exercisable  
- tests/test_pgvector_integration.py:285 — exercisable  
- install.py:2127 — exercisable

---
