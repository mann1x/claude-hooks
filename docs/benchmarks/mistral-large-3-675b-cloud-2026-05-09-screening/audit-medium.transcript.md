# Consultation transcript — csl-2026-05-09-0634-76f0

_Question_: Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python.


## Planner

1. Search the entire codebase for `import psycopg` and `from psycopg import` patterns, recording every file:line occurrence.
2. Cross-reference each import site with the git diff of 4e67dc2 to confirm whether the fallback logic was applied at that location.
3. For each unpatched import site, inspect the surrounding function/class/module to determine if it is reachable when running outside a conda environment (e.g., no `sys.prefix` checks, no `CONDA_DEFAULT_ENV` guards).
4. Verify if the import is inside a try-except block that already handles `ImportError` or `ModuleNotFoundError` without the subprocess fallback.
5. Check if the import is in a test file, example script, or documentation that is not part of the main runtime code paths.
6. For each remaining unpatched import, trace the call graph (static or dynamic) to confirm if it is exercisable from entry points or public APIs.
7. Compile a list of file:line locations with a boolean flag indicating whether the import is exercisable outside a conda environment.

---

## Researcher (round 1)

---
- **`claude_hooks/providers/pgvector.py:123`** — `import psycopg` inside `PgvectorProvider.verify()`. Wrapped in `try/except ImportError`; **no conda-env subprocess fallback**. Exercisable from any Python process that calls `verify()`.
- **`claude_hooks/providers/pgvector.py:328`** — `import psycopg` inside `PgvectorProvider._ensure_ready()`. Wrapped in `try/except ImportError` but re-raises as `ImportError("install psycopg: ...")`; **no conda-env subprocess fallback**. Exercisable from any Python process that instantiates `PgvectorProvider`.
- **`scripts/migrate_to_pgvector.py:624`** — bare `import psycopg` inside `main()`. **No guard or conda-env subprocess fallback**; crashes with `ModuleNotFoundError` if psycopg is absent. Exercisable from any Python process that runs the script.
- **`scripts/bench_recall.py:107`** — `import psycopg` inside `PgvectorBenchProvider.__init__()`. **No guard or conda-env subprocess fallback**; crashes with `ModuleNotFoundError` if the benchmark code path is hit. Exercisable from any Python process that instantiates `PgvectorBenchProvider`.

---
- **`install.py:2092`**, **`install.py:2127`**, **`install.py:2524`** — all use the **conda-env subprocess fallback pattern** (e.g., `find_conda_env_python()` spawns a subprocess to import psycopg). **Not lacking**; exercisable only if the fallback is triggered.
- **No matches** for `find_conda_env_python|subprocess.*psycopg|_spawn.*psycopg|fallback.*psycopg` in `scripts/migrate_to_pgvector.py` — confirms absence of fallback logic.

---

## Researcher (round 1)

survey_project": {}}

---

## Researcher (round 1)

**Report: psycopg import sites in `claude_hooks/providers/pgvector.py` and exercisability**

- **`claude_hooks/providers/pgvector.py:123`**
  `import psycopg  # type: ignore`
  **Exercisable outside conda env?** ❌ No.
  - Called in `verify()` (`claude_hooks/providers/pgvector.py:121`), which is invoked by the installer (`install.py`) and by the daemon (`claude_hooks/daemon.py`) during provider setup.
  - Both paths assume the conda env is active; no subprocess fallback exists.
  - **Evidence**: No `_resolve_python` or `subprocess.run` logic in the file (`grep` confirmed no matches).

- **`claude_hooks/providers/pgvector.py:328`**
  `import psycopg  # type: ignore`
  **Exercisable outside conda env?** ❌ No.
  - Called in `_ensure_ready()` (`claude_hooks/providers/pgvector.py:320`), which is invoked by `recall()` (`claude_hooks/providers/pgvector.py:144`), `store()` (`claude_hooks/providers/pgvector.py:210`), and all batch methods.
  - These are the core provider methods wired into the hook dispatcher (`claude_hooks/dispatcher.py`), so they run on every `UserPromptSubmit`/`Stop` event.
  - **Evidence**: No conda-env subprocess fallback; the import is unconditional and fatal if missing (`raise ImportError` at `claude_hooks/providers/pgvector.py:330`).

- **Summary**: Both import sites are **not exercisable** from a non-conda-env Python. The provider is effectively conda-env-only.

---

## Synthesizer

- `claude_hooks/providers/pgvector.py:123` – ❌ Not exercisable outside conda env (import in `verify()` called only during conda-env setup, no subprocess fallback).
- `claude_hooks/providers/pgvector.py:328` – ❌ Not exercisable outside conda env (import in `_ensure_ready()` called by core provider methods, no subprocess fallback).
- `scripts/migrate_to_pgvector.py:624` – ✅ Exercisable (bare import in `main()`, no guards or fallback).
- `scripts/bench_recall.py:107` – ✅ Exercisable (bare import in `PgvectorBenchProvider.__init__()`, no guards or fallback).

---
