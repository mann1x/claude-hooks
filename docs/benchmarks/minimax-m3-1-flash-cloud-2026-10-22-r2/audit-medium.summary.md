---
session_id: csl-2026-10-22-0813-fdb4
created: "2026-10-22T08:13:56"
question: "Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python."
models:
  planner: "minimax-m3.1-flash:cloud"
  researcher: "minimax-m3.1-flash:cloud"
  synthesizer: "minimax-m3.1-flash:cloud"
topology: council
effort: medium
duration_seconds: 127.05
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-10-22/tmp/claude-hooks-bench-bench-baseline-2026-05-07-7fei
---

- `claude_hooks/providers/pgvector.py:123` — **Missing the conda-environment subprocess fallback; not reachable through the normal installer verification path when the current interpreter lacks `psycopg`, but still exercisable through a direct/manual provider call.** `PgvectorProvider.verify()` imports `psycopg` locally, catches `ImportError`, and returns `False` without invoking a conda subprocess (`claude_hooks/providers/pgvector.py:121`, `claude_hooks/providers/pgvector.py:123`, `claude_hooks/providers/pgvector.py:124`). The installer first tests `psycopg` in its own interpreter; only after that import fails does it locate the conda Python and run the verification script via `subprocess.run` (`install.py:2125`, `install.py:2127`, `install.py:2135`, `install.py:2138`, `install.py:2140`, `install.py:2146`). Thus, a non-conda Python without `psycopg` does **not** reach line 123 through the confirmed installer path; a direct call to `PgvectorProvider.verify()` still does, and merely returns `False` (`claude_hooks/providers/pgvector.py:72 [no verify at this line; line is in PgvectorProvider]`, `claude_hooks/providers/pgvector.py:121`).

- `claude_hooks/providers/pgvector.py:328` — **Missing the conda-environment subprocess fallback; genuinely exercisable from a non-conda Python.** `_ensure_ready()` imports `psycopg` directly and converts `ImportError` into a new installation-oriented `ImportError`; it does not delegate to the conda environment (`claude_hooks/providers/pgvector.py:320`, `claude_hooks/providers/pgvector.py:328`, `claude_hooks/providers/pgvector.py:329`, `claude_hooks/providers/pgvector.py:330`). It is reached by ordinary provider operations including recall (`claude_hooks/providers/pgvector.py:144`, `claude_hooks/providers/pgvector.py:148`). Non-conda execution is supported by the shared resolver, which prefers a repository venv, then the `claude-hooks` conda environment, and finally system `python3`/`python` (`bin/_resolve_python.sh:3`, `bin/_resolve_python.sh:12`, `bin/_resolve_python.sh:14`, `bin/_resolve_python.sh:53`). Therefore, a system Python or non-conda venv without `psycopg` can reach line 328 when the provider is enabled and used; provider construction is guarded by configuration/DSN rather than by `psycopg` availability (`claude_hooks/dispatcher.py:129 [no psycopg at this line; line is in build_providers]`, `claude_hooks/dispatcher.py:131 [no psycopg at this line; line is in build_providers]`, `claude_hooks/dispatcher.py:136`).

- **Inventory result:** under `claude_hooks/`, these are the only direct `import psycopg` sites; no `psycopg2` or alternate indirect psycopg-loading site was found. The pgvector MCP package imports the provider module, not `psycopg` directly (`claude_hooks/pgvector_mcp/server.py:37 [no psycopg at this line; module scope]`).

- **Reference behavior and test coverage:** the installer’s missing-`psycopg` subprocess fallback has explicit tests (`tests/test_install_pgvector_verify.py:63 [no psycopg at this line; line is in TestSubprocessFallback]`, `tests/test_install_pgvector_verify.py:70`, `tests/test_install_pgvector_verify.py:83`). No equivalent provider-level test was confirmed for either provider import site. The pgvector integration test skips when `psycopg` is absent rather than exercising the provider fallback (`tests/test_pgvector_integration.py:54 [no psycopg at this line; line is in _skip_if_no_deps]`, `tests/test_pgvector_integration.py:56`, `tests/test_pgvector_integration.py:57`).

- **Scope note:** two additional production imports exist outside `claude_hooks/`, at `scripts/migrate_to_pgvector.py:624` and `scripts/bench_recall.py:107`; they are outside the requested `claude_hooks/` inventory. Their surrounding fallback logic was not fully classified.

- **Unfinished verification:** commit `4e67dc2` itself was not inspected, so the comparison against that historical commit is based on the current installer implementation and its documented subprocess behavior (`install.py:2118`, `install.py:2140`), not a directly verified commit diff. The Windows scheduled-task interpreter-selection path was also not confirmed. The audit did not run a live non-conda Python against either provider path; the reachability conclusions are based on the import/call-path inspection and existing test evidence.
