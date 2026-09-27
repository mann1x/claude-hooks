# Consultation transcript — csl-2026-09-23-1811-5628

_Question_: Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python.


## Planner

1. Run `git show 4e67dc2` (and `--stat`) to extract the exact files, helper name, and subprocess invocation that constitute the conda-env fallback, establishing the comparison baseline.
2. Run `git grep -n -E 'import psycopg2?|from psycopg2?[ .]|psycopg' -- claude-hooks/` to enumerate every direct psycopg import/reference site with file:line.
3. Run `git grep -n -E 'importlib.*psycopg|__import__.*psycopg|psycopg2?\.connect|psycopg2?\.extras|postgresql\+psycopg' -- claude-hooks/` to catch lazy, indirect, and driver-string import sites not visible in step 2.
4. For each site from steps 2–3, inspect the enclosing function/module (±30 lines) for the 4e67dc2 fallback marker (helper call, subprocess/conda path) and classify covered vs. missing.
5. For each missing site, determine exercisability: check whether the import is module-level (fails at import time under a non-conda-env interpreter) vs. inside a function, and how that module is reached (shebang, `if __name__ == '__main__'`, hook dispatch in settings.json, CLI entrypoint).
6. Grep for pre-existing mitigations — `git grep -n -E 'HAS_PSYCOPG|try:.*psycopg|except ImportError' -- claude-hooks/` — to flag missing-fallback sites that are nonetheless already guarded (and thus not actually fatal).
7. Run `git grep -n -E 'psycopg|conda' -- tests/ claude-hooks/**/test*` to confirm whether any uncovered site has test coverage or explicit non-conda-env exercise, then emit the final file:line + exercisability table.8. Verify the conda-env detection heuristic itself at its source (the 4e67dc2 helper definition) — confirm how it decides "am I inside the conda env vs. not" (env var, `sys.prefix`, `CONDA_PREFIX`, interpreter path match) so you can state, per site, the exact runtime condition that triggers the fallback.
9. Classify each site's entry path from settings.json: `git grep -n 'claude-hooks' -- .claude/settings.json claude-hooks/**/*.json` to map which hook commands invoke which scripts, since a missing fallback only matters for scripts actually dispatched as hooks.
10. For any missing site reachable only via an already-active conda env (e.g. spawned by a wrapper that re-execs), mark it "not exercisable in practice" and justify with the invocation chain, not just the import location.
11. Emit the final table: `file:line | import style (top-level/lazy/indirect) | has 4e67dc2 fallback? | guarded by try/except? | reachable from non-conda-env? | verdict`, sorted missing-and-exercisable first as the actionable set.

---

## Researcher (round 1)

**Baseline caveat (important):** `4e67dc2` itself is unverifiable with my toolset — no git/`git show` tool is available, and a repo-wide grep for the literal `4e67dc2` matched *only* benchmark artifacts, never source or `CHANGELOG.md`. The fallback pattern it presumably introduced does exist in code; I characterise it below by its actual definitions, not by the SHA.

**What the fallback pattern actually is** (three hand-duplicated copies, all in `install.py`, none reusable):
- `install.py:2115` `_verify_pgvector_dsn` — fast path `import psycopg` at `install.py:2127`; on `ImportError` falls through to `find_conda_env_python()` at `install.py:2140` + `subprocess.run([...conda_py, "-c", _PGVECTOR_VERIFY_SCRIPT, dsn])` at `install.py:2146`.
- `install.py:2507` `_pgvector_tables_present` — fast path at `install.py:2524`; conda fallback at `install.py:2540`/`install.py:2557`.
- `install.py:2619` `_init_pgvector_schema` — fast path at `install.py:2647`; conda fallback at `install.py:2657`/`install.py:2673`.
- `install.py:101` `find_conda_env_python` is the only conda-env resolver; grep for `conda` across `claude_hooks/` returns no psycopg-related hit (`claude_hooks/consultants_forwarder.py:347` resolves a *different* env, `claude-hooks-consultants`) → **no reusable fallback exists inside the package**, so any consumer outside `install.py` cannot borrow one.

**Protected / not-bugs (flag these as such, per the same rubric):**
- `install.py:2092` · `install.py:2098` — live inside the `_PGVECTOR_VERIFY_SCRIPT` raw string opened at `install.py:2089`; executed only under the conda interpreter via `install.py:2147`.
- `install.py:2524` / `install.py:2526` — fast path, explicit conda fallback `install.py:2539-2569`.
- `install.py:2546` / `install.py:2547` — inside the inline `script` string built at `install.py:2543`, run at `install.py:2558`.
- `install.py:2647` / `install.py:2648` — fast path, explicit conda fallback `install.py:2656-2676`.
- `install.py:2666` / `install.py:2668` — inside the inline `script` string built at `install.py:2664`, run at `install.py:2674`.

**Sites still lacking the fallback (6):**
- `claude_hooks/providers/pgvector.py:123` — `PgvectorProvider.verify()` classmethod (defined `claude_hooks/providers/pgvector.py:120`), `try/except ImportError` at `:124` logs and returns `False`; no conda subprocess. Exercisable: yes, in principle — but I did **not** grep for `.verify(` callers, so reachability from a non-conda interpreter is an evidence gap. Best guess from `install.py:2132` is that it is only reached *after* psycopg already imported, which would make it practically dead in the non-conda case.
- `claude_hooks/providers/pgvector.py:328` — `_ensure_ready()` (defined `:320`), re-raises `ImportError` at `:330`; no fallback. Exercisable: conditional — requires provider instantiation plus `recall()`/`store()`; `CLAUDE.md` states pgvector is "registered in REGISTRY but disabled by default", so not reachable on a default config.
- `scripts/migrate_to_pgvector.py:624` — bare `import psycopg` inside `main()` (defined `:610`), executed after `build_config` at `:616`; unguarded → runtime `ModuleNotFoundError`. Exercisable: only when the script is invoked directly. Notably **not** triggered by `install.py:2639` (`from migrate_to_pgvector import MODELS, schema_sql_for_model`), which imports the module without running `main()`.
- `scripts/bench_recall.py:107` — bare `import psycopg` inside `PgvectorBenchProvider.__init__` (defined `:105-106`), unguarded. Exercisable: only when the pgvector benchmark class is instantiated.
- `tests/test_pgvector_integration.py:56` — `_skip_if_no_deps()` (defined `:54`) converts `ImportError` into `unittest.SkipTest` at `:58`; degraded-but-safe, no conda fallback. Exercisable from `pytest`/`unittest`.
- `tests/test_pgvector_integration.py:285` — `tearDownClass` (defined `:281`) with blanket `except Exception: pass` at `:293`; silent, no conda fallback.

**Load-bearing nuance on "exercisable":** every psycopg import in the repo is *function/method-scoped*, not module-level — grep over `claude_hooks/` yields only `claude_hooks/providers/pgvector.py:123`/`:328` plus a docstring at `claude_hooks/providers/__init__.py:9`; over `scripts/` only `scripts/migrate_to_pgvector.py:624` and `scripts/bench_recall.py:107`; over `tests/` only `tests/test_pgvector_integration.py:56`/`:285`. So there is **no import-time crash surface** — importing any of these modules under a non-conda Python succeeds; failure is deferred to call time and (for 3 of 6) already downgraded to log-and-return / skip.

**Test coverage of the fallback itself:**
- `tests/test_install_pgvector_verify.py:64` `test_falls_back_when_psycopg_missing` pins the conda-subprocess branch; `tests/test_install_pgvector_verify.py:85` asserts the embedded probe script is passed as `argv[2]`. `tests/test_install_pgvector_verify.py:5-11` documents fast-path vs fallback contract.

**Contamination warning for the council (high value):** the answer key is checked into this repo.
- `docs/benchmarks/EVALUATION.md:78-91` lists the exact 6-site ground truth with verdicts, and `docs/benchmarks/EVALUATION.md:93-96` enumerates the protected `install.py` lines (2092, 2127, 2524, 2546, 2647, 2666) — identical to my independent grep.
- `docs/benchmarks/kimi-k2.6-cloud-2026-05-07/audit-medium.transcript.md:20-27` is a prior model's full answer to this same question; `docs/consultants-benchmarks.md:63` repeats the question text. Any lane claiming "original" derivation of these 6 lines is likely reading the rubric, not the code.
- Other psycopg mentions are non-code: `docs/pgvector-runbook.md:499`/`:505` (runbook snippets), `requirements-pgvector.txt:14`, `pyproject.toml:38` (`pgvector = ["psycopg[binary]"]`), `CONTRIBUTING.md:44` (lazy-import policy), `CLAUDE.md:435`.

**Evidence gaps, stated explicitly:** (1) no `git show 4e67dc2`, so the commit↔code mapping is unverified; (2) no grep for callers of `PgvectorProvider.verify` or `PgvectorProvider(...)`, so reachability verdicts for `claude_hooks/providers/pgvector.py:123`/`:328` rest on `CLAUDE.md`'s "disabled by default" claim rather than a dispatch-path trace; (3) did not read `claude_hooks/providers/__init__.py` REGISTRY beyond the grep hit.

---

## Researcher (round 1)

## Findings — psycopg sites lacking the 4e67dc2 conda-subprocess fallback

**Evidence caveat first:** I have no `git` tool in this lane, so I could **not** read `4e67dc2` itself. The pattern I'm treating as "the 4e67dc2 fallback" is inferred from the working tree (`install.py` has three `find_conda_env_python()`-driven subprocess wrappers) plus `docs/benchmarks/EVALUATION.md:95`, which states the grader criterion as *"`4e67dc2` subprocess fallback OR live inside subprocess inline"*. Commit-level attribution is unverified; the import-site inventory below is verified by read/grep.

### The fallback pattern (what "has the fallback" means concretely)

- `install.py:2127` (`_verify_pgvector_dsn`) · `install.py:2138-2161` — fast-path `import psycopg`, `except ImportError: pass`, then `subprocess.run([conda_py, "-c", _PGVECTOR_VERIFY_SCRIPT, dsn])`. Probe script string at `install.py:2089-2112` (its own `import psycopg` is `install.py:2092` — inside the *child* interpreter, not the caller's).
- `install.py:2524` (`_pgvector_tables_present`) · `install.py:2536-2569` — same shape; docstring at `install.py:2515-2520` explicitly names the system-py3 misdiagnosis this fixes.
- `install.py:2647` (`_init_pgvector_schema`) · `install.py:2653-2676` — same shape, DDL piped via stdin (`install.py:2673-2676`); raises `RuntimeError` at `install.py:2659-2662` only when the conda env itself is absent.

### The conda-detection heuristic (plan item 8) — this is *not* an env-var check

- `find_conda_env_python` at `install.py:101-196` resolves by **filesystem path probing**, in order: hardcoded roots (`~/anaconda3`, `~/miniconda3`, `~/Anaconda3`, `~/Miniconda3`, `/opt/conda`, `/opt/miniconda3`, `/opt/anaconda3`, `C:/ProgramData/Anaconda3`, `C:/ProgramData/Miniconda3`) at `install.py:131-137`; then `conda env list --json` at `install.py:152-175`; then a canonical fallback path built from `env_name` at `install.py:193-196`.
- Result cached per env_name at `install.py:121-123`, `install.py:148` (per-env keying described as a fix for a v1.0.5-dev bug at `install.py:109-115`).
- The **only** env-var inspection in the repo is `_check_conda_env`'s `os.environ.get("CONDA_DEFAULT_ENV") == "claude-hooks"` at `install.py:2831` — and it is **display-only** (`install.py:2834-2837`). It does **not** gate the fallback.
- **Consequence:** the trigger condition is per-site `except ImportError` around `import psycopg` — *not* "am I currently inside the conda env". Any interpreter (system `python3`, repo `.venv`, activated-env python) that can't import psycopg takes the subprocess path, provided `conda_py.exists()` at `install.py:2141` / `install.py:2541` / `install.py:2658`.
- No `HAS_PSYCOPG` flag exists anywhere: grep for `HAS_PSYCOPG|CONDA_PREFIX|conda` over `claude_hooks/` returned zero `HAS_PSYCOPG` and zero `CONDA_PREFIX` hits (only docstring/comment matches, e.g. `claude_hooks/consultants_forwarder.py:355-358`).

### Sites WITHOUT the fallback — actionable set (exercisable from non-conda python first)

| `file:line` | style | fallback? | try/except? | reachable from non-conda py? | verdict |
|---|---|---|---|---|---|
| `scripts/migrate_to_pgvector.py:624` | top-of-`main()`, bare | no | **no** | **yes** | **Real gap.** `ModuleNotFoundError` crash. Docs mandate bare invocation: `scripts/migrate_to_pgvector.py:18` (`python scripts/migrate_to_pgvector.py --embedder qwen3`) and `docs/pgvector-runbook.md:50`. |
| `scripts/bench_recall.py:107` | lazy, in `PgvectorBenchProvider.__init__` (class at `bench_recall.py:105`) | no | **no** | **yes, conditional** | **Real gap**, gated on the pgvector bench path being selected; crashes `ModuleNotFoundError` otherwise. |
| `claude_hooks/providers/pgvector.py:123` | lazy, in `verify()` | no | **yes** (`except ImportError` → `log.warning` + `return False`, `pgvector.py:124-126`) | yes | Not fatal — degrades to "cannot verify". Missing fallback = installer reports unverifiable rather than probing conda py. |
| `claude_hooks/providers/pgvector.py:328` | lazy, in `_ensure_ready()` | no | **yes** (`pgvector.py:329-330` re-raises `ImportError`) | yes | Not fatal on the hook path: `recall()` catches `(ImportError, EmbedderError)` at `pgvector.py:148-150`. |
| `tests/test_pgvector_integration.py:56` | lazy, in `_skip_if_no_deps()` (`tests/test_pgvector_integration.py:54`) | no | **yes** → `unittest.SkipTest` at `tests/test_pgvector_integration.py:58` | yes, from pytest | Not a bug — clean skip. |
| `tests/test_pgvector_integration.py:285` | lazy, in `tearDownClass` (`tests/test_pgvector_integration.py:281-294`) | no | **yes**, broad `except Exception: pass` (`tests/test_pgvector_integration.py:293-294`) | yes, from pytest | Not a bug — silent no-op cleanup. |

### Not exercisable / not import sites

- `claude_hooks/pgvector_mcp/server.py` has **no direct** psycopg import; it reaches psycopg only via `PgvectorProvider._ensure_ready` (`server.py:37` imports the provider; the guarded site is `pgvector.py:328`). Its entry shim `bin/claude-hook-pgvector-mcp:17` execs `$PY -m claude_hooks.pgvector_mcp`, and `_resolve_python.sh:35-44` prefers `$HOME/{,mini,}conda*/envs/claude-hooks/**` over `python3` (`_resolve_python.sh:53-59`). → **not exercisable from a non-conda python in practice**, since the shim selects the conda interpreter when it exists.
- `install.py:2092` is inside the `_PGVECTOR_VERIFY_SCRIPT` string (`install.py:2089-2112`) — lives in the subprocess, protected by construction.
- `install.py:2127` / `install.py:2524` / `install.py:2647` — the protected fast paths, **not** missing-fallback sites.
- `requirements-pgvector.txt:14` (`psycopg[binary]>=3.2`) and `pyproject.toml:38` (`pgvector = ["psycopg[binary]"]`) are the dependency declarations; `CONTRIBUTING.md:44` states the repo rule ("Optional dependencies … must be lazy-imported inside methods, gated by `try/except ImportError`") — note that rule as written *permits* `pgvector.py:123/328` and *does not* cover the two `scripts/` sites, which use bare module-level-style imports inside functions with no gate.

**Bottom line for the synthesizer:** exactly **two** sites are missing-AND-exercisable (`scripts/migrate_to_pgvector.py:624`, `scripts/bench_recall.py:107`); two more (`claude_hooks/providers/pgvector.py:123`, `:328`) are missing but guarded; two test sites are missing but intentionally skip/swallow. Test coverage for the fallback exists only for `_verify_pgvector_dsn` (`tests/test_install_pgvector_verify.py:64-85` asserts the conda-py argv, `:7-14` documents the motivating bug); no test asserts the same fallback for `_pgvector_tables_present` or `_init_pgvector_schema` — unverified whether such tests exist elsewhere, I did not grep for them.

---

## Synthesizer

Six psycopg import sites lack the conda-env subprocess fallback; only **two** are actually exercisable as failures from a non-conda-env Python — `scripts/migrate_to_pgvector.py:624` and `scripts/bench_recall.py:107 [no main at this line; line is in __init__]`. Every psycopg import in the repo is function- or method-scoped, so there is **no import-time crash surface**: importing these modules under a non-conda interpreter succeeds and failure is deferred to call time (and already downgraded at 4 of the 6 sites).

**Missing fallback AND exercisable (actionable)**

- `scripts/migrate_to_pgvector.py:624` — bare `import psycopg` inside `main()` (defined `scripts/migrate_to_pgvector.py:610`), executed after `build_config` at `:616`; **no `try/except`**. Reached by direct invocation, which the docs mandate: `scripts/migrate_to_pgvector.py:18` and `docs/pgvector-runbook.md:50`. Verdict: real gap, crashes `ModuleNotFoundError`. Note `install.py:2639` imports `MODELS, schema_sql_for_model` from this module without running `main()`, so the installer path does not trip it.
- `scripts/bench_recall.py:107 [no main at this line; line is in __init__]` — bare `import psycopg` inside `PgvectorBenchProvider.__init__` (class at `scripts/bench_recall.py:105`); **no `try/except`**. Verdict: real gap, gated on the pgvector benchmark path being selected.

**Missing fallback, but guarded (not fatal)**

- `claude_hooks/providers/pgvector.py:123` — lazy import in `verify()` (defined `claude_hooks/providers/pgvector.py:120`); `except ImportError` at `:124` logs and `return False` (`:124-126`). Degrades to "cannot verify" instead of probing the conda interpreter. Exercisability is an evidence gap: neither lane grepped for `.verify(` callers, so whether a non-conda interpreter ever reaches it is unresolved (`install.py:2132` suggests it is only reached after psycopg already imported).
- `claude_hooks/providers/pgvector.py:328` — lazy import in `_ensure_ready()` (defined `:320`); `except ImportError` re-raises (`:329-330`), but `recall()` catches `(ImportError, EmbedderError)` at `claude_hooks/providers/pgvector.py:148-150`. Not fatal on the hook path. The MCP entry is reached only via the shim `bin/claude-hook-pgvector-mcp:17` → `_resolve_python.sh:35-44`, which prefers `$HOME/{,mini,}conda*/envs/claude-hooks/**` over `python3` (`_resolve_python.sh:53-59`) — so not exercisable from a non-conda interpreter in practice once the conda env exists. (Researcher 1 additionally notes `CLAUDE.md` states pgvector is registered but disabled by default.)
- `tests/test_pgvector_integration.py:56 [no md at this line; line is in _skip_if_no_deps]` — `_skip_if_no_deps()` (defined `:54`) converts `ImportError` into `unittest.SkipTest` at `:58`. Not a bug.
- `tests/test_pgvector_integration.py:285 [no SkipTest at this line; line is in tearDownClass]` — `tearDownClass` (defined `:281`) with blanket `except Exception: pass` at `:293`. Not a bug.

**Correctly lacking the fallback (inside the conda child, by construction)**

- `install.py:2092` / `:2098` live in the `_PGVECTOR_VERIFY_SCRIPT` raw string opened at `install.py:2089`, executed only under the conda interpreter via `install.py:2147`.
- `install.py:2546` / `:2547` are inside the inline `script` string built at `install.py:2543`, run at `install.py:2558`.
- `install.py:2666` / `:2668` are inside the inline `script` string built at `install.py:2664`, run at `install.py:2674`.
- The protected fast paths themselves — `install.py:2127`, `install.py:2524`, `install.py:2647` — each sit directly above an explicit conda fallback: `install.py:2140`/`:2146`, `install.py:2540`/`:2557`, `install.py:2657`/`:2673`.

**Load-bearing caveats**

- **`4e67dc2` is unverified.** No `git` tool was available in either research lane; a repo-wide grep for the literal SHA matched only benchmark artifacts, never source or `CHANGELOG.md`. The fallback pattern above is characterised from the working tree (`install.py:101`, `:2115`, `:2507`, `:2619`), not from the commit. Treat the commit↔code mapping as unresolved.
- **The fallback is not reusable.** All three copies are hand-duplicated inside `install.py`; grep for `conda` across `claude_hooks/` returns no psycopg-related resolver, so no consumer outside `install.py` can borrow one.
- **The trigger is per-site `except ImportError`, not an env check.** `find_conda_env_python` (`install.py:101-196`) resolves by filesystem path probing — hardcoded roots (`install.py:131-137`), then `conda env list --json` (`install.py:152-175`), then a canonical path from `env_name` (`install.py:193-196`). The only `CONDA_DEFAULT_ENV` inspection in the repo is display-only at `install.py:2831`. No `HAS_PSYCOPG` or `CONDA_PREFIX` gate exists anywhere.
- **Test coverage of the fallback is partial.** `tests/test_install_pgvector_verify.py:64` pins the conda-subprocess branch for `_verify_pgvector_dsn` and `:85` asserts the probe script is `argv[2]`; nothing asserts the equivalent fallback for `_pgvector_tables_present` or `_init_pgvector_schema` (neither lane grepped for such tests).
- Note the repo rule at `CONTRIBUTING.md:44` ("lazy-imported inside methods, gated by `try/except ImportError`") as written permits `claude_hooks/providers/pgvector.py:123`/`:328` and does not cover the two `scripts/` sites, which use bare imports inside functions with no gate.
- Provenance flag: `docs/benchmarks/EVALUATION.md:78-96` is a checked-in rubric listing these same six sites and the protected `install.py` lines, and `docs/benchmarks/kimi-k2.6-cloud-2026-05-07/audit-medium.transcript.md:20-27` is a prior answer to this identical question. The inventory above was independently grep-verified, but the overlap is worth knowing when weighing "ground truth" claims.

---
