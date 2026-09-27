---
session_id: csl-2026-09-23-1811-5628
created: "2026-09-23T18:11:00"
question: "Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python."
models:
  planner: "deepseek-v4.1-flash:cloud"
  researcher: "deepseek-v4.1-flash:cloud"
  synthesizer: "deepseek-v4.1-flash:cloud"
topology: council
effort: medium
duration_seconds: 73.55
status: completed
truncated: planner=1
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-09-23/tmp/claude-hooks-bench-bench-baseline-2026-05-07-2JCu
---

Six psycopg import sites lack the conda-env subprocess fallback; only **two** are actually exercisable as failures from a non-conda-env Python — `scripts/migrate_to_pgvector.py:624` and `scripts/bench_recall.py:107 [no main at this line; line is in __init__]`. Every psycopg import in the repo is function- or method-scoped, so there is **no import-time crash surface**: importing these modules under a non-conda interpreter succeeds and failure is deferred to call time (and already downgraded at 4 of the 6 sites).

**Missing fallback AND exercisable (actionable)**

- `scripts/migrate_to_pgvector.py:624` — bare `import psycopg` inside `main()` (defined `scripts/migrate_to_pgvector.py:610`), executed after `build_config` at `:616`; **no `try/except`**. Reached by direct invocation, which the docs mandate: `scripts/migrate_to_pgvector.py:18` and `docs/pgvector-runbook.md:50`. Verdict: real gap, crashes `ModuleNotFoundError`. Note `install.py:2639` imports `MODELS, schema_sql_for_model` from this module without running `main()`, so the installer path does not trip it.
- `scripts/bench_recall.py:107 [no main at this line; line is in __init__]` — bare `import psycopg` inside `PgvectorBenchProvider.__init__` (class at `scripts/bench_recall.py:105`); **no `try/except`**. Verdict: real gap, gated on the pgvector benchmark path being selected.

**Missing fallback, but guarded (not fatal)**

- `claude_hooks/providers/pgvector.py:123` — lazy import in `verify()` (defined `claude_hooks/providers/pgvector.py:120`); `except ImportError` at `:124` logs and `return False` (`:124-126`). Degrades to "cannot verify" instead of probing the conda interpreter. Exercisability is an evidence gap: neither lane grepped for `.verify(` callers, so whether a non-conda interpreter ever reaches it is unresolved (`install.py:2132` suggests it is only reached after psycopg already imported).
- `claude_hooks/providers/pgvector.py:328` — lazy import in `_ensure_ready()` (defined `:320`); `except ImportError` re-raises (`:329-330`), but `recall()` catches `(ImportError, EmbedderError)` at `claude_hooks/providers/pgvector.py:148-150`. Not fatal on the hook path. The MCP entry is reached only via the shim `bin/claude-hook-pgvector-mcp:17` → `_resolve_python.sh:35-44`, which prefers `$HOME/{,mini,}conda*/envs/claude-hooks/**` over `python3` (`_resolve_python.sh:53-59`) — so not exercisable from a non-conda interpreter in practice once the conda env exists. (Researcher 1 additionally notes `CLAUDE.md` states pgvector is registered but disabled by default.)
- `tests/test_pgvector_integration.py:56 [no md at this line; line is in _skip_if_no_deps]` — `_skip_if_no_deps()` (defined `:54`) converts `ImportError` into `unittest.SkipTest` at `:58`. Not a bug.
- `tests/test_pgvector_integration.py:285 [no SkipTest at this line; line is in tearDownClass]` — `tearDownClass` (defined `:281`) with blanket `except Exception: pass` at `:293`. Not a bug.

**Correctly lacking the fallback (inside the conda child, by construction)**

- `install.py:2092` / `:2098` live in the `_PGVECTOR_VERIFY_SCRIPT` raw string opened at `install.py:2089`, executed only under the conda interpreter via `install.py:2147`.
- `install.py:2546` / `:2547` are inside the inline `script` string built at `install.py:2543`, run at `install.py:2558`.
- `install.py:2666` / `:2668` are inside the inline `script` string built at `install.py:2664`, run at `install.py:2674`.
- The protected fast paths themselves — `install.py:2127`, `install.py:2524`, `install.py:2647` — each sit directly above an explicit conda fallback: `install.py:2140`/`:2146`, `install.py:2540`/`:2557`, `install.py:2657`/`:2673`.

**Load-bearing caveats**

- **`4e67dc2` is unverified.** No `git` tool was available in either research lane; a repo-wide grep for the literal SHA matched only benchmark artifacts, never source or `CHANGELOG.md`. The fallback pattern above is characterised from the working tree (`install.py:101`, `:2115`, `:2507`, `:2619`), not from the commit. Treat the commit↔code mapping as unresolved.
- **The fallback is not reusable.** All three copies are hand-duplicated inside `install.py`; grep for `conda` across `claude_hooks/` returns no psycopg-related resolver, so no consumer outside `install.py` can borrow one.
- **The trigger is per-site `except ImportError`, not an env check.** `find_conda_env_python` (`install.py:101-196`) resolves by filesystem path probing — hardcoded roots (`install.py:131-137`), then `conda env list --json` (`install.py:152-175`), then a canonical path from `env_name` (`install.py:193-196`). The only `CONDA_DEFAULT_ENV` inspection in the repo is display-only at `install.py:2831`. No `HAS_PSYCOPG` or `CONDA_PREFIX` gate exists anywhere.
- **Test coverage of the fallback is partial.** `tests/test_install_pgvector_verify.py:64` pins the conda-subprocess branch for `_verify_pgvector_dsn` and `:85` asserts the probe script is `argv[2]`; nothing asserts the equivalent fallback for `_pgvector_tables_present` or `_init_pgvector_schema` (neither lane grepped for such tests).
- Note the repo rule at `CONTRIBUTING.md:44` ("lazy-imported inside methods, gated by `try/except ImportError`") as written permits `claude_hooks/providers/pgvector.py:123`/`:328` and does not cover the two `scripts/` sites, which use bare imports inside functions with no gate.
- Provenance flag: `docs/benchmarks/EVALUATION.md:78-96` is a checked-in rubric listing these same six sites and the protected `install.py` lines, and `docs/benchmarks/kimi-k2.6-cloud-2026-05-07/audit-medium.transcript.md:20-27` is a prior answer to this identical question. The inventory above was independently grep-verified, but the overlap is worth knowing when weighing "ground truth" claims.
