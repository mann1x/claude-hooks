# Consultation transcript — csl-2026-10-22-1928-c863

_Question_: Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python.


## Planner

1. Examine `claude_hooks/providers/pgvector.py` (lines 123, 328) to verify if `import psycopg` is wrapped in a try-except block that invokes a conda-env subprocess fallback.
2. Examine `scripts/migrate_to_pgvector.py` (line 624) to verify if `import psycopg` is wrapped in a try-except block that invokes a conda-env subprocess fallback.
3. Examine `scripts/bench_recall.py` (line 107) to verify if `import psycopg` is wrapped in a try-except block that invokes a conda-env subprocess fallback.
4. Review `install.py` (lines 2092, 2524, 2647) to ensure all `psycopg` import sites there correctly implement the fallback pattern identified in lines 2126-2147.
5. For each site lacking the fallback, analyze the call graph or entry point (e.g., `run.py`, `bin/` scripts) to determine if the code is exercisable from a non-conda-env Python interpreter.

---

## Researcher (round 1)

The audit of `psycopg` import sites reveals several locations that lack the conda-env subprocess fallback mechanism.

### Missing Fallback Summary

| File | Line | Context | Exercisable from non-conda Python? |
| :--- | :--- | :--- | :--- |
| `claude_hooks/providers/pgvector.py` | 123 | `verify()` method | **Yes** (called by installer) |
| `claude_hooks/providers/pgvector.py` | 328 | `_ensure_ready()` method | **Yes** (called during recall/store) |
| `scripts/migrate_to_pgvector.py` | 624 | `main()` entry point | **Yes** (direct CLI execution) |
| `scripts/bench_recall.py` | 107 | `PgvectorBenchProvider.__init__` | **Yes** (direct CLI execution) |

### Detailed Findings

*   **`claude_hooks/providers/pgvector.py`**:
    *   Line 123: `import psycopg` is inside a `try...except ImportError` block, but it only logs a warning and returns `False`. It does **not** attempt a subprocess fallback to the conda environment.
    *   Line 328: `import psycopg` is inside a `try...except ImportError` block that raises a user-facing `ImportError` instructions. It does **not** attempt a subprocess fallback.
*   **`scripts/migrate_to_pgvector.py`**:
    *   Line 624: `import psycopg` is a raw import at the start of the `main` logic. If the current interpreter lacks `psycopg`, the script crashes immediately with a standard `ImportError`.
*   **`scripts/bench_recall.py`**:
    *   Line 107: `import psycopg` is a raw import inside the `PgvectorBenchProvider` constructor. If the current interpreter lacks `psycopg`, the script crashes immediately.

In contrast, `install.py` has successfully implemented this pattern (e.g., `install.py:2138`), which provides the evidence that these other sites are lagging.

---

## Researcher (round 1)

The audit for `psycopg` import sites lacking the conda-env subprocess fallback (the pattern implemented in `install.py:2126-2147`) is complete.

### 1. Verified Sites (Fallback Implemented)
The following sites in `install.py` correctly implement the "Fast path (import) $\rightarrow$ Fallback (subprocess via `find_conda_env_python()`)" pattern:
- `install.py:2126` (`_verify_pgvector_dsn`)
- `install.py:2523` (`_pgvector_tables_present`)
- `install.py:2646` (`_init_pgvector_schema`)

### 2. Missing Fallback Sites
The following sites import `psycopg` without a subprocess fallback.

| File:Line | Context | Exercisable from non-conda Python? | Notes |
| :--- | :--- | :--- | :--- |
| `install.py:2092` | `_PGVECTOR_VERIFY_SCRIPT` | **No** | This is a string literal used *inside* the subprocess fallback. It is intended to run in the conda env. |
| `claude_hooks/providers/pgvector.py:123` | `PgvectorProvider.verify` | **Yes** | Called by the dispatcher/installer. If run in a non-conda env, it simply logs a warning and returns `False` (`pgvector.py:125`). |
| `claude_hooks/providers/pgvector.py:328` | `PgvectorProvider._ensure_ready` | **Yes** | Core provider logic. If `psycopg` is missing, it raises `ImportError` (`pgvector.py:330`), which is caught by the dispatcher to prevent hook failure. |
| `scripts/migrate_to_pgvector.py:624` | `main()` | **Yes** | Standalone migration script. Will crash with `ImportError` if run outside the conda env. |
| `scripts/bench_recall.py:107` | `PgvectorBenchProvider.__init__` | **Yes** | Standalone benchmark script. Will crash with `ImportError` if run outside the conda env. |

### Summary of Findings
The "subprocess fallback" is specifically designed for `install.py` to prevent the installer from misleading users when the installer itself is run with a system Python that lacks `psycopg`, while the actual `claude-hooks` conda environment (where `psycopg` is installed) is healthy.

The sites in `claude_hooks/providers/` and `scripts/` do **not** need this fallback because:
1. **Providers**: They are intended to be executed by the `bin/claude-hook` shims, which already prefer the conda environment's Python. If they are somehow run in a non-conda env, they fail gracefully via `ImportError` $\rightarrow$ `log.warning`.
2. **Scripts**: These are operator tools intended to be run within the activated `claude-hooks` environment. A crash on `ImportError` is the expected behavior for a missing dependency in a CLI tool.

---

## Synthesizer

* `claude_hooks/providers/pgvector.py:123`: Yes (called by the installer/dispatcher).
* `claude_hooks/providers/pgvector.py:328`: Yes (called during recall/store operations).
* `scripts/migrate_to_pgvector.py:624`: Yes (direct CLI execution).
* `scripts/bench_recall.py:107`: Yes (direct CLI execution).

---
