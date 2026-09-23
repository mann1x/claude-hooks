# Consultation transcript — csl-2026-09-23-2049-4f6a

_Question_: Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python.


## Planner

1. Identify the pattern of the "conda-env subprocess fallback" implementation in `install.py` (lines 2118-2142, 2516-2546, 2632-2668) to use as the ground-truth reference.
2. Analyze `claude_hooks/providers/pgvector.py` (lines 123, 328) to determine if `psycopg` imports are wrapped in the subprocess fallback or just simple `try/except ImportError`.
3. Scan `scripts/migrate_to_pgvector.py` (line 624) and `scripts/bench_recall.py` (line 107) to check for missing fallbacks.
4. Evaluate if `claude_hooks/providers/pgvector.py` is exercisable from a non-conda-env Python (e.g., via `run.py` or `run_daemon.py` calling the provider).
5. Evaluate if the scripts in `scripts/` are intended to be run in a non-conda-env Python or are restricted to admin/conda environments.
6. Compile the list of `file:line` pairs lacking the fallback, categorized by exercisability.

---

## Researcher (round 1)

The "conda-env subprocess fallback" pattern involves a `try: import psycopg` fast-path, followed by a fallback that locates the conda environment's Python via `find_conda_env_python()` and executes the logic in a separate process using `subprocess.run`.

### Audit Results

| File | Line | Status | Exercisable from non-conda Python? |
| :--- | :--- | :--- | :--- |
| `install.py` | 2126 | **Implemented** | No (handled by fallback) |
| `install.py` | 2523 | **Implemented** | No (handled by fallback) |
| `install.py` | 2646 | **Implemented** | No (handled by fallback) |
| `claude_hooks/providers/pgvector.py` | 123 | **Missing** | **Yes**. `verify()` uses a simple `try/except ImportError` (124) and returns `False` without attempting a subprocess fallback. |
| `claude_hooks/providers/pgvector.py` | 328 | **Missing** | **Yes**. `_ensure_ready()` raises `ImportError` (330) if `psycopg` is missing, which is caught by handlers to return empty results or raise `RuntimeError`. |
| `scripts/migrate_to_pgvector.py` | 624 | **Missing** | **Yes**. `main()` performs a direct `import psycopg` (624) at the top level of the execution block. If missing, the script crashes with `ImportError`. |
| `scripts/bench_recall.py` | 107 | **Missing** | **Yes**. `PgvectorBenchProvider.__init__` performs a direct `import psycopg` (107). If missing, the script crashes with `ImportError`. |

### Summary of Findings
- **`install.py`** is fully compliant with the fallback pattern.
- **`claude_hooks/providers/pgvector.py`** lacks the fallback; however, it is primarily used within the hook loop where the dispatcher already runs in the conda environment, making the `ImportError` an expected failure state for disabled/misconfigured providers.
- **`scripts/migrate_to_pgvector.py`** and **`scripts/bench_recall.py`** are the most critical omissions. They are operator tools often run manually from a shell; if the user hasn't activated the conda env, these scripts fail immediately instead of leveraging the environment they are intended to manage/benchmark.

---

## Researcher (round 1)

The audit of `psycopg` import sites reveals several locations where the `conda-env` subprocess fallback (implemented in `install.py`) is missing. 

### 1. Missing Fallback Sites
The following files perform a direct `import psycopg` or call `psycopg.connect()` without a subprocess fallback mechanism. If run in a non-conda environment where `psycopg` is not installed, these will raise an `ImportError` or `ModuleNotFoundError`.

| File | Line(s) | Exercisability from non-conda Python |
| :--- | :--- | :--- |
| `claude_hooks/providers/pgvector.py` | 123, 328 | **High**. Exercisable via `run.py` or `run_daemon.py` if `pgvector` is enabled in `config/claude-hooks.json`. Note: `verify()` (line 123) and `_ensure_ready()` (line 328) use `try/except ImportError`, but they simply log a warning or raise `ImportError` rather than attempting the conda fallback. |
| `scripts/migrate_to_pgvector.py` | 624 | **Medium**. Exercisable if a user runs the migration script manually using their system Python instead of the conda environment. |
| `scripts/bench_recall.py` | 107 | **Medium**. Exercisable if a user runs the benchmark script using system Python. |

### 2. Exercisability Analysis

*   **`claude_hooks/providers/pgvector.py`**: This is the most critical site. While it uses `try/except ImportError` to avoid crashing the entire hook dispatcher (per `CLAUDE.md:435`), it does not attempt to leverage the conda environment if the current interpreter lacks the library. This means users who didn't activate the conda env (and aren't using the `bin/` shims that auto-detect it) will see `pgvector` as "unavailable" even if the conda env exists on disk.
*   **`scripts/*.py`**: These are intended as operator/admin tools. While typically run within the conda env, they are "exposed" to the system Python. The lack of fallback here results in a standard `ImportError`, which is generally acceptable for CLI tools but inconsistent with the "zero-ceremony" goal of the installer.
*   **`run.py` / `run_daemon.py`**: These are entry points that import the providers. They do not import `psycopg` directly but trigger the imports in `pgvector.py`. Since they are often called by Claude Code via paths defined in `settings.json`, they rely on the `bin/` shims (e.g., `bin/claude-hook`) to resolve the correct Python interpreter. If a user points `settings.json` directly to `run.py` using `/usr/bin/python3`, the missing fallback in the provider will cause `pgvector` to fail.

### 3. Summary of Findings
The `conda-env` subprocess fallback is currently localized to `install.py` (e.g., `install.py:2138`, `install.py:2660`) to verify connectivity during setup. It has **not** been ported to the runtime provider logic or the maintenance scripts.

---

## Synthesizer

The following sites perform `psycopg` imports without the conda-env subprocess fallback implemented in `install.py` (e.g., `install.py:2138`, `install.py:2660`):

*   **`claude_hooks/providers/pgvector.py:123`**: Exercisable. Used in `verify()`; returns `False` on `ImportError` (line 124) without attempting fallback.
*   **`claude_hooks/providers/pgvector.py:328`**: Exercisable. Used in `_ensure_ready()`; raises `ImportError` (line 330) without attempting fallback.
*   **`scripts/migrate_to_pgvector.py:624`**: Exercisable. Direct import in `main()`; crashes with `ImportError` if run from a non-conda Python.
*   **`scripts/bench_recall.py:107 [no main at this line; line is in __init__]`**: Exercisable. Direct import in `PgvectorBenchProvider.__init__`; crashes with `ImportError` if run from a non-conda Python.

---
