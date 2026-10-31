---
session_id: csl-2026-10-22-1615-81c1
created: "2026-10-22T16:15:50"
question: "Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python."
models:
  planner: "minimax-m3.1-flash:cloud"
  researcher: "minimax-m3.1-flash:cloud"
  synthesizer: "minimax-m3.1-flash:cloud"
topology: council
effort: medium
duration_seconds: 139.52
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-10-22/tmp/claude-hooks-bench-bench-baseline-2026-05-07-PPRS
---

Bottom line: the production gap is `claude_hooks/providers/pgvector.py:328`; it is genuinely reachable through the supported hook launcher when the resolver selects system Python. The other uncovered production import, `claude_hooks/providers/pgvector.py:123`, is only conditionally reachable; installer flows bypass it when the current interpreter lacks `psycopg`.

### Import-site audit

- `install.py:2092` — **Covered; not a gap.** This import is inside the child verification program executed by the resolved conda interpreter. The parent process delegates the complete connection and extension check through that subprocess (`install.py:2098`, `install.py:2102`, `install.py:2145`).
- `install.py:2127` — **Covered; not a gap.** This is the current-interpreter fast path. `ImportError` leads to the conda subprocess fallback rather than failure (`install.py:2127`, `install.py:2135`, `install.py:2138`, `install.py:2146`).
- `install.py:2524` — **Covered; not a gap.** The current-interpreter table probe catches `ImportError` and delegates the table query to conda (`install.py:2524`, `install.py:2536`, `install.py:2539`, `install.py:2557`).
- `install.py:2546` — **Covered; not a gap.** This import is part of the child program run by the conda interpreter (`install.py:2543`, `install.py:2546`, `install.py:2557`).
- `install.py:2647` — **Covered; not a gap.** The current-interpreter schema-initialization path catches `ImportError` and falls back to a conda subprocess (`install.py:2647`, `install.py:2653`, `install.py:2656`, `install.py:2673`).
- `install.py:2666` — **Covered; not a gap.** This import belongs to the conda child program that executes and commits the complete DDL (`install.py:2664`, `install.py:2666`, `install.py:2673`).

- `claude_hooks/providers/pgvector.py:123` — **Conditionally exercisable; a genuine uncovered site, but not through the installer’s missing-`psycopg` path.** `PgvectorProvider.verify()` imports and uses `psycopg` directly, returning `False` on `ImportError` (`claude_hooks/providers/pgvector.py:123`, `claude_hooks/providers/pgvector.py:124`, `claude_hooks/providers/pgvector.py:128`, `claude_hooks/providers/pgvector.py:132`). The installer imports `psycopg` before calling `verify()`, so a non-conda interpreter without `psycopg` goes directly to the conda subprocess (`install.py:2127`, `install.py:2132`, `install.py:2135`, `install.py:2138`). The site is nevertheless reached when `PgvectorProvider.verify()` is called directly from a Python lacking `psycopg`; it then returns `False` rather than delegating to conda.

- `claude_hooks/providers/pgvector.py:328` — **Actually exercisable; production runtime gap.** `_ensure_ready()` imports `psycopg` directly and raises `ImportError` if absent, with no conda subprocess fallback (`claude_hooks/providers/pgvector.py:320`, `claude_hooks/providers/pgvector.py:328`, `claude_hooks/providers/pgvector.py:329`, `claude_hooks/providers/pgvector.py:334`). Normal recall and store paths call `_ensure_ready()` (`claude_hooks/providers/pgvector.py:144 [no _ensure_ready at this line; line is in recall]`, `claude_hooks/providers/pgvector.py:148`, `claude_hooks/providers/pgvector.py:214`). This is a supported runtime mode: the shared resolver can select `python3` or `python` from `PATH` when neither the repository venv nor conda is available (`bin/_resolve_python.sh:14`, `bin/_resolve_python.sh:53`), and the POSIX hook executes `run.py` with that selected interpreter (`bin/claude-hook:16`, `bin/claude-hook:18`, `bin/claude-hook:23`). Therefore a supported system-Python hook runtime lacking `psycopg` can reach the uncovered import.

- `scripts/migrate_to_pgvector.py:624` — **Actually exercisable; operator CLI gap outside the hook runtime.** The standalone migration script imports `psycopg` directly and connects without a local conda-subprocess fallback (`scripts/migrate_to_pgvector.py:624`, `scripts/migrate_to_pgvector.py:626`). Running it with a non-conda Python lacking `psycopg` reaches and fails at this import.

- `scripts/bench_recall.py:107` — **Actually exercisable; benchmark CLI gap outside the hook runtime.** The pgvector benchmark provider imports and connects directly in its constructor (`scripts/bench_recall.py:105`, `scripts/bench_recall.py:107`, `scripts/bench_recall.py:108`). It is reached when the benchmark selects that backend, including under a non-conda Python lacking `psycopg`.

- `tests/test_pgvector_integration.py:56` — **Executed but intentionally harmless.** The import is inside an optional-dependency probe that catches `ImportError` and skips the test (`tests/test_pgvector_integration.py:54`, `tests/test_pgvector_integration.py:56`, `tests/test_pgvector_integration.py:57`). A non-conda test interpreter can reach the import, but it is converted into a skip rather than a fallback failure.

- `tests/test_pgvector_integration.py:285` — **Conditionally exercisable; test-only.** This is a direct test import followed by a database connection (`tests/test_pgvector_integration.py:285`, `tests/test_pgvector_integration.py:287`). Its exact skip/failure control flow was not established, so whether a non-conda run skips or fails there remains **unverified**. It is not part of the production hook path.

The documented `psycopg` probe at `docs/pgvector-runbook.md:734` is a shell command rather than a Python import site and explicitly targets the conda interpreter, so it is not an uncovered import.

The exact `4e67dc2` diff was not available for direct inspection; this audit compares current imports against the current fallback implementation, which delegates the complete database operation—not merely an import probe—through `subprocess.run` with the resolved conda interpreter (`install.py:2146`, `install.py:2557`, `install.py:2673`).
