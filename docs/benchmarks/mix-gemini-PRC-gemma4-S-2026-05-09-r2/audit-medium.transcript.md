# Consultation transcript — csl-2026-05-09-1020-7cd4

_Question_: Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python.


## Planner

1. Search for all instances of `import psycopg` and `from psycopg` across the repository to locate all potential import sites.
2. Inspect the changes in commit `4e67dc2` or look for a utility wrapper (likely in `claude_hooks/utils/`) that implements the subprocess fallback mechanism.
3. Compare the identified import sites against the fallback pattern to isolate files performing direct imports without the wrapper or equivalent logic.
4. Check `pyproject.toml` or `setup.py` for entry points that map to the files identified in step 3.
5. Analyze the call paths of the identified direct imports to determine if they are invoked during initialization or CLI execution outside of a managed conda environment.

---

## Researcher (round 1)

- `claude_hooks/providers/pgvector.py:123`: Lacks fallback in `PgvectorProvider.verify()`. It catches `ImportError` and returns `False` but does not attempt a subprocess call. **Exercisable:** Yes, whenever a provider's reachability is checked.
- `claude_hooks/providers/pgvector.py:328`: Lacks fallback in `PgvectorProvider._ensure_ready()`. It catches `ImportError` and re-raises with a "pip install" hint. **Exercisable:** Yes, triggered on every `recall()` or `store()` operation if the connection is not yet active.
- `scripts/migrate_to_pgvector.py:624`: Lacks fallback. Bare `import psycopg` inside `main()`. **Exercisable:** Yes, if the migration script is run directly from a non-conda environment.
- `scripts/bench_recall.py:107`: Lacks fallback. Bare `import psycopg` inside `PgvectorBenchProvider.__init__`. **Exercisable:** Yes, when running the recall benchmark against the pgvector provider.
- `tests/test_pgvector_integration.py:56`: Lacks fallback. Used in `_skip_if_no_deps()` to trigger `unittest.SkipTest`. **Exercisable:** Yes, during test discovery/execution.
- `tests/test_pgvector_integration.py:285`: Lacks fallback. Direct `import psycopg` in `tearDownClass()`. **Exercisable:** Yes, during test cleanup.

Note: `install.py` (lines 2127, 2524, 2647) correctly implements the fallback pattern by checking for local `psycopg` and falling back to `find_conda_env_python()` on failure (e.g., `install.py:2140`). The imports at `install.py:2092`, `2546`, and `2666` are inside raw strings intended for the subprocess itself and are not problematic.

---

## Researcher (round 1)

* `claude_hooks/providers/pgvector.py:123`: `import psycopg` inside `PgvectorProvider.verify()`. Lacks fallback; returns `False` on `ImportError`. Exercisable from any Python process calling `verify()`, including the library fast-path in `install.py:2132`.
* `claude_hooks/providers/pgvector.py:328`: `import psycopg` inside `PgvectorProvider._ensure_ready()`. Lacks fallback; raises `ImportError`. Exercisable from any Python process that triggers a database operation (recall, store, count). Reached by the `pgvector-mcp` entry point defined in `pyproject.toml:34`.
* `scripts/migrate_to_pgvector.py:624`: Bare `import psycopg` in `main()`. Lacks fallback; crashes with `ModuleNotFoundError`. Exercisable by direct script invocation from any Python interpreter lacking the dependency.
* `scripts/bench_recall.py:107`: Bare `import psycopg` in `PgvectorBenchProvider.__init__`. Lacks fallback; crashes with `ModuleNotFoundError`. Exercisable by direct script invocation when benchmarking pgvector models.
* `tests/test_pgvector_integration.py:56`: `import psycopg` in `_skip_if_no_deps()`. Lacks fallback; raises `unittest.SkipTest`. Exercisable via `pytest` in environments without the `pgvector` extra.
* `tests/test_pgvector_integration.py:285`: `import psycopg` in `tearDownClass()`. Lacks fallback; wrapped in broad `except Exception: pass` which swallows the failure. Exercisable via `pytest`.
* `install.py:2127`, `install.py:2524`, and `install.py:2647`: These sites perform direct imports but are **not** lacking the fallback; they are the "fast-path" components of the `4e67dc2` pattern that explicitly shell out to `find_conda_env_python()` on `ImportError` (ref: `install.py:2140`, `install.py:2543`, `install.py:2661`).

---

## Researcher (round 1)

* `claude_hooks/providers/pgvector.py:123` — Lacks fallback; `verify()` returns `False` on `ImportError`. Exercisable via `run.py` or `run_daemon.py` when the calling interpreter lacks `psycopg`.
* `claude_hooks/providers/pgvector.py:328` — Lacks fallback; `_ensure_ready()` re-raises `ImportError`. Exercisable during any `recall`, `store`, or `count` call via `run.py` or `run_daemon.py` if `pgvector` is enabled.
* `scripts/migrate_to_pgvector.py:624` — Lacks fallback; bare `import` in `main()` crashes with `ModuleNotFoundError`. Exercisable from CLI.
* `scripts/bench_recall.py:107` — Lacks fallback; bare `import` in `PgvectorBenchProvider.__init__` crashes with `ModuleNotFoundError`. Exercisable from CLI.
* `tests/test_pgvector_integration.py:56` — Lacks fallback; `_skip_if_no_deps()` raises `unittest.SkipTest`. Exercisable during test execution.
* `tests/test_pgvector_integration.py:285` — Lacks fallback; `tearDownClass` silently catches and ignores `ImportError`. Exercisable during test cleanup.
* `install.py:[2127, 2524, 2647]` — **Not Lacking**; these sites are the "fast path" checks immediately followed by the `4e67dc2` conda-env subprocess fallback logic (verified at `install.py:2138`, `2539`, and `2656`).

---

## Synthesizer

Six import sites lack the conda-env subprocess fallback implemented in commit 4e67dc2.

* `claude_hooks/providers/pgvector.py:123`: Exercisable; `verify()` returns `False` on `ImportError` without attempting fallback.
* `claude_hooks/providers/pgvector.py:328`: Exercisable; `_ensure_ready()` re-raises `ImportError` during `recall`, `store`, or `count` operations (reachable via `pyproject.toml:34`).
* `scripts/migrate_to_pgvector.py:624`: Exercisable; bare import in `main()` causes `ModuleNotFoundError` upon direct script invocation.
* `scripts/bench_recall.py:107`: Exercisable; bare import in `PgvectorBenchProvider.__init__` causes `ModuleNotFoundError` during benchmarking.
* `tests/test_pgvector_integration.py:56`: Exercisable; `_skip_if_no_deps()` triggers `unittest.SkipTest` without attempting fallback.
* `tests/test_pgvector_integration.py:285`: Exercisable; `tearDownClass` performs a direct import and silently swallows the failure via a broad exception block.

---
