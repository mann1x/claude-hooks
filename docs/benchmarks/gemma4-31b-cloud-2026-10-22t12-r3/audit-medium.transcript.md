# Consultation transcript — csl-2026-10-22-1903-3cb5

_Question_: Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python.


## Planner

1. Read `install.py` around lines 2118-2142 and 2632-2668 to identify the exact implementation of the conda-env subprocess fallback.
2. Inspect `claude_hooks/providers/pgvector.py` around lines 123 and 328 to check for the fallback pattern.
3. Inspect `scripts/migrate_to_pgvector.py` around line 624 to check for the fallback pattern.
4. Inspect `scripts/bench_recall.py` around line 107 to check for the fallback pattern.
5. Analyze the call graph or entry points for these sites to determine if they are "exercisable" from a non-conda-env Python (i.e., can they be reached via `run.py` or other public interfaces without already being inside the conda env).

---

## Researcher (round 1)

The conda-env subprocess fallback (implemented in `install.py:2138-2142` and `install.py:2656-2668`) ensures that when `psycopg` is missing from the current interpreter, the tool shells out to the designated `claude-hooks` conda environment where the dependency is guaranteed to be present.

The following sites still lack this fallback:

*   **`claude_hooks/providers/pgvector.py:123`**: In `PgvectorProvider.verify()`, an `ImportError` simply returns `False` (`claude_hooks/providers/pgvector.py:126`). 
    *   **Exercisable**: Yes. This is called during provider detection/verification in the main hook loop if the user is running without the conda env.
*   **`claude_hooks/providers/pgvector.py:328`**: In `PgvectorProvider._ensure_ready()`, an `ImportError` raises a hard `ImportError` with installation instructions (`claude_hooks/providers/pgvector.py:330`).
    *   **Exercisable**: Yes. This is the primary connection path for any turn that actually attempts to recall from or store to pgvector.
*   **`scripts/migrate_to_pgvector.py:624`**: In `main()`, `import psycopg` is called at the top level of the function without any try/except or fallback logic.
    *   **Exercisable**: Yes. This is a standalone migration script; running it with system Python will cause a crash.

---

## Researcher (round 1)

The audit of `psycopg` import sites for the conda-env subprocess fallback (introduced in 4e67dc2) reveals several missing implementation sites. The "fallback pattern" involves attempting a local import and, upon `ImportError`, shelling out to the `claude-hooks` conda environment's Python interpreter.

### Missing Fallback Sites
The following files contain raw `import psycopg` calls without the subprocess fallback.

*   **`scripts/bench_recall.py:107`**: 
    *   **Site**: `import psycopg` inside `PgvectorBenchProvider.__init__`.
    *   **Exercisable?**: **Yes**. This is a standalone script. Running it from a non-conda environment will trigger a hard `ImportError` immediately upon instantiating `PgvectorBenchProvider`.
*   **`scripts/migrate_to_pgvector.py:624`**: 
    *   **Site**: `import psycopg` inside `main()`.
    *   **Exercisable?**: **Yes**. This is a standalone migration script. Running it from a non-conda environment will result in a hard `ImportError` before any migration logic executes.
*   **`claude_hooks/providers/pgvector.py:123`**: 
    *   **Site**: `import psycopg` inside `verify()`.
    *   **Exercisable?**: **Partial**. This is called by `install.py` and the dispatcher. While `install.py` (the primary caller) has its own fallback logic (e.g., `install.py:2138`), the provider's own `verify` method lacks it, meaning any other caller of `PgvectorProvider.verify` will face a hard `ImportError`.
*   **`claude_hooks/providers/pgvector.py:328`**: 
    *   **Site**: `import psycopg` inside `__init__`.
    *   **Exercisable?**: **Yes**. If the `PgvectorProvider` is instantiated in any context (e.g., via `claude_hooks/dispatcher.py`) where `psycopg` is missing from the current interpreter, it will raise `ImportError` (which is then caught by the dispatcher at `dispatcher.py`, but it doesn't attempt a subprocess fallback).

### Verified Fallback Sites
The following sites have successfully implemented the fallback pattern:
*   **`install.py`**: Implemented in `_verify_pgvector_dsn` (`install.py:2138`) and similar verification probes (`install.py:2660`). Verified by `tests/test_install_pgvector_verify.py`.

---

## Synthesizer

*   `claude_hooks/providers/pgvector.py:123`: Exercisable. Called during provider detection/verification in the main hook loop.
*   `claude_hooks/providers/pgvector.py:328`: Exercisable. Primary connection path for recall/store operations.
*   `scripts/migrate_to_pgvector.py:624`: Exercisable. Standalone migration script; crashes on system Python.
*   `scripts/bench_recall.py:107`: Exercisable. Standalone benchmarking script; crashes on system Python.

---
