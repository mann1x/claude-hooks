# Consultation transcript — csl-2026-10-27-1208-fa74

_Question_: Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python.


## Planner

1. Search for "conda" and "subprocess" across the codebase to identify the implementation pattern of the "conda-env subprocess fallback" introduced in 4e67dc2.
2. Examine `claude_hooks/providers/pgvector.py` at lines 123 and 328 to determine if the current `ImportError` handling implements this fallback or just raises a standard error.
3. Analyze `scripts/migrate_to_pgvector.py` around line 624 and `scripts/bench_recall.py` around line 107 for the same fallback pattern.
4. Audit `install.py` (lines 2092, 2127, 2524, 2546, 2647, 2666) to see if these imports are intended to be runtime-safe or are only for installation/setup logic.
5. Review `tests/test_pgvector_integration.py` (lines 56, 285) to check if test imports should be exempt from the fallback requirement.
6. For each identified site lacking the fallback, trace the call graph to determine if the code is reachable when running `claude-hooks` from a standard (non-conda) Python environment.

---

## Researcher (round 1)

The audit for `psycopg` import sites lacking the conda-env subprocess fallback reveals that the fallback is not implemented at any of the functional import sites in the codebase. 

Findings:
- `claude_hooks/providers/pgvector.py:123`: Lacks fallback. Implements a simple `try-except ImportError` that logs a warning and returns `False` (`claude_hooks/providers/pgvector.py:124-126`). Exercisable from non-conda Python during provider verification.
- `claude_hooks/providers/pgvector.py:328`: Lacks fallback. Raises a standard `ImportError` directing the user to `pip install psycopg[binary]` (`claude_hooks/providers/pgvector.py:329-330`). Exercisable from non-conda Python when the provider attempts to ensure readiness (`_ensure_ready`).
- `scripts/migrate_to_pgvector.py:624`: Lacks fallback. Direct `import psycopg` at the start of `main()`. Exercisable and will crash immediately if run from a non-conda Python without the library.
- `scripts/bench_recall.py:107`: Lacks fallback. Direct `import psycopg` inside `PgvectorBenchProvider.__init__`. Exercisable and will crash if the benchmark is run against pgvector from a non-conda Python.

**Summary Table:**

| File:Line | Fallback Present | Exercisable from non-conda Python |
| :--- | :--- | :--- |
| `claude_hooks/providers/pgvector.py:123` | No | Yes (returns `False`) |
| `claude_hooks/providers/pgvector.py:328` | No | Yes (raises `ImportError`) |
| `scripts/migrate_to_pgvector.py:624` | No | Yes (crashes) |
| `scripts/bench_recall.py:107` | No | Yes (crashes) |

---

## Researcher (round 1)

The audit of `psycopg` import sites reveals several locations that lack the conda-env subprocess fallback. While `install.py` has been largely updated, auxiliary scripts and the core provider still use direct imports that would fail in a non-conda environment.

### 1. `install.py` Audit
The planner's suggested lines have mostly been updated to include the fallback, but some internal probes still rely on the current interpreter:

*   **`install.py:2092`**: Inside `_PGVECTOR_VERIFY_SCRIPT`. This is a string literal used as a subprocess probe. It contains a `try/except ImportError` block (`install.py:2091-2096`), so it is runtime-safe; it reports a failure via JSON rather than crashing the installer.
*   **`install.py:2127`**: Part of the "Fast path" in `_verify_pgvector_dsn`. It is wrapped in a `try/except ImportError` (`install.py:2126-2136`), which then triggers the subprocess fallback at `install.py:2138`. **Safe.**
*   **`install.py:2524`**: Part of the "Fast path" in `_pgvector_tables_present`. Wrapped in `try/except ImportError` (`install.py:2523-2537`), triggering the fallback at `install.py:2539`. **Safe.**
*   **`install.py:2546`**: Inside the subprocess fallback script. Like `_PGVECTOR_VERIFY_SCRIPT`, it is a string literal executed by the conda-python; it is the *target* of the fallback, not a site needing one. **Safe.**
*   **`install.py:2647`**: Part of the "Fast path" in `_init_pgvector_schema`. Wrapped in `try/except ImportError` (`install.py:2646-2654`), triggering the fallback at `install.py:2656`. **Safe.**
*   **`install.py:2666`**: Inside the subprocess fallback script for schema init. **Safe.**

### 2. `tests/test_pgvector_integration.py` Audit
*   **`tests/test_pgvector_integration.py:56`**: Inside `_skip_if_no_deps()`. It uses a `try/except ImportError` to call `unittest.SkipTest` (`tests/test_pgvector_integration.py:55-58`). Since this is a test, skipping is the correct behavior. **Exempt.**
*   **`tests/test_pgvector_integration.py:285`**: Inside `tearDownClass`. Wrapped in `try/except Exception` (`tests/test_pgvector_integration.py:284-294`). While it lacks the conda fallback, a failure here just prevents cleanup of test tables. **Exempt.**

### 3. Identified Sites Lacking Fallback
The following files perform direct imports of `psycopg` without a subprocess fallback. These are **exercisable** from a non-conda environment if the user attempts to run these specific scripts:

| File:Line | Context | Exercisable? | Risk |
| :--- | :--- | :--- | :--- |
| `scripts/migrate_to_pgvector.py:624` | `main()` entry point | **Yes** | Crashes with `ImportError` if run with system Python. |
| `scripts/bench_recall.py:107` | `PgvectorBenchProvider.__init__` | **Yes** | Crashes with `ImportError` if run with system Python. |
| `claude_hooks/providers/pgvector.py:328` | `PgvectorProvider._ensure_ready` | **Yes** | Raises `ImportError` (`pgvector.py:330`). While caught by the dispatcher, it prevents the provider from functioning. |
| `claude_hooks/providers/pgvector.py:123` | `PgvectorProvider.verify` | **Yes** | Returns `False` via `ImportError` catch (`pgvector.py:124-126`). Safe, but no fallback. |

**Summary of Reachability**:
The `scripts/` hits are high-risk as they are standalone CLI tools. The `providers/pgvector.py` hits are "safe" in that they won't crash the main `claude-hooks` process (due to `try/except` blocks), but they strictly require the `psycopg` dependency to be present in the active interpreter to function, whereas the installer provides a way to verify them via subprocess.

---

## Synthesizer

* `scripts/migrate_to_pgvector.py:624`: Yes (Direct import in `main()` crashes the script if run from a non-conda environment).
* `scripts/bench_recall.py:107 [no main at this line; line is in __init__]`: Yes (Direct import in `PgvectorBenchProvider.__init__` crashes the script if run from a non-conda environment).
* `claude_hooks/providers/pgvector.py:328`: Yes (Raises `ImportError` at `claude_hooks/providers/pgvector.py:330` within `_ensure_ready` when run from a non-conda environment).
* `claude_hooks/providers/pgvector.py:123 [no _ensure_ready at this line; line is in verify]`: Yes (Caught by `try-except ImportError` at `claude_hooks/providers/pgvector.py:124-126` and returns `False` when run from a non-conda environment).

---
