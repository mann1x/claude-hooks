---
session_id: csl-2026-10-27-1126-a256
created: "2026-10-27T11:26:33"
question: "Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python."
models:
  planner: "glm-5.3-flash:cloud"
  researcher: "glm-5.3-flash:cloud"
  synthesizer: "glm-5.3-flash:cloud"
topology: council
effort: medium
duration_seconds: 63.83
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-10-27/tmp/claude-hooks-bench-bench-baseline-2026-05-07-g1tB
---

**Bottom line: four psycopg import sites lack the conda-env subprocess fallback, but only one is realistically exercisable from a non-conda-env Python in a way that matters — `claude_hooks/providers/pgvector.py:328` — and even there the failure degrades gracefully rather than crashing.** Commit `4e67dc2` itself could not be inspected (no git history available in this session; `CHANGELOG.md` has no mention of it), so "deliberately excluded vs. missed" is judged against the in-code reference pattern at `install.py:2115-2150`, whose docstring (`install.py:2118-2123`) gives the rationale: install.py runs under whatever `python` is on PATH, so its probes are pinned to the claude-hooks conda env.

**Sites lacking the fallback**

- `claude_hooks/providers/pgvector.py:123` — `PgvectorProvider.verify`: ImportError → warning + `return False` (`pgvector.py:124-126`), no fallback. **Exercisable: only marginally.** The sole production caller is `install.py:2132`, which sits inside the fallback-protected fast path of `_verify_pgvector_dsn` — the installer routes around it. Direct API consumers (e.g. tests at `tests/test_pgvector_integration.py:186`) get a silent `False`, not a crash. Low severity.
- `claude_hooks/providers/pgvector.py:328` — `_ensure_ready` connect path: ImportError → `raise ImportError("install psycopg: pip install psycopg[binary]")` (`pgvector.py:329-330`), no fallback. **Exercisable: yes, the sharpest gap.** `bin/_resolve_python.sh:53-58` falls through to system `python3`/`python` when neither repo `.venv` nor the conda env exists (`bin/_resolve_python.sh:31-44` lists only `.venv` and `claude-hooks` conda candidates), so hook-path invocations can land in a psycopg-less interpreter with `providers.pgvector.enabled=true` (providers are built regardless of interpreter via `claude_hooks/dispatcher.py:115` `build_providers`, used by `reflect.py:59`, `consolidate.py:58`, `store_async.py:136`, `pgvector_mcp/server.py:343`). Impact is bounded by design: `recall`/`search` catch the ImportError and return empty (`pgvector.py:148-151`, `265-267`); `store`/`batch_store` convert it to `RuntimeError` (`pgvector.py:216-217`, `293-294`) which the dispatcher swallows per the never-block contract. So a fallback here would improve recall quality, not prevent crashes.
- `scripts/migrate_to_pgvector.py:624` — bare `import psycopg` in the main migration flow, no try/except → **hard traceback** if missing. **Exercisable: yes** from any system Python, but acceptable-by-design: operator-run CLI (usage block at `:13-37`) and `docs/pgvector-runbook.md:733-737` explicitly instructs installing psycopg into the conda env first.
- `scripts/bench_recall.py:107` — `import psycopg` inside `PgvectorBenchProvider.__init__`, no try/except → **hard crash**. **Exercisable: yes** from system Python; same operator-tool class as migrate (docstring at `:1-3` positions it as a benchmark harness). Same acceptable-by-design caveat, though a friendly error would cost ~3 lines per site.

**Not gaps**

- `install.py:2092` (`_PGVECTOR_VERIFY_SCRIPT` probe) — this *is* the fallback mechanism; it only ever executes via the conda-env subprocess at `install.py:2146-2147`. The same fast-path/fallback shape covers the other install.py imports (`install.py:2127`, `2524`, `2647`, `2546`, `2666`).
- `claude_hooks/pgvector_mcp/` — zero psycopg references; it delegates to `PgvectorProvider` (`server.py:37`), so it inherits site #328's behavior. Exercisable from a non-conda interpreter via the pip console-script path (not the shim, which uses `_resolve_python.sh`); failures become MCP `isError` responses, never a crashed loop (`server.py:19-21`).

**Verified vs. unverified**: import sites, ImportError handling, the reference fallback pattern, and interpreter fall-through were verified by direct file reads this session. Unverified: the actual contents/diff of commit `4e67dc2` (no git tooling; CHANGELOG silent), and no test exercises the provider under a psycopg-less interpreter — `tests/test_pgvector_integration.py:54-58` skips the whole integration class when psycopg is missing, and `tests/test_install_pgvector_verify.py` asserts the fallback only for the three install.py functions, so omission vs. intent for the four sites above is not settled by the test suite.
