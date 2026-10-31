---
session_id: csl-2026-10-22-2050-c5f0
created: "2026-10-22T20:50:31"
question: "Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python."
models:
  planner: "minimax-m3.1-flash:cloud"
  researcher: "minimax-m3.1-flash:cloud"
  synthesizer: "minimax-m3.1-flash:cloud"
topology: council
effort: medium
duration_seconds: 130.22
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-10-22/tmp/claude-hooks-bench-bench-baseline-2026-05-07-i6US
---

Five import sites are reachable from a non-conda Python lacking `psycopg`; one additional test-only site is blocked by the preceding dependency skip. The two `claude_hooks` production sites both still lack the subprocess fallback.

| Import site | Fallback status | Exercisable from non-conda Python? | Reachability |
|---|---|---|---|
| `claude_hooks/providers/pgvector.py:123` | **Lacks fallback.** `ImportError` is caught and `verify()` returns `False` rather than invoking the conda interpreter (`claude_hooks/providers/pgvector.py:124`, `claude_hooks/providers/pgvector.py:126`). | **Yes.** `PgvectorProvider.verify()` is public and directly invoked by the integration test at `tests/test_pgvector_integration.py:186`. A non-conda interpreter without `psycopg` therefore reaches the import and gets `False`. | The normal installer path does not reach this site when `psycopg` is missing: its own import fails first at `install.py:2127`, then the installer selects the conda Python at `install.py:2140` and runs the subprocess at `install.py:2146`. Thus the site is directly exercisable but bypassed by the installer fallback. |
| `claude_hooks/providers/pgvector.py:328` | **Lacks fallback.** The missing-package `ImportError` is converted to an install instruction at `claude_hooks/providers/pgvector.py:330`; no conda subprocess is used. | **Yes.** `recall()` calls `_ensure_ready()` at `claude_hooks/providers/pgvector.py:148` and catches the resulting `ImportError` at `claude_hooks/providers/pgvector.py:149`, returning no results. | `PgvectorProvider` can be resolved lazily through `get_provider_class()` at `claude_hooks/providers/__init__.py:43` and `claude_hooks/providers/__init__.py:53`. The runtime resolver prefers a repo venv over conda and ultimately permits system `python3`/`python` (`bin/_resolve_python.sh:6`, `bin/_resolve_python.sh:12`, `bin/_resolve_python.sh:14`, `bin/_resolve_python.sh:53`), so a non-conda interpreter can reach this path. |
| `scripts/migrate_to_pgvector.py:624` | **Lacks fallback.** The standalone script imports `psycopg` directly and immediately connects at `scripts/migrate_to_pgvector.py:626`. | **Yes.** Running the migration script with a non-conda Python lacking `psycopg` reaches the import and fails. | Direct script entry point; it is not routed through the installer’s conda subprocess. |
| `scripts/bench_recall.py:107` | **Lacks fallback.** `PgvectorBenchProvider.__init__` imports `psycopg` and immediately connects at `scripts/bench_recall.py:108`. | **Yes.** Running the benchmark with a non-conda Python lacking `psycopg` reaches the import and fails. | Direct benchmark entry point; it is not routed through the installer’s conda subprocess. |
| `tests/test_pgvector_integration.py:56` | **Lacks fallback, intentionally as a dependency probe.** A missing import raises `unittest.SkipTest` at `tests/test_pgvector_integration.py:58`. | **Yes, but non-failing.** The import executes in a non-conda interpreter; missing `psycopg` converts the test into a skip rather than a fallback. | The helper is called during class setup at `tests/test_pgvector_integration.py:95`. |
| `tests/test_pgvector_integration.py:285` | **Lacks fallback.** This test-only import has no subprocess fallback. | **Not in the missing-`psycopg` case.** The earlier dependency probe skips the integration suite when `psycopg` is absent (`tests/test_pgvector_integration.py:56`, `tests/test_pgvector_integration.py:58`), so this later cleanup import is not reached. It is reachable in a non-conda environment that already has `psycopg` and the other integration dependencies. | Test cleanup only; not a production fallback defect. |

Install-time matches are **not omissions**:

- `install.py:2127` has the missing-import branch and conda subprocess fallback at `install.py:2135`, `install.py:2140`, and `install.py:2146`.
- `install.py:2524` has the corresponding schema-probe fallback beginning at `install.py:2539`.
- `install.py:2647` has the corresponding schema-initialization fallback beginning at `install.py:2656`.
- `install.py:2092`, `install.py:2546`, and `install.py:2666` are payload imports intended to execute inside the selected conda subprocess, so a nested fallback is unnecessary.

The current tree contains 12 source import occurrences: the six lacking sites above and the six install-time occurrences. No `from psycopg ...` or dynamic `import_module("psycopg")` source import was found. The available evidence verifies the current fallback implementation, but the `4e67dc2` commit diff itself could not be inspected, so equivalence to that exact historical diff remains unverified.
