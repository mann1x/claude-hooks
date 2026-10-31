# Consultation transcript — csl-2026-10-22-0824-e941

_Question_: Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python.


## Planner

1. Use `git show 4e67dc2 --stat` and `git show 4e67dc2` to identify the exact conda-env subprocess-fallback pattern added by the commit and enumerate every psycopg import site it changed; the repository-wide grep already found candidates at `install.py:2092`, `install.py:2127`, `install.py:2524`, `install.py:2647`, `claude_hooks/providers/pgvector.py:123`, `claude_hooks/providers/pgvector.py:328`, `tests/test_pgvector_integration.py:56`, `tests/test_pgvector_integration.py:285`, `scripts/migrate_to_pgvector.py:624`, and `scripts/bench_recall.py:107`.
2. Re-run an exact Python-token-aware search for `psycopg` imports and `from psycopg… import` forms across `claude_hooks/`, `scripts/`, `tests/`, and root entry points, excluding binaries such as `.claude-hooks/consultants/.../transcript.db-wal`, to ensure the candidate list is complete.
3. Compare each site against the commit-4e67dc2 fallback shape, especially `install.py:2138-2148`, `install.py:2539-2559`, and `install.py:2656-2675`, and classify each psycopg import as having a direct-import path, a conda subprocess fallback, or lacking a fallback; note that the fallback machinery relies on `find_conda_env_python()` at `install.py:101-196`.
4. Trace runtime entry points and callers for each deficient site, starting with `claude_hooks/providers/pgvector.py:121` and `claude_hooks/providers/pgvector.py:326`, then `scripts/migrate_to_pgvector.py:624` and `scripts/bench_recall.py:107`, to determine whether the enclosing code can be launched by a non-conda-env Python and therefore actually encounters `ImportError`.
5. Inspect the relevant launcher and test behavior around `bin/claude-hook`, `bin/claude-hook-pgvector-mcp`, `run.py`, and the pgvector integration tests to classify each site as “not exercisable from non-conda-env Python” when hooks are always pinned to the conda interpreter, “conditionally exercisable” when a direct script/module invocation bypasses that pin, or “directly exercisable”; use `install.py:2828-2847` and `install.py:3733-3736` as evidence that the installer explicitly allows system-Python fallback.
6. For every deficient site, verify behavior with a subprocess using a Python interpreter where `import psycopg` raises `ImportError`, while recording whether the site raises, returns a false/empty result, or lacks any conda-env subprocess route; then produce the final `file:line` audit table with direct evidence and an explicit “exercisable / not exercisable” verdict per site.
7. Report the unresolved comparison explicitly if commit `4e67dc2` cannot be retrieved: the attempted repository-wide grep for `4e67dc2` and subsequent file reads were unavailable because the tool budget ended, so the exact historical diff and final site-by-site verdicts remain unverified.

---

## Researcher (round 1)

- **Commit comparison is unresolved:** no `git show`/shell tool was available, so I could not verify which sites `4e67dc2` literally changed. The current tree nevertheless contains the recognizable pattern in three installer helpers: direct import → `ImportError` → `find_conda_env_python()` → conda subprocess. The resolver searches common env paths and `conda env list` at `install.py:101-107` and `install.py:151-175`.

- **Complete source-import inventory:** Python-token search found twelve import statements: six in `install.py` (`install.py:2092`, `install.py:2127`, `install.py:2524`, `install.py:2546`, `install.py:2647`, `install.py:2666`), two in the provider (`claude_hooks/providers/pgvector.py:123`, `claude_hooks/providers/pgvector.py:328`), two in tests (`tests/test_pgvector_integration.py:56`, `tests/test_pgvector_integration.py:285`), and two in scripts (`scripts/migrate_to_pgvector.py:624`, `scripts/bench_recall.py:107`). No additional `from psycopg... import` sites appeared.

- **`install.py:2092` — not deficient:** this import is inside `_PGVECTOR_VERIFY_SCRIPT`; it catches `ImportError`, emits a JSON failure, and exits cleanly at `install.py:2092-2096`. The parent invokes that script with the resolved conda interpreter at `install.py:2138-2148`, so a non-conda caller does not encounter this import.

- **`install.py:2127` — covered by fallback:** `_verify_pgvector_dsn` imports directly at `install.py:2125-2129`, catches `ImportError` at `install.py:2135-2136`, then retries through the conda interpreter at `install.py:2138-2161`. **Verdict: not exercisable through this helper from a non-conda interpreter lacking `psycopg`.**

- **`install.py:2524` — covered by fallback:** `_pgvector_tables_present` has a direct-import fast path at `install.py:2522-2537` and a conda subprocess retry at `install.py:2539-2569`. **Verdict: not exercisable through this helper from a non-conda interpreter lacking `psycopg`.**

- **`install.py:2546` — not deficient:** this is an import inside the subprocess program built by `_pgvector_tables_present`; the program is run with the resolved conda interpreter at `install.py:2556-2559`. A non-conda caller therefore does not execute it as its own import site.

- **`install.py:2647` — covered by fallback:** `_init_pgvector_schema` directly imports at `install.py:2645-2654` and retries through the conda interpreter at `install.py:2656-2681`. **Verdict: not exercisable through this helper from a non-conda interpreter lacking `psycopg`.**

- **`install.py:2666` — not deficient:** this import belongs to the subprocess program used by `_init_pgvector_schema`; that program is explicitly run with the resolved conda interpreter at `install.py:2673-2675`. A non-conda caller does not directly encounter it.

- **`claude_hooks/providers/pgvector.py:123` — deficient but handled locally:** `PgvectorProvider.verify` directly imports `psycopg` and catches `ImportError`, returning `False` with a warning at `claude_hooks/providers/pgvector.py:121-126`; there is no conda subprocess retry. **Exercisable: yes** when the classmethod is called directly under a Python without `psycopg`. In the installer path, however, `_verify_pgvector_dsn` catches the outer import at `install.py:2127-2136` and switches to its conda subprocess before calling `PgvectorProvider.verify` at `install.py:2130-2133`.

- **`claude_hooks/providers/pgvector.py:328` — deficient:** `_ensure_ready` directly imports `psycopg` and re-raises `ImportError` with an installation message at `claude_hooks/providers/pgvector.py:326-330`; no conda subprocess fallback exists. **Exercisable: yes.** `PgvectorProvider.recall` calls `_ensure_ready` at `claude_hooks/providers/pgvector.py:144-151`; on a non-conda interpreter without `psycopg`, the import failure is caught there and recall returns an empty list. The MCP dispatcher also calls provider recall at `claude_hooks/pgvector_mcp/server.py:267-276` and converts uncaught tool exceptions into an error response at `claude_hooks/pgvector_mcp/server.py:240-251`.

- **`scripts/migrate_to_pgvector.py:624` — deficient and directly exercisable:** `main()` performs an unguarded `import psycopg` before connecting at `scripts/migrate_to_pgvector.py:610-626`. The module is a normal CLI entry point at `scripts/migrate_to_pgvector.py:653-654`. **Verdict: yes**, running this script with a non-conda Python lacking `psycopg` reaches the import and raises `ImportError`; no fallback is present.

- **`scripts/bench_recall.py:107` — deficient and exercisable when that provider is selected:** `PgvectorBenchProvider.__init__` directly imports `psycopg` without handling `ImportError` at `scripts/bench_recall.py:105-109`. **Verdict: yes** for any non-conda Python that constructs this provider. The benchmark’s default CLI dispatch was not read, so whether the default invocation always selects this class remains unverified.

- **`tests/test_pgvector_integration.py:56` — intentional test skip, not a production gap:** the missing dependency is caught and converted to `unittest.SkipTest` at `tests/test_pgvector_integration.py:54-58`. **Exercisable: yes**, but the intended non-conda/no-`psycopg` result is a skipped test rather than a subprocess fallback.

- **`tests/test_pgvector_integration.py:285` — unguarded test cleanup import:** cleanup directly imports `psycopg` at `tests/test_pgvector_integration.py:283-287`. It is not reached if the surrounding test class is skipped by the missing-dependency guard, but the setup/teardown ordering was not read; therefore whether this specific line is reached on a no-`psycopg` run is **evidence-insufficient**. If reached, it raises `ImportError`, and this is test-only rather than a production path.

- **Launcher context:** the shared shell resolver prefers a repo venv or conda interpreter but explicitly falls back to `python3`/`python` from `PATH` at `bin/_resolve_python.sh:30-58`; `bin/claude-hook` executes that selected interpreter at `bin/claude-hook:16-23`. Thus a non-conda system Python remains a supported runtime selection, making the provider and script gaps real rather than merely theoretical.

---

## Researcher (round 1)

## Research findings

- **Current-tree import inventory:** outside `install.py`, the repository-wide Python-token search found six `import psycopg` sites: `claude_hooks/providers/pgvector.py:123`, `claude_hooks/providers/pgvector.py:328`, `tests/test_pgvector_integration.py:56`, `tests/test_pgvector_integration.py:285`, `scripts/migrate_to_pgvector.py:624`, and `scripts/bench_recall.py:107`.

- **`claude_hooks/providers/pgvector.py:123` — deficient; conditionally exercisable.**
  - `PgvectorProvider.verify()` catches `ImportError`, logs a warning, and returns `False`; it has no conda subprocess route itself (`claude_hooks/providers/pgvector.py:123-126`).
  - The installer masks this deficiency when *it* lacks `psycopg`: its fast path catches the import failure and invokes a conda interpreter subprocess (`install.py:2127-2148`).
  - The provider method remains directly callable without `psycopg`, so this is **conditionally exercisable**, not “not exercisable.”

- **`claude_hooks/providers/pgvector.py:328` — deficient; conditionally exercisable.**
  - The connection path catches the missing dependency and re-raises `ImportError("install psycopg: pip install psycopg[binary]")`; there is no provider-level conda fallback (`claude_hooks/providers/pgvector.py:326-334`).
  - This can be reached when a pgvector operation is invoked directly or through a system-Python hook/MCP process lacking `psycopg`.
  - **Verdict:** conditionally exercisable; static behavior is **raises `ImportError`**.

- **`scripts/migrate_to_pgvector.py:624` — deficient; directly/conditionally exercisable.**
  - The import is unguarded and immediately followed by `psycopg.connect(...)` (`scripts/migrate_to_pgvector.py:624-626`).
  - Running the migration script/module with a non-conda interpreter lacking `psycopg` bypasses every hook launcher and can reach this site when the pgvector migration path is selected.
  - **Verdict:** directly exercisable through script/module invocation; static behavior is **raises raw `ImportError`**. No conda subprocess route exists in this script.

- **`scripts/bench_recall.py:107` — deficient; directly/conditionally exercisable.**
  - `PgvectorBenchProvider.__init__` performs an unguarded import before connecting (`scripts/bench_recall.py:105-108`).
  - A direct benchmark invocation selecting the pgvector provider can therefore run under a non-conda interpreter without `psycopg`.
  - **Verdict:** directly exercisable through script invocation; static behavior is **raises raw `ImportError`**. No conda subprocess route exists in this script.

- **`tests/test_pgvector_integration.py:56` — deficient but intentionally non-operational; directly exercisable.**
  - The import is used as a dependency probe: `ImportError` becomes `unittest.SkipTest("psycopg not installed")` (`tests/test_pgvector_integration.py:54-58`).
  - **Verdict:** directly exercisable when pytest runs under a non-conda Python; static behavior is **returns/skips**, not a false result or exception.

- **`tests/test_pgvector_integration.py:285` — deficient; conditionally exercisable.**
  - The teardown import is unguarded and is followed by `psycopg.connect(...)` (`tests/test_pgvector_integration.py:282-287`).
  - It is not necessarily reached in a clean environment where the dependency probe skips the test class, but it is reachable if cleanup is entered without `psycopg`.
  - **Verdict:** conditionally exercisable; static behavior, if reached, is **raises raw `ImportError`**. Full test call-order evidence was not read, so normal reachability remains less certain than for the operational scripts.

- **The hooks are not “always pinned” to conda.** The shared resolver prefers a repo-local `.venv`, then conda, then system `python3`/`python`, and permits `CLAUDE_HOOKS_PY` override (`bin/_resolve_python.sh:3-17`, `bin/_resolve_python.sh:31-50`, `bin/_resolve_python.sh:53-58`). `bin/claude-hook` uses that resolver and executes `run.py` (`bin/claude-hook:16-23`); `bin/claude-hook-pgvector-mcp` uses the same resolver and executes the MCP module (`bin/claude-hook-pgvector-mcp:10-17`).

- **The installer explicitly permits system-Python fallback.** It reports “Hooks will fall back to system python3” when conda is unavailable (`install.py:2828-2847`), also selects system Python in non-interactive/declined setup paths (`install.py:2850-2857`), and prints the final runtime as either conda Python or `system python3` (`install.py:3733-3736`). Therefore no deficient site above can be classified as categorically “not exercisable from non-conda-env Python” under the current launchers.

- **Non-deficient `install.py` comparison sites.**
  - The verify fast path has a conda subprocess fallback (`install.py:2125-2161`).
  - The table-probe fast path has a conda subprocess fallback (`install.py:2522-2569`).
  - The schema-initialization fast path has a conda subprocess fallback (`install.py:2645-2681`).
  - The imports embedded in subprocess scripts occur inside the selected conda interpreter rather than the caller: verify at `install.py:2092`, table probe at `install.py:2546`, and schema initialization at `install.py:2666`.

- **Required subprocess experiment was not completed.** The available filesystem tools were disabled before an interpreter with an injected failing `psycopg` import could be run. The “raises / returns false / skips” outcomes above are static conclusions from the actual exception branches, not recorded subprocess results.

- **Historical comparison to `4e67dc2` remains unresolved.** The available tools did not provide `git show`/commit-object access; the repository-wide text search surfaced `4e67dc2` only in binary consultant checkpoint/WAL artifacts, not an authoritative source diff. The current tree demonstrably contains the installer fallback shape, but the exact sites changed by commit `4e67dc2` could not be independently verified.

---

## Synthesizer

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

---
