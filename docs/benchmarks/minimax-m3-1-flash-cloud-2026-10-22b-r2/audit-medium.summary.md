---
session_id: csl-2026-10-22-1636-4490
created: "2026-10-22T16:36:14"
question: "Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python."
models:
  planner: "minimax-m3.1-flash:cloud"
  researcher: "minimax-m3.1-flash:cloud"
  synthesizer: "minimax-m3.1-flash:cloud"
topology: council
effort: medium
duration_seconds: 117.18
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-10-22/tmp/claude-hooks-bench-bench-baseline-2026-05-07-NShH
---

Bottom line: under `claude_hooks/`, exactly two executable `psycopg` import sites still lack the conda-subprocess fallback, and both are exercisable from a non-conda Python.

- `claude_hooks/providers/pgvector.py:123` — **Yes, directly exercisable.** `PgvectorProvider.verify()` catches `ImportError`, logs that psycopg is unavailable, and returns `False`; it never attempts the conda interpreter (`claude_hooks/providers/pgvector.py:121`). The installer’s normal DSN-verification path is already protected: it imports psycopg first, catches `ImportError`, discovers the conda Python, and retries in a subprocess (`install.py:2125`, `install.py:2135`, `install.py:2138`). The generic installer verification loop also skips pgvector (`install.py:3540`, `install.py:3547`). Thus this is a genuine provider-level gap, but not a hole in the installer fallback.

- `claude_hooks/providers/pgvector.py:328` — **Yes, directly exercisable, with configuration caveat.** `_ensure_ready()` catches a missing psycopg and raises a new `ImportError`; it performs no conda discovery or subprocess retry (`claude_hooks/providers/pgvector.py:326`, `claude_hooks/providers/pgvector.py:329`). Pgvector is disabled by default (`claude_hooks/config.py:48`), but after it is enabled and given a DSN, the dispatcher constructs the provider (`claude_hooks/dispatcher.py:129`, `claude_hooks/dispatcher.py:140`), and provider operations such as recall enter `_ensure_ready()` (`claude_hooks/providers/pgvector.py:148`). Recall converts the missing dependency into an empty result rather than retrying through conda (`claude_hooks/providers/pgvector.py:147`). The launcher also uses the current interpreter when no conda interpreter is available (`install.py:2296`), so a system-Python invocation is a real execution path rather than merely theoretical.

The reference fallback contract exists only in `install.py`: find `find_conda_env_python()`, invoke it with `subprocess.run`, capture output with a timeout, and parse the final stdout line as JSON (`install.py:2138`, `install.py:2145`, `install.py:2153`). Neither provider import site implements an equivalent path.

The `install.py` psycopg hits are not additional uncovered sites: the current-interpreter imports at `install.py:2127`, `install.py:2524`, and `install.py:2647` fall back to conda subprocesses, while `install.py:2092`, `install.py:2546`, and `install.py:2666` are imports inside those child-process scripts. Existing installer tests cover the subprocess fallback but mock the provider, so they do not cover either provider branch (`tests/test_install_pgvector_verify.py:34`, `tests/test_install_pgvector_verify.py:64`).
