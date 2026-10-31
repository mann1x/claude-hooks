---
session_id: csl-2026-10-27-1153-34a1
created: "2026-10-27T11:53:29"
question: "Audit claude-hooks for psycopg import sites that still lack the conda-env subprocess fallback added in 4e67dc2 — list each file:line and whether it's actually exercisable from a non-conda-env Python."
models:
  planner: "glm-5.3-flash:cloud"
  researcher: "glm-5.3-flash:cloud"
  synthesizer: "glm-5.3-flash:cloud"
topology: council
effort: medium
duration_seconds: 86.78
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-10-27/tmp/claude-hooks-bench-bench-baseline-2026-05-07-GKFg
---

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
