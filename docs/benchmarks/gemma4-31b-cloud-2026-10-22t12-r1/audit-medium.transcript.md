# Consultation transcript — csl-2026-10-22-1838-07c9

_Question_: Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python.


## Planner

1. Read `claude_hooks/providers/pgvector.py` around lines 123 and 328 to check for `try/except ImportError` and whether any subprocess fallback to a conda environment is implemented.
2. Read `scripts/migrate_to_pgvector.py` around line 624 to check for the same fallback pattern.
3. Read `scripts/bench_recall.py` around line 107 to determine if it's a verification utility or a runtime tool and check for the fallback.
4. Examine `tests/test_pgvector_integration.py` around lines 56 and 285 to see if they are integration tests that should simply skip (`unittest.SkipTest`) or if they are meant to test the fallback mechanism.
5. Review all other `psycopg` import sites in `install.py` (excluding `_verify_pgvector_dsn`) to ensure consistency with the 4e67dc2 pattern.
6. For each identified site lacking the fallback, trace its call graph to determine if it's exercisable from a non-conda-env Python (e.g., called by `install.py` or as a standalone script).

---

## Researcher (round 1)

The audit of `psycopg` import sites reveals that **none** of the target files implement a conda-env subprocess fallback. They all use a simple `try/except ImportError` (which merely logs or raises) or a naked `import psycopg`.

### Audit Results

| File:Line | Fallback Implemented? | Exercisable from non-conda Python? | Notes |
| :--- | :--- | :--- | :--- |
| `claude_hooks/providers/pgvector.py:123` | **No** | Yes | `verify()`: catches `ImportError` and returns `False` (`claude_hooks/providers/pgvector.py:124-126`). |
| `claude_hooks/providers/pgvector.py:328` | **No** | Yes | `_ensure_ready()`: catches `ImportError` and raises a new `ImportError` with a pip install hint (`claude_hooks/providers/pgvector.py:329-330`). |
| `scripts/migrate_to_pgvector.py:624` | **No** | Yes | Naked `import psycopg` inside `main()`. Will crash with `ModuleNotFoundError` if not installed in current env. |
| `scripts/bench_recall.py:107` | **No** | Yes | Naked `import psycopg` inside `PgvectorBenchProvider.__init__`. Will crash with `ModuleNotFoundError` if not installed in current env. |

### Summary of Findings
- **Missing Fallback**: No site uses a subprocess call to `conda run -n claude-hooks python -c "import psycopg"` or similar logic to bridge the environment gap.
- **Risk**: Both `scripts/migrate_to_pgvector.py` and `scripts/bench_recall.py` are highly susceptible to environment crashes when run from a system Python that lacks the `psycopg` binary.
- **Graceful Failure**: Only `claude_hooks/providers/pgvector.py` handles the missing dependency gracefully (by disabling the provider), but it does so by giving up rather than attempting to find the conda environment.

---

## Researcher (round 1)

The audit of `psycopg` import sites reveals that while the `install.py` tool has largely adopted the conda-env subprocess fallback pattern, several other utility scripts and the provider core still rely on direct imports.

### 1. Integration Tests Analysis
In `tests/test_pgvector_integration.py`, the imports at `tests/test_pgvector_integration.py:56` and `tests/test_pgvector_integration.py:285` are **not** meant to test the fallback mechanism. They are guarded by `_skip_if_no_deps()` (`tests/test_pgvector_integration.py:95`), which explicitly raises `unittest.SkipTest` if `psycopg` is missing. These tests are intended to run only in environments where the dependency is already present.

### 2. `install.py` Audit
The following sites in `install.py` were reviewed:
- **Verified Fallbacks**: `_verify_pgvector_dsn` (`install.py:2126`), `_pgvector_tables_present` (`install.py:2524`), and the internal verify scripts (`install.py:2546`, `install.py:2666`) correctly implement the "Fast path $\rightarrow$ Conda Fallback" pattern.
- **Missing Fallback**: `install.py:2092` (inside a block that appears to be an older or duplicate verify implementation) lacks the fallback and simply catches `ImportError` to return a failure reason (`install.py:2095`). However, based on the surrounding code, this site is likely superseded by the more robust `_verify_pgvector_dsn` used in the main setup flow.

### 3. Other Import Sites & Exercisability
The following files contain `psycopg` imports without the conda-env fallback:

| File | Line | Exercisable from non-conda Python? | Impact |
| :--- | :--- | :--- | :--- |
| `claude_hooks/providers/pgvector.py` | `123` | **Yes** | `verify()` returns `False` (graceful). |
| `claude_hooks/providers/pgvector.py` | `328` | **Yes** | `_ensure_ready()` raises `ImportError` $\rightarrow$ `recall()`/`store()` log warning and no-op/fail. |
| `scripts/migrate_to_pgvector.py` | `624` | **Yes** | Script crashes with `ImportError` immediately. |
| `scripts/bench_recall.py` | `107` | **Yes** | Script crashes with `ImportError` immediately. |

**Summary**:
- **`install.py`**: Consistent with 4e67dc2 in all primary paths.
- **Core Provider**: Uses lazy imports and `try/except` blocks, ensuring the main hook framework does not crash if `psycopg` is missing.
- **Standalone Scripts**: `scripts/migrate_to_pgvector.py` and `scripts/bench_recall.py` are the only sites where a non-conda Python execution will result in an unhandled `ImportError` crash. These are typically operator tools and thus have lower priority than the installer/hooks.

---

## Synthesizer

* `claude_hooks/providers/pgvector.py:123`: Yes (graceful failure via `ImportError` catch at `claude_hooks/providers/pgvector.py:124-126`)
* `claude_hooks/providers/pgvector.py:328`: Yes (raises `ImportError` at `claude_hooks/providers/pgvector.py:329-330`)
* `scripts/migrate_to_pgvector.py:624`: Yes (naked import; causes `ModuleNotFoundError` crash)
* `scripts/bench_recall.py:107`: Yes (naked import; causes `ModuleNotFoundError` crash)
* `install.py:2092`: Yes (graceful failure via `ImportError` catch at `install.py:2095`)

---
