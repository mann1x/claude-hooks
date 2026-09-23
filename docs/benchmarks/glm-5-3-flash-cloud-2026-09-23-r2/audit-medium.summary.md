---
session_id: csl-2026-09-23-2029-e1eb
created: "2026-09-23T20:29:31"
question: "Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python."
models:
  planner: "glm-5.3-flash:cloud"
  researcher: "glm-5.3-flash:cloud"
  synthesizer: "glm-5.3-flash:cloud"
topology: council
effort: medium
duration_seconds: 68.08
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-09-23/tmp/claude-hooks-bench-bench-baseline-2026-05-07-xbWO
---

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
