# Consultation transcript — csl-2026-10-22-1259-0386

_Question_: Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python.


## Planner

1. Examine `claude_hooks/providers/pgvector.py` at lines 123 and 328 to see if `import psycopg` is wrapped in a try-except block with a `subprocess` fallback to a conda environment.
2. Examine `scripts/migrate_to_pgvector.py` at line 624 for the same fallback pattern.
3. Examine `scripts/bench_recall.py` at line 107 for the same fallback pattern.
4. Evaluate if `claude_hooks/providers/pgvector.py` is exercisable from a non-conda-env Python (e.g., by a client calling the provider).
5. Evaluate if `scripts/migrate_to_pgvector.py` and `scripts/bench_recall.py` are exercisable from a non-conda-env Python (e.g., via `python scripts/...`).
6. Compile the list of file:line where the fallback is missing and the exercisability status.

---

## Researcher (round 1)

The audit of `psycopg` import sites confirms that **none** of the targeted files implement the conda-env subprocess fallback. All three sites use direct `import psycopg` calls which will raise an `ImportError` if the package is not installed in the current Python environment.

### Audit Results

*   **`claude_hooks/providers/pgvector.py:123`**: Direct `import psycopg`.
    *   **Fallback**: None. Only a `try-except ImportError` that logs a warning and returns `False` in `verify()`.
    *   **Exercisable**: Yes. If a user enables `pgvector` without `pip install psycopg[binary]`, this site is hit during provider verification.
*   **`claude_hooks/providers/pgvector.py:328`**: Direct `import psycopg`.
    *   **Fallback**: None. Only a `try-except ImportError` that raises a `RuntimeError` (via `ImportError`) in `_ensure_ready()`.
    *   **Exercisable**: Yes. This is the primary entry point for all `pgvector` recall and store operations.
*   **`scripts/migrate_to_pgvector.py:624`**: Direct `import psycopg`.
    *   **Fallback**: None.
    *   **Exercisable**: Yes. This is the main migration script; it will crash immediately on `main()` if `psycopg` is missing.
*   **`scripts/bench_recall.py:107`**: Direct `import psycopg`.
    *   **Fallback**: None.
    *   **Exercisable**: Yes. The benchmark script will crash during `PgvectorBenchProvider` initialization if `psycopg` is missing.

---

## Researcher (round 1)

The audit of `psycopg` import sites reveals that several critical entry points still lack the conda-env subprocess fallback, which would cause them to fail in a non-conda-env Python interpreter.

### Missing Fallback Analysis

| File:Line | Import Style | Exercisable from non-conda Python? | Status |
| :--- | :--- | :--- | :--- |
| `claude_hooks/providers/pgvector.py:123` | `import psycopg` (inside `verify`) | **Yes** (via `Provider.verify`) | ❌ No fallback; returns `False` on `ImportError` but does not attempt subprocess. |
| `claude_hooks/providers/pgvector.py:328` | `import psycopg` (inside `_ensure_ready`) | **Yes** (via `recall`/`store`) | ❌ No fallback; raises `ImportError`. |
| `scripts/migrate_to_pgvector.py:624` | `import psycopg` (inside `main`) | **Yes** (via `python scripts/...`) | ❌ No fallback; raises `ImportError` immediately. |
| `scripts/bench_recall.py:107` | `import psycopg` (inside `PgvectorBenchProvider.__init__`) | **Yes** (via `python scripts/...`) | ❌ No fallback; raises `ImportError` immediately. |

### Grounding Evidence
- **`claude_hooks/providers/pgvector.py`**: The `verify` method (`:123`) and `_ensure_ready` method (`:328`) use standard `try/except ImportError` blocks. While they handle the error to prevent crashing the hook dispatcher, they do not implement the `subprocess` call to the conda environment's Python to verify/execute the operation, unlike the implementation found in `install.py` (`:2138`).
- **`scripts/migrate_to_pgvector.py`**: The `main` function performs a bare `import psycopg` at line `:624`. If the script is run via a system Python lacking `psycopg[binary]`, it will crash before any logic executes.
- **`scripts/bench_recall.py`**: The `PgvectorBenchProvider` constructor performs a bare `import psycopg` at line `:107`. Running this script in a non-conda environment will result in an immediate `ImportError`.

### Conclusion
The fallback mechanism added in `4e67dc2` (verified in `install.py`) has not been propagated to the actual provider implementation or the operator scripts. Consequently, any attempt to use `pgvector` features outside the dedicated conda environment will fail, even if the environment exists on the system.

---

## Synthesizer

* `claude_hooks/providers/pgvector.py:123`: Yes (hit during provider verification).
* `claude_hooks/providers/pgvector.py:328`: Yes (hit during recall and store operations).
* `scripts/migrate_to_pgvector.py:624`: Yes (hit when running the migration script).
* `scripts/bench_recall.py:107`: Yes (hit during `PgvectorBenchProvider` initialization).

---
