---
session_id: csl-2026-10-22-0759-9e9b
created: "2026-10-22T07:59:02"
question: "Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python."
models:
  planner: "minimax-m3.1-flash:cloud"
  researcher: "minimax-m3.1-flash:cloud"
  synthesizer: "minimax-m3.1-flash:cloud"
topology: council
effort: medium
duration_seconds: 121.87
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-10-22/tmp/claude-hooks-bench-bench-baseline-2026-05-07-YDhi
---

The static audit found **six direct `psycopg` import sites without the conda-subprocess fallback**; of these, **two are production-package sites**, **two are operator scripts**, and **two are test-only sites**. Only `claude_hooks/providers/pgvector.py:328` is on a confirmed normal non-conda hook path. Dynamic reproduction was not completed, and commit `4e67dc2` itself could not be inspected because Git-history access was unavailable.

### Production package

- `claude_hooks/providers/pgvector.py:123` — **no fallback; directly exercisable, but not by the confirmed installer fallback path.**
  - `PgvectorProvider.verify()` imports `psycopg` locally and returns `False` on `ImportError`; it does not invoke the conda interpreter (`claude_hooks/providers/pgvector.py:121`, `claude_hooks/providers/pgvector.py:123`, `claude_hooks/providers/pgvector.py:124`).
  - A direct call to `PgvectorProvider.verify()` from a Python lacking `psycopg` reaches this import and returns `False`.
  - The known installer path first imports `psycopg` in the current interpreter; if that fails, control goes to the conda subprocess before `PgvectorProvider.verify()` is called (`install.py:2127`, `install.py:2132`, `install.py:2135`, `install.py:2138`). Therefore that specific non-conda installer flow does **not** reach line 123.
  - The fallback executes the conda Python as `[conda_python, "-c", script, dsn]`, reads the final stdout line as JSON, and interprets its `ok` and `reason` fields (`install.py:2140`, `install.py:2146`, `install.py:2147`, `install.py:2153`, `install.py:2158`, `install.py:2161`). The target defaults to the `claude-hooks` conda environment (`install.py:66`, `install.py:101`).

- `claude_hooks/providers/pgvector.py:328` — **no fallback; yes, actually exercisable from a non-conda Python on a normal hook path.**
  - `_ensure_ready()` imports `psycopg` directly and converts `ImportError` into an installation-hint exception; it does not delegate to the conda environment (`claude_hooks/providers/pgvector.py:320`, `claude_hooks/providers/pgvector.py:328`, `claude_hooks/providers/pgvector.py:330`).
  - Provider recall calls `_ensure_ready()` (`claude_hooks/providers/pgvector.py:144 [no _ensure_ready at this line; line is in recall]`, `claude_hooks/providers/pgvector.py:148`) and converts its `ImportError` into an empty result (`claude_hooks/providers/pgvector.py:149`).
  - The pgvector MCP process imports the provider (`claude_hooks/pgvector_mcp/server.py:37`), while the installer permits the launcher to fall back to the current interpreter when conda is unavailable (`install.py:2296`, `install.py:2297`).
  - Consequently, system Python or another environment without `psycopg` can reach line 328 and lose database-backed recall or fail the MCP database path.

### Operator scripts

- `scripts/migrate_to_pgvector.py:624` — **no fallback; unconditionally exercisable when the migration command runs.**
  - The import is directly in `main()`, before connecting (`scripts/migrate_to_pgvector.py:610`, `scripts/migrate_to_pgvector.py:624`, `scripts/migrate_to_pgvector.py:626`).
  - Running the migration with system Python or another environment lacking `psycopg` fails at this import.

- `scripts/bench_recall.py:107` — **no fallback; conditionally exercisable.**
  - The import occurs when `PgvectorBenchProvider` is constructed (`scripts/bench_recall.py:105`, `scripts/bench_recall.py:106`, `scripts/bench_recall.py:107`) and is immediately followed by `psycopg.connect()` (`scripts/bench_recall.py:108`).
  - Merely importing the benchmark module does not exercise it; selecting and constructing the pgvector benchmark provider under a Python lacking `psycopg` does.

### Test-only sites

- `tests/test_pgvector_integration.py:56` — **no fallback; exercised as a dependency probe, intentionally skipped when unavailable.**
  - Missing `psycopg` raises `SkipTest` rather than invoking conda (`tests/test_pgvector_integration.py:54 [no SkipTest at this line; line is in _skip_if_no_deps]`, `tests/test_pgvector_integration.py:56`, `tests/test_pgvector_integration.py:58`).
  - Thus a non-conda pytest run encounters the import but skips the integration suite rather than failing.

- `tests/test_pgvector_integration.py:285` — **no fallback; normally unreachable in a Python lacking `psycopg`.**
  - The cleanup path imports `psycopg` before connecting (`tests/test_pgvector_integration.py:285`, `tests/test_pgvector_integration.py:287`).
  - However, the earlier dependency probe skips the suite when `psycopg` is absent (`tests/test_pgvector_integration.py:56`, `tests/test_pgvector_integration.py:58`). If the suite is running, the import has already succeeded in that interpreter.

### Excluded as already covered

The installer imports are not additional gaps: `install.py:2092`, `install.py:2546`, and `install.py:2666` are inside subprocess scripts; `install.py:2127`, `install.py:2524`, and `install.py:2647` are guarded fast paths followed by subprocess handling (`install.py:2089`, `install.py:2125`, `install.py:2138`, `install.py:2539`, `install.py:2656`).

The audit was verified by repository-wide import search and direct reads of the surrounding control flow. What remains unfinished is dynamic confirmation under a specific non-conda interpreter, and an exact `git show 4e67dc2` comparison; the current-tree fallback protocol was verified, but its attribution to that exact commit was not.
