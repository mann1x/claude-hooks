# Consultation transcript — csl-2026-09-23-2042-4194

_Question_: Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python.


## Planner

1. Characterize the reference fallback pattern in `install.py` — read `install.py:2118-2160` (`_verify_pgvector_dsn`), `install.py:2515-2561`, `install.py:2631-2674` (both "falls back to conda env subprocess") and confirm `install.py:2092` is an embedded probe-string import, not a live one; also grep `CHANGELOG.md`/`docs/pgvector-runbook.md` for what 4e67dc2 actually added, since the hash itself appears nowhere in-repo (grep found it only in `.claude-hooks/consultants/sessions.json:16`).
2. Establish the interpreter contract: read `bin/claude-hook.cmd`, `bin/claude-hooks-daemon-ctl.cmd`, and `systemd/claude-hooks-daemon.service` ExecStart to verify the hook normally runs under the conda env python resolved by `find_conda_env_python` (`install.py:101-196`) — this decides whether non-conda execution is realistic at all.
3. Audit the two psycopg import sites inside the `claude_hooks/` package — `claude_hooks/providers/pgvector.py:123` (`PgvectorProvider.verify`, try/except → returns False) and `claude_hooks/providers/pgvector.py:328` (`_ensure_ready`, try/except → raises ImportError) — and confirm neither shells out to `find_conda_env_python()` the way `install.py:2140/2540/2657` do.
4. Determine exercisability of each site by tracing callers: grep for `verify(` on `ServerCandidate`/pgvector in `claude_hooks/` (who invokes `pgvector.py:121`) and for what instantiates `PgvectorProvider`/triggers `_ensure_ready` (`pgvector.py:320-336`), then check whether those callers run only under `bin/claude-hook` (conda python) or also via bare `python -m`/system python entry points (`run.py`, `pyproject.toml` scripts).
5. Sweep for stragglers: run a project-wide grep for `import psycopg` / `psycopg.connect` excluding already-known sites to catch dynamic imports (importlib, string-embedded code) in `claude_hooks/`, `scripts/`, `consultants/`, and `vendor/`.
6. Classify the out-of-package sites: `scripts/migrate_to_pgvector.py:624` and `scripts/bench_recall.py:107` (are these documented as conda-env-only in `docs/pgvector-runbook.md:733-737`?) and `tests/test_pgvector_integration.py:56,285` (verify the `SkipTest` guard makes them unreachable without psycopg) — decide whether they're in audit scope or exempt.
7. Cross-check the environment assumption: memory says one deployment host's conda env is named `unsloth`, not `claude-hooks` — verify whether `find_conda_env_python(env_name="claude-hooks")` (`install.py:66`) would fail on that host, which would make even the "fixed" fallback sites non-exercisable there; flag this as environment-dependent rather than a code defect.

---

## Researcher (round 1)

**Lane 1 — reference fallback pattern (install.py)**
- The hash `4e67dc2` appears nowhere in tracked source — only in the consultant session record `.claude-hooks/consultants/sessions.json:16`. CHANGELOG.md has no psycopg/conda-fallback entry (grep hits at `CHANGELOG.md:309` and `:361` are unrelated); `docs/pgvector-runbook.md:733-737` only says "psycopg already in conda env / pip install psycopg[binary]" with no mention of a subprocess fallback. So the commit is characterized by code only, and is undocumented.
- Reference pattern #1: `install.py:2115-2161` (`_verify_pgvector_dsn`) — fast path `import psycopg` at `install.py:2127`, on `ImportError` resolves `find_conda_env_python()` at `install.py:2140` and runs `_PGVECTOR_VERIFY_SCRIPT` via `subprocess.run` at `install.py:2146-2149`, 15s timeout. Docstring `install.py:2118-2123` explicitly states install.py is often run with system py3 without psycopg.
- Reference pattern #2: `install.py:2522-2569` — fast path at `install.py:2524`, fallback `conda_py = find_conda_env_python()` at `install.py:2540`, inline table-probe script run at `install.py:2557-2560`.
- Reference pattern #3: `install.py:2645-2676` — fast path `import psycopg` at `install.py:2647`, fallback at `install.py:2657`, DDL piped via stdin to conda python at `install.py:2673-2676`; raises `RuntimeError` if conda env missing (`install.py:2658-2663`).
- `install.py:2092` confirmed as embedded probe-string import: `_PGVECTOR_VERIFY_SCRIPT` is a raw string literal opened at `install.py:2089` and closed at `install.py:2112`; its `import psycopg` executes only inside the subprocess (conda python), never in install.py's own interpreter.

**Lane 2 — interpreter contract**
- POSIX shim `bin/claude-hook:16-23`: sources `_resolve_python.sh`, which per the comment probes "repo-local .venv, then conda env, then system python" — so **system-python execution is an explicit, supported fallback**, not hypothetical.
- Windows shim `bin/claude-hook.cmd:16-30`: honors `CLAUDE_HOOKS_PY` override first, then `.venv\bin\python.exe` / `.venv\Scripts\python.exe` — again non-conda paths are first-class.
- `systemd/claude-hooks-daemon.service:14`: `ExecStart=__REPO_PATH__/bin/claude-hooks-daemon` — goes through the shim, same resolution chain.
- Verdict: non-conda-env execution is realistic on both OSes (venv + system-python fallbacks are wired in before/around conda). Additionally, the `pgvector-mcp` console script (`pyproject.toml [project.scripts]`) runs under whichever env pip-installed the package — inherently non-conda-capable.

**Lane 3 — package psycopg import sites (the audit target)**
- Site 1: `claude_hooks/providers/pgvector.py:123` inside `PgvectorProvider.verify` (`:121`). On `ImportError` → `log.warning("psycopg not installed — cannot verify pgvector")` + `return False` (`pgvector.py:124-126`). **No** `find_conda_env_python()` / subprocess fallback anywhere in the method (`pgvector.py:120-139`).
- Site 2: `claude_hooks/providers/pgvector.py:328` inside `_ensure_ready` (`:320-336`). On `ImportError` → re-raises `ImportError("install psycopg: pip install psycopg[binary]")` (`pgvector.py:329-330`). **No** conda-env subprocess fallback. Callers catch it: `recall()` at `pgvector.py:147-150` catches `(ImportError, EmbedderError)` and degrades to a warning.
- Neither site mirrors `install.py:2140/2540/2657` — confirmed by grep: `find_conda_env_python` has zero hits under `claude_hooks/` (all hits are `install.py` + `tests/test_install_*`).

**Lane 4 — exercisability per site**
- `pgvector.py:123` is exercisable from non-conda python via two live callers:
  - `install.py:3556` (`ok = cls.verify(candidate)`) — the generic provider-verify loop. install.py is routinely invoked with system python (per the docstring at `install.py:2118-2123`); the fallback exists only in the dedicated `_verify_pgvector_dsn`, not in this generic path. For a pgvector candidate under system python, this returns a false negative "verify failed" even when the DB is reachable. Caveat: I grepped this line but did not read its enclosing function, so I can't confirm whether pgvector candidates even reach this generic loop vs. always being routed through `_verify_pgvector_dsn`.
  - `install.py:2132` — the fast path of `_verify_pgvector_dsn` itself; safe, because its `ImportError` is caught at `install.py:2135` and the subprocess fallback takes over.
  - Also hit from `tests/test_pgvector_integration.py:186`, but tests run in the conda env (`CHANGELOG.md:361` documents pytest under the conda python) — not exercisable-from-system in practice.
- `pgvector.py:328` is exercisable from non-conda python via:
  - `claude_hooks/pgvector_mcp/server.py:37` — imports `PgvectorProvider` and builds providers via `dispatcher.build_providers` (`server.py:35`); this server is the `pgvector-mcp` pip console script and the `claude-hook-pgvector-mcp` shim, i.e. plausible non-conda execution. If psycopg is missing there, `_ensure_ready` raises and recall/store fail for the process lifetime.
  - Hook path (`dispatcher` → `recall()` at `pgvector.py:148`): degraded but non-fatal — ImportError is caught at `pgvector.py:149` and recall returns empty; only exercisable as a silent capability loss, not a crash, when the shim's resolution chain lands on system/venv python (`bin/claude-hook:16-17`).

**Evidence gaps**
- Did not read `install.py` around `:3556` (enclosing verify loop) or lines 16-30 of `systemd/claude-hooks-pgvector-mcp.service` (ExecStart) — exercisability of site 1 via the generic installer loop is asserted from the grep hit plus the install.py docstring, not from reading the full call path.

---

## Researcher (round 1)

## Findings — psycopg import sites vs conda-env subprocess fallback (4e67dc2)

**The fallback pattern exists only in `install.py`** (3 sites, all with fast-path + `find_conda_env_python()` subprocess fallback):
- `install.py:2115-2161` — `_verify_pgvector_dsn`: fast path `import psycopg` at `install.py:2127`; falls back to conda-env subprocess at `install.py:2140-2148` running the embedded `_PGVECTOR_VERIFY_SCRIPT` (`install.py:2089-2112`). ✅ fixed.
- `install.py:2524` + `install.py:2540` — `_pgvector_tables_present`: fast path, then subprocess fallback with inline probe script (`install.py:2546`). ✅ fixed.
- `install.py:2647` + `install.py:2657` — `_init_pgvector_schema`: fast path, then conda-env subprocess with DDL piped via stdin (`install.py:2666-2676`); raises explicit `RuntimeError` at `install.py:2659-2663` if env missing. ✅ fixed.
- No dynamic import sites: no `importlib`-based psycopg loads anywhere; the only string-embedded psycopg code is the three installer probe scripts above, which execute *inside* the conda env by design.

**Stragglers outside `install.py` (no fallback):**
- `claude_hooks/providers/pgvector.py:123-126` — `verify()`: bare `import psycopg`, returns `False` + warning on ImportError. No conda fallback. **Exercisable?** Yes, but graceful: returns False, doesn't raise. Note this makes the installer's *fast path* (`install.py:2132`) return "verify returned false" from a non-conda interpreter even when the env is fine — the fast path only falls through to the subprocess on ImportError, and `PgvectorProvider.verify` swallows the ImportError internally. Minor semantic gap, not a crash.
- `claude_hooks/providers/pgvector.py:328-334` — `_ensure_ready()`: bare `import psycopg`, re-raises as `ImportError("install psycopg: pip install psycopg[binary]")` at `pgvector.py:330`. Exercisable from hook context in a non-conda Python, but the dispatcher contract (never block, exit 0) makes this non-fatal; it's a scaffold provider disabled by default (CLAUDE.md:435).
- `scripts/migrate_to_pgvector.py:624` — bare `import psycopg` in `main()`; hard crash from non-conda Python. **Partially exempt**: `docs/pgvector-runbook.md:733-737` documents pre-flight as running psycopg checks via the conda env's python explicitly (`/root/anaconda3/envs/claude-hooks/bin/python`), so the script is conda-env-documented operator tooling.
- `scripts/bench_recall.py:107` — bare `import psycopg` in `PgvectorBenchProvider.__init__`; same classification — benchmark tool, implicitly conda-env-only per runbook.

**Tests — exempt, guarded:**
- `tests/test_pgvector_integration.py:54-58` — `_skip_if_no_deps()` raises `unittest.SkipTest` on ImportError; `:285-294` `tearDownClass` wraps its psycopg import in `try/except Exception: pass`, so it's safe even when the class skips (tearDownClass still runs on skipped classes). Unreachable-failure confirmed.
- `tests/test_install_pgvector_verify.py` — all fallback tests mock `psycopg` via `sys.modules` (`:70`, `:91`, etc.) and patch `install.find_conda_env_python` (`:75`, `:110`) — no real psycopg needed.

**Environment cross-check (memory claim: one host's env is `unsloth`, not `claude-hooks`):**
- `install.py:66` hardcodes `CONDA_ENV_NAME = "claude-hooks"`; `find_conda_env_python(env_name="claude-hooks")` (`install.py:101`) probes `~/anaconda3|~/miniconda3|/opt/conda/envs/claude-hooks` layouts (`install.py:131-149`), then `conda env list --json` prefix-name match (`install.py:160-173`).
- On a host whose env is named `unsloth`, both steps fail and the function returns a canonical-but-nonexistent path keyed to `claude-hooks` (`install.py:177-190`). Every fallback caller `.exists()`-checks: `_verify_pgvector_dsn` returns `(False, "conda env not found")` (`install.py:2141-2143`), `_pgvector_tables_present` returns `False` (`install.py:2541-2542`), `_init_pgvector_schema` raises `RuntimeError` (`install.py:2658-2663`). Confirmed memory: "Conda env name: unsloth" (Qdrant recall).
- **Verdict: environment-dependent, not a code defect.** The fallback code is correct but silently inert on that host; remediation is `conda create -n claude-hooks` / symlink, or set psycopg in the calling interpreter — no code change makes `CONDA_ENV_NAME` configurable (no env-var override at `install.py:66`).

**Summary for synthesizer:** zero unguarded psycopg import sites remain in `install.py`; real stragglers are `claude_hooks/providers/pgvector.py:123` and `:328` (graceful-by-design), plus `scripts/migrate_to_pgvector.py:624` and `scripts/bench_recall.py:107` (hard-crash but runbook-documented as conda-env tooling). The `unsloth`-env host makes even the fixed fallback sites non-exercisable there — flag as deployment config issue, not code.

---

## Synthesizer

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

---
