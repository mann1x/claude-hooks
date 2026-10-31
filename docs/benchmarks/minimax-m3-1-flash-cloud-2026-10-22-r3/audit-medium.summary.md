---
session_id: csl-2026-10-22-0824-e941
created: "2026-10-22T08:24:02"
question: "Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python."
models:
  planner: "minimax-m3.1-flash:cloud"
  researcher: "minimax-m3.1-flash:cloud"
  synthesizer: "minimax-m3.1-flash:cloud"
topology: council
effort: medium
duration_seconds: 115.58
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-10-22/tmp/claude-hooks-bench-bench-baseline-2026-05-07-TN4A
---

Bottom line: six current-tree `psycopg` import sites lack the installer’s conda-env subprocess fallback; four are demonstrably or conditionally reachable from a non-conda Python, while two test sites are reachable but test-only.

### Sites still lacking the fallback

| File:line | Missing-fallback behavior | Exercisable from non-conda Python? |
|---|---|---|
| `claude_hooks/providers/pgvector.py:123` | `PgvectorProvider.verify()` catches `ImportError`, logs a warning, and returns `False`; it has no provider-level conda subprocess retry (`claude_hooks/providers/pgvector.py:123-126`). The installer masks this when called through `_verify_pgvector_dsn`, whose fast path falls back to a conda interpreter (`install.py:2127-2148`). | **Yes, conditionally.** Direct provider invocation reaches it. Under the installer, the outer helper switches to conda first. |
| `claude_hooks/providers/pgvector.py:328` | `_ensure_ready()` imports `psycopg` and re-raises a replacement `ImportError`; there is no conda subprocess route (`claude_hooks/providers/pgvector.py:326-334`). | **Yes, conditionally.** A pgvector operation can reach it under a system Python lacking `psycopg`. `recall()` catches the resulting `ImportError` and returns an empty list (`claude_hooks/providers/pgvector.py:144-151`). |
| `scripts/migrate_to_pgvector.py:624` | Unguarded import immediately before `psycopg.connect()` (`scripts/migrate_to_pgvector.py:610-626`); no fallback exists. | **Yes, directly.** Running the script with a non-conda Python lacking `psycopg` reaches the import and raises `ImportError`. |
| `scripts/bench_recall.py:107` | `PgvectorBenchProvider.__init__` imports `psycopg` without handling `ImportError` (`scripts/bench_recall.py:105-109`). | **Yes, conditionally.** It is directly reachable when that provider is selected. Whether the benchmark’s default invocation selects it was not verified. |
| `tests/test_pgvector_integration.py:56` | Deliberate dependency probe: `ImportError` becomes `unittest.SkipTest("psycopg not installed")` (`tests/test_pgvector_integration.py:54-58`). | **Yes, directly, but test-only.** The result is a skipped test, not an exception or conda fallback. |
| `tests/test_pgvector_integration.py:285` | Unguarded teardown import followed by `psycopg.connect()` (`tests/test_pgvector_integration.py:282-287`). | **Conditionally.** If reached, it raises raw `ImportError`. Normal reachability is uncertain because the surrounding test may first be skipped by the dependency probe. Test-only. |

The provider and scripts are realistic non-conda paths rather than merely theoretical: the shared launcher prefers a repo venv or conda interpreter but permits `CLAUDE_HOOKS_PY` override and falls back to `python3`/`python` from `PATH` (`bin/_resolve_python.sh:3-17`, `bin/_resolve_python.sh:31-50`, `bin/_resolve_python.sh:53-58`). The hook and pgvector-MCP launchers execute the interpreter selected by that resolver (`bin/claude-hook:16-23`, `bin/claude-hook-pgvector-mcp:10-17`).

### Current-tree `install.py` sites that are not gaps

The complete Python-source inventory contains 12 import statements. The six `install.py` imports already have the relevant protection:

- `install.py:2092`: import inside the verify subprocess program; that program is run with the resolved conda interpreter (`install.py:2092-2098`, `install.py:2138-2148`). **Not independently exercisable as a non-conda caller import.**
- `install.py:2127`: `_verify_pgvector_dsn` fast path catches `ImportError` and retries through conda (`install.py:2125-2161`). **Not exercisable through this helper from a non-conda interpreter lacking `psycopg`.**
- `install.py:2524`: `_pgvector_tables_present` catches `ImportError` and retries through conda (`install.py:2522-2569`). **Not exercisable through this helper from a non-conda interpreter lacking `psycopg`.**
- `install.py:2546`: import inside that conda subprocess program (`install.py:2543-2559`). **Not independently exercisable as a caller import.**
- `install.py:2647`: `_init_pgvector_schema` catches `ImportError` and retries through conda (`install.py:2645-2681`). **Not exercisable through this helper from a non-conda interpreter lacking `psycopg`.**
- `install.py:2666`: import inside that conda subprocess program (`install.py:2664-2675`). **Not independently exercisable as a caller import.**

### Verification limits

- The current-tree inventory, exception branches, and fallback structure were checked by Python-token search and source inspection. The provider lines and all three installer fallback paths were directly re-read.
- The exact historical diff for commit `4e67dc2` could not be retrieved because Git object access was unavailable. Therefore, the audit verifies the fallback pattern present in the current tree, but cannot independently prove which lines that commit originally changed.
- The requested subprocess experiment with an interpreter whose `import psycopg` fails was **not completed**. The outcomes above are static results from the shown exception branches, not recorded subprocess runs.
- `tests/test_pgvector_integration.py:285` remains the only material reachability uncertainty: its setup/teardown ordering was not fully established, so it is conservatively classified as conditional rather than directly exercisable.
