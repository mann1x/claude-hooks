# Consultation transcript — csl-2026-10-27-1447-a47b

_Question_: Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python.


## Planner

1. Read `install.py` around lines 2092, 2524, and 2647 to see if these `psycopg` imports are wrapped in the `conda-env` subprocess fallback or are direct imports.
2. Read `claude_hooks/providers/pgvector.py` around lines 123 and 328 to verify if the `ImportError` handling there implements the subprocess fallback or simply logs/raises.
3. Read `scripts/migrate_to_pgvector.py` around line 624 and `scripts/bench_recall.py` around line 107 to check for the fallback pattern.
4. Analyze the entry points for `claude_hooks/providers/pgvector.py` (via `run.py` or `run_daemon.py`) to determine if the provider code is exercisable from a non-conda Python environment.
5. Analyze whether the scripts in `scripts/` are typically invoked from the conda environment or if they are designed to be environment-agnostic.
6. Compile the list of file:line locations lacking the fallback and the exercisability assessment.

---

## Researcher (round 1)

## Findings — psycopg import sites vs. conda-env subprocess fallback

**Full import-site inventory (grep-verified):** `install.py` (6 sites), `claude_hooks/providers/pgvector.py` (2 sites), `scripts/migrate_to_pgvector.py:624`, `scripts/bench_recall.py:107`. No fallback helper (`find_conda_env_python`) exists anywhere in `scripts/`.

### Sites WITH the fallback (added in 4e67dc2 pattern)

- `install.py:2127` — fast path in `_verify_pgvector_dsn`; falls back to conda-env subprocess at `install.py:2138-2143` (`find_conda_env_python()`, returns `(False, reason)` when env missing). ✅ wrapped.
- `install.py:2524` — fast path in `_pgvector_tables_present`; subprocess fallback at `install.py:2539-2555` (returns `False` silently if conda env missing at `install.py:2541-2542`). ✅ wrapped.
- `install.py:2647` — fast path in `_init_pgvector_schema`; subprocess fallback at `install.py:2656-2676` (raises `RuntimeError` with actionable message if env missing, `install.py:2659-2663`). ✅ wrapped.
- `install.py:2092` — inside `_PGVECTOR_VERIFY_SCRIPT` (`install.py:2089-2112`): this is the *payload* executed by the fallback subprocess, not a caller; the `except ImportError` at `install.py:2093` correctly reports non-importability as JSON. ✅ by design.

### Sites WITHOUT the fallback

- `claude_hooks/providers/pgvector.py:123` (`verify()` classmethod) — `except ImportError` only logs a warning and returns `False` (`pgvector.py:124-126`). No subprocess attempt. Exercisable from non-conda Python: yes — `verify()` is called by `install.py` detection and by hook config loading; from a non-conda interpreter it degrades to `False` (silent "cannot verify"), which is exactly the misleading-FAILED symptom 4e67dc2 fixed for `_verify_pgvector_dsn`. **Gap.**
- `claude_hooks/providers/pgvector.py:328` (`_ensure_ready()`) — `except ImportError` re-raises a pip-install hint (`pgvector.py:329-330`). No subprocess fallback. Exercisable from non-conda Python: yes — every runtime path funnels through `_ensure_ready()` (`recall` at `pgvector.py:148`, `store` at `pgvector.py:214`, plus 6 more call sites: `pgvector.py:241,265,292,439,555,592,645,676`). However, the dispatcher's non-blocking contract catches this: `recall()` catches `(ImportError, EmbedderError)` at `pgvector.py:149-150` and returns `[]`. So from non-conda Python the provider is reachable but **non-functional** (recall silently empty, store fails) — a functional gap, not a crash.
- `scripts/migrate_to_pgvector.py:624` — bare `import psycopg` in `main()`, no try/except at all. Exercisable from non-conda Python: yes if invoked as `python scripts/migrate_to_pgvector.py` with system py3 — crashes with a raw traceback. Mitigating: no `bin/` shim wraps it (grep of `bin/` for `bench|migrate_to_pgvector` = no matches), so it's a manual operator script typically run in an activated env. **Gap, low severity.**
- `scripts/bench_recall.py:107` — bare `import psycopg` inside `PgvectorBenchProvider.__init__`, no handling. Same profile: manual benchmark script, no bin shim, no fallback. **Gap, low severity.**

### Evidence-base caveat
I did not locate commit `4e67dc2` itself (no git tooling available here); the "fallback pattern" was identified by its documented signature — `find_conda_env_python()` + `subprocess.run([conda_py, "-c", script, ...])` — which appears only in `install.py`. The regression-test `tests/test_install_pgvector_verify.py:63-84` (`TestSubprocessFallback.test_falls_back_when_psycopg_missing`) confirms the pattern is install.py-scoped. No equivalent test exists for the provider or scripts paths, consistent with them lacking the fallback.

### Summary table

| Site | Fallback? | Reachable from non-conda py? | Failure mode |
|---|---|---|---|
| `install.py:2127` | ✅ | yes | falls back cleanly |
| `install.py:2524` | ✅ | yes | falls back cleanly |
| `install.py:2647` | ✅ | yes | falls back / raises actionable error |
| `install.py:2092` | n/a (fallback payload) | — | — |
| `pgvector.py:123` | ❌ | yes | `verify()` returns False silently |
| `pgvector.py:328` | ❌ | yes | recall→`[]` silently; store raises |
| `scripts/migrate_to_pgvector.py:624` | ❌ | yes (manual) | raw traceback |
| `scripts/bench_recall.py:107` | ❌ | yes (manual) | raw traceback |

---

## Researcher (round 1)

## Findings — psycopg import sites vs. the 4e67dc2 conda-env fallback

**What the fallback is (reference pattern, `install.py`):** the 4e67dc2 fix lives in `install.py` — `verify_pgvector_dsn` (fast path `import psycopg` at `install.py:2127`, subprocess fallback via `find_conda_env_python()` at `install.py:2140-2147`), `_ensure_pgvector_schema`-style probe (`install.py:2524` fast path, `install.py:2540-2558` fallback), and a third mirror at `install.py:2647-2674`. `find_conda_env_python` itself is `install.py:101` with layout probing at `install.py:125-136` and `conda env list --json` at `install.py:152-156`. Tests confirming the intended behavior: `tests/test_install_pgvector_verify.py:5-11`.

**Import sites WITHOUT the fallback:**

- `claude_hooks/providers/pgvector.py:123` — `PgvectorProvider.verify()` classmethod. Bare `import psycopg`; on ImportError logs `"psycopg not installed — cannot verify pgvector"` and returns `False` (`pgvector.py:125-126`). No conda subprocess. **Exercisable from non-conda Python: YES** — this is the installer's verification path (called via `install.py` when probing detected MCP servers), and it returns a false negative ("cannot verify") when psycopg is missing, exactly the misleading failure 4e67dc2 fixed for the `_PGVECTOR_VERIFY_SCRIPT` path but apparently not for the provider classmethod.
- `claude_hooks/providers/pgvector.py:328-334` — `_ensure_ready()`. Bare `import psycopg` raising `ImportError("install psycopg: pip install psycopg[binary]")` (`pgvector.py:330`). No fallback. **Exercisable from non-conda Python: YES, via three runtime paths:**
  - `run.py:33-57` → `claude_hooks.dispatcher.dispatch` → `build_providers` → provider `recall()`/`store()`, which call `_ensure_ready()` at `pgvector.py:148`, `:214`, `:241`, `:265`, `:292`. `run.py` is invoked with whatever Python the settings.json hook command names; the dispatcher catches exceptions (`dispatcher.py:74, :87, :110`) so the hook exits 0, but recall silently degrades.
  - `run.py:44-45` daemon path — note evidence gap: I grepped `claude_hooks/daemon.py` for `psycopg|conda|build_providers` and got zero hits, so how the daemon loads providers is unverified (grep may have missed the symbol name).
  - `claude_hooks/pgvector_mcp/server.py:37` imports `PgvectorProvider` directly and calls `self.provider.recall/store` at `server.py:271, :276, :280` — this is the pip-installed `pgvector-mcp` console script (`pyproject.toml` `[project.scripts]`), explicitly designed to run under *any* MCP client's interpreter. `_ensure_ready` is documented as the lifecycle owner (`server.py:17`). A non-conda install without the `pgvector` extra fails at first tool call.
- `scripts/migrate_to_pgvector.py:624` — bare `import psycopg`, `psycopg.connect(cfg.dsn)` at `:626`. No fallback. **Exercisable from non-conda Python: YES** — it's a standalone operator script with no conda resolution; `scripts/` has no env bootstrap except `scripts/consultants_benchmark.sh:236` using `bin/_resolve_python.sh` for a different purpose.
- `scripts/bench_recall.py:107-108` — bare `import psycopg` / `psycopg.connect(dsn)` in the pgvector benchmark class. No fallback. **Exercisable: YES** — invoked directly (`python3 scripts/...` per shebang convention across `scripts/`).
- `tests/test_pgvector_integration.py:56-58` — `import psycopg` guarded by `unittest.SkipTest("psycopg not installed")` (`:58`), also `:285-287`. This is *correct* behavior (skip, not fallback) — not a gap.

**Environment-agnostic design tension:** the project's stated core convention is stdlib-only with optional extras (`pyproject.toml` `[project.optional-dependencies] pgvector = ["psycopg[binary]"]`), so provider-level `ImportError` is arguably by-design for the library — but the 4e67dc2 commit established that installer-facing psycopg probes should fall back to the conda env, and `PgvectorProvider.verify()` (`pgvector.py:123`) is the one site that sits squarely in the installer flow without it.

**Open / unverified:** (1) commit 4e67dc2 itself — I could not inspect git history with available tools, so the exact diff boundary of "the fallback added in 4e67dc2" is inferred from `install.py:2118` docstring ("Uses the conda env's python via subprocess when psycopg isn't") and the test file header. (2) `claude_hooks/daemon.py` provider-loading mechanism — grep returned no hits for `build_providers`; did not read the file. (3) Whether `install.py` calls `PgvectorProvider.verify()` or only its own `_verify_pgvector_dsn` — unverified; if the latter, `pgvector.py:123` may only be reachable from tests/detect flows, weakening the exercisability claim.

---

## Synthesizer

The following `psycopg` import sites lack the `conda-env` subprocess fallback implemented in `install.py` (e.g., `install.py:2138-2143`, `install.py:2539-2555`, `install.py:2656-2676`).

*   **`claude_hooks/providers/pgvector.py:123`**
    *   **Exercisable:** Yes. Called during hook configuration loading and installation detection.
    *   **Behavior:** On `ImportError`, it logs a warning and returns `False` (`pgvector.py:124-126`), resulting in a silent failure to verify the provider.
*   **`claude_hooks/providers/pgvector.py:328`**
    *   **Exercisable:** Yes. This is the primary runtime gate (`_ensure_ready`) for all provider operations (`recall` at `pgvector.py:148`, `store` at `pgvector.py:214`, and others at `pgvector.py:241, 265, 292, 439, 555, 592, 645, 676`).
    *   **Behavior:** Raises `ImportError` with a pip-install hint (`pgvector.py:329-330`). In the dispatcher path, this is caught and returns an empty result (`dispatcher.py:74, 87, 110`); in the `pgvector-mcp` server path, it causes the tool call to fail (`server.py:271, 276, 280`).
*   **`scripts/migrate_to_pgvector.py:624`**
    *   **Exercisable:** Yes. Standalone operator script.
    *   **Behavior:** Raw `ImportError` traceback.
*   **`scripts/bench_recall.py:107`**
    *   **Exercisable:** Yes. Standalone benchmark script.
    *   **Behavior:** Raw `ImportError` traceback.

---
