# Consultation transcript — csl-2026-05-09-0805-017a

_Question_: Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python.


## Planner

1. Grep the repository for `import psycopg` and `from psycopg` to identify all current import sites.
2. Search for the specific fallback pattern introduced in `4e67dc2` (e.g., `subprocess.run(["conda", "run", ...])` or specialized try-except blocks) to establish the reference implementation.
3. Compare every `psycopg` import site against the reference implementation to identify files lacking the fallback logic.
4. Check the entry points and call stacks for each identified file to determine if the import is triggered during standard CLI or service execution.
5. Inspect `pyproject.toml`, `requirements.txt`, or `setup.py` to determine if `psycopg` is a required or optional dependency for the target execution environments.
6. Verify if the identified import sites are located within conditional blocks (e.g., `if TYPE_CHECKING`) that would prevent runtime failure in non-conda environments.

---

## Researcher (round 1)

### Psycopg Import Sites Lacking Conda-Env Fallback

*   **`claude_hooks/providers/pgvector.py:123`**: Deferred `import psycopg` inside `PgvectorProvider.verify()`. Uses `try/except ImportError` to return `False` but provides no subprocess fallback. **Exercisable** during provider verification/discovery.
*   **`claude_hooks/providers/pgvector.py:328`**: Deferred `import psycopg` inside `PgvectorProvider._ensure_ready()`. Re-raises `ImportError` on failure without attempting a conda-env fallback. **Exercisable** when the provider is used for active memory operations.
*   **`scripts/migrate_to_pgvector.py:624`**: Bare `import psycopg` inside `main()`. No guard or fallback; will crash with `ModuleNotFoundError` if run from a Python environment lacking the package. **Exercisable** via direct script execution.
*   **`scripts/bench_recall.py:107`**: Bare `import psycopg` inside `PgvectorBenchProvider.__init__`. Lacks guards or fallback logic. **Exercisable** when running pgvector benchmarks.
*   **`tests/test_pgvector_integration.py:56`**: `import psycopg` inside `_skip_if_no_deps()`. Catches `ImportError` to skip tests but does not check the conda environment. **Exercisable** during test suites.
*   **`tests/test_pgvector_integration.py:285`**: `import psycopg` inside `tearDownClass()`. Wrapped in a broad `except Exception: pass` which silently fails if the library is missing. **Exercisable** during test cleanup.

### Reference Fallback Pattern (for Audit Comparison)
The pattern introduced in `4e67dc2` (found in `install.py:2127`, `2524`, `2647`) uses a "Fast path" (in-process `import psycopg`) followed by an `except ImportError` block that invokes `find_conda_env_python()` and `subprocess.run()` to execute a probe script via the `claude-hooks` conda environment. None of the sites listed above implement this dual-path approach.

---

## Researcher (round 1)

Audit of `psycopg` import sites reveals the following locations still lacking the conda-env subprocess fallback introduced in `4e67dc2`:

*   `claude_hooks/providers/pgvector.py:123`: Lacks fallback. `PgvectorProvider.verify()` returns `False` on `ImportError`. **Exercisable** by any caller of provider verification (e.g., `install.py` fast-path or runtime discovery).
*   `claude_hooks/providers/pgvector.py:328`: Lacks fallback. `PgvectorProvider._ensure_ready()` re-raises `ImportError` with a manual install suggestion. **Exercisable** on the first database interaction if the current interpreter lacks the dependency.
*   `scripts/migrate_to_pgvector.py:624`: Lacks fallback. Bare `import psycopg` inside `main()`. **Exercisable** via direct script execution; will crash with `ModuleNotFoundError` if `psycopg` is missing.
*   `scripts/bench_recall.py:107`: Lacks fallback. Bare `import psycopg` inside `PgvectorBenchProvider.__init__`. **Exercisable** during pgvector benchmarking; will crash with `ModuleNotFoundError` if `psycopg` is missing.
*   `tests/test_pgvector_integration.py:56`: Lacks fallback. `_skip_if_no_deps()` raises `unittest.SkipTest` on `ImportError`. **Exercisable** during test runs; prevents the test suite from verifying connectivity via the conda-env fallback.
*   `tests/test_pgvector_integration.py:285`: Lacks fallback. `tearDownClass()` swallows all exceptions including `ImportError`. **Exercisable** during test cleanup; silently fails to drop test tables if `psycopg` is missing.

Reference implementations of the correct fallback (using `find_conda_env_python()`) are found in `install.py:2127`, `install.py:2524`, and `install.py:2647`.

---

## Researcher (round 1)

* `claude_hooks/providers/pgvector.py:123`: `import psycopg` inside `verify()` lacks the conda-env subprocess fallback; it returns `False` on `ImportError`. Exercisable from a non-conda environment when the installer or a client validates pgvector connectivity.
* `claude_hooks/providers/pgvector.py:328`: `import psycopg` inside `_ensure_ready()` lacks the fallback; it raises `ImportError` with a manual install instruction. Exercisable from a non-conda environment during any active recall or store operation.
* `scripts/migrate_to_pgvector.py:624`: Unconditional `import psycopg` inside `main()` lacks any guard or fallback. Exercisable (results in a crash) if the migration script is invoked directly from a shell where `psycopg` is not installed.
* `scripts/bench_recall.py:107`: Unconditional `import psycopg` inside `PgvectorBenchProvider.__init__` lacks any guard or fallback. Exercisable (results in a crash) if benchmarks are run from a non-conda environment.
* `tests/test_pgvector_integration.py:56`: `import psycopg` inside `_skip_if_no_deps()` lacks the fallback; it correctly raises `unittest.SkipTest`. Exercisable during test discovery/execution but behaves as expected for a test suite.
* `tests/test_pgvector_integration.py:285`: `import psycopg` inside `tearDownClass` lacks the fallback; it is wrapped in a broad `try/except` that suppresses errors. Exercisable during test cleanup.

---

## Synthesizer

Six `psycopg` import sites lack the conda-env subprocess fallback (as seen in `install.py:2127`, `install.py:2524`, and `install.py:2647`):

*   **`claude_hooks/providers/pgvector.py:123`**: Lacks fallback. Catches `ImportError` and returns `False` inside `verify()`. **Exercisable** during provider discovery/validation; prevents use even if the conda environment is configured correctly.
*   **`claude_hooks/providers/pgvector.py:328`**: Lacks fallback. Catches `ImportError` and re-raises it with manual install instructions inside `_ensure_ready()`. **Exercisable** during any database recall or store operation; results in a fatal error instead of falling back.
*   **`scripts/migrate_to_pgvector.py:624`**: Lacks fallback. Bare `import psycopg` in `main()`. **Exercisable** via direct script execution; will crash with `ModuleNotFoundError` if run outside the specific conda environment.
*   **`scripts/bench_recall.py:107`**: Lacks fallback. Bare `import psycopg` in `PgvectorBenchProvider.__init__`. **Exercisable** during benchmarking; will crash with `ModuleNotFoundError`.
*   **`tests/test_pgvector_integration.py:56`**: Lacks fallback. Catches `ImportError` to raise `unittest.SkipTest`. **Exercisable** during test execution; prevents the suite from verifying functionality via the conda-env bridge.
*   **`tests/test_pgvector_integration.py:285`**: Lacks fallback. Wrapped in a broad `try/except` that swallows the error in `tearDownClass()`. **Exercisable** during test cleanup; silently fails to drop test tables if the local interpreter lacks the package.

---
