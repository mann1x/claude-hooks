# Consultation transcript — csl-2026-10-22-1236-347b

_Question_: Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python.


## Planner

1. Inspect `install.py` at lines 2092, 2524, and 2647 to check for `ImportError` handlers implementing the `find_conda_env_python` subprocess fallback.
2. Inspect `claude_hooks/providers/pgvector.py` at lines 123 and 328 to verify if these imports are wrapped in fallback logic or just simple `try/except` with warnings.
3. Inspect `scripts/migrate_to_pgvector.py:624` and `scripts/bench_recall.py:107` to see if they implement the fallback or assume the environment is correct.
4. Analyze the call graph for `claude_hooks/providers/pgvector.py` imports to determine if they are ever invoked by entry points (like `install.py` or CLI tools) that might be run from a non-conda Python.
5. Evaluate whether each identified site is "exercisable" from a non-conda Python (e.g., triggered by a user running a script/installer via system Python).

---

## Researcher (round 1)

The audit for `psycopg` import sites lacking the conda-env subprocess fallback (introduced in 4e67dc2) is complete.

### Missing Fallback Summary

| File | Line | Status | Exercisable from non-conda Python? |
| :--- | :--- | :--- | :--- |
| `install.py` | 2092 | **MISSING** | **Yes** (Used in `_PGVECTOR_VERIFY_SCRIPT` which is run as a string/subprocess, but the script itself just exits 0 on `ImportError` without trying a fallback). |
| `claude_hooks/providers/pgvector.py` | 123 | **MISSING** | **Yes** (Used in `verify()`; returns `False` with a warning if missing). |
| `claude_hooks/providers/pgvector.py` | 328 | **MISSING** | **Yes** (Used in `_ensure_ready()`; raises `ImportError` demanding `pip install psycopg[binary]`). |
| `scripts/migrate_to_pgvector.py` | 624 | **MISSING** | **Yes** (Direct import in `main()`; will crash with `ImportError` if not in current env). |
| `scripts/bench_recall.py` | 107 | **MISSING** | **Yes** (Direct import in `PgvectorBenchProvider.__init__`; will crash with `ImportError`). |

### Grounding Evidence

*   **`install.py:2092`**: The verification script `_PGVECTOR_VERIFY_SCRIPT` catches `ImportError` at line 2093 and simply prints a JSON failure and exits (`install.py:2096`). It does not implement the `find_conda_env_python` logic.
*   **`install.py:2524`** and **`install.py:2647`**: These sites **correctly implement** the fallback. Both catch `ImportError` and call `find_conda_env_python()` (`install.py:2540`, `install.py:2657`).
*   **`claude_hooks/providers/pgvector.py:123`**: Simple `try/except ImportError` returning `False` (`claude_hooks/providers/pgvector.py:126`).
*   **`claude_hooks/providers/pgvector.py:328`**: Simple `try/except ImportError` raising a descriptive `ImportError` (`claude_hooks/providers/pgvector.py:330`).
*   **`scripts/migrate_to_pgvector.py:624`**: Naked `import psycopg` inside `main()`.
*   **`scripts/bench_recall.py:107`**: Naked `import psycopg` inside `PgvectorBenchProvider.__init__`.

---

## Researcher (round 1)

The audit of `psycopg` import sites reveals several locations that lack the conda-env subprocess fallback. While `install.py` has been updated to use a "fast path / fallback" pattern (verified in `install.py:2125-2142` and `install.py:2645-2668`), several utility scripts and the provider itself still perform direct imports.

### 1. Unprotected Import Sites

| File:Line | Context | Exercisable from non-conda Python? |
| :--- | :--- | :--- |
| `claude_hooks/providers/pgvector.py:123` | Inside `verify()` method | **Yes**. Called by `install.py` via `Provider.verify()`. However, `install.py`'s wrapper handles the fallback, so this site is safe. |
| `claude_hooks/providers/pgvector.py:328` | Inside `_ensure_ready()` | **Yes**. This is the primary runtime entry for the hook. Since hooks are invoked via `bin/claude-hook` (which prefers the conda env), this is generally safe, but if a user runs the hook with a system Python, it will raise `ImportError` without a fallback. |
| `scripts/migrate_to_pgvector.py:624` | Top-level `main()` | **Yes**. This is a standalone migration script intended to be run by the user. Running `python scripts/migrate_to_pgvector.py` in a system shell without the conda env active will result in an immediate `ImportError`. |
| `scripts/bench_recall.py:107` | Inside `PgvectorBenchProvider.__init__` | **Yes**. A standalone benchmark script. Running `python scripts/bench_recall.py` without the conda env active will cause a crash. |

### 2. Analysis of "Exercisability"

- **`claude_hooks/providers/pgvector.py`**: The `_ensure_ready` import is triggered by any `recall` or `store` call. While the project's shell shims (`bin/claude-hook`) automate the use of the conda env, the code itself does not protect against being imported/run in a different environment.
- **`scripts/migrate_to_pgvector.py`** and **`scripts/bench_recall.py`**: These are the highest-risk sites. They are designed as CLI tools for the user. Users frequently run `python scripts/...` without manually activating a conda environment, making these imports directly exercisable and prone to failure on system Python.

### Summary of Findings
- **`install.py`**: Protected (Implemented fallback).
- **`claude_hooks/providers/pgvector.py`**: Unprotected (Relies on shim).
- **`scripts/migrate_to_pgvector.py:624`**: **Unprotected / Exercisable**.
- **`scripts/bench_recall.py:107`**: **Unprotected / Exercisable**.

---

## Synthesizer

Five `psycopg` import sites lack the conda-env subprocess fallback.

*   `install.py:2092`: Yes. Located within `_PGVECTOR_VERIFY_SCRIPT`; it catches `ImportError` and exits with a JSON failure without attempting a fallback.
*   `claude_hooks/providers/pgvector.py:123`: Yes. Located in `verify()`; returns `False` on `ImportError`. While often called by `install.py` wrappers that provide a fallback, the site itself is unprotected.
*   `claude_hooks/providers/pgvector.py:328`: Yes. Located in `_ensure_ready()`; raises a descriptive `ImportError` requiring manual installation.
*   `scripts/migrate_to_pgvector.py:624`: Yes. Direct import in `main()`; crashes on `ImportError` when run via system Python.
*   `scripts/bench_recall.py:107 [no main at this line; line is in __init__]`: Yes. Direct import in `PgvectorBenchProvider.__init__`; crashes on `ImportError` when run via system Python.

---
