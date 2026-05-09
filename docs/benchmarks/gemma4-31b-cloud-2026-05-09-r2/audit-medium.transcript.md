# Consultation transcript — csl-2026-05-09-0916-b65d

_Question_: Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python.


## Planner

1. Examine commit `4e67dc2` to identify the exact code pattern used for the `conda-env` subprocess fallback for `psycopg`.
2. Search the entire repository for all occurrences of `import psycopg` or `from psycopg` to locate all import sites.
3. Compare every identified `psycopg` import site against the pattern found in `4e67dc2` to list those lacking the fallback.
4. Trace the execution paths of each identified site to determine if the code is reachable in a standard non-conda Python environment.
5. Document each missing fallback instance as `file:line` and mark its exercisability based on the call graph analysis.

---

## Researcher (round 1)

The audit identifies **6 import sites** that lack the `conda-env` subprocess fallback. 

The `conda-env` fallback pattern (introduced in `4e67dc2`) is characterized by a "fast path" `try: import psycopg` followed by a "fallback" that invokes `find_conda_env_python()` and uses `subprocess.run` to execute a probe script in the dedicated conda environment (as seen in `install.py:2126-2147` and `install.py:2646-2676`).

### Sites Lacking Fallback

| File:Line | Context | Exercisable from non-conda Python? | Note |
| :--- | :--- | :--- | :--- |
| `claude_hooks/providers/pgvector.py:123` | `PgvectorProvider.verify()` | **Yes** | Wrapped in `try/except ImportError`, but simply logs a warning and returns `False` rather than attempting the conda fallback. |
| `claude_hooks/providers/pgvector.py:328` | `PgvectorProvider._ensure_ready()` | **Yes** | Wrapped in `try/except ImportError`, but raises a hard `ImportError` instructing the user to pip install. |
| `scripts/migrate_to_pgvector.py:624` | `main()` | **Yes** | **Bare import.** Will crash with `ModuleNotFoundError` immediately upon calling the script in a clean environment. |
| `scripts/bench_recall.py:107` | `PgvectorBenchProvider.__init__` | **Yes** | **Bare import.** Crashes if the specific provider is instantiated. |
| `tests/test_pgvector_integration.py:56` | `_skip_if_no_deps()` | **Yes** | Wrapped in `try/except ImportError` and raises `unittest.SkipTest`. This is intentional for test isolation. |
| `tests/test_pgvector_integration.py:285` | `tearDownClass()` | **Yes** | Wrapped in a broad `except Exception`, which silently swallows the failure to import/connect during cleanup. |

### Summary of Findings
- **`install.py`** is the only file implementing the full subprocess fallback.
- **Core Provider Logic** (`pgvector.py`) uses lazy imports with `ImportError` guards but does not implement the subprocess fallback, meaning it cannot utilize the conda env's `psycopg` installation if the current interpreter is not that env.
- **Utility Scripts** (`migrate_to_pgvector.py`, `bench_recall.py`) are the most vulnerable, containing bare imports that lead to crashes in non-conda environments.

---

## Researcher (round 1)

The audit for `psycopg` import sites lacking the conda-env subprocess fallback (introduced in commit `4e67dc2`) is complete.

### Findings: Import Sites Lacking Conda Fallback

The following sites perform a direct `import psycopg` without the `find_conda_env_python()` subprocess fallback.

| File:Line | Context / Method | Exercisable from non-conda Python? | Note |
| :--- | :--- | :--- | :--- |
| `claude_hooks/providers/pgvector.py:123` | `PgvectorProvider.verify()` | **Yes** | Wrapped in `try/except ImportError` $\rightarrow$ returns `False`. |
| `claude_hooks/providers/pgvector.py:328` | `PgvectorProvider._ensure_ready()` | **Yes** | Wrapped in `try/except ImportError` $\rightarrow$ re-raises. |
| `scripts/migrate_to_pgvector.py:624` | `main()` | **Yes** | **Crashes** (`ModuleNotFoundError`) if run directly. |
| `scripts/bench_recall.py:107` | `PgvectorBenchProvider.__init__` | **Yes** | **Crashes** (`ModuleNotFoundError`) if the pgvector bench path is hit. |
| `tests/test_pgvector_integration.py:56` | `_skip_if_no_deps()` | **Yes** | Returns `unittest.SkipTest` on `ImportError`. |
| `tests/test_pgvector_integration.py:285` | `tearDownClass()` | **Yes** | Wrapped in `except Exception: pass` $\rightarrow$ swallowed. |

### Analysis of Execution Paths

*   **`install.py` (Correctly Implemented):** The installer correctly implements the fallback pattern in `_verify_pgvector_dsn` (`install.py:2127`), `_pgvector_tables_present` (`install.py:2524`), and `_init_pgvector_schema` (`install.py:2647`), allowing the installation process to verify and initialize the DB even when the host interpreter lacks `psycopg`.
*   **Provider Logic:** The `PgvectorProvider` handles missing dependencies via `ImportError` guards. While it doesn't use the subprocess fallback, it prevents the main application from crashing, effectively disabling the provider.
*   **Scripts/Tests:** `scripts/migrate_to_pgvector.py` and `scripts/bench_recall.py` are the most exposed, as they are intended to be run as standalone tools and will crash if the user does not activate the `claude-hooks` conda environment first.

---

## Researcher (round 1)

The audit of `psycopg` import sites reveals that while the core installer (`install.py`) has correctly implemented the conda-env subprocess fallback, several support scripts and the provider implementation still perform direct in-process imports.

### Missing Fallback Instances

| File:Line | Context | Exercisable (Non-Conda Python) | Note |
| :--- | :--- | :--- | :--- |
| `claude_hooks/providers/pgvector.py:123` | `PgvectorProvider.verify()` | **Yes** | Wrapped in `try/except ImportError`, returns `False` but no fallback. |
| `claude_hooks/providers/pgvector.py:328` | `PgvectorProvider._ensure_ready()` | **Yes** | Wrapped in `try/except ImportError`, re-raises with installation hint. |
| `scripts/migrate_to_pgvector.py:624` | `main()` | **Yes** | Bare `import psycopg`; crashes with `ModuleNotFoundError`. |
| `scripts/bench_recall.py:107` | `PgvectorBenchProvider.__init__()` | **Yes** | Bare `import psycopg`; crashes with `ModuleNotFoundError`. |
| `tests/test_pgvector_integration.py:56` | `_skip_if_no_deps()` | **Yes** | Wrapped in `try/except ImportError`, raises `unittest.SkipTest`. |
| `tests/test_pgvector_integration.py:285` | `tearDownClass()` | **Yes** | Direct import; results are swallowed by a broad `except Exception: pass`. |

### Verification of Correct Implementations (for contrast)
The following sites in `install.py` **already have** the fallback (Fast path `try: import` $\rightarrow$ `find_conda_env_python()` $\rightarrow$ `subprocess.run`):
- `install.py:2127` (`_verify_pgvector_dsn`)
- `install.py:2524` (`_pgvector_tables_present`)
- `install.py:2647` (`_init_pgvector_schema`)

---

## Synthesizer

Six import sites lack the `conda-env` subprocess fallback; all are exercisable from a non-conda Python environment.

* `claude_hooks/providers/pgvector.py:123`: Yes (returns `False` via `ImportError` guard)
* `claude_hooks/providers/pgvector.py:328`: Yes (re-raises `ImportError` via guard)
* `scripts/migrate_to_pgvector.py:624`: Yes (bare import; causes `ModuleNotFoundError`)
* `scripts/bench_recall.py:107`: Yes (bare import; causes `ModuleNotFoundError`)
* `tests/test_pgvector_integration.py:56`: Yes (raises `unittest.SkipTest` via guard)
* `tests/test_pgvector_integration.py:285`: Yes (failure swallowed by `except Exception`)

---
