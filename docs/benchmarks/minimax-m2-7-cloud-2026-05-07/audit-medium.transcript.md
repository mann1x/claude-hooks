# Consultation transcript — csl-2026-05-07-1006-bbd1

_Question_: Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python.


## Planner

1. `git show 4e67dc2 --stat` — identify which file(s) received the fallback addition
2. `git show 4e67dc2` — extract the exact diff to understand the fallback pattern (subprocess check for CONDA_DEFAULT_ENV env var)
3. `grep -rn "import psycopg\|from psycopg" --include="*.py" .` — list every psycopg import site with file:line numbers
4. For each hit, read the surrounding ~15-line context and check whether it wraps the import in the conda-env fallback block
5. Cross-reference each import site against the fallback pattern: mark "has fallback" if the import is inside a try/except that falls back to subprocess, or "missing fallback" otherwise
6. For each missing site, determine if the code path is exercisable from a non-conda-env Python (check if the import is guarded by a `if os.getenv("CONDA_DEFAULT_ENV")` or similar conditional)

---

## Researcher (round 1)

<minimax:tool_call>
<invoke name="read_file">
<parameter name="end_line">80</parameter>
<parameter name="path">bin/_resolve_python.sh</parameter>
<parameter name="start_line">1</parameter>
</invoke>
<invoke name="read_file">
<parameter name="end_line">50</parameter>
<parameter name="path">claude_hooks/providers/pgvector.py</parameter>
<parameter name="start_line">1</parameter>
</invoke>
</minimax:tool_call>

---

## Researcher (round 1)

Now let me verify there are no other import sites in `embedders.py` and confirm the exact context of `bench_recall.py:107`:

- `scripts/bench_recall.py:107` — bare `import psycopg` inside `PgvectorBenchProvider.__init__`, no conda fallback, exercisable from any Python that imports this class. `scripts/migrate_to_pgvector.py:624` — bare `import psycopg` inside `main()`, no try/except, no fallback. **Both crash** with `ModuleNotFoundError` when invoked from a non-conda-env Python.

The three sites in `install.py` that **do** have the conda-env subprocess fallback (4e67dc2 pattern) are `install.py:2127`, `install.py:2524`, `install.py:2647`. Let me verify all three are wrapped:

The pattern confirmed at all three `install.py` sites is: fast-path `try: import psycopg` → on `ImportError` → call `find_conda_env_python()` → spawn subprocess with the script inline as `-c` argument. Both `_verify_pgvector_dsn` (line 2145-2155), `_pgvector_tables_present` (line 2539-2569), and `_init_pgvector_schema` (line 2656-onward) implement it fully.

---

## Findings

- **`claude_hooks/providers/pgvector.py:123`** — `PgvectorProvider.verify()`: bare `import psycopg` in `try/except ImportError` → returns `False`. **No conda fallback.** Exercisable from any Python process that calls `verify()` when psycopg isn't installed in the current interpreter.

- **`claude_hooks/providers/pgvector.py:328`** — `PgvectorProvider._ensure_ready()`: bare `import psycopg` in `try/except ImportError` → re-raises as `ImportError("install psycopg: ...")` without attempting conda-env subprocess. **No fallback.** Exercisable from any Python that instantiates `PgvectorProvider` outside the conda env.

- **`tests/test_pgvector_integration.py:56`** — `_skip_if_no_deps()`: `try/import psycopg` → raises `unittest.SkipTest`. Test-only guard; appropriate behavior for pytest.

- **`tests/test_pgvector_integration.py:285`** — `tearDownClass()`: `import psycopg` inside broad `except Exception: pass`. Test teardown; silent no-op acceptable.

- **`scripts/migrate_to_pgvector.py:624`** — inside `main()`: bare `import psycopg` with **no try/except, no fallback**. **Exercisable from any Python.** Will crash with `ModuleNotFoundError` if run outside conda env.

- **`scripts/bench_recall.py:107`** — inside `PgvectorBenchProvider.__init__()`: bare `import psycopg` with **no try/except, no fallback**. **Exercisable from any Python.** Will crash on class instantiation when psycopg is absent.

- **`install.py:2127`** — `_verify_pgvector_dsn()`: `try/import psycopg` → on `ImportError` shells out to `find_conda_env_python()` subprocess. **Has fallback** (4e67dc2 pattern).

- **`install.py:2524`** — `_pgvector_tables_present()`: `try/import psycopg` → on `ImportError` falls through to `find_conda_env_python()` subprocess. **Has fallback** (4e67dc2 pattern).

- **`install.py:2647`** — `_init_pgvector_schema()`: `try/import psycopg` → on `ImportError` falls through to `find_conda_env_python()` subprocess. **Has fallback** (4e67dc2 pattern).

**Summary:** 6 files, 8 import sites total. The three `install.py` sites have the conda fallback. The two `claude_hooks/providers/pgvector.py` sites and the two scripts are missing it. The two test-file sites are appropriate as-is.

---

## Researcher (round 1)

Now I have a complete picture. The pattern in `install.py` (fast-path `import psycopg` + conda-env subprocess fallback via `find_conda_env_python()`) appears **only** in `install.py` — never in `claude_hooks/`, `tests/`, or `scripts/`. Here is the audit:

---

**Lacking fallback — exercisable from non-conda-env Python:**

- `claude_hooks/providers/pgvector.py:123` — bare `import psycopg` inside `PgvectorProvider.verify()`; `except ImportError` returns `False` without spawning anything. Callable from any Python that invokes `PgvectorProvider.verify()`.
- `claude_hooks/providers/pgvector.py:328` — bare `import psycopg` inside `_ensure_ready()`; `except ImportError` re-raises as `"install psycopg: ..."`. Callable from any Python that calls `recall()`, `store()`, `batch_recall()`, `batch_store()`, or any KG op on a `PgvectorProvider` instance.
- `tests/test_pgvector_integration.py:56` — bare `import psycopg` inside `_skip_if_no_deps()`; `except ImportError` raises `SkipTest`. Exercisable from `pytest` (or any unittest runner) regardless of which Python invokes it.
- `tests/test_pgvector_integration.py:285` — bare `import psycopg` inside `tearDownClass`; broad `except Exception: pass` swallows the failure silently. Exercisable from `pytest`.
- `scripts/migrate_to_pgvector.py:624` — bare `import psycopg` at module scope inside `main()` with no `try/except`, no conda fallback. Will crash with `ModuleNotFoundError` when invoked from a Python without psycopg on `PYTHONPATH`.
- `scripts/bench_recall.py:107` — bare `import psycopg` inside `PgvectorBenchProvider.__init__()`; no guard. Will crash with `ModuleNotFoundError` when the benchmark script is run from a Python without psycopg.

**Already has fallback (reference pattern from `install.py:2127`, `install.py:2524`, `install.py:2647`):**

- `install.py:2127` — `try: import psycopg` fast path; `except ImportError` → `find_conda_env_python()` + `subprocess.run([conda_py, "-c", script, ...])`.
- `install.py:2524` — same pattern in `_pgvector_schema_initialized()`.
- `install.py:2647` — same pattern in `_init_pgvector_schema()`.

**No conda-env fallback exists anywhere in `claude_hooks/`, `tests/`, `scripts/bench_recall.py`, or `scripts/migrate_to_pgvector.py`** — the pattern is confined to `install.py` only.

---

## Synthesizer

## psycopg Import Sites Lacking conda-env Subprocess Fallback

**Reference pattern** (4e67dc2): `try: import psycopg` fast path → on `ImportError` → `find_conda_env_python()` + `subprocess.run([conda_py, "-c", script, ...])`. Present only in `install.py:2127`, `install.py:2524`, `install.py:2647`.

---

**Missing fallback — exercisable from non-conda-env Python:**

- `claude_hooks/providers/pgvector.py:123` — bare `import psycopg` in `PgvectorProvider.verify()`; `except ImportError` returns `False` without subprocess. Callable from any Python.
- `claude_hooks/providers/pgvector.py:328` — bare `import psycopg` in `_ensure_ready()`; `except ImportError` re-raises as `"install psycopg: ..."` without fallback. Callable via any `PgvectorProvider` operation (`recall`, `store`, KG ops, etc.).
- `scripts/migrate_to_pgvector.py:624` — bare `import psycopg` at module scope inside `main()`, no `try/except`, no fallback. Crashes `ModuleNotFoundError` from any non-conda Python.
- `scripts/bench_recall.py:107` — bare `import psycopg` inside `PgvectorBenchProvider.__init__()`; no guard. Crashes on class instantiation from any Python lacking psycopg.
- `tests/test_pgvector_integration.py:56` — bare `import psycopg` in `_skip_if_no_deps()`; raises `unittest.SkipTest`. Test-only; appropriate as-is.
- `tests/test_pgvector_integration.py:285` — bare `import psycopg` in `tearDownClass()`; broad `except Exception: pass`. Test teardown; silent no-op acceptable.

---

**Verdict:** The fallback pattern from 4e67dc2 is confined to `install.py` only. The two sites in `claude_hooks/providers/pgvector.py` and the two scripts are user-facing code paths that will crash from non-conda-env Python. The two test-file sites are appropriately left unguarded.

---
