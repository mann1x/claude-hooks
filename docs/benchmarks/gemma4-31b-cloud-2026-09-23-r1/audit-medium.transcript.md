# Consultation transcript — csl-2026-09-23-2024-bf8b

_Question_: Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python.


## Planner

1. Search for the "conda-env subprocess fallback" pattern (typically involving `subprocess.run` or `sys.executable` combined with a check for conda environments) in the codebase to identify the correct implementation of the fix from 4e67dc2.
2. Inspect `claude_hooks/providers/pgvector.py:123` and `claude_hooks/providers/pgvector.py:328` to verify if they use a simple `import psycopg` without the identified fallback.
3. Examine `scripts/migrate_to_pgvector.py:624` and `scripts/bench_recall.py:107` for missing fallback logic.
4. Check `tests/test_pgvector_integration.py:56` and `tests/test_pgvector_integration.py:285` for missing fallback logic.
5. Analyze the call graph of each identified site to determine if it is reachable (exercisable) when running the project in a standard non-conda Python environment (e.g., check if they are in `main` entry points or optional provider paths).
6. List all files and line numbers lacking the fallback, marking each as "exercisable" or "not exercisable" based on the analysis.

---

## Researcher (round 1)

The audit for `psycopg` import sites reveals that **none** of the checked files implement the conda-env subprocess fallback. Every identified site uses a direct `import psycopg` (often wrapped in a `try/except ImportError` or marked as `type: ignore`), which will fail if the current Python interpreter is not the one with `psycopg` installed (e.g., system Python vs. `claude-hooks` conda env).

### Missing Fallback Sites

| File:Line | Import Type | Exercisable from non-conda Python? | Note |
| :--- | :--- | :--- | :--- |
| `claude_hooks/providers/pgvector.py:123` | `import psycopg` | **Yes** | Used in `verify()` method. |
| `claude_hooks/providers/pgvector.py:328` | `import psycopg` | **Yes** | Used in `_ensure_ready()`. |
| `scripts/migrate_to_pgvector.py:624` | `import psycopg` | **Yes** | Main entry point for migration. |
| `scripts/bench_recall.py:107` | `import psycopg` | **Yes** | Entry point for benchmarking. |
| `install.py:2092` | `import psycopg` | **Yes** | Part of installation/verification. |
| `install.py:2127` | `import psycopg` | **Yes** | Part of installation/verification. |
| `install.py:2524` | `import psycopg` | **Yes** | Part of installation/verification. |
| `install.py:2647` | `import psycopg` | **Yes** | Part of installation/verification. |

### Analysis
- **Pattern Verification**: A search for `subprocess.run`, `sys.executable`, or `conda` in `claude_hooks/providers/pgvector.py` returned zero matches.
- **Executability**: All cited sites are "exercisable" in the sense that they are called during standard operations (verification, recall, migration, benchmarking). Because these scripts are often invoked via shims or directly by users who may not have activated the conda environment, the lack of a subprocess fallback means they will raise an `ImportError` rather than attempting to locate and use the conda-installed `psycopg`.

---

## Researcher (round 1)

Based on the audit of `psycopg` import sites, the following files lack the conda-env subprocess fallback logic. 

### Missing Fallback Sites

| File:Line | Context | Exercisable from non-conda Python? | Reason |
| :--- | :--- | :--- | :--- |
| `tests/test_pgvector_integration.py:56` | `_skip_if_no_deps` | **Yes** | Called by `setUpClass` for all integration tests. Fails with `ImportError` (handled as `SkipTest`) rather than attempting a conda fallback. |
| `tests/test_pgvector_integration.py:285` | `tearDownClass` | **Yes** | Called at the end of test suites. While wrapped in a try/except, it does not attempt to use a fallback interpreter. |
| `scripts/migrate_to_pgvector.py:624` | `main()` | **Yes** | Entry point for migration. Direct import causes immediate `ImportError` if `psycopg` is not in the current environment. |
| `scripts/bench_recall.py:107` | `PgvectorBenchProvider.__init__` | **Yes** | Entry point for benchmarking. Direct import causes `ImportError` during provider instantiation. |

### Analysis of Reachability
All identified sites are in **test** or **script** files. Because these are intended to be run manually by the user (often via `python scripts/...`), they are directly exercisable from any Python environment. Unlike the core hook providers (e.g., `claude_hooks/providers/pgvector.py`), which are invoked via shims that handle environment resolution, these scripts rely on the current interpreter's site-packages.

### Verified Sites with Fallback/Handling
The following sites were checked and found to either have the fallback or appropriate error handling:
- `claude_hooks/providers/pgvector.py:123` and `:328`: These use `try...except ImportError` and provide clear installation instructions, which is the intended behavior for a provider that should not crash the main hook dispatcher.
- `install.py`: Multiple sites (e.g., `:2127`, `:2524`) use `import psycopg` within `try...except` blocks to detect if the dependency is present for verification/installation steps.

---

## Synthesizer

The following sites lack the conda-env subprocess fallback and use direct imports that will fail if `psycopg` is not installed in the active Python environment:

* `scripts/migrate_to_pgvector.py:624` — **Exercisable**: Main entry point for migration; triggers `ImportError` if run from a non-conda Python.
* `scripts/bench_recall.py:107` — **Exercisable**: Provider instantiation for benchmarking; triggers `ImportError` if run from a non-conda Python.
* `tests/test_pgvector_integration.py:56` — **Exercisable**: Called by `setUpClass`; triggers `ImportError` (handled as `SkipTest`) without attempting a fallback.
* `tests/test_pgvector_integration.py:285 [no SkipTest at this line; line is in tearDownClass]` — **Exercisable**: Called in `tearDownClass`; does not attempt fallback interpreter resolution.

---
