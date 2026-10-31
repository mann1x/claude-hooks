# Consultation transcript — csl-2026-10-22-1851-4be3

_Question_: Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python.


## Planner

1. Read `install.py` around lines 2118, 2138, and 2516 to identify the specific implementation pattern of the "conda-env subprocess fallback".
2. Audit `claude_hooks/providers/pgvector.py` (lines 123, 328) to verify if the identified fallback pattern is missing.
3. Audit `scripts/migrate_to_pgvector.py` (line 624) and `scripts/bench_recall.py` (line 107) for the same missing pattern.
4. Audit `tests/test_pgvector_integration.py` (lines 56, 285) for the same missing pattern.
5. For each identified site, analyze the call graph/entry point to determine if the code is reachable when running the project from a Python environment outside the designated conda environment.

---

## Researcher (round 1)

The "conda-env subprocess fallback" pattern identified in `install.py` (lines 2126-2149 and 2523-2530) consists of a `try...except ImportError` block that attempts a direct import of `psycopg`, and upon failure, shells out to the `claude-hooks` conda environment's Python executable via `subprocess.run` to execute the task.

Audit results for `psycopg` import sites:

*   `claude_hooks/providers/pgvector.py:123`: **Missing fallback**. It catches `ImportError` and returns `False` with a warning. 
    *   *Exercisable*: Yes. `PgvectorProvider.verify` is called by `install.py` and other discovery tools; if run from a non-conda Python, it will simply fail to verify the server.
*   `claude_hooks/providers/pgvector.py:328`: **Missing fallback**. It catches `ImportError` and raises a user-facing `ImportError` instructing them to `pip install`.
    *   *Exercisable*: Yes. This is the primary `_ensure_ready` path for the provider during actual recall/store operations.
*   `scripts/migrate_to_pgvector.py:624`: **Missing fallback**. Direct `import psycopg` without any `try...except` block.
    *   *Exercisable*: Yes. If the migration script is run via `python scripts/migrate_to_pgvector.py` from a system Python, it will crash with a `ModuleNotFoundError`.
*   `scripts/bench_recall.py:107`: **Missing fallback**. Direct `import psycopg` inside `PgvectorBenchProvider.__init__`.
    *   *Exercisable*: Yes. If the benchmark is run from a non-conda Python, it will crash with a `ModuleNotFoundError`.

---

## Researcher (round 1)

The audit of `tests/test_pgvector_integration.py` confirms two sites lacking the conda-env subprocess fallback:

*   `tests/test_pgvector_integration.py:56`: Performs a direct `import psycopg` within `_skip_if_no_deps()`. If `psycopg` is missing, it raises `unittest.SkipTest`.
*   `tests/test_pgvector_integration.py:285`: Performs a direct `import psycopg` within `tearDownClass()` to clean up test tables.

**Exercisability Analysis:**
Both sites are **not exercisable** as bugs from a non-conda-env Python in a way that would cause failure:
1.  **Line 56**: The logic explicitly catches `ImportError` (line 57) and skips the test. This is the intended behavior for integration tests when dependencies are missing.
2.  **Line 285**: The import is wrapped in a broad `try...except Exception` (lines 284-294), ensuring that a missing `psycopg` installation during teardown does not crash the test suite.

While these sites lack the "active" fallback (which attempts to find a conda env to execute a probe), the context of a test suite makes the current "skip or ignore" pattern appropriate.

---

## Synthesizer

The following sites lack the conda-env subprocess fallback:

*   `claude_hooks/providers/pgvector.py:123`: **Exercisable**. Returns `False` upon `ImportError` during verification.
*   `claude_hooks/providers/pgvector.py:328`: **Exercisable**. Raises a user-facing `ImportError` requesting `pip install` during provider readiness checks.
*   `scripts/migrate_to_pgvector.py:624`: **Exercisable**. Direct import will cause a `ModuleNotFoundError` crash if run from a non-conda Python.
*   `scripts/bench_recall.py:107`: **Exercisable**. Direct import will cause a `ModuleNotFoundError` crash if run from a non-conda Python.
*   `tests/test_pgvector_integration.py:56`: **Not exercisable**. The import is part of `_skip_if_no_deps()`, which catches the error to skip the test.
*   `tests/test_pgvector_integration.py:285 [no _skip_if_no_deps at this line; line is in tearDownClass]`: **Not exercisable**. The import in `tearDownClass()` is wrapped in a broad `try...except Exception` block.

---
