# Consultation transcript — csl-2026-10-22-1636-4490

_Question_: Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python.


## Planner

1. Enumerate every executable `import psycopg`/`from psycopg` site under `claude_hooks/`, excluding comments and generated `.claude-hooks/` data, and compare it with the known sites at `claude_hooks/providers/pgvector.py:123` and `claude_hooks/providers/pgvector.py:328`.
2. Inspect the full control flow around `PgvectorProvider.verify` at `claude_hooks/providers/pgvector.py:121-140` and determine whether the missing-psycopg branch at line 123 has any caller that retries through a conda-env subprocess.
3. Inspect `PgvectorProvider` connection initialization around `claude_hooks/providers/pgvector.py:326-340` and trace its public callers to establish whether the missing-psycopg branch at line 328 can execute when Python runs outside the `claude-hooks` conda environment.
4. Check the `4e67dc2` commit diff or its resulting implementation in `install.py:2116-2155` to identify the exact fallback contract—conda interpreter discovery, subprocess invocation, and output handling—then determine whether either `claude_hooks/providers/pgvector.py` site has an equivalent path.
5. Search `install.py:2090-2100`, `install.py:2522-2555`, and `install.py:2645-2672` to distinguish intentionally separate install-time psycopg imports that already have fallback behavior from actual uncovered import sites, and record their exact line numbers.
6. Trace configuration and provider registration in `claude_hooks/providers/__init__.py:1-20` and all references to `PgvectorProvider.verify`/provider construction, checking whether pgvector is disabled by default and whether the affected paths are actually exercisable from a non-conda-env Python rather than merely syntactically present.
7. Audit `tests/test_pgvector_integration.py`, `tests/test_install_pgvector_verify.py`, and any pgvector-related provider tests for coverage of the two `claude_hooks/providers/pgvector.py` branches under `sys.modules["psycopg"] = None`, distinguishing executable-from-system-Python behavior from tests skipped when psycopg is absent.

---

## Researcher (round 1)

- **Complete `claude_hooks/` inventory:** exactly two executable source-level `psycopg` imports exist; no `from psycopg ...` imports were found. Both are in `claude_hooks/providers/pgvector.py`; neither has a subprocess fallback.

- **`claude_hooks/providers/pgvector.py:123` — `PgvectorProvider.verify()`:** imports `psycopg` inside the method; `ImportError` logs `"psycopg not installed — cannot verify pgvector"` and returns `False` (`claude_hooks/providers/pgvector.py:121-126`). **Exercisable from non-conda Python: yes**, whenever this public classmethod is called under an interpreter lacking `psycopg`. However, the installer’s fallback does not reach this branch: it first imports `psycopg` in the caller, and only calls `PgvectorProvider.verify()` after that import succeeds (`install.py:2125-2135`). The generic installer verification loop also explicitly skips `pgvector` (`install.py:3540-3548`). Thus the branch is directly exercisable, but no normal installer retry path uses it as its fallback.

- **`claude_hooks/providers/pgvector.py:328` — `PgvectorProvider._ensure_ready()`:** lazily imports `psycopg`, converts `ImportError` into `"install psycopg: pip install psycopg[binary]"`, and otherwise opens the DSN directly with `psycopg.connect()` (`claude_hooks/providers/pgvector.py:320-334`). **Exercisable from non-conda Python: yes.** It is reached from provider operations including recall, store, and the KG extension methods (`claude_hooks/providers/pgvector.py:148`, `claude_hooks/providers/pgvector.py:214`, `claude_hooks/providers/pgvector.py:241`, `claude_hooks/providers/pgvector.py:265`, `claude_hooks/providers/pgvector.py:292`, `claude_hooks/providers/pgvector.py:439`, `claude_hooks/providers/pgvector.py:555`, `claude_hooks/providers/pgvector.py:592`, `claude_hooks/providers/pgvector.py:645`, `claude_hooks/providers/pgvector.py:676`). Recall converts the failure into an empty result (`claude_hooks/providers/pgvector.py:147-151`); there is no conda-subprocess retry.

- **Runtime reachability is real:** the pgvector MCP server constructs and uses `PgvectorProvider` (`claude_hooks/pgvector_mcp/server.py:344`), while the server documentation states that connection management occurs through the provider’s `_ensure_ready` lifecycle (`claude_hooks/pgvector_mcp/server.py:17`). The generated pgvector launcher prefers the `claude-hooks` conda interpreter when present but otherwise uses the current interpreter (`install.py:2296-2297`), and the general hook resolver likewise falls back to system `python3`/`python` when no conda interpreter is found (`bin/_resolve_python.sh:53-58`). Therefore a non-conda interpreter can genuinely reach the provider import.

- **Fallback contract from `4e67dc2`:** `_verify_pgvector_dsn()` first tries importing `psycopg` and calling `PgvectorProvider.verify()` in the current interpreter (`install.py:2125-2135`). On `ImportError`, it discovers the conda interpreter with `find_conda_env_python()` and fails explicitly if that path does not exist (`install.py:2138-2143`). It then invokes `subprocess.run([conda_python, "-c", script, dsn], capture_output=True, text=True, timeout=15)` (`install.py:2145-2149`) and parses the last stdout line as JSON, reporting empty or non-JSON output as an error (`install.py:2153-2160`). Neither provider site implements any part of that discovery/invocation/output contract.

- **Other project-level direct imports:** `scripts/migrate_to_pgvector.py:624` imports `psycopg` without a fallback and is therefore genuinely exercisable—and fatal—under a non-conda interpreter lacking the package (`scripts/migrate_to_pgvector.py:624-626`). `scripts/bench_recall.py:107` is likewise directly exercisable in `PgvectorBenchProvider.__init__()` and has no fallback (`scripts/bench_recall.py:105-108`).

- **Install-time imports are protected separately:** the direct import at `install.py:2127` is guarded by the subprocess fallback above; the direct imports at `install.py:2524` and `install.py:2647` likewise fall back to conda subprocesses on `ImportError` (`install.py:2536-2559`, `install.py:2653-2675`). The textual imports at `install.py:2092`, `install.py:2546`, and `install.py:2666` belong to subprocess probe scripts, not the parent process’s import path.

---

## Researcher (round 1)

## Import-site classification

- `install.py:2092` — embedded child-process script, not a current-interpreter import. It is executed only by the conda fallback at `install.py:2146-2149`; therefore it already has the intended fallback behavior.
- `install.py:2127` — current-interpreter fast path. `ImportError` is caught at `install.py:2135-2136`, then the conda Python is used at `install.py:2138-2149`. **Covered.**
- `install.py:2524` — current-interpreter table-probe fast path. Its `ImportError` falls through to the conda subprocess at `install.py:2536-2559`. **Covered.**
- `install.py:2546` — import inside the subprocess script generated for the table probe; it is not a separate uncovered import site. The subprocess is launched at `install.py:2556-2559`. **Covered.**
- `install.py:2647` — current-interpreter schema-init fast path. Its `ImportError` falls through to the conda subprocess at `install.py:2653-2675`. **Covered.**
- `install.py:2666` — import inside that subprocess script, which is launched at `install.py:2673-2675`. **Covered.**

## Actual remaining gaps

- `claude_hooks/providers/pgvector.py:123` — `PgvectorProvider.verify` catches missing `psycopg` and returns `False`; it has no conda subprocess fallback itself (`claude_hooks/providers/pgvector.py:121-126`). **Exercisable from non-conda Python:** yes, if `PgvectorProvider.verify` is called directly. However, the installer’s main DSN-verification path is protected: it catches the missing import and shells out to conda at `install.py:2127-2149`. Thus this is an uncovered provider-level branch, not an uncovered installer verification path.
- `claude_hooks/providers/pgvector.py:328` — `_ensure_ready` catches missing `psycopg` only to raise `ImportError` (`claude_hooks/providers/pgvector.py:326-334`); it has no conda fallback. **Exercisable from non-conda Python:** yes. Pgvector is disabled by default (`claude_hooks/config.py:48-50`), but once explicitly enabled with a DSN, the dispatcher constructs it (`claude_hooks/dispatcher.py:129-148`), and provider recall/store operations enter `_ensure_ready` (`claude_hooks/providers/pgvector.py:144-158`, `claude_hooks/providers/pgvector.py:210-217`). A direct system-Python invocation of the provider or pgvector MCP therefore fails at this import.
- `scripts/migrate_to_pgvector.py:624` — unconditional import in `main`, followed by a direct connection (`scripts/migrate_to_pgvector.py:610-626`). **Exercisable from non-conda Python:** yes, by running this operator script with system Python; it raises `ImportError` rather than delegating to the conda environment.
- `scripts/bench_recall.py:107` — import in `PgvectorBenchProvider.__init__`, immediately followed by `psycopg.connect` (`scripts/bench_recall.py:105-108`). **Exercisable from non-conda Python:** yes, whenever the pgvector benchmark provider is selected; there is no fallback.

## Registration and test findings

- The provider registry is lazy: pgvector is registered as a module path at `claude_hooks/providers/__init__.py:30-34`, and the module is imported only on demand at `claude_hooks/providers/__init__.py:43-53`. Consequently, merely importing the registry does not execute either provider import site.
- `tests/test_pgvector_integration.py:56` is a dependency probe, not a fallback implementation. Missing psycopg skips the entire integration class via `setUpClass` at `tests/test_pgvector_integration.py:54-58`, `tests/test_pgvector_integration.py:92-95`; therefore system Python without psycopg does **not** exercise the real provider’s missing-dependency branches.
- The integration test exercises real `PgvectorProvider.verify` only with psycopg and a live pgvector installation (`tests/test_pgvector_integration.py:175-186`); its setup already requires a reachable database and extension (`tests/test_pgvector_integration.py:60-70`).
- `tests/test_pgvector_integration.py:285` is cleanup-only, inside a broad `try/except` that silently passes on failure (`tests/test_pgvector_integration.py:281-294`); it is neither a fallback nor behavioral coverage.
- `tests/test_install_pgvector_verify.py:64-85` verifies the installer’s missing-psycopg conda fallback, while `tests/test_install_pgvector_verify.py:149-202` and `tests/test_install_pgvector_verify.py:207-244` cover the table-probe and schema-init fallbacks. These tests inject `sys.modules["psycopg"] = None` but mock the provider in the fast path (`tests/test_install_pgvector_verify.py:34-60`), so they do **not** cover `claude_hooks/providers/pgvector.py:123` or `claude_hooks/providers/pgvector.py:328`.
- The pgvector MCP unit suite uses a `FakePgvectorProvider` rather than the real provider (`tests/test_pgvector_mcp.py:19-27`), so it supplies no missing-psycopg coverage for those two branches.

---

## Synthesizer

Bottom line: under `claude_hooks/`, exactly two executable `psycopg` import sites still lack the conda-subprocess fallback, and both are exercisable from a non-conda Python.

- `claude_hooks/providers/pgvector.py:123` — **Yes, directly exercisable.** `PgvectorProvider.verify()` catches `ImportError`, logs that psycopg is unavailable, and returns `False`; it never attempts the conda interpreter (`claude_hooks/providers/pgvector.py:121`). The installer’s normal DSN-verification path is already protected: it imports psycopg first, catches `ImportError`, discovers the conda Python, and retries in a subprocess (`install.py:2125`, `install.py:2135`, `install.py:2138`). The generic installer verification loop also skips pgvector (`install.py:3540`, `install.py:3547`). Thus this is a genuine provider-level gap, but not a hole in the installer fallback.

- `claude_hooks/providers/pgvector.py:328` — **Yes, directly exercisable, with configuration caveat.** `_ensure_ready()` catches a missing psycopg and raises a new `ImportError`; it performs no conda discovery or subprocess retry (`claude_hooks/providers/pgvector.py:326`, `claude_hooks/providers/pgvector.py:329`). Pgvector is disabled by default (`claude_hooks/config.py:48`), but after it is enabled and given a DSN, the dispatcher constructs the provider (`claude_hooks/dispatcher.py:129`, `claude_hooks/dispatcher.py:140`), and provider operations such as recall enter `_ensure_ready()` (`claude_hooks/providers/pgvector.py:148`). Recall converts the missing dependency into an empty result rather than retrying through conda (`claude_hooks/providers/pgvector.py:147`). The launcher also uses the current interpreter when no conda interpreter is available (`install.py:2296`), so a system-Python invocation is a real execution path rather than merely theoretical.

The reference fallback contract exists only in `install.py`: find `find_conda_env_python()`, invoke it with `subprocess.run`, capture output with a timeout, and parse the final stdout line as JSON (`install.py:2138`, `install.py:2145`, `install.py:2153`). Neither provider import site implements an equivalent path.

The `install.py` psycopg hits are not additional uncovered sites: the current-interpreter imports at `install.py:2127`, `install.py:2524`, and `install.py:2647` fall back to conda subprocesses, while `install.py:2092`, `install.py:2546`, and `install.py:2666` are imports inside those child-process scripts. Existing installer tests cover the subprocess fallback but mock the provider, so they do not cover either provider branch (`tests/test_install_pgvector_verify.py:34`, `tests/test_install_pgvector_verify.py:64`).

---
