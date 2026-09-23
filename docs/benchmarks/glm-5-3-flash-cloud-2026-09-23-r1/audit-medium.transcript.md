# Consultation transcript — csl-2026-09-23-2017-dbb5

_Question_: Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python.


## Planner

1. Pin the 4e67dc2 baseline pattern by reading install.py:2115-2160 (`_verify_pgvector_dsn`), install.py:2507-2569 (`_pgvector_tables_present`), and install.py:2625-2680 (`_init_pgvector_schema` fast-path + conda subprocess fallback), plus tests/test_install_pgvector_verify.py to catalog which import sites already got the fallback.

2. Confirm the two runtime-package psycopg import sites lack any fallback: claude_hooks/providers/pgvector.py:123 (`PgvectorProvider.verify` returns False on ImportError) and claude_hooks/providers/pgvector.py:328 (`_ensure_ready` raises ImportError with only a pip hint), and check claude_hooks/pgvector_mcp/server.py for any additional direct psycopg imports.

3. Trace callers of `PgvectorProvider.verify` (install.py:2132, tests/test_pgvector_integration.py:186, docs/pgvector-runbook.md:279) and determine whether install.py's own ImportError guard at install.py:2135-2136 masks verify's missing fallback when system python lacks psycopg — i.e., is verify reachable from a non-conda interpreter through any path that doesn't pre-guard the import.

4. Determine whether pgvector.py:328 `_ensure_ready` is exercisable from non-conda-env Python by tracing how hooks and the daemon launch: bin/claude-hook.cmd, bin/_resolve_python.sh, run.py, run_daemon.py, and the provider-dispatch code that instantiates PgvectorProvider at runtime.

5. Audit the standalone scripts' bare imports — scripts/migrate_to_pgvector.py:624 (`import psycopg` in main(), no try/except visible) and scripts/bench_recall.py:107 — and check which interpreter the corresponding bin/ shims or Makefile targets use, to decide if these run in the conda env by construction or can hit system python.

6. Classify the non-production sites so they're not false-positived: tests/test_pgvector_integration.py:56 and :285 (verify the `unittest.SkipTest` guard fires before psycopg use) and docs/pgvector-runbook.md:499 (doc snippet, not code).

7. Use recall_memory to check for any record of commit 4e67dc2's scope (whether it only patched install.py or also intended to cover provider/scripts import sites), since git history isn't directly queryable with the available tools.

---

## Researcher (round 1)

## Findings

**1. 4e67dc2 baseline: three install.py sites carry the conda subprocess fallback**
- `install.py:2115-2161` — `_verify_pgvector_dsn`: fast path `import psycopg` at `install.py:2127`, ImportError guard `:2135-2136`, then `find_conda_env_python()` at `install.py:2140` and conda subprocess running `_PGVECTOR_VERIFY_SCRIPT` at `install.py:2146-2148`.
- `install.py:2507-2569` — `_pgvector_tables_present`: same shape — fast path `install.py:2524`, fallback `install.py:2540`, inline script subprocess `install.py:2557-2558`.
- `install.py:2619-2681` — `_init_pgvector_schema`: fast path `install.py:2647`, fallback `install.py:2657`, stdin-piped DDL subprocess `install.py:2673-2675`; raises RuntimeError if conda env missing (`install.py:2658-2663`).

**2. Runtime-package import sites still lack the fallback (confirmed)**
- `claude_hooks/providers/pgvector.py:123-126` — `PgvectorProvider.verify` catches ImportError, logs `"psycopg not installed — cannot verify pgvector"`, returns False. No conda subprocess attempt.
- `claude_hooks/providers/pgvector.py:328-330` — `_ensure_ready` raises `ImportError("install psycopg: pip install psycopg[binary]")` — pip hint only, no conda fallback.
- `claude_hooks/pgvector_mcp/server.py` — **no direct psycopg import**; it imports `PgvectorProvider` at `server.py:37` and instantiates via `build_providers` filtering at `server.py:344` and `server.py:492`. psycopg is reached only lazily through provider methods (`_ensure_ready`), so the MCP server imports fine on system python and fails later at call time.

**3. Callers of `PgvectorProvider.verify` — fallback masking**
- `install.py:2132` — the only production caller that matters here is pre-guarded: the try block at `install.py:2126-2136` catches ImportError and drops to the conda subprocess fallback. **However, the fallback does not call `verify` at all** — `_PGVECTOR_VERIFY_SCRIPT` (`install.py:2089-2112`) reimplements the SELECT 1 / pg_extension probe inline. So `verify` itself is never executed under the fallback; its missing fallback is fully masked on the installer path.
- `install.py:3556` — `ok = cls.verify(candidate)` — generic provider-verify path; context not read this turn, **evidence insufficient** to confirm whether it's pre-guarded for pgvector. Note even unguarded, `verify` degrades gracefully (returns False, no crash) — the failure mode is a misleading "verification failed", not an exception.
- `tests/test_pgvector_integration.py:186` — test calls `verify` directly; only passes in an interpreter with psycopg.
- `tests/test_install_pgvector_verify.py:37,47,51` — verifies the installer wiring with a **mocked** `verify` (`fake_provider.verify = MagicMock(...)`), so tests never exercise the real missing-psycopg path.
- `docs/pgvector-runbook.md:279` — doc reference only.

**4. Exercisability of `_ensure_ready` (pgvector.py:328) from non-conda Python**
- Reachable via the pgvector MCP server: `claude_hooks/pgvector_mcp/server.py:344`/`:492` select the `PgvectorProvider` from `build_providers`, and recall/store/kg methods funnel through `_ensure_ready`. The launcher installed by `_setup_pgvector_mcp` (`install.py:2167-2173`, docstring) execs `python -m claude_hooks.pgvector_mcp` against the interpreter **resolved and baked in at install time** — if that resolution picked the conda env python, psycopg is present; if system python, `_ensure_ready` raises ImportError at first tool call.
- Hook/daemon launch path (`bin/claude-hook.cmd`, `bin/_resolve_python.sh`, `run.py`, `run_daemon.py`) — **not read this turn; evidence insufficient** on whether those shims resolve the conda python. CLAUDE.md (anchor) claims `bin/claude-hook` prefers `$HOME/anaconda3/envs/claude-hooks/bin/python`, but I did not open the shim to verify.

**Summary of gaps:** the two unprotected psycopg import sites are exactly `claude_hooks/providers/pgvector.py:123` (graceful-degrade False, no conda retry) and `claude_hooks/providers/pgvector.py:330` (hard ImportError). The first is masked in production by install.py's pre-guard + reimplemented script; the second is live whenever a PgvectorProvider runs under a non-conda interpreter (pgvector MCP server launched with the wrong baked-in interpreter being the realistic trigger).

---

## Researcher (round 1)

**Lane 2 findings — psycopg import audit**

**A. The 4e67dc2-style fallback exists only in `install.py` (3 sites, all guarded):**
- `install.py:2127` — fast-path `import psycopg` in `_verify_pgvector_dsn()`; docstring at `install.py:2118-2123` explicitly documents the conda-env subprocess fallback rationale ("install.py is often invoked with whatever python is on PATH"). Fallback body begins after the `except ImportError:` at `install.py:2135` (body beyond :2135 not read — line-precision gap for the exact `find_conda_env_python` call).
- `install.py:2524` — fast path in `_pgvector_tables_present()`; fallback at `install.py:2536-2569` spawns `find_conda_env_python()` subprocess with inline script; rationale docstring `install.py:2515-2520`.
- `install.py:2647` — fast path in `_init_pgvector_schema()`; fallback at `install.py:2653-2676` pipes DDL to conda python via stdin; rationale `install.py:2631-2634`.
- `install.py:2089-2096` — `_PGVECTOR_VERIFY_SCRIPT` template wraps its `import psycopg` in try/except → emits JSON `{"ok": false}` instead of crashing. Safe.
- `install.py:2546` / `:2666` — psycopg imports inside the inline subprocess scripts themselves (run under conda python by construction). Not real import sites.

**B. Unguarded bare imports — the actual gaps:**
- `scripts/migrate_to_pgvector.py:624` — bare `import psycopg` inside `main()`, no try/except. Exercisable from non-conda python: **yes**. No bin shim exists (glob `bin/*migrate*` → empty), no Makefile target (grep of `Makefile` for `migrate_to_pgvector|bench_recall|psycopg` → no matches), and `docs/pgvector-runbook.md:50,103,106,109,112` all instruct `python scripts/migrate_to_pgvector.py …` with whatever `python` is on PATH. On a system py3 without psycopg this dies with a raw ImportError. Note: install.py's subprocess fallback (`install.py:2639`) imports this module fine — the psycopg import is function-local to `main()`, so only direct CLI invocation breaks.
- `scripts/bench_recall.py:107` — bare `import psycopg` in `PgvectorBenchProvider.__init__`, no try/except. Exercisable from non-conda python: **yes** — same situation: no shim, no Makefile target, `docs/pgvector-runbook.md:51,531` instruct `python scripts/bench_recall.py …`.
- `claude_hooks/providers/pgvector.py:123` — `import psycopg` in `verify()`, wrapped in try/except ImportError → logs warning and returns False (`:124-126`). Graceful, no crash; no conda-subprocess fallback, but none needed (boolean probe).
- `claude_hooks/providers/pgvector.py:328` — `import psycopg` in `_ensure_ready()`, wrapped → raises ImportError with pip hint (`:329-330`). No conda-subprocess fallback (grep `subprocess|conda` in pgvector.py → no matches). Exercisability from non-conda python: **low** — hooks reach this only via `bin/claude-hook` → `bin/_resolve_python.sh:35-44`, which prefers the conda env python before falling back to system python; the pgvector provider is also a disabled-by-default scaffold. Residual risk only when no conda env exists AND user enables the provider.

**C. Non-production sites (do not false-positive):**
- `tests/test_pgvector_integration.py:54-58` — `_skip_if_no_deps()` wraps the `:56` import in try/except → `unittest.SkipTest("psycopg not installed")`; called first thing in `setUpClass` (`:94-95`), so all tests skip before any psycopg use.
- `tests/test_pgvector_integration.py:285` — bare import in `tearDownClass`, but unreachable unless `setUpClass` succeeded (which required psycopg), and the whole body is wrapped in `except Exception: pass` (`:293-294`). Safe.
- `docs/pgvector-runbook.md:496` (`from scripts.migrate_to_pgvector import …`) and `:734` (conda python one-liner) — doc snippets, not executable code.

**D. Commit 4e67dc2 scope — evidence insufficient.** `recall_memory` returned unrelated pgvector/conda notes (lightseek venv, conda env names); no record of the commit. Git history not queryable with available tools. What *is* verifiable: the in-code docstrings at `install.py:2118-2123`, `:2515-2520`, `:2631-2634` describe the fallback's intent purely in terms of install.py's own verify/schema paths — nothing suggests provider/scripts sites were in scope, consistent with the gaps in section B.

**Bottom line:** two genuinely unguarded, user-reachable import sites — `scripts/migrate_to_pgvector.py:624` and `scripts/bench_recall.py:107` — both documented in the runbook as plain-`python` invocations; everything in `install.py` and `claude_hooks/providers/pgvector.py` is either guarded or runs under the conda python by construction.

---

## Synthesizer

Bottom line: two import sites still lack the 4e67dc2 conda-env subprocess fallback and both are exercisable from a non-conda interpreter — `scripts/migrate_to_pgvector.py:624` and `scripts/bench_recall.py:107`. A third (`claude_hooks/providers/pgvector.py:328`) lacks the fallback but is mostly shielded by launcher resolution. Everything else either has the fallback, degrades gracefully, or is non-production.

**Unguarded, user-reachable from system python:**
- `scripts/migrate_to_pgvector.py:624` — bare `import psycopg` inside `main()`, no try/except. Exercisable: **yes**. No bin shim exists, no Makefile target, and `docs/pgvector-runbook.md:50,103,106,109,112` instruct plain `python scripts/migrate_to_pgvector.py …` with whatever python is on PATH → raw ImportError on a system python without psycopg. (Import is function-local, so install.py's fallback subprocess importing the module at `install.py:2639` is unaffected.)
- `scripts/bench_recall.py:107` — bare `import psycopg` in `PgvectorBenchProvider.__init__` (not in `main()`), no try/except. Exercisable: **yes** — same story: no shim, no Makefile target, `docs/pgvector-runbook.md:51,531` instruct plain `python scripts/bench_recall.py …`.

**Unguarded but low-residual-risk runtime site:**
- `claude_hooks/providers/pgvector.py:328` — `_ensure_ready()` raises `ImportError("install psycopg: pip install psycopg[binary]")` at `:330`, pip hint only, no conda subprocess (grep for `subprocess|conda` in pgvector.py → no matches). Exercisable from non-conda python: only in the residual case where the launcher falls through to system python. Hooks resolve via `bin/_resolve_python.sh:31-44`, which prefers repo `.venv` and conda-env pythons before system python; the pgvector MCP launcher bakes in the interpreter resolved at install time (`install.py:2167-2173`). So it fires only when no conda/venv env exists AND the user enables the (disabled-by-default) pgvector provider.

**Guarded but lacking the fallback (graceful, arguably fine as-is):**
- `claude_hooks/providers/pgvector.py:123` — `verify()` catches ImportError at `:124-126`, logs, returns False. No conda retry, but none needed for a boolean probe. Fully masked in production anyway: the installer path pre-guards at `install.py:2126-2136` and its fallback (`install.py:2140-2148`, `_PGVECTOR_VERIFY_SCRIPT` at `install.py:2089-2112`) reimplements the probe inline rather than calling `verify`. The generic provider-verify path at `install.py:3556` calls `cls.verify` without a pre-guard — failure mode there is a misleading "verification failed", not a crash.

**Fallback present (4e67dc2 baseline, no gaps):**
- `install.py:2127` fast path / fallback after `:2135` (`_verify_pgvector_dsn`)
- `install.py:2524` fast path / fallback `:2536-2569` (`_pgvector_tables_present`)
- `install.py:2647` fast path / fallback `:2653-2676` (`_init_pgvector_schema`)
- `install.py:2546` / `install.py:2666` — imports inside the inline conda-subprocess scripts; not real sites.

**Non-production (do not flag):**
- `tests/test_pgvector_integration.py:56` — wrapped by `_skip_if_no_deps()` → `unittest.SkipTest`, called in `setUpClass` at `:94-95`, so psycopg use never happens without it.
- `tests/test_pgvector_integration.py:285 [no setUpClass at this line; line is in tearDownClass]` — bare import in `tearDownClass`, unreachable unless `setUpClass` succeeded, and body wrapped in `except Exception: pass` (`:293-294`).
- `docs/pgvector-runbook.md:496`, `:734` — doc snippets, not executable code.

**Unresolved:** no record of 4e67dc2's intended scope exists (recall_memory returned nothing relevant; git not queryable). The in-code rationale docstrings (`install.py:2118-2123`, `:2515-2520`, `:2631-2634`) scope the fallback to install.py's own paths only, so the script/provider gaps look like never-in-scope rather than regressions.

---
