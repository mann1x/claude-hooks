# Consultation transcript — csl-2026-05-09-0654-e2ce

_Question_: Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python.


## Planner

1. Grep for `import psycopg` and `from psycopg` (including `psycopg2`) across the entire claude-hooks repo (use `grep -rn "import psycopg" --include="*.py" .` and similarly for `from psycopg`). Record each file:line.

2. For each file:line found, inspect the surrounding 10 lines to see if the import is inside a function/method that is guarded by a conda‑detection check or uses `subprocess.run` / `conda run` fallback as added in commit 4e67dc2. The canonical fallback pattern is likely a helper in `conda_utils.py` or similar — locate that commit’s diff to identify the exact symbol (e.g., `connect_psycopg_with_fallback`).

3. For files lacking the fallback, determine execution context: is the import at module level (always runs when imported) or inside a function that is only called from a conda‑limited entry point? Check the call chain by tracing `__name__ == '__main__'` blocks or entry‑point scripts that assume a conda environment.

4. Identify any non‑fallback psycopg import that is in a module listed in `setup.py` / `pyproject.toml` entry points or in a script that is directly executable (e.g., `bin/`, `cli.py`). Those are likely exercisable from non‑conda‑env Python.

5. For each such site, also check if the import is in an `except` block or conditional branch (e.g., `if conda_is_active: import psycopg`)—if yes, note that it is not reachable without conda.

6. Output final list of `file:line` with annotation: `EXERCISABLE` (if the import runs when a user outside conda invokes the tool) or `NOT_EXERCISABLE` (if the import is dead code or only runs inside a conda‑guaranteed path).

---

## Researcher (round 1)

## Psycopg import sites lacking conda-env subprocess fallback

**Canonical fallback pattern (commit `4e67dc2`):** Only exists in `install.py`. The pattern: `try: import psycopg` → `except ImportError: pass` → `find_conda_env_python()` → `subprocess.run([conda_py, "-c", script, ...])`. Three instances at `install.py:2127`, `install.py:2524`, `install.py:2647`. Zero instances exist in `claude_hooks/` or `scripts/` or `tests/`.

---

- **`claude_hooks/providers/pgvector.py:123`** — `import psycopg` inside `PgvectorProvider.verify()`. Wrapped in `try/except ImportError` that returns `False` only. No conda-env subprocess fallback. **Exercisable: yes** — any caller of `verify()` (installer, config validator) on a non-conda Python gets `False` silently even if psycopg IS available in the conda env.

- **`claude_hooks/providers/pgvector.py:328`** — `import psycopg` inside `_ensure_ready()`. Wrapped in `try/except ImportError` that re-raises with a message. No conda-env subprocess fallback. **Exercisable: yes** — any code path that calls `recall()` or `store()` on a `PgvectorProvider` instance will hit this, and the re-raised exception kills the hook handler (though the dispatcher catches and logs it).

- **`tests/test_pgvector_integration.py:56`** — `import psycopg` inside `_skip_if_no_deps()`. Wrapped in `try/except ImportError` → `raise unittest.SkipTest`. No conda-env subprocess fallback. **Exercisable: yes** — pytest runs this function when collecting the test file; missing psycopg simply skips, no crash, but a conda-only psycopg install is invisible.

- **`tests/test_pgvector_integration.py:285`** — `import psycopg` inside `tearDownClass()`. Wrapped in a bare `except Exception: pass`. No conda-env subprocess fallback. **Exercisable: yes (weakly)** — swallowed silently if missing, but also silently fails to clean up if psycopg is only in the conda env and the test runs under a different interpreter.

- **`scripts/migrate_to_pgvector.py:624`** — Bare `import psycopg` at function level inside `main()`. No `try/except`, no guard, no conda-env subprocess fallback. **Exercisable: yes** — will crash with `ModuleNotFoundError` if invoked from a non-conda Python without `pip install psycopg[binary]`. No graceful degradation.

- **`scripts/bench_recall.py:107`** — `import psycopg` inside `PgvectorBenchProvider.__init__()`. No `try/except`, no guard, no conda-env subprocess fallback. **Exercisable: yes** — will crash with `ModuleNotFoundError` if the benchmark script is run from a non-conda Python and the pgvector bench provider is instantiated (conditional on `--pgvector` flag or similar).

**Bottom line:** 6 import sites across 4 files, all exercisable from a non-conda-env Python. The `claude_hooks/providers/pgvector.py` two sites are the highest-impact because they're in the production provider code — they don't crash (both have `try/except ImportError`), but they silently give up or misreport availability without ever checking if psycopg exists in the conda env. The `scripts/` two sites crash hard. The `tests/` two sites are harmless (skip/quiet swallow) but could give false negatives in CI.

---

## Researcher (round 1)

Here is the focused audit of every `import psycopg` site in the project, with execution-context analysis for each.

---

## 1. `claude_hooks/providers/pgvector.py:123` — `PgvectorProvider.verify()`

```python
try:
    import psycopg  # type: ignore
except ImportError:
    log.warning("psycopg not installed — cannot verify pgvector")
    return False
```

- **Import site**: inside a `@classmethod` that runs at install-time when the installer finds a `pgvector-mcp` candidate.
- **Conda fallback?** No. No subprocess-to-conda-env escape hatch.
- **Exercisable from non-conda Python?** **Yes**, via `python3 install.py` (which `_resolve_python.sh` does NOT wrap). If the user runs the installer with system Python, `verify()` logs a warning and returns `False` — a graceful failure, not a crash. Low risk.

---

## 2. `claude_hooks/providers/pgvector.py:328` — `PgvectorProvider._ensure_ready()`

```python
try:
    import psycopg  # type: ignore
except ImportError as e:
    raise ImportError("install psycopg: pip install psycopg[binary]") from e
```

- **Import site**: inside an instance method called by `recall()`, `store()`, `batch_recall()`, `batch_store()`, and all `kg_*()` methods.
- **Conda fallback?** No.
- **Exercisable from non-conda Python?** **No** — not under normal operation. The call chain is: Claude Code fires a hook event → `bin/claude-hook` → `bin/_resolve_python.sh` (prefers `$HOME/anaconda3/envs/claude-hooks/bin/python` at line 35) → `run.py` → dispatcher → provider methods. The injected Python is always the conda env Python, which has `psycopg[binary]` installed there. A non-conda Python would only reach this code if someone manually removes or bypasses the shim — not a realistic production path.

---

## 3. `scripts/migrate_to_pgvector.py:624` — `main()`

```python
import psycopg
```

- **Import site**: top-level in `main()`, after CLI argument parsing. No try/except.
- **Conda fallback?** No.
- **Exercisable from non-conda Python?** **Yes**. The user invokes this directly: `python scripts/migrate_to_pgvector.py`. There is no shim wrapping. If run from system Python lacking `psycopg`, this crashes with an unhandled `ImportError` at line 624. **High-risk gap** — the script already does arg-parsing and logging setup before hitting this import, so the pre-import work is wasted.

---

## 4. `scripts/bench_recall.py:107` — `PgvectorBenchProvider.__init__()`

```python
def __init__(self, dsn: str, model_name: str):
    import psycopg
    self.conn = psycopg.connect(dsn)
```

- **Import site**: inside a class `__init__` that's gated behind `bench_recall`'s `argparse` subcommand selection.
- **Conda fallback?** No.
- **Exercisable from non-conda Python?** **Yes** — `python scripts/bench_recall.py pgvector ...`. No shim wrapping. Crashes with unhandled `ImportError` at line 107 when invoked from system Python. Same pattern as `migrate_to_pgvector.py`.

---

## 5. `tests/test_pgvector_integration.py:56` — `_skip_if_no_deps()`

```python
try:
    import psycopg  # noqa: F401
except ImportError:
    raise unittest.SkipTest("psycopg not installed")
```

- **Import site**: inside a helper function called by `setUpClass`.
- **Conda fallback?** No.
- **Exercisable from non-conda Python?** **Yes**, but it's a **graceful skip** via `SkipTest`, not a crash. The CLAUDE.md recommends `conda activate claude-hooks` before running tests (`CLAUDE.md:7` in the conda-env section), but nothing enforces it. Acceptable — this is the standard pattern for optional integration tests.

---

## 6. `tests/test_pgvector_integration.py:285` — `tearDownClass()`

```python
try:
    import psycopg
    conn = psycopg.connect(PGVECTOR_DSN, connect_timeout=3)
    ...
except Exception:
    pass
```

- **Import site**: inside a `@classmethod` that only runs if the test class wasn't skipped.
- **Conda fallback?** No.
- **Exercisable from non-conda Python?** Theoretically yes, but logically gated by `_skip_if_no_deps()` in `setUpClass`. If `SkipTest` was raised, `tearDownClass` doesn't run. If the class proceeded, the import at line 56 already succeeded earlier in the same runtime. No realistic non-conda exposure.

---

## Summary

| # | Path:line | Function | Conda fallback? | Exercisable non-conda? | Severity |
|---|---|---|---|---|---|
| 1 | `pgvector.py:123` | `verify()` | No | Yes (installer) | Low — returns False gracefully |
| 2 | `pgvector.py:328` | `_ensure_ready()` | No | **No** (shim-gated) | None |
| 3 | `scripts/migrate_to_pgvector.py:624` | `main()` | No | **Yes** | **High** — unhandled `ImportError` |
| 4 | `scripts/bench_recall.py:107` | `PgvectorBenchProvider.__init__()` | No | **Yes** | **High** — unhandled `ImportError` |
| 5 | `tests/test_pgvector_integration.py:56` | `_skip_if_no_deps()` | No | Yes | Low — graceful `SkipTest` |
| 6 | `tests/test_pgvector_integration.py:285` | `tearDownClass()` | No | No (gated by #5) | None |

**Action items**: Two script files (`scripts/migrate_to_pgvector.py:624` and `scripts/bench_recall.py:107`) lack the conda-env subprocess fallback and will crash with an unhandled `ImportError` when invoked from system Python. Both should either (a) try a subprocess call to the conda env's Python before raising, or (b) print a one-line "activate the claude-hooks conda env first" message instead of the raw traceback. The provider module itself (`pgvector.py`) is safe because its runtime callers always go through the `bin/` shim that resolves the conda-env Python.

---

## Researcher (round 1)

## Findings: psycopg import sites lacking conda-env subprocess fallback (per `4e67dc2` pattern)

The commit `4e67dc2` pattern (seen in `install.py`) is: fast-path `try: import psycopg` in-process, and on `ImportError`, shell out to the conda env's python via `find_conda_env_python()` + `subprocess.run(...)`. Three sites in `install.py` already have this:
- `install.py:2118-2148` — `_verify_pgvector_dsn()`
- `install.py:2515-2540` — `_pgvector_tables_present()`
- `install.py:2631-2674` — `_init_pgvector_schema()`

Six sites across four files **lack** any conda subprocess fallback.

---

### 1. `claude_hooks/providers/pgvector.py:123`
```python
# Inside verify() — line 121-126
def verify(cls, ...):
    try:
        import psycopg
    except ImportError:
        log.warning("psycopg not installed")
        return False
```
- **EXERCISABLE** — any code calling `PgvectorProvider.verify()` from any Python.
- The `except ImportError` handles failure gracefully (returns `False`), so no crash. But it never attempts a conda subprocess, so a user outside conda gets `False` even when psycopg is installed and reachable in the `claude-hooks` conda env.

### 2. `claude_hooks/providers/pgvector.py:328`
```python
# Inside _ensure_ready() — line 327-330
try:
    import psycopg
except ImportError as e:
    raise ImportError("install psycopg: pip install psycopg[binary]") from e
```
- **EXERCISABLE** — triggered by any call to `recall()`, `store()`, or `verify_config()` on a `PgvectorProvider`  instance, from any Python.
- The re-raise gives a misleading error to users running outside conda: it says "pip install psycopg" into the current env, rather than activating the conda env where psycopg already lives.

### 3. `scripts/migrate_to_pgvector.py:624`
```python
# Inside main() — line 624, no try/except wrapper
import psycopg
```
- **EXERCISABLE** — run `python scripts/migrate_to_pgvector.py` from any interpreter.
- Crashes with `ModuleNotFoundError: No module named 'psycopg'` when psycopg is absent — no conda fallback, no graceful error message suggesting the conda env.

### 4. `scripts/bench_recall.py:107`
```python
# Inside PgvectorBenchProvider.__init__() — line 107, no try/except wrapper
import psycopg
```
- **EXERCISABLE** — run `python scripts/bench_recall.py` from any interpreter that hits the pgvector benchmark code path.
- Crashes with `ModuleNotFoundError` — no conda fallback.

### 5. `tests/test_pgvector_integration.py:56`
```python
# Inside _skip_if_no_deps() — line 54-58
def _skip_if_no_deps():
    try:
        import psycopg
    except ImportError:
        raise unittest.SkipTest("psycopg not installed")
```
- **EXERCISABLE** — any `pytest tests/test_pgvector_integration.py` run from a non-conda-env Python.
- Returns `SkipTest` — the pytest convention. This is arguably *correct* behavior for a test file (you skip when deps are missing). Adding a conda subprocess fallback here would be unusual and might break the test-as-documentation contract. But strictly, it lacks the fallback from `4e67dc2`.

### 6. `tests/test_pgvector_integration.py:285`
```python
# Inside tearDownClass() — line 284-294
try:
    import psycopg
    ...cleanup...
except Exception:
    pass
```
- **EXERCISABLE** — any pytest run where the test class's `tearDownClass` executes.
- The broad `except Exception: pass` swallows the error silently. Again, test teardown — unconventional to spawn a subprocess here. Same caveat as #5.

---

### Conda-guard check (Planner sub-task)

None of the six sites above have `if conda_is_active:` or any conda-based conditional guard. Every import runs unconditionally when the containing function/method is entered. The only "guarding" is:
- Sites 1, 2, 5: `try/except ImportError` — handles *absence* of psycopg, but never probes for the conda env.
- Sites 3, 4: no guard at all → unconditional `ModuleNotFoundError` crash on non-conda interpreters.

---

### Summary table

| `file:line` | Guard type | Conda fallback? | EXERCISABLE? |
|---|---|---|---|
| `claude_hooks/providers/pgvector.py:123` | `try/except ImportError` → False | No | EXERCISABLE |
| `claude_hooks/providers/pgvector.py:328` | `try/except ImportError` → re-raise | No | EXERCISABLE |
| `scripts/migrate_to_pgvector.py:624` | None | No | EXERCISABLE |
| `scripts/bench_recall.py:107` | None | No | EXERCISABLE |
| `tests/test_pgvector_integration.py:56` | `try/except ImportError` → SkipTest | No | EXERCISABLE |
| `tests/test_pgvector_integration.py:285` | `except Exception: pass` (broad) | No | EXERCISABLE |

The first two sites (`pgvector.py` lines 123, 328) are the highest-impact gaps because they live in the production runtime path — a user running Claude Code hooks outside the conda env (e.g., system `python3`) will silently get degraded/no pgvector recall when the provider falls to `except ImportError`, with no attempt to shell out to the conda env's Python the way `install.py` does. The script files (`migrate_to_pgvector.py`, `bench_recall.py`) crash hard but are invoked explicitly, so the user gets an immediate error message.

---

## Synthesizer

- `claude_hooks/providers/pgvector.py:123` — **EXERCISABLE** (inside `verify()`, called from `install.py` which runs under system Python, no shim).  
- `claude_hooks/providers/pgvector.py:328` — **NOT EXERCISABLE** (inside `_ensure_ready()`; production call chain `bin/` → `_resolve_python.sh` forces conda-env Python, so non-conda interpreter never reaches it).  
- `scripts/migrate_to_pgvector.py:624` — **EXERCISABLE** (bare `import psycopg` in `main()`, invoked directly via `python scripts/migrate_to_pgvector.py`; crashes with `ModuleNotFoundError` outside conda).  
- `scripts/bench_recall.py:107` — **EXERCISABLE** (same pattern, inside `PgvectorBenchProvider.__init__()`, invoked via `python scripts/bench_recall.py`; crashes with `ModuleNotFoundError`).  
- `tests/test_pgvector_integration.py:56` — **EXERCISABLE** (inside `_skip_if_no_deps()`, called from `setUpClass`; pytest collection from any interpreter hits it, gracefully raises `SkipTest`).  
- `tests/test_pgvector_integration.py:285` — **NOT EXERCISABLE** (inside `tearDownClass()`; runs only if `setUpClass` succeeded, which already imported psycopg at line 56 — if psycopg is missing the class is skipped, so line 285 never executes under non-conda Python).  

**Remediation priority**: `scripts/migrate_to_pgvector.py:624` and `scripts/bench_recall.py:107` are the highest-risk — they crash hard with no fallback or helpful error. `pgvector.py:123` silently returns `False` but never probes the conda env, causing degraded functionality without alerting the user.

---
