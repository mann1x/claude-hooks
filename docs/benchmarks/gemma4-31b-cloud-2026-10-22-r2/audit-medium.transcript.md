# Consultation transcript — csl-2026-10-22-1248-478e

_Question_: Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python.


## Planner

1. Read `install.py:2120-2160` to identify the specific implementation pattern of the conda-env subprocess fallback.
2. Inspect `claude_hooks/providers/pgvector.py` around lines 123 and 328 to check for the absence of the fallback pattern.
3. Inspect `scripts/migrate_to_pgvector.py:620-635` to check for the absence of the fallback pattern.
4. Inspect `scripts/bench_recall.py:100-115` to check for the absence of the fallback pattern.
5. Verify entry points for `claude_hooks/providers/pgvector.py` (e.g., via `run.py` or `run_daemon.py`) to determine if these sites are exercisable from a non-conda environment.

---

## Researcher (round 1)

The audit of `psycopg` import sites identifies several locations that lack the conda-env subprocess fallback implemented in `install.py`.

### Fallback Pattern Analysis
The established pattern (from `install.py:2126-2160`) involves:
1. A `try: import psycopg` block.
2. An `except ImportError` block that shells out to the conda environment's Python executable via `subprocess.run` to perform the operation (e.g., verification) in an environment where `psycopg` is guaranteed to be installed.

### Audit Results

| File:Line | Status | Exercisability from non-conda Python |
| :--- | :--- | :--- |
| `claude_hooks/providers/pgvector.py:123` | **Missing Fallback** | **Exercisable**. Called by `PgvectorProvider.verify()`. If run in a system Python without `psycopg`, it logs a warning and returns `False`. |
| `claude_hooks/providers/pgvector.py:328` | **Missing Fallback** | **Exercisable**. Called by `_ensure_ready()`. If run in a system Python, it raises a hard `ImportError` with a "pip install" suggestion. |
| `scripts/migrate_to_pgvector.py:624` | **Missing Fallback** | **Exercisable**. Direct import in `main()`. If run in a system Python, it results in an immediate `ImportError` crash. |
| `scripts/bench_recall.py:107` | **Missing Fallback** | **Exercisable**. Nested import in a benchmark loop. Results in `ImportError` crash. |

**Summary:** While `install.py` uses the fallback to avoid "false negatives" during setup, the actual provider logic and migration scripts still rely on the current interpreter having `psycopg` installed, which will cause failures when executed from a non-conda environment.

---

## Researcher (round 1)

The audit for `psycopg` import sites reveals the following:

### Lacking Conda-Env Fallback
The following files contain direct `import psycopg` or `psycopg.connect` calls without the `subprocess` fallback logic implemented in `install.py`:

*   **`scripts/bench_recall.py:107`**: Direct `import psycopg` inside `PgvectorBenchProvider.__init__`.
*   **`scripts/migrate_to_pgvector.py:624`**: Direct `import psycopg` in a function (likely the main migration loop).
*   **`claude_hooks/providers/pgvector.py:123`**: `import psycopg` inside `verify()`. (Note: this is a provider's verify method, which is often called by `install.py`, but the provider itself does not implement the fallback).
*   **`claude_hooks/providers/pgvector.py:328`**: `import psycopg` inside `_ensure_ready()`.

### Exercisability from Non-Conda Environment
*   **`scripts/bench_recall.py:107`**: **Exercisable.** This is a standalone benchmark script. If run with a system Python lacking `psycopg`, it will raise a hard `ImportError`.
*   **`scripts/migrate_to_pgvector.py:624`**: **Exercisable.** This is an operator script for data migration. Running it without the correct environment causes a hard failure.
*   **`claude_hooks/providers/pgvector.py:123`**: **Exercisable (Indirectly).** While the `PgvectorProvider.verify` method is used by `install.py` (which *does* have a fallback), other tools or future integrations calling `verify` directly from a non-conda Python would encounter an `ImportError`.
*   **`claude_hooks/providers/pgvector.py:328`**: **Exercisable.** This is the core initialization path for the provider. If the `pgvector` provider is enabled in `config/claude-hooks.json` but the current Python interpreter lacks `psycopg`, it raises an `ImportError` (caught at `pgvector.py:149` and `pgvector.py:216`, which logs a warning but does not attempt a subprocess fallback).

---

## Synthesizer

* `claude_hooks/providers/pgvector.py:123`: Exercisable. Called by `PgvectorProvider.verify()`; fails if run in system Python without `psycopg`.
* `claude_hooks/providers/pgvector.py:328`: Exercisable. Called by `_ensure_ready()`; raises `ImportError` if the provider is enabled but the current interpreter lacks `psycopg`.
* `scripts/migrate_to_pgvector.py:624`: Exercisable. Direct import in `main()`; results in immediate crash if run in a non-conda environment.
* `scripts/bench_recall.py:107 [no main at this line; line is in __init__]`: Exercisable. Direct import in `PgvectorBenchProvider.__init__`; results in `ImportError` crash if run in a non-conda environment.

---
