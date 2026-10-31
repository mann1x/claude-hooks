# Consultation transcript — csl-2026-10-22-1615-81c1

_Question_: Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python.


## Planner

1. Inspect commit `4e67dc2` (via git show/log if available) to identify the exact conda-environment subprocess fallback pattern, including its helper names, interpreter-selection rules, and the import sites it changed.
2. Search the entire repository—not only `claude_hooks/`—for every `import psycopg` and `from psycopg ...` occurrence, then compare each result against the fallback introduced by `4e67dc2`; the confirmed current in-tree sites are `claude_hooks/providers/pgvector.py:123` and `claude_hooks/providers/pgvector.py:328`.
3. Trace all call paths reaching `claude_hooks/providers/pgvector.py:123` (`PgvectorProvider.verify`) and `claude_hooks/providers/pgvector.py:328` (`PgvectorProvider._ensure_ready`) through provider construction, installer verification, and runtime recall/store paths, recording whether either path can execute under a Python outside the configured conda environment.
4. Inspect the launcher/runtime configuration that invokes the hooks (especially `bin/claude-hook*.cmd`, `bin/claude-hook*.sh`, `run.py`, `run_daemon.py`, and relevant systemd units) to determine the interpreter actually used and whether a non-conda Python is a supported or reachable execution mode.
5. Verify the behavior of the conda fallback as implemented in commit `4e67dc2`: determine whether it delegates the entire database operation through a subprocess in the conda interpreter, invokes a helper module, or merely probes for `psycopg`; test the resulting import-site classification against that exact mechanism.
6. For every unmatched `file:line`, classify it as “actually exercisable,” “not exercisable in supported flows,” or “conditionally exercisable,” citing the concrete call path and interpreter/launcher evidence; explicitly flag any site whose reachability cannot be established because the relevant `4e67dc2` diff or launcher configuration was not checked.

---

## Researcher (round 1)

- **Fallback mechanism (current tree; commit diff itself was not directly inspectable with the available file tools):** `find_conda_env_python()` selects the `claude-hooks` environment, probing common install layouts and then `conda env list --json` (`install.py:101`, `install.py:125`, `install.py:151`). The pattern is an in-process `try: import psycopg` fast path followed by `subprocess.run([conda_python, "-c", script, ...])`; the subprocess performs the complete database operation rather than merely checking importability (`install.py:2125`, `install.py:2138`, `install.py:2146`). The same pattern is used for table probing (`install.py:2522`, `install.py:2539`, `install.py:2557`) and schema initialization, with DDL passed over stdin (`install.py:2645`, `install.py:2656`, `install.py:2664`, `install.py:2673`).

- **Complete text import inventory outside generated `.claude-hooks/**` WAL noise:** `install.py:2092`, `install.py:2127`, `install.py:2524`, `install.py:2546`, `install.py:2647`, `install.py:2666`; `claude_hooks/providers/pgvector.py:123`, `claude_hooks/providers/pgvector.py:328`; `tests/test_pgvector_integration.py:56`, `tests/test_pgvector_integration.py:285`; `scripts/migrate_to_pgvector.py:624`; `scripts/bench_recall.py:107`; plus the documented shell probe at `docs/pgvector-runbook.md:734`. No `from psycopg ...` occurrence was found.

- **Installer sites do not lack the fallback.** `install.py:2092`, `install.py:2546`, and `install.py:2666` are import statements inside the child program run by the resolved conda interpreter, not imports in the original non-conda process (`install.py:2146`, `install.py:2557`, `install.py:2673`). The current-interpreter imports at `install.py:2127`, `install.py:2524`, and `install.py:2647` each have an `ImportError` branch leading to the conda subprocess.

- **`claude_hooks/providers/pgvector.py:123` — conditionally exercisable, not reachable through the installer’s missing-`psycopg` path.** `PgvectorProvider.verify()` directly imports `psycopg` and returns `False` if unavailable (`claude_hooks/providers/pgvector.py:121`, `claude_hooks/providers/pgvector.py:123`). However, `_verify_pgvector_dsn()` imports `psycopg` in the calling interpreter *before* invoking `PgvectorProvider.verify()`; on `ImportError`, it skips that call and delegates the entire verification to conda (`install.py:2125`, `install.py:2132`, `install.py:2135`, `install.py:2138`). Thus the import site is exercisable by a direct public call to `PgvectorProvider.verify()` from a non-conda Python lacking `psycopg`, but the normal installer path does not reach it under that condition.

- **`claude_hooks/providers/pgvector.py:328` — conditionally but genuinely exercisable at runtime.** `_ensure_ready()` directly imports `psycopg`; on `ImportError` it raises without a subprocess fallback (`claude_hooks/providers/pgvector.py:320`, `claude_hooks/providers/pgvector.py:328`, `claude_hooks/providers/pgvector.py:329`). Runtime provider construction occurs whenever `pgvector` is enabled and a DSN is present (`claude_hooks/dispatcher.py:131`, `claude_hooks/dispatcher.py:136`). The import is reached by normal recall (`claude_hooks/providers/pgvector.py:144`, `claude_hooks/providers/pgvector.py:148`), store (`claude_hooks/providers/pgvector.py:214`), batch recall (`claude_hooks/providers/pgvector.py:265`), batch store (`claude_hooks/providers/pgvector.py:292`), and hybrid recall (`claude_hooks/providers/pgvector.py:439`). The MCP server also builds the provider and dispatches recall/store operations through these methods (`claude_hooks/pgvector_mcp/server.py:271`, `claude_hooks/pgvector_mcp/server.py:280`, `claude_hooks/pgvector_mcp/server.py:343`). Therefore, if any dispatcher/provider/MCP process is launched with a non-conda Python lacking `psycopg`, this site is reached; recall degrades to no results (`claude_hooks/providers/pgvector.py:149`), while store paths convert the missing dependency into a runtime error (`claude_hooks/providers/pgvector.py:216`, `claude_hooks/providers/pgvector.py:293`).

- **`scripts/migrate_to_pgvector.py:624` — actually exercisable from a non-conda Python.** This is a direct top-level operation import in the standalone migration script, with no local conda fallback. Running that script under a Python without `psycopg` reaches the import and fails. It is an operator CLI path, not the normal hook-provider path.

- **`scripts/bench_recall.py:107` — conditionally exercisable from a non-conda Python.** The import occurs directly in `PgvectorBenchProvider.__init__` (`scripts/bench_recall.py:105`, `scripts/bench_recall.py:106`, `scripts/bench_recall.py:107`). It is reached whenever the benchmark selects the pgvector backend; a non-conda Python lacking `psycopg` then fails during provider construction.

- **`tests/test_pgvector_integration.py:56` — exercised as an optional-dependency probe, but harmless.** The import is inside `_skip_if_no_deps()` and catches `ImportError` to skip the test (`tests/test_pgvector_integration.py:54`, `tests/test_pgvector_integration.py:56`, `tests/test_pgvector_integration.py:57`). It is therefore actually executed by a non-conda test run, but its failure is intentionally converted into a skip.

- **`tests/test_pgvector_integration.py:285` — conditionally exercisable integration-test cleanup.** The direct import is in cleanup code (`tests/test_pgvector_integration.py:283`, `tests/test_pgvector_integration.py:285`); it matters only when the integration test reaches cleanup and `psycopg` is present in that test interpreter. Exact setup gating beyond the nearby dependency probe was not re-read before the tools became unavailable.

- **`docs/pgvector-runbook.md:734` — manually exercisable, but documentation rather than an application import site.** The documented command probes `psycopg` in the conda interpreter (`docs/pgvector-runbook.md:734`).

- **Runtime-launcher evidence gap:** the available reads established the installer’s conda selection and subprocess mechanism, but the planned reads of `bin/claude-hook`, `bin/claude-hook.cmd`, `bin/_resolve_python.sh`, `run.py`, and daemon/systemd launchers did not execute. Therefore, the strong conclusion for `claude_hooks/providers/pgvector.py:328` is that it is exercisable whenever the provider process is launched outside conda; whether every supported default hook/daemon/MCP launcher guarantees conda execution requires those launcher files to be re-read.

---

## Researcher (round 1)

## Findings

- **Fallback mechanism verified in current source:** the installer fallback is not merely an import probe. `_verify_pgvector_dsn` runs a subprocess under the resolved conda interpreter; that subprocess connects, executes `SELECT 1`, and checks `pg_extension` (`install.py:2098`, `install.py:2102`, `install.py:2145`). `_pgvector_tables_present` similarly delegates its table query (`install.py:2543`, `install.py:2557`), while `_init_pgvector_schema` delegates the complete DDL and commit (`install.py:2643`, `install.py:2664`, `install.py:2673`).

- **Installer import sites are covered by that mechanism:** the parent-process imports at `install.py:2127`, `install.py:2524`, and `install.py:2647` catch `ImportError` and use the corresponding subprocess fallback. The imports embedded in subprocess scripts at `install.py:2092`, `install.py:2546`, and `install.py:2666` are the fallback implementation itself, not uncovered sites.

- **`claude_hooks/providers/pgvector.py:123` — conditionally exercisable from non-conda Python.** `PgvectorProvider.verify` imports `psycopg` and performs the extension checks directly (`claude_hooks/providers/pgvector.py:123`, `claude_hooks/providers/pgvector.py:128`, `claude_hooks/providers/pgvector.py:132`). The normal installer fast path first imports `psycopg` before calling this method (`install.py:2127`, `install.py:2132`), so a non-conda interpreter lacking `psycopg` does **not** reach this method; it takes the subprocess fallback instead. It is reachable from a non-conda interpreter only if that interpreter itself has `psycopg`, or when called directly.

- **`claude_hooks/providers/pgvector.py:328` — actually exercisable from a supported non-conda hook runtime.** The provider registry includes pgvector (`claude_hooks/providers/__init__.py:33`), and the dispatcher supplies its configured DSN as the provider URL (`claude_hooks/dispatcher.py:132`). Provider recall and store both call `_ensure_ready` (`claude_hooks/providers/pgvector.py:148`, `claude_hooks/providers/pgvector.py:214`), which imports `psycopg` and opens the connection directly (`claude_hooks/providers/pgvector.py:328`, `claude_hooks/providers/pgvector.py:334`). This is a real gap in the runtime provider, not merely an installer concern.

- **Non-conda hook execution is explicitly reachable:** the shared resolver falls back from repo venv and conda to `python3`/`python` on `PATH` (`bin/_resolve_python.sh:14`, `bin/_resolve_python.sh:53`), and the POSIX hook executes `run.py` with that interpreter (`bin/claude-hook:16`, `bin/claude-hook:23`). The Windows launcher likewise falls back to `py -3` or `python` (`bin/claude-hook.cmd:43`, `bin/claude-hook.cmd:46`, `bin/claude-hook.cmd:50`). Therefore the runtime import at `claude_hooks/providers/pgvector.py:328` can execute in a supported system-Python installation, not merely under an unsupported manual invocation.

- **The pgvector MCP route has the same runtime exposure:** its launcher uses the same shared resolver (`bin/claude-hook-pgvector-mcp:10`, `bin/claude-hook-pgvector-mcp:17`), while MCP recall and store dispatch directly to the provider (`claude_hooks/pgvector_mcp/server.py:271`, `claude_hooks/pgvector_mcp/server.py:280`). Thus a system Python selected by the resolver can reach the uncovered provider import.

- **`scripts/migrate_to_pgvector.py:624` — actually exercisable, but outside the hook runtime.** The migration script imports `psycopg` and opens the DSN directly (`scripts/migrate_to_pgvector.py:624`, `scripts/migrate_to_pgvector.py:626`). It is operator-invoked code, not part of the hook dispatcher path. A non-conda invocation reaches this site, and the searched code showed no surrounding conda-subprocess delegation.

- **`scripts/bench_recall.py:107` — actually exercisable, but outside the hook runtime.** The benchmark’s PostgreSQL client imports `psycopg` and connects directly (`scripts/bench_recall.py:107`, `scripts/bench_recall.py:108`). It is therefore reachable whenever the benchmark is launched with a non-conda Python; no hook launcher invokes this script.

- **`tests/test_pgvector_integration.py:56` — not exercisable as a database operation when `psycopg` is absent from the test interpreter.** The import is guarded by `except ImportError` (`tests/test_pgvector_integration.py:56`, `tests/test_pgvector_integration.py:57`), unlike the production provider sites. This is test-only behavior and not a runtime fallback gap.

- **`tests/test_pgvector_integration.py:285` — conditionally exercisable; exact skip/failure path was not established.** This is a second direct test import (`tests/test_pgvector_integration.py:285`) followed by a database connection (`tests/test_pgvector_integration.py:287`). The remaining test control flow was not read, so it is not possible to state whether a non-conda test run skips or fails at this site. It is not a production hook path.

## Evidence gap

- The exact `4e67dc2` commit diff was **not inspected**; the analysis above compares current import sites against the current subprocess implementation. Classification of the two script sites and the second integration-test site should therefore be treated as provisional where their invocation documentation or full control flow was not checked.

---

## Synthesizer

Bottom line: the production gap is `claude_hooks/providers/pgvector.py:328`; it is genuinely reachable through the supported hook launcher when the resolver selects system Python. The other uncovered production import, `claude_hooks/providers/pgvector.py:123`, is only conditionally reachable; installer flows bypass it when the current interpreter lacks `psycopg`.

### Import-site audit

- `install.py:2092` — **Covered; not a gap.** This import is inside the child verification program executed by the resolved conda interpreter. The parent process delegates the complete connection and extension check through that subprocess (`install.py:2098`, `install.py:2102`, `install.py:2145`).
- `install.py:2127` — **Covered; not a gap.** This is the current-interpreter fast path. `ImportError` leads to the conda subprocess fallback rather than failure (`install.py:2127`, `install.py:2135`, `install.py:2138`, `install.py:2146`).
- `install.py:2524` — **Covered; not a gap.** The current-interpreter table probe catches `ImportError` and delegates the table query to conda (`install.py:2524`, `install.py:2536`, `install.py:2539`, `install.py:2557`).
- `install.py:2546` — **Covered; not a gap.** This import is part of the child program run by the conda interpreter (`install.py:2543`, `install.py:2546`, `install.py:2557`).
- `install.py:2647` — **Covered; not a gap.** The current-interpreter schema-initialization path catches `ImportError` and falls back to a conda subprocess (`install.py:2647`, `install.py:2653`, `install.py:2656`, `install.py:2673`).
- `install.py:2666` — **Covered; not a gap.** This import belongs to the conda child program that executes and commits the complete DDL (`install.py:2664`, `install.py:2666`, `install.py:2673`).

- `claude_hooks/providers/pgvector.py:123` — **Conditionally exercisable; a genuine uncovered site, but not through the installer’s missing-`psycopg` path.** `PgvectorProvider.verify()` imports and uses `psycopg` directly, returning `False` on `ImportError` (`claude_hooks/providers/pgvector.py:123`, `claude_hooks/providers/pgvector.py:124`, `claude_hooks/providers/pgvector.py:128`, `claude_hooks/providers/pgvector.py:132`). The installer imports `psycopg` before calling `verify()`, so a non-conda interpreter without `psycopg` goes directly to the conda subprocess (`install.py:2127`, `install.py:2132`, `install.py:2135`, `install.py:2138`). The site is nevertheless reached when `PgvectorProvider.verify()` is called directly from a Python lacking `psycopg`; it then returns `False` rather than delegating to conda.

- `claude_hooks/providers/pgvector.py:328` — **Actually exercisable; production runtime gap.** `_ensure_ready()` imports `psycopg` directly and raises `ImportError` if absent, with no conda subprocess fallback (`claude_hooks/providers/pgvector.py:320`, `claude_hooks/providers/pgvector.py:328`, `claude_hooks/providers/pgvector.py:329`, `claude_hooks/providers/pgvector.py:334`). Normal recall and store paths call `_ensure_ready()` (`claude_hooks/providers/pgvector.py:144 [no _ensure_ready at this line; line is in recall]`, `claude_hooks/providers/pgvector.py:148`, `claude_hooks/providers/pgvector.py:214`). This is a supported runtime mode: the shared resolver can select `python3` or `python` from `PATH` when neither the repository venv nor conda is available (`bin/_resolve_python.sh:14`, `bin/_resolve_python.sh:53`), and the POSIX hook executes `run.py` with that selected interpreter (`bin/claude-hook:16`, `bin/claude-hook:18`, `bin/claude-hook:23`). Therefore a supported system-Python hook runtime lacking `psycopg` can reach the uncovered import.

- `scripts/migrate_to_pgvector.py:624` — **Actually exercisable; operator CLI gap outside the hook runtime.** The standalone migration script imports `psycopg` directly and connects without a local conda-subprocess fallback (`scripts/migrate_to_pgvector.py:624`, `scripts/migrate_to_pgvector.py:626`). Running it with a non-conda Python lacking `psycopg` reaches and fails at this import.

- `scripts/bench_recall.py:107` — **Actually exercisable; benchmark CLI gap outside the hook runtime.** The pgvector benchmark provider imports and connects directly in its constructor (`scripts/bench_recall.py:105`, `scripts/bench_recall.py:107`, `scripts/bench_recall.py:108`). It is reached when the benchmark selects that backend, including under a non-conda Python lacking `psycopg`.

- `tests/test_pgvector_integration.py:56` — **Executed but intentionally harmless.** The import is inside an optional-dependency probe that catches `ImportError` and skips the test (`tests/test_pgvector_integration.py:54`, `tests/test_pgvector_integration.py:56`, `tests/test_pgvector_integration.py:57`). A non-conda test interpreter can reach the import, but it is converted into a skip rather than a fallback failure.

- `tests/test_pgvector_integration.py:285` — **Conditionally exercisable; test-only.** This is a direct test import followed by a database connection (`tests/test_pgvector_integration.py:285`, `tests/test_pgvector_integration.py:287`). Its exact skip/failure control flow was not established, so whether a non-conda run skips or fails there remains **unverified**. It is not part of the production hook path.

The documented `psycopg` probe at `docs/pgvector-runbook.md:734` is a shell command rather than a Python import site and explicitly targets the conda interpreter, so it is not an uncovered import.

The exact `4e67dc2` diff was not available for direct inspection; this audit compares current imports against the current fallback implementation, which delegates the complete database operation—not merely an import probe—through `subprocess.run` with the resolved conda interpreter (`install.py:2146`, `install.py:2557`, `install.py:2673`).

---
