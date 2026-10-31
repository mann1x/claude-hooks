# Consultation transcript — csl-2026-10-22-1815-967d

_Question_: Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python.


## Planner

1. Characterize the 4e67dc2 fallback pattern exactly: read `install.py:2085-2160` (`_PGVECTOR_VERIFY_SCRIPT` + `_verify_pgvector_dsn`), `install.py:2505-2575` (`_pgvector_tables_present`), and `install.py:2620-2685` (`_init_pgvector_schema`) to note the shape (fast-path `import psycopg` → `find_conda_env_python()` → `subprocess.run([conda_py, "-c", script, dsn])`).

2. Establish what commit 4e67dc2 actually touched: try `git log --oneline` / `git show 4e67dc2 --stat`; if `.git` is absent (confirmed absent by `list_files .git` → "not a directory"), record that the commit is unverifiable locally and treat `install.py` docstrings mentioning "conda-env subprocess when the calling interpreter lacks psycopg" as the only evidence of scope.

3. Enumerate every `import psycopg` site project-wide (`grep -n "import psycopg"` over the whole repo, excluding `.claude-hooks/**` sqlite artifacts) and classify each as having / lacking a conda fallback: expect `claude_hooks/providers/pgvector.py:123`, `claude_hooks/providers/pgvector.py:328`, `scripts/migrate_to_pgvector.py:624`, `scripts/bench_recall.py:107`, `tests/test_pgvector_integration.py:56`, `tests/test_pgvector_integration.py:285`.

4. For `claude_hooks/providers/pgvector.py:123` (`PgvectorProvider.verify`): find all callers (`grep -rn "\.verify(" install.py claude_hooks scripts tests`) to determine whether `install.py:2132` (already inside the conda-fallback try/except at `install.py:2135-2149`) makes this site unreachable from a non-conda interpreter, and whether the generic provider loop at `install.py:3556` can reach it (note `install.py:3540-3548` skips pgvector).

5. For `claude_hooks/providers/pgvector.py:328` (`_ensure_ready`): trace reachability from a non-conda interpreter — read `claude_hooks/dispatcher.py:120-160` (`build_providers`) and `claude_hooks/providers/pgvector.py:140-200` (`recall` → `_ensure_ready` → `ImportError` caught at `pgvector.py:149-151`) and confirm the hook process can be launched by a non-conda interpreter via `bin/_resolve_python.sh:30-58` (falls back to `python3`/`python` on PATH) and `bin/claude-hook.cmd:43-54`.

6. For `scripts/migrate_to_pgvector.py:624` and `scripts/bench_recall.py:107`: determine how each is invoked (grep docs/Makefile/systemd/README for `migrate_to_pgvector` and `bench_recall` invocations) and whether any launcher pins the conda-env python, to decide whether the bare `import psycopg` is exercisable from a system interpreter.

7. Check dependency declarations to see whether psycopg is expected to be present in the system interpreter at all: read `requirements.txt`, `requirements-pgvector.txt`, `pyproject.toml` optional-extras, and `install.py`'s pip-install targets for `requirements-pgvector.txt`; state explicitly if psycopg is conda-env-only, which is the precondition for the fallback to matter.

8. Verify the test sites are guarded rather than broken: read `tests/test_pgvector_integration.py:50-60` and `tests/test_pgvector_integration.py:280-290` to confirm the `unittest.SkipTest` guard makes them non-issues, and check `tests/` for any other psycopg-dependent test module.

---

## Researcher (round 1)

- **Fallback pattern:** `install.py` consistently uses a current-interpreter fast path followed by `find_conda_env_python()` and a subprocess:
  - DSN verification imports `psycopg`, calls `PgvectorProvider.verify()`, and on `ImportError` shells out with `_PGVECTOR_VERIFY_SCRIPT` (`install.py:2115`, `install.py:2125`, `install.py:2138`, `install.py:2145`).
  - Table detection falls back to `[conda_py, "-c", script, dsn, table_name]` (`install.py:2515`, `install.py:2522`, `install.py:2539`, `install.py:2556`).
  - Schema initialization falls back to `[conda_py, "-c", script, dsn]`, piping the DDL over stdin (`install.py:2631`, `install.py:2645`, `install.py:2656`, `install.py:2673`).
  - `find_conda_env_python()` locates the named environment and returns a path that callers existence-check (`install.py:101`, `install.py:104`).

- **Commit verification:** commit `4e67dc2` cannot be inspected in this checkout because `.git` is absent. The local evidence therefore establishes the fallback’s current shape, but not the exact files changed by that commit.

- **`install.py` import sites are covered:** `install.py:2092` is inside the subprocess verification script; `install.py:2127`, `install.py:2524`, and `install.py:2647` are guarded fast paths; `install.py:2546` and `install.py:2666` are subprocess scripts. These are therefore not fallback gaps. The subprocess imports are only exercised through the selected conda interpreter (`install.py:2146`, `install.py:2557`, `install.py:2673`).

- **`claude_hooks/providers/pgvector.py:123` — gap, but the installer route is protected:**
  - `PgvectorProvider.verify()` directly imports `psycopg` and returns `False` on `ImportError`; it has no local conda fallback (`claude_hooks/providers/pgvector.py:121`, `claude_hooks/providers/pgvector.py:123`, `claude_hooks/providers/pgvector.py:124`).
  - It is **not normally reached by a non-conda installer process**: `_verify_pgvector_dsn()` catches the missing dependency before calling it and then uses the subprocess fallback (`install.py:2127`, `install.py:2132`, `install.py:2135`, `install.py:2145`). The generic installer verification loop explicitly skips pgvector (`install.py:3540`, `install.py:3547`), so its generic `cls.verify(candidate)` call cannot reach this method (`install.py:3556`).
  - It **is** directly exercisable by non-conda Python that imports the provider and calls `PgvectorProvider.verify()`; the integration test does exactly that (`tests/test_pgvector_integration.py:186`). In that case it simply reports false rather than using conda.

- **`claude_hooks/providers/pgvector.py:328` — gap and operationally exercisable:**
  - `_ensure_ready()` directly imports `psycopg` and raises an install hint on `ImportError`; there is no conda subprocess fallback (`claude_hooks/providers/pgvector.py:320`, `claude_hooks/providers/pgvector.py:327`, `claude_hooks/providers/pgvector.py:328`, `claude_hooks/providers/pgvector.py:330`).
  - This is reachable through the provider’s public recall path: `recall()` calls `_ensure_ready()`, catches the resulting `ImportError`, and returns no results (`claude_hooks/providers/pgvector.py:144`, `claude_hooks/providers/pgvector.py:148`, `claude_hooks/providers/pgvector.py:149`, `claude_hooks/providers/pgvector.py:150`). Thus a non-conda interpreter can exercise the import site, but gets a silent empty recall rather than conda-backed pgvector access.

- **`scripts/migrate_to_pgvector.py:624` — gap and directly exercisable:**
  - The import is unconditional and uncaught; connection setup follows immediately (`scripts/migrate_to_pgvector.py:624`, `scripts/migrate_to_pgvector.py:626`).
  - The runbook invokes it with plain `python`, not a pinned conda interpreter (`docs/pgvector-runbook.md:50`, `docs/pgvector-runbook.md:103`). Running that command under a Python without `psycopg` aborts at this import.

- **`scripts/bench_recall.py:107` — gap and directly exercisable:**
  - `PgvectorBenchProvider.__init__()` imports `psycopg` unconditionally and immediately opens a connection (`scripts/bench_recall.py:105`, `scripts/bench_recall.py:107`, `scripts/bench_recall.py:108`).
  - The script documents plain `python scripts/bench_recall.py` invocation (`scripts/bench_recall.py:18`), and the pgvector runbook also invokes it with plain `python` (`docs/pgvector-runbook.md:51`). A non-conda Python lacking `psycopg` therefore aborts when this provider is instantiated.

- **`tests/test_pgvector_integration.py:56` — gap, but the pgvector test is not exercisable without the dependency:**
  - `_skip_if_no_deps()` attempts the import; `ImportError` is converted into `unittest.SkipTest("psycopg not installed")` before any connection (`tests/test_pgvector_integration.py:54`, `tests/test_pgvector_integration.py:56`, `tests/test_pgvector_integration.py:57`, `tests/test_pgvector_integration.py:58`). Thus the import site runs, but the test skips rather than exercising Postgres.

- **`tests/test_pgvector_integration.py:285` — gap, but failure is intentionally swallowed:**
  - Teardown imports `psycopg` and connects inside a broad `try`; `ImportError` is covered by `except Exception: pass` (`tests/test_pgvector_integration.py:281`, `tests/test_pgvector_integration.py:285`, `tests/test_pgvector_integration.py:287`, `tests/test_pgvector_integration.py:293`). A non-conda interpreter therefore cannot exercise cleanup, but does not fail the test because of the missing dependency.

- **Gap summary:** the meaningful unprotected runtime sites are `claude_hooks/providers/pgvector.py:328`, `scripts/migrate_to_pgvector.py:624`, and `scripts/bench_recall.py:107`; `claude_hooks/providers/pgvector.py:123` is unprotected but protected by the installer’s surrounding fallback. The two test sites degrade to skip/silent cleanup rather than using conda.

---

## Researcher (round 1)

- **Unresolved runtime sites:** `claude_hooks/providers/pgvector.py:328`, `scripts/migrate_to_pgvector.py:624`, and `scripts/bench_recall.py:107` import `psycopg` without a conda-env subprocess fallback. Each is reachable from a non-conda interpreter, although behavior differs:
  - **Provider — genuinely exercisable, fail-soft:** `build_providers()` instantiates an enabled pgvector provider when a DSN is configured (`claude_hooks/dispatcher.py:129`, `claude_hooks/dispatcher.py:133`, `claude_hooks/dispatcher.py:140`, `claude_hooks/dispatcher.py:148`). `recall()` then calls `_ensure_ready()` (`claude_hooks/providers/pgvector.py:144`, `claude_hooks/providers/pgvector.py:148`); the import at `claude_hooks/providers/pgvector.py:328` raises `ImportError`, which is caught at `claude_hooks/providers/pgvector.py:149` and converted to an empty recall. A non-conda interpreter can be selected: the resolver falls back to `python3`/`python` on `PATH` (`bin/_resolve_python.sh:53`, `bin/_resolve_python.sh:54`, `bin/_resolve_python.sh:56`), and the Windows shim similarly falls back to `py -3` or `python` (`bin/claude-hook.cmd:43`, `bin/claude-hook.cmd:45`, `bin/claude-hook.cmd:50`).
  - **Migration script — genuinely exercisable, hard failure:** the bare import is in `main()` immediately before connecting (`scripts/migrate_to_pgvector.py:610`, `scripts/migrate_to_pgvector.py:624`, `scripts/migrate_to_pgvector.py:626`). The runbook invokes it with an unpinned `python` (`docs/pgvector-runbook.md:50`, `docs/pgvector-runbook.md:103`), so a system Python without `psycopg` raises `ModuleNotFoundError`.
  - **Benchmark script — genuinely exercisable, hard failure:** the import occurs when `PgvectorBenchProvider` is constructed (`scripts/bench_recall.py:105`, `scripts/bench_recall.py:107`, `scripts/bench_recall.py:108`). The runbook likewise invokes it using an unpinned `python` (`docs/pgvector-runbook.md:51`, `docs/pgvector-runbook.md:531`); such an interpreter fails at construction if `psycopg` is absent.

- **`claude_hooks/providers/pgvector.py:123` is not an unresolved installer path:** `PgvectorProvider.verify()` has no local conda fallback, but `_verify_pgvector_dsn()` probes `psycopg` in the current interpreter first (`install.py:2125`, `install.py:2127`) and, on `ImportError`, shells out to the conda Python (`install.py:2135`, `install.py:2138`, `install.py:2140`, `install.py:2146`). Its normal `PgvectorProvider.verify(candidate)` call is inside that guarded fast path (`install.py:2132`).

- **Other installer import sites already have the fallback:** `_pgvector_tables_present()` uses a current-interpreter fast path followed by a conda subprocess (`install.py:2522`, `install.py:2524`, `install.py:2536`, `install.py:2539`, `install.py:2557`); schema initialization does the same (`install.py:2645`, `install.py:2647`, `install.py:2653`, `install.py:2656`, `install.py:2673`). The import at `install.py:2092` is already inside the conda-targeted verification script (`install.py:2089`, `install.py:2146`), so it is not another missing-fallback site.

- **Dependency premise:** `psycopg` is optional, not part of the core runtime set: `requirements.txt` separates provider extras into `requirements-pgvector.txt` (`requirements.txt:12`, `requirements.txt:16`, `requirements.txt:20`), while that file declares `psycopg[binary]>=3.2` (`requirements-pgvector.txt:3`, `requirements-pgvector.txt:14`). The package metadata exposes it only through the `pgvector` optional extra (`pyproject.toml:38`). Therefore it is **not technically conda-only**—it can be installed into any interpreter—but the core install does not require or place it in a system interpreter. The runbook’s host preflight specifically checks/installs it in the `claude-hooks` conda environment (`docs/pgvector-runbook.md:733`, `docs/pgvector-runbook.md:734`, `docs/pgvector-runbook.md:737`).

- **Installer dependency-target result:** a project-wide search found no `requirements-pgvector` reference or `psycopg` package-install target in `install.py`; its only relevant `psycopg` occurrences are probe/connect code and fallback logic. Thus there is no evidence that `install.py` automatically installs `psycopg` into either interpreter.

- **Integration tests are guarded:** the dependency check catches missing `psycopg` and raises `unittest.SkipTest` (`tests/test_pgvector_integration.py:54`, `tests/test_pgvector_integration.py:56`, `tests/test_pgvector_integration.py:58`); missing Postgres or pgvector is also skipped (`tests/test_pgvector_integration.py:62`, `tests/test_pgvector_integration.py:67`, `tests/test_pgvector_integration.py:70`). The later import in teardown is enclosed by a broad exception handler (`tests/test_pgvector_integration.py:285`, `tests/test_pgvector_integration.py:293`).

- **Other test coverage is mock-only, not a live dependency:** the only other `psycopg`-mentioned test module is `tests/test_install_pgvector_verify.py`; its cases manipulate mocked `psycopg` modules and test the fallback behavior (for example, `tests/test_install_pgvector_verify.py:34`, `tests/test_install_pgvector_verify.py:39`, `tests/test_install_pgvector_verify.py:64`, `tests/test_install_pgvector_verify.py:70`). No additional live-`psycopg` test module was found.

---

## Synthesizer

**Bottom line:** 6 `import psycopg` sites outside `install.py` still lack the 4e67dc2-style conda-env subprocess fallback. 3 are genuinely reachable from a non-conda Python (provider `_ensure_ready`, `migrate_to_pgvector.py`, `bench_recall.py`); 1 more (`PgvectorProvider.verify`) is reachable in principle but every production caller is already shielded; the 2 test sites are guarded (skip / swallowed) and never actually need psycopg.

**The fallback shape being referenced** (as it exists in `install.py` today): fast-path `import psycopg` in the current interpreter → on `ImportError`, `find_conda_env_python()` → `subprocess.run([conda_py, "-c", script, dsn])` → parse JSON status. Implemented at `install.py:2125`/`install.py:2140`/`install.py:2146` (DSN verify), `install.py:2522`/`install.py:2540`/`install.py:2557` (table-present probe), `install.py:2645`/`install.py:2657`/`install.py:2673` (schema init). `find_conda_env_python()` is at `install.py:101`, returns a path callers `.exists()`-check (`install.py:104`).

## Sites lacking the fallback

| file:line | Exercisable from non-conda Python? | Behavior |
|---|---|---|
| `claude_hooks/providers/pgvector.py:328` | **Yes — fail-soft** | `_ensure_ready()` raises `ImportError("install psycopg: pip install psycopg[binary]")` (`claude_hooks/providers/pgvector.py:330`); `recall()` catches it (`claude_hooks/providers/pgvector.py:149`) and returns `[]` (`claude_hooks/providers/pgvector.py:151`). Silent empty recall instead of conda-backed pgvector. |
| `scripts/migrate_to_pgvector.py:624` | **Yes — hard failure** | Bare, unguarded import inside `main()` (`scripts/migrate_to_pgvector.py:610`), immediately before `psycopg.connect` (`scripts/migrate_to_pgvector.py:626`). Runbook invokes it with unpinned `python` (`docs/pgvector-runbook.md:50`, `docs/pgvector-runbook.md:103`) → `ModuleNotFoundError`. |
| `scripts/bench_recall.py:107` | **Yes — hard failure** | Import at `PgvectorBenchProvider.__init__` (`scripts/bench_recall.py:105`, `scripts/bench_recall.py:107`) followed by `psycopg.connect` (`scripts/bench_recall.py:108`). Script's own usage block says `python scripts/bench_recall.py` (`scripts/bench_recall.py:18`); runbook likewise (`docs/pgvector-runbook.md:51`, `docs/pgvector-runbook.md:531`). |
| `claude_hooks/providers/pgvector.py:123` | **Yes in principle, but every production caller is protected** | `verify()` returns `False` on `ImportError` (`claude_hooks/providers/pgvector.py:124`) — no conda fallback. Its installer caller is inside the guarded fast path (`install.py:2127` → `install.py:2132`), and the generic provider verify loop skips pgvector outright (`install.py:3547`, so the `cls.verify(candidate)` at `install.py:3556` never reaches it). The only other caller is the integration test (`tests/test_pgvector_integration.py:186`), which is skip-guarded. Net: reports "unreachable" rather than using conda. |
| `tests/test_pgvector_integration.py:56` | Import executes, but harmless | `_skip_if_no_deps()` converts `ImportError` to `unittest.SkipTest("psycopg not installed")` (`tests/test_pgvector_integration.py:57`, `tests/test_pgvector_integration.py:58`) before any connection attempt. |
| `tests/test_pgvector_integration.py:285` | Import executes, but harmless | Teardown import sits inside a broad `try` with `except Exception: pass` (`tests/test_pgvector_integration.py:293`), so cleanup is silently skipped rather than delegated to conda. |

## Why the provider site is reachable in practice

- `build_providers()` instantiates the provider whenever `providers.pgvector.enabled` is true and a DSN is configured (`claude_hooks/dispatcher.py:129 [no enabled at this line; line is in build_providers]`, `claude_hooks/dispatcher.py:131`, `claude_hooks/dispatcher.py:136`, `claude_hooks/dispatcher.py:148`); `recall()` then calls `_ensure_ready()` (`claude_hooks/providers/pgvector.py:148`).
- The hook can run under a non-conda interpreter: the POSIX resolver falls back to `python3`/`python` on `PATH` (`bin/_resolve_python.sh:53`, `bin/_resolve_python.sh:54`, `bin/_resolve_python.sh:56`), and the Windows shim to `py -3` / `python` (`bin/claude-hook.cmd:43`, `bin/claude-hook.cmd:45`, `bin/claude-hook.cmd:50`).
- The MCP server path is the same provider (`claude_hooks/pgvector_mcp/server.py:343`, `claude_hooks/pgvector_mcp/server.py:490`), but the launcher it writes pins the conda Python when present (`install.py:2296`, `install.py:2297`) — so the MCP route is usually conda-backed and the hook route is the exposed one.

## Sites that already have the fallback (not gaps)

`install.py:2092` (inside the conda-targeted verify script, run via `install.py:2146`), `install.py:2127`, `install.py:2524`, `install.py:2647` (guarded fast paths), `install.py:2546`, `install.py:2666` (imports inside the subprocess scripts, executed by the conda interpreter at `install.py:2557` / `install.py:2673`).

## Precondition (why the fallback matters at all)

`psycopg` is an optional extra, not core: `requirements.txt:12`, `requirements.txt:16`, `requirements.txt:20` split it into `requirements-pgvector.txt` (`requirements-pgvector.txt:14` declares `psycopg[binary]>=3.2`), and package metadata exposes it only as the `pgvector` extra (`pyproject.toml:38`). The runbook's host preflight checks/installs psycopg specifically in the `claude-hooks` conda env (`docs/pgvector-runbook.md:734`, `docs/pgvector-runbook.md:737`). So psycopg is *not* conda-only by declaration — it can be pip-installed anywhere — but the project never places it in a system interpreter, and `install.py` contains no `requirements-pgvector` install target. Hence the system interpreter legitimately lacks it, and the three unguarded runtime sites above are the real gap.

## Unverified

- The contents of commit `4e67dc2` could not be inspected: there is no `.git` directory in this checkout. The "added in 4e67dc2" attribution is inferred from the `install.py` docstrings describing the conda-env subprocess fallback (`install.py:2115`-`install.py:2124`, `install.py:2515`-`install.py:2520`) — not from the commit itself.
- No code was executed during this audit. All reachability claims are static traces (read of call sites, guards, and launcher scripts), not runtime-observed failures.
- `tests/test_install_pgvector_verify.py` covers the *fallback* with mocked `psycopg` modules (`tests/test_install_pgvector_verify.py:39`, `tests/test_install_pgvector_verify.py:70`, `tests/test_install_pgvector_verify.py:75`) but there is no equivalent test for the three unguarded sites.

**Suggested fix order:** `claude_hooks/providers/pgvector.py:328` (silent recall degradation — highest user impact, since the hook falls back to a non-conda interpreter automatically), then `scripts/migrate_to_pgvector.py:624` and `scripts/bench_recall.py:107` (loud crashes, but only in operator-run scripts where pinning the conda interpreter is a one-line fix).

---
