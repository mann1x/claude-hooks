# Consultation transcript — csl-2026-05-07-0951-fe97

_Question_: Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python.


## Planner

1. Search codebase for the specific implementation pattern of the "conda-env subprocess fallback" introduced in commit `4e67dc2`.
2. Grep all occurrences of `import psycopg` and `import psycopg2` across the repository.
3. Compare all identified import sites against the fallback pattern to isolate those lacking the mechanism.
4. For each identified site, analyze the surrounding module and call hierarchy to determine if the code path is reachable in a non-conda-env Python execution.

---

## Researcher (round 1)

The audit identifies 6 `psycopg` import sites that lack the conda-env subprocess fallback introduced in `4e67dc2` (pattern verified in `install.py:2127-2147`, `install.py:2524-2540`, and `install.py:2647-2657`).

### Sites Lacking Conda Fallback

| File:Line | Context | Exercisable from non-conda Python? | Note |
| :--- | :--- | :--- | :--- |
| `claude_hooks/providers/pgvector.py:123` | `PgvectorProvider.verify()` | **Yes** | `try/except ImportError` returns `False` (no crash). |
| `claude_hooks/providers/pgvector.py:328` | `PgvectorProvider._ensure_ready()` | **Yes** | `try/except ImportError` re-raises a custom `ImportError` (crash). |
| `scripts/migrate_to_pgvector.py:624` | `main()` | **Yes** | Bare import in `main()`; crashes with `ModuleNotFoundError`. |
| `scripts/bench_recall.py:107` | `PgvectorBenchProvider.__init__` | **Yes** | Bare import in `__init__`; crashes with `ModuleNotFoundError`. |
| `tests/test_pgvector_integration.py:56` | `_skip_if_no_deps()` | **Yes** | `try/except ImportError` raises `unittest.SkipTest` (no crash). |
| `tests/test_pgvector_integration.py:285` | `tearDownClass()` | **Yes** | Broad `except Exception` swallows the failure (no crash). |

**Summary:** All listed sites are exercisable from a non-conda-env Python. While some use `try/except` to prevent a hard crash, they fail to utilize the `find_conda_env_python()` $\rightarrow$ `subprocess.run` pattern used in `install.py` to provide accurate reachability/functionality when the current interpreter lacks the dependency.

---

## Researcher (round 1)

The audit of `psycopg` import sites reveals that while `install.py` correctly implements the conda-env subprocess fallback for its probe logic, several other areas of the codebase still use bare `import psycopg` statements.

### Import Sites Lacking Conda-Env Fallback

| File:Line | Site Type | Exercisable from non-conda Python? | Note |
| :--- | :--- | :--- | :--- |
| `claude_hooks/providers/pgvector.py:123` | Method-local | **Yes** | `PgvectorProvider.verify()` catches `ImportError` and returns `False`, but does not attempt subprocess fallback. |
| `claude_hooks/providers/pgvector.py:328` | Method-local | **Yes** | `PgvectorProvider._ensure_ready()` catches `ImportError` and re-raises it; no subprocess fallback. |
| `scripts/migrate_to_pgvector.py:624` | `main()` local | **Yes** | Bare `import psycopg` in `main()`. Will crash with `ModuleNotFoundError` if run from any Python lacking the lib. |
| `scripts/bench_recall.py:107` | `__init__` local | **Yes** | Bare `import psycopg` in `PgvectorBenchProvider.__init__`. Will crash if the benchmark code path is executed. |
| `tests/test_pgvector_integration.py:56` | Function local | **Yes** | `_skip_if_no_deps()` imports in-process; raises `unittest.SkipTest` on `ImportError`, but no subprocess fallback. |
| `tests/test_pgvector_integration.py:285` | `tearDownClass` | **Yes** | `import psycopg` inside `tearDownClass`. Wrapped in a broad `except Exception: pass` which swallows errors, but no fallback. |

### Summary of Findings
- **Coverage**: The "fast path / subprocess fallback" pattern (seen in `install.py:2127`, `2524`, `2647`) is strictly limited to the installer's verification logic.
- **Risk**: All identified sites are "exercisable" in the sense that calling those specific methods or running those scripts from a vanilla Python environment will trigger the import. 
- **Behavior**: In `providers/pgvector.py`, the errors are handled gracefully (returning `False` or raising `ImportError`), whereas in `scripts/`, they result in hard crashes (`ModuleNotFoundError`).

---

## Researcher (round 1)

The following `psycopg` import sites lack the conda-env subprocess fallback (where the code shells out to the project's dedicated conda environment if the current interpreter lacks the library).

### 1. Core Provider Logic
*   `claude_hooks/providers/pgvector.py:123` — **Exercisable: Yes.** Called via `PgvectorProvider.verify()`. It uses a `try/except ImportError` block that simply returns `False`, which disables the provider for the session instead of attempting a conda fallback.
*   `claude_hooks/providers/pgvector.py:328` — **Exercisable: Yes.** Called via `PgvectorProvider._ensure_ready()`. It catches `ImportError` and re-raises a user-facing installation hint, but does not attempt to use the conda-env Python.

### 2. Utility Scripts
*   `scripts/migrate_to_pgvector.py:624` — **Exercisable: Yes.** A bare `import psycopg` inside `main()`. If run from a non-conda-env Python without `psycopg` installed, the script will crash immediately with `ModuleNotFoundError`.
*   `scripts/bench_recall.py:107` — **Exercisable: Yes.** A bare `import psycopg` inside `PgvectorBenchProvider.__init__`. If the pgvector benchmark path is exercised in a non-conda-env Python, it will crash with `ModuleNotFoundError`.

### 3. Tests
*   `tests/test_pgvector_integration.py:56` — **Exercisable: Yes (via pytest).** `_skip_if_no_deps()` imports `psycopg` and raises `unittest.SkipTest` on failure. While this prevents a crash, it does not attempt to verify if the dependency exists in the conda environment.
*   `tests/test_pgvector_integration.py:285` — **Exercisable: Yes (via pytest).** `tearDownClass` imports `psycopg` within a broad `except Exception: pass` block, swallowing the error but providing no fallback.

---

## Synthesizer

Six `psycopg` import sites lack the conda-env subprocess fallback; all are exercisable from a non-conda-env Python.

*   `claude_hooks/providers/pgvector.py:123`: Exercisable. Returns `False` on `ImportError` in `verify()`.
*   `claude_hooks/providers/pgvector.py:328`: Exercisable. Re-raises `ImportError` in `_ensure_ready()`.
*   `scripts/migrate_to_pgvector.py:624`: Exercisable. Bare import in `main()`; triggers `ModuleNotFoundError`.
*   `scripts/bench_recall.py:107`: Exercisable. Bare import in `PgvectorBenchProvider.__init__`; triggers `ModuleNotFoundError`.
*   `tests/test_pgvector_integration.py:56`: Exercisable. Raises `unittest.SkipTest` in `_skip_if_no_deps()`.
*   `tests/test_pgvector_integration.py:285`: Exercisable. Import in `tearDownClass()` is swallowed by a broad `except Exception` block.

---
