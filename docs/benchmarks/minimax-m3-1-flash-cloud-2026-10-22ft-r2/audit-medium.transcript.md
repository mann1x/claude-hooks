# Consultation transcript — csl-2026-10-22-2050-c5f0

_Question_: Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python.


## Planner

1. Inspect `git show 4e67dc2 --stat` and `git show 4e67dc2 --` to identify the exact conda-env subprocess fallback pattern, its helper/symbol names, and the import sites changed by that commit.
2. Search the entire repository—not only `claude_hooks/`—for all direct `import psycopg`, `from psycopg ... import ...`, and dynamic `import_module("psycopg")` variants, recording every exact file:line; the two confirmed direct imports currently are `claude_hooks/providers/pgvector.py:123` and `claude_hooks/providers/pgvector.py:328`.
3. Compare every discovered import site with the fallback introduced by `4e67dc2`, checking both the missing-`psycopg` branch and whether the fallback is actually invoked rather than merely defined elsewhere.
4. Trace callers of `PgvectorProvider.verify` and `PgvectorProvider._ensure_ready` from repository entry points and tests, beginning at `claude_hooks/providers/pgvector.py:121` and `claude_hooks/providers/pgvector.py:326`, to determine whether each import site can execute under a Python interpreter outside the `claude-hooks` conda environment.
5. Inspect installation, daemon, hook, and launcher code that selects `sys.executable` or the conda environment, including `bin/claude-hook*`, `run.py`, `run_daemon.py`, `install.py`, and `claude_hooks/daemon*.py`, to distinguish production paths guaranteed to run in the conda env from generic or directly invokable paths that can run in a non-conda-env Python.
6. Inspect relevant tests for pgvector/provider detection, verification, and provider factory dispatch, then identify whether they instantiate `PgvectorProvider` directly or execute a public hook/CLI path, because that determines whether `claude_hooks/providers/pgvector.py:123` and `claude_hooks/providers/pgvector.py:328` are independently exercisable from a non-conda-env Python.
7. Produce the final audit table with each lacking import site as `file:line`, the fallback status, the concrete caller/entry point, and a justified “exercisable” or “not exercisable” verdict; explicitly flag repository-wide search results that remain unchecked if the expanded search cannot be completed.

---

## Researcher (round 1)

- **Current fallback pattern:** `find_conda_env_python` is defined at `install.py:101`; the installer first tries the current interpreter’s `import psycopg` at `install.py:2127`, catches `ImportError` at `install.py:2135`, resolves the conda interpreter at `install.py:2140`, and shells out via `subprocess.run` at `install.py:2146`. The same pattern is present for schema detection at `install.py:2524` and `install.py:2540`, and schema initialization at `install.py:2647` and `install.py:2657`.

- **`claude_hooks/providers/pgvector.py:123` — lacks a conda subprocess fallback.** `PgvectorProvider.verify` imports `psycopg` directly, catches `ImportError`, and returns `False` at `claude_hooks/providers/pgvector.py:124`. Through the installer’s normal missing-`psycopg` path, this site is **not reached**: the outer import at `install.py:2127` fails before `PgvectorProvider.verify` is called at `install.py:2132`; the conda child instead reaches the script import at `install.py:2092`. The site remains **directly exercisable** if another caller invokes `PgvectorProvider.verify` under system Python; no such additional repository caller was verified.

- **`claude_hooks/providers/pgvector.py:328` — lacks a conda subprocess fallback.** `_ensure_ready` imports `psycopg` directly and raises `ImportError` at `claude_hooks/providers/pgvector.py:329`. `recall` invokes `_ensure_ready` at `claude_hooks/providers/pgvector.py:148` and catches the error at `claude_hooks/providers/pgvector.py:149`, so this import is **exercisable from a non-conda interpreter whenever `PgvectorProvider.recall` is invoked**; it degrades to an empty recall rather than using the conda environment. Normal hook reachability through provider registration/config was not fully traced.

- **`scripts/migrate_to_pgvector.py:624` — lacks a conda subprocess fallback.** The standalone `main` function begins at `scripts/migrate_to_pgvector.py:610`, imports `psycopg` unguarded at `scripts/migrate_to_pgvector.py:624`, and immediately connects at `scripts/migrate_to_pgvector.py:626`. It is **directly exercisable and fails** when this script is run with a non-conda Python lacking `psycopg`.

- **`scripts/bench_recall.py:107` — lacks a conda subprocess fallback.** `PgvectorBenchProvider.__init__` imports `psycopg` directly at `scripts/bench_recall.py:107` and connects at `scripts/bench_recall.py:108`. It is **directly exercisable and fails** when the benchmark is run with a non-conda Python lacking `psycopg`.

- **Test-only import sites:** `tests/test_pgvector_integration.py:56` and `tests/test_pgvector_integration.py:285` contain direct imports. Their surrounding guards were not read, so whether a missing dependency causes skip/failure versus successful fallback is **not established**.

- **Import sites that already have the intended fallback, not findings of omission:** current-interpreter imports at `install.py:2127`, `install.py:2524`, and `install.py:2647`; the corresponding child-process script imports at `install.py:2092`, `install.py:2546`, and `install.py:2666`.

- **Inventory completeness:** repository grep found 12 textual `psycopg` import occurrences: six in `install.py`, two in `claude_hooks/providers/pgvector.py`, two in `scripts/migrate_to_pgvector.py` and `scripts/bench_recall.py`, and two in `tests/test_pgvector_integration.py`. No `from psycopg ...` or `import_module("psycopg")` occurrence was found; binary consultant database matches were excluded.

- **Evidence gap:** the diff for commit `4e67dc2` itself could not be inspected because tool access stopped before `git show`; the fallback description above is verified from the current working tree rather than from that commit’s diff.

---

## Researcher (round 1)

- **Lacking fallback — `claude_hooks/providers/pgvector.py:123`**: `PgvectorProvider.verify()` directly imports `psycopg`; `ImportError` only logs and returns `False`, with no conda subprocess fallback. This site is independently exercisable from a non-conda Python by calling the public classmethod directly; the integration test does exactly that at `tests/test_pgvector_integration.py:186`. However, the normal installer fast path does not expose this failure: `install.py:2127` first imports `psycopg`, and on `ImportError` the function locates the conda Python and shells out at `install.py:2138`-`install.py:2148`. **Verdict: exercisable, but bypassed by the installer's fallback.**

- **Lacking fallback — `claude_hooks/providers/pgvector.py:328`**: `_ensure_ready()` directly imports `psycopg`; `ImportError` is re-raised as an install instruction with no subprocess fallback at `claude_hooks/providers/pgvector.py:329`-`claude_hooks/providers/pgvector.py:334`. The site is reached by `recall()` through `_ensure_ready()` at `claude_hooks/providers/pgvector.py:144`-`claude_hooks/providers/pgvector.py:150`; `recall()` catches the resulting `ImportError` and returns no results rather than blocking. Provider dispatch is lazy and can construct this class in any interpreter via `get_provider_class()` at `claude_hooks/providers/__init__.py:43`-`claude_hooks/providers/__init__.py:53`. **Verdict: exercisable from a non-conda Python.**

- **The normal hook runtime is not guaranteed to be the conda environment.** The shared shell resolver prefers a repo-local venv before conda and explicitly permits system `python3`/`python` as its final fallback at `bin/_resolve_python.sh:6`-`bin/_resolve_python.sh:14` and `bin/_resolve_python.sh:53`-`bin/_resolve_python.sh:58`. `run.py` itself can also be run directly with any Python, and dispatches in-process when the daemon cannot serve the event at `run.py:20`-`run.py:33` and `run.py:37`-`run.py:57`. Thus both a repo venv and the system-Python fallback are non-conda environments capable of reaching `claude_hooks/providers/pgvector.py:328` when pgvector is enabled.

- **The pgvector MCP launcher is not unconditionally conda-bound.** The installer chooses the resolved conda Python when present but otherwise uses the current interpreter at `install.py:2296`-`install.py:2297`. The MCP server constructs providers through `build_providers()` at `claude_hooks/pgvector_mcp/server.py:340`-`claude_hooks/pgvector_mcp/server.py:347`; a subsequently selected `PgvectorProvider` can therefore reach `_ensure_ready()` and its unguarded import at `claude_hooks/providers/pgvector.py:328` in that interpreter.

- **Integration tests do not demonstrate failure without `psycopg`; they skip instead.** The integration setup explicitly skips when the import fails at `tests/test_pgvector_integration.py:54`-`tests/test_pgvector_integration.py:58`. Nevertheless, the test suite contains a direct, real-provider construction at `tests/test_pgvector_integration.py:97`-`tests/test_pgvector_integration.py:116`, and direct `PgvectorProvider.verify()` invocation at `tests/test_pgvector_integration.py:175`-`tests/test_pgvector_integration.py:186`; these are concrete direct exercises when `psycopg` is installed, while a non-conda interpreter without it is merely skipped.

- **Additional lacking production import sites discovered outside `claude_hooks/`**:
  - `scripts/migrate_to_pgvector.py:624` imports `psycopg` directly and uses it at `scripts/migrate_to_pgvector.py:626`, with no fallback visible around that import. This is directly exercisable by running the migration script with a non-conda Python lacking `psycopg`.
  - `scripts/bench_recall.py:107` imports `psycopg` in `PgvectorBenchProvider.__init__()` and connects at `scripts/bench_recall.py:108`, with no fallback. This is directly exercisable by running the benchmark from a non-conda Python lacking `psycopg`.
  - **Verdict for both: exercisable**, assuming those standalone scripts are in the audit scope; they are not routed through the installer/provider fallback.

- **Install-time import sites already have the requested fallback and should not be reported as lacking it**:
  - `install.py:2127` falls back to the conda subprocess at `install.py:2138`-`install.py:2148`.
  - `install.py:2524` has the corresponding schema-table probe fallback beginning at `install.py:2539`.
  - `install.py:2647` has the schema-initialization fallback beginning at `install.py:2656`-`install.py:2675`.
  - The embedded imports at `install.py:2092`, `install.py:2546`, and `install.py:2666` are payloads intentionally executed by the selected conda subprocess, so a nested fallback is unnecessary.

- **Test-only imports:** `tests/test_pgvector_integration.py:56` and `tests/test_pgvector_integration.py:285` import `psycopg` without a fallback, but the first is explicitly a dependency probe/skip gate and the second is cleanup within the same integration-test environment. They are not production import sites.

- **Coverage caveat:** exact searches covered `claude_hooks/`, `claude_hooks/pgvector_mcp/`, `scripts/`, `tests/`, `install.py`, and `consultants/`; the repository-wide grep result was harness-truncated, and the `4e67dc2` commit itself was not inspected. Other top-level trees were not individually re-searched before tool access was disabled.

---

## Synthesizer

Five import sites are reachable from a non-conda Python lacking `psycopg`; one additional test-only site is blocked by the preceding dependency skip. The two `claude_hooks` production sites both still lack the subprocess fallback.

| Import site | Fallback status | Exercisable from non-conda Python? | Reachability |
|---|---|---|---|
| `claude_hooks/providers/pgvector.py:123` | **Lacks fallback.** `ImportError` is caught and `verify()` returns `False` rather than invoking the conda interpreter (`claude_hooks/providers/pgvector.py:124`, `claude_hooks/providers/pgvector.py:126`). | **Yes.** `PgvectorProvider.verify()` is public and directly invoked by the integration test at `tests/test_pgvector_integration.py:186`. A non-conda interpreter without `psycopg` therefore reaches the import and gets `False`. | The normal installer path does not reach this site when `psycopg` is missing: its own import fails first at `install.py:2127`, then the installer selects the conda Python at `install.py:2140` and runs the subprocess at `install.py:2146`. Thus the site is directly exercisable but bypassed by the installer fallback. |
| `claude_hooks/providers/pgvector.py:328` | **Lacks fallback.** The missing-package `ImportError` is converted to an install instruction at `claude_hooks/providers/pgvector.py:330`; no conda subprocess is used. | **Yes.** `recall()` calls `_ensure_ready()` at `claude_hooks/providers/pgvector.py:148` and catches the resulting `ImportError` at `claude_hooks/providers/pgvector.py:149`, returning no results. | `PgvectorProvider` can be resolved lazily through `get_provider_class()` at `claude_hooks/providers/__init__.py:43` and `claude_hooks/providers/__init__.py:53`. The runtime resolver prefers a repo venv over conda and ultimately permits system `python3`/`python` (`bin/_resolve_python.sh:6`, `bin/_resolve_python.sh:12`, `bin/_resolve_python.sh:14`, `bin/_resolve_python.sh:53`), so a non-conda interpreter can reach this path. |
| `scripts/migrate_to_pgvector.py:624` | **Lacks fallback.** The standalone script imports `psycopg` directly and immediately connects at `scripts/migrate_to_pgvector.py:626`. | **Yes.** Running the migration script with a non-conda Python lacking `psycopg` reaches the import and fails. | Direct script entry point; it is not routed through the installer’s conda subprocess. |
| `scripts/bench_recall.py:107` | **Lacks fallback.** `PgvectorBenchProvider.__init__` imports `psycopg` and immediately connects at `scripts/bench_recall.py:108`. | **Yes.** Running the benchmark with a non-conda Python lacking `psycopg` reaches the import and fails. | Direct benchmark entry point; it is not routed through the installer’s conda subprocess. |
| `tests/test_pgvector_integration.py:56` | **Lacks fallback, intentionally as a dependency probe.** A missing import raises `unittest.SkipTest` at `tests/test_pgvector_integration.py:58`. | **Yes, but non-failing.** The import executes in a non-conda interpreter; missing `psycopg` converts the test into a skip rather than a fallback. | The helper is called during class setup at `tests/test_pgvector_integration.py:95`. |
| `tests/test_pgvector_integration.py:285` | **Lacks fallback.** This test-only import has no subprocess fallback. | **Not in the missing-`psycopg` case.** The earlier dependency probe skips the integration suite when `psycopg` is absent (`tests/test_pgvector_integration.py:56`, `tests/test_pgvector_integration.py:58`), so this later cleanup import is not reached. It is reachable in a non-conda environment that already has `psycopg` and the other integration dependencies. | Test cleanup only; not a production fallback defect. |

Install-time matches are **not omissions**:

- `install.py:2127` has the missing-import branch and conda subprocess fallback at `install.py:2135`, `install.py:2140`, and `install.py:2146`.
- `install.py:2524` has the corresponding schema-probe fallback beginning at `install.py:2539`.
- `install.py:2647` has the corresponding schema-initialization fallback beginning at `install.py:2656`.
- `install.py:2092`, `install.py:2546`, and `install.py:2666` are payload imports intended to execute inside the selected conda subprocess, so a nested fallback is unnecessary.

The current tree contains 12 source import occurrences: the six lacking sites above and the six install-time occurrences. No `from psycopg ...` or dynamic `import_module("psycopg")` source import was found. The available evidence verifies the current fallback implementation, but the `4e67dc2` commit diff itself could not be inspected, so equivalence to that exact historical diff remains unverified.

---
