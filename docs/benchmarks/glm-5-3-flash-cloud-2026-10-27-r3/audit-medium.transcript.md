# Consultation transcript — csl-2026-10-27-1220-c129

_Question_: Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python.


## Planner

Write the plan. I have grep evidence for psycopg import sites:

- claude_hooks/providers/pgvector.py:123 (in a try/except ImportError at 125 — verify whether it falls back to conda subprocess or just logs warning)
- claude_hooks/providers/pgvector.py:328-330 (raises ImportError, no fallback visible)
- install.py:2092 (probe script run via subprocess? need to check context)
- install.py:2127 fast path + 2138 fallback (this is the 4e67dc2 pattern)
- install.py:2524-2526 (fast path, fallback at ~2645-2668)
- scripts/migrate_to_pgvector.py:624
- scripts/bench_recall.py:107
- tests/test_pgvector_integration.py:56, 285 (tests, likely not exercisable from production path)
- docs (not code)

Plan steps: read each site, determine context (try/except, subprocess fallback, entry-point reachability from non-conda python), check callers (e.g., who calls pgvector.py:123 verify method — is it invoked from hooks running in non-conda env?), check git history for 4e67dc2 to identify the exact pattern added, check run.py/daemon entry points to see which interpreter runs the hooks.</think>1. Run `git show 4e67dc2` (or `git log --all --oneline | grep` if the hash isn't on the current branch — unverified) to pin down the exact fallback pattern it added in `install.py` (~lines 2125–2142 fast-path `import psycopg` / subprocess to conda-env python, and ~2645–2668 second instance), so later steps can diff sites against the canonical pattern.
2. Read `claude_hooks/providers/pgvector.py:115–135` and `:320–340` — verify whether the `import psycopg` at `:123` (inside `try/except ImportError` that only logs a warning at `:125`) and the bare re-raise at `:328–330` have any conda-env subprocess fallback, and trace callers (likely `_verify`/`connect` paths reached from hook entry points) to judge exercisability from a non-conda interpreter.
3. Read `install.py:2085–2100` and `:2510–2560` to classify the `import psycopg` at `:2092` and `:2524–2526` — determine whether each is already inside a subprocess probe script (executed in the conda env's python, so import-site reachability is moot) or an in-process import missing the fallback; cross-check against `tests/test_install_pgvector_verify.py:34–240` which documents fast-path vs fallback behavior per function.
4. Read `scripts/migrate_to_pgvector.py:615–630` and `scripts/bench_recall.py:100–110` — check for try/except or subprocess fallback around the bare `import psycopg` sites, and check each script's `__main__`/docstring for the intended interpreter (dev-only scripts may be exempt).
5. Read `tests/test_pgvector_integration.py:50–70` and `:280–290` to confirm these import sites are test-only with explicit `unittest.SkipTest` when psycopg is absent (i.e., not exercisable in production, likely out of audit scope but list them).
6. Determine the runtime interpreter story: read `bin/claude-hook.cmd`, `bin/claude-hook-pgvector-mcp.cmd`, and `claude_hooks/daemon_ctl.py:41,262` (lazy conda detection) to establish which entry points can actually run under a non-conda-env python — this decides whether `pgvector.py` sites are "actually exercisable" or only reachable when psycopg is guaranteed present.
7. Sweep for indirect psycopg usage the grep may have under-counted (e.g., `importlib.import_module("psycopg")`, `from psycopg import`, or string-built probes) via `grep -r "psycopg" --include="*.py"` on `claude_hooks/`, `scripts/`, `run*.py`, and check `docs/pgvector-runbook.md:499–737` only as documentation of the intended pattern, not as code sites.

---

## Researcher (round 1)

## Findings: psycopg import sites vs. the 4e67dc2 conda-subprocess fallback

**Reference pattern (4e67dc2, `install.py`)** — fast path `import psycopg` at `install.py:2127`, fallback shells out to `find_conda_env_python()` at `install.py:2140-2148` (runs `_PGVECTOR_VERIFY_SCRIPT` via `subprocess.run`). The same pattern is replicated in `_pgvector_tables_present` (fast `install.py:2524`, fallback `install.py:2540-2542`) and `_init_pgvector_schema` (fast `install.py:2647`, fallback `install.py:2657-2668`).

### Sites WITH the fallback (OK)

- **`install.py:2127`** — `_verify_pgvector_dsn`: full fallback present (`install.py:2140-2148`). Exercisable from non-conda Python. ✅
- **`install.py:2524`** — `_pgvector_tables_present`: fallback at `install.py:2540-2542` (inline script, `import psycopg` at `install.py:2546`). ✅
- **`install.py:2647`** — `_init_pgvector_schema`: fallback at `install.py:2657-2668` (DDL piped via stdin). ✅
- **`install.py:2092`** — this `import psycopg` is *inside* `_PGVECTOR_VERIFY_SCRIPT` (`install.py:2089-2112`), the probe script that is only ever executed **in the conda env's python** by the fallback at `install.py:2147`. Not a live site in the calling interpreter. ✅

### Sites WITHOUT the fallback (gaps)

- **`claude_hooks/providers/pgvector.py:123`** (`PgvectorProvider.verify`): `except ImportError` → `log.warning("psycopg not installed — cannot verify pgvector")` + `return False` (`pgvector.py:124-126`). **No conda subprocess fallback.** Exercisability: yes — `install.py:2132` calls `PgvectorProvider.verify(candidate)` from the installer's fast path, and `install.py:3556` (`ok = cls.verify(candidate)`) is the generic verify loop — though `install.py:3547-3548` skips `pgvector` there, so the generic loop is only other providers. The `install.py:2132` call is guarded by the fast-path import at `install.py:2127`, so in install.py it's only reached when psycopg IS importable. However, `verify` is a public classmethod on the provider — any other caller (e.g. `claude_hooks/pgvector_mcp/server.py:37` imports `PgvectorProvider` and would inherit the gap if it ever called `verify`) hits the misleading `return False` from a system python. Severity: low (degrades to "unreachable" signal, no crash), but it's the exact misleading-`FAILED` symptom 4e67dc2 fixed in install.py.
- **`claude_hooks/providers/pgvector.py:328-330`** (`_ensure_ready`): raises `ImportError("install psycopg: pip install psycopg[binary]")` — **no conda subprocess fallback**. Exercisable from non-conda Python: yes, via `recall()` (`pgvector.py:148` catches `ImportError` → returns `[]` with a warning, `pgvector.py:149-151`) and `store()` (via the same `_ensure_ready` call). This is the **production hook path**: hooks run under the conda env's python normally (per `bin/claude-hook` shim preference), so in the standard install it's masked — but if the hook shim falls back to system `python3` (conda env absent), pgvector recall silently degrades to empty. Arguably by design (stdlib-only core, graceful degradation), but it's a real no-fallback site.
- **`scripts/migrate_to_pgvector.py:624`**: bare `import psycopg` in `main()` (`migrate_to_pgvector.py:610, 624`), no try/except, no conda fallback. Exercisable from non-conda Python: yes — standalone CLI (`scripts/migrate_to_pgvector.py:653-654` `if __name__ == "__main__"`), runs with whatever python invokes it. Raw `ImportError` traceback if psycopg absent. No `bin/` shim routes to it (grep of `bin/` for `migrate_to_pgvector|bench_recall`: 0 hits), so it's operator-invoked only.
- **`scripts/bench_recall.py:107`**: bare `import psycopg` inside `PgvectorBenchProvider.__init__` (`bench_recall.py:105-108`), no fallback. Exercisable from non-conda Python: yes — standalone CLI (`bench_recall.py:322-323`), no `bin/` shim. Same raw-`ImportError` profile as migrate script; dev/bench tooling, lower stakes.

### Test-only sites (not production-exercisable)

- **`tests/test_pgvector_integration.py:56`** — guarded by `_skip_if_no_deps()` raising `unittest.SkipTest("psycopg not installed")` (`test_pgvector_integration.py:57-58`); also skips on unreachable Postgres (`:70`), missing pgvector ext (`:67`), missing Ollama (`:87-89`). Second site at `:285` (teardown) is only reached after the skip gate passed, so psycopg is known-present. Not exercisable from a non-conda production path. ✅

### Summary

| Site | Fallback? | Exercisable from non-conda py? |
|---|---|---|
| `install.py:2092` | n/a (runs in conda subprocess) | no |
| `install.py:2127` | ✅ `:2140-2148` | yes, handled |
| `install.py:2524` | ✅ `:2540-2542` | yes, handled |
| `install.py:2647` | ✅ `:2657-2668` | yes, handled |
| `pgvector.py:123` | ❌ logs + `return False` | yes (via `install.py:2132`, guarded; public API otherwise) |
| `pgvector.py:328` | ❌ raises | yes — hook recall/store path, silently degrades |
| `migrate_to_pgvector.py:624` | ❌ bare | yes — standalone CLI |
| `bench_recall.py:107` | ❌ bare | yes — standalone CLI |
| `test_pgvector_integration.py:56,285` | n/a (SkipTest) | no |

Actionable gaps: the two `scripts/` CLIs and the two provider methods. The provider methods (`pgvector.py:123`, `:328`) are the only ones on a production code path; the scripts are operator tooling where a raw ImportError is arguably acceptable but inconsistent with the 4e67dc2 pattern.

---

## Researcher (round 1)

## Findings: psycopg import sites vs. the conda-env subprocess fallback pattern

**Evidence gap up front:** I could not verify commit `4e67dc2` directly — no git tool is exposed in this session. The canonical pattern is instead grounded in `install.py:2115-2151` (`_verify_pgvector_dsn` docstring explicitly describes the "conda env's python via subprocess when psycopg isn't importable" behavior) and `tests/test_install_pgvector_verify.py:5-14` (documents fast-path vs. fallback and the motivating bug). Treat "added in 4e67dc2" as unverified-but-consistent.

### Sites WITH the fallback (compliant)

- `install.py:2127` — fast-path `import psycopg` in `_verify_pgvector_dsn`; `except ImportError: pass` at `install.py:2135-2136`, then subprocess fallback via `find_conda_env_python()` at `install.py:2140-2149` running the embedded `_PGVECTOR_VERIFY_SCRIPT` (`install.py:2089-2112`).
- `install.py:2524` — fast path in `_pgvector_tables_present`; fallback at `install.py:2539-2560`.
- `install.py:2647` — fast path in `_init_pgvector_schema`; fallback at `install.py:2656-2676` (DDL piped via stdin).
- `install.py:2092` — **not an in-process import**: it's inside the `_PGVECTOR_VERIFY_SCRIPT` string constant (`install.py:2089`), executed *by* the conda subprocess at `install.py:2147`. Reachability is moot.

### Sites WITHOUT the fallback (the audit's answer)

- **`claude_hooks/providers/pgvector.py:123`** (`verify()`): `try/except ImportError` → `log.warning` + `return False` (`pgvector.py:125-126`). No conda subprocess fallback. **Exercisability:** `verify()` has no callers inside `claude_hooks/` (grep for `.verify(` in `claude_hooks/` = 0 hits); it's called from `install.py:2132` — but that call site is *inside* the guarded fast path, so a missing psycopg there is caught and the subprocess fallback runs. Second caller: `install.py:3556` (`ok = cls.verify(candidate)` in the status listing) — **unguarded**: verify returns False gracefully, but the user sees a misleading "UNREACHABLE" for pgvector on a non-conda python even when the DB is fine. This is the same "misleading FAILED" class of bug the fallback was added to fix (`tests/test_install_pgvector_verify.py:11-13`), and it remains at the `install.py:3556` call site.
- **`claude_hooks/providers/pgvector.py:328`** (`_ensure_ready()`): imports psycopg, re-raises as `ImportError` (`pgvector.py:329-330`). No fallback. **Exercisable from a non-conda python: YES.** `bin/_resolve_python.sh:53-58` falls through to system `python3`/`python` when neither repo `.venv` nor the conda env exists, and `bin/claude-hook-pgvector-mcp:14-17` execs that interpreter (`run.py` is invoked with whatever python `settings.json` names — `run.py:7-8`). So hooks *can* run pgvector code under a non-conda interpreter. However: pgvector defaults to `enabled: False` (`claude_hooks/config.py:48-49`), so it's unreachable in a default install. When enabled, the raised ImportError is *caught* by callers — `recall()` → warn + `[]` (`pgvector.py:149-151`), `batch_recall()` → `pgvector.py:266-268`, `store()` → re-raised as `RuntimeError` (`pgvector.py:216-217`), `batch_store()` → `pgvector.py:293-294`. Impact: silent recall degradation / failed stores, no crash — graceful, but no conda-env rescue.
- **`scripts/migrate_to_pgvector.py:624`**: bare `import psycopg` in `main()`, no try/except, no fallback. Exercisable from non-conda python → crashes with a raw ImportError traceback. Operator/dev tooling (usage in docstring `scripts/migrate_to_pgvector.py:18-33`), not hook-reachable — exempt from the hook-safety contract but a rough edge for the documented `python scripts/migrate_to_pgvector.py` invocation.
- **`scripts/bench_recall.py:107`**: bare `import psycopg` in `PgvectorBenchProvider.__init__`, no fallback. Dev-only benchmark (`scripts/bench_recall.py:16-21`), not hook-reachable; fails loudly under system python.
- **`tests/test_pgvector_integration.py:56` and `:285`**: guarded — `unittest.SkipTest("psycopg not installed")` at `tests/test_pgvector_integration.py:58`. Test-only, skipped when absent; out of audit scope.

### Interpreter-reachability verdict

- Hook runtime path: `bin/claude-hook` → `_resolve_python.sh` (prefers `.venv`, then conda env `claude-hooks`, then **system python3** — `bin/_resolve_python.sh:31-58`) → `run.py` → dispatcher. pgvector code paths are therefore genuinely exercisable under a non-conda python whenever (a) no conda env / CLAUDE_HOOKS_PY override exists and (b) `providers.pgvector.enabled: true`.
- The `pgvector_mcp` server (`claude_hooks/pgvector_mcp/server.py:37` imports `PgvectorProvider` at module level) is safe at import time — psycopg itself is only imported lazily inside the provider methods (`pgvector.py:123`, `:328`); the MCP server catches handler exceptions into `isError` responses (`server.py:246-251`), so a missing psycopg surfaces as a tool error, not a crash.
- `run.py:42-55` may hand the event to the daemon instead; the daemon is launched via the same shim family (`systemd/claude-hooks-daemon.service:14` → `bin/claude-hooks-daemon`), so the same interpreter-fallthrough logic applies.

### Recommendation for synthesizer

Real gaps, in priority order: (1) `install.py:3556` status listing misleadingly reports pgvector as UNREACHABLE under non-conda python — same bug class the 4e67dc2-style fallback fixed; (2) `claude_hooks/providers/pgvector.py:328` could reuse the `find_conda_env_python()` subprocess probe (it lives in `install.py`, so a shared helper or a small local copy in providers/ would be needed); (3) the two `scripts/` sites are dev-tooling-only and arguably fine to leave as loud failures.

---

## Synthesizer

**Bottom line: the 4e67dc2 fallback pattern is present and correct at all four `install.py` in-process import sites; the four sites still lacking it are the two provider methods (`claude_hooks/providers/pgvector.py:123`, `:328`) and the two standalone scripts (`scripts/migrate_to_pgvector.py:624`, `scripts/bench_recall.py:107`). Only the provider sites are on a production code path.**

**Compliant — fallback present (all verified by direct read):**
- `install.py:2127` (`_verify_pgvector_dsn` fast path) → `except ImportError: pass` at `install.py:2135-2136`, then `find_conda_env_python()` + `subprocess.run([conda_py, "-c", _PGVECTOR_VERIFY_SCRIPT, dsn])` at `install.py:2140-2148`. ✓
- `install.py:2524` (`_pgvector_tables_present`) → fallback at `install.py:2539-2548` (inline script with its own `import psycopg` at `install.py:2546`). ✓
- `install.py:2647` (`_init_pgvector_schema`) → fallback at `install.py:2656-2668`, DDL piped via stdin. ✓
- `install.py:2092` — not an in-process import; it's inside the `_PGVECTOR_VERIFY_SCRIPT` string, executed *by* the conda subprocess at `install.py:2147`. Reachability from the calling interpreter is moot. ✓

**Gaps — no fallback:**
- `claude_hooks/providers/pgvector.py:123` (`PgvectorProvider.verify`): `except ImportError` → `log.warning` + `return False` (`pgvector.py:124-126`). Exercisable from non-conda Python: **yes, but effectively guarded** — its only in-repo caller is `install.py:2132`, which sits inside the fast path and is only reached after `import psycopg` at `install.py:2127` succeeded, so a missing psycopg there falls through to the subprocess fallback instead. The generic verify loop never reaches pgvector (`install.py:3547-3548` skips it), so `install.py:3556` does not exercise this site. Residual exposure: any *external* caller of the public classmethod under a system python gets a misleading `False` ("unreachable") — same bug class 4e67dc2 fixed, but currently unreachable in-repo. Severity: low.
- `claude_hooks/providers/pgvector.py:328-330` (`_ensure_ready`): re-raises as `ImportError("install psycopg: pip install psycopg[binary]")`. No conda fallback. **Exercisable from non-conda Python: yes** — `bin/_resolve_python.sh:53-58` falls through to system `python3`/`python` when neither `.venv` nor the conda env exists, and that interpreter runs the hook runtime (`run.py`). Mitigations: pgvector defaults to disabled (`claude_hooks/config.py:48-49`, per researcher), and callers catch the error — `recall()` → warn + `[]` (`pgvector.py:149-151`), `store()` → `RuntimeError` (`pgvector.py:216-217`) — so behavior is graceful degradation, not a crash. This is the highest-value gap: it's the only one on the live hook path, and a conda-subprobe (reusing `find_conda_env_python()`, which lives in `install.py`) would restore recall under a system python.
- `scripts/migrate_to_pgvector.py:624`: bare `import psycopg` in `main()`, no try/except. Exercisable: **yes** — standalone CLI (`scripts/migrate_to_pgvector.py:653-654`), no `bin/` shim routes to it. Raw ImportError traceback under system python. Operator tooling; arguably acceptable.
- `scripts/bench_recall.py:107`: bare `import psycopg` in `PgvectorBenchProvider.__init__` (`bench_recall.py:105-108`). Exercisable: **yes** — standalone CLI (`bench_recall.py:322-323`), no shim. Same loud-failure profile; dev/bench tooling.

**Out of scope (not exercisable from production):**
- `tests/test_pgvector_integration.py:56` and `:285` — guarded by `_skip_if_no_deps()` raising `unittest.SkipTest("psycopg not installed")` (`test_pgvector_integration.py:57-58`); the `:285` teardown site is only reached after the skip gate passed.

**Unverified:**
- That commit `4e67dc2` is the origin of the `install.py:2127/2140-2148` pattern — no git tool available; the pattern itself is verified in the working tree, the attribution is not.
- "No other callers of `PgvectorProvider.verify` inside `claude_hooks/`" — researcher grep, not independently confirmed (harmless: the finding doesn't depend on it).

---
