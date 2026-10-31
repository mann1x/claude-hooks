---
session_id: csl-2026-10-22-1815-967d
created: "2026-10-22T18:15:54"
question: "Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python."
models:
  planner: "minimax-m3.1-flash:cloud"
  researcher: "minimax-m3.1-flash:cloud"
  synthesizer: "minimax-m3.1-flash:cloud"
topology: council
effort: medium
duration_seconds: 175.29
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-10-22/tmp/claude-hooks-bench-bench-baseline-2026-05-07-6kcB
---

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
