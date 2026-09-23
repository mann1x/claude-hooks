# Consultation transcript — csl-2026-09-23-1815-3041

_Question_: Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python.


## Planner

1. Locate the repository root and the `claude-hooks` directory containing the Python code to audit.
2. Run `git show 4e67dc2` (or equivalent) to view the exact diff, identifying the fallback mechanism added and which files/lines it modified.
3. Grep all `.py` files under `claude-hooks` for `psycopg` import statements (`import psycopg`, `from psycopg`, `import psycopg2`, etc.), recording file:line for each.
4. For each grep hit, compare the surrounding code against the fallback pattern from commit 4e67dc2 (try/except ImportError with a subprocess call to a conda-env Python, or similar), flagging sites that lack it.
5. For each flagged site, trace whether the import executes at module top-level or inside a function/conditional, and whether the enclosing code path can run under a non-conda-env Python (e.g., not guarded by an environment check or entrypoint that always uses conda env).
6. Check `claude-hooks` entrypoints, CLI scripts, and tests to confirm which import sites are actually reachable when Python is not running from the conda environment.
7. Produce the final audit list: each unpatched psycopg import site as `file:line` plus a boolean or short note on exercisability from a non-conda-env Python.

---

## Researcher (round 1)

## Findings — psycopg import sites vs. conda-env subprocess fallback

**Caveat:** I did not run `git show 4e67dc2` in this pass, so the exact diff content is unverified. The fallback pattern is inferable from `install.py` and `tests/test_install_pgvector_verify.py`, which document it.

- `claude_hooks/providers/pgvector.py:123` — `import psycopg` inside `PgvectorProvider.verify()`, wrapped in `try/except ImportError` (`pgvector.py:124-126`) that logs and returns `False`. **No subprocess fallback**; grep for `subprocess|conda|envs/|anaconda|miniconda` in `claude_hooks/providers/pgvector.py` matches only unrelated `_resolve_tables` identifiers (`pgvector.py:160,183,450`), no conda fallback. Exercisable from non-conda Python: yes — returns `False` gracefully rather than crashing. The only in-repo caller is `install.py:2132`, which itself has a fallback path (`install.py:2138-2149`), so the installer path is covered, but direct `verify()` calls from system Python are not.
- `claude_hooks/providers/pgvector.py:328` — `import psycopg` inside `PgvectorProvider._ensure_ready()`, except raises `ImportError("install psycopg: pip install psycopg[binary]")` at `pgvector.py:329-330`; then `psycopg.connect` at `pgvector.py:334`. **No subprocess fallback.** Exercisable from non-conda Python: yes — this is the real runtime hook path (`recall`/`store` → `_ensure_ready`). If `bin/claude-hook` falls back to system `python3` without psycopg, every pgvector recall/store raises ImportError. This site lacks the commit-4e67dc2 fallback.
- `scripts/migrate_to_pgvector.py:624` — bare `import psycopg` inside `main()` with **no** `try/except ImportError` and **no** subprocess/conda fallback; grep for `subprocess|conda|find_conda|envs/|anaconda|miniconda|ImportError` in that file returns no matches. `psycopg.connect` at `migrate_to_pgvector.py:626`. Exercisable from non-conda Python: yes — running the script under system Python without psycopg crashes with `ModuleNotFoundError`. This site lacks the fallback.
- `scripts/bench_recall.py:107` — bare `import psycopg` inside `PgvectorBenchProvider.__init__`, then `psycopg.connect` at `bench_recall.py:108`. **No** guard/fallback; grep only matches an unrelated comment at `bench_recall.py:72`. Exercisable from non-conda Python: conditionally yes — only when `PgvectorBenchProvider` is instantiated; then it crashes with `ModuleNotFoundError`. Lacks fallback.
- `install.py:2092` — `import psycopg` inside the embedded `_PGVECTOR_VERIFY_SCRIPT`, which is the *subprocess target* for the conda-env fallback at `install.py:2146-2149`. This is the fallback itself, not a missing site.
- `install.py:2127` — fast-path `import psycopg`; fallback to conda-env subprocess at `install.py:2138-2149` with `find_conda_env_python()` at `install.py:2140`. Covered.
- `install.py:2524` — fast-path `import psycopg` in `_pgvector_tables_present`; subprocess fallback at `install.py:2539-2560` (embedded script at `install.py:2543-2555`). Covered.
- `install.py:2647` — fast-path `import psycopg` in `_init_pgvector_schema`; subprocess fallback at `install.py:2656-2676` with embedded `import psycopg` at `install.py:2666`. Covered.
- `tests/test_pgvector_integration.py:56` and `tests/test_pgvector_integration.py:285` — `import psycopg` in test helpers. `test_pgvector_integration.py:54-58` shows `_skip_if_no_deps()` raises `unittest.SkipTest` when psycopg is missing, so these are not production exercisable paths. Not relevant to the runtime audit.
- No `from psycopg` or `import psycopg2` sites exist in the grep output — only `import psycopg` (psycopg 3). `claude_hooks/pgvector_mcp/server.py:37` imports `PgvectorProvider` but not psycopg directly; its psycopg exposure is transitive through `pgvector.py:328`.

**Bottom line:** the only files actually exercising psycopg from a non-conda-env Python and missing the fallback are `claude_hooks/providers/pgvector.py:328` (runtime hook path), `scripts/migrate_to_pgvector.py:624`, and `scripts/bench_recall.py:107`. `pgvector.py:123` lacks the fallback but degrades to `False` instead of crashing; `install.py` is fully covered at three call sites.

---

## Researcher (round 1)

## Findings — psycopg import sites vs. the `4e67dc2` conda-env subprocess fallback

The fallback pattern (`try: import psycopg` fast path → `find_conda_env_python()` subprocess on `ImportError`) exists **only in `install.py`**. Three functions have it; three embedded-script strings are the subprocess payloads themselves.

**Protected sites (not lacking — excluded from audit):**

- `install.py:2127` — fast-path `import psycopg` inside `_verify_pgvector_dsn()`; falls back at `install.py:2138`→`install.py:2140` (`find_conda_env_python()`) → `install.py:2146` subprocess.
- `install.py:2524` — fast-path inside `_pgvector_tables_present()`; fallback at `install.py:2539`→`install.py:2540`.
- `install.py:2647` — fast-path inside `_init_pgvector_schema()`; fallback at `install.py:2656`→`install.py:2657`.
- `install.py:2092`, `install.py:2546`, `install.py:2666` — these `import psycopg` lines live inside **subprocess inline-script string literals** (`_PGVECTOR_VERIFY_SCRIPT` at `install.py:2089` and the two `script = (...)` blocks), i.e. they run only under the conda env's python by design.

**Unpatched sites (still lack the fallback):**

- `claude_hooks/providers/pgvector.py:123` — `import psycopg` inside `PgvectorProvider.verify()` classmethod; `except ImportError` logs and returns `False` (`pgvector.py:125-126`). No conda fallback. Exercisable: **yes**, as a public classmethod reachable in-process — but non-crashy (graceful `False`), and the installer path that would call it is itself guarded by the outer `try/except ImportError` at `install.py:2126` (so under a psycopg-less Python the installer skips `verify()` entirely). Semantics lost: accurate conda-env reachability, not crash-safety.
- `claude_hooks/providers/pgvector.py:328` — `import psycopg` inside `_ensure_ready()`; `except ImportError` re-raises (`pgvector.py:329-330`). No conda fallback. Exercisable: **yes** — `_ensure_ready()` is the lazy connection gate (`if self._conn is None:` at `pgvector.py:326`), so any `recall()`/`store()`/`batch_*` call needing a connection reaches it. Module import itself is safe: no top-level psycopg import (`pgvector.py:45-61` show only stdlib + `embedders` + `base`). Reachable from non-conda Python when the daemon/hook shim falls back to system `python3` (`bin/_resolve_python.sh:53-58`) and `pgvector` is opt-in enabled. This one crashes (`ImportError`) rather than degrading.
- `scripts/migrate_to_pgvector.py:624` — bare `import psycopg` in `main()`, no guard, no fallback. Exercisable: **yes** — CLI entry at `scripts/migrate_to_pgvector.py:653` (`if __name__ == "__main__"`). Crashes with `ModuleNotFoundError` under a psycopg-less Python.
- `scripts/bench_recall.py:107` — bare `import psycopg` in `PgvectorBenchProvider.__init__`, no guard/fallback. Exercisable: **conditionally yes** — constructed only when the pgvector benchmark branch runs in `main()` at `scripts/bench_recall.py:239`; crashes if psycopg absent. No conda fallback.
- `tests/test_pgvector_integration.py:56` — `import psycopg` in `_skip_if_no_deps()`; `except ImportError` raises `SkipTest` (`:57-58`). No conda fallback. Exercisable from pytest: **yes**, but `SkipTest` is the correct test-skip behavior — not a production gap.
- `tests/test_pgvector_integration.py:285` — `import psycopg` in `tearDownClass`; broad `except Exception: pass` (`:293-294`). No conda fallback. Exercisable from pytest: **yes**, test-only, silently swallowed.

**Cross-check:** `claude_hooks/providers/__init__.py:33` registers `pgvector` lazily and the module has no import-time psycopg dependency, so importing the provider package does not itself exercise any of these sites. The MCP server `claude_hooks/pgvector_mcp/server.py:37` imports `PgvectorProvider` at module top, but that only loads `pgvector.py` (no psycopg import fires); the `:328` site becomes reachable when its tools run.

---

## Synthesizer

The unpatched psycopg import sites lacking the 4e67dc2 conda-env subprocess fallback are four production-reachable sites plus two test-only sites:

- `claude_hooks/providers/pgvector.py:123` — `import psycopg` in `PgvectorProvider.verify()`; catches `ImportError` and returns `False` (`pgvector.py:124-126`). Exercisable from a non-conda-env Python: yes, if `verify()` is called directly; it degrades instead of crashing, but does not use the conda-env fallback. The installer caller is guarded at `install.py:2126`, so the installer avoids this path when psycopg is absent.
- `claude_hooks/providers/pgvector.py:328` — `import psycopg` in `_ensure_ready()`; `except ImportError` re-raises (`pgvector.py:329-330`). Exercisable: yes — any `recall()`/`store()`/`batch_*` connection path reaches it, and a non-conda Python without psycopg crashes.
- `scripts/migrate_to_pgvector.py:624` — bare `import psycopg` in `main()`; no guard/fallback; CLI entry at `scripts/migrate_to_pgvector.py:653`. Exercisable: yes; crashes with `ModuleNotFoundError`.
- `scripts/bench_recall.py:107` — bare `import psycopg` in `PgvectorBenchProvider.__init__`; no guard/fallback; constructed in the benchmark branch at `scripts/bench_recall.py:239`. Exercisable: conditionally yes; crashes if psycopg is absent.
- `tests/test_pgvector_integration.py:56` — test helper `_skip_if_no_deps()`; `except ImportError` raises `SkipTest` (`:57-58`). Exercisable under pytest: yes; correct skip, not a production gap.
- `tests/test_pgvector_integration.py:285` — test `tearDownClass`; broad `except Exception: pass` (`:293-294`). Exercisable under pytest: yes; silently swallowed, not production.

Covered/excluded: `install.py:2127`, `install.py:2524`, and `install.py:2647` already have the fast-path → `find_conda_env_python()` subprocess fallback; `install.py:2092`, `install.py:2546`, and `install.py:2666` are inside the embedded subprocess scripts themselves. No `from psycopg` or `import psycopg2` sites were found.

---
