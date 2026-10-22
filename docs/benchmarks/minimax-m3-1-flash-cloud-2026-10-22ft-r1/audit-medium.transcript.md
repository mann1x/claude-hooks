# Consultation transcript — csl-2026-10-22-2029-db1d

_Question_: Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python.


## Planner

1. Inspect commit `4e67dc2` (for example with `git show 4e67dc2 -- install.py claude_hooks/`) to identify the exact conda-env subprocess fallback pattern and every file/symbol it changed, and note that the commit diff itself was not retrieved during planning.
2. Build a complete import-site inventory with repository-wide searches for `import psycopg`, `from psycopg`, and `psycopg.connect`, excluding generated `.claude-hooks/` state; the currently confirmed candidates are `install.py:2092`, `install.py:2127`, `install.py:2524`, `install.py:2546`, `install.py:2647`, `install.py:2666`, `claude_hooks/providers/pgvector.py:123`, `claude_hooks/providers/pgvector.py:328`, `scripts/migrate_to_pgvector.py:624`, and `scripts/bench_recall.py:107`.
3. Compare each candidate against the reference implementation in `install.py:_verify_pgvector_dsn` (`install.py:2115-2161`), specifically checking for the fast import followed by `find_conda_env_python()` and `subprocess.run` fallback, and inspect the unchecked blocks around `install.py:2524-2547` and `install.py:2647-2666` to determine whether those sites are subprocess script bodies or still lack a fallback.
4. Trace production callers of `PgvectorProvider.verify` and `PgvectorProvider.connect` from `claude_hooks/providers/pgvector.py:121-132` and `claude_hooks/providers/pgvector.py:325-334` through provider selection, hook startup, and MCP/daemon entry points, establishing whether a non-conda interpreter can actually reach either import site rather than merely whether the import is syntactically present.
5. Inspect the normal hook launch chain (`bin/claude-hook.cmd`, `bin/claude-hook.sh`, `run.py`, and `run_daemon.py`) plus any resolved-interpreter configuration to determine which Python executes those callers and whether the pgvector provider is enabled or skipped by configuration in the non-conda case.
6. Classify the standalone entry points `scripts/migrate_to_pgvector.py:624` and `scripts/bench_recall.py:107` by checking their `main`/CLI guard and documented invocation commands, distinguishing directly runnable scripts from import-only or test-only code, then report the final file:line list with an explicit “exercisable / not exercisable” rationale for each site.

---

## Researcher (round 1)

## Findings

- **Historical commit scope is unverified:** the exposed tools could not retrieve `git show 4e67dc2`, so the exact files/symbols changed by that commit cannot be attributed. The current tree nevertheless contains the reference pattern: current-interpreter fast path, `ImportError` handling, `find_conda_env_python()`, then `subprocess.run()` against that interpreter (`install.py:2115`, `install.py:2125`, `install.py:2135`, `install.py:2138`, `install.py:2140`, `install.py:2146`).

- **`install.py:2092` — no missing fallback; not executed by the non-conda caller.** This import is inside `_PGVECTOR_VERIFY_SCRIPT` (`install.py:2089`, `install.py:2092`). That script is launched as a child of the resolved conda interpreter (`install.py:2146`, `install.py:2147`). Thus it is exercised only in the fallback environment, not by the original non-conda Python.

- **`install.py:2127` — has the fallback; the import attempt is exercisable from non-conda Python.** The import is explicitly the fast path (`install.py:2125`, `install.py:2127`); `ImportError` reaches the conda subprocess fallback (`install.py:2135`, `install.py:2140`, `install.py:2146`). This is the reference implementation.

- **`install.py:2524` — has the fallback; the import attempt is exercisable from non-conda Python.** `_pgvector_tables_present` catches `ImportError` (`install.py:2522`, `install.py:2524`, `install.py:2536`) and invokes the resolved conda interpreter (`install.py:2539`, `install.py:2540`, `install.py:2557`).

- **`install.py:2546` — no missing fallback; not executed by the non-conda caller.** This is another import inside an embedded subprocess script (`install.py:2543`, `install.py:2546`), executed by the conda interpreter (`install.py:2557`, `install.py:2558`).

- **`install.py:2647` — has the fallback; the import attempt is exercisable from non-conda Python.** `_init_pgvector_schema` catches `ImportError` (`install.py:2645`, `install.py:2647`, `install.py:2653`) and reruns the DDL through the conda interpreter (`install.py:2656`, `install.py:2657`, `install.py:2673`).

- **`install.py:2666` — no missing fallback; not executed by the non-conda caller.** The import is inside the fallback subprocess script (`install.py:2664`, `install.py:2666`), which is launched with the resolved conda interpreter (`install.py:2673`, `install.py:2674`).

- **`claude_hooks/providers/pgvector.py:123` — actionable missing fallback; conditionally exercisable from non-conda Python.** `PgvectorProvider.verify` catches a missing `psycopg` and returns `False` without attempting the conda environment (`claude_hooks/providers/pgvector.py:121`, `claude_hooks/providers/pgvector.py:123`, `claude_hooks/providers/pgvector.py:124`). It is protected when called through `_verify_pgvector_dsn`, because that wrapper checks `import psycopg` first and never calls the provider on `ImportError` (`install.py:2127`, `install.py:2132`, `install.py:2135`). However, the installer’s generic provider-verification path calls `cls.verify(candidate)` directly (`install.py:3556`), so a pgvector candidate can reach this unguarded provider site under a non-conda interpreter.

- **`claude_hooks/providers/pgvector.py:328` — actionable missing fallback; exercisable from non-conda Python.** `_ensure_ready` directly imports `psycopg` and raises `ImportError` when unavailable (`claude_hooks/providers/pgvector.py:320`, `claude_hooks/providers/pgvector.py:328`, `claude_hooks/providers/pgvector.py:329`, `claude_hooks/providers/pgvector.py:334`). Recall invokes `_ensure_ready` (`claude_hooks/providers/pgvector.py:144`, `claude_hooks/providers/pgvector.py:148`). The normal shim prefers a repo `.venv` before conda and finally falls back to system `python3`/`python` (`bin/_resolve_python.sh:31`, `bin/_resolve_python.sh:35`, `bin/_resolve_python.sh:53`), so a non-conda interpreter is genuinely possible when a `.venv` or system interpreter is selected.

- **`scripts/migrate_to_pgvector.py:624` — actionable missing fallback; exercisable when the migration body runs under non-conda Python.** The import is direct and unguarded, immediately followed by `psycopg.connect` (`scripts/migrate_to_pgvector.py:624`, `scripts/migrate_to_pgvector.py:626`). No fallback is present at this site. Retrieved evidence did not establish whether every documented invocation pins the conda interpreter.

- **`scripts/bench_recall.py:107` — actionable missing fallback; exercisable whenever this provider class is instantiated under non-conda Python.** Its constructor imports `psycopg` directly and connects immediately (`scripts/bench_recall.py:105`, `scripts/bench_recall.py:107`, `scripts/bench_recall.py:108`). Retrieved evidence did not establish a production caller or whether the benchmark CLI pins conda, so practical exercise is conditional on that invocation path.

- **Non-actionable inventory matches:** `tests/test_pgvector_integration.py:56` intentionally skips when `psycopg` is absent, while `tests/test_pgvector_integration.py:285` is teardown inside that dependency-gated integration suite. `docs/pgvector-runbook.md:499` is documentation code, not an executable project import site. These should not be treated as production fallback defects.

---

## Researcher (round 1)

- **Remaining import sites:** four direct `psycopg` imports lack a conda-env subprocess fallback: `claude_hooks/providers/pgvector.py:123`, `claude_hooks/providers/pgvector.py:328`, `scripts/migrate_to_pgvector.py:624`, and `scripts/bench_recall.py:107`. The installer’s `psycopg` fast-path imports are not in this list because they are paired with subprocess fallbacks at `install.py:2138`, `install.py:2146`, `install.py:2515`, and `install.py:2631`.

- **`claude_hooks/providers/pgvector.py:123` — conditionally exercisable from non-conda Python.**
  - `PgvectorProvider.verify()` imports `psycopg` directly at `claude_hooks/providers/pgvector.py:123` and returns `False` on `ImportError` at `claude_hooks/providers/pgvector.py:124`.
  - The dedicated installer path is **not** exercisable there without `psycopg`: `install.py:2127` imports `psycopg` before calling `PgvectorProvider.verify()` at `install.py:2132`; the `ImportError` branch at `install.py:2135` diverts to the conda interpreter subprocess at `install.py:2140` and `install.py:2146`.
  - A second production caller exists in generic provider detection: `install.py:3556` calls `cls.verify(candidate)`. `pgvector` is in the provider registry at `claude_hooks/providers/__init__.py:33`, and its detector can produce a candidate when the `pgvector-mcp` launcher is on `PATH` at `claude_hooks/providers/pgvector.py:107`. Therefore a system Python running the installer can reach `claude_hooks/providers/pgvector.py:123` through this generic detection path, and it will report failure rather than use the conda fallback.

- **`claude_hooks/providers/pgvector.py:328` — exercisable from non-conda Python when pgvector is enabled and a DSN is configured.**
  - The import at `claude_hooks/providers/pgvector.py:328` is inside `_ensure_ready()`, and the resulting `ImportError` is converted to the “install psycopg” error at `claude_hooks/providers/pgvector.py:329`; there is no subprocess fallback.
  - Normal dispatch instantiates only enabled providers with a non-empty `mcp_url` or `dsn` at `claude_hooks/dispatcher.py:129`, `claude_hooks/dispatcher.py:131`, `claude_hooks/dispatcher.py:136`, and `claude_hooks/dispatcher.py:148`.
  - Default configuration leaves pgvector disabled at `claude_hooks/config.py:48` and `claude_hooks/config.py:49`, so the default hook path skips it. If a user enables it with a DSN, however, recall/store calls reach `_ensure_ready()` and therefore `claude_hooks/providers/pgvector.py:328`.
  - A non-conda interpreter is realistic in this chain: the POSIX resolver falls back from venv/conda to system `python3`/`python` at `bin/_resolve_python.sh:53`, and the Windows shim falls back to `py -3` or `python` at `bin/claude-hook.cmd:43`. `run.py` dispatches directly through `claude_hooks.dispatcher` at `run.py:33` and `run.py:57`.
  - The pgvector MCP launcher has the same system-Python fallback at `bin/claude-hook-pgvector-mcp:10` and `bin/claude-hook-pgvector-mcp:17`; the Windows equivalent falls back at `bin/claude-hook-pgvector-mcp.cmd:36` and `bin/claude-hook-pgvector-mcp.cmd:43`. The MCP server builds configured providers and selects `PgvectorProvider` at `claude_hooks/pgvector_mcp/server.py:342` and `claude_hooks/pgvector_mcp/server.py:344`; its tool calls then invoke provider recall/store at `claude_hooks/pgvector_mcp/server.py:271` and `claude_hooks/pgvector_mcp/server.py:280`, reaching `_ensure_ready()`.
  - The daemon can encounter the same condition because it creates fresh providers per request as described at `claude_hooks/daemon.py:50`; the launcher-controlled interpreter determines whether that daemon has `psycopg`.

- **`scripts/migrate_to_pgvector.py:624` — directly exercisable from non-conda Python.**
  - This is a standalone CLI with an `if __name__ == "__main__"` guard at `scripts/migrate_to_pgvector.py:653`; execution calls `main()` through `scripts/migrate_to_pgvector.py:654`.
  - `main()` unconditionally reaches `import psycopg` at `scripts/migrate_to_pgvector.py:624` and immediately opens a connection at `scripts/migrate_to_pgvector.py:626`. There is no fallback.
  - The documented invocation is `python scripts/migrate_to_pgvector.py --embedder qwen3` at `config/claude-hooks.example.json:80`, so running that command with a system Python lacking `psycopg` reaches the unguarded import.

- **`scripts/bench_recall.py:107` — directly exercisable from non-conda Python.**
  - `PgvectorBenchProvider.__init__()` imports `psycopg` at `scripts/bench_recall.py:107` and connects at `scripts/bench_recall.py:108`, with no fallback.
  - The class is instantiated for each selected pgvector model by the CLI’s `main()` at `scripts/bench_recall.py:239`; the script has a direct `__main__` guard at `scripts/bench_recall.py:322` and calls `main()` at `scripts/bench_recall.py:323`.
  - Therefore `python scripts/bench_recall.py` under a non-conda Python lacking `psycopg` reaches this import site.

---

## Synthesizer

Four executable `psycopg` import sites still lack the conda-environment subprocess fallback: `claude_hooks/providers/pgvector.py:123`, `claude_hooks/providers/pgvector.py:328`, `scripts/migrate_to_pgvector.py:624`, and `scripts/bench_recall.py:107`.

### Sites still lacking the fallback

- **`claude_hooks/providers/pgvector.py:123` — directly exercisable, but no normal non-conda production path was found.**  
  `PgvectorProvider.verify()` directly imports `psycopg` and converts `ImportError` into `False`; it does not attempt the conda environment (`claude_hooks/providers/pgvector.py:123`, `claude_hooks/providers/pgvector.py:124`). The dedicated installer verifier never reaches this method without `psycopg`: its own import fails first and triggers the conda subprocess (`install.py:2127`, `install.py:2135`, `install.py:2140`, `install.py:2146`). The generic installer verification loop explicitly skips the `pgvector` provider, so its generic `cls.verify(candidate)` call is not a pgvector route (`install.py:3540`, `install.py:3541`, `install.py:3547`, `install.py:3548`, `install.py:3556`). Thus:
  - **Directly callable from non-conda Python:** yes.
  - **Reachable through an audited normal production path:** no demonstrated route.

- **`claude_hooks/providers/pgvector.py:328` — yes, conditionally exercisable from a non-conda Python.**  
  `_ensure_ready()` directly imports `psycopg` and raises an “install psycopg” error instead of using the conda environment (`claude_hooks/providers/pgvector.py:328`, `claude_hooks/providers/pgvector.py:329`, `claude_hooks/providers/pgvector.py:334`). Recall reaches that method (`claude_hooks/providers/pgvector.py:144`, `claude_hooks/providers/pgvector.py:148`). The normal dispatch path constructs configured providers (`claude_hooks/dispatcher.py:129`, `claude_hooks/dispatcher.py:131`, `claude_hooks/dispatcher.py:136`, `claude_hooks/dispatcher.py:148`), and the launcher can select system Python rather than conda Python (`bin/_resolve_python.sh:53`, `bin/claude-hook.cmd:43`). Pgvector is disabled by default (`claude_hooks/config.py:48`, `claude_hooks/config.py:49`), so the defect is reached when pgvector is **enabled, has a DSN, and the selected non-conda interpreter lacks `psycopg`**. The MCP path can also reach provider recall/store (`claude_hooks/pgvector_mcp/server.py:271 [no psycopg at this line; line is in _dispatch_tool]`, `claude_hooks/pgvector_mcp/server.py:280`, `claude_hooks/pgvector_mcp/server.py:344`).

- **`scripts/migrate_to_pgvector.py:624` — yes, directly exercisable from a non-conda Python.**  
  `main()` unconditionally imports `psycopg` and immediately connects (`scripts/migrate_to_pgvector.py:624`, `scripts/migrate_to_pgvector.py:626`). The script is a directly runnable CLI (`scripts/migrate_to_pgvector.py:653`, `scripts/migrate_to_pgvector.py:654`), and its documented invocation uses plain `python`, so a system/non-conda interpreter without `psycopg` reaches the unguarded import (`config/claude-hooks.example.json:80`).

- **`scripts/bench_recall.py:107` — yes, directly exercisable when a pgvector benchmark model is selected.**  
  The provider constructor directly imports `psycopg` and connects (`scripts/bench_recall.py:107`, `scripts/bench_recall.py:108`). The CLI instantiates the provider for selected pgvector models (`scripts/bench_recall.py:239`) and is directly runnable (`scripts/bench_recall.py:322`, `scripts/bench_recall.py:323`). Therefore a non-conda interpreter lacking `psycopg` reaches the import when the benchmark selects pgvector.

### Installer import sites that are not missing the fallback

- `install.py:2092` — inside the embedded verification script; that script is launched with the resolved conda interpreter, so it is not executed by the original non-conda Python (`install.py:2092`, `install.py:2146`).
- `install.py:2127` — fast-path import; it is reached by non-conda Python but has the `ImportError` → `find_conda_env_python()` → `subprocess.run()` fallback (`install.py:2127`, `install.py:2135`, `install.py:2140`, `install.py:2146`).
- `install.py:2524` — fast-path import with a conda subprocess fallback (`install.py:2524`, `install.py:2536`, `install.py:2540`, `install.py:2557`).
- `install.py:2546` — inside the embedded subprocess script, which is run by the conda interpreter (`install.py:2546`, `install.py:2557`).
- `install.py:2647` — fast-path import with a conda subprocess fallback (`install.py:2647`, `install.py:2653`, `install.py:2657`, `install.py:2673`).
- `install.py:2666` — inside the embedded subprocess script, which is run by the conda interpreter (`install.py:2666`, `install.py:2673`).

The integration-test import is intentionally dependency-gated (`tests/test_pgvector_integration.py:56`), its teardown import is inside that gated suite (`tests/test_pgvector_integration.py:285`), and the runbook occurrence is documentation rather than executable code (`docs/pgvector-runbook.md:499`).

Historical attribution to commit `4e67dc2` could not be verified because the commit diff was unavailable; the classification above is against the fallback pattern present in the current tree (`install.py:2125`, `install.py:2135`, `install.py:2140`, `install.py:2146`).

---
