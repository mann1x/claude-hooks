# Consultation transcript — csl-2026-05-09-0810-8894

_Question_: Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python.


## Planner

1. Examine commit 4e67dc2 to identify the exact conda-env subprocess fallback pattern added for psycopg imports (e.g., a try/except ImportError block that calls subprocess to activate the conda environment).  
2. Run `git grep -n 'import psycopg\|from psycopg' claude-hooks/` to locate every psycopg import statement with file and line number.  
3. For each match from step 2, view the surrounding 10‑line context to verify whether the fallback pattern from step 1 is present.  
4. If the fallback is absent, determine if the import lies within a function or conditional that is only executed when a conda‑specific environment variable (e.g., `CONDA_DEFAULT_ENV`) is set; if not, flag it as exercisable from a non‑conda‑env Python.  
5. For any remaining ambiguous cases, check the call graph (e.g., using `grep -r '<function_name>' claude-hooks/`) to see if the import is reachable from entry points invoked outside a conda environment.  
6. Compile the findings into a list of `file:line` entries accompanied by a yes/no indicator of whether the import is actually exercisable without a conda environment.

---

## Researcher (round 1)

(researcher lane produced no findings within the iteration budget; gaps remain — see plan)

---

## Researcher (round 1)

- claude_hooks/providers/pgvector.py:123 — Import inside `verify()` method; try/except ImportError returns False but executes in main interpreter. Exercisable from any Python process calling `PgvectorProvider.verify()`.
- claude_hooks/providers/pgvector.py:328 — Import inside `_ensure_ready()` method; try/except ImportError re-raises without attempting conda fallback. Exercisable when instantiating `PgvectorProvider`.
- install.py:2092 — Bare `import psycopg` inside a function; no try/except or subprocess fallback visible in grep output. Executability depends on whether this function is called.
- scripts/migrate_to_pgvector.py:624 — Bare `import psycopg` inside `main()` with no guard or fallback; will crash with `ModuleNotFoundError` if psycopg absent.
- scripts/bench_recall.py:107 — Import inside `PgvectorBenchProvider.__init__` with no guard or fallback; exercisable when benchmark code path runs.

---

## Researcher (round 1)

- claude_hooks/providers/pgvector.py:123: yes
- claude_hooks/providers/pgvector.py:328: yes

---

## Synthesizer

- claude_hooks/providers/pgvector.py:123 – yes  
- claude_hooks/providers/pgvector.py:328 – yes  
- install.py:2092 – yes  
- scripts/migrate_to_pgvector.py:624 – yes  
- scripts/bench_recall.py:107 – yes

---
