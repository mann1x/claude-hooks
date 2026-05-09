# Consultation transcript — csl-2026-05-09-0615-2fb1

_Question_: Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python.


## Planner

1. Grep the repository for `import psycopg` and `from psycopg` to identify all files containing the dependency.
2. Locate the logic implemented in 4e67dc2 by searching for `conda` and `subprocess` keywords in files already known to contain the fallback.
3. Cross-reference the identified import sites with the presence of the fallback logic to list all non-compliant file:line instances.
4. Inspect entry points (e.g., `main` blocks, CLI scripts) associated with non-compliant files to determine if they are invoked outside of a managed conda environment.
5. Review `pyproject.toml` or setup files to verify if `psycopg` is marked as an optional dependency or required for all execution paths.
6. Check for any `try/except ImportError` blocks surrounding `psycopg` imports that might provide alternative (but non-conda) mitigations.

---

## Researcher (round 1)

The following sites lack the `conda-env` subprocess fallback and are exercisable from a non-conda-env Python:

*   `claude_hooks/providers/pgvector.py:123`: `PgvectorProvider.verify` catches `ImportError` and returns `False` without attempting a fallback. **Exercisable** during provider discovery/validation.
*   `claude_hooks/providers/pgvector.py:328`: `PgvectorProvider._ensure_ready` catches `ImportError` and re-raises it with a "pip install" suggestion. **Exercisable** during any `recall` or `store` operation using the pgvector provider.
*   `scripts/migrate_to_pgvector.py:624`: Bare `import psycopg` inside `main()`. **Exercisable** when running the migration script; will crash with `ModuleNotFoundError` if the dependency is missing in the current interpreter.
*   `scripts/bench_recall.py:107`: Bare `import psycopg` in `PgvectorBenchProvider.__init__`. **Exercisable** when running benchmarks; will crash if the dependency is missing.
*   `tests/test_pgvector_integration.py:56`: `import psycopg` inside `_skip_if_no_deps`. **Exercisable** during test runs; correctly triggers `unittest.SkipTest` but lacks fallback logic.
*   `tests/test_pgvector_integration.py:285`: `import psycopg` inside `tearDownClass`. **Exercisable** during test cleanup; silently fails due to a broad `except Exception: pass` block.

**Reference for implemented fallback logic:**
*   `install.py:2138-2147`: Correct implementation using `find_conda_env_python()` and `subprocess.run` to verify DSN when `psycopg` is missing in the caller.
*   `install.py:2539-2558`: Correct implementation for schema initialization fallback.
*   `install.py:2657-2674`: Correct implementation for pgvector setup fallback.

---

## Researcher (round 1)

- `claude_hooks/providers/pgvector.py:123`: `import psycopg` deferred inside `verify()` lacks the conda-env subprocess fallback; it only logs a warning and returns `False` if the import fails.
- `claude_hooks/providers/pgvector.py:328`: `import psycopg` inside `_ensure_ready()` lacks the fallback; it raises a bare `ImportError` if the package is missing, which is exercisable when the provider attempts to connect to a database.
- `scripts/migrate_to_pgvector.py:624`: Bare `import psycopg` inside the `main()` entry point lacks any fallback or guard; it is directly exercisable from a non-conda shell and will crash with `ModuleNotFoundError`.
- `scripts/bench_recall.py:107`: `import psycopg` inside `PgvectorBenchProvider.__init__` lacks the fallback; it is exercisable when running the recall benchmark CLI on a system lacking the package.
- `tests/test_pgvector_integration.py:56`: `import psycopg` in `_skip_if_no_deps()` lacks the fallback; while it correctly triggers `unittest.SkipTest`, it does not attempt to leverage the conda-env's interpreter.
- `tests/test_pgvector_integration.py:285`: `import psycopg` in `tearDownClass` lacks the fallback; it is exercisable during test cleanup and depends on the package being present in the test runner's environment.

---

## Researcher (round 1)

- `claude_hooks/providers/pgvector.py:123`: **Lacks fallback.** `PgvectorProvider.verify()` catches `ImportError` and returns `False` without attempting a subprocess call. **Exercisable** via health checks or provider validation in non-conda environments.
- `claude_hooks/providers/pgvector.py:328`: **Lacks fallback.** `PgvectorProvider._ensure_ready()` catches `ImportError` and re-raises it with an installation hint, skipping any conda fallback. **Exercisable** during any `recall()` or `store()` operation if the host Python lacks the library.
- `scripts/migrate_to_pgvector.py:624`: **Lacks fallback.** Unprotected `import psycopg` inside `main()`. **Exercisable** by direct script execution; crashes with `ModuleNotFoundError` if run outside the primary environment.
- `scripts/bench_recall.py:107`: **Lacks fallback.** Unprotected `import psycopg` inside `PgvectorBenchProvider.__init__`. **Exercisable** when running pgvector benchmarks in a non-conda Python.
- `tests/test_pgvector_integration.py:56`: **Lacks fallback.** Imports inside `_skip_if_no_deps()`; raises `unittest.SkipTest` on failure. **Exercisable** during test discovery/execution.
- `tests/test_pgvector_integration.py:285`: **Lacks fallback.** Imports inside `tearDownClass`; swallowed by broad `except Exception`. **Exercisable** at the end of the integration test suite.
- `install.py:2127`, `install.py:2524`, `install.py:2647`: **Verified fallback.** These sites correctly implement the 4e67dc2 pattern, attempting a local import before shelling out to `find_conda_env_python()`.

---

## Synthesizer

Six sites in `claude-hooks` lack the 4e67dc2 conda-env fallback, causing crashes or functional failures in non-conda environments.

*   `claude_hooks/providers/pgvector.py:123`: **Exercisable.** Returns `False` on `ImportError` without attempting a fallback. Affects provider discovery and health checks.
*   `claude_hooks/providers/pgvector.py:328`: **Exercisable.** Re-raises `ImportError` with a manual installation hint. Affects all `store` and `recall` operations using the pgvector provider.
*   `scripts/migrate_to_pgvector.py:624`: **Exercisable.** Bare import inside `main()`. Direct execution will crash with `ModuleNotFoundError` if the package is missing.
*   `scripts/bench_recall.py:107`: **Exercisable.** Bare import in `PgvectorBenchProvider.__init__`. Benchmarking CLI will crash if run outside the primary environment.
*   `tests/test_pgvector_integration.py:56`: **Exercisable.** Triggers `unittest.SkipTest` on failure. Correctly prevents crashes during discovery but ignores the conda interpreter fallback.
*   `tests/test_pgvector_integration.py:285`: **Exercisable.** Import in `tearDownClass` is caught by a broad `except Exception`. Silently fails to clean up database resources if the package is missing.

---
