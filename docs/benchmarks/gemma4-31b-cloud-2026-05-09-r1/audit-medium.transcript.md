# Consultation transcript — csl-2026-05-09-0859-c570

_Question_: Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python.


## Planner

1. Analyze commit `4e67dc2` to identify the exact fallback implementation pattern or utility function used for `psycopg` imports.
2. Search the entire codebase for all occurrences of `import psycopg` and `import psycopg2`.
3. Compare each identified import site against the fallback pattern from step 1 to isolate those lacking the mechanism.
4. For each identified missing fallback, trace the call chain to determine if the code path is exercisable from a non-conda-env Python environment.

---

## Researcher (round 1)

The audit for `psycopg` import sites lacking the conda-env subprocess fallback pattern (as seen in `install.py:2126-2149`) is complete.

### Summary of Findings
The "fallback pattern" consists of a `try: import psycopg` (fast path) followed by a call to `find_conda_env_python()` and a `subprocess.run()` call if the import fails. 

The following sites lack this fallback and are **exercisable** from a non-conda-env Python (meaning they will either crash or return a failure without attempting to use the specialized environment).

| File:Line | Context | Exercisable? | Note |
| :--- | :--- | :--- | :--- |
| `claude_hooks/providers/pgvector.py:123` | `PgvectorProvider.verify()` | **Yes** | Catches `ImportError` and returns `False`. Correct for a probe, but doesn't use the conda env to verify reachability. |
| `claude_hooks/providers/pgvector.py:328` | `PgvectorProvider._ensure_ready()` | **Yes** | Catches `ImportError` and raises a user-facing `ImportError`. This is the primary runtime path for the provider. |
| `scripts/migrate_to_pgvector.py:624` | `main()` | **Yes** | Bare `import psycopg`. Will crash with `ModuleNotFoundError` if run from system Python. |
| `scripts/bench_recall.py:107` | `PgvectorBenchProvider.__init__` | **Yes** | Bare `import psycopg`. Will crash if benchmark is run from system Python. |
| `tests/test_pgvector_integration.py:56` | `_skip_if_no_deps()` | **Yes** | Catches `ImportError` to trigger `unittest.SkipTest`. Expected behavior for tests. |
| `tests/test_pgvector_integration.py:285` | `tearDownClass()` | **Yes** | Occurs inside a broad exception handler; effectively a no-op if import fails. |

### Conclusion
The most critical omissions are in `scripts/migrate_to_pgvector.py` and `scripts/bench_recall.py`, where a user running the script via `python3` (instead of the conda-aware shim) will face a hard crash despite the conda environment being present on the system. The `PgvectorProvider` imports are handled via `try/except`, preventing crashes but bypassing the "correct" environment for verification and initialization.

---

## Researcher (round 1)

The following `psycopg` import sites lack the conda-env subprocess fallback (which consists of a `try: import psycopg` fast-path followed by a `subprocess` probe of the `claude-hooks` conda environment if the import fails), as implemented in `install.py:2127-2142` and `install.py:2647-2668`.

| File:Line | Context | Exercisable from non-conda Python? | Note |
| :--- | :--- | :--- | :--- |
| `claude_hooks/providers/pgvector.py:123` | `PgvectorProvider.verify()` | **Yes** | In-process import; catches `ImportError` to return `False` but does not attempt conda fallback. |
| `claude_hooks/providers/pgvector.py:328` | `PgvectorProvider._ensure_ready()` | **Yes** | In-process import; catches `ImportError` and re-raises without attempting conda fallback. |
| `scripts/migrate_to_pgvector.py:624` | `main()` | **Yes** | Bare `import psycopg`; crashes with `ModuleNotFoundError` if missing. |
| `scripts/bench_recall.py:107` | `PgvectorBenchProvider.__init__` | **Yes** | Bare `import psycopg`; crashes with `ModuleNotFoundError` when pgvector benchmark is targeted. |
| `tests/test_pgvector_integration.py:56` | `_skip_if_no_deps()` | **Yes** | In-process import; raises `unittest.SkipTest` on `ImportError` without attempting conda fallback. |
| `tests/test_pgvector_integration.py:285` | `tearDownClass()` | **Yes** | In-process import; failure swallowed by broad `except Exception: pass` but no conda fallback attempted. |

---

## Researcher (round 1)

The following `psycopg` import sites lack the conda-env subprocess fallback (the pattern of attempting a local import and then shelling out to the `claude-hooks` conda environment if that fails).

### 1. Direct Crashes (Bare Imports)
These sites will raise a `ModuleNotFoundError` if executed from a Python environment without `psycopg` installed.

*   **`scripts/migrate_to_pgvector.py:624`**
    *   **Exercisable**: Yes. This is a standalone operator script.
    *   **Behavior**: Bare `import psycopg` inside `main()`.
*   **`scripts/bench_recall.py:107`**
    *   **Exercisable**: Yes. Standalone benchmarking script.
    *   **Behavior**: Bare `import psycopg` inside `PgvectorBenchProvider.__init__`.

### 2. Soft Failures (Gated Imports without Fallback)
These sites use `try/except ImportError` to prevent a crash, but they lack the subprocess fallback that would allow the operation to actually succeed by using the conda env's Python.

*   **`claude_hooks/providers/pgvector.py:123`**
    *   **Exercisable**: Yes. Triggered during provider verification.
    *   **Behavior**: `import psycopg` is wrapped in a `try/except` that logs a warning and returns `False`. It does not attempt to verify via the conda environment.
*   **`claude_hooks/providers/pgvector.py:328`**
    *   **Exercisable**: Yes. Triggered when the `PgvectorProvider` is initialized and `_ensure_ready()` is called.
    *   **Behavior**: `import psycopg` is wrapped in a `try/except` that raises a helpful `ImportError` prompting the user to install the package. It does not attempt to use the conda environment fallback.

### 3. Test Suite (Expected Behavior)
The following are not considered "missing" fallbacks as they are designed to skip tests if dependencies are missing.

*   **`tests/test_pgvector_integration.py:56`**: In `_skip_if_no_deps()`, it imports `psycopg` and raises `unittest.SkipTest` on failure.
*   **`tests/test_pgvector_integration.py:285`**: In `tearDownClass()`, the import is wrapped in a broad `try...except Exception` to ensure cleanup doesn't crash the test runner.

---

## Synthesizer

Six sites lack the conda-env subprocess fallback, all of which are exercisable from a non-conda-env Python.

* `scripts/migrate_to_pgvector.py:624`: Yes (Bare import; crashes with `ModuleNotFoundError`).
* `scripts/bench_recall.py:107`: Yes (Bare import; crashes with `ModuleNotFoundError`).
* `claude_hooks/providers/pgvector.py:123`: Yes (Catches `ImportError` and returns `False` without attempting fallback).
* `claude_hooks/providers/pgvector.py:328`: Yes (Catches `ImportError` and re-raises without attempting fallback).
* `tests/test_pgvector_integration.py:56`: Yes (Catches `ImportError` to trigger `unittest.SkipTest`).
* `tests/test_pgvector_integration.py:285`: Yes (Import failure swallowed by broad exception handler).

---
