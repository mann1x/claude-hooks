# Consultation transcript — csl-2026-10-22-0759-9e9b

_Question_: Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python.


## Planner

1. Inspect commit `4e67dc2` with `git show` to identify the exact conda-env subprocess fallback introduced there, including its helper, target interpreter, command protocol, and call sites; this comparison is still open because no Git-history tool call completed.
2. Read `claude_hooks/providers/pgvector.py:110-140` around the first import at `claude_hooks/providers/pgvector.py:123` and compare `PgVectorProvider.verify` with the fallback pattern from commit `4e67dc2`; this site was verified to contain only a local `import psycopg` followed by `ImportError` handling, with no observed fallback.
3. Read `claude_hooks/providers/pgvector.py:315-345` around the second import at `claude_hooks/providers/pgvector.py:328` and compare its connection path with the fallback pattern from commit `4e67dc2`; this site was verified to contain only a local `import psycopg` followed by an `ImportError` raised as an installation hint, with no observed fallback.
4. Enumerate every `import psycopg` and `from psycopg ...` site across the repository—not only `claude_hooks/`—to ensure the project-wide grep does not reveal additional dynamic, aliased, or non-`claude_hooks/` imports; the attempted global grep was blocked by the tool budget, so this remains unverified.
5. Trace all call paths into `PgVectorProvider.verify` (`claude_hooks/providers/pgvector.py:121`) and the connection method containing `claude_hooks/providers/pgvector.py:328`, checking provider discovery, configuration parsing, daemon/CLI entry points, and tests to determine whether each import is reachable when the hook process runs under a non-conda-env Python; the observed grep found only the two imports in `claude_hooks/providers/pgvector.py`, while broader reachability tracing remains open.
6. Inspect `claude_hooks/providers/__init__.py:7-11` to verify whether provider registration eagerly imports the pgvector module or merely documents an optional psycopg probe, because this determines whether merely importing `claude_hooks` can reach either import site; only the docstring context was inspected, and the implementation was not read.
7. Reproduce reachability with the project’s test suite or a minimal subprocess using an interpreter outside the `claude-hooks` conda environment, recording for each import site whether it is actually exercised, merely reachable, or unreachable; no reproduction was completed before the tool budget expired.

---

## Researcher (round 1)

- **Commit-history limitation:** no Git-history tool was available, so commit `4e67dc2` could not be inspected directly. The current tree contains the expected fallback: `find_conda_env_python()` resolves the default `claude-hooks` environment (`install.py:101`, `install.py:66`), invokes it as `[conda_python, "-c", script, ...]`, captures JSON, and treats the final stdout line as `{"ok": ..., "reason": ...}` (`install.py:2138`, `install.py:2145`, `install.py:2153`).

- **`claude_hooks/providers/pgvector.py:123` — lacks fallback; exercisable from non-conda Python.** `PgvectorProvider.verify()` imports `psycopg` directly and returns `False` on `ImportError`, with no subprocess fallback (`claude_hooks/providers/pgvector.py:121`, `claude_hooks/providers/pgvector.py:123`, `claude_hooks/providers/pgvector.py:124`). It is callable from any interpreter, and the installer has a generic `cls.verify(candidate)` call (`install.py:3556`). The specialized `_verify_pgvector_dsn()` path is protected by the conda subprocess fallback (`install.py:2125`, `install.py:2138`), so that specific installer path does not expose the false-negative.

- **`claude_hooks/providers/pgvector.py:328` — lacks fallback; exercisable from non-conda Python.** `_ensure_ready()` directly imports `psycopg` and raises an installation hint on failure; it never tries the conda interpreter (`claude_hooks/providers/pgvector.py:320`, `claude_hooks/providers/pgvector.py:328`, `claude_hooks/providers/pgvector.py:330`). This path is reached by provider recall, store, count, and batch operations (`claude_hooks/providers/pgvector.py:148`, `claude_hooks/providers/pgvector.py:214`, `claude_hooks/providers/pgvector.py:241`, `claude_hooks/providers/pgvector.py:265`). Standard shims prefer a venv or conda interpreter and only fall back to system Python (`bin/_resolve_python.sh:31`, `bin/_resolve_python.sh:35`, `bin/_resolve_python.sh:53`), but a directly invoked non-conda interpreter, a venv without `psycopg`, or a host without the conda env reaches this failure.

- **`scripts/migrate_to_pgvector.py:624` — lacks fallback; exercisable from non-conda Python.** The migration command imports `psycopg` directly before connecting (`scripts/migrate_to_pgvector.py:624`, `scripts/migrate_to_pgvector.py:626`). Running this script with system Python or another environment lacking `psycopg` fails at that import; no conda subprocess path is present.

- **`scripts/bench_recall.py:107` — lacks fallback; conditionally exercisable from non-conda Python.** `PgvectorBenchProvider.__init__()` directly imports and uses `psycopg` (`scripts/bench_recall.py:105`, `scripts/bench_recall.py:107`, `scripts/bench_recall.py:108`). It is reached when the benchmark selects the pgvector provider, so a non-conda invocation fails there.

- **`tests/test_pgvector_integration.py:56` — lacks fallback; exercised by tests but intentionally skips.** The integration suite’s dependency probe imports `psycopg`; absence raises `SkipTest`, not a subprocess fallback (`tests/test_pgvector_integration.py:54`, `tests/test_pgvector_integration.py:56`, `tests/test_pgvector_integration.py:58`). Thus non-conda pytest runs encounter the import site but do not fail the suite.

- **`tests/test_pgvector_integration.py:285` — lacks fallback; normally gated by the suite’s dependency check.** The cleanup path directly imports `psycopg` before connecting (`tests/test_pgvector_integration.py:285`, `tests/test_pgvector_integration.py:287`). In a non-conda run, the earlier dependency probe skips the integration suite when `psycopg` is absent (`tests/test_pgvector_integration.py:56`), so this site is not normally reached in that scenario.

- **Installer import sites already have the fallback and are not defects:** `install.py:2092` is inside the subprocess verification script; `install.py:2127` is the guarded fast path followed by subprocess fallback; `install.py:2524` and `install.py:2647` are similarly guarded fast paths; `install.py:2546` and `install.py:2666` are inside subprocess scripts (`install.py:2089`, `install.py:2092`, `install.py:2125`, `install.py:2138`, `install.py:2539`, `install.py:2656`).

- **Project-wide import inventory:** the verified source import sites are `install.py:2092`, `install.py:2127`, `install.py:2524`, `install.py:2546`, `install.py:2647`, `install.py:2666`, `claude_hooks/providers/pgvector.py:123`, `claude_hooks/providers/pgvector.py:328`, `tests/test_pgvector_integration.py:56`, `tests/test_pgvector_integration.py:285`, `scripts/migrate_to_pgvector.py:624`, and `scripts/bench_recall.py:107`. No `from psycopg ...` import was found.

---

## Researcher (round 1)

- **Exhaustive import inventory:** Within the production package, only two direct `psycopg` imports lack a conda-subprocess fallback: `claude_hooks/providers/pgvector.py:123` and `claude_hooks/providers/pgvector.py:328`. The repository also has two operator-script imports without that fallback: `scripts/migrate_to_pgvector.py:624` and `scripts/bench_recall.py:107`.

- **`claude_hooks/providers/pgvector.py:123` — reachable, but not exercised by the confirmed non-conda installer flow.** The site is inside `PgvectorProvider.verify`; missing `psycopg` immediately logs and returns `False` at `claude_hooks/providers/pgvector.py:124`. The install helper only calls `PgvectorProvider.verify` after successfully importing `psycopg` in the current interpreter at `install.py:2127` and `install.py:2132`. When the import fails, control passes to the conda subprocess fallback at `install.py:2135` and `install.py:2138`, so the known non-conda path does **not** reach line 123. A direct call to `PgvectorProvider.verify` can still reach it; I did not find another confirmed production caller in `claude_hooks/`.

- **`claude_hooks/providers/pgvector.py:328` — actually exercisable, and consequential.** `_ensure_ready` imports `psycopg` at `claude_hooks/providers/pgvector.py:328`; on failure it raises `ImportError` at `claude_hooks/providers/pgvector.py:329` rather than using the conda environment. The normal hook path can reach this method: provider construction is enabled by configuration plus a DSN at `claude_hooks/dispatcher.py:129`, `claude_hooks/dispatcher.py:136`, and `claude_hooks/dispatcher.py:148`; recall invokes `_ensure_ready` at `claude_hooks/providers/pgvector.py:148` and converts the resulting `ImportError` into an empty recall at `claude_hooks/providers/pgvector.py:149`. A non-conda interpreter is plausible because the shim prefers a repo `.venv`, then conda, then system Python at `bin/_resolve_python.sh:31`, `bin/_resolve_python.sh:35`, and `bin/_resolve_python.sh:53`.

- **The same line-328 defect also affects the standalone pgvector MCP process.** The MCP server imports `PgvectorProvider` at `claude_hooks/pgvector_mcp/server.py:37`, and its documented lifecycle delegates database initialization to `_ensure_ready` at `claude_hooks/pgvector_mcp/server.py:14`. The installer deliberately permits the launcher to fall back to the current interpreter when conda is absent at `install.py:2296` and `install.py:2297`. Therefore line 328 is exercisable from a non-conda Python running `pgvector-mcp`; without `psycopg`, database-backed MCP tools fail rather than delegating to conda.

- **Provider import itself is lazy.** Merely importing `claude_hooks.providers` does not import the pgvector module or execute either psycopg site. The registry stores the module path as data at `claude_hooks/providers/__init__.py:30` and `claude_hooks/providers/__init__.py:33`; `get_provider_class` performs the import only on demand at `claude_hooks/providers/__init__.py:43` and `claude_hooks/providers/__init__.py:53`. The dispatcher follows the same on-demand pattern at `claude_hooks/dispatcher.py:125` and `claude_hooks/dispatcher.py:140`.

- **`scripts/migrate_to_pgvector.py:624` — definitely exercisable from any interpreter.** The import is unconditional in `main` at `scripts/migrate_to_pgvector.py:610` and `scripts/migrate_to_pgvector.py:624`; it has no conda fallback. A non-conda Python lacking `psycopg` fails immediately when the migration command runs.

- **`scripts/bench_recall.py:107` — conditionally exercisable.** The import occurs when `PgvectorBenchProvider` is constructed at `scripts/bench_recall.py:106` and `scripts/bench_recall.py:107`; it has no conda fallback. It is not exercised merely by importing the benchmark module, but a pgvector benchmark run under a non-conda interpreter lacking `psycopg` fails at construction.

- **Test-only direct imports are not production fallback gaps.** `tests/test_pgvector_integration.py:56` intentionally detects missing `psycopg` and skips the integration suite at `tests/test_pgvector_integration.py:57`; the teardown import at `tests/test_pgvector_integration.py:285` is guarded by exception handling. Neither represents a production hook path.

- **Installer imports already have the intended fallback.** The current tree’s direct installer imports at `install.py:2127`, `install.py:2524`, and `install.py:2647` are fast paths followed by conda-subprocess handling, rather than additional missing-fallback sites. A static audit was completed, but dynamic subprocess reproduction was not completed before tool access ended.

---

## Synthesizer

The static audit found **six direct `psycopg` import sites without the conda-subprocess fallback**; of these, **two are production-package sites**, **two are operator scripts**, and **two are test-only sites**. Only `claude_hooks/providers/pgvector.py:328` is on a confirmed normal non-conda hook path. Dynamic reproduction was not completed, and commit `4e67dc2` itself could not be inspected because Git-history access was unavailable.

### Production package

- `claude_hooks/providers/pgvector.py:123` — **no fallback; directly exercisable, but not by the confirmed installer fallback path.**
  - `PgvectorProvider.verify()` imports `psycopg` locally and returns `False` on `ImportError`; it does not invoke the conda interpreter (`claude_hooks/providers/pgvector.py:121`, `claude_hooks/providers/pgvector.py:123`, `claude_hooks/providers/pgvector.py:124`).
  - A direct call to `PgvectorProvider.verify()` from a Python lacking `psycopg` reaches this import and returns `False`.
  - The known installer path first imports `psycopg` in the current interpreter; if that fails, control goes to the conda subprocess before `PgvectorProvider.verify()` is called (`install.py:2127`, `install.py:2132`, `install.py:2135`, `install.py:2138`). Therefore that specific non-conda installer flow does **not** reach line 123.
  - The fallback executes the conda Python as `[conda_python, "-c", script, dsn]`, reads the final stdout line as JSON, and interprets its `ok` and `reason` fields (`install.py:2140`, `install.py:2146`, `install.py:2147`, `install.py:2153`, `install.py:2158`, `install.py:2161`). The target defaults to the `claude-hooks` conda environment (`install.py:66`, `install.py:101`).

- `claude_hooks/providers/pgvector.py:328` — **no fallback; yes, actually exercisable from a non-conda Python on a normal hook path.**
  - `_ensure_ready()` imports `psycopg` directly and converts `ImportError` into an installation-hint exception; it does not delegate to the conda environment (`claude_hooks/providers/pgvector.py:320`, `claude_hooks/providers/pgvector.py:328`, `claude_hooks/providers/pgvector.py:330`).
  - Provider recall calls `_ensure_ready()` (`claude_hooks/providers/pgvector.py:144 [no _ensure_ready at this line; line is in recall]`, `claude_hooks/providers/pgvector.py:148`) and converts its `ImportError` into an empty result (`claude_hooks/providers/pgvector.py:149`).
  - The pgvector MCP process imports the provider (`claude_hooks/pgvector_mcp/server.py:37`), while the installer permits the launcher to fall back to the current interpreter when conda is unavailable (`install.py:2296`, `install.py:2297`).
  - Consequently, system Python or another environment without `psycopg` can reach line 328 and lose database-backed recall or fail the MCP database path.

### Operator scripts

- `scripts/migrate_to_pgvector.py:624` — **no fallback; unconditionally exercisable when the migration command runs.**
  - The import is directly in `main()`, before connecting (`scripts/migrate_to_pgvector.py:610`, `scripts/migrate_to_pgvector.py:624`, `scripts/migrate_to_pgvector.py:626`).
  - Running the migration with system Python or another environment lacking `psycopg` fails at this import.

- `scripts/bench_recall.py:107` — **no fallback; conditionally exercisable.**
  - The import occurs when `PgvectorBenchProvider` is constructed (`scripts/bench_recall.py:105`, `scripts/bench_recall.py:106`, `scripts/bench_recall.py:107`) and is immediately followed by `psycopg.connect()` (`scripts/bench_recall.py:108`).
  - Merely importing the benchmark module does not exercise it; selecting and constructing the pgvector benchmark provider under a Python lacking `psycopg` does.

### Test-only sites

- `tests/test_pgvector_integration.py:56` — **no fallback; exercised as a dependency probe, intentionally skipped when unavailable.**
  - Missing `psycopg` raises `SkipTest` rather than invoking conda (`tests/test_pgvector_integration.py:54 [no SkipTest at this line; line is in _skip_if_no_deps]`, `tests/test_pgvector_integration.py:56`, `tests/test_pgvector_integration.py:58`).
  - Thus a non-conda pytest run encounters the import but skips the integration suite rather than failing.

- `tests/test_pgvector_integration.py:285` — **no fallback; normally unreachable in a Python lacking `psycopg`.**
  - The cleanup path imports `psycopg` before connecting (`tests/test_pgvector_integration.py:285`, `tests/test_pgvector_integration.py:287`).
  - However, the earlier dependency probe skips the suite when `psycopg` is absent (`tests/test_pgvector_integration.py:56`, `tests/test_pgvector_integration.py:58`). If the suite is running, the import has already succeeded in that interpreter.

### Excluded as already covered

The installer imports are not additional gaps: `install.py:2092`, `install.py:2546`, and `install.py:2666` are inside subprocess scripts; `install.py:2127`, `install.py:2524`, and `install.py:2647` are guarded fast paths followed by subprocess handling (`install.py:2089`, `install.py:2125`, `install.py:2138`, `install.py:2539`, `install.py:2656`).

The audit was verified by repository-wide import search and direct reads of the surrounding control flow. What remains unfinished is dynamic confirmation under a specific non-conda interpreter, and an exact `git show 4e67dc2` comparison; the current-tree fallback protocol was verified, but its attribution to that exact commit was not.

---
