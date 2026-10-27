# Consultation transcript — csl-2026-10-27-1114-58b8

_Question_: Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python.


## Planner

1. Examine `install.py` around lines 2118 and 2516 to identify the specific implementation pattern of the "conda-env subprocess fallback" for `psycopg`.
2. Check `claude_hooks/providers/pgvector.py` at lines 123 and 328 to verify if `import psycopg` is wrapped in a try-except block that triggers the identified subprocess fallback.
3. Check `scripts/migrate_to_pgvector.py` at line 624 to verify if `import psycopg` implements the subprocess fallback.
4. Check `scripts/bench_recall.py` at line 107 to verify if `import psycopg` implements the subprocess fallback.
5. Check `tests/test_pgvector_integration.py` at lines 56 and 285 to verify if `import psycopg` implements the subprocess fallback.
6. For each site lacking the fallback, analyze the call graph or entry points to determine if the code is exercisable when the current Python interpreter does not have `psycopg` installed (e.g., if it's a core provider used by the daemon or a standalone script).

---

## Researcher (round 1)

Based on the audit of `psycopg` import sites, the "conda-env subprocess fallback" pattern—which consists of a `try: import psycopg` block followed by a `subprocess.run` call to a Python interpreter found via `find_conda_env_python()`—is inconsistently applied.

### Implementation Pattern (Baseline)
As seen in `install.py:2126-2148` and `install.py:2523-2540`, the required pattern is:
1. Attempt `import psycopg`.
2. On `ImportError`, call `find_conda_env_python()`.
3. Execute the required logic via `subprocess.run([str(conda_py), "-c", ...])`.

### Audit Results

| File:Line | Status | Exercisable from non-conda Python? | Note |
| :--- | :--- | :--- | :--- |
| `claude_hooks/providers/pgvector.py:123` | **Lacks Fallback** | Yes | `verify()` simply logs a warning and returns `False` on `ImportError`. While it doesn't crash, it provides a "false negative" for reachability if the user is running the provider in a non-conda env. |
| `claude_hooks/providers/pgvector.py:328` | **Lacks Fallback** | Yes | `_ensure_ready()` raises `ImportError` on failure. This will crash any hook invocation that attempts to use pgvector if the current interpreter lacks `psycopg`. |
| `scripts/migrate_to_pgvector.py:624` | **Lacks Fallback** | Yes | Bare `import psycopg` at the top level of `main()`. This script will crash immediately with `ImportError` if run from a non-conda Python environment. |

### Summary of Failures
The `install.py` logic correctly implements the fallback to ensure the installer doesn't misreport system state, but the actual provider logic and migration scripts still rely on the current interpreter having `psycopg` installed, violating the goal of supporting non-conda environment execution via subprocess delegation.

---

## Researcher (round 1)

The following `psycopg` import sites lack the conda-env subprocess fallback (which is implemented in `install.py` at lines 2127, 2524, 2647).

### Sites Lacking Subprocess Fallback

*   **`scripts/bench_recall.py:107`**: Direct `import psycopg` inside `PgvectorBenchProvider.__init__`.
    *   **Exercisable?** Yes. This is a standalone benchmark script. If run from a non-conda environment without `psycopg` installed, it will crash with an `ImportError`.
*   **`scripts/migrate_to_pgvector.py:624`**: Direct `import psycopg` in `main`.
    *   **Exercisable?** Yes. This is a standalone migration script. Running it outside a `psycopg`-equipped environment will cause an immediate crash.
*   **`tests/test_pgvector_integration.py:56`**: `import psycopg` inside `_skip_if_no_deps`.
    *   **Exercisable?** Yes, but handled. It catches `ImportError` and raises `unittest.SkipTest`, so it doesn't crash the test suite, but it does not attempt the subprocess fallback.
*   **`tests/test_pgvector_integration.py:285`**: `import psycopg` inside `tearDownClass`.
    *   **Exercisable?** Yes, but handled. It's wrapped in a broad `try...except Exception`, so it fails silently.
*   **`claude_hooks/providers/pgvector.py:123`**: `import psycopg` inside `Provider.verify`.
    *   **Exercisable?** Yes. This is called by the `install.py` dispatcher. While `install.py` has its own fallbacks, this specific provider method simply logs a warning and returns `False` on `ImportError` (`claude_hooks/providers/pgvector.py:125`).
*   **`claude_hooks/providers/pgvector.py:328`**: `import psycopg` inside `_ensure_ready`.
    *   **Exercisable?** Yes. This is the runtime path for the pgvector provider. If the provider is enabled in config but `psycopg` is missing in the current interpreter, it raises an `ImportError` (`claude_hooks/providers/pgvector.py:330`). This is a critical failure point for users running via the standard hooks if they aren't in the conda env.

---

## Synthesizer

The following `psycopg` import sites lack the conda-env subprocess fallback implemented in `install.py:2126-2148` and `install.py:2523-2540`:

* `claude_hooks/providers/pgvector.py:123`: Exercisable. Returns `False` and logs a warning upon `ImportError` (`claude_hooks/providers/pgvector.py:125`).
* `claude_hooks/providers/pgvector.py:328`: Exercisable. Raises `ImportError` (`claude_hooks/providers/pgvector.py:330`), causing runtime crashes if the provider is enabled but `psycopg` is missing.
* `scripts/migrate_to_pgvector.py:624`: Exercisable. Triggers an immediate `ImportError` crash when run from a non-conda environment.
* `scripts/bench_recall.py:107`: Exercisable. Triggers an immediate `ImportError` crash when run from a non-conda environment.
* `tests/test_pgvector_integration.py:56`: Exercisable. Catches `ImportError` to raise `unittest.SkipTest`, preventing a crash but bypassing the fallback.
* `tests/test_pgvector_integration.py:285 [no SkipTest at this line; line is in tearDownClass]`: Exercisable. Wrapped in a broad `try...except Exception` block, resulting in silent failure.

---
