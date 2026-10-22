# Consultation transcript — csl-2026-10-22-1701-a0f1

_Question_: Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python.


## Planner

1. Enumerate every Python `import psycopg`/`from psycopg` site in the repository, excluding generated `.claude-hooks/` artifacts, and verify whether any sites exist beyond `claude_hooks/providers/pgvector.py:123`, `claude_hooks/providers/pgvector.py:328`, and `install.py:2092`/`install.py:2127`/`install.py:2524`/`install.py:2647`.
2. Inspect commit `4e67dc2` and the surrounding `install.py` fallback implementations at `install.py:2116`, `install.py:2515`, and `install.py:2631` to establish exactly which import sites received the conda-env subprocess treatment and what pattern counts as equivalent coverage.
3. Read `claude_hooks/providers/pgvector.py` around `PgvectorProvider.verify` at `claude_hooks/providers/pgvector.py:123` and the connection initialization at `claude_hooks/providers/pgvector.py:328` to confirm neither path invokes a conda-env subprocess when `psycopg` is absent.
4. Trace all callers and configuration paths for `PgvectorProvider.verify` and `PgvectorProvider` initialization through `claude_hooks/providers/__init__.py` and hook/provider dispatch code, determining whether the missing fallbacks at `claude_hooks/providers/pgvector.py:123` and `claude_hooks/providers/pgvector.py:328` are reachable when hooks run under a non-conda Python with pgvector enabled.
5. Inspect the embedded subprocess probe at `install.py:2092` and its caller around `install.py:2080` to determine whether this is a subprocess-only import site that already has conda fallback coverage and therefore should not be reported as missing.
6. Check tests covering the fallback—`tests/test_install_pgvector_verify.py:1` and related pgvector install tests—for explicit behavior that distinguishes direct in-process imports, subprocess-embedded imports, and provider-level imports.
7. Produce the final audit table containing each genuinely uncovered import site as `file:line`, the surrounding symbol/path, whether it lacks the fallback, and whether a non-conda-env Python can actually exercise it; explicitly separate unreachable/disabled code and subprocess-executed imports from actionable in-process gaps.

---

## Researcher (round 1)

- **Inventory:** excluding generated `.claude-hooks/`, the repository contains 12 Python `import psycopg` sites: six in `install.py`, two in `claude_hooks/providers/pgvector.py`, two in `scripts/`, and two in integration tests. No `from psycopg ...` import sites were found.

- **`claude_hooks/providers/pgvector.py:123` — uncovered, conditionally exercisable:** `PgvectorProvider.verify()` directly imports `psycopg`; missing dependency immediately logs and returns `False`, with no conda subprocess (`claude_hooks/providers/pgvector.py:121-126`). The specialized installer caller is protected by `_verify_pgvector_dsn()`’s outer fallback, but the generic provider-verification path calls `cls.verify(candidate)` directly (`install.py:3556`), making this site reachable with a pgvector candidate when `install.py` runs under a Python lacking `psycopg`. Pgvector detection can produce such a candidate when the launcher is on `PATH` (`claude_hooks/providers/pgvector.py:91-118`).

- **`claude_hooks/providers/pgvector.py:328` — uncovered, directly exercisable in hook runtime:** `_ensure_ready()` directly imports `psycopg` and raises `ImportError` when unavailable; it does not spawn the conda interpreter (`claude_hooks/providers/pgvector.py:320-334`). Enabled providers with a configured DSN are instantiated by `build_providers()` (`claude_hooks/dispatcher.py:129-148`), and recall calls `_ensure_ready()` (`claude_hooks/providers/pgvector.py:144-151`). Thus a hook process lacking `psycopg` reaches this import and treats pgvector recall as unavailable. The shell shim does not guarantee conda: it prioritizes an override or repo-local venv before conda (`bin/_resolve_python.sh:25-44`), and `bin/claude-hook` executes whichever interpreter that resolver selects (`bin/claude-hook:13-23`).

- **`scripts/migrate_to_pgvector.py:624` — uncovered, exercisable by the migration CLI:** the migration path directly executes `import psycopg` and then `psycopg.connect()` without a fallback (`scripts/migrate_to_pgvector.py:624-626`). Running the migration script with a non-conda interpreter that lacks `psycopg` reaches this site and fails immediately. It is not part of ordinary hook dispatch.

- **`scripts/bench_recall.py:107` — uncovered, conditionally exercisable by the pgvector benchmark:** `PgvectorBenchProvider.__init__()` directly imports `psycopg` and opens a connection (`scripts/bench_recall.py:105-108`). A non-conda interpreter lacking `psycopg` fails when the benchmark selects this provider. It is benchmark-only, not hook runtime.

- **`tests/test_pgvector_integration.py:56` — intentionally guarded, not a missing-fallback defect:** the import is wrapped by a helper that raises `unittest.SkipTest` when `psycopg` is absent (`tests/test_pgvector_integration.py:54-58`), so the missing-dependency path does not exercise DB behavior.

- **`tests/test_pgvector_integration.py:285` — test cleanup import, effectively dependency-gated:** cleanup directly imports `psycopg` before connecting (`tests/test_pgvector_integration.py:283-287`), but the integration suite already skips when the dependency is unavailable (`tests/test_pgvector_integration.py:54-58`). This is test infrastructure, not production reachability.

- **`install.py:2092` — covered subprocess-only site:** this import belongs to `_PGVECTOR_VERIFY_SCRIPT` (`install.py:2089-2112`). The script is run specifically with the conda interpreter discovered by `find_conda_env_python()` (`install.py:2138-2148`), so it is not executed in the caller’s non-conda interpreter.

- **`install.py:2127` — covered:** this is the fast-path import in `_verify_pgvector_dsn()`. `ImportError` leads to a conda-interpreter subprocess containing the independent probe (`install.py:2125-2148`).

- **`install.py:2524` — covered:** `_pgvector_tables_present()` catches the direct import’s `ImportError` and runs the table check through `find_conda_env_python()` (`install.py:2522-2542`, `install.py:2556-2568`).

- **`install.py:2546` — covered subprocess-only site:** this import is embedded in the table-check script executed by the conda subprocess (`install.py:2543-2559`).

- **`install.py:2647` — covered:** `_init_pgvector_schema()` catches the fast-path `ImportError` and pipes the DDL to a conda-interpreter subprocess (`install.py:2645-2657`, `install.py:2664-2675`).

- **`install.py:2666` — covered subprocess-only site:** this import is embedded in the schema-initialization script run by the conda subprocess (`install.py:2664-2675`).

- **Equivalent coverage pattern:** all six covered installer sites use the same structure—attempt an in-process import, catch `ImportError`, resolve `find_conda_env_python()`, and execute the DB operation through `subprocess.run()` using the conda interpreter. The pgvector provider and two standalone scripts do not use that pattern.

- **Commit evidence limitation:** the available filesystem tools did not expose `git show 4e67dc2`, so the commit’s exact diff was not independently inspected. The current implementations above establish the fallback pattern and its current coverage.

---

## Researcher (round 1)

- **Import inventory:** repository-wide `import psycopg` search found exactly 10 sites: five in `install.py`, two in `claude_hooks/providers/pgvector.py`, two in `scripts/`, and two in `tests/test_pgvector_integration.py` (`install.py:2092`, `install.py:2127`, `install.py:2524`, `install.py:2647`, `claude_hooks/providers/pgvector.py:123`, `claude_hooks/providers/pgvector.py:328`, `scripts/migrate_to_pgvector.py:624`, `scripts/bench_recall.py:107`, `tests/test_pgvector_integration.py:56`, `tests/test_pgvector_integration.py:285`).

- **Planner’s `install.py:2080` caller reference is stale:** lines 2080–2083 are Windows daemon-task error text, not the pgvector probe. The embedded probe is actually launched at `install.py:2146`–`install.py:2148`.

- **Covered subprocess-only import — do not report as missing:** `install.py:2092` is inside `_PGVECTOR_VERIFY_SCRIPT` (`install.py:2089`–`install.py:2112`). The caller locates the conda interpreter and executes that script with it (`install.py:2138`–`install.py:2148`). Thus this import is not expected to succeed in the parent non-conda interpreter.

- **Covered installer fast paths — do not report as missing:** the direct import at `install.py:2127` is followed by `ImportError` handling and the conda subprocess fallback (`install.py:2132`–`install.py:2149`); the import at `install.py:2524` has the equivalent fallback (`install.py:2524`–`install.py:2559`); the import at `install.py:2647` also has the fallback (`install.py:2647`–`install.py:2675`). Their embedded subprocess imports at `install.py:2546` and `install.py:2666` execute under the selected conda interpreter.

- **Actionable in-process provider gap — `claude_hooks/providers/pgvector.py:123`:** `PgvectorProvider.verify` directly imports `psycopg`; missing `psycopg` merely logs and returns `False` (`claude_hooks/providers/pgvector.py:120`–`claude_hooks/providers/pgvector.py:138`). It has no local subprocess fallback. A non-conda Python can exercise this by calling `PgvectorProvider.verify` directly, but the installer’s `_verify_pgvector_dsn` path is protected by its parent fallback (`install.py:2125`–`install.py:2149`). Therefore this is a direct-call/provider-level gap, not an uncovered installer path.

- **Actionable in-process provider gap — `claude_hooks/providers/pgvector.py:328`:** `_ensure_ready` directly imports `psycopg` and raises `ImportError` when unavailable (`claude_hooks/providers/pgvector.py:320`–`claude_hooks/providers/pgvector.py:334`). No subprocess fallback exists at this site. A non-conda Python can exercise it whenever a configured `PgvectorProvider` reaches readiness/connection initialization; the module itself remains importable because the import is lazy. This is the most consequential production-path gap.

- **Optional operator-tool gap — `scripts/migrate_to_pgvector.py:624`:** `main()` imports `psycopg` directly before opening the database (`scripts/migrate_to_pgvector.py:610`–`scripts/migrate_to_pgvector.py:626`). Running this CLI with a non-conda Python lacking `psycopg` fails immediately; there is no fallback. It is exercisable, but optional/operator-facing rather than part of normal hook execution.

- **Optional benchmark gap — `scripts/bench_recall.py:107`:** `PgvectorBenchProvider.__init__` directly imports `psycopg` and connects (`scripts/bench_recall.py:105`–`scripts/bench_recall.py:109`). A non-conda Python can exercise it when the pgvector benchmark provider is selected. No fallback is present.

- **Test-only imports — not production gaps:** `tests/test_pgvector_integration.py:56` intentionally skips the integration suite when `psycopg` is absent (`tests/test_pgvector_integration.py:54`–`tests/test_pgvector_integration.py:58`); `tests/test_pgvector_integration.py:285` is cleanup inside a broad `except Exception` (`tests/test_pgvector_integration.py:281`–`tests/test_pgvector_integration.py:294`). Neither needs a conda subprocess fallback.

- **Fallback tests distinguish parent and subprocess paths:** the fast-path test injects both `psycopg` and a mocked provider (`tests/test_install_pgvector_verify.py:34`–`tests/test_install_pgvector_verify.py:47`); the fallback test forces import failure and asserts that the fake conda Python is selected (`tests/test_install_pgvector_verify.py:63`–`tests/test_install_pgvector_verify.py:83`). This coverage is specifically for `install.py`’s installer wrapper, not for direct `PgvectorProvider` imports or standalone scripts.

- **Evidence limit:** the diff for commit `4e67dc2` itself was not inspected; classification is based on the current verified source and its explicit fallback comments (`install.py:2118`–`install.py:2123`, `install.py:2515`–`install.py:2520`, `install.py:2631`–`install.py:2634`).

---

## Synthesizer

Four production/operator import sites still lack the conda-interpreter fallback; two are reachable in normal hook runtime or its provider APIs, and two are CLI-only. The remaining eight sites are either already covered or intentionally dependency-gated.

| Import site | Classification | Exercisable by a Python without `psycopg`? |
|---|---|---|
| `claude_hooks/providers/pgvector.py:123` | **Uncovered:** `PgvectorProvider.verify()` directly imports `psycopg`; `ImportError` is converted to `False`, with no conda subprocess (`claude_hooks/providers/pgvector.py:120 [no psycopg at this line; line is in PgvectorProvider]`). | **Yes, when `PgvectorProvider.verify()` is called directly.** It returns `False` rather than verifying through the conda environment. The generic installer loop explicitly skips `pgvector`, so `install.py:3556` is not a generic caller of this provider method (`install.py:3546`). |
| `claude_hooks/providers/pgvector.py:328` | **Uncovered and consequential:** `PgvectorProvider._ensure_ready()` directly imports `psycopg` and raises `ImportError`; it has no conda fallback (`claude_hooks/providers/pgvector.py:320 [no psycopg at this line; line is in _ensure_ready]`). | **Yes, in ordinary hook runtime.** An enabled provider with a configured DSN is instantiated by `build_providers()` (`claude_hooks/dispatcher.py:129`), and `recall()` calls `_ensure_ready()` (`claude_hooks/providers/pgvector.py:144 [no _ensure_ready at this line; line is in recall]`). The resulting `ImportError` is caught as “pgvector unavailable,” and recall returns no results. |
| `scripts/migrate_to_pgvector.py:624` | **Uncovered:** the migration CLI directly imports `psycopg` and connects without a fallback (`scripts/migrate_to_pgvector.py:610 [no psycopg at this line; line is in main]`). | **Yes**, when this migration CLI is run with a Python lacking `psycopg`; it fails at the import rather than delegating to the conda environment. It is operator-facing, not part of normal hook dispatch. |
| `scripts/bench_recall.py:107` | **Uncovered:** `PgvectorBenchProvider` directly imports `psycopg` and connects without a fallback (`scripts/bench_recall.py:105 [no psycopg at this line; line is in PgvectorBenchProvider]`). | **Yes**, when the pgvector benchmark provider is selected. It is benchmark-only, not hook runtime. |

Already covered or intentionally excluded:

- `install.py:2092` — **covered, subprocess-only:** this import is inside `_PGVECTOR_VERIFY_SCRIPT`; the script is executed with the discovered conda Python rather than the caller’s interpreter (`install.py:2089`, `install.py:2138`).
- `install.py:2127` — **covered:** the fast-path import falls back to a subprocess running with the conda Python when unavailable (`install.py:2125`, `install.py:2138`).
- `install.py:2524` — **covered:** `_pgvector_tables_present()` catches import failure and retries through the conda interpreter (`install.py:2523`, `install.py:2543`).
- `install.py:2546` — **covered, subprocess-only:** the table-check script imports `psycopg` under the selected conda interpreter (`install.py:2543`).
- `install.py:2647` — **covered:** `_init_pgvector_schema()` falls back to a conda-interpreter subprocess (`install.py:2645`).
- `install.py:2666` — **covered, subprocess-only:** the schema script imports `psycopg` under the selected conda interpreter (`install.py:2664`).
- `tests/test_pgvector_integration.py:56` — **intentionally dependency-gated:** absence of `psycopg` causes the integration suite to skip, rather than attempting a conda fallback (`tests/test_pgvector_integration.py:54`).
- `tests/test_pgvector_integration.py:285` — **test cleanup only:** this is cleanup inside the dependency-gated integration suite and is not a production path (`tests/test_pgvector_integration.py:281`).

The installer fallback tests cover the installer’s fast-path and conda-subprocess behavior, not the provider-level imports or standalone scripts (`tests/test_install_pgvector_verify.py:34`, `tests/test_install_pgvector_verify.py:63`).

The exact diff for commit `4e67dc2` could not be inspected; the classification above is based on the current source’s explicit fallback implementations. Excluding generated `.claude-hooks/` artifacts, the inventory contains 12 `import psycopg` sites and no `from psycopg` sites.

---
