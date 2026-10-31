# Consultation transcript — csl-2026-10-22-1940-0a19

_Question_: Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python.


## Planner

1. Search for "conda" and "subprocess" in `claude_hooks/` and `scripts/` to identify the pattern used for the conda-env fallback implementation.
2. Audit `claude_hooks/providers/pgvector.py` (lines 123, 328) to verify if the `ImportError` handlers implement the fallback or just raise a standard error.
3. Audit `scripts/migrate_to_pgvector.py` and `scripts/bench_recall.py` for `import psycopg` sites and check for fallback logic.
4. Audit `install.py` for `import psycopg` sites, specifically checking if these are used during installation/setup where a fallback would be appropriate.
5. Review `tests/test_pgvector_integration.py` to determine if its imports are part of the production path or strictly test-time (rendering the fallback unnecessary).
6. For each identified site lacking the fallback, determine if it's "exercisable from a non-conda-env Python" by tracing the call graph from entry points (`run.py`, `run_daemon.py`, `bin/*.sh`).

---

## Researcher (round 1)

The audit for `psycopg` import sites lacking the conda-env subprocess fallback (as implemented in `4e67dc2`) reveals that **no such fallback logic exists** in any of the audited files. All `psycopg` imports are currently standard, synchronous imports that will raise a hard `ImportError` if the package is missing from the current Python environment.

### Audit Results

| File:Line | Import Type | Fallback Logic? | Exercisable from non-conda Python? |
| :--- | :--- | :--- | :--- |
| `claude_hooks/providers/pgvector.py:123` | `import psycopg` | **No** (Logs warning and returns `False` in `verify`) | **Yes**. `verify` is called during installation/probing. |
| `claude_hooks/providers/pgvector.py:328` | `import psycopg` | **No** (Raises `ImportError` with a pip install hint) | **Yes**. `_ensure_ready` is called on every `recall` or `store` attempt. |
| `scripts/migrate_to_pgvector.py:624` | `import psycopg` | **No** (Standard import in `main`) | **Yes**. This is a standalone migration script. |
| `scripts/bench_recall.py:107` | `import psycopg` | **No** (Standard import in `PgvectorBenchProvider.__init__`) | **Yes**. This is a standalone benchmarking script. |

### Analysis
*   **`claude_hooks/providers/pgvector.py`**: The logic in `_ensure_ready` (`328-330`) explicitly raises an `ImportError` to notify the user to install the dependency via pip. It does not attempt to locate or spawn a subprocess using a conda environment.
*   **`scripts/`**: Both the migration and benchmark scripts perform bare imports. If run in a system Python without `psycopg` installed, they will crash immediately upon entering `main` or initializing the provider.
*   **Pattern Verification**: Grep results for `conda` and `subprocess` in `claude_hooks/` showed subprocess usage for detached tasks (e.g., `store_async.py`, `daemon_ctl.py`, `lsp_engine/`), but none of these implement a "proxy-import" or "fallback-execution" pattern for `psycopg`.

---

## Researcher (round 1)

The audit of `psycopg` import sites reveals that the conda-env subprocess fallback (introduced to avoid `ImportError` when `install.py` or scripts are run from a non-conda Python) has been implemented in the core installer but is missing from standalone scripts and benchmarks.

### 1. `install.py` Audit
The installer has correctly implemented the fallback pattern in all its critical `psycopg` interaction sites:
- **Verification**: `install.py:2127` attempts a local import, falling back to a subprocess call via `find_conda_env_python()` and the `_PGVECTOR_VERIFY_SCRIPT` (`install.py:2089`).
- **Schema Audit**: `install.py:2524` attempts local import, falling back to a subprocess (`install.py:2539`).
- **Schema Init**: `install.py:2647` attempts local import, falling back to a subprocess (`install.py:2657`).

**Verdict**: Fully covered.

### 2. `tests/test_pgvector_integration.py` Audit
- **Import sites**: `tests/test_pgvector_integration.py:56` and `tests/test_pgvector_integration.py:285`.
- **Context**: The `_skip_if_no_deps()` helper (`tests/test_pgvector_integration.py:54`) explicitly uses `unittest.SkipTest` if `psycopg` is missing.

**Verdict**: Fallback unnecessary. Tests are intended to be run within the `claude-hooks` conda env; skipping them is the correct behavior for a non-env Python.

### 3. Identified Sites Lacking Fallback
The following production-adjacent scripts lack the subprocess fallback. If run from a system Python without `psycopg` installed, these will crash with an `ImportError`.

| File:Line | Site | Exercisable from non-conda Python? | Impact |
| :--- | :--- | :--- | :--- |
| `scripts/migrate_to_pgvector.py:624` | `import psycopg` | **Yes** | High: Users running migration from system shell will crash. |
| `scripts/bench_recall.py:107` | `import psycopg` | **Yes** | Low: Benchmarks are usually run by devs in-env. |
| `claude_hooks/providers/pgvector.py:123` | `import psycopg` | **No** | None: This is inside `verify()`, which is called by the installer's fallback-protected wrapper. |
| `claude_hooks/providers/pgvector.py:328` | `import psycopg` | **No** | None: This is inside `_ensure_ready()`, which is only called by the provider during active recall/store operations (already running in the conda-managed daemon or shim). |

### Summary Recommendation
The subprocess fallback should be added to `scripts/migrate_to_pgvector.py` to ensure that users can perform migrations without manually activating the conda environment, mirroring the reliability of `install.py`.

---

## Synthesizer

* `scripts/migrate_to_pgvector.py:624`: Yes
* `scripts/bench_recall.py:107`: Yes
* `claude_hooks/providers/pgvector.py:123`: No (called via `install.py` fallback-protected wrapper)
* `claude_hooks/providers/pgvector.py:328`: No (called via conda-managed daemon or shim)

---
