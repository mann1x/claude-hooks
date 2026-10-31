---
session_id: csl-2026-10-22-2113-87ad
created: "2026-10-22T21:13:26"
question: "Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python."
models:
  planner: "minimax-m3.1-flash:cloud"
  researcher: "minimax-m3.1-flash:cloud"
  synthesizer: "minimax-m3.1-flash:cloud"
topology: council
effort: medium
duration_seconds: 132.94
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-10-22/tmp/claude-hooks-bench-bench-baseline-2026-05-07-Rb1F
---

Bottom line: the current tree has **six remaining textual gaps**—two runtime/provider sites, two standalone-CLI sites, and two test-only sites. The four non-test sites are exercisable from a non-conda Python that lacks `psycopg`; normal provider-dispatch and some CLI entry paths remain only partially traced. Exact attribution of the fallback hunks to commit `4e67dc2` could not be verified because Git history was unavailable.

- **`claude_hooks/providers/pgvector.py:123` — fallback absent; yes, directly exercisable.**
  - `PgvectorProvider.verify()` imports `psycopg` directly, catches `ImportError`, and returns `False` without trying the conda interpreter (`claude_hooks/providers/pgvector.py:121`, `claude_hooks/providers/pgvector.py:123`, `claude_hooks/providers/pgvector.py:124`).
  - A direct `PgvectorProvider.verify(...)` call therefore reaches this behavior from a psycopg-less non-conda Python.
  - It is **not reached through the normal installer verification path** in that environment: the installer's earlier current-interpreter import fails first (`install.py:2127`), is caught (`install.py:2135`), and proceeds to the conda subprocess (`install.py:2138`, `install.py:2140`, `install.py:2146`).
  - Classification: **real runtime gap, but graceful failure rather than a crash**.

- **`claude_hooks/providers/pgvector.py:328` — fallback absent; yes, directly exercisable.**
  - `_ensure_ready()` imports `psycopg` directly and raises `ImportError` when unavailable; there is no subprocess fallback (`claude_hooks/providers/pgvector.py:320`, `claude_hooks/providers/pgvector.py:328`, `claude_hooks/providers/pgvector.py:329`, `claude_hooks/providers/pgvector.py:330`).
  - A proven path is `PgvectorProvider.recall()` → `_ensure_ready()` (`claude_hooks/providers/pgvector.py:144 [no _ensure_ready at this line; line is in recall]`, `claude_hooks/providers/pgvector.py:148`). The resulting `ImportError` is caught by `recall()`, which logs the failure and returns no memories (`claude_hooks/providers/pgvector.py:149`, `claude_hooks/providers/pgvector.py:150`, `claude_hooks/providers/pgvector.py:151`).
  - Normal dispatcher/provider-selection reachability was not completely established, but direct provider use under a non-conda interpreter is sufficient to exercise the site.
  - Classification: **real runtime gap, with silent degradation to no results**.

- **`scripts/migrate_to_pgvector.py:624` — fallback absent; conditionally yes.**
  - The migration performs a direct, unguarded `import psycopg` (`scripts/migrate_to_pgvector.py:624`).
  - Invoking the script with a non-conda Python lacking `psycopg` and reaching the migration's database phase exercises the missing dependency there.
  - The exact enclosing-function/CLI call path was not fully inspected, so normal end-to-end entry-point reachability remains **unverified**.
  - Classification: **real CLI gap**.

- **`scripts/bench_recall.py:107` — fallback absent; conditionally yes.**
  - The import occurs in `PgvectorBenchProvider.__init__`, immediately before the provider connects (`scripts/bench_recall.py:105`, `scripts/bench_recall.py:107`, `scripts/bench_recall.py:108`).
  - A non-conda Python lacking `psycopg` exercises the site whenever that provider is selected and instantiated.
  - The benchmark CLI/provider-selection path and documented environment intent were not fully inspected, so normal command-line reachability remains **unverified**.
  - Classification: **real conditional CLI gap**.

- **`tests/test_pgvector_integration.py:56 [no SkipTest at this line; line is in _skip_if_no_deps]` — fallback absent; yes, but intentional and benign.**
  - This is a dependency probe: `ImportError` is converted into `unittest.SkipTest` (`tests/test_pgvector_integration.py:56 [no SkipTest at this line; line is in _skip_if_no_deps]`, `tests/test_pgvector_integration.py:57`, `tests/test_pgvector_integration.py:58`).
  - A psycopg-less non-conda test process reaches it and skips the test rather than failing.
  - Classification: **test-only exception; a conda fallback would defeat the purpose of the dependency probe**.

- **`tests/test_pgvector_integration.py:285` — fallback absent; conditionally yes, test-only.**
  - The import is in `tearDownClass()` (`tests/test_pgvector_integration.py:282`, `tests/test_pgvector_integration.py:285`).
  - It is exercisable if that teardown executes under a psycopg-less interpreter, but whether the class can reach teardown after the dependency-skip check was not fully established.
  - Classification: **test-only site; normal reachability unverified and not a production fallback candidate**.

The six `install.py` occurrences are **not remaining gaps**:

- `install.py:2092` is inside the verification subprocess source (`install.py:2089`, `install.py:2092`), which is run with the resolved conda Python (`install.py:2146`, `install.py:2147`).
- `install.py:2127` is the current-interpreter fast path, with `ImportError` leading to the conda subprocess (`install.py:2127`, `install.py:2135`, `install.py:2138`, `install.py:2146`).
- `install.py:2524` is the table-probe fast path, with `ImportError` leading to the conda subprocess (`install.py:2524`, `install.py:2536`, `install.py:2539`, `install.py:2557`).
- `install.py:2546` is part of that subprocess program and is executed with the conda interpreter (`install.py:2546`, `install.py:2557`, `install.py:2558`).
- `install.py:2647` is the schema-initialization fast path, with `ImportError` leading to the conda subprocess (`install.py:2647`, `install.py:2653`, `install.py:2656`, `install.py:2673`).
- `install.py:2666` is part of that subprocess program and is executed with the conda interpreter (`install.py:2666`, `install.py:2673`, `install.py:2674`).

The documented occurrences are examples/commands rather than executable Python import sites (`docs/pgvector-runbook.md:499`, `docs/pgvector-runbook.md:734`).
