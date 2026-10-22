# Consultation transcript — csl-2026-10-22-2004-abd1

_Question_: Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python.


## Planner

1. Search for the string "conda" or "subprocess" in all files containing "psycopg" to identify the implemented pattern of the "conda-env subprocess fallback".
2. Inspect `install.py` for `import psycopg` sites and verify if they implement the identified fallback pattern.
3. Inspect `claude_hooks/providers/pgvector.py` at lines 123 and 328 to determine if the fallback is missing.
4. Inspect `scripts/migrate_to_pgvector.py:624` and `scripts/bench_recall.py:107` for the fallback pattern.
5. Inspect `tests/test_pgvector_integration.py` imports to see if they require the fallback (though tests are typically run in the environment).
6. Evaluate if each identified site is "exercisable from a non-conda-env Python" by checking if the surrounding function/class is called during standard execution paths.

---

## Researcher (round 1)

The "conda-env subprocess fallback" pattern consists of a `try...except ImportError` block where the `except` handler calls `find_conda_env_python()` and uses `subprocess.run` to execute a probe script using the conda environment's Python interpreter.

### 1. Implemented Fallback Sites
The following sites in `install.py` correctly implement this pattern:
- `install.py:2126` (`_verify_pgvector_dsn`): Fast path at `2127`, fallback to `subprocess.run` at `2146`.
- `install.py:2523` (`_pgvector_tables_present`): Fast path at `2524`, fallback to `subprocess.run` at `2557`.
- `install.py:2646` (`_init_pgvector_schema`): Fast path at `2647`, fallback to `subprocess.run` at `2673`.

### 2. Missing Fallback Sites
The following sites use `import psycopg` but **lack** the subprocess fallback, relying instead on a simple `ImportError` raise or log warning:

| File:Line | Context | Exercisable from non-conda Python? | Note |
| :--- | :--- | :--- | :--- |
| `claude_hooks/providers/pgvector.py:123` | `PgvectorProvider.verify` | **Yes** | Called by `dispatcher.py` / `detect.py` during provider verification. Returns `False` and logs a warning if missing. |
| `claude_hooks/providers/pgvector.py:328` | `PgvectorProvider._ensure_ready` | **Yes** | Called during any `recall` or `store` operation. Raises `ImportError` if missing. |
| `scripts/migrate_to_pgvector.py:624` | `migrate_to_pgvector.py` | **Yes** | CLI tool; will crash with `ImportError` if run outside the conda env. |
| `scripts/bench_recall.py:107` | `bench_recall.py` | **Yes** | Benchmark tool; will crash with `ImportError` if run outside the conda env. |

### Summary of Exercisability
The sites in `claude_hooks/providers/pgvector.py` are the most critical, as they are part of the runtime hook execution path. If a user runs the hooks using a system Python that lacks `psycopg`, the `PgvectorProvider` will either fail to verify (`:123`) or crash the recall/store pipeline (`:328`), whereas the `install.py` sites were specifically patched to avoid this during setup.

---

## Researcher (round 1)

The audit of `psycopg` import sites reveals that while the `install.py` logic has been updated with the conda-env subprocess fallback, several utility scripts and tests still use naked `import psycopg` calls.

### Identified Sites Lacking Fallback

| File:Line | Context | Exercisable from non-conda Python? |
| :--- | :--- | :--- |
| `scripts/migrate_to_pgvector.py:624` | Top-level of `main()` block | **Yes**. If run via `python3 scripts/migrate_to_pgvector.py` in a shell without `psycopg`, it will crash with `ImportError`. |
| `scripts/bench_recall.py:107` | Inside `PgvectorBenchProvider.__init__` | **Yes**. If the benchmark is run and the pgvector provider is instantiated, it will crash. |
| `tests/test_pgvector_integration.py:56` | Inside `_skip_if_no_deps()` | **No (effectively)**. The `ImportError` is caught at `57: except ImportError` and results in a `unittest.SkipTest`, which is the intended behavior for tests. |
| `tests/test_pgvector_integration.py:285` | Inside `tearDownClass()` | **No (effectively)**. Wrapped in a broad `try...except Exception` block at `293`, so it fails silently. |

### Summary of Findings
- **`install.py`**: Correctly implements the fallback pattern (verified in `install.py:2138`, `install.py:2516`, `install.py:2660`).
- **Critical Gaps**: `scripts/migrate_to_pgvector.py` and `scripts/bench_recall.py` are the only sites where a missing `psycopg` in the current interpreter will lead to an unhandled crash rather than a graceful fallback or skip.
- **Tests**: The integration tests are safe as they explicitly handle `ImportError` to skip tests.

---

## Synthesizer

Several critical sites in `claude_hooks/providers/pgvector.py` and utility scripts still lack the conda-env subprocess fallback, leading to potential runtime crashes when executed from a non-conda Python environment.

* `claude_hooks/providers/pgvector.py:123`: Yes (fails provider verification)
* `claude_hooks/providers/pgvector.py:328`: Yes (crashes during `recall` or `store` operations)
* `scripts/migrate_to_pgvector.py:624 [no store at this line; line is in main]`: Yes (crashes during CLI execution)
* `scripts/bench_recall.py:107`: Yes (crashes during provider instantiation)
* `tests/test_pgvector_integration.py:56`: No (handled via `unittest.SkipTest` at `tests/test_pgvector_integration.py:57`)
* `tests/test_pgvector_integration.py:285 [no SkipTest at this line; line is in tearDownClass]`: No (handled via broad exception block at `tests/test_pgvector_integration.py:293`)

---
