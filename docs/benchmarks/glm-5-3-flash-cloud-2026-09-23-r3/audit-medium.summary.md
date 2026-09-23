---
session_id: csl-2026-09-23-2042-4194
created: "2026-09-23T20:42:23"
question: "Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python."
models:
  planner: "glm-5.3-flash:cloud"
  researcher: "glm-5.3-flash:cloud"
  synthesizer: "glm-5.3-flash:cloud"
topology: council
effort: medium
duration_seconds: 65.16
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-09-23/tmp/claude-hooks-bench-bench-baseline-2026-05-07-m9iJ
---

**Bottom line: 4 psycopg import sites lack the conda-env subprocess fallback added in 4e67dc2 — 2 in the package (`claude_hooks/providers/pgvector.py:123` and `:328`, both graceful-by-design), 2 in operator scripts (`scripts/migrate_to_pgvector.py:624`, `scripts/bench_recall.py:107`, hard-crash but runbook-documented as conda-env tooling). All three fixed fallback sites live in `install.py`; none exist anywhere else. Note: the commit hash `4e67dc2` appears nowhere in tracked source (only in `.claude-hooks/consultants/sessions.json:16 [unverified — file not found]`), and there's no CHANGELOG entry for it — the change is characterized from code only.**

**Fallback sites (fixed, for reference)**
- `install.py:2115-2161` `_verify_pgvector_dsn` — fast path `import psycopg` at `install.py:2127`, fallback resolves `find_conda_env_python()` at `install.py:2140` and runs `_PGVECTOR_VERIFY_SCRIPT` via subprocess (`install.py:2146-2148`).
- `install.py:2524` + `install.py:2540` `_pgvector_tables_present` — inline table-probe subprocess fallback.
- `install.py:2647` + `install.py:2657` `_init_pgvector_schema` — DDL piped to conda python via stdin (`install.py:2666-2676`); raises `RuntimeError` if env missing (`install.py:2658-2663`).
- `install.py:2092` is an embedded probe-string import (`_PGVECTOR_VERIFY_SCRIPT`, raw string `install.py:2089-2112`) — executes only inside the subprocess, not a live site.
- No dynamic (`importlib`) psycopg imports anywhere; grep confirmed `find_conda_env_python` has zero hits under `claude_hooks/`.

**Unfixed sites, with exercisability**

- **`claude_hooks/providers/pgvector.py:123`** (`PgvectorProvider.verify`) — bare import; on `ImportError` logs a warning and returns `False` (`pgvector.py:124-126`). No fallback.
  - Exercisable from non-conda Python: yes. Non-conda execution is a supported path — `bin/claude-hook:16-23` resolves repo `.venv` → conda → system python, and `pyproject.toml [project.scripts]` entry points run under whichever env installed them.
  - Caller 1: the installer fast path itself (`install.py:2132`). **Semantic gap worth flagging**: `verify()` swallows the `ImportError` internally and returns `False`, so `_verify_pgvector_dsn`'s fast path reports "verify failed" from a non-conda interpreter *without ever reaching the subprocess fallback at `install.py:2135`* — the fallback only fires on its own `import psycopg` failing, not on `verify()` returning False. A reachable-but-unverifiable DB yields a false negative.
  - Caller 2: `install.py:3556` (`cls.verify(candidate)` in a generic provider-verify loop). Researcher flagged this as **unverified** — the enclosing function wasn't read, so it's uncertain whether pgvector candidates reach that loop or are always routed through `_verify_pgvector_dsn`. Treat as probable but unconfirmed.
  - `tests/test_pgvector_integration.py:186 [no _verify_pgvector_dsn at this line; line is in test_07_verify_works]` also hits it, but tests run under the conda env (`CHANGELOG.md:361`) — not exercisable from system python in practice.

- **`claude_hooks/providers/pgvector.py:328`** (`_ensure_ready`) — bare import; re-raises `ImportError("install psycopg: pip install psycopg[binary]")` (`pgvector.py:329-330`). No fallback.
  - Exercisable: yes, via `claude_hooks/pgvector_mcp/server.py:37` (the `pgvector-mcp` console script / `claude-hook-pgvector-mcp` shim) — if psycopg is missing there, recall/store fail for the process lifetime.
  - Hook path: `recall()` catches `(ImportError, EmbedderError)` at `pgvector.py:147-150` and degrades to an empty recall — exercisable only as silent capability loss, not a crash, when `bin/claude-hook`'s resolution chain lands on venv/system python.

- **`scripts/migrate_to_pgvector.py:624`** — bare import in `main()`; hard crash from non-conda Python. **Partially exempt**: `docs/pgvector-runbook.md:733-737` documents pre-flight as running via the conda env's python explicitly, so it's conda-env-documented operator tooling.

- **`scripts/bench_recall.py:107`** — bare import in `PgvectorBenchProvider.__init__`; same classification — benchmark tooling, implicitly conda-env-only per the runbook.

**Exempt (guarded)**
- `tests/test_pgvector_integration.py:54-58` — `_skip_if_no_deps()` raises `SkipTest` on ImportError; `:285-294` tearDownClass wraps its import in `try/except Exception: pass`, safe even when the class skips.
- `tests/test_install_pgvector_verify.py` — all psycopg via `sys.modules` mocks and patched `install.find_conda_env_python` — no real psycopg needed.

**Environment cross-check (deployment, not code)**
`install.py:66` hardcodes `CONDA_ENV_NAME = "claude-hooks"`; `find_conda_env_python` (`install.py:101`) probes standard layouts (`install.py:131-149`) then `conda env list --json` (`install.py:160-173`). On the host whose env is named `unsloth`, both fail and the function returns a nonexistent canonical path (`install.py:177-190`); every fallback caller handles this (`install.py:2141-2143` returns False, `install.py:2541-2542` returns False, `install.py:2658-2663` raises). The fallback code is correct there but **silently inert** — remediation is `conda create -n claude-hooks` (no env-var override exists at `install.py:66`), not a code change.

**Verdict**: the only genuinely unguarded, non-exempt import sites are the two in `claude_hooks/providers/pgvector.py`. The `:123` site is the one to prioritize fixing — it silently defeats the installer's own fallback via the swallow-and-return-False path through `install.py:2132`.
