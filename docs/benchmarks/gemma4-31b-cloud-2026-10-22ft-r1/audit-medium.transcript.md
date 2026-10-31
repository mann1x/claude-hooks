# Consultation transcript — csl-2026-10-22-1952-7e10

_Question_: Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python.


## Planner

1. Read `install.py` (lines 2100-2670) to identify the exact implementation pattern of the conda-env subprocess fallback for `psycopg`.
2. Inspect `claude_hooks/providers/pgvector.py` (lines 120-130 and 320-340) to verify if the identified fallback pattern is used or if it's a direct `import psycopg`.
3. Inspect `scripts/migrate_to_pgvector.py` (lines 620-630) to check for the fallback pattern.
4. Inspect `scripts/bench_recall.py` (lines 100-110) to check for the fallback pattern.
5. Determine if `claude_hooks/providers/pgvector.py` is imported by any main entry points (e.g., `run.py`, `run_daemon.py`) that could be executed from a non-conda environment.
6. Evaluate if the scripts in `scripts/` are intended to be run via the conda environment or if they are often called via absolute paths/different interpreters.

---

## Researcher (round 1)

The implementation pattern for the conda-env subprocess fallback for `psycopg` (as seen in `install.py:2126-2161`, `install.py:2523-2569`, and `install.py:2646-2670`) follows a "Fast Path / Fallback" structure:
1. **Fast Path**: Attempt `import psycopg` within a `try...except ImportError` block. If successful, execute the logic in the current interpreter.
2. **Fallback**: In the `ImportError` block, locate the conda environment python via `find_conda_env_python()`, and shell out using `subprocess.run` with a `-c` string (or stdin) to execute the logic in the environment where `psycopg` is guaranteed to be installed.

Audit of target files:

- **`claude_hooks/providers/pgvector.py`**: Lacks fallback.
    - `verify()` method (`claude_hooks/providers/pgvector.py:123-128`): Uses a `try...except ImportError` but only logs a warning (`log.warning("psycopg not installed...")`) and returns `False` without attempting a subprocess fallback.
    - `__init__` / connection logic (`claude_hooks/providers/pgvector.py:328-334`): Uses a `try...except ImportError` but explicitly raises a new `ImportError` with installation instructions instead of falling back.
    - **Exercisability**: Yes. This provider is invoked by the `dispatcher.py` during hook execution. If a user runs Claude Code via a system Python (non-conda), these calls will fail/warn instead of utilizing the installed conda environment.

- **`scripts/migrate_to_pgvector.py`**: Lacks fallback.
    - `migrate()` / connection logic (`scripts/migrate_to_pgvector.py:624-626`): Performs a direct `import psycopg` at the top level of the function/block without any `try...except` or subprocess logic.
    - **Exercisability**: Yes. This is a standalone CLI script. Running it with system Python will result in an immediate `ModuleNotFoundError`.

---

## Researcher (round 1)

The audit of `psycopg` import sites reveals several locations that lack the conda-env subprocess fallback. While the core `PgvectorProvider` is designed to fail gracefully via `ImportError` (non-blocking), several utility scripts and the system-wide MCP server will crash or fail to function when executed from a non-conda environment.

### 1. Sites lacking conda-env fallback

| File:Line | Context | Exercisable from non-conda Python? | Impact |
| :--- | :--- | :--- | :--- |
| `scripts/bench_recall.py:107` | `import psycopg` inside `PgvectorBenchProvider.__init__` | **Yes** | `ImportError` crashes the benchmark if `PgvectorBenchProvider` is instantiated. |
| `scripts/migrate_to_pgvector.py:624` | Top-level `import psycopg` in `main()` | **Yes** | `ImportError` crashes the migration script immediately upon execution. |
| `claude_hooks/pgvector_mcp/server.py:37` | Top-level `from claude_hooks.providers.pgvector import PgvectorProvider` | **Yes** | `ImportError` inside `PgvectorProvider` (if it had one) or the import itself could crash the MCP server at startup. |

### 2. Analysis of Entry Points

*   **`run.py` and `run_daemon.py`**: These are safe. They use `claude_hooks.dispatcher`, which lazily loads providers. `PgvectorProvider` itself uses a `try/except ImportError` block in `_ensure_ready` (`claude_hooks/providers/pgvector.py:328`), ensuring that the core hook flow never blocks or crashes if `psycopg` is missing.
*   **`bin/claude-hook-pgvector-mcp`**: This invokes `claude_hooks.pgvector_mcp.__main__`, which imports `server.py`. Since `server.py:37` performs a static import of `PgvectorProvider`, it is subject to any import-time failures in the provider. However, the provider's current structure avoids top-level `psycopg` imports, so the server will start but the provider will fail internally when `_ensure_ready` is called.
*   **`scripts/`**: These are intended for operator use. While the `bin/` shims prefer the conda environment, users calling `python scripts/migrate_to_pgvector.py` directly from a system Python will experience an immediate crash due to the static import at `scripts/migrate_to_pgvector.py:624`.

### 3. Comparison with Grounded Implementation
The correct fallback pattern (as seen in `install.py:2118`, `install.py:2515`, and `install.py:2632`) involves wrapping the `psycopg` check and shelling out to the conda environment's Python when the current interpreter lacks the dependency. This is entirely absent from the `scripts/` directory.

---

## Synthesizer

* `claude_hooks/providers/pgvector.py:123`: Yes (invoked via `dispatcher.py` during hook execution)
* `claude_hooks/providers/pgvector.py:328`: Yes (invoked via `dispatcher.py` during hook execution)
* `scripts/migrate_to_pgvector.py:624`: Yes (standalone CLI script)
* `scripts/bench_recall.py:107`: Yes (standalone CLI script)

---
