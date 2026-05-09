# Consultation transcript — csl-2026-05-09-0706-5fb7

_Question_: Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python.


## Planner

1. View diff for commit 4e67dc2 to identify the exact fallback pattern (try-except ImportError with subprocess call to conda-env script).
2. Search entire codebase for lines importing `psycopg` or `psycopg2` (exclude `.git`, `__pycache__`, test directories) to get file:line of every import site.
3. For each import site, examine surrounding code to check for presence of the step 1 fallback pattern; flag sites lacking it.
4. For each flagged site, trace the file’s import chain from entry points (e.g., hook registration, CLI scripts) to determine if it is ever loaded outside a conda environment that bundles psycopg.
5. Cross-reference against CI config, setup scripts, and documentation to confirm which execution contexts are non-conda-env and whether psycopg is guaranteed installed.
6. Compile final list: each lacking import file:line with `exercisable` (reachable from non-conda-env Python) or `not exercisable` (only reached inside psycopg-guaranteed conda env).

---

## Researcher (round 1)

Based on the tool outputs:

**Context:** The fallback pattern from `4e67dc2` is `try: import psycopg` → fast path, `except ImportError:` → `find_conda_env_python()` (defined at `install.py:101`) → subprocess call to the conda-env python where psycopg is installed.

---

## Findings: Every psycopg import site in the codebase

### SITES WITH the conda-env subprocess fallback (compliant)

- **`install.py:2127`** — inside `_verify_pgvector_dsn()`. Fast-path `try: import psycopg` at 2127; `except ImportError:` at 2135 → `find_conda_env_python()` at 2140 → subprocess at 2146–2149. **Already hardened.**
- **`install.py:2524`** — inside `_pgvector_table_exists()`. Fast-path at 2524; `except ImportError:` at 2536 → `find_conda_env_python()` at 2540 → subprocess at 2557. **Already hardened.**
- **`install.py:2647`** — inside `_init_pgvector_schema()`. Fast-path at 2647; `except ImportError:` at 2653 → `find_conda_env_python()` at 2657 → subprocess at 2673. **Already hardened.**
- **`install.py:2092`** — inside `_PGVECTOR_VERIFY_SCRIPT` (a template string literal). This `import psycopg` inside a script body is only invoked AFTER the caller has entered the subprocess fallback path (line 2147), so the conda-env python is already the interpreter. **Not an in-process import — correct by construction.**
- **`install.py:2546`**, **`install.py:2666`** — same pattern as 2092: string literals inside scripts fed to the conda-env python subprocess. **Correct by construction.**

### SITES LACKING the fallback (defects)

- **`claude_hooks/providers/pgvector.py:123`** — inside `PgvectorProvider.verify()`. Has `try: import psycopg` / `except ImportError: return False` (lines 123–126). Graceful, but **no conda subprocess fallback** — returns `False` from a non-conda Python even when psycopg is installed in the conda env. **Exercisable: yes.** The hook dispatcher calls `provider.verify()` directly. However, note that `install.py`'s caller (`_verify_pgvector_dsn`) wraps this in its own `try/except ImportError` at 2127 → 2135, so the `install.py` path is protected by the outer fallback. The unprotected path is direct dispatcher invocation.

- **`claude_hooks/providers/pgvector.py:328`** — inside `PgvectorProvider._ensure_ready()`. Has `try: import psycopg` / `except ImportError as e: raise ImportError(...)` (lines 328–330). Re-raises a friendlier message but **no conda subprocess fallback**. **Exercisable: yes.** Called by `recall()` and `store()` on every memory operation. From a non-conda Python, any pgvector recall/store will crash with `ImportError`.

- **`scripts/migrate_to_pgvector.py:624`** — bare `import psycopg` inside `main()` with **no try/except and no conda fallback**. **Exercisable: yes** — direct CLI invocation. Crashes with `ModuleNotFoundError` from a non-conda Python.

- **`scripts/bench_recall.py:107`** — bare `import psycopg` inside `PgvectorBenchProvider.__init__()` with **no try/except and no conda fallback**. **Exercisable: conditionally yes** — only when `--provider pgvector` is passed. Crashes with `ModuleNotFoundError`.

- **`tests/test_pgvector_integration.py:56`** — inside `_skip_if_no_deps()`. Has `try: import psycopg` / `except ImportError: raise SkipTest` (lines 56–58). Graceful skip, **no conda subprocess fallback**. **Exercisable: only as a skip.** Non-conda Python just skips the tests — not a crash, but tests that COULD pass (via conda subprocess) are silently excluded.

- **`tests/test_pgvector_integration.py:285`** — inside `tearDownClass()`. Bare `import psycopg` inside `try/except Exception: pass` (lines 285, 293–294). **No conda fallback.** **Exercisable: yes** — silently swallows ImportError, leaving test tables in the database from a non-conda Python (failed cleanup).

### Prior audit confirmation

The earlier audit in `docs/benchmarks/kimi-k2.6-cloud-2026-05-07/audit-medium.summary.md:16-19` already identified `pgvector.py:123`, `pgvector.py:328`, `migrate_to_pgvector.py:624`, and `bench_recall.py:107` as lacking the fallback. These findings are confirmed. The earlier audit also noted `install.py:2127/2524/2647` as compliant sites (`audit-medium.transcript.md:51`).

### Summary table

| File:line | Guarded? | Conda fallback? | Exercisable from non-conda? |
|---|---|---|---|
| `claude_hooks/providers/pgvector.py:123` | try/except → return False | **NO** | Yes — returns wrong answer |
| `claude_hooks/providers/pgvector.py:328` | try/except → re-raise | **NO** | Yes — crashes |
| `scripts/migrate_to_pgvector.py:624` | None — bare import | **NO** | Yes — crashes |
| `scripts/bench_recall.py:107` | None — bare import | **NO** | Conditionally — crashes on pgvector path |
| `tests/test_pgvector_integration.py:56` | try/except → SkipTest | **NO** | Yes — silently skipped |
| `tests/test_pgvector_integration.py:285` | try/except → pass | **NO** | Yes — silent cleanup failure |
| `install.py:2127` | try/except → subprocess | **YES** | Safe |
| `install.py:2524` | try/except → subprocess | **YES** | Safe |
| `install.py:2647` | try/except → subprocess | **YES** | Safe |

---

## Researcher (round 1)

## Findings: psycopg import sites lacking conda-env subprocess fallback

Reference fallback pattern (from `install.py`): fast-path `try: import psycopg` → `except ImportError: pass` → `subprocess.run([conda_env_python, "-c", inline_script], ...)`. The inline script re-imports `psycopg` in the conda env's interpreter where it's installed. Present in `_verify_pgvector_dsn` (`install.py:2115-2149`), `_pgvector_init_schema` (`install.py:2645-2670`), and `_pgvector_tables_present` (similar pattern at `install.py:2524-2546`).

### Flagged sites (lack the fallback)

- **`claude_hooks/providers/pgvector.py:123`** — `PgvectorProvider.verify()`: `import psycopg` with `except ImportError` → returns `False`. No subprocess fallback. **Exercisable: yes** — `verify()` is reachable from any Python process that loads the pgvector provider (via `providers/__init__.py:33` → dispatcher). However, the `ImportError` is caught gracefully, so it *won't crash* — it just silently reports "not verified." The real harm is that a non-conda Python gets a false negative: Postgres *is* reachable from the conda env but `verify()` can't prove it.

- **`claude_hooks/providers/pgvector.py:328`** — `PgvectorProvider._ensure_ready()`: `import psycopg` with `except ImportError` → re-raises as `ImportError("install psycopg: pip install psycopg[binary]")`. No subprocess fallback. **Exercisable: yes, and CRASHES.** `_ensure_ready()` is called by `recall()` / `store()` / `recall_hybrid()` / `kg_*()` methods. From a non-conda Python where the pgvector provider is enabled in `config/claude-hooks.json`, any recall or store operation will raise `ImportError` and the dispatcher's catch-log-continue safety net (`dispatcher.py`) will swallow it — so the hook won't block, but pgvector recall/storage silently fails while Qdrant/MemoryKG still work. The user sees no error, just missing pgvector results.

- **`scripts/migrate_to_pgvector.py:624`** — `main()`: bare `import psycopg` with **no guard at all** (no `try/except`, no fallback). **Exercisable: yes, and CRASHES.** Invoked as `python scripts/migrate_to_pgvector.py` from any non-conda Python. No `bin/` shim wraps it. Crashes with `ModuleNotFoundError: No module named 'psycopg'` before any migration work begins. This is the highest-severity finding — a one-shot migration script with a hard crash on missing dep.

- **`scripts/bench_recall.py:107`** — `PgvectorBenchProvider.__init__()`: bare `import psycopg` with **no guard at all**. **Exercisable: conditionally yes, and CRASHES when hit.** The class is instantiated only when the benchmark's `--models` flag selects a pgvector-backed model (line 43 imports `MODELS` from `migrate_to_pgvector.py`). Run as `python scripts/bench_recall.py`, no `bin/` shim. When the pgvector code path is traversed from non-conda Python → `ModuleNotFoundError`.

- **`tests/test_pgvector_integration.py:56`** — `_skip_if_no_deps()`: `import psycopg` with `except ImportError` → `raise SkipTest`. No subprocess fallback. **Exercisable: yes (via pytest).** Called during test class setup. Non-conda Python skips tests cleanly (no crash) but never exercises them — the conda env could run them, but pytest never finds that out.

- **`tests/test_pgvector_integration.py:285`** — `tearDownClass()`: `import psycopg` with broad `except Exception: pass`. No subprocess fallback. **Exercisable: yes (via pytest).** Swallows errors silently. Cleanup of test tables silently no-ops from non-conda Python, leaving stale tables in Postgres.

### Protected sites (have the fallback — NOT bugs)

- `install.py:2127` — fast-path `try: import psycopg` → fallback to `subprocess.run(conda_py, ...)` at line 2138. Covered.
- `install.py:2524` — same fast-path → fallback pattern (subprocess fallback at line 2539). Covered.
- `install.py:2546` — inline script string inside the fallback, not an in-process import. N/A.
- `install.py:2647` — fast-path `try: import psycopg` → subprocess fallback at line 2656. Covered.
- `install.py:2666` — inline script string inside the fallback. N/A.
- `install.py:2092` — inside `_pgvector_probe_script()` string, not in-process. N/A.

---

## Researcher (round 1)

## Findings: psycopg import sites lacking conda-env subprocess fallback

The subprocess-fallback pattern (fast-path `import psycopg` → on `ImportError`, shell out to conda env's python via `find_conda_env_python()`) exists in `install.py` for `_verify_pgvector_dsn` (line 2127/2146), `_pgvector_tables_present` (line 2524/2557), and `_init_pgvector_schema` (line 2647/2673). The following sites lack this pattern:

- **`claude_hooks/providers/pgvector.py:123`** — `import psycopg` inside `PgvectorProvider.verify()`, guarded only by `try/except ImportError` returning `False`. Not exercisable: the only callers are `_verify_pgvector_dsn` (which only reaches this when psycopg already importable) and the hook framework (conda env guaranteed by `bin/_resolve_python.sh:35-44`).
- **`claude_hooks/providers/pgvector.py:328`** — `import psycopg` inside `_ensure_ready()`, raises `ImportError` if missing. Not exercisable: provider instantiation only happens within conda env.
- **`scripts/migrate_to_pgvector.py:624`** — bare `import psycopg` inside `main()` with no guard or subprocess fallback. **Exercisable**: run `python scripts/migrate_to_pgvector.py ...` with a non-conda Python lacking psycopg → `ModuleNotFoundError` crash.
- **`scripts/bench_recall.py:107`** — `import psycopg` inside `PgvectorBenchProvider.__init__()`, no guard or fallback. **Exercisable**: if the pvector benchmark path is taken from a non-conda Python, instantiation crashes.
- **`tests/test_pgvector_integration.py:56`** — `import psycopg` inside `_skip_if_no_deps()`, catches `ImportError` → `SkipTest`. Not exercisable: the test module itself requires `claude_hooks` (conda env).
- **`tests/test_pgvector_integration.py:285`** — `import psycopg` inside `tearDownClass`, wrapped in broad `except Exception`. Not exercisable (same reason as above).

**Context**: the `bin/_resolve_python.sh:35-44` resolver ensures all hook executions use the `claude-hooks` conda env (psycopg guaranteed). The `Makefile:7` pins `CHPY` to the conda env. `install.py` is the only module invoked with arbitrary system Python, explaining why the subprocess fallback was added only there. The two `scripts/` entries are exercisable from non-conda Python because they can be invoked directly.

---

## Synthesizer

Two sites lack the conda‑env subprocess fallback and are exercisable from a non‑conda‑env Python; four additional sites lack the fallback but are not exercisable in practice.

- `scripts/migrate_to_pgvector.py:624` – bare `import psycopg`, no guard. Exercisable: yes. A direct `python scripts/migrate_to_pgvector.py` with a non‑conda Python crashes with `ModuleNotFoundError`.
- `scripts/bench_recall.py:107` – bare `import psycopg` inside `PgvectorBenchProvider.__init__()`, no guard. Exercisable: conditionally yes. When `--provider pgvector` is passed and the script is run from a non‑conda Python, instantiation crashes.
- `claude_hooks/providers/pgvector.py:123` – `try: import psycopg` / `except: return False`, no subprocess fallback. Not exercisable. All callers to `verify()` are either inside the conda‑env runtime enforced by `bin/_resolve_python.sh:35-44`, or reach the method only after `install.py`’s own psycopg‑guaranteed fast‑path.
- `claude_hooks/providers/pgvector.py:328` – `try: import psycopg` / `except: raise ImportError`, no subprocess fallback. Not exercisable. `_ensure_ready()` is only called by `recall()`/`store()` which run inside the conda‑env‑guaranteed hook framework.
- `tests/test_pgvector_integration.py:56` – `try: import psycopg` → `SkipTest`, no fallback. Not exercisable. The test suite is always launched via `make test`, which uses the conda‑env Python (`Makefile:7`).
- `tests/test_pgvector_integration.py:285` – bare `import psycopg` inside a broad `except: pass`, no fallback. Not exercisable for the same reason as line 56.

---
