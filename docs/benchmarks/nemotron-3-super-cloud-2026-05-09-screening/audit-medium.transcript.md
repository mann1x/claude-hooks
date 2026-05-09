# Consultation transcript — csl-2026-05-09-0621-2dfe

_Question_: Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python.


## Planner

1. Show the changes introduced in commit 4e67dc2 to identify the exact conda‑env subprocess fallback pattern that was added (e.g., `try: import psycopg2 ... except ImportError: subprocess.check_call([...])`).  
2. Run `git grep -n 'import psycopg' claude-hooks/` to locate all psycopg import statements and record their file:line numbers.  
3. For each import match, extract a context window (e.g., `-10, +10` lines) and check whether the fallback pattern from step 1 appears in that window.  
4. If the fallback is absent, examine the surrounding function or module to determine whether the code path can be invoked when running outside a conda environment (e.g., not guarded by a conda‑specific environment variable).  
5. Compile a list of file:line entries where the import lacks the fallback and mark each as “exercisable” or “non‑exercisable” based on the analysis in step 4.

---

## Researcher (round 1)

(researcher lane produced no findings within the iteration budget; gaps remain — see plan)

---

## Researcher (round 1)

claude_hooks/providers/pgvector.py:123 - lacks conda-env subprocess fallback; exercisable outside conda-env (called by detector during provider verification, runs in installer/hook contexts that may use system Python)
claude_hooks/providers/pgvector.py:328 - lacks conda-env subprocess fallback; exercisable outside conda-env (called during provider initialization when connection is lazily established, runs in hook handler paths that may use system Python)

---

## Researcher (round 1)

- claude_hooks/providers/pgvector.py:123 — exercisable (import in verify() method; runs in main interpreter on ImportError, no conda fallback)
- claude_hooks/providers/pgvector.py:328 — exercisable (import in _ensure_ready() method; re-raises ImportError on failure, no conda fallback)
- scripts/migrate_to_pgvector.py:624 — exercisable (bare import in main(); crashes with ModuleNotFoundError if psycopg missing)
- scripts/bench_recall.py:107 — exercisable (import in PgvectorBenchProvider.__init__(); crashes if benchmark runs without psycopg)
- tests/test_pgvector_integration.py:56 — exercisable (import in _skip_if_no_deps(); raises SkipTest on ImportError, no conda fallback)
- tests/test_pgvector_integration.py:285 — exercisable (import in tearDownClass(); broad except swallows error but no conda fallback attempted)

---

## Synthesizer

- claude_hooks/providers/pgvector.py:123 – exercisable  
- claude_hooks/providers/pgvector.py:328 – exercisable  
- claude_hooks/scripts/migrate_to_pgvector.py:624 – exercisable  
- claude_hooks/scripts/bench_recall.py:107 – exercisable  
- claude_hooks/tests/test_pgvector_integration.py:56 – exercisable  
- claude_hooks/tests/test_pgvector_integration.py:285 – exercisable

---
