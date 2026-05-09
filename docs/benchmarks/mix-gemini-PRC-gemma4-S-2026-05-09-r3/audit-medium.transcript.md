# Consultation transcript — csl-2026-05-09-1035-b617

_Question_: Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python.


## Planner

1. Search git history for commit `4e67dc2` to identify the specific code pattern used for the conda-env subprocess fallback.
2. Grep the repository for all occurrences of `import psycopg`, `import psycopg2`, and `from psycopg` to locate current import sites.
3. Compare the identified import sites against the fallback pattern found in step 1, excluding files that already implement the fix.
4. Inspect `pyproject.toml`, `setup.py`, or `requirements.txt` to identify external entry points and dependency requirements for the affected files.
5. Trace the call graph for each unpatched import site to determine if it is reachable via scripts or modules likely to be executed in a standard (non-conda) Python environment.
6. Generate a final list of `file:line` coordinates for unpatched imports with a boolean flag for non-conda reachability.

---

## Researcher (round 1)

Psycopg import sites lacking the `4e67dc2` conda-env subprocess fallback:

*   **`claude_hooks/providers/pgvector.py:123`**: `import psycopg` inside `PgvectorProvider.verify`. Catches `ImportError` and returns `False` but provides no fallback.
    *   *Exercisable:* Yes, when `verify` is called in-process (e.g., during hook registration).
*   **`claude_hooks/providers/pgvector.py:328`**: `import psycopg` inside `PgvectorProvider._ensure_ready`. Catches `ImportError` and re-raises with an install hint but provides no fallback.
    *   *Exercisable:* Yes, when any `PgvectorProvider` operation (recall/store) is triggered in-process.
*   **`scripts/migrate_to_pgvector.py:624`**: Bare `import psycopg` inside `main()`.
    *   *Exercisable:* Yes, whenever this migration script is executed directly.
*   **`scripts/bench_recall.py:107`**: `import psycopg` inside `PgvectorBenchProvider.__init__`.
    *   *Exercisable:* Yes, whenever this benchmark script is executed directly.
*   **`tests/test_pgvector_integration.py:56`**: `import psycopg` inside `_skip_if_no_deps`. Catches `ImportError` and raises `unittest.SkipTest`.
    *   *Exercisable:* Yes, during test suite execution.
*   **`tests/test_pgvector_integration.py:285`**: `import psycopg` inside `tearDownClass`. Wrapped in broad `except Exception: pass`.
    *   *Exercisable:* Yes, during test suite cleanup.

**Pattern used in `install.py` for comparison:**
*   `install.py:2127`, `install.py:2524`, `install.py:2647`: These sites *already* implement the fallback using `find_conda_env_python()` and `subprocess.run` to execute a small script in the target environment where `psycopg` is expected to be installed.

---

## Researcher (round 1)

### Psycopg Import Sites Lacking Conda-Env Fallback

*   **`claude_hooks/providers/pgvector.py:123`**: `import psycopg` deferred inside `PgvectorProvider.verify()`.
    *   **Exercisable:** Yes. Returns `False` on `ImportError` without attempting a subprocess fallback. Called by `install.py` and potentially external consumers to check backend health.
*   **`claude_hooks/providers/pgvector.py:328`**: `import psycopg` deferred inside `PgvectorProvider._ensure_ready()`.
    *   **Exercisable:** Yes. Catches `ImportError` and re-raises it with a "pip install" message. Hard-fails during RAG operations (recall/store) if the active Python lacks the library, even if the conda env has it.
*   **`scripts/migrate_to_pgvector.py:624`**: Bare `import psycopg` inside `main()`.
    *   **Exercisable:** Yes. The script crashes immediately with `ModuleNotFoundError` when run from a non-conda Python (e.g., system `/usr/bin/python3`).
*   **`scripts/bench_recall.py:107`**: Bare `import psycopg` inside `PgvectorBenchProvider.__init__`.
    *   **Exercisable:** Yes. Benchmarking pgvector paths will crash with `ModuleNotFoundError` if the calling environment lacks the dependency.
*   **`tests/test_pgvector_integration.py:56`**: `import psycopg` inside `_skip_if_no_deps()`.
    *   **Exercisable:** Yes. Causes `pytest` to skip pgvector integration tests instead of using the available conda-env installation.
*   **`tests/test_pgvector_integration.py:285`**: `import psycopg` inside `tearDownClass()`.
    *   **Exercisable:** Yes. Swallows the exception via `except Exception: pass`, but results in failed cleanup of test tables if the test runner lacks `psycopg`.

### Summary of Sites with Implementation (for Reference)
The fallback pattern (checking `ImportError` then calling `find_conda_env_python()`) is correctly implemented in **`install.py`** at lines **2127**, **2524**, and **2647**. All other identified sites in the project lack this logic.

---

## Researcher (round 1)

### Findings
*   `claude_hooks/providers/pgvector.py:123` — **Exercisable: Yes.** Deferred `import psycopg` inside `PgvectorProvider.verify()`. While `install.py` provides a fallback when calling this method during setup, other components (like the agent loop or CLI) calling `verify()` directly from a non-conda environment will encounter a standard `ImportError` without the subprocess fallback.
*   `claude_hooks/providers/pgvector.py:328` — **Exercisable: Yes.** Deferred `import psycopg` inside `PgvectorProvider._ensure_ready()`. This is called during the first vector operation. It lacks the subprocess fallback, meaning the active Python interpreter MUST have `psycopg` installed to use pgvector storage.
*   `scripts/migrate_to_pgvector.py:624` — **Exercisable: Yes.** Bare `import psycopg` inside `main()`. Running this script directly from a system Python or a non-conda environment without `psycopg` will cause an immediate crash.
*   `scripts/bench_recall.py:107` — **Exercisable: Yes.** `import psycopg` inside `PgvectorBenchProvider.__init__`. Triggered when running recall benchmarks with the `pgvector` provider; lacks any environment-aware fallback.
*   `tests/test_pgvector_integration.py:56` — **Exercisable: Yes (Test context).** `import psycopg` inside `_skip_if_no_deps()`. Used by `pytest` to decide whether to skip integration tests; correctly uses `unittest.SkipTest` on failure but does not attempt a subprocess fallback.
*   `tests/test_pgvector_integration.py:285` — **Exercisable: Yes (Test context).** `import psycopg` inside `tearDownClass()`. Used for cleanup; lacks fallback logic.
*   `install.py:[2127, 2524, 2647]` — **Exercisable: No (Lacking).** These sites in `install.py` are the "fast-path" attempts that are immediately followed by the `4e67dc2` subprocess fallback logic (at lines 2138, 2539, and 2656 respectively), and thus are not "lacking" the protection.

---

## Synthesizer

Six import sites lack the conda-env subprocess fallback:

* `claude_hooks/providers/pgvector.py:123` — Exercisable: Yes
* `claude_hooks/providers/pgvector.py:328` — Exercisable: Yes
* `scripts/migrate_to_pgvector.py:624` — Exercisable: Yes
* `scripts/bench_recall.py:107` — Exercisable: Yes
* `tests/test_pgvector_integration.py:56` — Exercisable: Yes
* `tests/test_pgvector_integration.py:285` — Exercisable: Yes

---
