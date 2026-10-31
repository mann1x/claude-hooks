# Consultation transcript — csl-2026-10-22-1720-75d8

_Question_: Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python.


## Planner

1. Enumerate every `psycopg` import under `claude_hooks/` and classify whether it is an in-process import or embedded in a subprocess-only script, using the confirmed sites `claude_hooks/providers/pgvector.py:123` and `claude_hooks/providers/pgvector.py:328`.

2. Inspect `claude_hooks/providers/pgvector.py:121-139` and `claude_hooks/providers/pgvector.py:320-336` to determine the immediate behavior of each site when the running interpreter lacks `psycopg`, and whether either branch shells out to a conda-env Python.

3. Trace `PgvectorProvider.verify` callers in `install.py:2115-2149` and `install.py:3544-3558` to establish whether the import at `claude_hooks/providers/pgvector.py:123` is reachable from a non-conda Python through the installer or only through direct API/test calls.

4. Trace runtime pgvector construction and operations through `claude_hooks/dispatcher.py:131-137`, `claude_hooks/pgvector_mcp/server.py:267-299`, and the provider methods that call `_ensure_ready` at `claude_hooks/providers/pgvector.py:144-158`, `claude_hooks/providers/pgvector.py:210-235`, and `claude_hooks/providers/pgvector.py:255-315` to determine whether the import at `claude_hooks/providers/pgvector.py:328` is genuinely exercisable by a non-conda interpreter.

5. Inspect the interpreter-selection paths `bin/_resolve_python.sh:1-17`, `bin/_resolve_python.sh:31-57`, and `bin/claude-hook:13-23`, plus the generated MCP launcher logic `install.py:2294-2304` and `install.py:2354-2377`, to distinguish hooks/daemon launches that can select a repo `.venv` or system Python from the MCP launcher that is explicitly pinned to the conda env.

6. Compare the confirmed fallback pattern in `install.py:2125-2160` and `install.py:2522-2560` against every confirmed package import site, then report each deficient `file:line` separately with a concrete yes/no assessment of non-conda exercisability and the relevant entry point or exception to the normal path.

---

## Researcher (round 1)

- **Import inventory under `claude_hooks/`:** exactly two `psycopg` import sites, both in-process lazy imports in `claude_hooks/providers/pgvector.py`; there are no embedded/subprocess-only `psycopg` imports elsewhere in the package (`claude_hooks/providers/pgvector.py:123`, `claude_hooks/providers/pgvector.py:328`).

- **`claude_hooks/providers/pgvector.py:123` — `PgvectorProvider.verify`:** an in-process import. If the current interpreter lacks `psycopg`, it logs a warning and returns `False`; it does **not** invoke a conda-env subprocess (`claude_hooks/providers/pgvector.py:121`, `claude_hooks/providers/pgvector.py:123`, `claude_hooks/providers/pgvector.py:125`). Connection or verification exceptions are likewise converted to `False` (`claude_hooks/providers/pgvector.py:137`).

- **Installer reachability for `claude_hooks/providers/pgvector.py:123`:** from a non-conda interpreter lacking `psycopg`, the installer does **not** reach the provider method. `_verify_pgvector_dsn` first imports `psycopg` itself; only that successful fast path calls `PgvectorProvider.verify` (`install.py:2125`, `install.py:2127`, `install.py:2132`). On `ImportError`, it instead executes `_PGVECTOR_VERIFY_SCRIPT` using the conda Python (`install.py:2135`, `install.py:2138`, `install.py:2140`, `install.py:2146`). The generic installer verification loop also explicitly skips `pgvector`, whose setup is bespoke (`install.py:3540`, `install.py:3547`, `install.py:3566`).

- **Can `claude_hooks/providers/pgvector.py:123` actually be exercised from non-conda Python?** Yes, but only through a direct `PgvectorProvider.verify(...)` API or test call—not through the normal installer path when `psycopg` is absent. The integration suite calls it directly (`tests/test_pgvector_integration.py:186`).

- **`claude_hooks/providers/pgvector.py:328` — `PgvectorProvider._ensure_ready`:** an in-process import used to create the provider’s live Postgres connection. Missing `psycopg` raises `ImportError("install psycopg: ...")`; there is no subprocess or conda fallback in this branch (`claude_hooks/providers/pgvector.py:320`, `claude_hooks/providers/pgvector.py:326`, `claude_hooks/providers/pgvector.py:328`, `claude_hooks/providers/pgvector.py:330`). Recall catches this and returns no results (`claude_hooks/providers/pgvector.py:147`, `claude_hooks/providers/pgvector.py:149`); store converts it to a runtime failure (`claude_hooks/providers/pgvector.py:216`).

- **Can `claude_hooks/providers/pgvector.py:328` actually be exercised from non-conda Python?** Yes: direct provider recall/store calls execute it in-process. It is also operationally reachable when the pgvector MCP launcher runs under a non-conda interpreter: the installer bakes in the conda Python when present, otherwise the current `sys.executable` (`install.py:2294`, `install.py:2296`, `install.py:2303`). Standard shell shims prefer the conda environment, so the usual hook path avoids this case unless conda is absent, the interpreter is overridden, or the API is called directly (`bin/_resolve_python.sh:12`, `bin/_resolve_python.sh:31`, `bin/_resolve_python.sh:53`).

- **Repository-wide scope note:** outside the requested `claude_hooks/` package, two additional direct, unguarded imports exist in standalone scripts and are likewise exercisable from a non-conda interpreter: `scripts/migrate_to_pgvector.py:624` and `scripts/bench_recall.py:107`. The `install.py` import sites are fast paths paired with conda subprocess fallbacks rather than remaining gaps (`install.py:2127`, `install.py:2146`, `install.py:2524`, `install.py:2647`).

---

## Researcher (round 1)

- `claude_hooks/providers/pgvector.py:328` — **YES, deficient and runtime-exercisable from non-conda Python.** `recall()`, `store()`, `batch_recall()`, and `batch_store()` call `_ensure_ready()` (`claude_hooks/providers/pgvector.py:148`, `claude_hooks/providers/pgvector.py:214`, `claude_hooks/providers/pgvector.py:265`, `claude_hooks/providers/pgvector.py:292`); it imports `psycopg` directly at `claude_hooks/providers/pgvector.py:328` and raises `ImportError` when unavailable (`claude_hooks/providers/pgvector.py:329`). There is no conda subprocess fallback. The hook launcher can select a repo `.venv` before conda and a system `python3`/`python` after conda (`bin/_resolve_python.sh:31`, `bin/_resolve_python.sh:53`), then executes that interpreter (`bin/claude-hook:18`, `bin/claude-hook:23`). Thus a non-conda interpreter lacking `psycopg` can reach this import when pgvector is enabled. Recall degrades to an empty result (`claude_hooks/providers/pgvector.py:147`); store converts the failure to `RuntimeError` (`claude_hooks/providers/pgvector.py:213`).

- `claude_hooks/providers/pgvector.py:123` — **YES only as a direct `PgvectorProvider.verify()` call; NO through the confirmed installer fallback path.** The method imports `psycopg` directly and returns `False` when absent (`claude_hooks/providers/pgvector.py:121`). However, `install._verify_pgvector_dsn()` imports `psycopg` first and only calls `PgvectorProvider.verify()` inside that guarded fast path (`install.py:2125`, `install.py:2127`, `install.py:2132`); when the current interpreter lacks `psycopg`, it shells out to the conda interpreter instead (`install.py:2138`, `install.py:2140`, `install.py:2146`). No unguarded non-conda runtime caller was confirmed.

- `scripts/migrate_to_pgvector.py:624` — **YES, deficient.** The migration script directly imports `psycopg` and connects with it (`scripts/migrate_to_pgvector.py:624`, `scripts/migrate_to_pgvector.py:626`). Running this script under a system or repo-venv Python without `psycopg` reaches the import and fails; there is no conda subprocess fallback.

- `scripts/bench_recall.py:107` — **YES, deficient.** `PgvectorBenchProvider.__init__` directly imports `psycopg` (`scripts/bench_recall.py:105`, `scripts/bench_recall.py:107`) and connects immediately (`scripts/bench_recall.py:108`). It is exercisable from any non-conda interpreter used to run the benchmark, with no fallback.

- `tests/test_pgvector_integration.py:56` — **Test-only, intentionally non-fatal.** A non-conda Python without `psycopg` reaches this import, but it raises `unittest.SkipTest` rather than exercising runtime pgvector behavior (`tests/test_pgvector_integration.py:56`, `tests/test_pgvector_integration.py:58`).

- `tests/test_pgvector_integration.py:285` — **Test-only cleanup import.** The direct import is present (`tests/test_pgvector_integration.py:285`), but the integration setup first skips when `psycopg` is unavailable (`tests/test_pgvector_integration.py:56`); this is not a production fallback gap.

- `install.py:2127`, `install.py:2524`, `install.py:2647` — **Not deficient.** These are guarded current-interpreter fast paths; the confirmed conda-subprocess fallbacks begin at `install.py:2138`, `install.py:2543`, and `install.py:2657`, respectively.

- `install.py:2092`, `install.py:2546`, `install.py:2666` — **Not deficient standalone sites.** These imports belong to embedded probe/DDL scripts executed through the conda subprocess paths at `install.py:2146`, `install.py:2543`, and `install.py:2657`, rather than being unguarded imports in the calling interpreter.

- `bin/claude-hook-pgvector-mcp:10` — The checked-in MCP shim also sources the general resolver, which can select `.venv` or system Python; it is not itself conda-pinned (`bin/claude-hook-pgvector-mcp:10`, `bin/_resolve_python.sh:31`, `bin/_resolve_python.sh:53`). The generated `~/.local/bin/pgvector-mcp` launcher’s exact interpreter-pinning logic at `install.py:2294`–`install.py:2377` was not verified after filesystem tools became unavailable, so generated-launcher reachability remains open.

---

## Synthesizer

Two deficient `psycopg` imports exist under `claude_hooks/`: `claude_hooks/providers/pgvector.py:328` is a real runtime gap; `claude_hooks/providers/pgvector.py:123` is reachable only through a direct `verify()` call because the installer normally substitutes the conda subprocess.

- **`claude_hooks/providers/pgvector.py:123` — `PgvectorProvider.verify()`**
  - **Fallback status:** Deficient. A missing `psycopg` is caught locally and converted to `False`; there is no conda subprocess from this method (`claude_hooks/providers/pgvector.py:123`, `claude_hooks/providers/pgvector.py:124`, `claude_hooks/providers/pgvector.py:125`).
  - **Exercisable from a non-conda Python?** **Yes, but only by directly calling `PgvectorProvider.verify(...)`; no through the normal installer path.** The integration test directly calls it at `tests/test_pgvector_integration.py:186`, although the test class skips when `psycopg` is absent (`tests/test_pgvector_integration.py:56`, `tests/test_pgvector_integration.py:58`, `tests/test_pgvector_integration.py:95`).
  - **Why the installer does not expose the gap:** `_verify_pgvector_dsn()` first imports `psycopg` in the current interpreter and only then calls `PgvectorProvider.verify()` (`install.py:2125`, `install.py:2127`, `install.py:2132`). An `ImportError` exits that fast path and invokes the conda-Python subprocess instead (`install.py:2135`, `install.py:2138`, `install.py:2140`, `install.py:2146`). The generic provider-verification loop also skips pgvector (`install.py:3540`, `install.py:3547`, `install.py:3566`).
  - **Assessment:** A real import-site gap, but operationally shielded in the installer; only a direct API caller from a psycopg-less interpreter reaches it.

- **`claude_hooks/providers/pgvector.py:328` — `PgvectorProvider._ensure_ready()`**
  - **Fallback status:** Deficient and operationally significant. A missing `psycopg` becomes `ImportError("install psycopg: ...")`; there is no conda subprocess (`claude_hooks/providers/pgvector.py:328`, `claude_hooks/providers/pgvector.py:329`, `claude_hooks/providers/pgvector.py:330`).
  - **Exercisable from a non-conda Python?** **Yes.** It is reached by normal provider operations: `recall()` at `claude_hooks/providers/pgvector.py:148`, `store()` at `claude_hooks/providers/pgvector.py:214`, `batch_recall()` at `claude_hooks/providers/pgvector.py:265`, and `batch_store()` at `claude_hooks/providers/pgvector.py:292`. Runtime code invokes provider recall/store operations, for example `claude_hooks/recall.py:86` and `claude_hooks/store_async.py:172`; the MCP server does likewise at `claude_hooks/pgvector_mcp/server.py:271` and `claude_hooks/pgvector_mcp/server.py:280`.
  - **Why a non-conda interpreter can select it:** The shared resolver accepts an explicit interpreter override (`bin/_resolve_python.sh:25`), prefers a repo `.venv` over conda (`bin/_resolve_python.sh:31`, `bin/_resolve_python.sh:32`), and otherwise falls back to system `python3`/`python` when no candidate is found (`bin/_resolve_python.sh:53`, `bin/_resolve_python.sh:54`, `bin/_resolve_python.sh:56`). The hook executes under that selected interpreter (`bin/claude-hook:18`, `bin/claude-hook:23`). The checked-in MCP shim uses the same resolver (`bin/claude-hook-pgvector-mcp:10`, `bin/claude-hook-pgvector-mcp:17`).
  - **Generated launcher exception:** The installed launcher is conda-pinned only when the conda interpreter exists; otherwise it bakes in the installer’s `sys.executable` (`install.py:2296`, `install.py:2297`, `install.py:2303`) and executes it directly (`install.py:2375`). Thus it can also be a non-conda interpreter when conda is absent.
  - **Observed failure mode:** Recall catches the import failure and returns no results (`claude_hooks/providers/pgvector.py:149`, `claude_hooks/providers/pgvector.py:151`); store converts it to a runtime failure (`claude_hooks/providers/pgvector.py:216`, `claude_hooks/providers/pgvector.py:217`). Batch recall and batch store behave analogously (`claude_hooks/providers/pgvector.py:266`, `claude_hooks/providers/pgvector.py:268`, `claude_hooks/providers/pgvector.py:293`, `claude_hooks/providers/pgvector.py:294`).
  - **Assessment:** **Genuine, normally reachable runtime gap.**

Other repository-wide `psycopg` imports do not alter that package-level result:

- If “claude-hooks” means the entire repository, two additional production-script gaps are exercisable from a non-conda interpreter: `scripts/migrate_to_pgvector.py:624` and `scripts/bench_recall.py:107`. Both import and use `psycopg` directly without a fallback (`scripts/migrate_to_pgvector.py:626`, `scripts/bench_recall.py:108`).
- Installer imports at `install.py:2127`, `install.py:2524`, and `install.py:2647` are guarded fast paths with conda subprocess fallbacks. The embedded-script imports at `install.py:2092`, `install.py:2546`, and `install.py:2666` execute in those subprocesses.
- Test imports at `tests/test_pgvector_integration.py:56` and `tests/test_pgvector_integration.py:285` are test dependency/cleanup code, not production runtime fallbacks.

---
