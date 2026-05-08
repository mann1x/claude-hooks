# Consultation transcript — csl-2026-05-07-0957-ccac

_Question_: Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python.


## Planner

1. Find the commit 4e67dc2 diff to identify the exact conda-env subprocess fallback pattern added and which file:lines it was applied to — search git log for the commit and show its full diff.
2. Grep all Python files in claude-hooks for `import psycopg` and `from psycopg` to enumerate every import site, recording file:line for each.
3. For each import site found in step 2, read surrounding context (±30 lines) to check whether the conda-env subprocess fallback wrapper (the pattern from step 1) is present at that site.
4. For any import sites lacking the fallback, trace the call graph upward: grep for function/class definitions containing that import, then grep for callers of those functions to determine if the code path is reachable from CLI entrypoints or top-level module execution (i.e., exercisable from a plain `python` invocation without conda-env).
5. Check pyproject.toml or setup.cfg for `psycopg` dependency declarations and any conda-specific conditional dependency markers, to determine whether a non-conda install would even pull in psycopg (affects exercisability assessment).

---

## Researcher (round 1)

## Findings

- **psycopg import sites (2 total, both in same file):**
  - `claude_hooks/providers/pgvector.py:123` — inside `PgvectorProvider.verify()`, bare `import psycopg` in a try/except that logs a warning and returns `False` on `ImportError`
  - `claude_hooks/providers/pgvector.py:328` — inside `PgvectorProvider._ensure_ready()`, bare `import psycopg` in a try/except that re-raises `ImportError` with an install hint

- **No conda-env subprocess fallback exists at either site.** `grep` for `subprocess` inside `pgvector.py` returned zero matches. Both sites use direct `import psycopg` with no subprocess delegation to a conda-env Python.

- **No conda-env subprocess fallback pattern exists anywhere in the project for psycopg.** `grep` across `claude_hooks/` for `subprocess.*conda|_run_in_conda|conda_env.*python|_conda_python` all returned zero matches. The only conda-aware subprocess patterns found are in `claude_hooks/consultants_forwarder.py:347-358` (dedicated `_find_consultants_python()` for a separate conda env) and `bin/_resolve_python.sh` (shell-shim level, not Python-level).

- **Commit 4e67dc2 is not referenced anywhere in the codebase** (`grep` for `4e67dc2` returned zero matches). The "conda-env subprocess fallback added in 4e67dc2" described in the question could not be confirmed from the current source state — it may be unreleased, in a different branch, or the pattern may not exist yet.

- **Exercisability from non-conda-env Python:**
  - `pgvector.py:123` (`verify`) — **exercisable**: if pgvector provider is enabled and the installer or daemon calls `verify()`, and system Python lacks psycopg, it silently returns `False` (non-fatal but provider won't verify).
  - `pgvector.py:328` (`_ensure_ready`) — **exercisable**: called on every `recall()`/`store()` when pgvector is enabled. If system Python lacks psycopg, raises `ImportError("install psycopg: pip install psycopg[binary]")`. This is a hard failure for that provider's recall/store path (caught by dispatcher's per-provider exception handler, so the hook itself still exits 0).
  - `pgvector.py` is omitted from coverage (`pyproject.toml` `[tool.coverage.run] omit`) and the provider is disabled by default (scaffold per CLAUDE.md), so the blast radius is low — only users who manually enable pgvector AND run hooks outside the conda env would hit this.

---

## Researcher (round 1)

## Findings: psycopg import sites lacking conda-env subprocess fallback

**No conda-env subprocess fallback pattern exists anywhere in the codebase.** Grepping for `_run_in_conda`, `conda_env`, `conda-env`, `conda_run`, `_conda_exec`, and `subprocess.*conda` across `claude_hooks/` returned zero hits in any psycopg context. The only conda-adjacent code is `_resolve_ruff()` in `claude_hooks/hooks/post_tool_use.py:162`, which resolves a binary path — unrelated to psycopg.

### All 4 psycopg import sites, none have the fallback:

- **`claude_hooks/providers/pgvector.py:123`** — bare `import psycopg` inside `PgvectorProvider.verify()` classmethod. Catches `ImportError` and logs a warning, then returns `False`. **Exercisable**: called when `install.py` detects a pgvector MCP server and attempts verification; also reachable if provider is enabled and daemon/client calls `detect` → `verify`. Runs in whatever Python the `bin/claude-hook` shim resolves — could be system Python without psycopg.

- **`claude_hooks/providers/pgvector.py:328`** — bare `import psycopg` inside `_ensure_ready()`. Catches `ImportError` and re-raises with install instructions. **Exercisable**: called from `recall()` (line 148) and `store()` whenever pgvector provider is enabled. Same path concern as above — the hook shim may run under system Python where psycopg is absent.

- **`scripts/migrate_to_pgvector.py:624`** — bare `import psycopg` at top of `main()`. No try/except at all — hard crash on missing module. **Exercisable**: invoked via `if __name__ == "__main__"` (line 653) or as a CLI script. Operator tooling, typically run with conda env activated, but no guard exists.

- **`scripts/bench_recall.py:107`** — bare `import psycopg` inside `PgvectorBenchProvider.__init__()`. No try/except — hard crash. **Exercisable**: instantiated when bench script targets pgvector. Same operator-tooling concern as above.

### Exercisability summary for non-conda-env Python:

| Site | Guarded? | Exercisable without conda env? |
|---|---|---|
| `pgvector.py:123` (verify) | yes (catches ImportError, returns False) | Yes — installer may invoke via system Python |
| `pgvector.py:328` (_ensure_ready) | partial (catches + re-raises ImportError) | Yes — hooks dispatch to this if pgvector enabled |
| `migrate_to_pgvector.py:624` | no (unconditional, crashes) | Yes — `python scripts/migrate_to_pgvector.py` |
| `bench_recall.py:107` | no (unconditional, crashes) | Yes — `python scripts/bench_recall.py --pgvector` |

The two `scripts/` sites are lowest risk (operator scripts, usually run interactively with conda env active). The two `pgvector.py` sites are higher risk — they're wired into the hook dispatch path and could fire under system Python if the user enabled the pgvector provider. None of the four sites attempt a subprocess fallback to the conda env's Python.

---

## Researcher (round 1)

Findings on psycopg dependency declarations and their exercisability impact:

- **`pyproject.toml:38`**: `psycopg[binary]` declared under `[project.optional-dependencies]` as `pgvector = ["psycopg[binary]"]`. It is **not** a core/required dependency — only installed via `pip install claude-hooks[pgvector]` or manually.
- **No `setup.cfg`** exists in the repo (glob returned empty). `pyproject.toml` is the sole build config.
- **No conda-specific conditional markers** (e.g. `environment_markers`, platform conditions) exist in `pyproject.toml` — the optional dep is unconditional.
- **`requirements-pgvector.txt:14`**: lists `psycopg[binary]>=3.2` as a separate pin for manual `pip install -r` usage.

**Exercisability implication**: A default `pip install claude-hooks` (without the `[pgvector]` extra) will **not** pull in `psycopg`. A non-conda Python (system python3 without the claude-hooks conda env) that didn't explicitly install the `[pgvector]` extra will lack `psycopg` entirely. This means:

1. **`claude_hooks/providers/pgvector.py:123`** (`verify()` — `import psycopg` in try/except) — **exercisable**: if pgvector provider is enabled in config and the hook runs on system python without `[pgvector]` extra, `ImportError` is caught → returns `False`. No crash, but **no conda-env subprocess fallback** unlike `install.py:2127`.

2. **`claude_hooks/providers/pgvector.py:328`** (`_ensure_ready()` — `import psycopg` in try/except) — **exercisable**: same scenario, `ImportError` re-raised with a user-facing message. **No conda-env subprocess fallback**.

3. **`install.py:2127`**, **`install.py:2524`**, **`install.py:2647`** — **NOT lacking fallback**: each has the fast-path `try: import psycopg` followed by `find_conda_env_python()` subprocess fallback. These are the sites that commit `4e67dc2` added the fallback to.

4. **`scripts/migrate_to_pgvector.py:624`** — **exercisable**: bare `import psycopg` with no try/except and no conda fallback. Will `ImportError`-crash from any Python lacking psycopg. Since this is an operator script (not a hook), it's expected to run inside the conda env — but has no guard.

5. **`scripts/bench_recall.py:107`** — **exercisable**: bare `import psycopg` inside `PgvectorBenchProvider.__init__` with no fallback. Same situation — operator script, expected in conda env, no guard.

6. **`tests/test_pgvector_integration.py:56`**, **`tests/test_pgvector_integration.py:285`** — **exercisable from pytest**: import psycopg, skip on `ImportError`. No conda fallback, but pytest's `SkipTest` is the appropriate behavior for test files (not a production path).

---

## Synthesizer

Four psycopg import sites lack the conda-env subprocess fallback that `install.py:2127`, `install.py:2524`, and `install.py:2647` already have:

- **`claude_hooks/providers/pgvector.py:123`** — bare `import psycopg` in `verify()`, wrapped in try/except that returns `False` on `ImportError`. **Exercisable from non-conda Python**: fires whenever the pgvector provider is enabled and `verify()` is called (e.g. by the installer or daemon). Won't crash, but silently degrades — no fallback to conda env's Python.

- **`claude_hooks/providers/pgvector.py:328`** — bare `import psycopg` in `_ensure_ready()`, wrapped in try/except that re-raises `ImportError` with an install hint. **Exercisable from non-conda Python**: called on every `recall()`/`store()` when pgvector provider is enabled. Hard failure for that provider's operations (caught by dispatcher, so hook still exits 0).

- **`scripts/migrate_to_pgvector.py:624`** — bare `import psycopg` at top of `main()`, no try/except, no fallback. **Exercisable from non-conda Python**: runs via `__main__` or CLI invocation; crashes immediately on `ImportError`. Lower risk (operator script, typically run with conda env active), but has zero guard.

- **`scripts/bench_recall.py:107`** — bare `import psycopg` in `PgvectorBenchProvider.__init__()`, no try/except, no fallback. **Exercisable from non-conda Python**: crashes on instantiation if psycopg absent. Same low-risk operator-script caveat as above.

Note: `psycopg[binary]` is an optional dependency (`pyproject.toml:38` — `pgvector = ["psycopg[binary]"]`), so a default install won't have it, making these paths reachable precisely when a user enables the pgvector provider but the hook shim resolves to a Python outside the conda env. The two `pgvector.py` sites are the highest priority to fix since they sit on the live hook dispatch path; the `scripts/` sites are lower priority but still unprotected.

---
