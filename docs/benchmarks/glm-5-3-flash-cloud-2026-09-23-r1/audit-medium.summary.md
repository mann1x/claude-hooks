---
session_id: csl-2026-09-23-2017-dbb5
created: "2026-09-23T20:17:11"
question: "Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python."
models:
  planner: "glm-5.3-flash:cloud"
  researcher: "glm-5.3-flash:cloud"
  synthesizer: "glm-5.3-flash:cloud"
topology: council
effort: medium
duration_seconds: 63.73
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-09-23/tmp/claude-hooks-bench-bench-baseline-2026-05-07-l2Px
---

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
