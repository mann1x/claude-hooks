# Consultation transcript — csl-2026-10-22-1749-23e7

_Question_: Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python.


## Planner

1. Establish the reference change by inspecting commit `4e67dc2` and enumerate every repository `import psycopg`/`from psycopg` occurrence, distinguishing executable imports from psycopg code embedded in subprocess script strings; the confirmed candidate executable sites outside `install.py` are `claude_hooks/providers/pgvector.py:123`, `claude_hooks/providers/pgvector.py:328`, `scripts/migrate_to_pgvector.py:624`, and `scripts/bench_recall.py:107`.

2. Verify which sites already receive the commit’s fallback by tracing `install.py:2115-2155`, `install.py:2520-2570`, and `install.py:2630-2680`; do not report `install.py:2127`, `install.py:2524`, or `install.py:2647` as missing if their enclosing functions retain the documented conda-subprocess paths at `install.py:2138-2147`, `install.py:2539-2558`, and `install.py:2656-2674`.

3. Classify `claude_hooks/providers/pgvector.py:123` by tracing all callers of `PgvectorProvider.verify`: confirm whether it is reachable only through `install.py:2132`—whose `_verify_pgvector_dsn` catches the missing import and falls back—or through any independent runtime path, then report the exact non-conda-Python reachability.

4. Classify `claude_hooks/providers/pgvector.py:328` by tracing `_ensure_ready` callers through `PgvectorProvider.recall`, `store`, `count`, batch methods, `claude_hooks/dispatcher.py:131-137`, and `claude_hooks/pgvector_mcp/server.py:341-348`; determine whether system/`.venv` Python can reach it when the `claude-hooks` conda environment contains psycopg but the current interpreter does not, noting that the normal POSIX launcher prefers conda after `.venv` (`bin/_resolve_python.sh:30-55`).

5. Classify `scripts/migrate_to_pgvector.py:624` and `scripts/bench_recall.py:107` by locating their CLI entry points and invocation instructions, then determine whether either is a supported route that can actually be launched by a non-conda Python rather than merely an import-time/test artifact.

6. Check `tests/test_pgvector_integration.py:56` and `tests/test_pgvector_integration.py:285` for pytest skip guards and determine whether they are runtime import sites or test-only probes; exclude them from the production audit if their failure is explicitly skipped when psycopg is absent.

7. Produce the final audit as an exhaustive table with columns `file:line`, surrounding function/import purpose, whether the 4e67dc2-style conda subprocess fallback exists, whether the site is executable under a non-conda interpreter, and the concrete call path or reason it is not exercisable.

---

## Researcher (round 1)

- **Reference-change limitation:** I could not independently inspect git commit `4e67dc2` because the available tools do not expose git history. I verified the corresponding current-source fallback pattern in `install.py`.

- **`install.py:2127` — not missing.** `_verify_pgvector_dsn` tries the current interpreter first, catches `ImportError`, then runs a child interpreter found by `find_conda_env_python()` with the pgvector verification script: `install.py:2115`, `install.py:2127`, `install.py:2135`, `install.py:2138`, `install.py:2146`. The embedded import at `install.py:2092` is subprocess script text, not a parent-process import site.

- **`install.py:2524` — not missing.** `_pgvector_tables_present` catches a missing current-interpreter `psycopg` and falls back to the conda Python subprocess: `install.py:2515`, `install.py:2524`, `install.py:2536`, `install.py:2539`, `install.py:2557`. The import at `install.py:2546` is inside that child script.

- **`install.py:2647` — not missing.** `_init_pgvector_schema` catches the fast-path `ImportError` and executes the schema DDL through the conda Python subprocess: `install.py:2631`, `install.py:2647`, `install.py:2653`, `install.py:2656`, `install.py:2673`. The import at `install.py:2666` is inside the child script.

- **`claude_hooks/providers/pgvector.py:123` — lacks a local fallback, but is not reached by the normal installer flow from a Python without `psycopg`.** `PgvectorProvider.verify` imports `psycopg` locally and returns `False` on `ImportError`: `claude_hooks/providers/pgvector.py:121`, `claude_hooks/providers/pgvector.py:123`, `claude_hooks/providers/pgvector.py:124`. The sole repository production call found is `install.py:2132`; there, the preceding current-interpreter import at `install.py:2127` fails first, is caught at `install.py:2135`, and the installer uses its conda subprocess at `install.py:2138` and `install.py:2146`. Thus the method is directly exercisable by external code calling the public method, but the normal installer does not execute line 123 under the reported non-conda condition.

- **`claude_hooks/providers/pgvector.py:328` — missing fallback and genuinely reachable from a non-conda Python.** `_ensure_ready` directly imports `psycopg` and raises `ImportError` when absent: `claude_hooks/providers/pgvector.py:320`, `claude_hooks/providers/pgvector.py:328`, `claude_hooks/providers/pgvector.py:329`. It is reached by `recall` and `store`: `claude_hooks/providers/pgvector.py:144`, `claude_hooks/providers/pgvector.py:148`, `claude_hooks/providers/pgvector.py:210`, `claude_hooks/providers/pgvector.py:214`; the MCP server directly invokes those methods: `claude_hooks/pgvector_mcp/server.py:271`, `claude_hooks/pgvector_mcp/server.py:280`. The dispatcher also constructs an enabled, DSN-configured pgvector provider: `claude_hooks/dispatcher.py:131`, `claude_hooks/dispatcher.py:136`, `claude_hooks/dispatcher.py:140`, `claude_hooks/dispatcher.py:148`. The POSIX MCP launcher selects the interpreter through the shared resolver: `bin/claude-hook-pgvector-mcp:10`, `bin/claude-hook-pgvector-mcp:17`; that resolver prefers a repo `.venv` before conda: `bin/_resolve_python.sh:30`, `bin/_resolve_python.sh:32`, `bin/_resolve_python.sh:35`. Therefore a `.venv` lacking `psycopg` can reach line 328 even when the separate conda environment contains it.

- **`scripts/migrate_to_pgvector.py:624` — missing fallback and directly exercisable from a non-conda Python.** The standalone script’s `main` unconditionally imports `psycopg` after configuration and before opening the connection: `scripts/migrate_to_pgvector.py:610`, `scripts/migrate_to_pgvector.py:616`, `scripts/migrate_to_pgvector.py:624`, `scripts/migrate_to_pgvector.py:626`; its module entry point invokes `main`: `scripts/migrate_to_pgvector.py:653`. `install.py` imports only the schema helper from this module at `install.py:2639`, so that import does not execute the function-local import at line 624.

- **`scripts/bench_recall.py:107` — missing fallback and directly exercisable from a non-conda Python.** `PgvectorBenchProvider.__init__` imports `psycopg` and immediately opens a connection: `scripts/bench_recall.py:105`, `scripts/bench_recall.py:107`, `scripts/bench_recall.py:108`. No subprocess fallback is present in this constructor.

- **Test-only imports are not production gaps.** `tests/test_pgvector_integration.py:56` is a dependency probe that skips when `psycopg` is unavailable, while `tests/test_pgvector_integration.py:285` is inside integration-test teardown. Neither is a production runtime import site requiring the installer’s subprocess behavior.

---

## Researcher (round 1)

- **Reference fallback pattern:** `4e67dc2` catches a missing current-interpreter `psycopg`, locates the `claude-hooks` conda interpreter, and retries via `subprocess.run` (`install.py:2115`, `install.py:2135`, `install.py:2140`, `install.py:2146`).

| `file:line` | Purpose | Conda subprocess fallback? | Exercisable under non-conda Python? | Evidence / call path |
|---|---|---:|---:|---|
| `scripts/migrate_to_pgvector.py:624` | Imports `psycopg` inside `main()` and immediately opens a database connection. | **No** | **Yes** | This is a real runtime CLI path: `parse_args()` (`scripts/migrate_to_pgvector.py:584`) → `main()` (`scripts/migrate_to_pgvector.py:610`) → unguarded `import psycopg` (`scripts/migrate_to_pgvector.py:624`) → `psycopg.connect()` (`scripts/migrate_to_pgvector.py:626`). The module has a direct `__main__` launcher (`scripts/migrate_to_pgvector.py:653`). A non-conda Python lacking `psycopg` reaches the import and raises an uncaught `ModuleNotFoundError`; there is no `try/except ImportError` or conda retry. |
| `scripts/bench_recall.py:107` | Imports `psycopg` inside `PgvectorBenchProvider.__init__`, then connects to PostgreSQL. | **No** | **Yes, when the provider is constructed** | `PgvectorBenchProvider(...)` (`scripts/bench_recall.py:105`) → unguarded `import psycopg` (`scripts/bench_recall.py:107`) → `psycopg.connect(dsn)` (`scripts/bench_recall.py:108`). Because the import is function-local, importing the module alone does not trigger it, but a non-conda interpreter invoking this benchmark provider does. A non-conda interpreter without `psycopg` gets an uncaught import error. **Evidence limitation:** the lower-file CLI dispatch could not be read, so a repository-documented invocation route for constructing this provider was not verified. |
| `tests/test_pgvector_integration.py:56` | Dependency probe in `_skip_if_no_deps()`. | **Not needed** | **Test execution is skipped when `psycopg` is absent** | Missing `psycopg` is converted to `unittest.SkipTest("psycopg not installed")` (`tests/test_pgvector_integration.py:54`, `tests/test_pgvector_integration.py:56`, `tests/test_pgvector_integration.py:58`). The helper runs from `setUpClass()` (`tests/test_pgvector_integration.py:92`, `tests/test_pgvector_integration.py:95`). This is a test-only probe, not a production import site. |
| `tests/test_pgvector_integration.py:285` | Imports `psycopg` in `tearDownClass()` to remove test tables. | **Not needed** | **No independent production path; harmless if called without `psycopg`** | The import occurs inside cleanup (`tests/test_pgvector_integration.py:281`, `tests/test_pgvector_integration.py:285`), and all exceptions—including `ImportError`—are swallowed (`tests/test_pgvector_integration.py:293`). Moreover, when the earlier dependency probe skips the class, `tearDownClass()` is not needed. This is test-only and should be excluded from the production audit. |

- **Lane conclusion:** the two production-adjacent script sites are genuine runtime imports and should be listed:
  - `scripts/migrate_to_pgvector.py:624` — definitely reachable through the script’s direct CLI entry.
  - `scripts/bench_recall.py:107` — reachable whenever the benchmark provider is instantiated; the exact documented CLI call path remains unverified.
- The two `tests/test_pgvector_integration.py` sites are explicitly skip-guarded or cleanup-only and are not missing-fallback production defects.

---

## Synthesizer

Three production import sites still lack the conda subprocess fallback and are reachable from a Python without `psycopg`: `claude_hooks/providers/pgvector.py:328`, `scripts/migrate_to_pgvector.py:624`, and `scripts/bench_recall.py:107`. A fourth site, `claude_hooks/providers/pgvector.py:123`, lacks the fallback but is only directly exercisable through the public provider API—not through the normal pgvector installer path.

| file:line | Purpose | Conda subprocess fallback? | Exercisable from non-conda Python? | Assessment / reachability |
|---|---|---:|---:|---|
| `claude_hooks/providers/pgvector.py:123` | `PgvectorProvider.verify()` imports `psycopg`; missing dependency is caught and the method returns `False`. `claude_hooks/providers/pgvector.py:123` `claude_hooks/providers/pgvector.py:124` `claude_hooks/providers/pgvector.py:126` | **No** | **Yes, but not through the normal installer path** | The site is directly reachable by external code calling the public `PgvectorProvider.verify()` method. However, `_verify_pgvector_dsn()` first imports `psycopg` itself and catches `ImportError` before calling `PgvectorProvider.verify()`, then uses the conda subprocess. `install.py:2127` `install.py:2132` `install.py:2135` `install.py:2146` Thus a non-conda installer does **not** reach line 123: its own import fails first. The generic provider-verification loop also explicitly skips pgvector. `install.py:3540` `install.py:3547` |
| `claude_hooks/providers/pgvector.py:328` | `PgvectorProvider._ensure_ready()` imports `psycopg` and opens the configured DSN. `claude_hooks/providers/pgvector.py:320 [no psycopg at this line; line is in _ensure_ready]` `claude_hooks/providers/pgvector.py:328` `claude_hooks/providers/pgvector.py:330` `claude_hooks/providers/pgvector.py:334` | **No** | **Yes — genuine production gap** | `_ensure_ready()` is called by provider operations including `recall()`, `store()`, `count()`, and batch operations. `claude_hooks/providers/pgvector.py:148 [no count at this line; line is in recall]` `claude_hooks/providers/pgvector.py:214 [no count at this line; line is in store]` `claude_hooks/providers/pgvector.py:241` `claude_hooks/providers/pgvector.py:265` `claude_hooks/providers/pgvector.py:292` The MCP server dispatches directly to `recall()`, `store()`, and `count()`. `claude_hooks/pgvector_mcp/server.py:271 [no count at this line; line is in _dispatch_tool]` `claude_hooks/pgvector_mcp/server.py:280 [no count at this line; line is in _dispatch_tool]` `claude_hooks/pgvector_mcp/server.py:283` The launcher selects a repo `.venv` before the conda environment, so a `.venv` lacking `psycopg` can run this code even while the separate conda environment contains it. `bin/_resolve_python.sh:31` `bin/_resolve_python.sh:32` `bin/_resolve_python.sh:35` `bin/claude-hook-pgvector-mcp:10` `bin/claude-hook-pgvector-mcp:17` Missing `psycopg` is converted into an `ImportError`; no conda retry occurs. |
| `scripts/migrate_to_pgvector.py:624` | `main()` imports `psycopg`, then immediately opens the migration connection. `scripts/migrate_to_pgvector.py:610 [no psycopg at this line; line is in main]` `scripts/migrate_to_pgvector.py:624` `scripts/migrate_to_pgvector.py:626` | **No** | **Yes — documented CLI path** | The script has a direct `__main__` entry point. `scripts/migrate_to_pgvector.py:653` The runbook documents invoking it as `python scripts/migrate_to_pgvector.py`. `docs/pgvector-runbook.md:50` `docs/pgvector-runbook.md:103` Therefore a non-conda Python without `psycopg` reaches the unguarded import and fails there. |
| `scripts/bench_recall.py:107` | `PgvectorBenchProvider.__init__()` imports `psycopg` and immediately connects to PostgreSQL. `scripts/bench_recall.py:105 [no psycopg at this line; line is in PgvectorBenchProvider]` `scripts/bench_recall.py:107` `scripts/bench_recall.py:108` | **No** | **Yes — documented CLI path** | The benchmark CLI constructs `PgvectorBenchProvider` for each selected model. `scripts/bench_recall.py:214 [no PgvectorBenchProvider at this line; line is in main]` `scripts/bench_recall.py:238` `scripts/bench_recall.py:239` It has a direct `__main__` entry point, and the runbook documents `python scripts/bench_recall.py`. `scripts/bench_recall.py:322` `docs/pgvector-runbook.md:51` `docs/pgvector-runbook.md:531` A non-conda Python without `psycopg` reaches the import when the provider is constructed. |

Excluded import occurrences:

- `install.py:2127` already has the fallback: the current interpreter is tried first, and an `ImportError` leads to `find_conda_env_python()` and a `subprocess.run()` retry. `install.py:2115` `install.py:2127` `install.py:2135` `install.py:2140` `install.py:2146`
- `install.py:2524` already has the equivalent fallback. `install.py:2515` `install.py:2524` `install.py:2536` `install.py:2540` `install.py:2557`
- `install.py:2647` already has the equivalent fallback. `install.py:2631` `install.py:2647` `install.py:2653` `install.py:2657` `install.py:2673`
- `install.py:2092`, `install.py:2546`, and `install.py:2666` are imports embedded in subprocess script strings, not additional parent-process import sites. `install.py:2089` `install.py:2092` `install.py:2543` `install.py:2546` `install.py:2664` `install.py:2666`
- `tests/test_pgvector_integration.py:56` is a dependency probe that converts missing `psycopg` into a test skip. `tests/test_pgvector_integration.py:54 [no psycopg at this line; line is in _skip_if_no_deps]` `tests/test_pgvector_integration.py:56` `tests/test_pgvector_integration.py:58`
- `tests/test_pgvector_integration.py:285` is test cleanup whose import is inside exception-swallowing teardown, not a production runtime path. `tests/test_pgvector_integration.py:285` `tests/test_pgvector_integration.py:293`

Reference limitation: the available evidence verifies the current-source fallback pattern, but commit `4e67dc2` itself could not be inspected directly, so attribution to that exact commit is unverified.

---
