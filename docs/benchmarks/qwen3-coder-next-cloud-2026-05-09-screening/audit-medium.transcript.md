# Consultation transcript — csl-2026-05-09-0650-8ab2

_Question_: Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python.


## Planner

1. Search for all `import psycopg` statements in the `claude-hooks` directory using `grep -r "import psycopg" claude-hooks/`.  
2. For each file found, examine the surrounding context to determine if it uses `psycopg.connect()` or `psycopg.connect_raw()` (line numbers from grep output).  
3. Check if any of those sites invoke `subprocess.run(["conda", "run", ...])` or reference `CONDA_PREFIX` (to compare against the fallback pattern introduced in 4e67dc2).  
4. Inspect `claude-hooks/lib/utils.py` for the `conda_env_subprocess()` helper added in 4e67dc2 to understand the exact fallback implementation for comparison.  
5. Review `claude-hooks/meta/hooks.py` for any hook initialization or execution logic that may route through conda-specific paths.  
6. Manually trace execution paths from each psycopg import site to determine if they are reachable from a bare `python -m claude-hooks.*` invocation (non-conda environment).  
7. Cross-reference each identified file with `setup.py` or `pyproject.toml` to confirm whether `psycopg` is declared conditionally (e.g., extras_require).

---

## Researcher (round 1)

(researcher lane produced no findings within the iteration budget; gaps remain — see plan)

---

## Researcher (round 1)

The plan mentioned `claude-hooks/lib/utils.py` and `claude-hooks/meta/hooks.py`, but these do not exist — the actual structure has `claude_hooks/hooks/` and no `lib/` or `meta/` top-level directories.

Let me search for `conda_env_subprocess`, `psycopg`, and `conda` references across the project.

---

## Researcher (round 1)

- **psycopg import sites in `claude_hooks/providers/pgvector.py`:**  
  - Line 123: `import psycopg` inside `verify()` method, which is reachable when `PgvectorProvider.verify()` is invoked directly (e.g., via `claude-hooks daemon` or CLI commands invoking provider verification) — **exercisable from non-conda environments** if `psycopg` is installed.  
  - Line 328: `import psycopg` inside `_ensure_ready()` method (inside a try/except ImportError block) — **exercisable from non-conda environments**, as this is the core initialization path for any pgvector operation (`recall`, `store`, `batch_recall`, etc.).  

- **No other psycopg import sites detected** in `claude_hooks/` directory per grep search — only these two locations in `claude_hooks/providers/pgvector.py`.  

- **Conda-env subprocess fallback (4e67dc2)** is not referenced or implemented at either site — both imports are direct, bare `import psycopg` without fallback to subprocess invocation via conda environment activation.  
  - Specifically, neither line 123 nor line 328 wraps the import in a `subprocess`/`shutil.which("conda")` fallback block (as added in commit 4e67dc2 elsewhere), confirming the audit target: **both remain vulnerable to failure when running under a bare `python -m claude_hooks.*` invocation without psycopg installed in the active environment**.

---

## Synthesizer

Both `claude_hooks/providers/pgvector.py:123` and `claude_hooks/providers/pgvector.py:328` import `psycopg` directly and lack the conda-env subprocess fallback introduced in `4e67dc2`.  

- **Line 123** (`verify()` method) is exercisable from a non-conda environment if `psycopg` is installed — no subprocess fallback is present.  
- **Line 328** (`_ensure_ready()` method) is the core initialization path for all pgvector operations and is also exercisable from a non-conda environment without the subprocess fallback.  

No other psycopg import sites exist in `claude_hooks/`.

---
