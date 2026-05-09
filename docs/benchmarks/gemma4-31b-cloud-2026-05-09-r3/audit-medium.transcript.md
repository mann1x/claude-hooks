# Consultation transcript — csl-2026-05-09-0931-a145

_Question_: Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python.


## Planner

1. Inspect commit `4e67dc2` to extract the exact code pattern used for the `conda-env` subprocess fallback for `psycopg`.
2. Search all files for `import psycopg` or `from psycopg` to identify every instance of the library being loaded.
3. Compare each identified import site against the pattern from `4e67dc2` to flag those lacking the fallback.
4. Trace the call graph or import chain for each flagged site to determine if it is guarded by environment checks or isolated to conda-specific logic.
5. Verify if the flagged import sites are reachable in a non-conda Python environment by checking the containing module's entry points.

---

## Researcher (round 1)

The "conda-env subprocess fallback" pattern (introduced in `4e67dc2`) consists of a fast-path `try: import psycopg` followed by a fallback that locates the conda environment's Python via `find_conda_env_python()` and executes the logic via `subprocess.run`. This pattern is correctly implemented in `install.py` (e.g., `install.py:2126`, `install.py:2523`, `install.py:2646`).

The following sites still lack this fallback and will crash or fail if executed by a Python interpreter that does not have `psycopg` installed (e.g., system Python):

### Core Framework
*   **`claude_hooks/providers/pgvector.py:123`**: Inside `PgvectorProvider.verify()`.
    *   **Status**: Lacks fallback.
    *   **Exercisable**: **Yes**. Called during provider verification. It returns `False` on `ImportError`, effectively disabling the provider for that session without a subprocess attempt.
*   **`claude_hooks/providers/pgvector.py:328`**: Inside `PgvectorProvider._ensure_ready()`.
    *   **Status**: Lacks fallback.
    *   **Exercisable**: **Yes**. This is the primary guard for all `pgvector` operations (`recall`, `store`). If the hook is invoked via a non-conda Python, it will raise a hard `ImportError`.

### Operator Scripts
*   **`scripts/migrate_to_pgvector.py:624`**: Inside `main()`.
    *   **Status**: Lacks fallback.
    *   **Exercisable**: **Yes**. A bare `import psycopg` at the start of the main loop. Running this script with `python scripts/migrate_to_pgvector.py` outside the conda env results in an immediate `ModuleNotFoundError`.
*   **`scripts/bench_recall.py:107`**: Inside `PgvectorBenchProvider.__init__()`.
    *   **Status**: Lacks fallback.
    *   **Exercisable**: **Yes**. Bare import inside the constructor. Running the benchmark outside the conda env will crash upon instantiating the pgvector provider.

### Test Suite
*   **`tests/test_pgvector_integration.py:56`** and **`:285`**:
    *   **Status**: Lacks fallback.
    *   **Exercisable**: **Conditionally**. `line 56` is designed to `SkipTest` if deps are missing, which is the intended behavior for integration tests. `line 285` is in `tearDownClass` and is wrapped in a generic `try/except`.

---

## Researcher (round 1)

The following `psycopg` import sites lack the conda-env subprocess fallback (the pattern where the code attempts an in-process import, then falls back to shelling out to the conda-env Python if it fails, as seen in `install.py:2127`, `2524`, `2647`).

### Lacking Fallback & Exercisable

| File:Line | Context | Exercisable from non-conda Python? | Note |
| :--- | :--- | :--- | :--- |
| `claude_hooks/providers/pgvector.py:123` | `PgvectorProvider.verify()` | **Yes** | Returns `False` on `ImportError`, but does not attempt conda-env fallback. |
| `claude_hooks/providers/pgvector.py:328` | `PgvectorProvider._ensure_ready()` | **Yes** | Re-raises `ImportError` on failure; no conda-env fallback attempt. |
| `scripts/migrate_to_pgvector.py:624` | `main()` | **Yes** | Bare `import psycopg`; crashes with `ModuleNotFoundError` if missing. |
| `scripts/bench_recall.py:107` | `PgvectorBenchProvider.__init__` | **Yes** | Bare `import psycopg`; crashes with `ModuleNotFoundError` if the benchmark is run. |
| `tests/test_pgvector_integration.py:56` | `_skip_if_no_deps()` | **Yes** | Raises `unittest.SkipTest` on `ImportError`; no conda-env fallback. |
| `tests/test_pgvector_integration.py:285` | `tearDownClass()` | **Yes** | Broad `except Exception: pass` swallows errors, but no conda-env fallback attempted. |

---

## Researcher (round 1)

The audit of `psycopg` import sites reveals that several locations still lack the conda-env subprocess fallback introduced in commit `4e67dc2` (as seen in `install.py:2138`). While the core installer in `install.py` uses the fallback, other scripts and providers do not.

### 🚨 Missing Fallback (Exercisable from non-conda Python)

These sites will raise a `ModuleNotFoundError` if executed in a Python environment where `psycopg` is not installed, regardless of whether a `claude-hooks` conda environment exists on the system.

| File:Line | Location | Exercisability | Note |
| :--- | :--- | :--- | :--- |
| `scripts/migrate_to_pgvector.py:624` | `main()` | **High** | Direct `import psycopg` at the start of the main logic. Will crash immediately upon invocation without `psycopg`. |
| `scripts/bench_recall.py:107` | `PgvectorBenchProvider.__init__` | **Medium** | Occurs when instantiating the benchmark provider. If a user runs the benchmark with `--models` including pgvector, it will crash. |
| `claude_hooks/providers/pgvector.py:328` | `PgvectorProvider._ensure_ready()` | **High** | Called by `recall()` and `store()`. While wrapped in a `try/except ImportError` (line 327), it **only** raises a new `ImportError` (line 330) without attempting a conda fallback. |
| `claude_hooks/providers/pgvector.py:123` | `PgvectorProvider.verify()` | **High** | Wrapped in `try/except ImportError` (line 122). Returns `False` on failure (line 126) but does not attempt the conda-env fallback. |

### ✅ Guarded / Low Risk

| File:Line | Location | Risk | Note |
| :--- | :--- | :--- | :--- |
| `tests/test_pgvector_integration.py:56` | `_skip_if_no_deps()` | **Low** | Wrapped in `try/except ImportError` and raises `unittest.SkipTest`. Correct behavior for tests. |
| `tests/test_pgvector_integration.py:285` | `tearDownClass()` | **Low** | Wrapped in a broad `try/except Exception: pass` (line 293). |
| `install.py:2127` | `_verify_pgvector_dsn()` | **None** | Correctly implements the fast-path $\to$ conda-fallback pattern (`install.py:2138`). |

---

## Synthesizer

* `scripts/migrate_to_pgvector.py:624`: Yes. Bare import in `main()` causes immediate `ModuleNotFoundError`.
* `claude_hooks/providers/pgvector.py:328`: Yes. `_ensure_ready()` re-raises `ImportError` without attempting fallback; blocks `recall()` and `store()`.
* `claude_hooks/providers/pgvector.py:123`: Yes. `verify()` returns `False` on `ImportError` without attempting fallback.
* `scripts/bench_recall.py:107`: Yes. Bare import in `PgvectorBenchProvider.__init__` causes crash during instantiation.
* `tests/test_pgvector_integration.py:56`: Yes. Raises `unittest.SkipTest` on `ImportError`; expected behavior for tests.
* `tests/test_pgvector_integration.py:285`: Yes. `tearDownClass()` swallows failure via broad `except` block.

---
