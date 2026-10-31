# Consultation transcript — csl-2026-10-27-1153-34a1

_Question_: Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python.


## Planner

The user asked: audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python.

I'm the planner. I need to write a numbered list of investigation steps for the researcher. I did some verification already:

- `claude_hooks/providers/pgvector.py:123` — `import psycopg` in `verify()` classmethod
- `claude_hooks/providers/pgvector.py:328` — `import psycopg` in `_ensure_ready()`
- Grep for `^import psycopg|from psycopg` in claude_hooks only found those two (with `^\s*` anchor — note: my pattern was `^\s*(import psycopg|from psycopg)` which catches indented imports too, so those two are the only direct psycopg import sites in claude_hooks/).

Wait — actually the grep pattern was `^\s*(import psycopg|from psycopg)` on path `claude_hooks`, and it returned only pgvector.py:123 and pgvector.py:328. But it wouldn't catch things like `import psycopg2` or `from psycopg_pool` — pattern `from psycopg` would match `from psycopg_pool import ...`? No — `from psycopg` matches the prefix of `from psycopg_pool`, yes it would match as a substring... Actually grep matches substrings, so `from psycopg_pool` contains `from psycopg` — it would have matched. It didn't, so no psycopg_pool imports. But `import psycopg2` — `import psycopg` is a substring of `import psycopg2`, so it would have matched too. It didn't. So within claude_hooks/ those two are the only direct import sites.

But the commit 4e67dc2 — I couldn't verify it exists (no git tool available anyway; tools are off now). The "conda-env subprocess fallback" — I found no evidence of it in the grep for "conda" in claude_hooks. The conda mentions were: wrapup_synth.py:478 (hardcoded anaconda path), daemon_ctl.py:41,262 (conda detection in install.py), consultants_forwarder.py (conda env for consultants engine), post_tool_use.py:153-163 (ruff resolution). Nothing that looks like a "psycopg fallback via conda-env subprocess". So the fallback mechanism itself is unverified — it may live in install.py, scripts/migrate_to_pgvector.py, bin/ launchers, or it may not exist in this snapshot.

Also scripts/ has migrate_to_pgvector.py mentioned in pgvector.py:364 comment — scripts/ files might import psycopg too. The user said "claude-hooks" — the audit scope is the claude_hooks package presumably, but scripts and bin could matter for exercisability. The question: "whether it's actually exercisable from a non-conda-env Python" — i.e., can the import site be reached when running under a Python that's not the conda env (e.g., system python where psycopg isn't installed).

From what I saw:
- pgvector.py:123 `verify()` — classmethod, called during detection/installation flow. Exercisable from any Python that instantiates the provider and calls verify; but it's guarded by try/except ImportError returning False gracefully — so the import lacking a fallback means verify just returns False rather than falling back to a conda-env subprocess.
- pgvector.py:328 `_ensure_ready()` — called from recall() (line 148), and presumably store paths. recall() catches ImportError (line 149) and logs "pgvector unavailable". So exercisable from non-conda Python — recall would silently degrade. _ensure_ready is also called in store paths presumably (I saw the tail extension functions at line 395+, "Hybrid recall + KG extensions" — unverified whether they call _ensure_ready).

But wait — is the provider even loaded from a non-conda-env Python? The daemon runs in conda env per consultants_forwarder.py:12 ("the daemon — which already runs in the..."). Memory says claude-hooks is installed in conda env "unsloth" and "surgery". So exercisability depends on entry points: hooks run via bin/claude-hook.cmd etc. — need to check which python bin/ launchers resolve (bin/_resolve_python_consultants.sh mirrors). If all entry points pin the conda env python, then import sites are never hit from non-conda Python in practice — that's the exercisability answer the researcher must verify.

Also the fallback "added in 4e67dc2" — I found no git tools. The researcher may have git access? Tools available to me: survey_project, list_files, read_file, glob, grep, recall_memory. No git. So verifying the commit itself may be impossible; researcher may have the same toolset — so step should be: check CHANGELOG.md / git log via whatever's available (CHANGELOG.md exists at root — I saw it in root-level files). Also grep for "fallback" near psycopg, e.g. a helper like `_psycopg_connect_via_conda` or `subprocess` + `python -c "import psycopg"` pattern.

Plan steps (3-7, concrete, file/symbol pointed):

1. Verify the fallback exists and its shape: grep the repo for the 4e67dc2 change — check CHANGELOG.md at repo root and grep for `subprocess` + `conda` + `psycopg` co-occurrence (e.g., a helper in `claude_hooks/providers/pgvector.py` or `install.py` like a conda-python re-exec); if nothing found, state the fallback is not present in this snapshot (unchecked: no git tool available to read commit 4e67dc2 directly).

2. Enumerate all psycopg import sites repo-wide, not just claude_hooks/: grep `^\s*(import psycopg|from psycopg)` across `scripts/` (esp. `scripts/migrate_to_pgvector.py`, referenced at pgvector.py:364), `install.py`, `tests/`, `bench/`, `vendor/` — my earlier grep covered only `claude_hooks/` and found exactly `claude_hooks/providers/pgvector.py:123` and `:328`.

3. For each hit, determine reachability from non-conda Python: trace entry points — read `bin/claude-hook.cmd`, `bin/claude-hook-pgvector-mcp.cmd`, `run.py`, `run_daemon.py`, `run_proxy.py` to see which interpreter they pin (bin/_resolve_python*.sh pattern per consultants_forwarder.py:348), and whether the daemon (systemd/claude-hooks-daemon.service) hardcodes a conda-env python.

4. For pgvector.py:123 `PgvectorProvider.verify` — confirm callers (grep `\.verify(` / `verify(` on ServerCandidate in install.py / provider registry) and note the ImportError at :124 is swallowed to `return False` — i.e., no fallback, silently fails verification from non-conda Python.

5. For pgvector.py:328 `PgvectorProvider._ensure_ready` — enumerate call sites (`recall` at :148 catches ImportError at :149; check the hybrid extensions at :395+ and `store`/batch insert path) to determine which user-visible operations degrade silently vs. raise when psycopg is missing.

6. Check whether the pgvector provider is even instantiated outside the conda env: grep config `config/claude-hooks.example.json` for pgvector activation and check `claude_hooks/providers/__init__.py` (or registry) for how PgvectorProvider is selected — if it's opt-in via config, non-conda exercisability is conditional on user config.

7. Cross-check the two known import sites against the fallback helper found in step 1 (if any): determine whether :123/:328 predate/miss the fallback, and report each as `file:line — lacks fallback — exercisable yes/no` with the exercisability evidence from steps 3-6.

That's 7 steps. Keep each one sentence. Also note in step 1 that recall_memory shows conda envs "unsloth"/"surgery" host claude-hooks — relevant context but memory, not verified.

Budget is fine. Output numbered list, nothing else. Should I mention what I already verified? The instructions say each step points at WHERE to look; I can encode my findings into the steps (e.g., "confirm the two sites I already found at pgvector.py:123/:328 and check for others in scripts/"). That's good — dense, concrete.

Output: numbered list, nothing else.</think>1. Establish the fallback's shape: grep the repo for the conda-env subprocess fallback (patterns: `conda` near `subprocess`/`python -c`/re-exec, e.g. a helper like `_psycopg`/`_conda_python` in `claude_hooks/providers/pgvector.py` or `install.py`) and check root `CHANGELOG.md` for 4e67dc2 — note: I found no such helper in my `claude_hooks/` conda grep (hits were only `wrapup_synth.py:478`, `daemon_ctl.py:41`, `consultants_forwarder.py:347-359`, `post_tool_use.py:153-163`, none psycopg-related), and no git tool exists here, so if nothing surfaces the commit/fallback must be reported as absent-or-unverifiable in this snapshot.
2. Enumerate psycopg import sites repo-wide, not just the package: grep `^\s*(import psycopg|from psycopg)` across `scripts/` (especially `scripts/migrate_to_pgvector.py`, referenced at `claude_hooks/providers/pgvector.py:364`), `install.py`, `bench/`, `vendor/`, `tests/` — my grep over `claude_hooks/` already found exactly two sites: `claude_hooks/providers/pgvector.py:123` (in `PgvectorProvider.verify`) and `claude_hooks/providers/pgvector.py:328` (in `_ensure_ready`).
3. Trace entry points to decide exercisability from a non-conda Python: read `bin/claude-hook.cmd`, `bin/claude-hook-pgvector-mcp.cmd`, `bin/_resolve_python_consultants.sh` (the resolver pattern `consultants_forwarder.py:347` mirrors), `run.py`, `run_daemon.py`, and `systemd/claude-hooks-daemon.service` to confirm which interpreter each launcher pins — if all pin a conda-env python, the import sites are unreachable from non-conda Python in practice.
4. For `pgvector.py:123`: find its callers (grep `verify(` on `ServerCandidate` in `install.py` and the provider registry) and record that the `ImportError` at `:124` is swallowed to `return False` with only a log line — i.e., from a psycopg-less Python, pgvector verification silently reports "not verified" with no subprocess fallback.
5. For `pgvector.py:328`: enumerate call paths to `_ensure_ready` — `recall()` at `:148` catches `ImportError` (`:149-151`, silent degrade), but check the hybrid/KG extensions block at `pgvector.py:395+` and the store/batch path (`:305-315`) for whether they catch or propagate, to classify each as silent-degrade vs. hard failure from non-conda Python.
6. Check whether the pgvector provider is even activated outside the conda env: read `config/claude-hooks.example.json` and the provider registry/selection code (likely `claude_hooks/providers/__init__.py`) to see if `PgvectorProvider` is opt-in via DSN config — exercisability is conditional on that config, and recall_memory (unverified) says installs live in conda envs `unsloth`/`surgery`.
7. Produce the final audit line per import site — `file:line — fallback present/absent — exercisable from non-conda Python: yes/no/conditional` — cross-referencing each site against the fallback helper from step 1 and the launcher evidence from steps 3-6, flagging any site where the fallback exists but isn't wired into that specific import's `except ImportError` branch.

---

## Researcher (round 1)

Findings:

**Fallback shape (commit 4e67dc2 unverified — no git tool; CHANGELOG.md has no psycopg/conda-fallback entry)**. The fallback exists only in `install.py`, pattern = "fast-path try import, else `subprocess.run([conda_py, '-c', script])`" via `find_conda_env_python()` (`install.py:101`):
- `install.py:2115-2149` — `_verify_pgvector_dsn`: fast path `import psycopg` at `install.py:2127`, fallback shells `_PGVECTOR_VERIFY_SCRIPT` (embedded script with its own import at `install.py:2092`) through the conda python at `install.py:2146-2148`.
- `install.py:2524` — fast path in `_pgvector_tables_present`, subprocess fallback at `install.py:2539-2558`.
- `install.py:2647` — fast path in schema init, fallback at `install.py:2657-2674` (raises RuntimeError if conda env missing).

**Import sites lacking the fallback** (repo-wide grep `^\s*(import psycopg|from psycopg)` — 9 hits, planner's two are confirmed and complete for `claude_hooks/`):
1. `claude_hooks/providers/pgvector.py:123` — `PgvectorProvider.verify()`: no conda/subprocess/fallback anywhere in the file (grep for `conda|subprocess|fallback` in `claude_hooks/providers/pgvector.py` → 0 matches). ImportError swallowed → `return False` (`pgvector.py:124-126`).
2. `claude_hooks/providers/pgvector.py:328` — `_ensure_ready()`: bare import, raises ImportError (`pgvector.py:330`). Exercisable via `recall()` (`pgvector.py:148`), which catches ImportError and degrades to `[]` (`pgvector.py:149-151`).
3. `scripts/migrate_to_pgvector.py:624` — bare `import psycopg` in main, no fallback, no conda mention in file.
4. `scripts/bench_recall.py:107` — bare import in `PgvectorBenchProvider.__init__`, no fallback, no conda mention.
5. `tests/test_pgvector_integration.py:56, 285` — guarded by `_skip_if_no_deps()` skip; test-only, not a production concern.
6. `install.py:2092, 2127, 2524, 2647` — all covered by the fallback (listed for completeness; not gaps).

**Exercisability from non-conda Python**:
- `pgvector.py:123/:328` are reachable from a non-conda interpreter whenever the hooks/daemon resolve to one. `bin/_resolve_python.sh:31-50` prefers repo `.venv`, then conda env `claude-hooks`, but **falls back to system `python3`/`python`** (`bin/_resolve_python.sh:53-58`) when neither exists — so yes, exercisable. Same resolution for the daemon: `systemd/claude-hooks-daemon.service:14` execs `bin/claude-hooks-daemon` (shim-based, no hardcoded conda path). Windows parity: `bin/claude-hook-pgvector-mcp.cmd:28-30` probes conda envs but the chain ends in system python too. Practical impact: under system python, `verify()` returns False (provider appears unreachable during install detection, `install.py:3556`) and `recall()` silently returns `[]` — degraded, not crashed.
- `scripts/migrate_to_pgvector.py:624` and `scripts/bench_recall.py:107` are operator CLIs — fully exercisable from any interpreter; will raise bare ImportError with no conda re-exec if psycopg is missing.
- `claude_hooks/pgvector_mcp/server.py:37,344` imports `PgvectorProvider` (not psycopg directly) — if run under non-conda python, the provider loads but recall degrades as above.

**Verdict**: the "conda-env subprocess fallback added in 4e67dc2" is present only in `install.py` (3 helper sites). The two `claude_hooks/providers/pgvector.py` import sites (`:123`, `:328`) and both `scripts/` sites (`migrate_to_pgvector.py:624`, `bench_recall.py:107`) lack it. Exercisable from non-conda python: yes for all four — hooks resolve to system python when `.venv`/conda env are absent (`bin/_resolve_python.sh:53-58`), and the scripts/CLIs run under whatever `python` invokes them. Severity differs: hooks degrade silently (`recall` → `[]`, `verify` → False), scripts crash with ImportError.

---

## Researcher (round 1)

1. **Fallback helper (4e67dc2) — found in `install.py` only, not in `claude_hooks/`.** Three conda-env subprocess fallbacks exist: `_verify_pgvector_dsn` (fast path `install.py:2127`, fallback shells out via `find_conda_env_python()` at `install.py:2140-2147`), `_pgvector_tables_present` (`install.py:2524` fast path, fallback `install.py:2540-2562`), `_init_pgvector_schema` (`install.py:2647` fast path, fallback `install.py:2657-2676`). Docstrings explicitly state the rationale: install.py often runs on system py3 without psycopg (`install.py:2118-2123`, `install.py:2515-2520`). Test coverage confirms the two-path design (`tests/test_install_pgvector_verify.py:5-8`). Commit 4e67dc2 itself is unverifiable here — no git tool; CHANGELOG.md has no hash or "fallback" entry matching it (grep hit only an unrelated HyDE model-fallback line, `CHANGELOG.md:309`).
2. **Full psycopg import-site inventory** (grep `^\s*(import psycopg|from psycopg)` repo-wide): `claude_hooks/providers/pgvector.py:123`, `:328`; `install.py:2092` (inside the `_PGVECTOR_VERIFY_SCRIPT` string executed under the conda subprocess, so psycopg is expected there by construction), `:2127`, `:2524`, `:2647`; `scripts/migrate_to_pgvector.py:624`; `scripts/bench_recall.py:107`; `tests/test_pgvector_integration.py:56`, `:285` (test-only, not audited in depth).
3. **`claude_hooks/providers/pgvector.py:123` (`verify`) — lacks fallback — exercisable: yes.** ImportError is swallowed to `return False` with only a log warning (`pgvector.py:124-126`) — from a psycopg-less interpreter, pgvector verification silently reports "not verified". The installer-side caller is insulated (install.py's fast path only calls `PgvectorProvider.verify` after its own psycopg import succeeded, `install.py:2127-2132`), but direct callers are not: `tests/test_pgvector_integration.py:186`, and the code graph shows edges from `verify` into `claude_hooks/caliber_proxy/tools.execute` (`graphify-out/graph.json:26732-26739`).
4. **`claude_hooks/providers/pgvector.py:328` (`_ensure_ready`) — lacks fallback — exercisable: conditional (pgvector is opt-in: `config/claude-hooks.example.json:93` `"enabled": false`).** Degradation varies by caller: `recall()` :148, `store()` :214, `batch_recall()` :265, hybrid recall :439 all catch `(ImportError, EmbedderError)` and silently degrade (`pgvector.py:149-151`, `:216`, `:266-268`, `:440-441`); `batch_store()` converts it to `RuntimeError` (`pgvector.py:293-294`); the four KG extension methods call `self._ensure_ready()` bare with no try/except — `_kg_create_entities` :555, `_kg_add_observations` :592, `_kg_create_relations` :645, `_kg_search_nodes` :676 — so ImportError propagates as a hard failure there.
5. **`scripts/migrate_to_pgvector.py:624` — bare `import psycopg` in `main()`, no fallback — exercisable: yes** (operator-run CLI, any interpreter); raises ImportError uncaught.
6. **`scripts/bench_recall.py:107` — `import psycopg` in `PgvectorBenchProvider.__init__`, no fallback — exercisable: yes**; raises ImportError uncaught.
7. **Non-conda exercisability of the hook path:** `bin/claude-hook:16-18` and `bin/claude-hook-pgvector-mcp:10` source `bin/_resolve_python.sh`, whose comment says it "Probes repo-local .venv, then conda env, then system python" — meaning hooks CAN run under system Python when no conda env exists, making both pgvector.py sites reachable from non-conda Python. Caveat: my read of `_resolve_python.sh` itself was rejected (tools off), so the probe order is evidenced only by the shim comment at `bin/claude-hook:16-17`.
8. **Memory (unverified, not tool-checked):** recall_memory indicates claude-hooks installs live in conda envs "unsloth"/"surgery" — if accurate, real-world exercisability of the two pgvector.py sites is further reduced, but this is memory, not verified evidence.
9. **Bottom line per site:** `pgvector.py:123 — no fallback — yes (silent false)`; `pgvector.py:328 — no fallback — conditional (silent degrade on recall/store/batch_recall/hybrid; RuntimeError on batch_store; uncaught raise on all four KG ops)`; `migrate_to_pgvector.py:624 — no fallback — yes (raise)`; `bench_recall.py:107 — no fallback — yes (raise)`; `install.py:2127/:2524/:2647 — fallback present`; `install.py:2092 — runs under conda python by construction`.

---

## Synthesizer

Let me synthesize the final answer. I have two researcher reports plus my own tool verifications:

Verified by me directly:
- `claude_hooks/providers/pgvector.py:123` — `import psycopg` in `verify()`, ImportError swallowed → `return False` (lines 122-126). Confirmed by read.
- `claude_hooks/providers/pgvector.py:328` — `import psycopg` in `_ensure_ready()`, ImportError → raises ImportError with pip hint (lines 329-330). Confirmed by read.
- Repo-wide grep `^\s*(import psycopg|from psycopg)` → exactly 10 hits: install.py:2092, 2127, 2524, 2647; pgvector.py:123, 328; tests/test_pgvector_integration.py:56, 285; scripts/migrate_to_pgvector.py:624; scripts/bench_recall.py:107. Confirmed — no other sites.
- `bin/_resolve_python.sh` full read — confirms probe order: repo .venv → conda env `claude-hooks` → system python3/python fallback (lines 53-58). So hooks CAN run under system Python when neither .venv nor conda env exists. Confirmed exercisability.
- Grep `conda|subprocess` in `claude_hooks/` → no conda-subprocess psycopg fallback anywhere in the package; the only conda mentions are unrelated (wrapup_synth.py:478 pytest path, daemon_ctl.py:41/262 comments, consultants_forwarder.py conda env for consultants engine, post_tool_use.py:153-163 ruff). Confirmed pgvector.py has no conda/subprocess/fallback.
- `recall()` at pgvector.py:144-151 catches (ImportError, EmbedderError) → returns [] — verified by read.
- `store()` at pgvector.py:210-217 catches (ImportError, EmbedderError) → raises RuntimeError — verified by read.
- `batch_recall()` at pgvector.py:264-268 catches → returns empty lists — verified by read.
- `batch_store()` at pgvector.py:291-294 → raises RuntimeError — verified by read.
- `count()` at pgvector.py:239-243 catches Exception → returns 0 — verified by read.

From researcher reports (not directly verified by me but reported with citations):
- install.py fallback sites: install.py:2127 fast path with fallback via find_conda_env_python() at install.py:2140-2147 (lane 1) / 2146-2148 (lane 0); _pgvector_tables_present install.py:2524 fast path, fallback 2539-2558/2540-2562; _init_pgvector_schema install.py:2647 fast path, fallback 2657-2674/2657-2676. Both lanes agree on the shape. I attempted to read install.py:2115-2150 but tools were off — so install.py internals are researcher-reported, not my-verified. I should mark that or just cite as reported. The citation integrity rule says every path:line must appear verbatim in a researcher report or tool result — these do appear in researcher reports, so they're citable. My verification level: the two pgvector.py sites and the grep inventory and _resolve_python.sh are tool-verified by me; install.py fallback details are researcher-reported (two lanes agreeing).
- KG extension methods call _ensure_ready bare with no try/except: _kg_create_entities :555, _kg_add_observations :592, _kg_create_relations :645, _kg_search_nodes :676 (lane 1). I didn't verify these myself — researcher-reported. Actually I could have checked but tools are off now. I'll relay as reported. Hmm — the instructions say rank defects only when checked. Lane 1 read these lines presumably. The critic's verdict — wait, there's no critic verdict in this conversation. The prompt says "consuming the planner's plan, researcher's report(s), and critic's verdict" but I only see planner + two researcher reports. No critic report provided. So no corrections to apply.
- scripts/migrate_to_pgvector.py:624 — bare import in main(), no fallback, uncaught ImportError (both lanes).
- scripts/bench_recall.py:107 — import in PgvectorBenchProvider.__init__, no fallback (both lanes).
- tests/test_pgvector_integration.py:56, 285 — test-only, guarded by skip.
- config/claude-hooks.example.json:93 `"enabled": false` — pgvector opt-in (lane 1).
- install.py:2092 — inside _PGVECTOR_VERIFY_SCRIPT string executed under conda python by construction (both lanes).
- verify() caller insulation: install.py fast path calls PgvectorProvider.verify only after its own psycopg import succeeded (install.py:2127-2132, lane 1). Also tests/test_pgvector_integration.py:186 calls verify.
- Commit 4e67dc2 itself unverifiable — no git tool; CHANGELOG.md has no matching entry (both lanes agree).

Line-number discrepancies between lanes: lane 0 said fallback at install.py:2146-2148, lane 1 said 2140-2147; lane 0 said 2539-2558, lane 1 said 2540-2562; both 2657-2674/2657-2676. These are minor ranges for the fallback body — I'll cite the fast-path lines which both lanes agree on (2127, 2524, 2647) and describe the fallback without pinning conflicting ranges, or name the ranges as researchers disagree. Simpler: cite install.py:2127 / :2524 / :2647 (agreed) and mention find_conda_env_python() at install.py:101 (lane 0). For the fallback body ranges, I can say "fallback shells the script through the conda python (install.py:2140-2147 / 2146-2148 — researchers disagree on exact range)". That's per instructions: name both. Or I could just omit the precise body range since the fast-path line + description suffices. The instruction says if researchers disagree, name both. I'll do it once compactly.

Also lane 0 noted scripts/migrate_to_pgvector.py:624 "[no recall at this line; line is in main]" — that's a caveat annotation in the report itself. Both lanes say it's in main(). Fine.

Exercisability summary:
- The hook path: bin shims source bin/_resolve_python.sh which falls back to system python3/python (bin/_resolve_python.sh:53-58) when no .venv and no conda env claude-hooks exists. So yes — the pgvector.py sites are exercisable from a non-conda Python, conditional on (a) pgvector provider being enabled in config (config/claude-hooks.example.json:93 default false) and (b) no .venv/conda env present (or CLAUDE_HOOKS_PY override pinning a non-conda interpreter — bin/_resolve_python.sh:25-28).

Actually note: exercisability from non-conda Python — the resolver prefers .venv first. If a .venv exists without psycopg, that's also "non-conda-env Python" — the question specifically says non-conda-env Python, which includes .venv and system python. So exercisability is even broader: any .venv without psycopg counts. The resolver's preference order means a .venv without psycopg is actually the more likely scenario than system fallback. Good point to include.

Per-site verdict table:

1. `claude_hooks/providers/pgvector.py:123` (verify) — NO fallback. Exercisable: yes, but consequence is silent False (verified by my read: lines 124-126). Caller context: installer path insulates it (install.py:2127-2132 per lane 1); direct callers (tests:186) get silent False.
2. `claude_hooks/providers/pgvector.py:328` (_ensure_ready) — NO fallback. Exercisable: conditional on provider enabled. Outcomes by caller (verified by my reads for recall/store/batch_recall/batch_store/count; researcher-reported for KG methods):
   - recall :148-151 → silent []
   - store :213-217 → RuntimeError
   - batch_recall :264-268 → silent empty
   - batch_store :291-294 → RuntimeError
   - count :239-243 → silent 0
   - KG extensions :555/:592/:645/:676 → uncaught ImportError (researcher-reported, not re-verified by me)
3. `scripts/migrate_to_pgvector.py:624` — NO fallback, uncaught ImportError in main(). Exercisable: yes (operator CLI, runs under whatever python invokes it).
4. `scripts/bench_recall.py:107` — NO fallback, uncaught ImportError in __init__. Exercisable: yes.
5. `tests/test_pgvector_integration.py:56, 285` — test-only, skip-guarded; not a production gap.
6. `install.py:2092` — inside the embedded verify script executed under the conda python by construction — not a gap.
7. `install.py:2127, 2524, 2647` — fallback PRESENT (the 4e67dc2 mechanism lives here only).

Commit 4e67dc2: unverifiable in this snapshot — no git tool; CHANGELOG.md has no matching entry (only unrelated HyDE fallback line at CHANGELOG.md:309 per lane 1). The fallback mechanism described (conda-env subprocess via find_conda_env_python) exists only in install.py.

What I meant to check but couldn't: install.py:2115-2150 direct read (tools cut off) — so install.py fallback internals are two-lane researcher-reported, not independently verified. Also the KG call sites :555/:592/:645/:676 not re-verified by me.

Format: list-shaped question → bulleted list, lead with bottom line. No markdown headings unless 3+ sections... I have essentially: bottom line + per-site list + exercisability + caveat. I can do a compact list. Keep it dense.

CONFIDENCE line at end. Confidence: high on the two pgvector sites and inventory (my own grep/read), high on resolver behavior (my read), medium on install.py fallback internals (researcher-reported, two lanes agree on fast-path lines), medium on KG call sites (single lane). Overall ~0.85.

Let me write it. Lead: The fallback exists only in install.py; four psycopg import sites lack it — pgvector.py:123, pgvector.py:328, migrate_to_pgvector.py:624, bench_recall.py:107 — and all four are exercisable from a non-conda-env Python, though the hook-path ones degrade silently rather than crash.</think>**Bottom line: the conda-env subprocess fallback exists only in `install.py`; four psycopg import sites still lack it — `claude_hooks/providers/pgvector.py:123`, `claude_hooks/providers/pgvector.py:328`, `scripts/migrate_to_pgvector.py:624`, `scripts/bench_recall.py:107` — and all four are exercisable from a non-conda-env Python.** Commit 4e67dc2 itself could not be verified (no git tool available; `CHANGELOG.md` has no matching entry — only an unrelated HyDE model-fallback line at `CHANGELOG.md:309`).

Per-site audit (repo-wide grep `^\s*(import psycopg|from psycopg)` returns exactly 10 hits; nothing else exists):

- `claude_hooks/providers/pgvector.py:123` — `PgvectorProvider.verify()`. **No fallback** (grep for `conda|subprocess` in `pgvector.py` → 0 matches; the only conda/subprocess hits in `claude_hooks/` are unrelated modules). Exercisable: **yes** — `bin/_resolve_python.sh:53-58` falls back to system `python3`/`python` when neither repo `.venv` nor conda env `claude-hooks` exists, and `.venv` (preferred at `bin/_resolve_python.sh:31-34`) may itself lack psycopg. Effect: ImportError swallowed → `return False` with a warning (`pgvector.py:124-126`) — silent "not verified", no crash. Mitigating context: the installer only calls `verify()` after its own fast-path import succeeded (`install.py:2127-2132`), so the silent-False path mainly hits direct callers (e.g. `tests/test_pgvector_integration.py:186`).
- `claude_hooks/providers/pgvector.py:328` — `_ensure_ready()`. **No fallback**; raises `ImportError("install psycopg: …")` (`pgvector.py:329-330`). Exercisable: **conditional** — pgvector is opt-in (`config/claude-hooks.example.json:93` `"enabled": false`). Impact varies by caller: `recall()` and `batch_recall()` silently degrade to `[]` (`pgvector.py:147-151`, `:264-268`); `count()` silently returns 0 (`pgvector.py:239-243`); `store()` and `batch_store()` convert to `RuntimeError` (`pgvector.py:213-217`, `:291-294`); the four KG extension methods call `_ensure_ready()` bare with no try/except — `_kg_create_entities` :555, `_kg_add_observations` :592, `_kg_create_relations` :645, `_kg_search_nodes` :676 — so ImportError propagates uncaught there (researcher-reported; I did not independently re-read those four lines).
- `scripts/migrate_to_pgvector.py:624` — bare `import psycopg` in `main()`. **No fallback**, no conda logic in the file. Exercisable: **yes** — operator CLI running under whatever interpreter invokes it; uncaught ImportError if psycopg is missing.
- `scripts/bench_recall.py:107` — `import psycopg` in `PgvectorBenchProvider.__init__`. **No fallback**. Exercisable: **yes**; uncaught ImportError.
- `tests/test_pgvector_integration.py:56`, `:285` — test-only, skip-guarded; not a production gap.
- `install.py:2127`, `:2524`, `:2647` — fallback **present** (the 4e67dc2 mechanism): fast-path try-import, else shell an embedded script through the conda-env python located by `find_conda_env_python()` (`install.py:101`; fallback bodies reported at `install.py:2140-2147` / `:2146-2148` and `:2539-2558` / `:2540-2562` — researchers disagree on the exact ranges). `install.py:2092` is inside the embedded script executed under the conda python by construction — not a gap.

Caveats: the `install.py` fallback internals and the four KG call sites are researcher-reported (both lanes agree on the fast-path lines); my direct read of `install.py:2115-2150` was cut off before completion, so those details rest on the two concordant reports rather than my own file read. The `pgvector.py` sites, the import inventory, and the resolver fallback (`bin/_resolve_python.sh:53-58`) I verified directly.

---
