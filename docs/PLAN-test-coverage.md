# Plan: close the test-coverage gaps from the 2026-04-14 audit

**Audited commit:** `220e18b`
**Status as of 2026-04-14:** **PLAN COMPLETE.** 412 passed, 16 skipped
(up from 113 at plan start). Branch coverage **91.8 %** on
`claude_hooks/` (target ≥ 80 %; final pass overshot to ~92 %).

**Completed commits (all on `origin/main`):**

- `7940e6e` — Phase 0 fixtures + Phase 1 (dedup, decay, embedders, 65 tests)
- `ce794f1` — Phase 2 (hyde, recall, 38 tests)
- `49f9f6a` — Phase 3 (4 hook handlers, 28 tests)
- `841ef6e` — Phase 4 instincts + Phase 7 (stop_guard escape, /wrapup skill)
- `9cc357f` — Phase 4 reflect/consolidate + Phase 5 coverage gate + Phase 6 README
- this commit — Phase 8 (low-coverage modules; +120 tests, 81.3 % → 91.8 %)

**Phase status:**

| Phase | Status | Tests added |
|-------|--------|-------------|
| 0 — fixtures & mocks | done | — |
| 1 — dedup / decay / embedders | done | 48 |
| 2 — hyde / recall | done | 38 |
| 3 — hook handlers | done | 28 |
| 4 — reflect / consolidate / instincts | done | 41 |
| 5 — coverage gate (≥ 80 % branch) | done | — |
| 6 — README test-section refresh | done | — |
| 7 — stop_guard user-intent escape + /wrapup skill | done | 7 |
| 8 — lift low-coverage modules to ≥ 90 % overall | done | 120 |

---

## Guiding principles

1. **Never hit the network.** Every external call (Ollama, MCP, HTTP,
   subprocess) is mocked at the boundary. Where a module already has
   a dedicated helper (`_call_ollama`, `subprocess.run`, `urllib.request.urlopen`),
   we patch the helper; otherwise we patch `urllib.request.urlopen`.

2. **Never hit the filesystem outside tmpdir.** Tests that touch
   state files (`decay.py`, `instincts.py`, `consolidate.py`) use
   `tempfile.TemporaryDirectory`. No writes under `~/.claude`.

3. **Test observable behaviour, not internals.** For each module,
   the minimum table is: happy path, empty/degenerate input, and
   each documented failure mode. We don't fuzz regexes.

4. **Speed budget: < 3 s.** Full suite target stays well under 5 s.
   If a test needs more, it's an integration test and moves to a
   `@unittest.skipUnless` gated file.

5. **Shared fixtures in `tests/conftest.py`.** Hook events, transcript
   JSONL fragments, fake providers, and the "base config with X
   enabled" dicts live there.

---

## Phase 0 — shared infrastructure (~45 min)

Before writing any module tests, land the scaffolding once:

### 0.1 `tests/conftest.py` (new)

- `fake_transcript(user_msg, asst_msg, tools=[])` — builds the JSONL
  transcript shape the hooks read. Parameterises the trivial-vs-
  noteworthy axis (Bash/Edit vs TaskList only).
- `fake_provider(name, recall_returns=[], store_errors=False)` —
  minimal `Provider`-compatible stub with `recall()`/`store()`
  returning fixed data. Avoids the ServerCandidate dance.
- `base_config(**overrides)` — deep-copies `DEFAULT_CONFIG` and
  applies keyword overrides. Turns off `safety_log_enabled` by
  default (no writes to `~/.claude/permission-scanner`).
- `tmp_claude_home(monkeypatch)` — patches `claude_hooks.config.
  expand_user_path` so any `~/.claude/...` path resolves under a
  tmpdir, making state-file tests hermetic.

### 0.2 `tests/mocks/ollama.py` (new)

Single source of truth for Ollama HTTP mocking:
- `mock_ollama_generate(response_text, fail=False)` — context
  manager that patches `urllib.request.urlopen` for the
  `/api/generate` endpoint.
- `mock_ollama_embeddings(vector, fail=False)` — same for
  `/api/embeddings`.

### 0.3 `tests/mocks/mcp.py` (new)

- `FakeMcpProvider` — in-memory provider implementing `recall`
  and `store` with a simple list backend. Reused by recall-pipeline
  and stop-handler tests.

**Deliverable:** new files, 0 behaviour change in product code, all
113 existing tests still pass.

---

## Phase 1 — core memory helpers (library unit tests, ~90 min)

The smallest modules first. Purely data-shape logic with no hooks or
network involvement.

### 1.1 `tests/test_dedup.py` (~8 tests, 57 LOC covered)

Functions under test: `text_similarity`, `should_store`.

Cases:
- `text_similarity`
  - identical strings → 1.0
  - completely disjoint strings → 0.0 (or very close)
  - substring → somewhere between
  - empty string → 0.0 (not division-by-zero)
  - unicode / non-ASCII doesn't crash
- `should_store`
  - empty store list → True
  - new text below threshold → True
  - new text above threshold → False
  - mixed list: returns False on first above-threshold hit
  - provider raising on `recall()` → degrades to True (don't block storage)

### 1.2 `tests/test_decay.py` (~10 tests, 156 LOC covered)

Functions under test: `memory_hash`, `update_recalled`, `apply_decay`,
`_recency_boost`, `_frequency_boost`, `_load_history`,
`_save_history`, `_prune_old`.

Cases:
- `memory_hash`: stable across same input, differs on whitespace
  vs real content change
- `update_recalled`: increments count, records timestamp
- `_recency_boost`: monotone decreasing with age, bounded in [0, 1],
  returns 1.0 for "just now"
- `_frequency_boost`: honours `cap`, monotone up to cap
- `apply_decay`: reorders a list, respects halflife, no-op when
  history empty
- `_load_history` / `_save_history`: atomic write semantics in
  tmpdir; corrupt file → empty history (don't crash)
- `_prune_old`: drops entries older than `N` days, preserves newer

### 1.3 `tests/test_embedders.py` (~9 tests, 152 LOC covered)

Classes: `NullEmbedder`, `OllamaEmbedder`, `OpenAiCompatibleEmbedder`,
factory `make_embedder`.

Cases:
- `NullEmbedder.embed` raises `EmbedderError`
- `make_embedder("ollama", opts)` → `OllamaEmbedder` instance
- `make_embedder("openai", opts)` → `OpenAiCompatibleEmbedder`
- `make_embedder("unknown", ...)` → `NullEmbedder`
- `OllamaEmbedder.embed` with mocked HTTP returning a vector →
  returns that vector
- `OllamaEmbedder.embed` on connection refused → `EmbedderError`
  with meaningful message
- `OllamaEmbedder.embed` on non-JSON response → `EmbedderError`
- `OpenAiCompatibleEmbedder.embed` handles `data[0].embedding`
  envelope shape
- `OpenAiCompatibleEmbedder.embed` on HTTP 500 → `EmbedderError`

---

## Phase 2 — HyDE and the recall pipeline (~90 min)

These are used by every `UserPromptSubmit` so correctness matters.

### 2.1 `tests/test_hyde.py` (~12 tests, 202 LOC covered)

Functions: `expand_query`, `expand_query_with_context`,
`_call_ollama`, `_format_context`.

Cases:
- `expand_query` with mocked Ollama returning text → returns that
  text stripped
- `expand_query` on timeout → returns original prompt
- `expand_query` on connection refused → returns original prompt
- `expand_query` with empty prompt → returns empty prompt early
- `expand_query` fallback path: primary fails, fallback succeeds
- `expand_query` both fail → returns original prompt
- `expand_query_with_context` with grounding memories → includes
  them in the request body and strips `<think>` blocks from output
- `_format_context` respects `max_chars` cap and per-entry cap
- `_call_ollama` sends `keep_alive` in request body
- `_call_ollama` honours `num_predict` → `max_tokens`
- `_call_ollama` sets `think=False`
- Non-JSON response doesn't crash — returns empty string

### 2.2 `tests/test_recall.py` (~10 tests, 244 LOC covered)

Functions: `run_recall`, `_hyde_expand`, `_gather_snippets`,
`_format_additional_context`, `_grounded_recall`.

Cases:
- `run_recall` with no providers → returns None
- `run_recall` with one provider returning hits → additionalContext
  contains all snippet texts and the provider label
- `run_recall` honours `max_total_chars` by truncating snippets
- `run_recall` with empty query → returns None
- `run_recall` with dedup enabled → skips near-duplicate snippets
- `run_recall` with HyDE enabled but no raw recall → skips HyDE
  (grounded short-circuit)
- `run_recall` with HyDE enabled, raw recall hits present →
  second provider call uses expanded query
- `_format_additional_context` includes OpenWolf block when
  `include_openwolf=True` and project has a `.wolf` dir
- `_format_additional_context` omits OpenWolf when not a wolf
  project
- Provider raising `recall()` → continues with next provider

Mocks HyDE via `claude_hooks.hyde.expand_query` patched at the
recall module's import site.

---

## Phase 3 — hook handlers (~90 min)

End-to-end integration tests for the three handlers currently lacking
coverage. Pattern mirrors `test_pre_tool_use_handler.py`.

### 3.1 `tests/test_user_prompt_submit_handler.py` (~8 tests)

- Disabled in config → returns None
- Prompt shorter than `min_prompt_chars` → returns None
- No providers configured → returns None
- Happy path with fake provider → emits `additionalContext` JSON
- HyDE enabled + grounded + empty raw recall → short-circuits
  (no hyde call on the second pass)
- OpenWolf cwd injects the Do-Not-Repeat block
- Provider that raises gets logged and skipped, others continue
- Decay enabled — `update_recalled()` called on returned hits

### 3.2 `tests/test_session_start_handler.py` (~8 tests)

- Disabled → returns None
- No providers → returns None, still fires claudemem stale-check
- Status line format: `_Started with claude-hooks recall enabled …_`
- `source="compact"` with `compact_recall=True` + recall hits →
  status line + recalled block joined
- `source="resume"` uses "Resumed" in the status line
- `claudemem_reindex.enabled=false` → stale-check NOT called
- `claudemem_reindex.enabled=true` + cwd outside a git repo →
  stale-check called, returns silently (mock verifies)
- Handler never raises on claudemem import failure

### 3.3 `tests/test_session_end_handler.py` (~6 tests)

- Disabled → returns None
- Episodic client mode with no server URL → logs, returns None
- Episodic client mode with server URL: `_push_transcript()` called
  with correct payload
- Episodic server mode → `_local_sync()` called (but not
  `_push_transcript`)
- Transcript file missing → degrades gracefully
- HTTP failure on push → caught, hook exits 0

### 3.4 `tests/test_stop_handler_store.py` (~10 tests, complements
  existing `test_stop_guard.py`)

These hit the memory-store half of `hooks/stop.py` that stop_guard
tests don't touch.

- Disabled → returns None
- `store_threshold=off` → returns None
- `store_threshold=noteworthy` + no tool calls → returns None
  (not noteworthy)
- `store_threshold=noteworthy` + Edit call → stores summary to
  every `store_mode=auto` provider
- `store_threshold=always` + trivial turn → stores anyway
- Meta-prompt detection (the fix from `472e220`) — prompt matching
  `extract reusable operational lessons` is filtered out of the
  summary
- Dedup threshold 0.85 + near-duplicate in provider → skips store
- `classify_observations=true` → metadata has `observation_type`
  key with expected value for a "fix" turn
- Stop_guard blocks stop → decision:block, store does NOT run
- Claudemem reindex fires when turn_modified=true and project has
  `.claudemem/` (verified via patched `reindex_if_dirty_async`)

---

## Phase 4 — periodic maintenance modules (~60 min)

Lower priority because they're invoked manually (via CLI commands)
rather than on every turn, but still public surface.

### 4.1 `tests/test_reflect.py` (~8 tests, 220 LOC covered)

`reflect.py` builds the `/reflect` skill payload — summarises Qdrant
memories into suggested CLAUDE.md rules.

Cases:
- Disabled → returns None
- With mocked fake-provider returning <`min_pattern_count` memories
  → returns None (nothing to synthesise)
- With enough memories + mocked Ollama call → writes expected
  CLAUDE.md content under a tmpdir
- Ollama failure → returns with a warning, no file write
- Respects `max_memories_to_analyze` cap
- Detects project-local vs user-global output path
- Handles empty-string Ollama response
- Dedup: identical memories collapsed before synthesis

### 4.2 `tests/test_consolidate.py` (~8 tests, 217 LOC covered)

Similar shape to reflect — cleanup skill for dedup/prune.

Cases:
- Disabled → no-op
- `trigger=manual` with no explicit invocation → no-op
- `min_sessions_between_runs` cooldown honoured via state file
- Similarity threshold 0.80: two near-duplicates merged
- `prune_stale_days=90` prunes old memories
- Provider rejects a delete → continues with others
- State file corruption → treated as "never run"
- End-to-end dry-run: Ollama mocked, tmp state file, verify the
  decision list written to state

### 4.3 `tests/test_instincts.py` (~6 tests, 196 LOC covered)

`instincts.py` extracts "instinct" rules from assistant messages.

Cases:
- Non-instinct message → no extraction
- Explicit instinct pattern ("always do X") → extracted rule
- Multiple instincts in one message → all captured
- Persistence to `instincts_dir` via tmpdir
- Duplicate detection — same instinct not written twice
- Disabled → no I/O

---

## Phase 5 — coverage gate & CI pass (~30 min)

### 5.1 Coverage measurement

Add `coverage.py` dev dep. Target branch coverage ≥ 80% on
`claude_hooks/` (excluding `providers/` which is already covered by
integration tests, and `detect.py` / `install.py` which are
install-time only).

Add `pyproject.toml` entry:
```toml
[tool.coverage.run]
source = ["claude_hooks"]
omit = [
    "claude_hooks/providers/pgvector.py",   # integration test covered
    "claude_hooks/providers/sqlite_vec.py", # integration test covered
    "claude_hooks/detect.py",               # install-time
]
```

Add to README test section:
```bash
pip install coverage
coverage run -m pytest tests/
coverage report
```

### 5.2 CI check (documentation only — user runs manually)

README section: "Before merging, please run `python3 -m pytest tests/`
and confirm 0 failures plus ≥ 80% coverage." Matches the repo's
existing `pytest-in-conda` workflow.

---

## Phase 6 — documentation & follow-up (~15 min)

- Update README "Tests" section: list the new test files and what
  each covers in a table.
- Note in `docs/PLAN-code-factory-integration.md` (the earlier plan)
  that test gaps are now tracked in this document.

---

## Effort summary

| Phase | Deliverable | Tests | Est. time |
|-------|-------------|-------|-----------|
| 0 | Shared fixtures & mocks | — | 45 min |
| 1 | dedup, decay, embedders | 27 | 90 min |
| 2 | hyde, recall | 22 | 90 min |
| 3 | 4 hook handlers | 32 | 90 min |
| 4 | reflect, consolidate, instincts | 22 | 60 min |
| 5 | coverage measurement | — | 30 min |
| 6 | docs | — | 15 min |
| **Total** | **11 new test files, 103 tests** | **103** | **≈ 6 h** |

Projected suite after this plan lands: **~216 passed, 16 skipped,
overall coverage ~85%.**

---

## Ordering constraints

Phase 0 must land first (everything after uses `conftest.py`). After
that the phases are independent — can be done in parallel, paused,
or shipped as separate commits per phase. Recommendation: one PR per
phase, each self-contained.

## Explicit non-goals

- Property-based / fuzz testing — diminishing returns at this
  codebase size.
- Real-network integration tests for Ollama or MCP — we already
  have `test_pgvector_integration.py` and `test_sqlite_vec_integration.py`
  which skip when deps absent; the pattern is fine as-is.
- `install.py` and `detect.py` — install-time only, tested manually
  when running the installer.
- `providers/qdrant.py` and `providers/memory_kg.py` — covered by
  `test_providers.py`'s integration tests plus live MCP calls from
  the running fleet.
- Benchmarking — handled separately when a perf regression surfaces.

## Risk notes

- **conftest.py adds ~50 LOC of fixture code.** If the fixtures are
  wrong, many tests fail simultaneously. Mitigation: write and land
  Phase 0 with a trivial smoke test for each fixture before moving
  on.
- **Mocking `urllib.request.urlopen` is brittle** when modules
  import it at module scope vs function scope. Check each module's
  import style before mocking (some use `import urllib.request`
  and call `urllib.request.urlopen(...)`; others do
  `from urllib.request import urlopen`).
- **Caliber pre-commit and claudemem-reindex hooks fire during
  test development** — they don't run tests themselves but they
  will reindex and log after each commit. No action needed, just
  expected.

---

## Phase 7 — stop_guard user-intent escape + `/wrapup` skill (NEW)

### Background

The original `stop_guard` (commit `24f69b7`) had two escapes:
quoted-span stripping and meta-marker phrases. Both triggered on the
*assistant's* message content. A third failure mode emerged in
practice: when the **USER** explicitly asks to wrap up ("compact the
context", "save state", "we'll continue another time"), the assistant's
compliant response often contains phrases that match the guard's
own pattern list ("next session", "continue later", "wrap up"),
blocking the legitimate stop.

Example incident (2026-04-14):

```
USER: I need to compact the context

ASSISTANT: ... When you start the next session ...

→ stop_guard FIRES: "There is no 'next session.' Sessions are
  unlimited. Continue working."
```

The assistant was following orders; the guard was wrong.

### Fix (already landed locally, pending commit)

Added to `claude_hooks/stop_guard.py`:

- `DEFAULT_USER_WRAP_UP_MARKERS` — substring list (case-insensitive)
  covering "compact the context", "wrap up", "save state",
  "/wrapup", "let's close this session", "continue later" and ~25
  more formulations.
- `check_message(..., last_user_message, skip_on_user_wrap_up,
  user_wrap_up_markers)` — NEW kwargs. If the last user message
  matches a wrap-up marker, the whole check short-circuits before
  any pattern scanning. This is the strongest escape: user intent
  beats all pattern matches.

Wired into `claude_hooks/hooks/stop.py`: `_run_stop_guard` now
extracts the last user-role text from the transcript and threads
it through `check_message`.

Config keys added:
- `hooks.stop_guard.skip_on_user_wrap_up` (default `true`)
- `hooks.stop_guard.user_wrap_up_markers` (default `[]` → built-in list)

Tests added: 7 new cases in `tests/test_stop_guard.py`
(`UserWrapUpEscapeTests` class). Suite at 264 passed / 16 skipped
locally.

### `/wrapup` skill (landed at `.claude/skills/wrapup/SKILL.md`)

The skill instructs the assistant to produce a complete restore-ready
state summary with 8 required sections:

1. Session snapshot (one paragraph)
2. Session achievements (commits, tests, files)
3. Open items (in-progress, unresolved, pending-user)
4. Next items (commands / edits / questions)
5. Plans in use or referenced
6. Active monitorings to re-establish (bg tasks, wakeups, crons)
7. Pods / remote hosts status
8. Restore checklist (copy-paste commands)

Explicit user-argument support: `/wrapup <extra text>` treats the
extra text as filter / emphasis instructions.

The skill description (the trigger for auto-invocation) enumerates
wrap-up phrases so Claude Code activates it automatically when the
user says "compact the context" / "save state" / "wrap up" / etc.

### What's left for Phase 7

- [ ] Commit the stop_guard changes + `/wrapup` skill + this plan update.
- [ ] Push to remote.
- [ ] Deploy to pandorum (`git pull` after push).
- [ ] Enable `/wrapup` auto-invocation end-to-end test in a real
      session (the user will observe whether the stop_guard false
      positive from 2026-04-14 recurs when the same test phrase is
      uttered).

### Remaining original plan (Phases 4-6) — COMPLETE

**Phase 4 — maintenance modules**: done
- [x] `tests/test_instincts.py` (13 tests)
- [x] `tests/test_reflect.py` (12 tests)
- [x] `tests/test_consolidate.py` (16 tests)

**Phase 5 — coverage gate**: done
- [x] `coverage>=7.0` added to `requirements-dev.txt` and
      `[project.optional-dependencies].dev`
- [x] `[tool.coverage.run]` and `[tool.coverage.report]` in `pyproject.toml`
- [x] Measured 81.3 % branch coverage (≥ 80 % target)

**Phase 6 — README test-section refresh**: done
- [x] Test count updated, full file map added, coverage workflow
      documented under `## Tests`

### Phase 8 — coverage push to ≥ 90 %

Added `tests/test_coverage_phase8.py` (120 tests) targeting the lowest
modules from the Phase 5 baseline. Per-module gains:

| Module | Before | After |
|--------|--------|-------|
| `hooks/pre_tool_use.py` | 46.1 % | **99.3 %** |
| `dispatcher.py` | 59.8 % | **95.5 %** |
| `hooks/stop.py` | 70.9 % | **87.2 %** |
| `providers/memory_kg.py` | 72.2 % | **97.2 %** |
| `claudemem_reindex.py` | 78.3 % | **89 %** (more branch coverage) |
| `recall.py` | 83.0 % | **97.0 %** |
| `safety_scan.py` | 81.7 % | **92.5 %** |
| `providers/__init__.py` | 50.0 % | **100 %** |
| `providers/base.py` | 80.2 % | **93.8 %** |
| `providers/qdrant.py` | 84.1 % | **93.5 %** |

Total: **81.3 % → 91.8 %** branch coverage.
