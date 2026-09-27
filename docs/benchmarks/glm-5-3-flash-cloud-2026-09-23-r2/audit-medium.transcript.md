# Consultation transcript — csl-2026-09-23-2029-e1eb

_Question_: Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python.


## Planner

1. Grep the repo (excluding `.claude-hooks/` transcripts) for `import psycopg`, `from psycopg`, `psycopg_pool`, and `importlib`+`"psycopg"` to freeze the canonical site list — expected: install.py:2092, 2127, 2524, 2546, 2647, 2666; claude_hooks/providers/pgvector.py:123, 328; scripts/migrate_to_pgvector.py:624; scripts/bench_recall.py:107; tests/test_pgvector_integration.py:56, 285 — and flag any stragglers.
2. Read install.py:2115–2160, 2507–2565, 2640–2685 and classify each site: fast-path-import-with-conda-subprocess-fallback (2127, 2524, 2647) vs. embedded script-strings that only ever execute inside the conda-env subprocess itself (2092, 2546, 2666) — the latter are the fallback machinery, not gaps.
3. Grep for callers of `PgvectorProvider.verify` and `PgvectorProvider(` across `claude_hooks/` and install.py, then read those call sites, to determine which runtime paths actually reach pgvector.py:123 (`verify` → returns False on ImportError) and pgvector.py:328 (`_ensure_ready` → raises ImportError, caught as [] in recall at 149, RuntimeError in store at 217).
4. For the two scripts, establish exercisability from a non-conda-env interpreter: read scripts/migrate_to_pgvector.py:610–637 (`main()` does a bare `import psycopg` at 624 with no handler) and scripts/bench_recall.py's provider-selection argument parsing to confirm `PgvectorBenchProvider.__init__` (107) is only hit when `--provider pgvector` is chosen and that ImportError propagates as a traceback.
5. Confirm the production interpreter: read bin/claude-hook.cmd, bin/claude-hooks-daemon.cmd, and systemd/claude-hooks-daemon.service to see whether hooks/daemon launch under the conda-env python — if yes, the pgvector.py:123/:328 gaps are latent rather than live in production.
6. Call recall_memory for notes on commit 4e67dc2 to confirm the fallback's intended scope (install.py-only vs project-wide); no git tool is available, so if memory is empty, state the pattern is inferred solely from install.py docstrings (2118–2123, 2515–2520, 2631–2634).
7. Emit the verdict per site as file:line → (fallback present? exercisable from non-conda python? failure mode: graceful False / silent empty results / ImportError crash), explicitly separating "covered by 4e67dc2 pattern" from "unguarded" and from "runs only inside the conda subprocess".

---

## Researcher (round 1)

**Report: psycopg import-site audit (4e67dc2 conda-env subprocess fallback)**

Canonical site list confirmed (excluding `.claude-hooks/` transcript WAL, which only echoes these same lines): `install.py:2092, 2127, 2524, 2546, 2647, 2666`; `claude_hooks/providers/pgvector.py:123, 328`; `scripts/migrate_to_pgvector.py:624`; `scripts/bench_recall.py:107`; `tests/test_pgvector_integration.py:56, 285`. No stragglers found beyond these.

**install.py — all three fast-path sites have the fallback; no gaps**
- `install.py:2115–2147` — `_verify_pgvector_dsn`: fast path `import psycopg` at `install.py:2127`, `except ImportError: pass` at 2135, then `find_conda_env_python()` subprocess running `_PGVECTOR_VERIFY_SCRIPT` (2140–2148). Fallback present. ✅
- `install.py:2507–2558` — `_pgvector_tables_present`: fast path `import psycopg` at `install.py:2524`, ImportError → conda subprocess with embedded probe script (2540–2558). Fallback present. ✅
- `install.py:2647` — `_init_pgvector_schema` fast path; ImportError → conda subprocess with DDL piped via stdin (2656–2676); raises clear RuntimeError only if conda env missing (2658–2663). Fallback present. ✅
- **Fallback machinery, not gaps**: `install.py:2092` is inside the `_PGVECTOR_VERIFY_SCRIPT` raw string (2089–2112); `install.py:2546` and `install.py:2666` are lines inside embedded script strings (2543–2555, 2664–2672). These execute only inside the conda-env subprocess itself — they can never run in the outer interpreter, so no fallback is needed/possible there. Not exercisable from non-conda Python.

**claude_hooks/providers/pgvector.py — graceful, no conda fallback by design**
- `claude_hooks/providers/pgvector.py:123` — `verify()`: `except ImportError` → logs warning, returns False (124–126). Only direct caller is `install.py:2132`, which is reachable **only on the fast path where psycopg just imported successfully** (2127) — so the ImportError branch is unreachable in practice from a non-conda interpreter via that route. No gap: failure degrades to `False`, never crashes.
- `claude_hooks/providers/pgvector.py:328` — `_ensure_ready()`: ImportError → raises with install hint (329–330). Caught in `recall()` at `pgvector.py:149` → returns `[]` (graceful). In `store()` at `pgvector.py:216–217` → converted to `RuntimeError`. Runtime exercisable from non-conda Python if `pgvector` provider is enabled in config, but degrades safely on recall; store failure propagates to the Stop handler (dispatcher-side catch not verified — evidence gap). Provider itself has no conda-subprocess fallback; this is deliberate graceful-degradation, not a 4e67dc2 regression site.

**scripts — two real, exercisable gaps**
- `scripts/migrate_to_pgvector.py:624` — bare `import psycopg` in `main()` (610) with **no try/except and no conda-env fallback**; `psycopg.connect(cfg.dsn)` immediately at 626. Exercisable: yes — `python scripts/migrate_to_pgvector.py` from system Python hits a raw `ModuleNotFoundError` traceback. **Confirmed gap.**
- `scripts/bench_recall.py:107` — `PgvectorBenchProvider.__init__` bare `import psycopg` (107–108), no handler. Exercisable: yes, and **worse than the planner assumed** — there is no `--provider` flag; `main()` builds `PgvectorBenchProvider` for every selected model at `bench_recall.py:239`, and `--models` defaults to `"all"` (`bench_recall.py:195`, expansion at 228–231). So the **default invocation** `python scripts/bench_recall.py` from a non-conda interpreter raises ImportError as a traceback. Only `--skip-qdrant` exists as a provider toggle (197); it doesn't gate pgvector. **Confirmed gap, hit by default.**

**Evidence gaps (not read this turn)**
- `tests/test_pgvector_integration.py:56, 285` — sites exist per grep but I did not read the file, so cannot confirm whether they're `skipif`-guarded or mocked. Recommend the critic treat these as unverified.
- Dispatcher-side handling of the `RuntimeError` raised by `pgvector.py:217` during Stop-store: not grepped/read; unverified.

**Summary**: 4e67dc2 fallback is complete in `install.py` (fast paths 2127/2524/2647; embedded scripts 2092/2546/2666 are the machinery itself). Remaining non-conda-exercisable gaps: `scripts/migrate_to_pgvector.py:624` (invoked explicitly) and `scripts/bench_recall.py:107` (hit on **default** invocation). `pgvector.py:123/328` degrade gracefully and are correctly fallback-free.

---

## Researcher (round 1)

## Research report — psycopg import sites vs the 4e67dc2 conda-subprocess fallback

**Production interpreter resolution (all launch paths prefer conda env):**
- POSIX: `bin/_resolve_python.sh:31-50` — candidate order is repo-venv → conda env (`anaconda3`/`miniconda3` `envs/claude-hooks`) → system python3. Conda env wins over system python whenever it exists (`bin/_resolve_python.sh:35-44`).
- Windows: `bin/claude-hook.cmd:36-41` and `bin/claude-hook-pgvector-mcp.cmd:28-34` — same ordering (`CLAUDE_HOOKS_PY` → `.venv` → conda env → `py -3`/`python`). System python is only reached when neither venv nor conda env exists.
- Daemon: `systemd/claude-hooks-daemon.service:14` execs `bin/claude-hooks-daemon`, which sources `_resolve_python.sh` (`bin/claude-hooks-daemon:21-28`) → also runs under conda-env python in a normal install.
- Note: with a repo-local `.venv` present, the **venv** wins over conda (`bin/_resolve_python.sh:32-34`) — whether psycopg is importable there depends on the venv's installs, not the conda env.

**The 4e67dc2 fallback itself:** fast-path `import psycopg` then subprocess out to `find_conda_env_python()` — `install.py:2115-2150` (`_verify_pgvector_dsn`); same pattern repeated in the schema-init helper (`install.py:2516-2547`) and a third verifier (`install.py:2632-2668`). Scope is explicitly install.py-only per docstring: "install.py is often invoked with whatever python is on PATH" (`install.py:2118-2123`). No project-wide policy — `CONTRIBUTING.md:44` only mandates lazy `try/except ImportError`, not a conda fallback. recall_memory returned no commit-specific notes (only env-detection anecdotes), so the commit's intended scope is **inferred solely from install.py docstrings**, as the planner anticipated.

**Per-site verdicts (runtime import sites only):**

- `claude_hooks/providers/pgvector.py:123` (`verify` classmethod) — **unguarded, but graceful**: bare `try/except ImportError` → `log.warning` + `return False` (`pgvector.py:124-126`). Exercisable from non-conda python? Yes (it's a classmethod called directly by `install.py:2132` fast path; also called by MCP server). Failure mode: **graceful False**, never crashes. Note: the 4e67dc2 fallback in `install.py:2135-2149` exists precisely to compensate when this returns False spuriously — so this site is *covered by* the pattern, though indirectly.

- `claude_hooks/providers/pgvector.py:328` (`_ensure_ready`) — **unguarded fallback absent**: raises `ImportError("install psycopg: pip install psycopg[binary]")` (`pgvector.py:329-330`). But every caller wraps it: `recall` catches `ImportError` → `[]` (`pgvector.py:147-151`), and `store`/`batch_recall`/`batch_store`/`_delete_*` all call `_ensure_ready()` at `pgvector.py:214,241,265,292,439,555,592,645,676` with similar handlers (verified for `recall`; the other call sites' except clauses were not individually read). Failure mode from non-conda python: **graceful — silent empty results + warning log**, no crash. Exercisable from non-conda python: yes, if the provider is enabled in config and the interpreter lacks psycopg. This is the only production site where the conda-subprocess fallback pattern genuinely does *not* apply and isn't needed for crash-safety — but it means recall silently returns nothing instead of probing the conda env, the exact "misleading failure" 4e67dc2 fixed in install.py.

- `scripts/migrate_to_pgvector.py:624` (`main`) — **unguarded**: bare `import psycopg` → `psycopg.connect(cfg.dsn)` (`migrate_to_pgvector.py:626`). Exercisable from non-conda python: **yes** — operator-run CLI. Failure mode: **ImportError crash** (traceback). No conda fallback, no try/except.

- `scripts/bench_recall.py:107` (`PgvectorBenchProvider.__init__`) — **unguarded**: bare `import psycopg` + connect (`bench_recall.py:107-108`). Exercisable from non-conda python: **yes**. Failure mode: **ImportError crash** at benchmark construction.

- Test files (not production, for completeness): `tests/test_pgvector_integration.py:56-58` skips gracefully without psycopg; `tests/test_pgvector_integration.py:285` and `tests/test_install_pgvector_verify.py` use mocks/skips. Not exercisable harm in CI.

**Bottom line:** sites genuinely lacking the 4e67dc2 pattern and crashing from a non-conda interpreter: `scripts/migrate_to_pgvector.py:624` and `scripts/bench_recall.py:107` (both operator CLIs, no fallback, ImportError). In production hook paths (`pgvector.py:123`, `pgvector.py:328`) the gaps are **latent, not live**: all production launchers (`bin/_resolve_python.sh:35-44`, `bin/claude-hook.cmd:36-41`, `bin/claude-hook-pgvector-mcp.cmd:28-34`, daemon via `systemd/claude-hooks-daemon.service:14` → `bin/claude-hooks-daemon:21`) resolve to the conda-env python where psycopg is installed, and both provider sites degrade gracefully (`False` / empty results) rather than crash even when the interpreter lacks psycopg. Caveat: repo-local `.venv` outranks conda in all resolvers — if that venv exists without psycopg, the provider sites become live silent-empty (still not crashes).

---

## Synthesizer

**Verdict: the 4e67dc2 conda-env subprocess fallback is fully present in `install.py`; the only exercisable, crash-capable gaps are `scripts/migrate_to_pgvector.py:624 [no find_conda_env_python at this line; line is in main]` and `scripts/bench_recall.py:107` — and the bench script hits its bare import on the *default* invocation.**

**Covered by the 4e67dc2 pattern (no gaps)**
- `install.py:2127` (`_verify_pgvector_dsn` fast path): `try/except ImportError` → conda-env subprocess running `_PGVECTOR_VERIFY_SCRIPT` (install.py:2135–2149). ✅
- `install.py:2524` (`_pgvector_tables_present` fast path): ImportError → conda subprocess with embedded probe (2540–2558). ✅
- `install.py:2647` (`_init_pgvector_schema` fast path): ImportError → conda subprocess piping DDL via stdin; clear `RuntimeError` only if the conda env is missing (2656–2676). ✅
- Scope of the pattern is install.py-only, per its own docstring ("install.py is often invoked with whatever python is on PATH", install.py:2118–2123). `CONTRIBUTING.md:44` mandates only lazy `try/except ImportError`, no conda fallback project-wide.

**Fallback machinery, not gaps — not exercisable from a non-conda interpreter**
- `install.py:2092`, `install.py:2546`, `install.py:2666` are `import psycopg` lines *inside embedded script strings* (the `_PGVECTOR_VERIFY_SCRIPT` raw string at 2089–2112 and the two inline probes). They execute only inside the conda-env subprocess itself, so they can never run in the outer interpreter.

**Production runtime sites — no conda fallback, but graceful (latent, not crashes)**
- `claude_hooks/providers/pgvector.py:123` (`verify`): `except ImportError` → warning + `return False` (pgvector.py:124–126). Failure mode: graceful `False`. Reachability differs by report: report 1 notes the install.py caller only invokes it on the fast path where psycopg just imported; report 2 adds the MCP server as a caller. Either way, never crashes.
- `claude_hooks/providers/pgvector.py:328` (`_ensure_ready`): raises `ImportError` with an install hint (pgvector.py:329–330). `recall` catches it → returns `[]` (pgvector.py:147–151) — i.e., **silent empty results**; `store` converts it to `RuntimeError` (pgvector.py:216–217). Verified call sites: 214, 241, 265, 292, 439, 555, 592, 645, 676; only `recall`'s handler was read line-by-line, and the dispatcher-side handling of the store `RuntimeError` was **not verified**.
- These gaps are latent in production: every launcher prefers the conda env — `bin/_resolve_python.sh:35–44` (POSIX), `bin/claude-hook.cmd:36–41` and `bin/claude-hook-pgvector-mcp.cmd:28–34` (Windows), daemon via `systemd/claude-hooks-daemon.service:14` → `bin/claude-hooks-daemon:21–28`. Caveat: a repo-local `.venv` outranks conda in all resolvers (`bin/_resolve_python.sh:32–34`); if that venv lacks psycopg, the provider sites become live silent-empty (still not crashes).

**Exercisable gaps (missing the 4e67dc2 pattern, ImportError crash from non-conda Python)**
- `scripts/migrate_to_pgvector.py:624 [no find_conda_env_python at this line; line is in main]` — bare `import psycopg` in `main()` with no handler, followed immediately by `psycopg.connect(cfg.dsn)` (626). `python scripts/migrate_to_pgvector.py` from system Python → raw `ModuleNotFoundError` traceback. Operator-invoked only.
- `scripts/bench_recall.py:107` — bare `import psycopg` in `PgvectorBenchProvider.__init__` (107–108). **Worse: no `--provider` flag gates it** — `main()` builds the provider for every selected model at bench_recall.py:239, and `--models` defaults to `"all"` (bench_recall.py:195, expansion at 228–231); only `--skip-qdrant` exists (197) and it doesn't gate pgvector. So the *default* `python scripts/bench_recall.py` crashes with ImportError.

**Tests (not production harm)**
- `tests/test_pgvector_integration.py:56–58` — skips gracefully without psycopg; `tests/test_pgvector_integration.py:285` and `tests/test_install_pgvector_verify.py` — mocks/skips. No exercisable failure in CI.

**Recommendation**: add the conda-env subprocess fallback (or at minimum a `try/except ImportError` with the same `find_conda_env_python()` probe used in install.py) to `scripts/migrate_to_pgvector.py:624 [no find_conda_env_python at this line; line is in main]` and `scripts/bench_recall.py:107`; optionally make `pgvector.py:328`'s recall path probe the conda env instead of silently returning `[]`, which is exactly the "misleading failure" mode 4e67dc2 fixed in install.py.

---
