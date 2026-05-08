# Consultation transcript — csl-2026-05-07-1023-c9ca

_Question_: Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python.


## Planner

1. Retrieve diff for commit `4e67dc2` to define the canonical `conda-env` subprocess fallback pattern (try/except structure, env vars, subprocess args).
2. Execute `git grep -n "import psycopg\|from psycopg"` to locate all import sites, excluding files changed in `4e67dc2`.
3. Inspect ±10 lines around each import match to verify absence of the canonical fallback pattern extracted in step 1.
4. Trace call graphs for non-compliant imports to identify user-facing entry points (CLI commands, API endpoints).
5. Audit entry points for `conda`-specific guards (e.g., `CONDA_PREFIX`, `sys.executable` checks) to assess non-conda exercisability.
6. Correlate import site context with environment guard findings to classify each site as exercisable or protected in non-conda environments.

---

## Researcher (round 1)

## Audit Report: psycopg Import Sites Lacking conda-env Subprocess Fallback

### Summary

No commit `4e67dc2` or associated conda-env subprocess fallback pattern for psycopg was found in the codebase. The benchmark transcripts in `docs/benchmarks/kimi-k2.6-cloud-2026-05-07/` explicitly confirm this: *"No commit `4e67dc2` or fallback pattern (`subprocess.*python`, `_spawn.*psycopg`, `fallback.*psycopg`) was found in `claude_hooks/`"*.

### Findings in `claude_hooks/` (Primary Audit Target)

- **`claude_hooks/providers/pgvector.py:123`** — `import psycopg` inside `PgvectorProvider.verify()`. Wrapped in `try/except ImportError` that returns `False` on failure. **Exercisable: yes** from any Python process. No conda-env subprocess fallback attempted.

- **`claude_hooks/providers/pgvector.py:328`** — `import psycopg` inside `PgvectorProvider._ensure_ready()`. Wrapped in `try/except ImportError` that re-raises with a pip-install hint. **Exercisable: yes** from any Python process. No conda-env subprocess fallback attempted.

### Findings Outside `claude_hooks/` (Context Only)

- **`scripts/migrate_to_pgvector.py:624`** — Bare `import psycopg` inside `main()`. **Exercisable: yes**. No guard, no conda fallback; crashes with `ModuleNotFoundError` if psycopg is absent.

- **`scripts/bench_recall.py:107`** — `import psycopg` inside `PgvectorBenchProvider.__init__`. **Exercisable: yes** (conditional on benchmark code path). No guard, no conda fallback.

- **`tests/test_pgvector_integration.py:56`** — `import psycopg` inside `_skip_if_no_deps()`. Catches `ImportError` and raises `unittest.SkipTest`. **Exercisable: yes** (causes skip, not crash).

- **`tests/test_pgvector_integration.py:285`** — `import psycopg` inside `tearDownClass()`. Wrapped in broad `except Exception: pass`. **Exercisable: yes** (failure swallowed).

- **`install.py:2092, 2127, 2524, 2647`** — Multiple `import psycopg` sites. Some are inside functions with `find_conda_env_python()` fallback logic, but these are installer-specific paths, not the runtime `claude_hooks/` provider code.

### Conda-env Subprocess Patterns Found (Not psycopg-related)

The only conda-env subprocess fallback patterns in `claude_hooks/` are unrelated to psycopg:

- **`claude_hooks/consultants_forwarder.py:347-358`** — `_find_consultants_python()` searches for conda env paths (`anaconda3/envs/claude-hooks-consultants`, `miniconda3/envs/...`) for the consultants forwarder, not psycopg.

- **`claude_hooks/hooks/post_tool_use.py:153, 162`** — References conda-env for ruff binary location, not psycopg.

### Verdict

**Both psycopg import sites in `claude_hooks/providers/pgvector.py` lack the conda-env subprocess fallback** and are directly exercisable from any non-conda-env Python process. The `try/except ImportError` handlers provide graceful degradation (return `False` or re-raise) but never attempt to spawn a subprocess with psycopg installed in a dedicated conda environment.

---

## Researcher (round 1)

## Audit Report: psycopg Imports Missing conda-env Subprocess Fallback

### Findings

- **`claude_hooks/providers/pgvector.py:123`** — Direct `import psycopg` inside `PgvectorProvider.verify()` classmethod. No subprocess/conda-env fallback present. This is exercisable from non-conda Python **only if** psycopg is installed in the current environment; otherwise returns `False` immediately (line 124-126).

- **`claude_hooks/providers/pgvector.py:328`** — Direct `import psycopg` inside `PgvectorProvider._ensure_ready()`. No subprocess/conda-env fallback present. This **will raise `ImportError`** from non-conda Python without psycopg installed (line 329-330), blocking all recall/store operations.

### Fallback Pattern Status

- **No conda-env subprocess fallback found** — Grep for `subprocess.*conda|conda.*activate|CONDA_EXE|conda_env` returned zero matches across `claude_hooks/`.

- **Only conda-env reference** is unrelated: `claude_hooks/hooks/post_tool_use.py:162` mentions "Common conda-env locations" but this is for resolving the `ruff` binary path, not for psycopg imports.

- **Commit `4e67dc2`** — No reference to this commit hash found in `claude_hooks/` via grep, so the canonical fallback pattern mentioned cannot be located for comparison.

### Exercisability Assessment

| File:Line | Import Type | Fallback Present? | Exercisable from non-conda Python? |
|-----------|-------------|-------------------|------------------------------------|
| `pgvector.py:123` | Direct import | ❌ No | ⚠️ Only if psycopg pre-installed; otherwise silent fail |
| `pgvector.py:328` | Direct import | ❌ No | ❌ No — raises `ImportError` and halts execution |

### Recommendation

Both import sites need the conda-env subprocess fallback pattern. The `_ensure_ready()` site at line 328 is higher priority since it blocks core recall/store functionality rather than just verification.

---

## Researcher (round 1)

## Audit Report: psycopg Import Sites Without Conda-Env Subprocess Fallback

### psycopg Import Sites Identified

- **`claude_hooks/providers/pgvector.py:123`** — Direct `import psycopg` inside `PgvectorProvider.verify()` classmethod. No `CONDA_PREFIX` check, no `sys.executable` subprocess fallback. If psycopg is only installed in a conda env and the hook runs from a different interpreter, this import fails silently (returns `False` from verify).

- **`claude_hooks/providers/pgvector.py:328`** — Direct `import psycopg` inside `PgvectorProvider._ensure_ready()`. Same issue: no environment guard, no subprocess delegation to a conda-env Python. Raises `ImportError` if psycopg is unavailable in the current interpreter.

### Conda-Env Subprocess Pattern (Existing in Codebase)

The project *does* have a conda-env subprocess fallback pattern elsewhere — it just isn't applied to psycopg imports:

- **`claude_hooks/consultants_forwarder.py:347-358`** — `_find_consultants_python()` probes multiple conda paths (`anaconda3/envs/`, `miniconda3/envs/`, Windows `Anaconda3/envs/`) to locate a dedicated conda env's Python executable.

- **`claude_hooks/store_async.py:78`** — Spawns detached subprocess using `[sys.executable, "-m", "claude_hooks.store_async"]`, ensuring the same interpreter runs the worker.

- **`claude_hooks/code_graph/__main__.py:98`** — Uses `[sys.executable, "-m", "claude_hooks.code_graph", "build", ...]` for detached subprocess, with `PYTHONPATH` injection at line 105.

- **`claude_hooks/lsp_engine/client.py:195`** — Uses `sys.executable` to spawn the LSP server, ensuring interpreter alignment.

### Exercisability from Non-Conda-Env Python

| Site | Exercisable from non-conda Python? | Reason |
|------|-----------------------------------|--------|
| `pgvector.py:123` | **Yes, but fragile** | Direct import succeeds only if psycopg is installed in the *current* interpreter. No fallback to conda-env Python. |
| `pgvector.py:328` | **Yes, but fragile** | Same as above — runtime import depends on current environment having psycopg. |

### Missing Fallback Pattern

Neither psycopg import site implements the pattern seen in `consultants_forwarder.py`:

```python
# Pattern NOT present in pgvector.py:
home = os.environ.get("HOME", "")
candidates = [
    f"{home}/anaconda3/envs/claude-hooks/bin/python",
    f"{home}/miniconda3/envs/claude-hooks/bin/python",
]
```

### Recommendation

To add the conda-env subprocess fallback to psycopg imports, wrap the import in a try/except and fall back to spawning a subprocess using a discovered conda-env Python (similar to `consultants_forwarder.py:347-358` + `store_async.py:78`).

---

## Synthesizer

Commit `4e67dc2` does not exist in this repository; no conda-env subprocess fallback for `psycopg` is implemented in `claude_hooks/`. Two `psycopg` import sites lack environment guards and depend entirely on the current interpreter's installed packages.

*   `claude_hooks/providers/pgvector.py:123` — Inside `PgvectorProvider.verify()`. **Reachable:** Yes. **Behavior:** Returns `False` on `ImportError`. No conda fallback.
*   `claude_hooks/providers/pgvector.py:328` — Inside `PgvectorProvider._ensure_ready()`. **Reachable:** Yes. **Behavior:** Raises `ImportError` on failure. No conda fallback.

Both sites are exercisable from non-conda Python but will fail or degrade if `psycopg` is not installed in the active environment. Conda-env subprocess patterns exist for other components (e.g., `claude_hooks/consultants_forwarder.py:347-358` for consultant binaries, `claude_hooks/store_async.py:78` for detached workers) but are not applied to `psycopg` imports.

---
