# Consultation transcript — csl-2026-10-22-2016-2fb1

_Question_: Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python.


## Planner

1. Examine `claude_hooks/providers/pgvector.py` to check how `psycopg` is imported at lines 123 and 328, and whether any subprocess fallback exists.
2. Inspect `scripts/migrate_to_pgvector.py:624` to determine if the `psycopg` import there lacks the fallback and if it's exercisable from a non-conda-env Python.
3. Inspect `scripts/bench_recall.py:107` to determine if the `psycopg` import there lacks the fallback and if it's exercisable from a non-conda-env Python.
4. Review `tests/test_pgvector_integration.py` imports (lines 56, 285) to see if they are intended to be "exercisable" in a way that requires the fallback.
5. Cross-reference any other `psycopg` usage found via `grep` (excluding `install.py` and `docs/`) to ensure no sites were missed.

---

## Researcher (round 1)

The audit of `psycopg` import sites reveals that while the `install.py` tool has been updated with a conda-env subprocess fallback, the actual provider and operator scripts still use naked `import psycopg` statements.

### Non-Compliant Import Sites

*   **`claude_hooks/providers/pgvector.py:123`**: Naked `import psycopg` inside `verify()`.
*   **`claude_hooks/providers/pgvector.py:328`**: Naked `import psycopg` inside `_ensure_ready()`.
*   **`scripts/migrate_to_pgvector.py:624`**: Naked `import psycopg` inside `main()`.
*   **`scripts/bench_recall.py:107`**: Naked `import psycopg` inside `PgvectorBenchProvider.__init__`.

### Exercisability Analysis

| File:Line | Exercisable from non-conda Python? | Note |
| :--- | :--- | :--- |
| `claude_hooks/providers/pgvector.py:123` | **Yes** | Called by `install.py` during provider verification. |
| `claude_hooks/providers/pgvector.py:328` | **Yes** | Called whenever the `PgvectorProvider` is instantiated and used (recall/store). |
| `scripts/migrate_to_pgvector.py:624` | **Yes** | Executed as a standalone migration script. |
| `scripts/bench_recall.py:107` | **Yes** | Executed as a standalone benchmarking script. |

**Conclusion**: All four sites lack the subprocess fallback and will raise an `ImportError` if executed in a Python environment where `psycopg` is not installed, regardless of whether a valid `claude-hooks` conda environment exists on the system.

---

## Researcher (round 1)

The audit for `psycopg` import sites lacking the conda-env subprocess fallback (introduced in 4e67dc2) identifies the following:

### 1. Core Provider
*   `claude_hooks/providers/pgvector.py:123` and `:328`: These sites use `import psycopg` within `try/except ImportError` blocks. They do **not** use the subprocess fallback, but they are intended as lazy-imports for the provider's runtime. 
    *   **Exercisability**: High. If a user configures pgvector without installing `psycopg` in their current Python environment, these will trigger the `ImportError` and a warning/exception. They are "exercisable" but intended to fail gracefully.

### 2. Integration Tests
*   `tests/test_pgvector_integration.py:56` and `:285`: Both sites use `import psycopg` within `try/except` blocks to decide whether to skip tests.
    *   **Exercisability**: Moderate. These are only executed when running integration tests. They are designed to skip (`unittest.SkipTest`) if `psycopg` is missing, so they do not "break" the test suite, but they do not attempt to find the conda-env fallback.

### 3. Operator Scripts (Missing Fallback)
The following scripts perform direct `import psycopg` calls without any `try/except` or subprocess fallback:
*   `scripts/migrate_to_pgvector.py:624`: Direct import.
    *   **Exercisability**: High. Any user running this migration script from a non-conda environment without `psycopg` installed will experience a hard `ImportError` crash.
*   `scripts/bench_recall.py:107`: Direct import.
    *   **Exercisability**: High. Running the benchmark without the dependency will result in a hard crash.

### Summary Table

| File | Line(s) | Fallback Present? | Exercisable (Non-Conda)? | Result if Missing |
| :--- | :--- | :--- | :--- | :--- |
| `claude_hooks/providers/pgvector.py` | 123, 328 | No | Yes | Graceful `ImportError` |
| `tests/test_pgvector_integration.py` | 56, 285 | No | Yes | `unittest.SkipTest` |
| `scripts/migrate_to_pgvector.py` | 624 | No | Yes | **Hard Crash** |
| `scripts/bench_recall.py` | 107 | No | Yes | **Hard Crash** |

**Note**: `install.py` was excluded per plan but is confirmed via `grep` to have implemented the fallback correctly (e.g., `install.py:2138`).

---

## Synthesizer

Six import sites still lack the conda-env subprocess fallback:

*   `claude_hooks/providers/pgvector.py:123`: Yes
*   `claude_hooks/providers/pgvector.py:328`: Yes
*   `scripts/migrate_to_pgvector.py:624`: Yes
*   `scripts/bench_recall.py:107`: Yes
*   `tests/test_pgvector_integration.py:56`: Yes
*   `tests/test_pgvector_integration.py:285`: Yes

---
