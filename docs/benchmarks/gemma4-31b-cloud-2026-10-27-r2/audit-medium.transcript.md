# Consultation transcript — csl-2026-10-27-1141-1376

_Question_: Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python.


## Planner

1. Examine `claude_hooks/providers/pgvector.py` at lines 123 and 328 to check if `import psycopg` is wrapped in a try/except block that invokes a conda-env subprocess fallback.
2. Examine `scripts/migrate_to_pgvector.py` at line 624 to check for the conda-env subprocess fallback.
3. Examine `scripts/bench_recall.py` at line 107 to check for the conda-env subprocess fallback.
4. Review the other `psycopg` import sites in `install.py` (lines 2092, 2524, 2647) to ensure they either use the fallback or are intentionally direct imports (e.g., inside a check function).
5. For each site lacking the fallback, determine if it is exercisable from a non-conda-env Python by tracing the call path from entry points (e.g., `run.py`, `run_daemon.py`, or the specific scripts).

---

## Researcher (round 1)

The audit of `psycopg` import sites reveals several locations that lack the conda-env subprocess fallback. While `install.py` (lines 2125-2142, 2524-2546, 2645-2668) correctly implements the "fast path vs. fallback" pattern, the following sites are missing it:

### Missing Fallback Sites

*   **`claude_hooks/providers/pgvector.py:123`**: Wrapped in `try/except ImportError` but simply logs a warning and returns `False`. It does **not** attempt a subprocess fallback.
    *   **Exercisable?** Yes. This is called during provider `verify()`. If run from a non-conda Python, it will fail to verify the provider.
*   **`claude_hooks/providers/pgvector.py:328`**: Wrapped in `try/except ImportError` but simply raises the `ImportError` with a pip install hint.
    *   **Exercisable?** Yes. This is called in `_ensure_ready()` during `recall()` or `store()`. If the user's current interpreter lacks `psycopg`, the provider becomes unusable.
*   **`scripts/migrate_to_pgvector.py:624`**: Raw `import psycopg` at the start of `main()`.
    *   **Exercisable?** Yes. Running this migration script from a non-conda Python will result in an immediate `ImportError`.
*   **`scripts/bench_recall.py:107`**: Raw `import psycopg` inside `PgvectorBenchProvider.__init__`.
    *   **Exercisable?** Yes. Attempting to run the pgvector benchmark from a non-conda Python will trigger an `ImportError`.

### Summary Table

| File | Line | Fallback Present? | Exercisable from non-conda Python? | Result without fallback |
| :--- | :--- | :--- | :--- | :--- |
| `claude_hooks/providers/pgvector.py` | 123 | No | Yes | `verify()` returns `False` |
| `claude_hooks/providers/pgvector.py` | 328 | No | Yes | `ImportError` raised |
| `scripts/migrate_to_pgvector.py` | 624 | No | Yes | `ImportError` (crash) |
| `scripts/bench_recall.py` | 107 | No | Yes | `ImportError` (crash) |

---

## Researcher (round 1)

The audit of `psycopg` import sites in `install.py` and related components reveals the following:

### 1. `install.py` Import Sites
The specific lines mentioned in the plan are already covered by the conda-env subprocess fallback:

*   **`install.py:2092`**: This is inside `_PGVECTOR_VERIFY_SCRIPT`, which is a string literal. It is executed via `subprocess.run([str(conda_py), "-c", _PGVECTOR_VERIFY_SCRIPT, dsn], ...)` at `install.py:2147`. **(Fallback active)**.
*   **`install.py:2524`**: This is the "Fast path" inside a function that explicitly implements the fallback via a subprocess call starting at `install.py:2539`. **(Fallback active)**.
*   **`install.py:2647`**: This is the "Fast path" inside a function that implements the fallback starting at `install.py:2656`. **(Fallback active)**.

### 2. Other `psycopg` Import Sites (Lack Fallback)
Several sites lack the subprocess fallback because they are either core provider logic (which *must* have the dependency to function) or standalone scripts:

| File:Line | Context | Exercisable from non-conda Python? | Note |
| :--- | :--- | :--- | :--- |
| `claude_hooks/providers/pgvector.py:123` | `Provider.verify()` | **Yes** | Called by `install.py` during provider detection. Returns `False` on `ImportError` (`pgvector.py:126`). |
| `claude_hooks/providers/pgvector.py:328` | `_ensure_ready()` | **Yes** | Called during runtime recall/store. Raises `ImportError` (`pgvector.py:330`) if missing. |
| `scripts/migrate_to_pgvector.py:624` | Migration script | **Yes** | Direct import. Fails with `ImportError` if not in current env. |
| `scripts/bench_recall.py:107` | Benchmark script | **Yes** | Direct import. Fails with `ImportError` if not in current env. |
| `tests/test_pgvector_integration.py:56` | Integration tests | **Yes** | Skips test if `psycopg` is missing (`test_pgvector_integration.py:58`). |

### Summary of Findings
All `install.py` paths identified in the plan correctly utilize the conda-env fallback. The remaining sites that lack it are either:
1.  **Runtime components** (`providers/pgvector.py`) where the dependency is mandatory for the feature to work.
2.  **Operator tools** (`scripts/`) which are expected to be run within the configured conda environment.

---

## Synthesizer

* `claude_hooks/providers/pgvector.py:123`: Yes (exercisable during provider `verify()`; currently returns `False` on `ImportError`)
* `claude_hooks/providers/pgvector.py:328 [no verify at this line; line is in _ensure_ready]`: Yes (exercisable during `recall()` or `store()` via `_ensure_ready()`; currently raises `ImportError`)
* `scripts/migrate_to_pgvector.py:624 [no _ensure_ready at this line; line is in main]`: Yes (direct import in `main()`; causes crash on `ImportError`)
* `scripts/bench_recall.py:107 [no main at this line; line is in __init__]`: Yes (direct import in `PgvectorBenchProvider.__init__`; causes crash on `ImportError`)
* `tests/test_pgvector_integration.py:56 [no __init__ at this line; line is in _skip_if_no_deps]`: Yes (exercisable during test execution; currently skips the test on `ImportError`)

---
