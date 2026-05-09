# Consultation transcript — csl-2026-05-09-1009-ef89

_Question_: Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python.


## Planner

1. Search for commit `4e67dc2` or search code for "subprocess" and "psycopg" to define the specific implementation pattern of the conda-env fallback.
2. Search the codebase for all instances of `import psycopg2` and `import psycopg` using `grep`.
3. Compare each identified import site against the fallback pattern to isolate files missing the logic.
4. Inspect the call stack for each missing site to determine if the code is executed in a context where `psycopg` is not a guaranteed dependency (e.g., global hooks vs. internal conda environment).
5. Examine `setup.py`, `pyproject.toml`, or environment YAML files to verify which entry points are intended for non-conda usage.
6. Map the identified file:line sites to their specific entry points to assess the risk of import failure in non-conda environments.

---

## Researcher (round 1)

* `claude_hooks/providers/pgvector.py:123` — **Exercisable.** `PgvectorProvider.verify()` performs an in-process import. It catches `ImportError` but only logs a warning and returns `False` without attempting a subprocess fallback.
* `claude_hooks/providers/pgvector.py:328` — **Exercisable.** `PgvectorProvider._ensure_ready()` (invoked on every recall/store) catches `ImportError` and re-raises it as a "pip install" instruction, causing the hook to fail if the current interpreter lacks the dependency.
* `scripts/migrate_to_pgvector.py:624` — **Exercisable.** Bare `import psycopg` inside `main()` with no `try/except` block or conda-env search logic; running this script from a system Python will crash.
* `scripts/bench_recall.py:107` — **Exercisable.** Bare `import psycopg` inside `PgvectorBenchProvider.__init__`. The script will crash when attempting to benchmark pgvector if not executed within the conda environment.
* `tests/test_pgvector_integration.py:56` — **Exercisable.** Performs a direct import to decide whether to skip tests; while standard for a test suite, it lacks the fallback logic found in the installer.
* `tests/test_pgvector_integration.py:285` — **Exercisable.** Bare import in `tearDownClass`; will crash during test cleanup if dependencies are missing and the skip at line 56 was somehow bypassed.

---

## Researcher (round 1)

* `claude_hooks/providers/pgvector.py:123`: **Exercisable.** `PgvectorProvider.verify()` imports `psycopg` in-process and returns `False` on failure; no conda-env subprocess fallback.
* `claude_hooks/providers/pgvector.py:328`: **Exercisable.** `PgvectorProvider._ensure_ready()` (called by `recall` and `store`) imports `psycopg` in-process and re-raises `ImportError`; lacks fallback.
* `scripts/migrate_to_pgvector.py:624`: **Exercisable.** `main()` contains a bare `import psycopg` without error handling or fallback; will crash with `ModuleNotFoundError` if run outside the conda environment.
* `scripts/bench_recall.py:107`: **Exercisable.** `PgvectorBenchProvider.__init__` performs a direct in-process `import psycopg`; lacks fallback logic.
* `tests/test_pgvector_integration.py:56`: **Exercisable via pytest.** `_skip_if_no_deps()` performs in-process import to trigger `unittest.SkipTest`; does not attempt to reach the conda-env's `psycopg`.
* `tests/test_pgvector_integration.py:285`: **Exercisable via pytest.** `tearDownClass` imports `psycopg` in-process inside a broad `try/except Exception: pass` block; no fallback attempted.
* `install.py:2127`, `install.py:2524`, and `install.py:2647`: **Correctly implemented.** These sites successfully utilize the fallback pattern (e.g., `install.py:2140-2147`) which invokes `find_conda_env_python()` and `subprocess.run` when the local import fails.

---

## Researcher (round 1)

**Findings: Psycopg import sites lacking conda-env subprocess fallback**

*   `claude_hooks/providers/pgvector.py:123`: Lacks fallback; `PgvectorProvider.verify()` catches `ImportError` and returns `False`. Exercisable whenever the provider is discovered or validated.
*   `claude_hooks/providers/pgvector.py:328`: Lacks fallback; `PgvectorProvider._ensure_ready()` catches `ImportError` and re-raises with an installation suggestion. Exercisable when a pgvector-backed session is initialized.
*   `scripts/migrate_to_pgvector.py:624`: Lacks fallback; bare `import psycopg` inside `main()` will crash with `ModuleNotFoundError`. Exercisable when running the migration script directly from a non-conda environment.
*   `scripts/bench_recall.py:107`: Lacks fallback; bare `import psycopg` inside `PgvectorBenchProvider.__init__` will crash. Exercisable when running recall benchmarks.
*   `tests/test_pgvector_integration.py:56`: Lacks fallback; catches `ImportError` to raise `unittest.SkipTest`. Exercisable during test suite execution.
*   `tests/test_pgvector_integration.py:285`: Lacks fallback; `tearDownClass()` swallows the failure via a broad `except Exception`. Exercisable during test cleanup.

Note: `install.py` contains several `import psycopg` sites (lines 2127, 2524, 2647), but these already implement the `find_conda_env_python()` subprocess fallback as intended. String-wrapped imports (e.g., `install.py:2092`) are components of the fallback scripts themselves and are not considered "lacking."

---

## Synthesizer

* `claude_hooks/providers/pgvector.py:123`: Exercisable. `PgvectorProvider.verify()` returns `False` on `ImportError` without fallback.
* `claude_hooks/providers/pgvector.py:328`: Exercisable. `PgvectorProvider._ensure_ready()` re-raises `ImportError` as an installation instruction.
* `scripts/migrate_to_pgvector.py:624`: Exercisable. Bare `import psycopg` in `main()` results in `ModuleNotFoundError`.
* `scripts/bench_recall.py:107`: Exercisable. Bare `import psycopg` in `PgvectorBenchProvider.__init__` results in `ModuleNotFoundError`.
* `tests/test_pgvector_integration.py:56`: Exercisable. `_skip_if_no_deps()` catches `ImportError` to trigger `unittest.SkipTest`.
* `tests/test_pgvector_integration.py:285`: Exercisable. `tearDownClass()` imports `psycopg` within a broad `except Exception` block.

---
