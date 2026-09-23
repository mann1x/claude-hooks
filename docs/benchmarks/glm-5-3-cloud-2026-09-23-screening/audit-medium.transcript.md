# Consultation transcript — csl-2026-09-23-1823-b6c2

_Question_: Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python.


## Planner

1. Read `install.py:2115-2170` (`_verify_pgvector_dsn`) and `install.py:2516-2570` plus `2632-2680` to pin down the exact 4e67dc2 pattern (in-process `try: import psycopg` → on ImportError shell out to `find_conda_env_python()` running an embedded probe script); classify `install.py:2127`, `2524`, `2647` as protected sites, and confirm `install.py:2092` sits inside the `_PGVECTOR_VERIFY_SCRIPT` heredoc payload (executed in the conda env by design — not a missing-fallback site).
2. Verify each candidate unprotected site in context: `claude_hooks/providers/pgvector.py:118-135` (`verify()`, import at :123) and `:320-340` (`_ensure_ready()`, import at :328) — confirm the `except ImportError` handlers return `False` / re-raise with no subprocess attempt; `scripts/migrate_to_pgvector.py:615-635` (bare import at :624); `scripts/bench_recall.py:95-115` (import at :107 inside `PgvectorBenchProvider.__init__`); `tests/test_pgvector_integration.py:50-70` (`_skip_if_no_deps`, :56) and `:280-295` (`tearDownClass`, :285).
3. Sweep for sites the initial `psycopg` grep could miss and for any fallback wrapper outside install.py: grep `psycopg2|from psycopg|import psycopg` in `claude_hooks/`, `scripts/`, `consultants/`, `episodic_server/`; grep `subprocess.*psycopg|find_conda_env` outside `install.py` to prove no other fallback exists; also confirm `claude_hooks/providers/__init__.py:33` and `claude_hooks/pgvector_mcp/server.py:37` reference the provider (import chain, not import sites themselves).
4. Trace exercisability of `pgvector.py:123/:328` from a non-conda interpreter: `PgvectorProvider` is imported by `claude_hooks/pgvector_mcp/server.py:37`, launched via `bin/claude-hook-pgvector-mcp.cmd:43-46` and `bin/_resolve_python.sh:54-57`, which fall back to system `python3`/`python` on PATH — establish whether the verify/`_ensure_ready` paths can run under a Python lacking psycopg in production (also `systemd/claude-hooks-pgvector-mcp.service:16`).
5. Trace exercisability of the script/test sites: check how `scripts/migrate_to_pgvector.py` and `scripts/bench_recall.py` are invoked (grep the `Makefile`, `docs/pgvector-runbook.md`, `install.py` for those script names) — do callers pin a conda python or allow bare `python`?; same for `tests/test_pgvector_integration.py` — check `tests/conftest.py` and docs for whether pytest is assumed to run inside the conda env.
6. Cross-check the independently-derived list against the ground truth in `docs/benchmarks/EVALUATION.md:78` (Q2, "psycopg ground truth") and `docs/benchmarks/EVALUATION.md:280` (line-drift warning), reporting any file:line that has shifted; note this repo also contains prior answer attempts under `docs/benchmarks/kimi-k2.6-cloud-2026-05-07*/` — use only as cross-reference, verify each claim from source.

---

## Researcher (round 1)

Findings verified with direct reads. (Note: grep also matched `.claude-hooks/.../transcript.db-wal` — stale consultant transcripts, ignored. No psycopg hits in `consultants/` or `episodic_server/`.)

## The 4e67dc2 pattern (protected sites, in `install.py`)

- `install.py:2127` — `import psycopg` fast path inside `_verify_pgvector_dsn`; `except ImportError: pass` at `install.py:2135-2136` → `find_conda_env_python()` at `install.py:2140` → `subprocess.run([str(conda_py), "-c", _PGVECTOR_VERIFY_SCRIPT, dsn], ...)` at `install.py:2146-2148`. **Protected.**
- `install.py:2524` — same shape (table-exists check): fast path at `install.py:2522-2537`, `find_conda_env_python()` at `install.py:2540`, embedded probe script built at `install.py:2543-2555`, subprocess at `install.py:2557-2559`. **Protected.**
- `install.py:2647` — schema-init DDL: fast path at `install.py:2646-2654`, `find_conda_env_python()` at `install.py:2657`, raises `RuntimeError` if env missing (`install.py:2658-2663`), subprocess at `install.py:2673-2676`. **Protected.**
- `install.py:2092` — `import psycopg` inside the `_PGVECTOR_VERIFY_SCRIPT` string literal (defined `install.py:2089-2112`), executed **in the conda env's child process** by design. Not a missing-fallback site. Similarly `install.py:2546` and `install.py:2666` are imports inside embedded child scripts.

## Unprotected sites lacking the fallback

- `claude_hooks/providers/pgvector.py:123` — `import psycopg` inside `PgvectorProvider.verify()`; `except ImportError` at `:124-126` logs a warning and `return False` — **no subprocess attempt**. Exercisable: yes — `verify()` is a classmethod called in-process by `install.py:2132` fast path and by detection/registry wiring (`claude_hooks/providers/__init__.py:33` maps `"pgvector"` → `PgvectorProvider`). From a non-conda-env Python without psycopg it silently degrades verification to False (misleading "FAILED", the exact bug 4e67dc2 fixed in install.py).
- `claude_hooks/providers/pgvector.py:328` — `import psycopg` inside `_ensure_ready()`; `except ImportError` at `:329-330` re-raises as `ImportError("install psycopg: pip install psycopg[binary]")` — **no subprocess attempt**. Exercisable: yes — reached from `recall()`/`store()` paths (line 315 is a `raise` just above, confirming this is the gate). `claude_hooks/pgvector_mcp/server.py:37` imports `PgvectorProvider` at module scope, so the system-wide MCP server inherits this gap.
- `scripts/migrate_to_pgvector.py:624` — bare `import psycopg` inside `main()` (function starts `:610`), no try/except at all. Exercisable: yes — any direct invocation crashes with `ModuleNotFoundError` from a non-conda-env Python; no fallback.
- `scripts/bench_recall.py:107` — bare `import psycopg` inside `PgvectorBenchProvider.__init__` (class at `:105`). Exercisable: conditionally — only when the pgvector benchmark provider is selected; crashes with `ModuleNotFoundError`, no fallback.
- `tests/test_pgvector_integration.py:56` — `import psycopg` inside `_skip_if_no_deps()` (function at `:54`); `except ImportError` at `:57-58` raises `unittest.SkipTest("psycopg not installed")`. Exercisable: yes, but behavior is a clean skip, not a crash — lowest severity; note `:62` and `:69` then use `psycopg.connect`/`psycopg.OperationalError` which would `NameError` if the first import failed, unreachable in practice.
- `tests/test_pgvector_integration.py:285` — `import psycopg` inside `tearDownClass()` (`:281-294`), wrapped in broad `except Exception: pass` (`:293-294`). Exercisable: yes (swallowed); lowest severity — cleanup silently skipped from non-conda-env Python.

## Proving no fallback exists outside install.py

- `find_conda_env_python` grep: definition at `install.py:101`, all call sites in `install.py` (plus test files patching it). **Zero** references in `claude_hooks/`, `scripts/`, `consultants/`, or `episodic_server/` — the fallback exists only in `install.py`.
- Full-project `psycopg2|from psycopg|import psycopg` grep: no psycopg imports in `consultants/` or `episodic_server/` at all; the complete list of real sites is the nine above. No module-level/top-of-file `import psycopg` anywhere in `claude_hooks/` — all are deferred inside methods/functions.
- `claude_hooks/providers/__init__.py:33` and `claude_hooks/pgvector_mcp/server.py:37` are import-chain references (registry entry / module import of `PgvectorProvider`), not import sites themselves.

Summary for synthesizer: 3 protected sites (`install.py:2127`, `:2524`, `:2647`); 2 runtime-exercisable gaps (`claude_hooks/providers/pgvector.py:123` verify-degrades-to-False, `:328` re-raises) with real user impact; 2 script gaps that crash (`scripts/migrate_to_pgvector.py:624`, `scripts/bench_recall.py:107`); 2 test-file sites (`tests/test_pgvector_integration.py:56` skip, `:285` swallowed) that are low-severity by design.

---

## Researcher (round 1)

## Findings — psycopg import sites lacking the 4e67dc2 conda-subprocess fallback

### Unprotected sites (all confirmed in current source; no line drift vs `docs/benchmarks/EVALUATION.md:85-90` ground truth)

- `claude_hooks/providers/pgvector.py:123` — `import psycopg` inside `verify()`; `except ImportError` at `pgvector.py:124-126` returns `False` with a warning, no subprocess fallback. **Exercisable**: provider verification runs wherever the shims resolve Python. The MCP path is real: `claude_hooks/pgvector_mcp/server.py:37` imports `PgvectorProvider`, and `tests/test_pgvector_integration.py:186` calls `verify()` directly.
- `claude_hooks/providers/pgvector.py:328` — `import psycopg` inside `_ensure_ready()` (`pgvector.py:320-334`); ImportError re-raised at `pgvector.py:330` with a pip-install hint. **Exercisable in production**: `claude_hooks/pgvector_mcp/server.py:271-280` dispatches `pgvector-find`/`pgvector-store` to `provider.recall/store`, which trigger `_ensure_ready`. Launched via `bin/claude-hook-pgvector-mcp:10-17` → `bin/_resolve_python.sh:53-58`, which falls back to bare system `python3`/`python` when no `.venv`/conda env exists; Windows `bin/claude-hook-pgvector-mcp.cmd:43-47` likewise falls back to bare `python` (and `:36-41` to `py -3`). So yes — a host without the conda env runs the MCP server under a Python that may lack psycopg.
- `systemd/claude-hooks-pgvector-mcp.service:16` — `ExecStart=.../bin/claude-hook-pgvector-mcp --http` confirms the production HTTP service uses the same resolver chain → system-python fallback applies.
- `scripts/migrate_to_pgvector.py:624` — bare `import psycopg` in `main()`, no guard. **Exercisable from any Python**: runbook invokes it with bare `python scripts/migrate_to_pgvector.py` (`docs/pgvector-runbook.md:50`, `:103`, `:106`, `:112`, `:489`) — no conda pin; crashes with `ModuleNotFoundError` under a psycopg-less interpreter.
- `scripts/bench_recall.py:107` — `import psycopg` inside `PgvectorBenchProvider.__init__` (`bench_recall.py:105-108`), no guard. **Conditionally exercisable**: only when the pgvector bench path is selected; runbook uses bare `python scripts/bench_recall.py` (`docs/pgvector-runbook.md:51`, `:531`). Note `bench_recall.py:43` imports from `scripts.migrate_to_pgvector`, but that import is module-level-psycopg-free (psycopg only at `migrate_to_pgvector.py:624` inside `main()`).
- `tests/test_pgvector_integration.py:56` — `_skip_if_no_deps()` imports psycopg in-process, raises `SkipTest` at `:58`. **Exercisable from pytest but non-crashing**: under a non-conda Python it degrades to a skip, not a `ModuleNotFoundError`.
- `tests/test_pgvector_integration.py:285` — `tearDownClass` imports psycopg inside `try/except Exception: pass` (`:284-294`). **Exercisable from pytest, fully swallowed** — no failure mode even without psycopg.

### Test-runner assumption

- `tests/conftest.py:33-54` — warns loudly when pytest is not the claude-hooks conda env and suggests `/root/anaconda3/envs/claude-hooks/bin/python -m pytest tests/`, but enforces nothing; the integration test sites above remain reachable from any interpreter.

### Protected sites (contrast, per EVALUATION.md:93-95)

- `install.py:2092/2098`, `:2127` (+ fallback at `:2138-2142`), `:2524`, `:2546`, `:2647`, `:2666` — these have the fast-path-then-conda-subprocess pattern (`install.py:2118-2125` documents it; `install.py:2660-2662` emits the "conda env not found" message). Tests at `tests/test_install_pgvector_verify.py:64-85` pin the fallback behavior.

### Exercisability nuance worth flagging to the synthesizer

- The shims **prefer** the conda env (`bin/_resolve_python.sh:35-44`) and `docs/pgvector-runbook.md:733-737` assumes psycopg lives there (`/root/anaconda3/envs/claude-hooks/bin/python -c "import psycopg"`). So the non-conda execution path materializes only when (a) no conda env / venv exists, or (b) an env exists but lacks psycopg — the resolver picks the interpreter by existence, not by package presence. The `verify()` site additionally fails *silently* (`pgvector.py:125-126` returns False), which is the misleading "FAILED (psycopg not installed)" failure mode the 4e67dc2 fallback fixed in `install.py` (per `tests/test_install_pgvector_verify.py:11`).

### Cross-reference

- Ground truth at `docs/benchmarks/EVALUATION.md:85-90` lists exactly the six sites above; every line number verified against current source — **zero drift**. Line-drift warning context at `docs/benchmarks/EVALUATION.md:277-282` (baseline tag `bench-baseline-2026-05-07`). Prior attempts in `docs/benchmarks/kimi-k2.6-cloud-2026-05-07/audit-medium.summary.md:16-21` and the `-pre-harden` variant agree with this list; one variant correctly noted (metadata.json:53) that no `4e67dc2` commit/fallback pattern exists inside `claude_hooks/` itself — the fallback lives only in `install.py`.

---

## Synthesizer

**Six import sites still lack the 4e67dc2 conda-env subprocess fallback; the three sites in `install.py` are the only protected ones.** The fallback (`find_conda_env_python` at `install.py:101`) has zero call sites outside `install.py`, so everything below degrades whenever it runs under a Python without psycopg.

**Exercisable in production from a non-conda Python (the real gaps):**

- `claude_hooks/providers/pgvector.py:123` — `import psycopg` in `verify()`; `except ImportError` at `claude_hooks/providers/pgvector.py:124-126` logs a warning and returns `False`, no subprocess attempt. **Yes, exercisable**: the MCP server imports the provider at `claude_hooks/pgvector_mcp/server.py:37`, and the launcher chain (`bin/_resolve_python.sh:53-58`, `bin/claude-hook-pgvector-mcp.cmd:43-47`) falls back to bare system `python3`/`python` when no conda env/venv exists — so a psycopg-less interpreter silently reports pgvector verification as "FAILED". This is exactly the failure mode 4e67dc2 fixed in `install.py` (`tests/test_install_pgvector_verify.py:11`).
- `claude_hooks/providers/pgvector.py:328` — `import psycopg` in `_ensure_ready()`; re-raised as `ImportError("install psycopg: pip install psycopg[binary]")` at `claude_hooks/providers/pgvector.py:329-330`. **Yes, exercisable**: `claude_hooks/pgvector_mcp/server.py:271-280` dispatches `pgvector-find`/`pgvector-store` to `recall`/`store`, both gated by `_ensure_ready`. The production systemd unit (`systemd/claude-hooks-pgvector-mcp.service:16`) uses the same resolver chain, so a host without psycopg sees every MCP request crash with ImportError.
- `scripts/migrate_to_pgvector.py:624` — bare `import psycopg` in `main()`, no guard at all. **Yes, unconditionally exercisable**: the runbook invokes it as bare `python scripts/migrate_to_pgvector.py` (`docs/pgvector-runbook.md:50`), no conda pin — `ModuleNotFoundError` under any psycopg-less interpreter.
- `scripts/bench_recall.py:107` — bare `import psycopg` inside `PgvectorBenchProvider.__init__` (`scripts/bench_recall.py:105-108`). **Conditionally exercisable**: only when the pgvector bench provider is selected; runbook also uses bare `python` (`docs/pgvector-runbook.md:51`). Crashes with `ModuleNotFoundError`, no fallback.

**Test-file sites — reachable but low-severity by design:**

- `tests/test_pgvector_integration.py:56` — `import psycopg` in `_skip_if_no_deps()`; raises `unittest.SkipTest` at `tests/test_pgvector_integration.py:58`. Exercisable from any interpreter, but degrades to a clean skip, not a crash. (`tests/conftest.py:33-54` only warns when pytest runs outside the conda env; nothing is enforced.)
- `tests/test_pgvector_integration.py:285` — `import psycopg` in `tearDownClass()`, wrapped in `except Exception: pass` at `tests/test_pgvector_integration.py:293-294`. Exercisable but fully swallowed — cleanup silently skipped; no failure mode.

**Protected (contrast, per the 4e67dc2 pattern):** `install.py:2127` (fallback at `install.py:2140-2148`), `install.py:2524` (at `install.py:2540-2559`), and `install.py:2647` (at `install.py:2657-2676`). `install.py:2092` is not a missing-fallback site — it's an import inside the `_PGVECTOR_VERIFY_SCRIPT` string payload, executed in the conda env's child process by design (as are `install.py:2546` and `install.py:2666`). `claude_hooks/providers/__init__.py:33` and `claude_hooks/pgvector_mcp/server.py:37` are registry/import-chain references, not import sites.

One nuance for remediation: the resolver shims *prefer* the conda env (`bin/_resolve_python.sh:35-44`), and the runbook assumes psycopg lives there (`docs/pgvector-runbook.md:733-737`). The non-conda path materializes only when (a) no env exists, or (b) an env exists but lacks psycopg — the resolver picks the interpreter by existence, not by package presence. Line numbers match the `docs/benchmarks/EVALUATION.md:85-90` ground truth with zero drift.

---
