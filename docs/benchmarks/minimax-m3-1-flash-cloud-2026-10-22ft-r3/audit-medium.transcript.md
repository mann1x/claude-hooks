# Consultation transcript — csl-2026-10-22-2113-87ad

_Question_: Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python.


## Planner

1. Inspect commit `4e67dc2` with `git show --stat` and `git show 4e67dc2 --` to identify the exact conda-env subprocess fallback pattern, the files it changed, and the import sites that were intentionally exempted.
2. Enumerate every direct `import psycopg`/`from psycopg ...` site across the repository, then compare each against the `4e67dc2` diff; the confirmed candidates already include `claude_hooks/providers/pgvector.py:123`, `claude_hooks/providers/pgvector.py:328`, `install.py:2092`, `install.py:2127`, `install.py:2524`, `install.py:2546`, `install.py:2647`, `install.py:2666`, `scripts/migrate_to_pgvector.py:624`, and `scripts/bench_recall.py:107`, with `tests/test_pgvector_integration.py:56` and `tests/test_pgvector_integration.py:285` requiring separate test-only classification.
3. For each non-test import site, inspect its surrounding function and all callers to determine whether it is reachable from an ordinary non-conda interpreter, distinguishing runtime code, installer-only/template code, and CLI scripts (`claude_hooks/providers/pgvector.py:121-139`, `claude_hooks/providers/pgvector.py:328`, and the listed `install.py`/script locations).
4. Trace how `claude_hooks/providers/pgvector.py` is instantiated and invoked from provider selection, recall/store paths, and verification flows, checking whether the current process can be a system Python or only the dedicated conda environment; verify the second import’s enclosing method beginning near `claude_hooks/providers/pgvector.py:328`.
5. Inspect `install.py:2092-2666` to distinguish actual executed `import psycopg` checks from generated launcher/source strings, and determine whether generated launchers pin the conda Python or merely expose a `pgvector-mcp` command that can be run by a non-conda Python.
6. Inspect `scripts/migrate_to_pgvector.py:624` and `scripts/bench_recall.py:107` entry points and usage docs to verify whether each script is intended to run under the conda env, and separately classify whether a user can invoke it directly with a non-conda Python.
7. Produce a final table containing every confirmed import `file:line`, whether the `4e67dc2` fallback is absent, the precise call path that makes the site exercisable from non-conda Python (or “not exercisable” with the blocking reason), and note explicitly where a conclusion remains uncertain because the relevant enclosing function/caller was not yet inspected.

---

## Researcher (round 1)

- **Commit baseline:** I could not inspect `4e67dc2` itself because no Git command tool was available; the commit’s changed-file list and exact diff remain unverified. The current tree nevertheless exposes the intended pattern clearly: try the current interpreter, catch `ImportError`, then invoke the resolved conda Python via `subprocess.run` (`install.py:2135`, `install.py:2138`, `install.py:2140`, `install.py:2146`).

- **`claude_hooks/providers/pgvector.py:123` — missing fallback; conditionally exercisable.** This is the in-process import in `PgvectorProvider.verify()`. It catches `ImportError` and returns `False`, with no conda subprocess (`claude_hooks/providers/pgvector.py:121`, `claude_hooks/providers/pgvector.py:123`, `claude_hooks/providers/pgvector.py:124`). A direct `PgvectorProvider.verify()` call from a psycopg-less system Python reaches this site. The normal installer path does not: its earlier import at `install.py:2127` raises first and redirects to the subprocess fallback at `install.py:2138`.

- **`claude_hooks/providers/pgvector.py:328` — missing fallback; exercisable from a non-conda runtime.** This import is in `_ensure_ready()` and converts absence of psycopg into `ImportError`; it never shells out (`claude_hooks/providers/pgvector.py:320`, `claude_hooks/providers/pgvector.py:326`, `claude_hooks/providers/pgvector.py:328`, `claude_hooks/providers/pgvector.py:330`). The import is reached by ordinary provider operations: `recall()` calls `_ensure_ready()` (`claude_hooks/providers/pgvector.py:144`, `claude_hooks/providers/pgvector.py:148`), `store()` does likewise (`claude_hooks/providers/pgvector.py:210`, `claude_hooks/providers/pgvector.py:214`), and `count()` does too (`claude_hooks/providers/pgvector.py:237`, `claude_hooks/providers/pgvector.py:241`). This matters because the installer explicitly permits hooks to fall back to system Python when the conda environment is absent (`install.py:2846`, `install.py:2847`). Thus, with pgvector enabled, a system-Python hook process lacking psycopg can reach the failing import.

- **`install.py:2092` — fallback present; not a missing site.** This import is text inside `_PGVECTOR_VERIFY_SCRIPT`, not an import in the installer process (`install.py:2089`, `install.py:2092`). The script is passed to the resolved conda Python (`install.py:2140`, `install.py:2146`, `install.py:2147`), so it is not exercisable as an in-process non-conda import.

- **`install.py:2127` — fallback present.** This is the installer’s fast-path import (`install.py:2125`, `install.py:2127`). On `ImportError`, control reaches the conda subprocess fallback (`install.py:2135`, `install.py:2138`, `install.py:2146`).

- **`install.py:2524` — fallback present.** `_pgvector_tables_present()` catches the missing current-interpreter import (`install.py:2522`, `install.py:2524`, `install.py:2536`) and falls back to the conda Python (`install.py:2539`, `install.py:2540`, `install.py:2557`, `install.py:2558`).

- **`install.py:2546` — fallback present; not a missing site.** This import is embedded in the subprocess program string (`install.py:2543`, `install.py:2546`) and that program is executed with the resolved conda interpreter (`install.py:2557`, `install.py:2558`).

- **`install.py:2647` — fallback present.** `_init_pgvector_schema()` catches the fast-path import failure (`install.py:2645`, `install.py:2647`, `install.py:2653`) and launches the DDL program with the conda Python (`install.py:2656`, `install.py:2657`, `install.py:2673`, `install.py:2674`).

- **`install.py:2666` — fallback present; not a missing site.** This import is part of the generated DDL subprocess source (`install.py:2664`, `install.py:2666`), executed by the resolved conda interpreter (`install.py:2673`, `install.py:2674`).

- **`scripts/migrate_to_pgvector.py:624` — missing fallback; exercisable when the migration CLI performs its database phase.** The import is performed directly in the running script and has no conda subprocess alternative (`scripts/migrate_to_pgvector.py:624`, `scripts/migrate_to_pgvector.py:626`). A repository grep found no other psycopg handling in `scripts/`, so this script itself contains no fallback. Running the migration with ordinary Python lacking psycopg therefore reaches an immediate `ImportError`.

- **`scripts/bench_recall.py:107` — missing fallback; conditionally exercisable.** The import occurs when `PgvectorBenchProvider` is constructed (`scripts/bench_recall.py:105`, `scripts/bench_recall.py:107`), immediately before connecting (`scripts/bench_recall.py:108`). It is reachable only when the benchmark selects that provider; otherwise the site is not executed. No conda fallback appears elsewhere in `scripts/`.

- **`tests/test_pgvector_integration.py:56` — missing fallback, but intentionally test-only and benign.** The import is a dependency probe that converts absence of psycopg into `unittest.SkipTest` (`tests/test_pgvector_integration.py:54`, `tests/test_pgvector_integration.py:56`, `tests/test_pgvector_integration.py:57`, `tests/test_pgvector_integration.py:58`). A psycopg-less non-conda pytest process does reach it, but the test is skipped rather than failed.

- **`tests/test_pgvector_integration.py:285` — missing fallback; test cleanup only.** This import is in `tearDownClass()` (`tests/test_pgvector_integration.py:282`, `tests/test_pgvector_integration.py:285`) and is not a production call path. It is only relevant after the pgvector integration test class has otherwise been admitted; it does not represent a hook/installer/runtime fallback gap.

- **Documentation-only occurrences:** `docs/pgvector-runbook.md:499` is an embedded example, and `docs/pgvector-runbook.md:734` is a shell command. Neither is an executable Python import site. The runbook explicitly treats the conda environment as the place where psycopg is expected to be installed (`docs/pgvector-runbook.md:733`, `docs/pgvector-runbook.md:734`).

- **Bottom line from verified current-tree evidence:** the production imports still lacking the fallback are `claude_hooks/providers/pgvector.py:123` and `claude_hooks/providers/pgvector.py:328`; the standalone CLI gaps are `scripts/migrate_to_pgvector.py:624` and `scripts/bench_recall.py:107`; and the test-only occurrences are `tests/test_pgvector_integration.py:56` and `tests/test_pgvector_integration.py:285`. The six `install.py` occurrences do **not** lack the `4e67dc2`-style fallback. Full provider-registry call-chain verification and direct inspection of commit `4e67dc2` remain open because Git and further filesystem reads were unavailable.

---

## Researcher (round 1)

- **Fallback pattern in `install.py`:** `_verify_pgvector_dsn` explicitly handles `ImportError` from the current interpreter, resolves the conda Python with `find_conda_env_python()`, and executes `_PGVECTOR_VERIFY_SCRIPT` through that interpreter (`install.py:2115`, `install.py:2127`, `install.py:2138`, `install.py:2146`). `_pgvector_tables_present` and `_init_pgvector_schema` implement the same guarded-fast-path/subprocess-fallback structure (`install.py:2515`, `install.py:2524`, `install.py:2539`, `install.py:2557`; `install.py:2631`, `install.py:2647`, `install.py:2656`, `install.py:2673`).

- **Generated import strings in `install.py` are not unguarded current-interpreter imports:**
  - `install.py:2092` is inside `_PGVECTOR_VERIFY_SCRIPT`; that script is run specifically with the resolved conda Python at `install.py:2146-2148`. Its own missing-`psycopg` handling is at `install.py:2093-2096`. **Fallback absent at the literal site: yes, but not a defect: this is already the conda fallback subprocess.**
  - `install.py:2546` is in the inline subprocess script used by `_pgvector_tables_present`; the subprocess command explicitly uses the conda Python (`install.py:2543-2559`). **Not independently exercisable from the non-conda installer interpreter.**
  - `install.py:2666` is in the inline subprocess script used by `_init_pgvector_schema`; its subprocess explicitly uses the conda Python (`install.py:2664-2675`). **Not independently exercisable from the non-conda installer interpreter.**

- **Actual installer fast-path imports are reachable from a non-conda Python, but their enclosing operations have the fallback:**
  - `install.py:2127`: path is `_verify_pgvector_dsn` → current-interpreter `import psycopg` → `ImportError` → conda subprocess → generated import at `install.py:2092` (`install.py:2127`, `install.py:2135`, `install.py:2146`). **Fallback present; not a remaining gap.**
  - `install.py:2524`: path is `_pgvector_tables_present` → current-interpreter import → `ImportError` → conda subprocess → generated import at `install.py:2546` (`install.py:2524`, `install.py:2536`, `install.py:2557`). **Fallback present.**
  - `install.py:2647`: path is `_init_pgvector_schema` → current-interpreter import → `ImportError` → conda subprocess → generated import at `install.py:2666` (`install.py:2647`, `install.py:2653`, `install.py:2673`). **Fallback present.**

- **Generated `pgvector-mcp` launcher pins an interpreter.** Installer chooses the resolved conda Python when it exists, otherwise `sys.executable`, and bakes that exact path into the launcher (`install.py:2294-2303`, `install.py:2354-2377`). Thus the normal generated launcher does **not** merely expose an unbound command: it selects conda Python if available. If conda is absent, however, it pins the installer’s current interpreter (`install.py:2296-2297`), which may lack `psycopg`. A separately installed console-script entry point can also run under whichever environment installed it; that path is not controlled by `_write_pgvector_launcher`.

- **`claude_hooks/providers/pgvector.py:123` still lacks a conda fallback.** `PgvectorProvider.verify` imports `psycopg` directly and returns `False` on `ImportError` (`claude_hooks/providers/pgvector.py:121-139`). The inspected installer call path is **not** reached from a non-conda interpreter lacking `psycopg`: `_verify_pgvector_dsn` first encounters its own missing import at `install.py:2127`, so it never calls `PgvectorProvider.verify` at `install.py:2128-2133`. The site is nevertheless directly exercisable by any caller invoking `PgvectorProvider.verify` under a non-conda interpreter. Other production callers were not inspected, so the complete real-world reachability remains **uncertain**.

- **`claude_hooks/providers/pgvector.py:328` still lacks a conda fallback.** The proven path is `PgvectorProvider.recall` → `_ensure_ready` (`claude_hooks/providers/pgvector.py:144-150`) → direct `import psycopg` at `claude_hooks/providers/pgvector.py:328`; missing `psycopg` is raised and then caught by `recall`, which returns no memories (`claude_hooks/providers/pgvector.py:147-151`). Therefore the site is **actually exercisable whenever a non-conda runtime invokes this provider**, although whether that provider is selected by the normal dispatcher was not established in this lane.

- **`scripts/migrate_to_pgvector.py:624` lacks the conda fallback.** It is a direct, unguarded `import psycopg` (`scripts/migrate_to_pgvector.py:624`). A direct invocation under a non-conda Python without `psycopg` will fail when execution reaches that import. The enclosing function, its CLI entry point, and the intended conda-only usage documentation were not successfully read before tool access ended, so the exact normal call path and whether the script is contractually conda-only remain **uncertain**.

- **`scripts/bench_recall.py:107` lacks the conda fallback.** The import occurs inside `PgvectorBenchProvider.__init__` (`scripts/bench_recall.py:105-108`). A direct invocation with a non-conda Python lacking `psycopg` will fail if that provider is instantiated. The script’s CLI/entry-point path, intended environment, and usage documentation were not successfully read, so its normal exercisability and conda-only intent remain **uncertain**.

- **Test-only sites:** `tests/test_pgvector_integration.py:56` intentionally probes for `psycopg` and skips when absent; a conda fallback would defeat that dependency check. `tests/test_pgvector_integration.py:285` is an unguarded teardown import, but the enclosing setup/teardown guards were not read, so whether a non-conda collection can reach it is **uncertain**. Neither test site is an appropriate candidate for production conda fallback.

- **Scope/caveat:** The current tree establishes the presence and shape of the installer fallbacks, but `git show 4e67dc2` was not inspected in this lane, so attribution of those exact hunks to that commit was not independently verified. The complete source-text inventory found was `install.py:2092`, `install.py:2127`, `install.py:2524`, `install.py:2546`, `install.py:2647`, `install.py:2666`, `claude_hooks/providers/pgvector.py:123`, `claude_hooks/providers/pgvector.py:328`, `tests/test_pgvector_integration.py:56`, `tests/test_pgvector_integration.py:285`, `scripts/migrate_to_pgvector.py:624`, and `scripts/bench_recall.py:107`.

---

## Synthesizer

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

---
