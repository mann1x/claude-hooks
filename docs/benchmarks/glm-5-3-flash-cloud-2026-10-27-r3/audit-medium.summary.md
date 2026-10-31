---
session_id: csl-2026-10-27-1220-c129
created: "2026-10-27T12:20:40"
question: "Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python."
models:
  planner: "glm-5.3-flash:cloud"
  researcher: "glm-5.3-flash:cloud"
  synthesizer: "glm-5.3-flash:cloud"
topology: council
effort: medium
duration_seconds: 77.45
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-10-27/tmp/claude-hooks-bench-bench-baseline-2026-05-07-WSBV
---

**Bottom line: the 4e67dc2 fallback pattern is present and correct at all four `install.py` in-process import sites; the four sites still lacking it are the two provider methods (`claude_hooks/providers/pgvector.py:123`, `:328`) and the two standalone scripts (`scripts/migrate_to_pgvector.py:624`, `scripts/bench_recall.py:107`). Only the provider sites are on a production code path.**

**Compliant — fallback present (all verified by direct read):**
- `install.py:2127` (`_verify_pgvector_dsn` fast path) → `except ImportError: pass` at `install.py:2135-2136`, then `find_conda_env_python()` + `subprocess.run([conda_py, "-c", _PGVECTOR_VERIFY_SCRIPT, dsn])` at `install.py:2140-2148`. ✓
- `install.py:2524` (`_pgvector_tables_present`) → fallback at `install.py:2539-2548` (inline script with its own `import psycopg` at `install.py:2546`). ✓
- `install.py:2647` (`_init_pgvector_schema`) → fallback at `install.py:2656-2668`, DDL piped via stdin. ✓
- `install.py:2092` — not an in-process import; it's inside the `_PGVECTOR_VERIFY_SCRIPT` string, executed *by* the conda subprocess at `install.py:2147`. Reachability from the calling interpreter is moot. ✓

**Gaps — no fallback:**
- `claude_hooks/providers/pgvector.py:123` (`PgvectorProvider.verify`): `except ImportError` → `log.warning` + `return False` (`pgvector.py:124-126`). Exercisable from non-conda Python: **yes, but effectively guarded** — its only in-repo caller is `install.py:2132`, which sits inside the fast path and is only reached after `import psycopg` at `install.py:2127` succeeded, so a missing psycopg there falls through to the subprocess fallback instead. The generic verify loop never reaches pgvector (`install.py:3547-3548` skips it), so `install.py:3556` does not exercise this site. Residual exposure: any *external* caller of the public classmethod under a system python gets a misleading `False` ("unreachable") — same bug class 4e67dc2 fixed, but currently unreachable in-repo. Severity: low.
- `claude_hooks/providers/pgvector.py:328-330` (`_ensure_ready`): re-raises as `ImportError("install psycopg: pip install psycopg[binary]")`. No conda fallback. **Exercisable from non-conda Python: yes** — `bin/_resolve_python.sh:53-58` falls through to system `python3`/`python` when neither `.venv` nor the conda env exists, and that interpreter runs the hook runtime (`run.py`). Mitigations: pgvector defaults to disabled (`claude_hooks/config.py:48-49`, per researcher), and callers catch the error — `recall()` → warn + `[]` (`pgvector.py:149-151`), `store()` → `RuntimeError` (`pgvector.py:216-217`) — so behavior is graceful degradation, not a crash. This is the highest-value gap: it's the only one on the live hook path, and a conda-subprobe (reusing `find_conda_env_python()`, which lives in `install.py`) would restore recall under a system python.
- `scripts/migrate_to_pgvector.py:624`: bare `import psycopg` in `main()`, no try/except. Exercisable: **yes** — standalone CLI (`scripts/migrate_to_pgvector.py:653-654`), no `bin/` shim routes to it. Raw ImportError traceback under system python. Operator tooling; arguably acceptable.
- `scripts/bench_recall.py:107`: bare `import psycopg` in `PgvectorBenchProvider.__init__` (`bench_recall.py:105-108`). Exercisable: **yes** — standalone CLI (`bench_recall.py:322-323`), no shim. Same loud-failure profile; dev/bench tooling.

**Out of scope (not exercisable from production):**
- `tests/test_pgvector_integration.py:56` and `:285` — guarded by `_skip_if_no_deps()` raising `unittest.SkipTest("psycopg not installed")` (`test_pgvector_integration.py:57-58`); the `:285` teardown site is only reached after the skip gate passed.

**Unverified:**
- That commit `4e67dc2` is the origin of the `install.py:2127/2140-2148` pattern — no git tool available; the pattern itself is verified in the working tree, the attribution is not.
- "No other callers of `PgvectorProvider.verify` inside `claude_hooks/`" — researcher grep, not independently confirmed (harmless: the finding doesn't depend on it).
