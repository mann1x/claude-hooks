# Consultation transcript — csl-2026-10-27-1126-a256

_Question_: Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python.


## Planner

1. Enumerate every `import psycopg` site with a project-wide grep excluding tests/docs/vendor, and confirm my preliminary count: `claude_hooks/providers/pgvector.py:123` (PgvectorProvider.verify) and `claude_hooks/providers/pgvector.py:328` (connect path), `scripts/migrate_to_pgvector.py:624`, `scripts/bench_recall.py:107`, plus the embedded probe string at `install.py:2092` — classify install.py:2092 as the fallback mechanism itself (runs under conda-env python) rather than a gap.
2. Read `claude_hooks/providers/pgvector.py:110-140` and `:315-345` in full to confirm neither site attempts a conda-env subprocess fallback (compare against the pattern at `install.py:2125-2143`), and capture what each site does on ImportError (verify returns False; connect raises).
3. Glob for `claude_hooks/pgvector_mcp.py` (referenced by `bin/claude-hook-pgvector-mcp.cmd:19`) and grep it for psycopg imports — the MCP server is a plausible additional import site not covered by my two-site count of the package.
4. Establish exercisability per site by tracing entry points: `bin/_resolve_python.sh:32-44` prefers the conda env python (so hook-path invocations of pgvector.py usually land where psycopg exists), while `bin/claude-hook.cmd:43-54` and `bin/claude-hook-pgvector-mcp.cmd:43-47` fall through to system `py -3`/`python` when no conda env exists — determine whether the pgvector provider is even registered/instantiated in that degraded path (check `claude_hooks/dispatcher.py` build_providers and config gating in `claude_hooks/config.py`).
5. For the two scripts sites, check their `if __name__ == "__main__"` / CLI headers and docs (`docs/pgvector-runbook.md`, module docstrings) to determine whether they are operator-run tools where a missing-psycopg crash is acceptable-by-design vs. needing the fallback.
6. Pin down commit 4e67dc2's actual scope via CHANGELOG.md and `git log/show 4e67dc2` if the repo has history, so "still lacks the fallback" is judged against what that commit deliberately covered (the three install.py functions at `install.py:2115`, `install.py:2507`, `install.py:2619`) versus left out.
7. Check `tests/test_pgvector_integration.py` skip logic (psycopg-missing skips) and `tests/test_install_pgvector_verify.py` fallback tests to see whether any test already asserts the fallback pattern is install.py-only, which would signal intent rather than omission for the other sites.

---

## Researcher (round 1)

## Findings

**Import-site inventory** (project-wide grep, excluding tests/docs/vendor — full grep also confirmed no other production sites):

| Site | What happens on ImportError |
|---|---|
| `claude_hooks/providers/pgvector.py:123` (`PgvectorProvider.verify`) | logs warning, returns `False` (pgvector.py:124-126) — no conda-env subprocess fallback |
| `claude_hooks/providers/pgvector.py:328` (`_ensure_ready` connect path) | raises `ImportError("install psycopg: pip install psycopg[binary]")` (pgvector.py:329-330) — no fallback |
| `scripts/migrate_to_pgvector.py:624` | bare `import psycopg` inside main flow, no try/except around it |
| `scripts/bench_recall.py:107` | bare `import psycopg` inside `PgvectorBenchProvider.__init__`, no try/except |
| `install.py:2092` | embedded `_PGVECTOR_VERIFY_SCRIPT` probe string — **this is the fallback mechanism itself**, executed via `subprocess.run([conda_py, "-c", _PGVECTOR_VERIFY_SCRIPT, dsn])` at install.py:2146-2147 |
| `install.py:2127`, `install.py:2524`, `install.py:2647`, `install.py:2546` (string-built), `install.py:2666` (string-built) | all fast-path imports; each is immediately followed by the conda-env subprocess fallback pattern (verified for 2127 at install.py:2138-2147; 2524/2647/2546/2666 are the same fast-path/fallback shape per grep context) — these are the reference implementation, not gaps |

**pgvector_mcp — cleared.** `claude_hooks/pgvector_mcp/` (3 files: `__init__.py`, `__main__.py`, `server.py`) contains **zero psycopg references** (grep: no matches). It imports `PgvectorProvider` at `claude_hooks/pgvector_mcp/server.py:37` and instantiates via `build_providers` (`server.py:35`), so psycopg usage is entirely delegated to the provider's lazy imports — no additional import site.

**Fallback pattern (reference, from 4e67dc2's presumed scope):** `install.py:2115-2143` — `_verify_pgvector_dsn` tries a fast path (import psycopg + call `PgvectorProvider.verify`, install.py:2125-2136), and on ImportError shells out to `find_conda_env_python()` (install.py:2140-2143, subprocess at 2146). Docstring at install.py:2118-2123 explicitly states the rationale: install.py is often run under whatever `python` is on PATH, so verify is pinned to the claude-hooks env's python.

**Exercisability per site from a non-conda-env Python:**

- **`pgvector.py:123` (verify) — exercisable.** Entry paths: `bin/_resolve_python.sh:31-44` prefers `.venv` then conda envs, but falls through to system `python3` (later lines of the 59-line shim); on Windows `bin/claude-hook.cmd:43-54` falls through to `py -3` / `python` with no conda env. `verify()` is also called directly from `install.py:2132` under an arbitrary interpreter — but that call site is already guarded by the fast-path/fallback wrapper. The residual gap: any non-install caller of `PgvectorProvider.verify` under system Python gets a silent `False` rather than a conda-env probe.
- **`pgvector.py:328` (connect) — exercisable, and the sharpest gap.** `claude_hooks/dispatcher.py:115-149` (`build_providers`) instantiates any provider whose config has `enabled: true` + a `dsn` (dispatcher.py:131-136, instantiation at 148) regardless of which interpreter runs the hook. In the degraded Windows path (`claude-hook.cmd:44-53`) or POSIX system-python fallthrough, a user with `providers.pgvector.enabled=true` hits `raise ImportError` at pgvector.py:330 on first recall/store — the hook wrapper may catch it, but the provider is unusable where the conda-env fallback would have worked.
- **`scripts/migrate_to_pgvector.py:624` — operator-run CLI** (bare import in main flow). Exercisable from system Python; a missing psycopg crashes with a raw traceback. No fallback.
- **`scripts/bench_recall.py:107` — operator-run benchmark** (import in `PgvectorBenchProvider.__init__`). Same: exercisable from system Python, raw ImportError, no fallback.
- **`install.py:2092` probe — not a gap**: it only ever runs via the conda-env subprocess at install.py:2146-2147.

**Commit-scope caveat:** I could not verify commit `4e67dc2` directly — no git tooling was available in this session, and `CHANGELOG.md` contains no mention of `4e67dc2`, a psycopg-conda fallback entry, or related wording (grep: no matches). The "reference pattern" is grounded in the code shape at `install.py:2115-2150` (fast-path + `find_conda_env_python` subprocess), which matches the description of the commit; judging whether the two provider sites and two scripts were *deliberately excluded* vs *missed* requires the actual diff — evidence insufficient.

**Test coverage note:** `tests/test_pgvector_integration.py:54-58` skips when psycopg is missing (`_skip_if_no_deps`), so no test exercises the provider under a psycopg-less interpreter; `tests/test_install_pgvector_verify.py` exists and tests the fallback (per grep context showing `patch("install.find_conda_env_python", ...)` at its :110), i.e. the fallback pattern is asserted only for the install.py path, not for the provider/scripts sites.

---

## Researcher (round 1)

## Findings

**Commit scope (4e67dc2):** No git history tool available and grep for `4e67dc2` across the repo returns only consultant-session artifacts — evidence insufficient to pin the commit itself. However, the fallback's deliberate scope is documented in-code: exactly three install.py functions — `_verify_pgvector_dsn` (`install.py:2116-2143`), `_pgvector_tables_present` (`install.py:2514-2546`), `_init_pgvector_schema` (`install.py:2630-2670`) — each with an explicit rationale docstring. Tests cover exactly those three and nothing else: `tests/test_install_pgvector_verify.py:63` (TestSubprocessFallback), `:143-147` (tables fallback, "bug observed on solidpc 2026-05-06"), `:205` (TestInitSchema). **No test asserts the fallback pattern is install.py-only** — the other sites are untested for fallback either way, so omission vs. intent for them is not settled by the test suite.

**Sites lacking the fallback (all confirmed by grep, no others exist outside tests/docs/vendor):**

1. `claude_hooks/providers/pgvector.py:123` — `PgvectorProvider.verify` classmethod: `ImportError` → `log.warning` + `return False` (`:124-126`). No subprocess fallback. Exercisable from non-conda python: **only marginally** — its sole production caller is `install.py:2132`, which sits *inside* the fallback-protected `_verify_pgvector_dsn` fast path, so the installer routes around it. Direct API consumers (tests at `tests/test_pgvector_integration.py:186`) would hit the False return, not a crash. Low severity.

2. `claude_hooks/providers/pgvector.py:328` — `_ensure_ready`: raises `ImportError("install psycopg: ...")` (`:330`). Exercisable from non-conda python: **yes** — `bin/_resolve_python.sh:53-58` falls back to system `python3`/`python` when neither repo venv nor conda env exists, so hook-path invocations can land in a psycopg-less interpreter. Impact is graceful, though: `recall()` catches the ImportError and returns `[]` (`:148-151`), and the project's never-block contract (`CONTRIBUTING.md:45`) means the dispatcher swallows store-side `RuntimeError`s (`:216-217`, `:293-294`).

3. `claude_hooks/pgvector_mcp/server.py:37` — module-level import of `PgvectorProvider` (psycopg itself stays lazy inside the provider). Launched via `python -m claude_hooks.pgvector_mcp` (`claude_hooks/pgvector_mcp/__main__.py:1-3`) or the pip console script `pgvector-mcp` (`pyproject.toml` `[project.scripts]`) — both run in whatever interpreter invokes them. Exercisable from non-conda python: **yes**. Impact bounded by design: handler exceptions become MCP `isError: true` responses, never crash the loop (`server.py:19-21`, `:246-249`). Note the shim `bin/claude-hook-pgvector-mcp` uses `_resolve_python.sh`, so the shim path lands in conda; the pip console-script path does not.

4. `scripts/migrate_to_pgvector.py:624` — bare `import psycopg` in the main migration flow, no try/except → **hard traceback crash** if missing. Operator-run CLI (usage block at `:13-37`). The runbook explicitly instructs installing psycopg into the conda env before running (`docs/pgvector-runbook.md:733-737`), so the crash is acceptable-by-design, though a friendly error would cost 3 lines.

5. `scripts/bench_recall.py:107` — `import psycopg` inside `PgvectorBenchProvider.__init__`, no try/except → **hard crash**. Same operator-tool class as #4; docstring positions it as a benchmark harness (`scripts/bench_recall.py:1-3`).

**Test-side signal:** `tests/test_pgvector_integration.py:54-58` skips the whole integration class when psycopg is missing — the suite never exercises the provider sites from a psycopg-less env, consistent with "provider code is expected to run where psycopg exists."

**Reference pattern for any remediation:** the embedded-probe + `find_conda_env_python()` shape at `install.py:2138-2143` / `install.py:2522-2546`.

**Judgment:** sites 4-5 are acceptable-by-design (operator tools, documented conda prerequisite). Site 2 is the only one where a non-conda path is realistically exercisable (system-python fallback in `_resolve_python.sh`), and it already degrades gracefully — a fallback there would improve recall quality, not prevent crashes. Sites 1 and 3 are protected or self-limiting by their callers.

---

## Synthesizer

**Bottom line: four psycopg import sites lack the conda-env subprocess fallback, but only one is realistically exercisable from a non-conda-env Python in a way that matters — `claude_hooks/providers/pgvector.py:328` — and even there the failure degrades gracefully rather than crashing.** Commit `4e67dc2` itself could not be inspected (no git history available in this session; `CHANGELOG.md` has no mention of it), so "deliberately excluded vs. missed" is judged against the in-code reference pattern at `install.py:2115-2150`, whose docstring (`install.py:2118-2123`) gives the rationale: install.py runs under whatever `python` is on PATH, so its probes are pinned to the claude-hooks conda env.

**Sites lacking the fallback**

- `claude_hooks/providers/pgvector.py:123` — `PgvectorProvider.verify`: ImportError → warning + `return False` (`pgvector.py:124-126`), no fallback. **Exercisable: only marginally.** The sole production caller is `install.py:2132`, which sits inside the fallback-protected fast path of `_verify_pgvector_dsn` — the installer routes around it. Direct API consumers (e.g. tests at `tests/test_pgvector_integration.py:186`) get a silent `False`, not a crash. Low severity.
- `claude_hooks/providers/pgvector.py:328` — `_ensure_ready` connect path: ImportError → `raise ImportError("install psycopg: pip install psycopg[binary]")` (`pgvector.py:329-330`), no fallback. **Exercisable: yes, the sharpest gap.** `bin/_resolve_python.sh:53-58` falls through to system `python3`/`python` when neither repo `.venv` nor the conda env exists (`bin/_resolve_python.sh:31-44` lists only `.venv` and `claude-hooks` conda candidates), so hook-path invocations can land in a psycopg-less interpreter with `providers.pgvector.enabled=true` (providers are built regardless of interpreter via `claude_hooks/dispatcher.py:115` `build_providers`, used by `reflect.py:59`, `consolidate.py:58`, `store_async.py:136`, `pgvector_mcp/server.py:343`). Impact is bounded by design: `recall`/`search` catch the ImportError and return empty (`pgvector.py:148-151`, `265-267`); `store`/`batch_store` convert it to `RuntimeError` (`pgvector.py:216-217`, `293-294`) which the dispatcher swallows per the never-block contract. So a fallback here would improve recall quality, not prevent crashes.
- `scripts/migrate_to_pgvector.py:624` — bare `import psycopg` in the main migration flow, no try/except → **hard traceback** if missing. **Exercisable: yes** from any system Python, but acceptable-by-design: operator-run CLI (usage block at `:13-37`) and `docs/pgvector-runbook.md:733-737` explicitly instructs installing psycopg into the conda env first.
- `scripts/bench_recall.py:107` — `import psycopg` inside `PgvectorBenchProvider.__init__`, no try/except → **hard crash**. **Exercisable: yes** from system Python; same operator-tool class as migrate (docstring at `:1-3` positions it as a benchmark harness). Same acceptable-by-design caveat, though a friendly error would cost ~3 lines per site.

**Not gaps**

- `install.py:2092` (`_PGVECTOR_VERIFY_SCRIPT` probe) — this *is* the fallback mechanism; it only ever executes via the conda-env subprocess at `install.py:2146-2147`. The same fast-path/fallback shape covers the other install.py imports (`install.py:2127`, `2524`, `2647`, `2546`, `2666`).
- `claude_hooks/pgvector_mcp/` — zero psycopg references; it delegates to `PgvectorProvider` (`server.py:37`), so it inherits site #328's behavior. Exercisable from a non-conda interpreter via the pip console-script path (not the shim, which uses `_resolve_python.sh`); failures become MCP `isError` responses, never a crashed loop (`server.py:19-21`).

**Verified vs. unverified**: import sites, ImportError handling, the reference fallback pattern, and interpreter fall-through were verified by direct file reads this session. Unverified: the actual contents/diff of commit `4e67dc2` (no git tooling; CHANGELOG silent), and no test exercises the provider under a psycopg-less interpreter — `tests/test_pgvector_integration.py:54-58` skips the whole integration class when psycopg is missing, and `tests/test_install_pgvector_verify.py` asserts the fallback only for the three install.py functions, so omission vs. intent for the four sites above is not settled by the test suite.

---
