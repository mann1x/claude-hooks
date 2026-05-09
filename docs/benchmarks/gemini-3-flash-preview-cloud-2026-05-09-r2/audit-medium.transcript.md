# Consultation transcript — csl-2026-05-09-0730-d194

_Question_: Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python.


## Planner

1. Search the git log/diff for commit `4e67dc2` to extract the specific code pattern used for the "conda-env subprocess fallback".
2. Search all `.py` files for `import psycopg`, `import psycopg2`, or `from psycopg` to identify every import site.
3. Cross-reference the identified sites against the pattern from step 1, noting specific file:line locations that lack the fallback logic.
4. Analyze the execution context of each unguarded import (top-level vs. function-level) to determine if it triggers immediately upon module load.
5. Trace the call paths from project entry points (e.g., `src/`, CLI scripts, or hooks) to each unguarded site to verify if they are exercisable in a standard Python environment.

---

## Researcher (round 1)

### Audit of `psycopg` Import Sites Lacking Conda-Env Fallback

The following sites perform direct `import psycopg` calls without the `subprocess` fallback logic (probed via `find_conda_env_python`) implemented in `install.py`:

*   **`claude_hooks/providers/pgvector.py:123`**: `PgvectorProvider.verify` performs a deferred import inside a `try/except ImportError` block. It returns `False` if `psycopg` is missing. **Exercisable:** Yes; if called from an environment without `psycopg` (e.g., system Python), it will fail to verify the DSN even if the package exists in the `claude-hooks` conda env.
*   **`claude_hooks/providers/pgvector.py:328`**: `PgvectorProvider._ensure_ready` performs a deferred import and raises `ImportError` with a manual install instruction if missing. **Exercisable:** Yes; any runtime usage of the pgvector provider (recall/store) from system Python will crash with an `ImportError`.
*   **`scripts/migrate_to_pgvector.py:624`**: Bare `import psycopg` inside the `main()` function. **Exercisable:** Yes; executing the migration script directly using `python3` (system) will crash with `ModuleNotFoundError` before database connection attempts.
*   **`scripts/bench_recall.py:107`**: Bare `import psycopg` inside `PgvectorBenchProvider.__init__`. **Exercisable:** Yes; running the recall benchmark from a non-conda environment will crash when initializing the pgvector provider.
*   **`tests/test_pgvector_integration.py:285`**: Bare `import psycopg` inside `tearDownClass`. **Exercisable:** Yes; though `setUpClass` (line 95) uses a skip-guard, the `tearDownClass` lacks protection and would crash if the skip-guard were bypassed or if `unittest` reaches cleanup in certain failure modes.

**Note on `install.py`**: Lines `2127`, `2524`, and `2647` contain `import psycopg` calls that are **not** lacking the fallback; they represent the "fast path" within functions that explicitly implement the `find_conda_env_python()` subprocess fallback as a subsequent `except ImportError` step.

---

## Researcher (round 1)

- `claude_hooks/providers/pgvector.py:123`: Lacks conda-env fallback; inside `PgvectorProvider.verify()`, it catches `ImportError` and returns `False`. Exercisable from any Python process that triggers provider verification.
- `claude_hooks/providers/pgvector.py:328`: Lacks conda-env fallback; inside `PgvectorProvider._ensure_ready()`, it re-raises `ImportError`. Exercisable whenever the pgvector provider is instantiated and initialized.
- `scripts/migrate_to_pgvector.py:624`: Lacks conda-env fallback; bare `import` inside `main()`. Exercisable when running the migration script directly (will crash if `psycopg` is missing).
- `scripts/bench_recall.py:107`: Lacks conda-env fallback; bare `import` inside `PgvectorBenchProvider.__init__`. Exercisable when running recall benchmarks using the pgvector backend (will crash if `psycopg` is missing).
- `tests/test_pgvector_integration.py:56`: Lacks conda-env fallback; inside `_skip_if_no_deps()`, it raises `unittest.SkipTest` on `ImportError`. Exercisable during test runs.
- `tests/test_pgvector_integration.py:285`: Lacks conda-env fallback; inside `tearDownClass()`, a broad `except Exception` swallows any import failure. Exercisable during test suite cleanup.

---

## Researcher (round 1)

* `claude_hooks/providers/pgvector.py:123`: `import psycopg` inside `PgvectorProvider.verify()`. Lacks conda-env fallback; exercisable by any process invoking the `verify` method (e.g., via `claude-hook` CLI or internal agent loop) in a non-conda environment.
* `claude_hooks/providers/pgvector.py:328`: `import psycopg` inside `PgvectorProvider._ensure_ready()`. Lacks conda-env fallback; exercisable during provider initialization in a non-conda environment.
* `scripts/migrate_to_pgvector.py:624`: `import psycopg` inside `main()`. Lacks conda-env fallback; exercisable when running the migration script directly from a standard Python interpreter.
* `scripts/bench_recall.py:107`: `import psycopg` inside `PgvectorBenchProvider.__init__`. Lacks conda-env fallback; exercisable when executing benchmarks using the pgvector provider in a non-conda environment.
* `tests/test_pgvector_integration.py:56`: `import psycopg` inside `_skip_if_no_deps()`. Lacks conda-env fallback; exercisable when running tests in a non-conda environment (though used to trigger a skip).
* `tests/test_pgvector_integration.py:285`: `import psycopg` inside `tearDownClass`. Lacks conda-env fallback; exercisable during test cleanup in a non-conda environment.
* `install.py:2092`: `import psycopg` within a string literal (`_PGVECTOR_VERIFY_SCRIPT`) used for subprocess execution. This is the implementation *of* the fallback mechanism itself, not a site lacking it.
* `install.py:2127`, `2524`, `2647`: `import psycopg` sites that **already include** the conda-env subprocess fallback introduced in `4e67dc2`.

---

## Synthesizer

The following `psycopg` import sites lack the conda-env subprocess fallback introduced in `4e67dc2` and are exercisable from a non-conda-env Python:

*   **`claude_hooks/providers/pgvector.py:123`**: Exercisable via `PgvectorProvider.verify()`. It catches `ImportError` and returns `False` immediately, bypassing any attempt to locate `psycopg` in the `claude-hooks` conda environment.
*   **`claude_hooks/providers/pgvector.py:328`**: Exercisable via `PgvectorProvider._ensure_ready()`. It re-raises a bare `ImportError` if the package is missing from the current `sys.path`.
*   **`scripts/migrate_to_pgvector.py:624`**: Exercisable when running the migration script directly via a system interpreter. The `import` is inside `main()` and lacks any fallback logic.
*   **`scripts/bench_recall.py:107`**: Exercisable when executing recall benchmarks from a non-conda environment. The `import` is inside `PgvectorBenchProvider.__init__`.
*   **`tests/test_pgvector_integration.py:56`**: Exercisable during test discovery/execution. It uses `ImportError` to trigger `unittest.SkipTest` but does not check the conda environment first.
*   **`tests/test_pgvector_integration.py:285`**: Exercisable during `tearDownClass`. It performs a bare `import` inside a broad `except Exception` block which would swallow the failure but skip the intended cleanup.

Sites in `install.py` (`2127`, `2524`, `2647`) already implement the `find_conda_env_python()` fallback and are not listed here.

---
