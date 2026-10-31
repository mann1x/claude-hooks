---
session_id: csl-2026-10-22-1720-75d8
created: "2026-10-22T17:20:14"
question: "Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python."
models:
  planner: "minimax-m3.1-flash:cloud"
  researcher: "minimax-m3.1-flash:cloud"
  synthesizer: "minimax-m3.1-flash:cloud"
topology: council
effort: medium
duration_seconds: 217.63
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-10-22/tmp/claude-hooks-bench-bench-baseline-2026-05-07-Rwwx
---

Two deficient `psycopg` imports exist under `claude_hooks/`: `claude_hooks/providers/pgvector.py:328` is a real runtime gap; `claude_hooks/providers/pgvector.py:123` is reachable only through a direct `verify()` call because the installer normally substitutes the conda subprocess.

- **`claude_hooks/providers/pgvector.py:123` — `PgvectorProvider.verify()`**
  - **Fallback status:** Deficient. A missing `psycopg` is caught locally and converted to `False`; there is no conda subprocess from this method (`claude_hooks/providers/pgvector.py:123`, `claude_hooks/providers/pgvector.py:124`, `claude_hooks/providers/pgvector.py:125`).
  - **Exercisable from a non-conda Python?** **Yes, but only by directly calling `PgvectorProvider.verify(...)`; no through the normal installer path.** The integration test directly calls it at `tests/test_pgvector_integration.py:186`, although the test class skips when `psycopg` is absent (`tests/test_pgvector_integration.py:56`, `tests/test_pgvector_integration.py:58`, `tests/test_pgvector_integration.py:95`).
  - **Why the installer does not expose the gap:** `_verify_pgvector_dsn()` first imports `psycopg` in the current interpreter and only then calls `PgvectorProvider.verify()` (`install.py:2125`, `install.py:2127`, `install.py:2132`). An `ImportError` exits that fast path and invokes the conda-Python subprocess instead (`install.py:2135`, `install.py:2138`, `install.py:2140`, `install.py:2146`). The generic provider-verification loop also skips pgvector (`install.py:3540`, `install.py:3547`, `install.py:3566`).
  - **Assessment:** A real import-site gap, but operationally shielded in the installer; only a direct API caller from a psycopg-less interpreter reaches it.

- **`claude_hooks/providers/pgvector.py:328` — `PgvectorProvider._ensure_ready()`**
  - **Fallback status:** Deficient and operationally significant. A missing `psycopg` becomes `ImportError("install psycopg: ...")`; there is no conda subprocess (`claude_hooks/providers/pgvector.py:328`, `claude_hooks/providers/pgvector.py:329`, `claude_hooks/providers/pgvector.py:330`).
  - **Exercisable from a non-conda Python?** **Yes.** It is reached by normal provider operations: `recall()` at `claude_hooks/providers/pgvector.py:148`, `store()` at `claude_hooks/providers/pgvector.py:214`, `batch_recall()` at `claude_hooks/providers/pgvector.py:265`, and `batch_store()` at `claude_hooks/providers/pgvector.py:292`. Runtime code invokes provider recall/store operations, for example `claude_hooks/recall.py:86` and `claude_hooks/store_async.py:172`; the MCP server does likewise at `claude_hooks/pgvector_mcp/server.py:271` and `claude_hooks/pgvector_mcp/server.py:280`.
  - **Why a non-conda interpreter can select it:** The shared resolver accepts an explicit interpreter override (`bin/_resolve_python.sh:25`), prefers a repo `.venv` over conda (`bin/_resolve_python.sh:31`, `bin/_resolve_python.sh:32`), and otherwise falls back to system `python3`/`python` when no candidate is found (`bin/_resolve_python.sh:53`, `bin/_resolve_python.sh:54`, `bin/_resolve_python.sh:56`). The hook executes under that selected interpreter (`bin/claude-hook:18`, `bin/claude-hook:23`). The checked-in MCP shim uses the same resolver (`bin/claude-hook-pgvector-mcp:10`, `bin/claude-hook-pgvector-mcp:17`).
  - **Generated launcher exception:** The installed launcher is conda-pinned only when the conda interpreter exists; otherwise it bakes in the installer’s `sys.executable` (`install.py:2296`, `install.py:2297`, `install.py:2303`) and executes it directly (`install.py:2375`). Thus it can also be a non-conda interpreter when conda is absent.
  - **Observed failure mode:** Recall catches the import failure and returns no results (`claude_hooks/providers/pgvector.py:149`, `claude_hooks/providers/pgvector.py:151`); store converts it to a runtime failure (`claude_hooks/providers/pgvector.py:216`, `claude_hooks/providers/pgvector.py:217`). Batch recall and batch store behave analogously (`claude_hooks/providers/pgvector.py:266`, `claude_hooks/providers/pgvector.py:268`, `claude_hooks/providers/pgvector.py:293`, `claude_hooks/providers/pgvector.py:294`).
  - **Assessment:** **Genuine, normally reachable runtime gap.**

Other repository-wide `psycopg` imports do not alter that package-level result:

- If “claude-hooks” means the entire repository, two additional production-script gaps are exercisable from a non-conda interpreter: `scripts/migrate_to_pgvector.py:624` and `scripts/bench_recall.py:107`. Both import and use `psycopg` directly without a fallback (`scripts/migrate_to_pgvector.py:626`, `scripts/bench_recall.py:108`).
- Installer imports at `install.py:2127`, `install.py:2524`, and `install.py:2647` are guarded fast paths with conda subprocess fallbacks. The embedded-script imports at `install.py:2092`, `install.py:2546`, and `install.py:2666` execute in those subprocesses.
- Test imports at `tests/test_pgvector_integration.py:56` and `tests/test_pgvector_integration.py:285` are test dependency/cleanup code, not production runtime fallbacks.
