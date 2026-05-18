# Changelog

All notable changes to **claude-hooks** are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html):

- **MAJOR** — incompatible config / hook contract changes
- **MINOR** — new providers, new hook handlers, new opt-in subsystems
- **PATCH** — bug fixes and internal refactors that do not change behavior

Each release ships as a Git tag (`vX.Y.Z`) on `main` and a GitHub
release with the auto-generated source archive
(`claude-hooks-X.Y.Z.zip` / `.tar.gz`). See
[`docs/RELEASING.md`](docs/RELEASING.md) for the cut procedure.

## [Unreleased]

### Improved — stop_guard: 5 new commitment-stall patterns from live observation (#219, 2026-05-18)

The pre-v1.7.x release-cut soak surfaced 5 distinct stall-after-
commitment shapes that the current ``COMMITMENT_PATTERNS`` list
didn't catch. Each event was a real solidPC session where the model
wrote a clean commitment paragraph and then ended the turn without
firing the tool calls it described — the failure mode the stop_guard
exists to detect. Patterns added (gated by the same end_turn +
zero-tool_use + last-paragraph stack as the originals):

| # | Live event tail | New pattern shape |
|---|---|---|
| 1 | "Going to update STYLE_SHIFT_ISSUE.md … log RESULTS.md, then start M27." | ``\bgoing to\s+(?:update\|log\|run\|write\|…)\b`` |
| 2 | "Now drafting T17.3 mapping script in the meantime —" | ``\bnow\s+(?:drafting\|writing\|adding\|…)\b`` |
| 3a | "Writing the three Step 1 scripts now:" | ``\b(?:writing\|implementing\|…)\b.{0,120}\bnow[:.]?\s*$`` |
| 3b | "Now writing the orchestrator wrapper that applies Step 1 + re-smokes a variant…" | same as #2 |
| 4 | "Starting with Phase 1: two parallel Explore agents to map the current wiring before I draft the refactor." | ``\bstarting with\s+(?:phase\s+\d+\|step\s+\d+\|the\s+\w+)\b.*:`` |
| 5 | "Now add the ``_maybe_start_store_reaper`` helper. Let me insert it near ``_start_idle_reaper``:" | ``\bnow\s+add\b`` + ``\blet me\s+(?:insert\|patch\|apply\|…)\b`` |

Design notes:

- **Verb curation matters.** Each verb list is restricted to action
  verbs that imply tool use (write, edit, run, apply, commit, …).
  Read verbs (examine, look, check, search, investigate) are
  deliberately excluded — narration like "Now examining the
  structure" is the model thinking aloud, not committing to action.
- **End-anchored tail pattern (#3a).** The pre-existing
  ``\b(?:writing|implementing|…)\s+(?:the|that|…)?\s*(?:script|code|…)\b``
  required strict verb→noun adjacency, missing multi-word objects
  like "the three Step 1 scripts". The new pattern accepts up to
  120 chars between the verb and a ``\bnow[:.]?\s*$`` close, end-
  anchored so it only fires on a paragraph that genuinely ends on
  a "…now" commitment.
- **Colon-anchored phase pattern (#4).** ``Starting with Phase 1:``
  requires the colon to fire — bare ``starting with Phase 1`` in
  conditional planning ("starting with Phase 1 would be too
  risky") stays silent.
- **Past tense stays silent.** "I wrote / I added / I implemented"
  doesn't match any of the new patterns. Reporting is not
  committing.

Test surface (``tests/test_stop_guard.py``):

- 6 new positive cases — one per live event (3a and 3b separate
  because they exercise different patterns).
- 5 new negative cases — read verbs, bare "Continuing", past
  tense, no-colon "starting with Phase", "Going to think" with
  a non-action verb.
- All 38 existing tests unchanged.

A standalone manual verification of the 11 cases (6 fire + 5
silent) is in the commit's bash output as a smoke against
``_commitment_regex().search``.

Affected files:

- ``claude_hooks/stop_guard.py`` — +5 patterns appended to
  ``COMMITMENT_PATTERNS`` with inline forensic comments tying
  each pattern to the event that surfaced it.
- ``tests/test_stop_guard.py`` — +11 tests
  (``StallAfterCommitmentLiveEventsTests`` class).

Both envs full sweep: ``claude-hooks-consultants`` 3871 + 30
skipped + 117 subtests; ``claude-hooks`` 3765 + 136 skipped + 110
subtests.

### Added — PreCompact wrap-up: preserve AskUserQuestion Q&A across the compaction boundary (#217, 2026-05-18)

The 2026-05-18 #214 regression matrix lost two question shapes (Q2
and Q3 wordings) across a context-compaction boundary. The
mechanical extractor in ``wrapup_synth`` was preserving files,
commits, bash commands, plan refs, and remote endpoints — but
**not the user's explicit decisions made via AskUserQuestion**.
The summarizer model can't reconstruct those from prose because
the prose only carries the AssistantAgent's downstream actions,
not the original options menu nor the chosen label.

Result: post-compact sessions resumed work without knowing what
the user had already decided. The summary's "(AskUserQuestion
answer) 'all the three' + 'both modes'" line stripped both the
question text AND the option labels — leaving the resumed
session unable to tell whether Q2 was a citation-lint stress
question or a synthesizer-enumeration question.

Fix: a third class of mechanical extraction added to
``claude_hooks/wrapup_synth.py``:

| Helper | What it does |
|---|---|
| ``collect_ask_user_questions(transcript)`` | Two-pass walk over the transcript: pass 1 indexes every ``AskUserQuestion`` ``tool_use`` by id; pass 2 pairs each with its ``tool_result`` (matched on ``tool_use_id``) and parses the canonical "User has answered your questions: …" answer string. Returns ``[(question, answer), …]`` in chronological order. |
| ``_parse_aq_answers(question_texts, result_str)`` | Anchors on the known question texts (from the tool_use's ``questions`` list) so the parser doesn't have to traverse the trailing prose. Tolerates the three known terminator shapes (``", "`` next-pair, ``" selected preview`` for preview options, ``". You can now…`` canonical close). |

The wrap-up markdown grows an **unnumbered** section between
``## 1. Session snapshot`` and ``## 2. Session achievements``:

```markdown
## User decisions captured this session

_Verbatim AskUserQuestion exchanges from this session, in order.
Preserved here because the May-2026 #214 regression matrix lost
Q2/Q3 across a compaction boundary — these decisions are
load-bearing for resumed work and the summarizer can't
reconstruct them from prose alone._

- **Q:** Run shape for these 4 models?
  **A:** Screening (1 run × 3 queries × 4 models = 12 consultations) (Recommended)
- **Q:** Embedder bottleneck: which direction to take?
  **A:** I would go with: Bump embedder timeout from 60s → 300s and retry. llamafile…
...
```

Position rationale: the section sits at the top of the file (post-
Snapshot, pre-Achievements) where post-compact attention lands
first. Unnumbered to preserve the canonical 1-8 ``/wrapup``
skill numbering — renumbering would have broken the skill's
schema-pinned readers.

Cost: one extra pass over the transcript at PreCompact time.
Zero LLM calls, zero network. Cap of 30 pairs per session keeps
the wrap-up readable on very long sessions; the full series
remains in the transcript.

Validated against the live transcript that drove this fix
(``0ac25a2d-bb65-4d0a-9b53-9001606a9811.jsonl``, 45 185
messages): 88 ``(Q, A)`` pairs extracted cleanly, including the
exact "What's Q2 for the regression matrix?" exchange that
triggered the discovery.

Test surface (``tests/test_pre_compact.py``):

- 7 new ``CollectorTests`` cases: empty transcript, single pair,
  multi-pair single exchange, isolation from other tool_uses,
  unanswered (orphaned) exchange handling, list-shaped
  tool_result content, chronological ordering.
- 3 new ``SynthesizeMarkdownTests`` cases: section emitted when
  pairs present, section omitted when empty, canonical 1-8
  numbering preserved.
- Existing 17 tests unchanged.

Affected files:

- ``claude_hooks/wrapup_synth.py`` — ``collect_ask_user_questions``,
  ``_parse_aq_answers``, new section in ``synthesize_markdown``.
- ``tests/test_pre_compact.py`` — +10 tests.

Both envs full sweep: ``claude-hooks-consultants`` 3860 + 30
skipped + 117 subtests; ``claude-hooks`` 3754 + 136 skipped + 110
subtests.

### Fixed — pgvector: read-only methods leak transactions, blocking concurrent DDL (#218, 2026-05-18)

The #214 regression matrix's cell 2 (Q1 × high) deadlocked for **14
minutes** at the planner→researcher transition. Three lanes were
stuck in ``recall_research → _ensure_ready → _create_table``
waiting on an ``ALTER TABLE consultants_store ADD COLUMN IF NOT
EXISTS expires_at`` that PG was refusing to grant because the
store-reaper's connection held an unrelated lock.

Forensic via ``pg_stat_activity``:

- pid 458296 (store-reaper): state ``idle in transaction``,
  wait ``Client/ClientRead``, last query =
  ``SELECT ... FROM consultants_store WHERE expires_at IS NOT NULL``
  (the reaper's ``expire_before`` sweep). Held ``AccessShareLock``
  on ``consultants_store`` for **20 minutes** since 18:00:40.
- pid 460699 (researcher session): state ``active``, wait
  ``Lock/relation``, query = ``ALTER TABLE ... ADD COLUMN``.
  Needed ``AccessExclusiveLock``; blocked by 458296.

Root cause: every read-only method in ``pgvector.py`` (``expire_before``,
``count``, ``_search_tables``, ``_recall_hybrid_unlocked``,
``_kg_search_nodes``) calls ``cur.execute(SELECT ...)`` inside a
``with self._conn.cursor()`` block and returns the rows
**without calling commit() or rollback()**. psycopg3's default
mode (autocommit=False) auto-starts a transaction on the first
execute and keeps it open until the caller closes it explicitly.
Closing the cursor context manager does NOT close the transaction.

Result: every read-only call leaves the connection sitting ``idle
in transaction`` holding ``AccessShareLock`` on every table it
touched. With one connection per provider, a same-connection
caller sees no issue (the lock is shared with itself). But the
M14 reaper has its own provider with its own connection — and
every researcher session that opens its provider for M14's lazy
``ALTER TABLE`` migration walks into the reaper's lingering lock.

Fix: introduce ``_read_only_finish()`` (rollback-based, since
read-only commit and rollback are semantically identical at the
PG level but rollback is cheaper), and call it from every
read-only happy-path exit:

| Method | Happy path | Old | New |
|---|---|---|---|
| ``expire_before`` | after ``fetchall()`` | (none) | ``_read_only_finish()`` |
| ``count`` | after ``fetchone()`` | (none) | ``_read_only_finish()`` |
| ``_search_tables`` | after merge | (none) | ``_read_only_finish()`` |
| ``_recall_hybrid_unlocked`` | after RRF merge | (none) | ``_read_only_finish()`` |
| ``_kg_search_nodes`` | after rank+trim | (none) | ``_read_only_finish()`` |

The error paths already call ``self._conn.rollback()``; this fix
only changes the success paths. Write methods (``store``,
``delete_by_hashes``, ``refresh_expires_at``, ``batch_store``,
``_create_table``, the ``_kg_*`` writers) already commit
explicitly and are untouched.

The helper tolerates a missing/aborted connection defensively —
if the rollback itself raises, log and proceed. The caller's
``self._lock`` (RLock from the 2026-05-18 thread-safety fix) is
still held around the close call so the close races safely with
the next read on the same provider.

Test surface:

- ``tests/test_pgvector_expires_at.py:test_expire_before_closes_readonly_transaction``
  — drives the fake connection through a happy-path
  ``expire_before`` and asserts the rollback counter incremented.
- Existing 17 pgvector tests continue to pass — the cleanup is
  additive on the happy path.

Affected files:

- ``claude_hooks/providers/pgvector.py`` — new
  ``_read_only_finish()`` helper; +5 call sites at read-only
  happy-path exits.
- ``tests/test_pgvector_expires_at.py`` — +1 regression test.

Operational note: a previously-stuck transaction can only be
released by ``pg_terminate_backend(pid)`` against the offending
backend (``pg_cancel_backend`` only interrupts active queries,
not idle-in-transaction ones). After deploying this fix, restart
``claude-hooks-daemon`` and the consultants daemon so the new
code loads. Existing leaked transactions from pre-#218 daemons
need a one-shot terminate on the matching ``pg_stat_activity``
row.

### Fixed — consultants: stop tool_executor's per-round store writes from fanning out per lane (#216, 2026-05-18)

The same csl-2026-05-18-1724-0f9f deadlock investigation that drove
#215 also surfaced a write-amplification issue on the store side:
sessions with ``tool_executor`` enabled produced ~4× more provider
rows per lane than sessions without it. A direct census comparison
showed csl-1527 (tool_executor=ON, 9 researcher lanes) carrying **36
research rows** vs. csl-1801 (tool_executor=OFF, 9 lanes) carrying
**6 rows**. Each extra row is an extra embedder call at write time
and an extra distillation candidate 30 days later.

Root cause: ``record_research`` used a content-derived key
(``L{lane_idx}-{sha1(text)[:12]}``), so a researcher lane that
loops PLAN→tool→REPORT N times (high effort allows 3 rounds, plus
critic reroutes) writes N **different texts** under N **different
keys**. The in-process index legitimately holds N entries (one per
round); the provider has no key-based upsert and dedupes only on
``content_hash``, so it gets N new rows too. The embedder fires
once per round per lane, the reaper distillation budget rises in
proportion, and the resulting per-tick load was the trigger for the
embedder saturation that #215 paces but doesn't actually shrink.

Fix: **one provider row per (namespace, key) pair**, replacing the
pre-#216 "one row per (namespace, key, content)" semantics.

| Layer | Change |
|---|---|
| ``record_research`` | Key shape collapsed from ``L{lane_idx}-{sha1[:12]}`` to ``L{lane_idx}`` — stable per lane regardless of content. ``lane_idx=None`` (single-researcher, non-fanout) still maps to the ``L?`` sentinel so the legacy path keeps working. |
| ``ProviderBackedStore._do_put`` | On overwrite (in-process index already holds ``op.key``), recompute the **prior** content's hash and call ``provider.delete_by_hashes`` to remove the stale row **before** inserting the new one. The new row is the only durable record. |
| Same-content re-puts | ``ON CONFLICT(content_hash) DO NOTHING`` (existing provider semantics) handles these; the new path skips the delete when prior text equals new text — no spurious provider churn. |
| Delete failure | Best-effort. If ``delete_by_hashes`` raises, log and proceed with the insert. Worst case is the pre-#216 accumulation, which the M14 reaper eventually GCs via TTL. Never blocks the durable write. |

This composes with M14 TTL semantics: the new row carries a fresh
``expires_at`` (jittered per #215); the deleted row was always
going to expire under the same TTL window, just N round-times
earlier on the calendar. Refresh-on-read still applies — repeated
reads of the same lane finding bump the timestamp on the SINGLE
surviving row.

Why this is safe for the M8 peer-findings recall semantics: each
later round's text is strictly the **most refined** version of the
lane's finding (the model has seen the prior round's tool results
when it writes round N+1). Discarding earlier rounds doesn't lose
signal — it discards drafts.

Test surface (``tests/test_consultants_v2_store.py``):

- ``test_record_research_key_is_stable_per_lane`` — same lane =
  same key regardless of content.
- ``test_record_research_keys_differ_across_lanes`` — cross-lane
  isolation.
- ``test_record_research_missing_lane_idx_uses_question_mark`` —
  ``L?`` sentinel for the single-researcher path.
- ``test_per_lane_overwrite_deletes_prior_provider_row`` — three
  consecutive puts under one key leave exactly one provider row.
- ``test_per_lane_overwrite_skips_delete_when_content_unchanged``
  — identity re-puts are zero-cost.
- ``test_per_lane_overwrite_with_record_research_smoke`` — same,
  end-to-end via ``record_research`` (4 rounds → 1 row).
- ``test_multi_lane_writes_keep_one_row_per_lane`` — 3 lanes ×
  4 rounds → 3 rows, one per lane, each holding the latest text.

The fake provider grew ``delete_by_hashes`` mirroring the real
pgvector / sqlite_vec contract.

Affected files:

- ``consultants/engine/store.py`` — stable per-lane key in
  ``record_research``; ``_do_put`` adds prior-row delete on
  overwrite; drops the ``hashlib`` import (no longer used).
- ``tests/test_consultants_v2_store.py`` — +7 tests;
  ``_FakeProvider.delete_by_hashes`` mirrors the provider contract.

Both envs full sweep: ``claude-hooks-consultants`` 3849 + 30
skipped + 117 subtests; ``claude-hooks`` 3743 + 136 skipped + 110
subtests. M12 parity green (the store stays gated on
``effort=medium`` by default — the new key shape only matters
when the store is enabled and the lane actually loops).

### Fixed — consultants: M14 reaper pacing + TTL jitter to prevent embedder saturation (#215, 2026-05-18)

Two compounding issues surfaced during the deadlock investigation of
``csl-2026-05-18-1724-0f9f``:

1. **Cohort alignment at write time.** When the M14 default-on flip
   landed on 2026-05-18, every existing research row got stamped
   with ``expires_at = now + 30d`` within the same minute. Without
   jitter, the reaper would have seen those 30 days later all
   collapsing onto the same hourly sweep tick — N session groups,
   N distillations, N project-namespace writes, all back-to-back
   against the single-llamafile CPU embedder.

2. **Unbounded per-sweep fan-out.** ``sweep_once`` walked every
   ready group in one tick, with no inter-group pacing. Even a
   modest backlog (10 sessions × 3-5 findings each) would emit
   10 LLM calls + 10 embedder writes inside the same wall-clock
   window — exactly the saturation profile that caused the
   embedder timeouts that pre-#212 swallowed silently.

The fix is two additive knobs, all configurable, all with
recommended defaults:

| Knob | Default | Behavior |
|---|---:|---|
| ``store.ttl.jitter_pct`` | ``0.1`` (±10%) | At write time, ``expires_at = now + ttl * (1 + uniform(-jitter, +jitter))``. Spreads aligned cohorts across ±jitter of the nominal TTL so the reaper never sees N sessions expire on one tick. ``0.0`` disables. |
| ``store.distillation.max_groups_per_sweep`` | ``5`` | Cap on **successful distillations** per sweep tick. Remaining research groups roll over to the next tick (their originals stay in place, counted under ``rolled_over`` in the result dict). Cost-gated skips (below ``min_entries_per_distillation``) and ``tool_results`` deletes do NOT consume the budget — only LLM-driven distillations do. ``0`` = uncapped (pre-#215 behavior). |
| ``store.distillation.pace_seconds_between_distillations`` | ``5.0`` | Sleep N seconds between consecutive distillations within one sweep tick. Gives the embedder breathing room between project-namespace summary writes. Slept in 0.5 s slices so :meth:`StoreReaperThread.stop` stays responsive. ``0.0`` = back-to-back. |

All three knobs live under their existing ``[store.ttl]`` and
``[store.distillation]`` TOML blocks, render through
``claude-consultants config show``, and round-trip through the
TOML merge layer.

Worst-case wall budget per sweep under defaults: ``5 groups × (~60 s
cloud LLM + ~5 s embedder + 5 s pace) ≈ 5.5 min``. Backlogs larger
than that bleed across multiple ticks at hourly cadence —
intentional. The reaper is meant to be background hygiene, not a
synchronous batch flush.

The critical M14 invariant from #212 holds: a group that rolls over
is **not deleted** — its originals stay in place and the next sweep
re-attempts. Groups that distill successfully but fail to delete
also stay in place (caller logs and moves on). The cap counter
increments only after a successful end-to-end
``distill → write_summary → delete`` cycle.

Test surface:

- ``tests/test_consultants_v2_store_reaper.py:TestReaperPacing``
  — 9 tests covering cap behavior, pace timing, helper accessors,
  default config sanity, and that cost-gated + tool_results
  groups don't consume the budget.
- ``tests/test_consultants_v2_store_reaper.py:TestTTLJitter`` —
  jitter default + spread distribution sanity (200 writes
  produce > 100 distinct expires_at stamps).
- Existing TTL tests pin ``jitter_pct = 0.0`` explicitly so the
  nominal-window assertions stay deterministic.

Affected files:

- ``consultants/config.py`` — ``StoreTTLConfig.jitter_pct`` (new),
  ``StoreDistillationConfig.{max_groups_per_sweep,pace_seconds_between_distillations}`` (new), TOML parse + render for all three.
- ``consultants/engine/store.py`` — ``_compute_expires_iso``
  applies ``random.uniform`` jitter when configured.
- ``consultants/engine/store_reaper.py`` — ``sweep_once`` cap +
  pace logic, three new accessor methods, ``_sleep_paced`` helper,
  ``rolled_over`` stat in result dict.
- ``tests/test_consultants_v2_store_reaper.py`` — +10 tests.
- ``tests/test_consultants_v2_store_ttl.py`` — ``_ttl_cfg`` helper
  pins ``jitter_pct=0.0``.

Both envs full sweep: ``claude-hooks-consultants`` 3842 + 30 skipped
+ 117 subtests; ``claude-hooks`` 3743 + 129 skipped + 110 subtests.
M12 parity green (default ``effort=medium`` still gates the store
off, so the new defaults don't reach the parity-recorded baseline).

### Fixed — consultants: M9 control surface — runtime_events emission + default checkpointer + node-enter INFO log (#214, 2026-05-18)

The live regression run for #213 (Q2 × xhigh × tool_exec=OFF, sid
``csl-2026-05-18-1724-0f9f``) deadlocked at the planner→researcher
transition. While diagnosing, three independent M9 control-surface
failures surfaced simultaneously — explaining why we'd been
running blind:

1. **``runtime_events`` writer never fired.** The recorder shipped
   a ``record_event`` method (writes a narrow row into
   ``runtime_events``), and the SSE bridge reads from
   ``runtime_events`` via ``list_runtime_events``, but **nothing in
   the engine ever called the writer**. Census across all
   sessions on this host: 6 sessions had the table, 0 sessions had
   any rows in it (the 8 older sessions don't even have the
   table). SSE has been emitting empty streams since M9 shipped.

2. **``state`` / ``cancel`` / ``inject`` / ``pause`` / ``resume``
   endpoints all 500'd with ``ValueError: No checkpointer set``.**
   The runner compiled ``build_council_graph`` without passing a
   checkpointer; LangGraph's ``get_state`` / ``update_state``
   require one. Every M9 mutation endpoint went through one of
   those calls.

3. **Daemon app log emitted zero structured lines between role
   transitions** — only ``uvicorn.access`` records, no per-node
   INFO. Ops watching ``~/.claude/claude-hooks-consultants.log``
   couldn't see what the runner was doing.

Three fixes shipped together since they share root causes:

**Fix A: recorder auto-emits ``runtime_events`` mirror rows.**
``MessageRecorder.record_node`` / ``record_llm`` / ``record_tool``
each gain a narrow mirror row into ``runtime_events`` alongside
their existing detailed-events row. Single source of truth at the
recorder; no engine changes needed. Payload shape:

| Method | Mirror kind | Narrow payload (omits) |
|---|---|---|
| ``record_node`` | ``node_enter`` / ``node_exit`` | ``ts`` / ``duration_ms`` / ``error`` |
| ``record_llm`` | ``llm_call`` | ``model`` / ``duration_ms`` / ``prompt_tokens`` / ``completion_tokens`` / ``error`` (omits full request_json / response_json — those stay in the audit log) |
| ``record_tool`` | ``tool_call`` | ``tool`` / ``output_chars`` / ``duration_ms`` / ``error`` (omits full args / output) |

The audit log table (``events``) is untouched — every audit row
the post-mortem queries need is still written. The mirror is
additive.

**Fix B: runner attaches an ``in-memory`` LangGraph checkpointer
by default.** ``consultants/server/runner.py:make_runner`` now
imports ``MemorySaver`` from ``langgraph.checkpoint.memory`` and
passes it to both ``build_council_graph`` call sites. Cost: one
in-process checkpoint write per superstep, bounded for a council
with ~10 supersteps per run. Persistence isn't a goal — the
recorder's ``transcript.db`` is the durable audit log; this
checkpointer is purely for live introspection via M9 endpoints.

**Fix D: ``record_node(kind="node_enter")`` emits an INFO log
line.** Single line at the recorder level, names role + round +
lane_idx. Ops watching the daemon log now see role transitions
without querying the DB.

(Fix C in the original task draft — recorder-based reconstruction
fallback when checkpointer is off — was rendered unnecessary by
Fix B making the checkpointer default-on.)

Test surface:

- ``tests/test_consultants_recorder.py:TestRuntimeEventsMirror``
  — 8 new tests pinning the mirror contract: ``node_enter`` /
  ``node_exit`` / ``llm_call`` / ``tool_call`` each emit a row,
  narrow payload omits the heavy blobs, audit log is still
  written alongside, ``list_runtime_events`` returns rows in
  insertion order, ``node_enter`` emits an INFO log line, and
  ``node_exit`` does NOT (volume hygiene).
- ``tests/test_consultants_v2_control_routes.py:TestGetState.test_real_graph_with_memory_checkpointer_serves_state``
  — builds a real one-node ``StateGraph`` with ``MemorySaver``,
  installs it on a session, and asserts ``GET /v1/consult/<sid>/state``
  returns 200 (no ``ValueError("No checkpointer set")``).
- ``tests/test_consultants_v2_control_routes.py:TestEvents.test_real_recorder_mirror_rows_stream_through_sse``
  — drives a real ``MessageRecorder`` with node/llm/tool events,
  installs the recorder on a session, asserts ``GET /v1/consult/
  <sid>/events`` SSE-replays all three mirror rows with the
  expected ``event:`` headers + narrow payload fields.

Full sweep: 3821 → 3831 passed (+10 new tests). Targeted suites
green.

Out of scope for this commit:

- The underlying **deadlock** that triggered the investigation
  (planner→researcher transition stuck for 22+ min with all
  threads on futex_wait, no upstream TCP, no CPU). The fix to
  the introspection surface lands here; the deadlock root cause
  is a separate investigation (next: py-spy on a fresh repro
  with the new logging in place).

### Fixed — consultants: triple-fix (#213, 2026-05-18) — UNKNOWN preservation + exhaustive synthesizer + prompt compaction

Three follow-ups from the csl-2026-05-18-1554-c8dc forensic + the
README audit.

**1. `KIND_UNKNOWN` rows now preserved, not deleted** —
``store_reaper.py:sweep_once`` previously fell through into the
``KIND_TOOL_RESULTS`` branch for rows whose namespace metadata
the daemon couldn't classify, deleting them unconditionally. The
consultation flagged this as silent data loss: ``KIND_UNKNOWN``
means the metadata is corrupted OR a future schema added a
namespace kind the running daemon doesn't recognise — in either
case, the row's content may still be recoverable, and "we don't
know what kind it is" is too thin a basis to delete. Post-#213
the sweep leak-then-logs: UNKNOWN rows stay on disk, a new
``rows_unknown_skipped`` stat increments, and a WARNING log line
is emitted so ops can audit via direct provider query. Cost is
bounded (UNKNOWN only happens on corrupted metadata or
schema-version skew, both rare); the TTL filter on
``_do_search`` / ``_do_get`` already hides them from consumers
since they're expired.

Two new tests in ``tests/test_consultants_v2_store_reaper.py``:
``test_unknown_kind_rows_skipped_not_deleted`` (end-to-end
preservation invariant) and
``test_unknown_kind_does_not_block_other_groups`` (mixed-group
isolation — UNKNOWN preserved while research distills + deletes
normally). The existing
``test_unknown_namespace_buckets_unknown`` docstring updated
(grouping unchanged; sweep branch changed).

**2. Synthesizer prompt requires exhaustive list enumeration** —
``csl-2026-05-18-1554-c8dc`` ended with ``finish_reason="stop"``
mid-bullet 5 of 6 (~2.6 k chars output). Investigation via
``transcript.db`` confirmed it was NOT a token-budget cap: the
synthesizer (gemma4:31b-cloud) decided the list was "complete
enough" after 4.5 of the researchers' 5 reported edge cases.
Fix is prompt-level, not a ``num_predict`` knob. Both
``SYNTHESIZER_SYSTEM`` and ``SYNTHESIZER_SELF_CRITIC_SYSTEM``
gained an ``EXHAUSTIVE ENUMERATION`` block requiring the model
to walk every researcher item before stopping on list-shaped
answers (edge cases, failure modes, gotchas, alternatives).

One new prompt-content test:
``tests/test_consultants_council.py:TestPromptBuilders.test_synthesizer_system_demands_exhaustive_enumeration``.

**3. Prompt compaction (token + bias hygiene)** — The two
prompt blocks added in #213 originally carried verbose project
history ("the csl-2026-05-18-1554 audit stopped mid-bullet 5 of
6 because…"). User correctly flagged that as bias risk +
prompt-token waste: the model wonders "what is csl-2026-05-18-
1554?" and the load-bearing rule is the same with or without
the example. Same pass applied to the existing #207 additions:

- ``RESEARCHER_SYSTEM`` ``CITATION INTEGRITY`` block: dropped
  the worked-example bits (``store_sql.py``, "line 128 vs 360",
  the 60-line plausible-looking imports paragraph). Three
  failure modes still named explicitly. ~150 chars saved.
- ``SYNTHESIZER_SYSTEM`` ``CITATION INTEGRITY`` block: dropped
  the ``store_sql.py`` reference. Rule unchanged.
- ``tool_executor.build_tool_plan_user_appendix`` REPORT-NOW
  closing instruction: dropped the "2026-05-18 M14 first-real-
  ask repeatedly caught" paragraph. Same forbid-fabrication
  rule, in fewer tokens.
- ``SYNTHESIZER_SYSTEM`` + ``SYNTHESIZER_SELF_CRITIC_SYSTEM``
  ``EXHAUSTIVE ENUMERATION`` block: shipped in compact form
  from the start.

Net prompt-token reduction across the four blocks: ~600 chars
(~150 tokens) per researcher / synthesizer round. Bias surface
reduced: no project-specific filenames, csl IDs, or dated
incident narration in any prompt the council sees.

**4. README `whats-new.md` link description** —
``docs/whats-new.md`` actually contains v1.7 highlights, but
the README described it as "v1.4 highlights" (pre-existing
stale text from before the v1.5/1.6/1.7 cuts landed). Fixed +
added the archive link to ``whats-new-v1.4.md`` alongside the
existing ``whats-new-v1.1.md``.

Full sweep: 3818 → 3821 passed (+3 new tests). Targeted suites
green.

### Fixed — consultants: M14 silent-durable-write hole at store.py:281-288 (#212, 2026-05-18)

Discovered during the
[A/B WITHOUT-tool_executor consultation](.claude-hooks/consultants/csl-2026-05-18-1554-c8dc/summary.md)
that was reviewing the M14 critical invariant
("research originals only get deleted after a successful
distillation write to the project namespace"). The council
correctly identified that ``ProviderBackedStore._do_put``
wrapped ``self._provider.store(...)`` in a bare
``except Exception: log.exception(...)`` with no re-raise, so
``store.put`` returned success even when the underlying
pgvector / sqlite_vec write failed. The
``StoreReaperThread.sweep_once`` chain then saw
``_write_summary`` succeed and proceeded to
``delete_by_hashes(originals)`` — distilled originals gone,
summary missing, data loss.

Root cause was older than M14: an existing test
(``tests/test_consultants_v2_store.py:test_provider_store_failure_does_not_break_put``)
**pinned the silent-swallow contract** with the rationale "the
in-process index still gets the item so recall in the SAME
process works." That stance is fine for best-effort callers
(researcher per-turn puts via ``record_research``, which wraps
its own ``try/except`` at ``store.py:776`` and was always
covered), but it broke M14 the moment the reaper started
relying on durable persistence for the invariant.

Fix is two-file, two-line:

1. ``consultants/engine/store.py:281-301`` — ``_do_put`` now
   re-raises after logging. The in-process index update on lines
   246-255 is deliberately NOT rolled back; callers that want
   transactional semantics wrap themselves. Updated inline
   contract comment to spell out the post-#212 contract +
   reference ``record_research`` for the best-effort pattern.
2. ``consultants/engine/distillation.py:422-433`` —
   ``write_distilled_summary`` wraps ``store.put`` in
   ``try/except`` and re-raises any non-``DistillationFailed``
   exception as ``DistillationFailed(f"durable write to project
   namespace failed: {e!r}") from e``. The reaper's existing
   ``except DistillationFailed`` block at ``sweep_once:303``
   catches it and treats the group as "originals stay; retry
   next tick" — matching the M14 plan's stated invariant
   exactly.

Test surface (claude-hooks-consultants env):

- ``tests/test_consultants_v2_store.py`` — renamed
  ``test_provider_store_failure_does_not_break_put`` →
  ``test_provider_store_failure_propagates_to_caller``;
  ``assertRaises(RuntimeError)`` instead of "no exception
  expected"; in-process index assertion preserved (the
  deliberate non-rollback of the in-memory update).
- ``tests/test_consultants_v2_distillation.py`` — two new
  ``TestWriteDistilledSummary`` cases:
  ``test_write_summary_wraps_store_put_failure_as_distillation_failed``
  (RuntimeError → DistillationFailed with chained ``__cause__``)
  and
  ``test_write_summary_passes_through_distillation_failed_from_store``
  (no double-wrap if the store itself raises DistillationFailed).
- ``tests/test_consultants_v2_store_reaper.py`` — new
  ``test_durable_write_failure_keeps_originals`` exercises the
  end-to-end failure path (distiller healthy, store-side put
  fails) and asserts the M14 invariant: distilled=0,
  groups_distill_failed=1, deleted=0, no ``delete_by_hashes``
  calls. ``_FakeStore`` grew a ``fail_put`` knob so future
  durable-failure regressions can reuse the helper.

Full sweep: 3815 → 3818 passed (+3 new tests). Targeted M14
surface (store + distillation + reaper + ttl + e2e): 116
passed.

### Changed — consultants: tool_executor flipped back to disabled-by-default (#211, 2026-05-18)

The M14 first-real-ask A/B run on 2026-05-18 measured the
``tool_executor`` role at full x-tier (3 models × 3 lanes) under
its M11c-5 default-on configuration:

| Variant            | Wall   | Tokens | Edge cases caught |
|--------------------|-------:|-------:|------------------:|
| WITH tool_executor | 1121 s | ~840 k | 8                 |
| WITHOUT (synth)    |  403 s | ~480 k | 9                 |

Net result: 3× slower wall time, +43% tokens, ``-1`` edge case
under the role's intended best-case shape (a grep-heavy
research question on this repo). Full A/B record in
[``benchmarks/consultants/results/2026-05-18/tool-executor-ab/report.md``](benchmarks/consultants/results/2026-05-18/tool-executor-ab/report.md).

The architectural read: the tool_executor → researcher fanback
forces each researcher lane to read tool results through the
M11c-3 ``parent_lane_idx``-routed ``Send`` aggregation barrier,
which (a) serializes a fan-in step that the synthesizer-direct
path skips entirely, and (b) makes researcher-side fabrications
more visible because tool_results give the lane plausible
"sources" to cite from without grounding.

The role remains supported, fully tested, and ready to enable
per-role for **slow-tool-budget questions** where the synthesizer
would otherwise re-issue the same five grep calls across nine
lanes (the role's actual win condition). Flipped in two places
that mirror each other intentionally:

- ``consultants/config.py:DEFAULT_ENABLED_BY_ROLE["tool_executor"]: False``
- ``consultants/engine/tool_executor_defaults.py:RECOMMENDED_DEFAULT_ON = False``

The flip history comment block in both files records the full
M11c-1 → M11c-5 → 2026-05-18 path so the next time someone
considers flipping this they have to read the A/B record first.

Test surface follows the flip:

- ``tests/test_tool_executor_defaults.py`` — renamed
  ``test_recommended_default_on_true_after_103_resolved`` →
  ``test_recommended_default_on_is_false_after_m14_ab``;
  ``TestM11c5RuntimeDefault`` → ``TestRuntimeDefault``;
  ``test_default_enabled_by_role_is_true`` →
  ``test_default_enabled_by_role_is_false``.
- ``tests/test_consultants_v2_parity.py:test_tool_executor_role_disabled_by_default`` —
  renamed from ``_enabled_by_default``; ``assertFalse`` with the
  flip-history comment block.
- ``tests/test_consultants_config.py:TestDefaults.test_opt_in_roles_default_state``
  and ``test_load_with_no_files_returns_defaults`` — updated to
  assert ``cfg.roles["tool_executor"].enabled is False`` with the
  same flip-history comment.

M12 parity is preserved by construction: the parity baseline
locks "today-default == tomorrow-default", and we have a new
default — the parity tests assert the new default and the
fixtures that exercise the on path explicitly opt in via
``cfg.roles["tool_executor"].enabled = True``.

### Added — consultants: role-by-role reference doc (#208, 2026-05-18)

[``docs/consultants-roles.md``](docs/consultants-roles.md) is a
~250-line role-by-role reference covering all 6 active roles:
``planner``, ``researcher`` (Mode A inline / Mode B PLAN-REPORT
split), ``critic``, ``synthesizer``, ``tool_executor``,
``coder``. The doc opens with a quick map table
(default state, model, tier coverage, doc anchor) and each
section gives the role's responsibility, the prompts that drive
it, the planner-emitted JSON contracts where applicable, and the
M11c / M11b bench evidence backing the model picks.

Particular focus on the two opt-in roles per the user's request:

- ``coder`` — sandbox limits, why model picks matter, the M11b
  skill-eval rubric, the failure modes (sandbox cap rejection,
  no transactional rollback, planner-emitted ``coder_tasks``
  contract).
- ``tool_executor`` — opens with a "default disabled" status
  banner, links the M14 A/B record, lists 4 specific
  shortcomings to know about when enabled (wall time, token
  cost, aggregation lossiness, researcher contamination
  amplification), and a "when to enable anyway" section
  explaining the role's actual win condition (cross-lane tool
  call deduplication under slow-tool budgets).

### Added — consultants: prompt-level guard against source-listing fabrications (#207, 2026-05-18)

Cross-trace of the 2026-05-18 ``csl-2026-05-18-1428-589c`` run
(with #204 + #205 wired) showed the 9 surviving fabrications in
the final answer were all fake method / table / scheduler names
(``_find_candidates``, ``_distillation_confirmed``,
``_remove_originals``, ``_mark_ledger_completed``,
``_write_to_project``, ``_SimpleScheduler``,
``research_originals``, ``distillation_ledger``, ``distill``) —
**every one of them originated in ``glm-5.1:cloud`` researcher
lanes**. Same pattern as ``csl-2026-05-18-1031-9e3b`` (lane 5,
17 fabs incl. fake ``store_sql.py`` / ``distiller.py`` /
``transcript_db.py`` modules). Two consults, two glm-5.1 lanes
emitting plausible-looking 60-line source listings that survive
through to the user-visible answer.

The CitationLinter catches these after the fact, but the user
preferred a prompt-side fix before any roster change. Two prompt
sites tightened:

1. ``consultants/engine/council.py:RESEARCHER_SYSTEM`` — the
   CITATION INTEGRITY block adds a third explicit failure mode:
   *"writing a fake numbered SOURCE LISTING (lines of code with
   line numbers prepended) for a file you did not actually
   read"*. Names the specific class with worked-example shape so
   the model can pattern-match.
2. ``consultants/engine/tool_executor.py:build_tool_plan_user_appendix``
   — the REPORT-NOW closing instruction (the tightest
   load-bearing prompt at the moment of REPORT emission) gains a
   ``DO NOT FABRICATE SOURCE LISTINGS`` block: the EVIDENCE
   blocks above are the ONLY file content the researcher may
   quote / paraphrase / cite. Numbered code blocks (``21
   import foo``...) must be verbatim from EVIDENCE. Invented
   dataclass fields / function names / table names are
   explicitly forbidden, even if they sound plausible.

This is a behavioral nudge — the linter remains the
verification layer. If the next M14-style consult still
produces glm-5.1 source-listing fabrications under the tightened
prompt, the next step is roster-side (demote glm-5.1 from the
researcher extras list to tool_executor-only).

Test impact: ``tests/test_consultants_v2_tool_executor.py::
TestBuildToolPlanUserAppendix::test_truncates_long_content``
cap raised from 3000 to 4000 chars (the new instruction tail
adds ~530 chars). All other tests pass unchanged.

Both envs full sweep: 3815 + 3717 passing.

### Fixed — consultants: CitationLinter symbol-match — text-at-cited-line instead of enclosing-function (#205, 2026-05-18)

The 2026-05-18 ``csl-2026-05-18-1428-589c`` re-run (M14 first-real-
ask with #204 wired) reported **106 fabrications caught at the
researcher boundary** — a 10× jump from prior runs that didn't
match operator intuition about glm-5.1's normal behavior.

Investigation: ~95% of those were **false positives** in the
symbol-mismatch heuristic. The rule was "is line N inside the
def of the claimed symbol?" — but natural prose like *"the
reaper calls ``_distill_group`` at ``store_reaper.py:301``"*
puts the cite at the CALL site (whose enclosing function is
``sweep_once``). Line 301 genuinely contains
``self._distill_group(…)`` — the model's claim about the call
location was accurate; the linter was flagging it for not being
the *definition* location.

Re-classifying the 106:

| Class | Count | Verdict |
|---|---:|---|
| ``sweep_once`` line claimed ``_delete_rows`` / ``_write_summary`` / ``_distill_group`` | 51 | **FALSE POSITIVE** — call sites |
| Method body lines claimed callee names | 8 | **FALSE POSITIVE** — internal calls |
| ``file not found`` | 3 | REAL — glm-5.1 fake paths |
| Genuine wrong-line / fake-symbol | ~37 | REAL — module-scope claims for non-existent functions |

The new rule (``_symbol_appears_in_range`` in
``consultants/engine/citation_linter.py``) replaces the enclosing-
function compare with a text-occurrence check at the cited line
range (±1 line slack for off-by-one prose like decorator-vs-def).
If the claimed identifier appears as a word-boundary substring
inside the cited line(s), the cite is treated as a valid
reference; otherwise it's flagged with annotation showing where
line N actually lives. Path-not-found and line-bounds checks
unchanged.

Net effect on the live ``csl-2026-05-18-1428-589c`` answer
(re-linted with the new rule after annotation strip):

* Old rule: 10 annotations in shipped answer (mix of real + FP).
* New rule: 9 annotations, **all real** — every flagged symbol
  is a fabricated method/table name that doesn't exist anywhere
  in the cited file (``_find_candidates``,
  ``_distillation_confirmed``, ``_remove_originals``,
  ``_mark_ledger_completed``, ``_write_to_project``,
  ``_SimpleScheduler``, ``research_originals``, ``distillation_ledger``,
  ``distill``). All 8 trace back to ``glm-5.1:cloud`` researcher
  lanes via transcript.db cross-trace — same fabrication mode as
  ``csl-2026-05-18-1031-9e3b``.

Annotation format changed from ``[in X, not Y]`` to
``[no Y at this line; line is in X]`` — same semantics, clearer
phrasing. Tests in ``tests/test_citation_linter.py``
updated to match. New ``TestCallSiteIsNotFabrication`` class
(4 tests) explicitly locks in the no-flag behavior for call
sites + the ±1 slack window for decorator-line cites.

Both envs full sweep: 3815 + 3717 passing.

### Fixed — consultants: CitationLinter now runs at researcher boundary, not just synthesizer (#204, 2026-05-18)

The CitationLinter shipped in commit ``159d353`` runs at the
synthesizer node — caught fabricated path:line cites in the
final answer, but only at the very last step. A 2026-05-18
forensic on ``csl-2026-05-18-1031-9e3b`` proved the worst-class
fabrication (the fake module ``consultants/engine/store_sql.py``,
plus invented ``distiller.py`` and ``transcript_db.py``)
originated in a single researcher lane (``glm-5.1:cloud``,
lane 5) with **ZERO tool calls** — pure hallucination. The bad
cites then flowed unchallenged through peer_findings → critic →
synthesizer.

Retroactive lint of the originating researcher REPORT
(event_id=341) caught **17 distinct fabrications** including
three entirely fake filenames. Had the linter been wired at the
researcher boundary, every downstream role would have seen the
annotated form (``store_sql.py:41-61 [unverified — file not
found]``) instead of the bare claim.

Fix: ``researcher_node`` in ``consultants/engine/council.py``
gains a ``_lint_research_text`` closure called at all three
successful-REPORT exit sites (M6 REPORT mode, PLAN-mode empty
fallback, v1 inline-loop). The annotated form flows into the
``research`` field of the return dict, into peer_findings, into
the store, and into the synthesizer's input. The ``turn`` record
keeps the RAW model output so the transcript stays a faithful
"what the model said" forensic for future investigations.

Companion forensic write-up at
``benchmarks/consultants/results/2026-05-18/m14-first-real-ask/forensic-202-204.md``
documents the full chain for both fabrication classes (gemma's
real-but-wrong grep-line interpretation, glm's pure
hallucination) and the cross-trace that proved the synthesizer
was relaying, not inventing, the gemma-emitted wrong lines.

New tests in ``tests/test_consultants_council.py::TestResearcherCitationLint``:

- Fabricated cite annotated in downstream-visible ``research``
  field; raw text preserved in turn record.
- Empty cwd → linter no-ops (defensive — no false-flagging when
  there's nothing to verify against).
- Real path:line cite passes through unchanged.

Both envs full sweep: 3811 + 3713 passing.

### Added — code_graph: ``end_line`` on def/class/method nodes + ``enclosing_symbol_at`` query API (#200, 2026-05-18)

The 2026-05-18 CitationLinter parsed every cited file on demand
with stdlib :mod:`ast` to answer "is line L inside function F?".
Acceptable for the M14 first-real-ask (5 cites pointing at one
file, ~10 ms/lint), but doesn't scale and pays the same parse
cost every consult turn.

This change makes the on-disk code_graph the fast path:

1. **Schema bump (additive)** —
   ``claude_hooks/code_graph/builder.py`` now stamps ``end_line``
   on every function, async function, method, and class node.
   ``EXTRACTOR_VERSION`` constant moves from ``1`` → ``2`` and is
   recorded in ``graphify-out/cache/manifest.json``. A mismatch
   forces a full rebuild instead of trusting per-file SHA matches
   that would otherwise replay stale extractions.
2. **New module** —
   ``claude_hooks/code_graph/enclosing.py`` (``enclosing_symbol_at``,
   ``graph_covers_file``, ``clear_cache``). Loads
   ``graphify-out/graph.json`` lazily, mtime-caches the per-file
   index in-process, and answers the line-containment query in
   O(N_defs_in_file). Legacy v1 graphs without ``end_line`` are
   detected and reported as uncovered so callers correctly fall
   back to :mod:`ast` parsing.
3. **Linter wiring** —
   ``consultants/engine/citation_linter.py`` tries the graph
   helper first and falls back to its existing on-demand
   :mod:`ast` walk when the graph is missing, predates the
   ``end_line`` field, or doesn't cover the cited file. Existing
   regression tests pass unchanged, including the 5
   ``csl-2026-05-18-1031-9e3b`` fabrications.

**Measured speedup** on the M14 fabrication corpus (5 cites,
``store_reaper.py``):

| Path                          | ms/lint (200 runs) |
|-------------------------------|--------------------:|
| ``ast.parse`` fallback (prior) |               10.46 |
| Graph fast-path, warm cache   |                0.38 |
| Graph fast-path, cold cache   |               32.15 |

Net win in the steady-state consultants server (graph stays
cached across consult turns). One-shot script invocations like
``scripts/lint_consult_answer.py`` pay the cold-start tax but
still produce identical output.

New tests: ``tests/test_code_graph_enclosing.py`` (13 tests:
schema correctness, line-containment lookup including innermost-
span tiebreak, module-scope/EOF edges, mtime cache invalidation,
no-graph and legacy-graph fallback). ``tests/test_citation_linter.py``
gains ``TestGraphFastPath`` (3 tests: end-to-end graph-path
catch, fallback-when-graph-missing, fallback-when-file-outside-
graph). Both envs full sweep clean: 3808 + 3710 passing.

### Fixed — consultants: primary cwd line in allowed-roots log now uses display form (#199, 2026-05-18)

The 2026-05-18 M14 re-runs showed the `allowed roots:` log block
rendering symlinked extras correctly (`/shared/dev/laserRMT
(/srv/dev-disk-by-label-opt/dev/laserRMT)`) but the **primary**
cwd line still showed the bare realpath:

```
primary: /srv/dev-disk-by-label-opt/dev/claude-hooks
extra:
  /shared/dev/laserRMT  (/srv/dev-disk-by-label-opt/dev/laserRMT)
```

Root cause: `consultants/server/app.py` was calling
`Path(cwd).resolve()` upstream of
`discover_allowed_roots_with_display`, so by the time the
discoverer computed the display form it was operating on the
already-resolved path — `display == real` for the primary entry.

Fix: pass `str(Path(cwd).expanduser())` (expanduser-only, symlinks
preserved) to the discoverer; keep the resolved form for the
existing `is_dir()` validation only. Net effect: the primary line
now renders `/shared/dev/claude-hooks
(/srv/dev-disk-by-label-opt/dev/claude-hooks)` the same way extras
do. Behavior unchanged when the cwd has no symlink alias.

New regression test
`TestDiscoverWithDisplay.test_symlinked_cwd_keeps_display_on_primary`
(tests/test_allowed_roots.py) builds an explicit symlink chain and
asserts both the discoverer output and the `render_for_log` round-
trip preserve the alias on the primary entry. All 27 allowed_roots
tests + 60 consultants server/multi-root tests pass.

### Added — consultants: CitationLinter + tightened prompts for path:line fabrications (2026-05-18)

The 2026-05-18 M14 first-real-ask re-run produced a coherent
4-step answer but with mixed-fidelity citations — two distinct
fabrication modes that this commit neutralizes.

**Diagnosis** (full forensic in
`benchmarks/consultants/results/2026-05-18/m14-first-real-ask/rerun-report.md`):

- **glm-5.1:cloud in researcher role lane 5** invented the file
  `consultants/engine/store_sql.py` outright (no such file
  exists). Tool_executor evidence was honest; the researcher
  generated this filename in its REPORT.
- **gemma4:31b in synthesizer role** relayed the fake filename
  forward AND added its own wrong line numbers within real
  files (claimed `_distill_group` at line 128 when it's
  actually at line 360, `_write_summary` at 148 vs the real
  371, `_delete_rows` at 157 vs the real 402 — all four
  citations landed in unrelated regions of `store_reaper.py`).
- **Tool_executor itself was honest** — every cite emitted by
  the tool_executor role traced back to real grep/read_file
  output.

**Fix — two layers, ship together:**

1. **Prompt tightening
   (`consultants/engine/council.py`)** — `RESEARCHER_SYSTEM`,
   `SYNTHESIZER_SYSTEM`, and `SYNTHESIZER_SELF_CRITIC_SYSTEM`
   gain explicit "CITATION INTEGRITY (load-bearing)" blocks.
   Researchers must only cite path:line they verified in
   their own tool_results; synthesizers must only relay
   cites the researchers actually emitted, never introduce
   new ones. The synthesizer prompt names the M14 first-ask
   `store_sql.py` failure mode by name so the model has a
   concrete anti-pattern to avoid.

2. **CitationLinter
   (`consultants/engine/citation_linter.py`)** — new module,
   wired into `synthesizer_node` as a post-output pass. Three
   verification layers per `path:line` cite:

   - **Filesystem**: path must resolve under any of the
     session's allowed_roots. Miss → annotate
     `path:line [unverified — file not found]`.
   - **Bounds**: line ≤ file_length. Beyond EOF → annotate
     `path:line [unverified — file has N lines]`.
   - **AST symbol match** (Python files only — stdlib
     `ast`): when a backticked symbol name appears within
     80 chars before the cite, the cited line's enclosing
     function/class (via `ast.walk`) must match the claimed
     symbol's leaf name. Mismatch → annotate
     `path:line [in <actual>, not <claimed>]`. Filters
     Python literals/keywords/builtins + exception-class
     suffixes from claimed-symbol candidates so legitimate
     prose like "DistillationFailed exception caught at
     `…:129`" doesn't trigger false positives.

   The linter is **non-blocking**: it annotates inline, never
   rejects the answer. A clean answer survives byte-identical
   (idempotent on re-runs via the substring marker check). The
   `state.extra_roots` plumbing now reaches LangGraph state via
   `runner.py` so the linter sees the full session-allowed
   directory set, not just cwd.

   Validation against the prior failed-fabrication answer
   (`scripts/lint_consult_answer.py csl-2026-05-18-1031-9e3b`):
   **5 fabrications caught** in the on-disk answer — both
   `store_sql.py:41-61` + `store_sql.py:61` flagged as
   file-not-found; `_distill_group:128`, `_write_summary:148`,
   `_delete_rows:157` flagged as wrong-symbol-at-line. The
   annotated answer is checked in at
   `benchmarks/consultants/results/2026-05-18/m14-first-real-ask/answer-linted.md`.

**New files**:

- `consultants/engine/citation_linter.py` — module (~360 lines)
- `tests/test_citation_linter.py` — 26 tests covering regex
  extraction, single-cite verification, AST symbol matching,
  full pipeline, and the csl-2026-05-18-1031-9e3b regression
  fixture
- `scripts/lint_consult_answer.py` — operator-facing CLI for
  retroactive answer lint; auto-falls-back to on-disk
  `metadata.json` when the HTTP service doesn't hold the
  session in memory
- `benchmarks/.../answer-linted.md` — annotated prior answer

**Verification.** 3730 + 3632 = 7362 passing tests across both
conda envs (up from 7310; +26 linter tests × 2 envs). M12
parity holds (no default behavior change — linter is wired
unconditionally into the synthesizer but is a pure annotation
pass when the answer has no fabrications).

### Changed — consultants: log readability — symlink-aware allowed_roots + transcript.db path (2026-05-18)

Two operator-quality-of-life fixes surfaced during the
2026-05-18 M14 first-real-ask re-run.

**1. Allowed-roots log shows the user-facing path, not the
realpath.** When the operator's `~/.claude/settings.json`
`additionalDirectories` points at `/shared/dev/<x>` (a
symlink), the consultants engine's `allowed roots:` log line
used to render the realpath canonical form
(`/srv/dev-disk-by-label-opt/dev/<x>`) because
`discover_allowed_roots` realpath-canonicalizes for security
+ de-dup. Confusing — operators don't recognize the path
they typed.

Fix:

- `claude_hooks/allowed_roots.py` grows
  `discover_allowed_roots_with_display(...)` which returns
  parallel `(realpath_list, display_list)` lists — same
  length, same order. Realpaths are still used for the
  tool-sandbox security check; display is the pre-realpath
  user-facing form (`~` expanded but symlinks NOT resolved).
- `render_for_log` gets an optional `display_roots=` kwarg.
  When supplied, the log line shows the display path; when
  the display differs from realpath, the realpath appears
  in parentheses for forensic clarity:
  `extra:\n  /shared/dev/laserRMT  (/srv/dev-disk-by-label-opt/dev/laserRMT)`.
- `SessionState` (`consultants/server/app.py`) grows parallel
  `extra_roots_display: list[str]` and `cwd_display:
  Optional[str]` fields; the runner's two `render_for_log`
  call sites (consult + follow-up) thread the display info
  through. Follow-up merge reuses the parent's display map.

**2. transcript.db path logged at session start.**
`MessageRecorder` opens the per-session SQLite sidecar at
`<cwd>/.claude-hooks/consultants/<sid>/transcript.db`, but
the convention was only documented in the runner source.
Now `_build_recorder` (`consultants/server/runner.py`)
emits an INFO log line `consultants sid=<sid> transcript.db:
<abs_path>` so operators can locate the sidecar from
`/root/.claude/claude-hooks-consultants.log` without
grepping the source.

**Tests.** `tests/test_allowed_roots.py` grows 9 new tests
covering `render_for_log` with display kwarg (parens vs not,
length mismatch) + `discover_allowed_roots_with_display`
(cwd-only, symlink keeps display, dedup by realpath,
add_dirs, consistency vs the realpath-only helper). Both
conda envs now at 3704 + 3606 = 7310 passing (no
regressions).

### Fixed — M14 follow-up: researcher REPORT-mode regression + pgvector concurrency race (2026-05-18)

Diagnosed and fixed three issues caught by the first live
`claude-consultants consult` after the M14 default-on flip
(SID `csl-2026-05-18-0937-4f4f`, 737 s, refusal answer).

**1. Researcher REPORT-mode prompt regression
(`consultants/engine/`).** The xhigh consultation stayed in
PLAN mode for every researcher round — every turn emitted
`{"tool_plan": [...]}` JSON instead of a research report, so
the synthesizer correctly concluded "no findings available"
and refused the answer.

Root cause: the M6 REPORT-mode appendix
(`build_tool_plan_user_appendix` in
`consultants/engine/tool_executor.py`) dumped tool results but
contained no explicit "write a report now" instruction. In M13
this was load-bearing — the model defaulted to free-text
reports — but two M14-era changes combined to break it:

- **Peer findings leaking into REPORT mode.** With the M14
  default-on store, sibling lanes' findings get recalled into
  the researcher's prompt as a "## Peer findings (recalled from
  earlier lanes)" block built by `format_findings_block`. In
  PLAN mode this is helpful (lets the lane dedup against what
  other lanes already tackled), but in REPORT mode it competes
  with the PRIOR TOOL RESULTS appendix and steers
  `gemini-3-flash-preview` toward re-planning to "verify what
  the peers found." The store row `L2-56807ca7491b` captured
  the smoking gun: `"The prior tool results gave a good
  overview but I need to verify the actual line numbers and
  code paths myself, since line numbers may be stale."`
- **No explicit "report now" instruction.** Once the model
  was nudged toward re-planning, nothing in the prompt told it
  not to.

Fix:

- `consultants/engine/council.py`: in the M6 researcher branch,
  rebuild `msgs` with `peer_findings=None` when
  `report_mode=True` so REPORT-mode prompts no longer carry
  the peer-findings block.
- `consultants/engine/tool_executor.py`: append an explicit
  "REPORT NOW. Do NOT emit another `tool_plan` block. Write a
  plain-prose research finding citing the evidence you have…"
  closing instruction to `build_tool_plan_user_appendix` so
  the dispatch decision is unambiguous regardless of prompt
  context.

**2. pgvector concurrency race
(`claude_hooks/providers/pgvector.py`).** The same session
emitted 5 `current transaction is aborted, commands ignored
until end of transaction block` warnings during researcher
fanout. The smoking gun: 4 of the 5 fires landed in a 6-second
window during simultaneous tool_executor lanes hitting the
same shared `PgvectorProvider` instance. psycopg's connection
is **not** thread-safe — concurrent `cursor()` calls on the
same connection race each other's transaction state, producing
the abort cascade.

The 2026-05-18 rollback fix caught each abort post-facto but
did not prevent the race. M14 makes this hot because (a) the
consultants engine fans out 3+ concurrent researcher lanes at
xhigh and (b) every recall AND every store call now traverses
the same provider instance.

Fix: `PgvectorProvider.__init__` grows
`self._lock = threading.RLock()`. Every public method that
touches `self._conn` (`recall`, `store`, `count`,
`expire_before`, `refresh_expires_at`, `delete_by_hashes`,
`batch_recall`, `batch_store`, `recall_hybrid`,
`kg_create_entities`, `kg_add_observations`,
`kg_create_relations`, `kg_search_nodes`) now wraps its full
SQL sequence in `with self._lock:`. RLock (not Lock) because
`kg_search_nodes` re-enters via `recall_hybrid`. The lock is
held for the embedding call too — that costs latency under
fanout but keeps the patch minimal and is correctness-safe.
Each except clause that catches a SQL exception now also
attempts `self._conn.rollback()` so a half-aborted txn doesn't
leak into the next lock-acquirer.

**3. Cleanup.** 10 rows from session
`csl-2026-05-18-0937-4f4f` deleted from `consultants_store` so
the 30-day TTL doesn't fire a distillation LLM call on garbage.
One-liner used (operator reference only):
`DELETE FROM consultants_store WHERE
(metadata->'namespace')::jsonb ? 'csl-2026-05-18-0937-4f4f'`.

**Verification.** 3696 + 3598 = 7294 passing tests across both
conda envs (no regressions). Live re-run with the original
question pending (next entry in this log when complete).

### Added — M14 follow-up: store embedder config + install.py wiring + TTL backfill script (2026-05-18)

Three pieces that close the M14 "production-ready" gap surfaced
during deployment to solidPC:

**1. Store embedder config plumbing.** The M14 ProviderBackedStore
needs an embedder to turn `content` into vectors at write /
recall time. Pre-this-commit, `_load_pgvector` and
`_load_sqlite_vec` didn't read `embedder` / `embedder_options`
off `StoreConfig` — the provider fell back to `NullEmbedder` and
every store call would have raised `EmbedderError`. Smoke
worked because it built the provider directly with embedder
options; the daemon path would have spun.

Fix:

- `StoreConfig` grew `embedder: Optional[str]` and
  `embedder_options: dict` fields, mirroring the shape used by
  the main recall pipeline's `providers.<name>` block in
  `claude-hooks.json`.
- TOML merge layer (`_merge_layer`) reads `embedder` /
  `embedder_options` from `[store]` and from a nested
  `[store.embedder_options]` table.
- TOML render emits both fields so `save_config` →
  re-`load_config` round-trips cleanly. When no embedder is
  configured, the renderer emits a commented-out template so
  hand-editing operators see the right shape.
- `_load_pgvector` / `_load_sqlite_vec` thread the fields into
  the provider's `options` dict via the new
  `_merge_embedder_options` helper.

**2. `install.py` consultants-store wiring** —
`_setup_consultants_store(cfg, ...)` is a new helper called
from `_install_consultants` after the conda env install. It
inspects the main recall pipeline's
`providers.pgvector` / `providers.sqlite_vec` blocks and:

- If pgvector is enabled with an embedder → consultants
  `backend = "pgvector"`, DSN copied across, dedicated
  `pgvector_table = "consultants_store"` to keep recall +
  consultants writes separate, embedder block borrowed.
- Else if sqlite_vec is enabled with an embedder → consultants
  `backend = "sqlite_vec"`, dedicated db path
  `~/.claude/consultants-store.db`, embedder block borrowed.
- Else → leaves M14 defaults but prints a warning that the
  store will fail at runtime without manual embedder config.

The helper writes via `consultants.config.save_config(scope=
"user")` so the canonical TOML render path is used — hand-
edited and CLI-mutator round-trips stay intact.

**3. `scripts/backfill_expires_at.py` — retroactive TTL.**
Pre-M14 rows have `expires_at = NULL` and live forever by
design (correct default-preserving behaviour on upgrade — see
the [`feedback_pgvector_rollback_hygiene`](../../../root/.claude/projects/-srv-dev-disk-by-label-opt-dev-claude-hooks/memory/feedback_pgvector_rollback_hygiene.md)
memory and the M14 plan's "Non-goals" section). But operators
sometimes WANT to age out historical data ("anything older than
6 months"). The new script does exactly that, opt-in, with
safety rails:

```bash
# Dry-run first — reports counts + SQL, no writes:
python scripts/backfill_expires_at.py \
    --dsn "postgresql://user:pass@host/db" \
    --table memories_qwen3 \
    --days 180 \
    --dry-run

# Apply:
python scripts/backfill_expires_at.py \
    --sqlite-vec-path ~/.claude/consultants-store.db \
    --table memory \
    --days 30
```

Safety rails:

- Only touches rows where `expires_at IS NULL` — M14-stamped
  rows are untouched.
- `--days <= 0` refused (would expire everything immediately).
- `--max-rows N` cap (default 1_000_000) prevents accidental
  multi-hour migrations on giant tables.
- `--dry-run` mode reports counts + the actual SQL.
- Unsafe table names refused via the same regex pgvector
  provider uses (`_safe_table`).
- Refuses with a clear error when the table lacks `expires_at`
  (M14 migration hasn't run yet).
- Per-tx commit so a Ctrl-C mid-run leaves a partial backfill
  that re-running with the same `--days` picks up cleanly.
- Stdlib-only beyond psycopg / sqlite3 — psycopg only imported
  when `--dsn` is supplied.

Exit codes: 0 (applied), 1 (nothing to update / usage), 2
(backend error), 3 (SIGINT).

**Tests** (+20 since the flip baseline of 3735 / 3637):

- `tests/test_consultants_v2_store_embedder.py` — 11 tests
  covering field defaults, TOML merge, TOML render round-trip,
  `_load_pgvector` / `_load_sqlite_vec` thread the embedder
  into provider options.
- `tests/test_backfill_expires_at.py` — 9 tests via
  subprocess invocation against a real sqlite db: dry-run
  reports counts without writes, apply updates only NULL rows
  + preserves M14-stamped rows, missing column errors, no-NULL
  rows returns rc=1, --max-rows cap refuses oversized backfill,
  --days safety rails, unsafe table name refused, --dsn /
  --sqlite-vec-path mutually exclusive.

**Verification** (both envs full sweep):

- **claude-hooks-consultants**: 3757 / 30 (+20).
- **claude-hooks**: 3659 / 128 (+20).
- M12 parity green in both envs.

### Fixed — pgvector M14 migration + transaction-abort regressions surfaced by live distillation smoke (2026-05-18)

Two real defects landed by the M14 commit `aac87e7`, both caught
by the live distillation smoke against the user's solidPC
pgvector (run write-up at
[`benchmarks/consultants/results/2026-05-18/m14-distillation-smoke/report.md`](benchmarks/consultants/results/2026-05-18/m14-distillation-smoke/report.md)).

**1. Pre-existing tables never received the `expires_at` column.**

`PgvectorProvider._create_table` early-returned at the table-
exists check, BEFORE the M14 `ALTER TABLE … ADD COLUMN
IF NOT EXISTS expires_at` and the partial index. Net effect:
the user's live `memories_qwen3` (and every pre-M14 table on every
host) would never grow `expires_at`, and any subsequent
`expire_before` query would fail with
`column "expires_at" does not exist`. This was the worst kind of
M14 bug — silent for fresh tables (which worked), catastrophic
for upgrades.

**Fix**: split the create branch from the migration branch in
[`claude_hooks/providers/pgvector.py:470`](claude_hooks/providers/pgvector.py).
`CREATE TABLE` is skipped when the table exists; the additive M14
migration (ALTER + CREATE INDEX, both `IF NOT EXISTS`) runs on
every connection. The migration block also catches failures and
calls `self._conn.rollback()` before re-raising, so a permissions
issue or DDL race doesn't leave the connection aborted for the
next caller.

**2. Recall paths left the connection aborted on soft failure.**

`recall_hybrid`'s BM25 leg and `_search_tables`'s vector leg both
caught query exceptions as "soft" failures (BM25: tables without
`content_tsv` should degrade to vector-only; vector: per-table
errors should just skip that table). Both logged + continued
WITHOUT calling `self._conn.rollback()`. PostgreSQL leaves the
connection in an aborted state until a rollback fires, so the
very next query on the same connection — in the smoke's case,
`provider.expire_before` 35 s later — saw
`current transaction is aborted, commands ignored until end of
transaction block`.

This was a pre-existing pgvector defect, not introduced by M14 —
M14 just surfaced it because the M14 smoke uses an ad-hoc test
table that doesn't have `content_tsv` (canonical migration-script
tables do). Any caller that hit a malformed table on a non-canonical
DSN would have tripped it.

**Fix**: every soft-failure `except` in `recall_hybrid` (vector +
BM25 legs) and `_search_tables` now calls
`self._conn.rollback()` in a guarded inner try before the
`continue`. The rollback restores the connection so the next
caller can use it.

**Regression gate** — new test class
`TestCreateTableMigratesExistingTables` in
[`tests/test_pgvector_expires_at.py`](tests/test_pgvector_expires_at.py):

- `test_existing_table_still_gets_alter_and_index` — locks the
  bug-1 fix at the SQL-statement level.
- `test_migration_rolls_back_on_failure` — locks the
  rollback-before-raise contract so a future refactor can't
  silently regress the aborted-transaction behaviour.

**Verification** (both envs full sweep):

- **claude-hooks-consultants**: 3737 pass / 30 skip (+2 new
  pgvector regression tests vs the 3735 post-flip baseline).
- **claude-hooks**: 3639 pass / 128 skip (+2 same).
- **Live smoke**: ✅ End-to-end PASS. 4 research findings seeded,
  expired after 30 s, distilled by `gemma4:31b-cloud` in 25.71 s
  into a 2073-char summary; written to
  `("project", "6ba3d13e1e58")`; originals deleted; project
  namespace verified to hold 1 distilled entry.

The smoke is permanent at
[`benchmarks/consultants/results/2026-05-18/m14-distillation-smoke/`](benchmarks/consultants/results/2026-05-18/m14-distillation-smoke/)
and can be re-run on any host that has psycopg + a reachable
pgvector + the daemon-managed llamafile embedder.

### Changed — `/consultants` v2 M14 default-on flip (2026-05-18)

The companion to commit `aac87e7` (M14 land). With the TTL +
distillation chain shipped and verified, the shipped defaults
now flip to "store on, self-curating":

| Field                              | Before flip | After flip                              |
|------------------------------------|-------------|-----------------------------------------|
| `store.enabled`                    | `false`     | `true`                                  |
| `store.backend`                    | `"memory"`  | `"sqlite_vec"`                          |
| `store.sqlite_vec_path`            | `null`      | `"~/.claude/consultants-store.db"`      |
| `store.ttl.enabled`                | `false`     | `true`                                  |
| `store.distillation.enabled`       | `false`     | `true`                                  |

**Why sqlite_vec (not pgvector) as the shipped default**: lowest-
friction persistence (a single file under `~/.claude/`, no daemon
dependency) that still triggers the TTL + distillation chain.
Hosts that run claude-hooks against pgvector (e.g. solidPC)
configure `backend = "pgvector"` explicitly. Hosts that want zero
new state set `backend = "memory"` or `store.enabled = false`.

**Effort-gate safety net preserves M12 parity**: `enable_at_efforts
= ("high", "max", "xmedium", "xhigh", "xmax", "xauto")` excludes
the default `effort = "medium"`. So a plain `claude-consultants
ask` run at default effort still pays zero store cost — the
factory short-circuits to `None` because of the gate, not because
of `enabled = false`. The flip becomes observable only at high+
tiers, where x-tier diversity benefits most from cross-lane
recall.

**Parity tests updated** (`tests/test_consultants_v2_parity.py`):
- `test_store_disabled_by_default` → `test_store_enabled_by_default`
  (assertion inverted).
- New: `test_store_backend_is_sqlite_vec_by_default`,
  `test_store_ttl_enabled_by_default`,
  `test_store_distillation_enabled_by_default`.
- `test_make_consultants_store_returns_none_on_default` →
  `test_make_consultants_store_returns_none_at_default_effort`
  (verifies the effort-gate safety net explicitly — same outcome,
  the comment now documents *why*).

**Pre-M14 configs continue to work unchanged**: explicit
`[store] enabled = false` (or any explicit backend) is preserved
by the TOML merge layer. Only configs with NO `[store]` block see
the new defaults take over.

**Verification** (both envs full sweep):

- **claude-hooks-consultants**: 3735 pass / 30 skip (+3 new
  parity tests vs the M14-land baseline of 3732).
- **claude-hooks**: 3637 pass / 128 skip (+3 same parity tests
  vs the M14-land baseline of 3634).
- **M12 parity** — `pytest -m parity` — green in both envs.

This is the post-M14 manual flip the user committed to in writing
during the M14 plan ("flip store.enabled = True immediately after
M14 ships so accumulation pressure starts the moment we land").
Same shape as the M11c-5 atomic flip after the M11c-2 +
M11c-3 land + verify sequence.

### Added — `/consultants` v2 per-namespace TTL + distillation-on-expiry for the M8 store (M14, task #104, 2026-05-18)

The M8 LangGraph BaseStore adapter ships with TTL semantics and a
Caliber-style distillation pass that consolidates expiring
research findings into a durable project-level summary *before*
the originals are deleted. **Episodic short-term → semantic long-
term**, mirroring how humans consolidate working memory into
autobiographical memory. The user committed in writing to flip
`store.enabled = True` immediately after this lands so the
accumulation pressure starts the moment it's available.

**The three pieces ship together as one commit on `dev`** — any
one alone is wrong (TTL alone deletes signal; cleanup alone is
just TTL with extra steps; distillation without TTL never
fires):

1. **TTL** — both providers grow a first-class `expires_at`
   column.
   - **pgvector**: `ALTER TABLE … ADD COLUMN IF NOT EXISTS
     expires_at TIMESTAMPTZ` + partial index `WHERE expires_at IS
     NOT NULL`, emitted at `_create_table` time. PG 9.6+
     supports the IF NOT EXISTS form — idempotent on every boot.
   - **sqlite_vec**: bumped `LATEST_VERSION = 2`. New
     `_migrate_v1_to_v2` step adds `expires_at TEXT NULL` +
     partial index. Mirrors the v0→v1 pattern: bookkeeping table
     drives one-shot lazy migration on first open.
   - **Shared helper**: `claude_hooks/providers/_content_hash.py`
     grew `compute_expires_at(now, ttl_seconds)` and the
     provider-agnostic `ExpiringRow` dataclass.
   - **ProviderBackedStore** (`consultants/engine/store.py`):
     `_do_put` stamps `expires_at` on metadata when the namespace
     has a TTL; `_do_search` / `_do_get` filter expired items;
     `_do_search` hits trigger `provider.refresh_expires_at` when
     `refresh_on_read = True`.
   - **Provider API**: both providers grew three new methods —
     `expire_before(*, before_iso, limit=1000) -> list[ExpiringRow]`,
     `refresh_expires_at(content_hash, new_expires_iso)`,
     `delete_by_hashes(hashes) -> int`. Provider-agnostic in
     shape so the daemon has no per-store SQL knowledge.

2. **Cleanup** — `consultants/engine/store_reaper.py` is a new
   daemon thread that runs in the consultants-daemon. Mirrors
   `embedding_manager._reaper_loop`'s 0.5 s-slice pattern for
   responsive shutdown. Each sweep tick calls
   `provider.expire_before` with a 5-minute grace window, groups
   rows by `(sid, kind)`, and dispatches:
   - **research** above `min_entries_per_distillation` → distill
     then delete.
   - **research** below the threshold → delete without
     distillation (cost gate).
   - **tool_results** / unknown → delete unconditionally.
   - **Critical invariant**: the reaper **only deletes research
     originals after a successful distillation write to the
     project namespace**. If every model in the configured
     fallback chain fails, `DistillationFailed` propagates and
     the originals stay in place — the next sweep tick retries.
     This is the single line of defense against the "TTL deleted
     my findings before distillation could capture them" failure
     mode.

3. **Distillation** — `consultants/engine/distillation.py` is the
   Caliber-style summarizer. Reads expiring rows from one
   session's research namespace, builds a prompt with the rubric
   (retain file:line citations + decisions + gotchas + open
   questions; drop process narration + retries + padding), calls
   the distillation LLM with a fallback chain, writes the
   resulting summary into the durable
   `("project", project_id)` namespace via the new
   `write_distilled_summary` helper. `project_id` is
   `sha256(Path(cwd).resolve())[:12]` — deterministic,
   collision-resistant, no extra registry table needed.

**User-locked defaults (2026-05-17 + 2026-05-18 plan)**:

| Knob                            | Default               |
|---------------------------------|-----------------------|
| `store.ttl.research_days`       | 30 days               |
| `store.ttl.tool_results_hours`  | 24 hours              |
| `store.ttl.project_days`        | `null` (never)        |
| `store.ttl.user_days`           | `null` (never)        |
| `store.ttl.refresh_on_read`     | `true`                |
| `store.distillation.model`      | `gemma4:31b-cloud`    |
| `store.distillation.fallback_models` | `["glm-5.1:cloud"]` |
| `store.distillation.sweep_interval_seconds` | `3600` (1 h) |
| `store.distillation.min_entries_per_distillation` | `3` |
| `store.distillation.max_session_entries` | `50` (~30 k tokens) |
| `store.enabled`                 | **`false` still**     |

The default-on flip on `store.enabled` is a separate manual step
the user will take after this lands — same shape as the
M11c-5 atomic flip.

**App wiring** — `consultants/server/app.py`'s `create_app` now
accepts `cfg` and `ollama_base_url` kwargs; when both are passed
AND `cfg.store.enabled` is True AND either TTL or distillation
is enabled, `_maybe_start_store_reaper` spawns the daemon
thread and stashes it at `app.state.store_reaper`. The FastAPI
`shutdown` hook stops it with a 5 s timeout. Pre-M14 callers
that don't pass `cfg` keep working unchanged.

**Backfill**: none. Pre-M14 rows have `expires_at = NULL` and
live forever, which is the correct default-preserving behavior.
A user who wants retroactive TTL has to write it themselves — a
future helper can land if anyone asks.

**Verification** (both envs full sweep):

- **claude-hooks-consultants**: 3732 pass, 30 skip (was 3632
  post-M13; +100 new M14 tests).
- **claude-hooks**: 3634 pass, 128 skip (was 3550 post-M13; +84
  net, M14 tests that import LangGraph-free pieces also run
  here).
- **M12 parity** — `pytest -m parity` — 23 + 12 sub-tests green
  in consultants env, 14 + 5 sub-tests green in main env.
  TTL/distillation default `enabled = False` keeps behavior
  identical to M12 baseline.

**New modules** (`~700` LOC engine + `~840` LOC tests):

- `consultants/engine/distillation.py` (~420 LOC) — prompt,
  rubric, fallback chain, project_id derivation, summary write
  helper.
- `consultants/engine/store_reaper.py` (~340 LOC) — daemon
  thread, sweep loop, group-by-sid-and-kind helper.
- `tests/test_consultants_v2_store_ttl.py` (24 tests) — TTL
  filter, refresh-on-read, factory plumbing.
- `tests/test_consultants_v2_distillation.py` (26 tests) —
  prompt shape, fallback chain, project_id, write helper.
- `tests/test_consultants_v2_store_reaper.py` (18 tests) —
  grouping, lifecycle, happy paths, failure isolation.
- `tests/test_consultants_v2_app_store_reaper.py` (7 tests) —
  app-factory wiring gates.
- `tests/test_pgvector_expires_at.py` (15 tests) — DDL shape +
  expire/refresh/delete query shape (no live PG).
- `tests/test_sqlite_vec_schema_v2.py` (10 tests) — v1→v2
  migration idempotency, fresh-DB v2 migration, multi-step
  v0→v1→v2 path, write+read column.

**Non-goals (this commit)**:

- Live distillation smoke is deferred to a separate
  user-confirmed run after the commit lands (mirrors M11c-4
  absorbed-by-M13 pattern).
- TTL UI in `claude-consultants config show` — render the new
  blocks, but no interactive editing skill until anyone asks.
- Tool-results distillation — explicitly out of scope per the
  user-locked decisions table; the daemon just deletes them.
- Default-on flip — `store.enabled` stays `False`. Manual flip
  by the user after M14 lands.

### Fixed — `/consultants` v2 LangGraph Send state-isolation bug surfaced by M13 live smoke (2026-05-17)

The M13 live x-tier smoke (task #102, the milestone whose explicit
purpose is full-council end-to-end verification) **caught the M11c-3
regression the stubbed tests couldn't reach**: `_fanout_after_tool_executor`
omitted `tool_results` from its per-lane Send dict. LangGraph 1.2's
`Send` dispatch delivers ONLY the dict's keys to the target node —
channels not in the Send dict are absent from the receiver's state
even when a global `operator.add` reducer is registered for that
channel. The first live consultation at `xhigh` against the
`multipkg` fixture stayed in PLAN mode in **47 of 49** researcher
calls and synthesized a 180-char "could not perform the audit"
tombstone instead of a real answer.

**Root cause** (verified by a 4-line LangGraph experiment, now
locked in the regression suite as
`test_send_isolation_baseline_when_tool_results_omitted`): each
fanned-back researcher saw an empty `tool_results` channel in its
isolated Pregel state, so `tool_results_for_round` returned empty,
and `researcher_node` fell back to PLAN mode instead of consuming
the executor's results in REPORT mode.

**The fix**: `_fanout_after_tool_executor` now computes
`lane_results` per Send — filtered by `parent_round == current_round`
AND (`parent_lane_idx == this lane` OR `parent_lane_idx is None`
for legacy rows) — and includes it under the `tool_results` key in
every emitted Send dict. The #103 composition guarantee is
preserved at the data layer: no sibling-lane results leak into any
lane's Send.

**Regression gate** — new test class
`TestFanbackSendCarriesToolResults` in
[`tests/test_consultants_v2_tool_executor_xtier_composition.py`](tests/test_consultants_v2_tool_executor_xtier_composition.py):

- `test_each_fanback_send_delivers_its_own_lane_results` — builds
  a minimal real `StateGraph` with stub nodes, drives a real Send
  fanout, asserts each receiver saw its own filtered
  `tool_results` (no cross-pollution AND no PLAN-mode fallback).
- `test_send_isolation_baseline_when_tool_results_omitted` — locks
  the underlying LangGraph contract: omitting `tool_results` from
  a Send dict yields empty receiver state. If this test ever
  starts failing, LangGraph changed semantics and the
  explicit-pass workaround can be dropped.
- `test_engine_fanback_dict_includes_tool_results` — white-box
  check on the production closure: every emitted Send dict must
  contain `tool_results`, filtered to that lane.

The 3 new tests are the regression gate the M11c-3 stubbed tests
lacked — they invoke the routing function with a synthetic dict
and inspect its return Sends, but never drive Pregel.

**Rerun** (`csl-2026-05-17-2336-5124`, post-fix): 218.30 s,
29 of 38 researcher calls in REPORT mode (9 PLAN + 29 REPORT —
one PLAN per lane, multi-round REPORT cycles), 77 tool calls,
single critic pass with no reroute, 756-char structured answer
with file:line citations. All 9 lanes pass the headline #103
no-cross-pollution contract via the post-hoc inspector at
[`benchmarks/consultants/results/2026-05-17/m13-smoke/m13_inspect.py`](benchmarks/consultants/results/2026-05-17/m13-smoke/m13_inspect.py).

**Verification**:

- Both envs full sweep: 3550 + 3632 passing (+3 isolation tests
  in the consultants env vs the M11c-3 baseline of 3629).
- M12 parity holds — the change is additive to the Send dict
  contents, no schema changes, no externally observable API
  shifts.
- Live x-tier smoke: ✅ PASS.

The full M13 writeup including run-1 vs run-2 numbers, the final
answer, and the cross-pollution analysis is at
[`benchmarks/consultants/results/2026-05-17/m13-smoke/report.md`](benchmarks/consultants/results/2026-05-17/m13-smoke/report.md).

Task #102 (M13) closes with the fix-up; task #103 (proper
composition) and M11c-5 (default-on flip) stay in place — the
engine refactor was correct in shape, it just needed the explicit
Send-channel passthrough that LangGraph 1.2 semantics require.

### Changed — `/consultants` v2 tool_executor flipped to enabled-by-default (M11c-5, 2026-05-17)

The atomic flip of the two defaults that the M11c-1/2/3 sequence
was building toward. **Both parts of the two-part gate from the
M11c plan have now cleared**:

1. ✅ **Rubric pass** (M11c-2, commit `235fe6c`):
   `gemma4:31b-cloud` at `pass_rate=87.5%` AND
   `avg_quality_score=5.00` — won every tiebreaker among 4
   models tied on pass rate. Baselines row in
   [`docs/consultants-skill-eval-baselines.md`](docs/consultants-skill-eval-baselines.md).
2. ✅ **Task #103 (x-tier proper composition) resolved**
   (M11c-3, commit `e62fd85`): per-lane `parent_lane_idx`
   threading + the new `_fanout_after_tool_executor`
   conditional edge replacing the M6 unconditional
   `tool_executor → researcher` edge. The no-cross-pollution
   contract is pinned by
   [`tests/test_consultants_v2_tool_executor_xtier_composition.py`](tests/test_consultants_v2_tool_executor_xtier_composition.py).

**The atomic flip**:

- [`consultants/engine/tool_executor_defaults.py`](consultants/engine/tool_executor_defaults.py):
  `RECOMMENDED_DEFAULT_ON: bool = True` (was `False`).
- [`consultants/config.py`](consultants/config.py):
  `DEFAULT_ENABLED_BY_ROLE["tool_executor"] = True` (was
  `False`). The M11c-3 parity test
  `test_scaffold_default_on_matches_runtime_default` enforces
  bit-for-bit alignment between these two constants so a future
  commit can't quietly desync them.

**What this changes for users**:

- A fresh `claude-consultants ask` run now compiles the graph
  with the `tool_executor` lane wired. PLAN-mode researcher
  delegates tool intents (`survey_project` / `list_files` /
  `read_file` / `glob` / `grep` / `recall_memory`) to a
  dedicated specialist running `gemma4:31b-cloud`, then folds
  the cited evidence into REPORT mode.
- The "researcher with inline tool subloop" topology of M6
  remains accessible — set `[role.tool_executor].enabled =
  false` in `~/.claude/consultants.toml` (one line) to revert
  to the legacy shape.
- x-tier (`xmedium` / `xhigh` / `xmax` / `xauto`) consultations
  benefit from the M11c-3 proper-composition wiring: each
  researcher lane in REPORT mode sees only its own
  ToolResults, no cross-pollination from sibling lanes.

**Test updates**:

- `tests/test_tool_executor_defaults.py`:
  `test_recommended_default_on_still_false_pending_103` →
  `test_recommended_default_on_true_after_103_resolved`. Class
  `TestM12ParityGuarantee` →
  `TestM11c5RuntimeDefault` (the parity-guarantee framing
  changed; the new class pins the new runtime state).
- `tests/test_consultants_v2_parity.py`:
  `test_tool_executor_role_disabled_by_default` →
  `test_tool_executor_role_enabled_by_default`.
- `tests/test_consultants_config.py`:
  `test_opt_in_roles_disabled_by_default` →
  `test_opt_in_roles_default_state`; the `coder`-only
  disabled-by-default check stays. One pipeline-order test
  gains an explicit `tool_executor.enabled = False` to keep
  its focus on the critic-disable invariant.

**Verification**:

- Both envs full sweep: 3550 + 3629 passing, **zero
  regressions** vs the M11c-3 baseline. Every test that pinned
  the old `False` default was updated coherently — no silent
  failures left behind.
- M12 parity: 13 + 5 sub-tests / 22 + 12 sub-tests green with
  the renamed assertion.
- The M11c-3 stubbed x-tier composition suite (19 tests) stays
  green — the engine wiring is unchanged from M11c-3, only the
  default-on bit moved.

**Live x-tier validation** belongs to M13 (task #102, still
pending — the live-smoke milestone whose explicit purpose is
full-council end-to-end verification including ops runbook +
CHANGELOG cut). The skill-eval protocol explicitly carves out
that the per-role bench does NOT exercise the full council; M13
is the canonical home for that work.

### Changed — `/consultants` v2 tool_executor + x-tier proper composition (M11c-3, task #103, 2026-05-17)

The engine refactor that makes the optional `tool_executor`
role compose cleanly under Phase 9 multi-model researcher
fanout at x-prefixed effort tiers (xmedium / xhigh / xmax /
xauto). The M6 wiring assumed a single researcher lane; under
x-tier the scalar `awaiting_tool_results` flag's last-writer-
wins reducer made dispatch ambiguous and the unconditional
`tool_executor → researcher` edge barriered all parallel
researcher lanes into a single REPORT-mode invocation that saw
the union of every lane's `tool_results` — cross-pollination.
**M11c-3 fixes all three failure modes documented in the
`graph.py:1025-1030` scope note.**

The post-M11c-2 user-confirmed work, following the path the
[[feedback_xtier_diversity_priority]] memory mandates: invest in
proper composition rather than auto-gate the role off at x-tier
(Option 3, rejected). The M11c-2 bench cleared part 1 of the
two-part default-on gate (87.5% / 5.00 well above the 70% / 3.5
rubric floors); this commit clears part 2 (engine wiring) but
**does not flip the default-on bit** — that's reserved for
M11c-4 (live x-tier validation) and M11c-5 (the atomic flip).

**What changed**:

- [`consultants/engine/state_v2.py`](consultants/engine/state_v2.py)
  - `ToolPlanItem` and `ToolResult` gain a
    `parent_lane_idx: Optional[int] = None` field — records
    WHICH researcher lane emitted the plan item (the globally-
    unique lane index from `_fanout_after_planner`), not the
    plan-item position within a single researcher's output
    (that's the existing `lane_idx`, unchanged).
  - **Dropped**: the scalar `awaiting_tool_results: Optional[bool]`
    field from `CouncilStateV2`. The post-researcher router
    now derives the dispatch decision from `tool_plan` vs
    `tool_results` directly — strictly more robust than a
    last-writer-wins scalar under N×M parallel writes. Legacy
    checkpoints that still carry the key are tolerated (the
    TypedDict ignores unknown keys); the engine simply stops
    writing it.
  - `tool_results_for_round(state, round, *, parent_lane_idx=None)`
    grows the optional per-lane filter. `None` returns every
    matching-round result (the pre-#103 single-researcher
    contract); a set value returns only results from that
    researcher lane (plus legacy `parent_lane_idx=None` rows for
    in-flight checkpoint tolerance).

- [`consultants/engine/tool_executor.py`](consultants/engine/tool_executor.py)
  - `parse_tool_plan(text, *, parent_round, parent_lane_idx=None)`
    — the researcher_node passes its own `state["lane_idx"]`
    so every emitted item records its originating researcher.
  - `tool_executor_node` reads `state["parent_lane_idx"]` from
    the Send slice and stamps it on every emitted `ToolResult`
    (all four construction sites: happy path, missing-item
    tombstone, loop-import failure tombstone, loop-exception
    tombstone).

- [`consultants/engine/council.py`](consultants/engine/council.py)
  - M6 researcher_node branch threads its own `lane_idx` as
    `parent_lane_idx` into both `parse_tool_plan` (PLAN mode
    stamps items) and `tool_results_for_round` (REPORT mode
    filters to own-lane results).
  - **Drops** the four `awaiting_tool_results=True/False` writes
    in the return dicts (PLAN normal, empty-plan fallback,
    REPORT normal, M6 error path). The router no longer reads
    the field.

- [`consultants/engine/graph.py`](consultants/engine/graph.py)
  - `_route_after_researcher` drops the scalar flag read. The
    `completed` set keys on the full per-lane identity tuple
    `(parent_round, lane_idx, parent_lane_idx)` so x-tier
    sibling lanes don't mask each other's unconsumed items.
    Each emitted Send carries `parent_lane_idx` so
    tool_executor can stamp it on the result.
  - **New** `_fanout_after_tool_executor` conditional edge —
    replaces the M6 unconditional `tool_executor → researcher`
    edge. Reads current-round `tool_results`; extracts distinct
    non-None `parent_lane_idx` values; if empty (single-
    researcher path), returns the string `"researcher"` —
    identical to the old unconditional edge. Otherwise re-
    derives each lane's `(plan_item, model_override)`
    deterministically via the same
    `group_items_into_lanes(plan_items, FANOUT_MAX_LANES)` +
    `[primary] + extras` shape `_fanout_after_planner` uses, and
    emits one Send per distinct `parent_lane_idx`. Defensive
    paths: malformed partition / out-of-range index / no
    current-round results all degrade gracefully to the single-
    researcher fanback rather than crashing.
  - Replaces the obsolete scope note at the old `graph.py:1025-1030`
    block with the post-refactor description.

**M12 parity preserved**:

- `DEFAULT_ENABLED_BY_ROLE["tool_executor"]` stays `False` —
  default-config consultations don't register the tool_executor
  node at all, so the new edges don't even exist on the
  default-config graph.
- `tests/test_consultants_v2_parity.py` adds one new assertion
  in `TestOptInsOffByDefault`:
  `test_awaiting_tool_results_field_dropped_from_state` — locks
  the schema change.

**New test surface** (~430 LOC):

- [`tests/test_consultants_v2_tool_executor_xtier_composition.py`](tests/test_consultants_v2_tool_executor_xtier_composition.py)
  — 19 tests across 6 classes covering the data round-trip
  (`parse_tool_plan` stamps, `tool_executor_node` stamps), the
  no-cross-pollution headline contract (2 researcher lanes × 3
  plan items each — each lane sees only its own 3 results),
  the routing topology (compiled graph branches contain the
  new `_fanout_after_tool_executor` conditional), the fanback
  closure behavior (single-researcher → `"researcher"` string;
  x-tier → one Send per `parent_lane_idx` with correct
  lane_idx + model_override; pathological states degrade
  gracefully), and the `_route_after_researcher` decision rule
  (unconsumed items dispatch with `parent_lane_idx` propagated;
  x-tier sibling lanes with shared `lane_idx` don't mask each
  other thanks to the full identity tuple key).
- `tests/test_consultants_v2_tool_executor.py` gains 5 new
  round-filter overload tests on the widened helper signature.

**Verification**:

- Both envs full sweep: 3550 + 3629 passing, zero regressions
  vs the M11c-2 baseline of 3536 + 3604.
- New x-tier composition suite: 19 tests pass in consultants
  env (langgraph available); 8 pass + 11 properly skipped in
  main env (the langgraph-gated routing tests).
- M12 parity unchanged at the behavioral layer; +1 new
  static-shape assertion locking the dropped scalar field.

**Non-goals (deferred to M11c-4 / M11c-5)**:

- **No default-on flip**. `RECOMMENDED_DEFAULT_ON` stays
  `False`, `DEFAULT_ENABLED_BY_ROLE["tool_executor"]` stays
  `False`. The user-confirmed live x-tier validation (M11c-4)
  must land green first; the atomic flip is M11c-5.
- **No live cloud calls in this commit**. All new tests are
  stubbed.
- **No round-counter refactor**. The pre-existing
  `research_rounds_used` additive-reducer pathology at x-tier
  (sums across N×M REPORT-mode returns instead of counting
  rounds) is orthogonal and not surfaced by any current test
  — leaving it for a future commit if M11c-4 surfaces a real
  failure mode rooted in it.

### Added — Tool_executor skill-eval bench M11c-2 closeout (live cohort, 2026-05-17)

The live cohort that turns the M11c-1 dry-run-only scaffold into a
**bench-grounded recommendation**. 48 trials across 6 cloud models
× 8 questions × 1 trial each, gemma4:31b-cloud judging on the 1-5
quality scale. **Outcome**: `gemma4:31b-cloud` is the recommended
model when the role is enabled; `RECOMMENDED_DEFAULT_ON` stays
`False` pending task #103 (x-tier proper composition).

**Results headline** (full table in
[`docs/consultants-skill-eval-baselines.md`](docs/consultants-skill-eval-baselines.md#tool_executor-v10-2026-05-17)):

| Model | pass_rate | avg_quality | avg_wall_s | avg_tool_calls | rubric |
|-------|----------:|------------:|-----------:|---------------:|--------|
| `gemma4:31b-cloud` | **0.875** | **5.00** | 4.9 | 2.6 | ✅ pick |
| `glm-5.1:cloud` | 0.875 | 4.12 | 6.9 | 2.2 | ✅ qualifying |
| `kimi-k2.6:cloud` | 0.875 | 4.50 | 11.3 | 3.0 | ✅ qualifying |
| `deepseek-v4-pro:cloud` | 0.875 | 4.25 | 6.7 | 2.4 | ✅ qualifying |
| `gemini-3-flash-preview:cloud` | 0.750 | 4.00 | 5.1 | 4.0 | ✅ qualifying |
| `qwen3-coder-next:cloud` | 0.625 | 3.62 | 7.1 | 4.4 | ❌ < 0.70 floor |

Four models tied on pass rate at 0.875; `gemma4:31b-cloud` won
every tiebreaker — perfect avg judge quality (5.00 / 5.00),
fastest avg wall (4.9 s vs 6.7–11.3 s for the other tied models),
and low avg tool-call cost (2.6 calls — beaten only by glm-5.1's
2.2, but glm-5.1's 4.12 quality drag cost it the tiebreaker).
Notably this matches the M6 fallback default
(`DEFAULT_MODEL_BY_ROLE["tool_executor"] = "gemma4:31b-cloud"`) —
the empirical bench confirms the trace-data intuition that put
gemma4 in the role's seed config.

**`tool_executor_defaults.py` populated**.
[`consultants/engine/tool_executor_defaults.py`](consultants/engine/tool_executor_defaults.py)
swaps the M11c-1 sentinel string for the bench winner:

- `RECOMMENDED_TOOL_EXECUTOR_MODEL = "gemma4:31b-cloud"`
- `RECOMMENDED_AS_OF = "2026-05-17"`
- `RECOMMENDED_SUITE_VERSION = "1.0"`
- `RECOMMENDED_SUITE_HASH_PREFIX = "7921555c"`
- `RECOMMENDED_DEFAULT_ON = False`  *(still gated by task #103)*

`tests/test_tool_executor_defaults.py` flips the two M11c-1
"scaffold" assertions to their M11c-2 form
(`test_recommended_model_populated_by_m11c2`,
`test_recommended_default_on_still_false_pending_103`); 8/8 tests
green.

**Default-on bit stays `False`** because part 2 of the two-part
gate from [`/root/.claude/plans/recursive-petting-planet.md`](.) is
unresolved. Part 1 (rubric pass: `pass_rate ≥ 0.70` AND
`avg_quality ≥ 3.5`) clears comfortably at 87.5% / 5.00. Part 2
(task #103 x-tier proper composition) is the open user-facing
decision — three paths surveyed in the M11c-1 plan: Option 1 (doc
deferral, recommend tool_executor for base tiers only), Option 2
(engine refactor for per-lane `awaiting_tool_results` + post-
barrier merge router + per-lane round filtering), Option 3 (auto-
gate at runtime — last resort per
`feedback_xtier_diversity_priority`). When part 2 resolves, a
separate engine commit flips `RECOMMENDED_DEFAULT_ON=True` AND
wires `DEFAULT_ENABLED_BY_ROLE["tool_executor"]=True` atomically
so the M12 parity test
(`test_scaffold_default_on_matches_runtime_default`) stays green.

**Bench artifacts** at
[`benchmarks/consultants/results/2026-05-17/tool_executor/`](benchmarks/consultants/results/2026-05-17/tool_executor/)
— `metadata.json` (suite hash, harness version, model list, cost
estimate, judge model, git SHA), `trials.jsonl` (48 rows, one per
trial, includes `tool_call_log` + `final_text` + judge score +
rationale), `report.md` (per-model + per-question pass/fail
matrix). Re-run with
`claude-consultants skill-eval tool_executor --live --accept-cost`
to refresh.

**What the failures told us**. 9 of 48 trials failed the oracle —
discriminating signal, exactly as the suite was designed to
produce:

- **`medium-02-redundancy-test` fired on 4 of 6 models** (the
  question's answer is already in the `why:` frontmatter block;
  passing requires ≥0 filesystem tool calls). Only `glm-5.1:cloud`
  and `deepseek-v4-pro:cloud` recognised the redundancy and
  returned the answer without re-reading files. The other 4 issued
  unnecessary `read_file` / `grep` calls.
- **`medium-01-multifile-audit` cost qwen3-coder-next 5 reads and
  gemini-3-flash-preview hit `max_iterations=6`** before
  converging — the failure mode for "summarise N files" tasks is
  high-iteration loops on models with weaker plan-then-act
  behaviour.
- **`hard` tier passed 100% across all 6 models**. The "hard"
  label was misnamed for these two questions (ambiguous
  `survey_project → glob → read` chain + cite-exact-line) —
  candidate fix for a future `1.0.1` suite bump.
- **`trivial-02-read-section` × glm-5.1:cloud** produced 0 tool
  calls — the model answered from the question task text without
  reading the README. Oracle correctly rejected it.

**Verification**:

- Live bench exit code: 0. Total wall: 339 s for 48 trials + 48
  judge calls.
- Both envs full sweep: 3434 tests + 102 sub-tests passing, zero
  regressions vs the M11c-1 baseline.
- M12 parity unchanged: 13 tests + 5 sub-tests green.
- `DEFAULT_ENABLED_BY_ROLE["tool_executor"]` still `False`,
  `DEFAULT_MODEL_BY_ROLE["tool_executor"]` still
  `"gemma4:31b-cloud"` — bit-for-bit identical to the M6 / M11c-1
  state.

### Added — Tool_executor skill-eval bench harness (M11c-1, task #99 + #103 prelude, 2026-05-17)

The Consultancy Skill-Eval Protocol's **tool_executor**
sub-protocol — the empirical bench that picks the recommended
model for `cfg.roles.tool_executor.model` and gates whether the
role flips from disabled-by-default to enabled-by-default.
**M11c-1 ships the harness + suite + tests + an empty
`tool_executor_defaults.py` scaffold as a dry-run-only commit;
the live cloud spend lives in a separate M11c-2 closeout commit
per the M11b / M11a precedent.**

**Why a third sub-protocol.** The M6 `tool_executor` role
consumes a `ToolPlanItem` emitted by the researcher in PLAN
mode, runs a full agent loop with the shared tool stack
(`survey_project` / `list_files` / `read_file` / `glob` / `grep`
/ `recall_memory`), and returns a `ToolResult` with citations
the researcher folds into REPORT mode. Unlike coder (which
**writes** code) and stall (a pure measurement bench), this
suite measures **reading + reasoning over an existing codebase
via tool calls**. Different skill axis, different evidence
needs, same Consultancy Skill-Eval Protocol shape.

**New files**:

- [`benchmarks/consultants/tool_executor_bench.py`](benchmarks/consultants/tool_executor_bench.py)
  (~1400 LOC) — orchestration. `cmd_main(argv)` entry point;
  iterates `(question × model × trial_idx)`; drives
  `tool_executor_node` directly with a per-trial ChatClient,
  the production tool stack, and a `_ToolCallCapture` recorder
  that captures the ordered tool-call log + per-iteration token
  usage; runs the per-question oracle pytest with three env
  vars (`TOOL_EXEC_OUTPUT`/`TOOL_EXEC_CALLS`/`TOOL_EXEC_FIXTURE_DIR`)
  threaded through via `run_pytest_against_sandbox(extra_env=...)`;
  optionally calls a judge LLM (1-5 + rationale) on each
  completed answer; writes `metadata.json` + `trials.jsonl` +
  `report.md`. The dry-run path replays per-question scripted
  tool calls through the **real** tool callable (so the closure
  + sandbox + arg parsing all get exercised) and stubs only
  the model call — same "every dry-run trial passes the oracle"
  promise as the coder bench.
- [`benchmarks/consultants/questions/tool_executor/SUITE.md`](benchmarks/consultants/questions/tool_executor/SUITE.md)
  plus 8 question files × 4 tiers + 8 pytest oracles + a
  synthetic fixture corpus under
  [`fixtures/`](benchmarks/consultants/questions/tool_executor/fixtures/)
  (8 cohorts: `simple_constants`, `readme_basic`, `auth`,
  `multipkg`, `todos`, `redundant`, `serializers`,
  `configdrift`). Hand-authored Python + Markdown; each
  question's frontmatter names its `fixtures_subdir` and the
  bench scopes the tool sandbox to it so file paths resolve
  inside the cohort, not the bench cwd. Suite hash `7921555c`.
  Notable traps: `medium-02-redundancy-test` rewards the model
  that recognises the answer is already in the question's
  `why` block (penalises ≥2 filesystem tool calls);
  `hard-02-cite-correct-line` rewards precision (rejects the
  comment line + the historical value `100`).
- [`consultants/engine/tool_executor_defaults.py`](consultants/engine/tool_executor_defaults.py)
  (~105 LOC) — empty per-model scaffold mirroring
  `stall_defaults.py` and `coder_defaults.py`. Ships with
  `RECOMMENDED_TOOL_EXECUTOR_MODEL=""` (sentinel) and
  `RECOMMENDED_DEFAULT_ON=False` so `DEFAULT_ENABLED_BY_ROLE
  ["tool_executor"]` stays bit-for-bit unchanged from M6 — the
  M12 parity test
  (`test_scaffold_default_on_matches_runtime_default`) holds.
- [`tests/test_tool_executor_defaults.py`](tests/test_tool_executor_defaults.py)
  + [`tests/test_tool_executor_bench_harness.py`](tests/test_tool_executor_bench_harness.py)
  + [`tests/test_consultants_cli_v2_m11c.py`](tests/test_consultants_cli_v2_m11c.py)
  — 8 + 19 + 21 = 48 new tests covering scaffold shape, M12
  parity guarantee, `ToolExecTrial` schema + `fixtures_subdir`
  plumbing + cost estimator + the new
  `run_pytest_against_sandbox(extra_env=...)` parameter +
  oracle dispatch end-to-end + CLI sub-subparser dispatch +
  Namespace → bench-argv translation.

**Harness changes (mostly additive)**:

- [`benchmarks/consultants/harness.py`](benchmarks/consultants/harness.py)
  gains `ToolExecTrial` (27 fields covering bookkeeping +
  outcome + cost + tool-call mechanics + soft quality) and
  `estimate_tool_exec_cost(questions, models, *,
  trials_per_question=1, judge_model=None)`.
  `BenchQuestion` grows an optional `fixtures_subdir: str = ""`
  field, threaded from frontmatter through `load_questions`.
  `run_pytest_against_sandbox` grows an optional
  `extra_env: Optional[dict] = None` parameter so the
  tool_executor bench can pass `TOOL_EXEC_*` env vars without
  duplicating the timeout / junit / safety-net logic. Empty /
  None values preserve v1.0.1 behaviour exactly.
- [`consultants/cli.py`](consultants/cli.py) gains the
  `skill-eval tool_executor` sub-subparser + the
  `cmd_skill_eval_tool_executor(args, base)` handler — same
  shape as the coder/stall handlers (`--dry-run | --live`
  mutually-exclusive, `--accept-cost`, `--models`,
  `--ollama-base`, `--judge-model`, `--trials`,
  `--output-dir`, repeatable `--tier`/`--id`, `--smoke`).

**Rubric** (in SUITE.md frontmatter, mirrors coder):

> A model qualifies for the tool_executor role default iff
> `pass_rate ≥ 0.70` AND `avg_quality_score ≥ 3.5`. Among
> qualifying models the recommended default is the one with the
> highest pass rate; ties break on `median_tokens`.

**Two-part gate for flipping the default-on bit** (separate
decision from the model pick, see
[`docs/consultants-skill-eval-protocol.md`](docs/consultants-skill-eval-protocol.md#tool_executor-sub-protocol-v10)):
the rubric must pass AND task #103 (x-tier proper composition)
must resolve. Until both clear, the role stays opt-in even if
M11c-2 picks a winner.

**Verification**:

- Dry-run smoke: 2 trials, both pass through the real tool
  callable + real oracle pytest + real fixtures.
- Dry-run full cohort × 2 models: **16/16 PASS** end-to-end.
- `--live` without `--accept-cost`: exits rc=2 with the cost
  estimate (~1.38M tokens + 74K judge for 48 trials across the
  6-model cohort × 8 questions × 1 trial).
- 87 tests across CLI + bench harness + defaults + stall
  regression: 0 regressions.
- M12 parity: 13 tests + 5 subtests, untouched.

**Non-goals (deferred to M11c-2 / #103)**:

- No live cloud calls in M11c-1.
- No engine wiring of `tool_executor_defaults.py` into the
  runner. Module ships as importable scaffold; the actual
  default-on flip is a separate user-confirmed commit gated by
  both the rubric pass AND #103 resolving.
- No #103 engine refactor in M11c-1. The user-facing decision
  flow (Option 1 doc deferral / Option 2 proper composition /
  Option 3 auto-gate at runtime) happens after M11c-2 data
  lands.

### Added — Per-language coder model routing with failover (task #111, 2026-05-17)

The optional `coder` role (M10) now picks its model **per
language** instead of using a single global model for every code-
generation task. Each entry — and the global default — carries a
`primary` and a `fallback`, giving the lane an opt-out-able two-
step failover chain. The defaults are seeded from the v1.0.1-mlang
bench (`docs/consultants-skill-eval-baselines.md`).

**Data model.**
[`consultants/engine/state_v2.py`](consultants/engine/state_v2.py)
gains a frozen `CoderLanguageRoute(primary, fallback="")`. Both
the per-language map and the global default share that shape.

**Defaults.**
[`consultants/engine/coder_defaults.py`](consultants/engine/coder_defaults.py)
gains `LANGUAGE_BY_EXTENSION` (extension → language id),
`language_from_path()`, `RECOMMENDED_CODER_ROUTES_BY_LANGUAGE`
(six in-cohort languages × `(primary, fallback)`),
`RECOMMENDED_CODER_DEFAULT_ROUTE` (used when a language has no
per-language entry), and `resolve_coder_route()` — a single-source
pure-function resolver used by both the graph and the tests.
`RECOMMENDED_AS_OF` bumps to `2026-05-17`, the suite version
becomes `1.0.1-mlang`, and the hash prefix is `ddef8095`. The
legacy `RECOMMENDED_CODER_MODEL` constant stays as the v1
back-compat fallback.

**Config.**
`RoleConfig` (`consultants/config.py`) gains
`routes_by_language: dict[str, CoderLanguageRoute]` and
`default_route: Optional[CoderLanguageRoute]`. Both are seeded for
the coder role only; other roles keep them empty / None. The TOML
emitter writes `[role.coder.default_route]` and `[role.coder.routes.<lang>]`
sub-tables; the merger round-trips through `_coerce_route()`. New
helpers `coder_resolve_route(cfg, lang)` and `coder_unique_models(cfg)`
back the graph wiring + the runner's per-model ChatClient
materialisation.

**Failover semantics** (in `consultants/engine/coder.py`). The
`coder_node` signature gains an optional `model_chain_resolver`
parameter; when set, the lane walks the resolved
`[(client, model_name), ...]` chain in order. Triggers:
exception, no artifacts written, OR empty final assistant message
— strictest wins for the recorded reason. Each attempt rebuilds
the sandbox so a partial write from a failed attempt doesn't leak
into the next; the recorder + event log tag every attempt with
the actual model used. A new typed event
`CoderFailover(from_model, to_model, reason, attempt_idx,
next_attempt_idx, error_preview)` lands between attempts AND once
more with `reason="chain_exhausted"` on full failure. Back-compat:
when `model_chain_resolver` is `None`, the node behaves exactly as
before (single-attempt call using `chat_client` + `model`
kwargs). On chain exhaustion the tombstone names BOTH models:
`primary <m1> and fallback <m2> both failed: <last error>`.

**Graph + runner wiring.** `GraphDeps`
(`consultants/engine/graph.py`) grows three fields:
`coder_chat_clients_by_model`, `coder_routes_by_language`,
`coder_default_route`. `_wrap_coder` builds a resolver closure
over them — empty config falls through to the v1 single-attempt
shape (M12 parity-safe). `consultants/server/runner.py`
materialises one TracedChat per unique model named across the
per-language map + default route + legacy fallback. Follow-ups
inherit the warm per-model dict via a new
`SessionState._coder_chat_clients_by_model` field so a follow-up's
fallback lane reuses the parent's warmed-up ChatClient.

**CLI.** `claude-consultants config` grows a sub-namespace:

```
claude-consultants config coder list
claude-consultants config coder set <lang> --primary <m> [--fallback <m>]
claude-consultants config coder unset <lang>
claude-consultants config coder set-default --primary <m> [--fallback <m>]
```

All accept the existing `--cwd` / `--project` scope flags. Empty-
string `--fallback ""` is the explicit-clear sentinel; `None` (no
flag) is "keep current".

**Skill dialog.** `.claude/skills/consultants/SKILL.md` grows a
new top-level menu option (**"4. Coder routing"**) and **Subflow
E** that walks the user through editing the global default, per-
language entries, adding a new language, or removing an entry. The
`config show` block also renders the routing table for coder when
present.

**Tests.** Three new files (~50 tests):
- `tests/test_coder_defaults_language_map.py` — extension → lang
  → route resolution, default-table shape, provenance stamps.
- `tests/test_config_coder_routes.py` — RoleConfig seeding, TOML
  round-trip, partial-override merge, the three mutators.
- `tests/test_cli_config_coder.py` — argparse round-trip + handler
  dispatch + validation errors.
- Plus 8 extension cases in `tests/test_consultants_v2_coder.py`
  for the resolver dispatch + every failover trigger.

Full suite: **3418 tests + 101 sub-tests passing**, zero regressions.

### Added — `/consultants` v2 behavior-parity regression suite (M12, task #98, 2026-05-17)

M12 closes the v2 overhaul's most important non-feature promise:
every milestone landed under M0–M11 + #111 was supposed to be
**opt-in, default-off**, so a default-config consultation behaves
bit-for-bit like the v1 council it replaces. The new
`tests/test_consultants_v2_parity.py` is the regression gate that
holds that promise — three cohorts of tests, locked to current
default behavior at this commit, that fail loudly if any v2
opt-in starts leaking into the default path.

**Why "parity" and not "v1 diff".** There is no v1 in tree to
compare against. The suite snapshots *current* default behavior
and treats that as the baseline. Future changes to the default
path WILL break the suite — which is correct: a change to the
default path is a behavior change, not a refactor. Each new
opt-in must come with a new off-by-default test in cohort 2.

**Cohort 1 — Per-effort-tier scenarios** (`TestPerEffortTierParity`,
9 tests + 7 subtests). Runs a real `build_council_graph` for each
of `low / medium / high / max / xmedium / xhigh / xmax` with stub
chat clients that return deterministic content keyed on role
name. Each tier asserts:

- the `final_answer` equals the synthesizer stub's pinned output,
- no `CoderFailover`, `RuntimeMutation`, `Interrupt`, `Resumed`,
  or `DeadlineWarning` event fires on a default-config run,
- the planner / researcher / synthesizer trio actually invoked
  their stub clients (each `StableChat` records calls).

The `xauto` tier gets its own test that verifies the escalator
stays at `xmedium` when no dissent signal arrives.

**Cohort 2 — Opt-ins off by default** (`TestOptInsOffByDefault`,
11 tests + 5 subtests). Static-state assertions on a fresh
`ConsultantsConfig()`:

| Gate | Assertion |
|------|-----------|
| `cfg.runtime.review_before_synthesis` | `False` |
| `cfg.runtime.interrupt_on_low_confidence` | `False` |
| `cfg.roles.tool_executor.enabled` | `False` |
| `cfg.runtime.effort != "xauto"` | true |
| `cfg.store.enabled` | `False` AND `make_consultants_store(cfg) is None` |
| `cfg.roles.coder.enabled` | `False` |
| Coder routes seeded but inert | `coder_unique_models(cfg)` empty when role disabled |
| Non-coder roles have empty routing | `routes_by_language == {}`, `default_route is None` |
| `cfg.checkpointer.backend` | `"sqlite"` (the zero-dep default) |
| `V2_OPT_IN_EVENT_KINDS` | covers `coder_failover / runtime_mutation / interrupt / resumed / deadline_warning` |

**Cohort 3 — 2h-session stall replay** (`TestStallRecoveryReplay`,
2 tests). Replays the 2026-05-15 audit-session pathology that
motivated M3:

- `test_stalled_lane_is_retried_then_tombstoned` — patches
  `time.monotonic` via a `MockedClock`, feeds a stub that emits 3
  tokens then hangs forever. Asserts the M3 `StallMonitor` raises
  `_StallCancelled` within the configured `stall_threshold_s`
  mocked seconds — well inside a 20-minute mocked-wall budget,
  vs the 127-minute pathology v1 produced.
- `test_slow_but_progressing_lane_is_not_killed` — stub that
  emits 1 token every 60 mocked seconds for 20 mocked minutes
  (legitimate slow thinking). Asserts no stall fires; lane
  completes normally. Validates the M3 design goal: the stall
  detector punishes hangs, not deep work.

**Test infrastructure.** A new `tests/_parity_helpers.py` (1
file, ~280 LOC) holds the shared kit: `StableChat`,
`RoundAwareChat`, `HangAfterTokensStream`, `SlowButProgressingStream`,
`MockedClock`, `_StallCancelled`, `capture_events()`
context-manager that patches `consultants.engine.events.emit`,
`assert_no_v2_optin_events()`, `events_by_kind()`,
`make_default_stubs()`, and `build_default_deps()`. Three
cohorts share this base; no test mocks past the real engine.

**Pytest marker.** A new `parity` marker is registered in
`pyproject.toml`'s `[tool.pytest.ini_options].markers`. Run only
the parity suite with `pytest -m parity` — handy as a pre-push
gate or for narrowing a CI step.

**Verification.**

- Parity file: 22 tests + 12 sub-tests pass in 2.00 s
  (consultants env).
- Main env full suite: 3431 passed, 98 skipped, 106 sub-tests
  passed, zero regressions.
- Consultants env full suite: 3474 passed (excluding 33
  pre-existing proxy failures unrelated to M12).
- `pytest --collect-only -q tests/test_consultants_v2_parity.py
  | wc -l` confirms the expected case count per cohort.

**Non-goals (deferred).** No v1 reference run (none in tree).
No live cloud calls (M13 owns that story). No new instrumentation
in production code — the suite uses the existing surface end-to-
end. No M14 / TTL work.

### Added — Stall skill-eval Tier-1 live baseline + populated stall_defaults.py (M11a-2, task #99, 2026-05-17)

The Tier-1 closeout of the M11a-1 bench harness: 84 trials (7
cohort models × 4 standalone questions × 3 trials each) against
the live `192.168.178.2:11433` Ollama-Pro proxy, 61 min total
wall, **zero errors**. The 7 cohort models match the M11b-mlang
baseline plus `gemini-3-flash-preview:cloud` (the model that
triggered the 2026-05-15 audit-session pathology).

**Headline finding**: `kimi-k2.6:cloud` measured p99 TTFT
**150 s**, which derives a recommended `stall_threshold_s=390`
— **higher than the existing global default of 300 s**. This
is empirical proof that the M3 detector's current global default
would have falsely tripped `STARTUP_STALL` on kimi calls
periodically. The bench's premise validated.

**Second finding**: `deepseek-v4-flash:cloud` produced a p99
inter-token gap of **4534 ms** and a p99 wall of **256 s** —
both indicate the model has cadence pathology even when not
fully stalled. Derived `hard_cap_s=780` (above the 300 s floor).

**Populated per-model thresholds** in
[`consultants/engine/stall_defaults.py`](consultants/engine/stall_defaults.py):

| Model | `stall_threshold_s` | `hard_cap_s` |
|------|---:|---:|
| `glm-5.1:cloud`              |  90 | 300 |
| `kimi-k2.6:cloud`            | **390** | **540** |
| `gemma4:31b-cloud`           |  30 | 300 |
| `qwen3-coder-next:cloud`     |  30 | 300 |
| `deepseek-v4-pro:cloud`      | 150 | 300 |
| `deepseek-v4-flash:cloud`    | 210 | **780** |
| `gemini-3-flash-preview:cloud` |  30 | 300 |

Out-of-cohort models fall through to `RECOMMENDED_DEFAULT_STALL`
= `(300, 3600)` (matching the existing `control.py` globals) so
the M12 parity guarantee holds bit-for-bit for any model not yet
measured.

**Tier 2 deferred**. Tier 1 already produces clearly
differentiated per-model recommendations; we hold Tier 2 (full
council under load) for if real-world usage flags problems with
these Tier-1-derived defaults. Re-run is a single CLI invocation:
`claude-consultants skill-eval stall --live --tier2
--accept-cost`.

**Provenance** baked into `stall_defaults.py`:
- `RECOMMENDED_AS_OF = "2026-05-17"`
- `RECOMMENDED_SUITE_VERSION = "1.0"`
- `RECOMMENDED_SUITE_HASH_PREFIX = "c8306c62"`
- `RECOMMENDED_TIER_MIX = "tier1-only"`

**No engine-side wiring change in this commit**. The runner /
researcher still reads `runtime_control.stall_threshold_s` /
`per_lane_hard_s` from RuntimeControl as before; wiring
`resolve_stall_thresholds(model)` into the RuntimeControl
defaults is a separate plumbing decision (the constants are
imported and available; the wiring belongs in a follow-up
commit that owns the cross-module ripple).

**Tests**. Updated `tests/test_stall_defaults.py` to assert the
populated map shape:
- All 7 cohort models present.
- `kimi-k2.6:cloud.stall_threshold_s > global default` (the
  headline finding as a regression flag).
- `deepseek-v4-flash:cloud` carries the elevated hard_cap.
- Resolver returns the per-model override for in-cohort models,
  the global default for out-of-cohort.
- Provenance constants stamped correctly.

**Artifacts**:
- Results dir:
  [`benchmarks/consultants/results/2026-05-17/stall-tier1/`](benchmarks/consultants/results/2026-05-17/stall-tier1/)
  (`metadata.json` + `trials.jsonl` + `report.md` + `run.log`).
- Baselines table row in
  [`docs/consultants-skill-eval-baselines.md`](docs/consultants-skill-eval-baselines.md)
  (Stall thresholds section).

### Added — Stall skill-eval bench harness (M11a-1, task #99, 2026-05-17)

The Consultancy Skill-Eval Protocol's **stall** sub-protocol —
the empirical bench that fills the per-model
`(stall_threshold_s, hard_cap_s)` defaults the M3 stall detector
currently picks as conservative global guesses. **M11a-1 ships
the harness + suite + tests as a dry-run-only commit; the live
cloud spend lives in a separate M11a-2 closeout commit per the
M11b precedent.**

**Why two tiers.** The 2026-05-15 audit-session pathology
(gemini-3-flash lanes that held TCP open for 27-31 min without
producing useful tokens) happened inside a **full council under
load** — multi-round researcher, parallel lanes, proxy
back-pressure. A single isolated `chat_streamed` call doesn't
reproduce that pattern, so the bench measures both:

- **Tier 1 — standalone**: one `chat_streamed` per (question ×
  model × trial_idx). Cheap, broad per-model baseline.
- **Tier 2 — fake-consultancy**: a full `build_council_graph`
  run per (question × model × trial_idx) at `effort="medium"`,
  every chat client in `GraphDeps` pinned to the same model so
  we measure that model's cadence under realistic council load.
  Per-call timings captured via a `TimingCaptureChat` wrapper.

When both tiers measured a given model, the M11a-2 derivation
prefers Tier 2 numbers (representative); Tier 1 is the fallback
so models not in the Tier 2 cohort still get a defensible
default.

**New files**:

- [`benchmarks/consultants/stall_bench.py`](benchmarks/consultants/stall_bench.py)
  (~620 LOC) — Tier 1 + Tier 2 orchestration, dry-run + live
  paths, JSONL writer, `metadata.json` writer, `report.md`
  renderer, `derive_thresholds` rule.
- [`benchmarks/consultants/stall_capture.py`](benchmarks/consultants/stall_capture.py)
  (~360 LOC) — `TimingCaptureChat` wrapper around any chat
  client. Intercepts `chat_streamed`, records per-token monotonic
  timestamps, computes TTFT + inter-token gaps + total wall on
  every call. `CallTiming` + `aggregate_calls` helpers.
- [`benchmarks/consultants/questions/stall/SUITE.md`](benchmarks/consultants/questions/stall/SUITE.md)
  plus 8 question files: 4 `standalone-*` researcher-style
  analytical prompts + 2 `council-synth-*` audit-style + 2
  `council-gpqa-*` GPQA-Diamond-style items (physics + biology)
  for external difficulty anchor. Suite hash `c8306c62`.
- [`consultants/engine/stall_defaults.py`](consultants/engine/stall_defaults.py)
  (~140 LOC) — empty per-model scaffold. Ships with
  `RECOMMENDED_STALL_THRESHOLDS_BY_MODEL = {}` and
  `RECOMMENDED_DEFAULT_STALL` set to the existing global
  `(300, 3600)` so importing the module changes **no** runtime
  behavior. M11a-2 populates the map; M11a-2 also wires
  `resolve_stall_thresholds()` into the runtime's
  `RuntimeControl` defaults once data exists.

**Modified**:

- `benchmarks/consultants/harness.py` — new `StallTrial`
  dataclass + `estimate_stall_cost()` helper; `load_questions()`
  gains `require_oracle=False` for measurement benches (the
  stall suite has no oracles).
- `consultants/cli.py` — new `skill-eval stall` sub-subparser +
  `cmd_skill_eval_stall` handler. Mirrors the `coder`
  subcommand's shape with `--tier1` / `--tier2` / `--both` for
  tier selection and `--trials-tier1` / `--trials-tier2` for
  per-tier sample counts.
- `docs/consultants-skill-eval-protocol.md` — expanded the
  previously-placeholder "stall" row in the sub-protocols table
  and added a full "Stall sub-protocol (v1.0)" section covering
  the decision question, two-tier flow, percentile derivation
  rule, and rubric.

**Tests** (no cloud spend; all run against fixtures):

- `tests/test_stall_defaults.py` (13 tests + 5 subtests) —
  M12 parity guarantee: imports change no runtime behavior,
  resolver falls through to the global default, all provenance
  constants present.
- `tests/test_stall_capture.py` (27 tests) —
  `TimingCaptureChat` against fake streaming clients with
  controlled per-token timing; verifies TTFT, inter-token p99,
  `CancelledByOrchestrator` handling, `chat()` non-streamed
  path, error paths, aggregate-across-calls.
- `tests/test_stall_bench_harness.py` (13 tests) — schema
  round-trip, `load_questions(require_oracle=False)` path,
  suite manifest parse, cost estimator across tier
  combinations.

**Verification**:

- All three new test files: **53 passed + 5 sub-tests** in both
  envs.
- M12 parity (`pytest -m parity`): **22 passed + 12 sub-tests**,
  unchanged.
- End-to-end dry-run: `python -m benchmarks.consultants.stall_bench
  --dry-run --both --smoke` exits 0, writes
  `metadata.json` + `trials.jsonl` + `report.md`. Tier 2 captures
  3 chat calls per trial (planner + researcher + synthesizer)
  with non-zero p99 inter-token gaps — confirming the
  `runtime_control` plumbing routes the researcher through
  `chat_streamed`.
- CLI dispatch: `claude-consultants skill-eval stall --dry-run
  --both --smoke` exits 0; `--live --both` without
  `--accept-cost` exits 2 with cost estimate.
- Cost estimate for full live run (defaults): **7 models × 8
  questions × (3 t1 + 2 t2) = 280 trials, ~6.86 M tokens**.

**Non-goals (deferred to M11a-2)**:

- No live cloud calls in this commit.
- No `control.py` defaults change. Until measured data justifies
  it, the global `(300, 3600)` stays.
- No engine-side wiring of `stall_defaults.py`. Module ships as
  importable scaffold only.
- No M11c (tool_executor bench) — separate sub-milestone of #99.

### Added — M11b coder skill-eval first live baseline (2026-05-16)

The Consultancy Skill-Eval Protocol's coder sub-protocol now has
its first **recorded baseline**. The M11b harness shipped in the
previous commit (`4d2da90`) was fired against the live
`192.168.178.2:11433` Ollama Pro proxy — once as an 8-trial smoke
to validate the pipeline end-to-end, then again as a 32-trial
full run to crown a default model. Headline: **all four candidate
models qualified the rubric**, `glm-5.1:cloud` wins on
`avg_quality` (4.88) and the tokens tie-breaker (1841 median).

**`docs/consultants-skill-eval-baselines.md`** — first four rows
landed in the coder table (one per candidate). The `Recommended
default` block names `glm-5.1:cloud` and links to the
results-dir + the constant that holds it.

**`consultants/engine/coder_defaults.py`** (NEW): single-file
home for the recommended coder model + provenance (the run date,
the suite version, the suite hash prefix, the qualifying-models
list at decision time). `consultants/config.py` imports
`RECOMMENDED_CODER_MODEL` and seeds `DEFAULT_MODEL_BY_ROLE`
with it — so the coder role inherits the evidence-based pick
when its TOML doesn't override `[role.coder].model`.

**`benchmarks/consultants/results/2026-05-16/coder/`** — full
artifact dump for the run:
- `quota.md` — pre-smoke / pre-full / post-full Ollama Pro usage
  readings (session 0% → 1.6%, weekly 4% → 4.3%).
- `smoke-trials.jsonl` + `smoke-metadata.json` + `smoke-report.md` +
  `smoke-trials/` — 8 trivial-tier trials, 4 models × 2 questions
  (2 trials failed against the **wrong** `qwen3-next:cloud` name
  before the user-confirmed `qwen3-coder-next:cloud` was wired).
- `trials.jsonl` + `metadata.json` + `report.md` + `trials/` —
  full 32-trial run, all PASS, all 4 models qualifying.
- `full-run.log` + `smoke-runlog.log` — captured stdout for both
  passes.
- `*.broken-2026-05-16-1556` — the pre-fix smoke artifacts where
  every coder trial errored on the tool-executor signature bug,
  kept for the post-mortem record.

**Post-mortem hardening** (`benchmarks/consultants/coder_bench.py`
`_judge_trial_quality`): trial 29 (`trivial-02-strlen ×
kimi-k2.6:cloud`) recorded `quality_score=None` with empty
`quality_rationale` — the kimi judge returned empty content and
the harness coerced that to the same `(None, "")` shape as
"parse failure" / "call raised". Three changes close the
observability gap:

1. Empty-content path now retries the judge call once (cheap
   insurance, mirrors the consultants stall-retry pattern at
   1-call scope).
2. Every failure mode writes a *discriminating* rationale string
   (`"judge returned empty content twice (model=…)"` /
   `"judge call raised: …"` / `"judge text unparseable (first 200
   chars: …)"`) so future trials.jsonl entries fingerprint the
   exact failure without needing the raw response preserved.
3. `tests/test_consultants_v2_benchmarks_smoke.py` gains
   `TestJudgeTrialQualityRetry` (4 cases pinning happy path,
   retry-then-succeed, retry-then-give-up, and call-raised).

**Signature fix** (`consultants/engine/coder.py`
`make_sandbox_tool_executor`): the 2026-05-16 smoke caught a
2-vs-3-positional-arg mismatch between the sandboxed executor
and `claude_hooks/agent_loop/runner.py:147`'s actual call shape.
`_exec(name, args, cwd='', **kw)` now matches the runner; the
dry-run stub in `benchmarks/consultants/harness.py
make_dry_run_loop_runner` also calls with 3 positional args so
future signature drift is caught at smoke time, not after a full
cloud run. Three new regression tests pin the contract.

**Tests**: 3322 main-env pass (+7 over the previous M11b
baseline). Two `tests/test_consultants_config.py` cases were
updated to assert the evidence-based default (`glm-5.1:cloud`
for coder) instead of inheriting global `DEFAULT_MODEL`.

---

### Added — Consultancy Skill-Eval Protocol + coder bench (M11b)

The M11b coder skill-eval is the **first** sub-protocol of a new
**Consultancy Skill-Eval Protocol** — the canonical evaluation
procedure for picking the default model of each consultant role
(coder / tool_executor / researcher / critic / planner /
synthesizer). The protocol is designed to be **rerun every time** a
new candidate model lands on the proxy, so the cost of trying a
candidate is one command + one docs append, not a multi-day effort.

The harness is shared across the three planned sub-protocols
(M11a stall thresholds, M11b coder, M11c tool_executor — only b
ships in this commit; a/c land in follow-up commits as the
infrastructure is ready). The repo also gets a canonical baselines
ledger (`docs/consultants-skill-eval-baselines.md`) — append-only
record of every model × suite × date so future-us can compare new
candidates against the recorded trend.

**`docs/consultants-skill-eval-protocol.md`** (NEW, canonical
methodology document):

- The three sub-protocols (coder / stall / tool_executor) + which
  role's default each one gates.
- Per-trial flow for the coder sub-protocol: sandbox → coder_node
  → produced-file → py_compile → pytest oracle → optional
  judge LLM (1-5 rubric).
- Aggregation + decision rubric (`pass_rate ≥ 70% AND avg_quality
  ≥ 3.5`, tie-broken by `median_tokens`).
- Re-run rules: full re-run on new candidate / cloud disruption /
  suite version bump; smoke re-run on harness change; never
  re-roll the dice on a sub-threshold model.
- Adding-a-question workflow + suite-versioning rules (PATCH /
  MINOR / MAJOR semantics, when a new version requires re-baselining
  every prior model).
- Explicit non-goals: doesn't pick the global default, doesn't
  exercise full council pipeline (that's M12 parity), doesn't
  measure dollar cost (Ollama Pro is quota-based, not USD).

**`docs/consultants-skill-eval-baselines.md`** (NEW): running
ledger of every (model × suite × date) score. Append-only; first
live run lands a row in a follow-up commit.

**`benchmarks/consultants/`** (NEW directory):

- `harness.py` (HARNESS_VERSION = "1.0") — shared infrastructure:
  - `SuiteManifest` + `load_suite_manifest` parse `SUITE.md` with
    a tiny in-house YAML subset (no PyYAML dep). Computes a
    content-stable `suite_hash` over manifest ids + question files
    + oracles so undeclared drift trips the report.
  - `BenchQuestion` + `load_questions` discover + filter (by tier
    or by id). Skips SUITE.md / README.md / NOTES.md.
  - `CoderTrial` dataclass — every measurable metric per
    (question × model). `append_trial` is JSONL-append so Ctrl-C
    mid-run preserves prior trials; `load_trials` round-trips with
    tolerance for malformed lines.
  - `estimate_cost(questions, models, judge_model)` — token-budget
    summary with per-model coefficients tuned from the csl-2026-05-*
    trace battery. Unknown models fall back to a conservative
    mid-point so callers can add a model without touching the
    coeff table.
  - `run_pytest_against_sandbox` — subprocesses pytest with
    `CODER_SANDBOX` env var; 60 s default timeout catches
    runaway-import loops. `OracleResult` captures stdout / stderr /
    returncode for post-hoc inspection.
  - `count_code_lines` + `measure_complexity` (radon soft-dep — no
    hard requirement).
  - `JUDGE_SYSTEM` + `build_judge_messages` + `parse_judge_response`
    — strict 1-5 rubric for the LLM judge; tolerates `SCORE: <n>`
    / `score = <n>` / `<n>/5` shapes.
  - `make_dry_run_loop_runner` — stub agent loop that simulates
    write_file calls + iteration accounting, used by `--dry-run`.
- `coder_bench.py` — M11b runner CLI. Two-phase: `--dry-run`
  (stub ChatClient + stub run_loop + canonical reference
  submissions → every trial passes by design, validates the
  pipeline) and `--live --accept-cost` (real
  `make_agent_chat_client` against `--ollama-base`, default
  `192.168.178.2:11433`). `--smoke` shorthand for `--tier trivial`.
  `--id` / `--tier` filters are repeatable. Estimate prints as a
  summary line at run start; `--live` without `--accept-cost`
  prints the estimate and exits 2 (gate, not stop).
- `analyze.py` — renders `report.md` from a `trials.jsonl`:
  provenance header (harness + suite version + hash + git commit),
  per-model summary table, rubric application + recommended
  default, per-question detail tables, reproducibility footer
  with the exact re-run command.
- `questions/coder/SUITE.md` — coder suite v1.0 manifest:
  - 8 questions × 4 tiers (2 per tier).
  - Rubric: `pass_rate_floor = 0.70`, `quality_score_floor = 3.5`,
    `tie_breaker = median_tokens`.
  - Suite versioning rules (PATCH = oracle tighter only; MINOR =
    new question added → every prior model needs re-baselining;
    MAJOR = rubric change).
- `questions/coder/*.md` + `*-oracle.py` — 8 curated HumanEval-
  style problems with pytest oracles:
  - `trivial-01-truncate` (custom) — first N characters; edge cases.
  - `trivial-02-strlen` (humaneval/23, modified) — length without
    `len()`; constraint is checked via AST inspection.
  - `easy-01-dedupe` (humaneval/26) — stable dedupe; the
    `list(set(items))` trap is caught.
  - `easy-02-fib` (humaneval/55) — Fibonacci; naive recursion
    fails the fib(40) 2s timeout.
  - `medium-01-balance` (custom) — multi-type bracket matching;
    the counter-trap (`"(]"` → True via counter, False via stack)
    is caught.
  - `medium-02-prime-length` (humaneval/82) — prime-length string;
    edge cases (length 0, 1, 2).
  - `hard-01-matrix-path` (classic DP) — min path sum; naive
    recursion fails the 10×10 3s timeout; negatives allowed; input
    validation (empty / jagged).
  - `hard-02-digit-filter` (humaneval/146-style) — count
    multi-digit positives with odd first+last digit; "> 10"
    boundary + signed-vs-abs digit extraction.

**`consultants/cli.py`** gains `claude-consultants skill-eval coder`
— thin wrapper over `benchmarks/consultants/coder_bench.py:main`
so users don't have to remember the script path. Forwards
`--dry-run` / `--live` / `--accept-cost` / `--models` / `--ollama-base`
/ `--judge-model` / `--tier` / `--id` / `--smoke` / `--output-dir`.

**`.claude/skills/consultants/SKILL.md`** gains a "Skill-eval —
pick the right model for a role" section: when to suggest running
the eval, how to invoke it, where to record results. Two paragraphs
+ a CLI example, in line with the rest of the skill doc's
verb-by-verb structure.

**`.gitignore`** adds `benchmarks/consultants/results/` so per-run
sandboxes (which contain model-generated code + pytest stdout) stay
local. The committed artifact is the rendered `report.md` summary
that lands in `docs/consultants-skill-eval-baselines.md` as a
single ledger row.

**Tests**: `tests/test_consultants_v2_benchmarks_smoke.py` — 31
tests covering:

- Suite manifest loading + hash stability across re-loads.
- Question loading (tier filter, id filter, oracle resolution,
  SUITE.md skipped).
- Cost estimator (basic, unknown model fallback, judge tokens
  separate).
- Oracle grader's happy path: **every** canonical reference
  submission in `_DRY_RUN_SUBMISSIONS` passes its respective
  oracle (8 subtests, one per question — locks in the bench's
  "happy path is reachable" guarantee).
- Oracle correctly rejects buggy code + missing modules.
- Code-line counter, judge-message builder, judge response parser
  (canonical / lowercase / slash-5 / no-score / empty).
- Trial JSONL round-trip preserves every field.
- End-to-end dry-run smoke: 16 trials, all PASS, metadata.json
  correct.
- Analyzer rubric: picks qualifier, refuses on no-qualifier, tie-
  breaker prefers lower tokens, report renders cleanly.
- CLI gate: `--live` without `--accept-cost` exits 2 and prints
  the cost summary.

**Verification**: main env 3315 pass (+31 from M10 baseline 3284),
89 skipped (unchanged), zero regressions.

**What ships in M11b vs the rest of M11**:

- M11b (this commit): coder sub-protocol infrastructure +
  questions + bench script + analyzer + skill-eval protocol doc
  + baselines ledger.
- M11a (next): stall sub-protocol — GPQA-Diamond-style questions,
  streaming-token cadence measurement, per-model
  `stall_threshold_s` + `hard_cap_s` recommendations.
- M11c (after M11a): tool_executor sub-protocol — multi-tool
  research tasks, baseline vs gemma4/glm-5.1/qwen3-next
  configurations, decides whether to flip the M6 default-on bit
  and unblocks task #103 (x-tier composition).

The first live run of the coder sub-protocol lands in a follow-up
commit (separate from M11b's infrastructure commit) so the
infrastructure can be reviewed without the user committing to the
cloud spend.

### Added — `/consultants` v2 coder role + sandbox + planner gate (M10)

Closes M10 — the **coder** specialist role for code-generation
consultations. Same shape as the M6 ``tool_executor`` lane (semantic
delegation, additive reducer, recorder-tagged transcript rows) but
with a *sandboxed* tool surface instead of access to the
researcher's full read/grep/glob stack. Ships disabled by default;
the M11b benchmark commit will decide whether to flip a default.

`consultants/engine/coder.py` (NEW, 667 lines):

- ``CODER_SYSTEM`` prompt — anchors the role: write code via
  ``write_file`` only, paths relative to sandbox root, short summary
  message at the end, do NOT paste code into the summary (the
  synthesizer reads files via the artifact listing).
- ``CODER_WRITE_FILE_TOOL_SPEC`` — OpenAI-shape tool spec the agent
  loop serializes to the model. ``{path, content}`` required;
  ``additionalProperties: false`` so the model can't append junk
  fields the executor would silently ignore.
- ``CoderSandbox`` — per-lane sandbox + audit buffer. Tracks
  ``writes`` (list of ``{path, bytes, sha256}`` dicts),
  ``bytes_written`` (running sum), and ``rejections`` (cap-violation
  reasons). Atomic writes via temp-file + ``os.replace`` so a
  partial write never lands on disk.
- ``_normalise_sandbox_path`` — rejects absolute paths, traversal
  (``..``), empty segments, null bytes, and Windows separators
  (normalized to forward slashes). Tested against every rejection
  arm; the path guard is the security boundary, not the tool
  description.
- ``make_sandbox_tool_executor(sandbox)`` — returns an
  ``(name, args, **kw) -> str`` callable the agent_loop runner
  uses. Wraps ``ValueError`` cap violations into the OpenAI
  tool-result error shape so the LLM sees ``"error: per-file cap…"``
  inline and can self-correct rather than crash the lane.
- ``coder_node`` — node entrypoint. Reads ``state["coder_task_item"]``
  (Send-injected per-lane), builds the sandbox, builds the prompt
  via ``build_coder_messages`` (grounding → CODER_SYSTEM → user
  message with question + plan + research + task + sandbox caps),
  invokes ``run_loop`` with the sandbox tool, emits NodeStarted /
  NodeFinished / ToolCall events tagged ``role="coder"``, records
  every iteration to the recorder, and returns
  ``{"coder_artifacts": [CoderArtifact(...)]}``. Tombstones cleanly
  on missing task, empty task, ``run_loop`` exception, and
  zero-files-with-rejections (so the synthesizer surfaces the gap
  rather than silently composing past it).
- ``parse_coder_preamble(text)`` — extracts the planner's
  ``requires_code_generation`` flag from a fenced ``json`` block.
  Tolerates bare JSON / missing fence. Returns ``True`` / ``False``
  / ``None`` (no decidable signal).
- ``parse_coder_tasks(text, parent_round=N)`` — extracts
  ``CoderTaskItem`` entries from the planner's coder-gate JSON
  block. Items missing ``task`` are skipped silently; malformed
  blocks return ``[]``. ``parent_round`` stamps every emitted item
  for forward-compat with a future re-plan path.
- ``PLANNER_CODER_GATE_BLOCK`` — system-prompt fragment appended to
  ``PLANNER_SYSTEM`` when ``cfg.roles.coder.enabled``. Instructs the
  planner to emit ONE fenced JSON block at the end of its reply
  shaped ``{"requires_code_generation": bool, "coder_tasks":
  [{"task", "path", "why"}]}``. Omitted entirely when the planner
  decides no code generation is needed — preserving v1 plan shape
  byte-for-byte for the common case.
- ``build_coder_artifacts_block(artifacts)`` — renders the
  ``coder_artifacts`` channel for the synthesizer's user message.
  Each entry shows the task, file listing with byte counts, and
  truncated summary; tombstones render as ``(task: '...' FAILED:
  error)`` so the synthesizer sees the gap.

`consultants/engine/state_v2.py`:

- ``CoderTaskItem`` (frozen dataclass) — ``task``, ``path``,
  ``why``, ``lane_idx``, ``parent_round``. The Send-payload shape.
- ``CoderArtifact`` (frozen dataclass) — ``task``, ``summary``,
  ``files: list[dict]``, ``lane_idx``, ``parent_round``,
  ``duration_ms``, ``error``. Output shape; ``files`` is plain
  dicts (not a nested dataclass) so they round-trip through JSON
  for SSE + transcript.db with no custom encoder.
- ``CouncilStateV2`` gains ``requires_code_generation``
  (non-additive flag), ``coder_tasks`` (additive list reducer),
  ``coder_task_item`` (per-lane Send slice), ``coder_artifacts``
  (additive list reducer).
- ``coder_artifacts_for_round(state, round)`` — mirror of
  ``tool_results_for_round`` for forward-compat with re-plan flows.

`consultants/engine/graph.py`:

- ``GraphDeps`` gains ``coder_max_file_bytes`` / ``_total_bytes`` /
  ``_files`` fields; consulted only when ``coder`` ∈ ``enabled_roles``.
- ``CouncilState`` declares the new channels + reducers; the
  ``_wire_v2_reducers()`` import-time hook resolves the
  ``CoderArtifact`` / ``CoderTaskItem`` forward refs.
- ``_wrap_planner`` checks ``"coder" in deps.enabled_roles`` at
  compile time and passes ``coder_enabled=`` to ``planner_node`` so
  the gate block is appended.
- ``_wrap_coder`` (NEW) — same wrapper pattern as
  ``_wrap_tool_executor``; binds ChatClient + sandbox caps + sid
  into the node closure.
- **Coder gate routing.** When the role is enabled,
  every existing edge that targets ``synthesizer`` (plan_topology's
  unconditional edges + the critic conditional's ROUTE_SYNTHESIZER
  + M6's route_after_researcher fallthrough + the critic-disabled
  fallthrough) is redirected to a new ``coder_router`` pass-through
  node. The router fires a conditional edge that emits one ``Send``
  per declared ``coder_tasks`` entry when
  ``requires_code_generation=True``, OR routes straight to
  ``synthesizer`` otherwise. After coder Sends complete, an
  unconditional edge ``coder -> synthesizer`` fires (LangGraph
  barriers Send-multiplexed sources before unconditional successors,
  so the synthesizer's ``coder_artifacts`` read sees every lane's
  output merged). Wiring is bypass-free when ``coder ∉ enabled`` —
  the v1 topology is byte-identical, the M12 parity suite stays
  safe.
- ``interrupt_before`` filter accepts ``coder`` / ``coder_router``
  so HITL pause-before-codegen is reachable from the M9 control
  surface.

`consultants/engine/council.py`:

- ``planner_node`` gains ``coder_enabled=False`` kwarg. When True,
  appends ``PLANNER_CODER_GATE_BLOCK`` to its system message and
  parses the response for the coder declaration; success returns
  include ``requires_code_generation`` + ``coder_tasks`` deltas.
  When the planner asserted ``true`` but emitted no tasks (confused
  model), the delta is downgraded to ``False`` so the graph routes
  around the coder cleanly.
- ``build_synthesizer_messages`` gains ``coder_artifacts=None``
  kwarg; when non-empty the rendered block is appended to the
  user message. v1 prompt shape is byte-identical when the channel
  is empty / absent. ``synthesizer_node`` plumbs the channel.

`consultants/config.py`:

- ``"coder"`` joins ``ROLES`` (inserted between ``critic`` and
  ``synthesizer`` to keep canonical pipeline order).
- ``DEFAULT_ENABLED_BY_ROLE["coder"] = False`` — opt-in.
- ``DEFAULT_THINK_BY_ROLE["coder"] = "high"`` — exploratory; M11b
  will tighten per-model.
- No ``DEFAULT_MODEL_BY_ROLE`` override — coder inherits
  ``DEFAULT_MODEL`` (``kimi-k2.6:cloud``). The plan calls out: M10
  ships infrastructure with a config-only default; the M11b commit
  picks the model with evidence.
- ``CoderLimitsConfig`` dataclass — ``max_file_bytes`` (50 KB),
  ``max_total_bytes`` (1 MB), ``max_files`` (16). Wired through
  the TOML parser ``[coder_limits]`` block and the emitter.
- ``ConsultantsConfig.coder_limits`` field surfaces it.

`consultants/server/runner.py`:

- Both ``run_council`` and the follow-up runner thread
  ``cfg.coder_limits`` values into ``GraphDeps`` so the per-lane
  sandbox honors per-session caps.

**Tests**: 73 new tests across two files. ``test_consultants_v2_coder.py``
(67, main env): dataclasses + reducer, sandbox path normalisation +
caps, prompt builder, node happy path + tombstones, parsers (preamble
+ tasks), synthesizer block builder, planner gate. ``test_consultants_v2_coder_e2e.py``
(6, consultants env — langgraph-gated): graph compiles with coder
on/off, routing fires Sends when ``requires_code_generation=True``
+ tasks, falls through to synthesizer when off/empty.

**Verification**: main env 3284 pass (+69 from M9 baseline 3215),
consultants env 3276 pass (excl. 24 pre-existing proxy failures on
both M9 and M10), zero regressions on M0–M9.

### Added — `/consultants` v2 HTTP control surface + CLI subcommands (M9)

Closes M9 — the seven control endpoints exposed by
``consultants.server.app`` for in-flight consultations + mirrored
CLI subcommands. M5 shipped the pure-Python payload builders;
M9 is the FastAPI+HTTP layer that applies them to the live
LangGraph and the CLI that drives them.

`consultants/server/control_routes.py` (NEW):

- ``register_control_routes(app)`` — attaches 7 endpoints under
  ``/v1/consult/{sid}/`` to the FastAPI app:
  * ``GET /state`` — live or last-known StateSnapshot, summarized
    via M5's ``summarize_state_for_get``. Returns a static
    snapshot from SessionState fields when the run is over and the
    live graph handle is cleared.
  * ``POST /inject`` — applies ``build_inject_delta`` via
    ``graph.update_state(..., as_node="researcher")``.
  * ``POST /control`` — applies ``build_runtime_control_delta``
    (validates per-key, rejects unknown keys with 400).
  * ``POST /interrupt`` — flips ``runtime_control.pause_requested``.
  * ``POST /resume`` — clears ``interrupt_state``, then schedules a
    ``Command(resume=value)`` re-invoke on the app's executor pool
    (returns 202-equivalent ``{"mode": "scheduled"}`` so the HTTP
    request stays short; caller polls ``GET /state``).
  * ``POST /cancel`` — flips ``runtime_control.cancel_requested``;
    optional ``discard_partial=true`` triggers checkpoint cleanup.
    Idempotent on completed sessions (200, no-op).
  * ``GET /events`` — SSE stream over the recorder's
    ``runtime_events`` table. Replays everything with
    ``event_id > Last-Event-ID``, then tails for new rows every
    200 ms; heartbeats every 15 s; terminates cleanly when the
    session reaches a final state. Polling-based rather than
    ``astream_events`` subscription because the runner already
    pumps events to the recorder — a second ``astream_events``
    call would kick off a fresh invocation.
- Lifecycle helpers ``_require_session`` / ``_require_live_session``
  / ``_safe_apply_state_delta`` express the HTTP contract once:
  * ``404`` — session not found in memory.
  * ``410`` — session has been closed (idle reap / explicit).
  * ``409`` — session is ``completed`` / ``failed`` (mutations
    rejected explicitly rather than silently swallowed).
  * ``503`` — runner hasn't attached the live graph handles yet
    (millisecond race between ``executor.submit`` and runner's
    first line; clients should retry).
  * ``400`` — payload validation surfaced from the builders.
- FastAPI imports lifted to module level so ``Request`` resolves
  at registration time (a lazy import would leave the annotation
  as a string and FastAPI 422s on the path).

`consultants/server/app.py`:

- ``SessionState._compiled`` / ``_thread_config`` / ``_recorder``
  — live LangGraph handles attached by the runner. Cleared at
  session close so the checkpointer file lock + ChatClient caches
  are released.
- ``create_app`` calls ``register_control_routes`` after the
  v1 routes register. Failure is non-fatal — the app comes up
  without M9 endpoints if the import path is unhappy.

`consultants/server/runner.py`:

- Both the primary and follow-up runners build a
  ``thread_config = {"configurable": {"thread_id": state.sid}}``
  and pass it to ``compiled.stream(...)`` so the checkpointer
  scopes the run to the SessionState's sid. Without this LangGraph
  would generate a synthetic thread_id that the control routes
  can't address.
- ``state._compiled`` / ``_thread_config`` / ``_recorder`` set
  just before the stream loop so an HTTP route that hits the
  endpoint immediately gets a live snapshot (no 503 race except
  in the millisecond window between ``executor.submit`` and the
  first runner line).

`consultants/cli.py`:

- Eight new subcommands mirror the endpoints:
  * ``state <sid>`` — deep state view (vs the v1 ``status``).
  * ``inject <sid> [--role ROLE] (-m TEXT | -f FILE) [--source]``
  * ``control <sid> [--time +30m | --soft-target SPEC |
    --max-rounds N | --max-reroutes N | --confidence FLOAT |
    --strictness lax|normal|strict | --enable ROLE | --disable ROLE]``
  * ``pause <sid> [--reason …]`` (friendlier name for /interrupt)
  * ``resume <sid> [--value JSON] [--decision …]``
  * ``cancel <sid> [--discard-partial] [--reason …]``
  * ``events <sid> [--since EVENT_ID]`` — line-by-line SSE tail
    with ``Last-Event-ID`` resume.
- New helper ``_parse_relative_time(spec)`` parses ``+30m`` /
  ``+2h`` / ``+45s`` / ``+1d`` / bare seconds into an absolute
  ``time.time()`` value for ``deadline_ts`` / ``soft_target_ts``.
- ``control --disable ROLE`` ergonomically issues a ``GET /state``
  first and subtracts from the current ``enabled_roles`` snapshot
  before posting (the wire-level delta replaces outright).

Tests:

- ``tests/test_consultants_v2_control_routes.py`` (24 tests,
  fastapi-gated):
  * GET /state — 404 unknown, snapshot summary live, static
    snapshot when graph cleared, 500 on get_state raise (4)
  * POST /inject — happy path, 400 bad role, 400 empty text,
    404 unknown sid, 409 completed, 410 closed, 503 missing graph (7)
  * POST /control — applies delta, 400 invalid payload, 400
    unknown field, 400 invalid value (4)
  * POST /interrupt — sets pause_requested, default reason (2)
  * POST /resume — clears interrupt + schedules re-invoke (1,
    langgraph-gated)
  * POST /cancel — flips cancel_requested, no-op on completed,
    410 on closed (3)
  * GET /events — 404 unknown, replays then terminates on
    completed, Last-Event-ID skips replayed (3)
- ``tests/test_consultants_cli_v2_m9.py`` (26 tests, main env):
  * argv dispatch wiring for every new subparser (6)
  * ``_parse_relative_time`` — minutes/hours/seconds/days, bare
    number, plus-optional, empty/garbage/zero/negative rejected (10)
  * Handler HTTP shaping — method, URL, body via monkeypatched
    ``_http`` (8)
  * ``control --disable`` snapshot-subtract path (1)
  * ``cmd_control`` empty-knobs CLIError (1)

Verification:

- Consultants env: 3187 pass / 30 skip — up from 3137 (+50:
  24 routes + 26 CLI).
- Main env: 3215 pass / 83 skip — up from 3166 (+49: 23 routes
  (resume class langgraph-gated) + 26 CLI).
- Zero regressions on M0-M8.

### Added — `/consultants` v2 long-term-memory BaseStore adapter (M8)

Closes M8 — a LangGraph `BaseStore` adapter that gives the council
a shared namespaced read/write surface for findings. Two concrete
wins motivate the milestone:

1. **Within-session cross-lane recall.** A researcher lane in round 2
   can search what other lanes (or earlier rounds) already discovered
   for the same plan-item, instead of duplicating tool calls and
   re-discovering the same evidence. Cheap protection against
   redundant work at x-tier diversity fanout.
2. **Cross-session follow-up recall.** When a follow-up's parent
   transcript.db is cold (v1.0 parent, or recorder-disabled run),
   `recall_for_follow_up(store, parent_sid, question)` falls back to
   semantic search over the parent's research namespace — replacing
   today's chronological pre-seed with relevance-ranked recall.

The store is **opt-in** and **effort-gated** so the v1 zero-cost
path is the default: low/medium tiers stay store-free, high / max /
x-tiers can be wired to it via a single `[store]` config block.

`consultants/engine/store.py` (NEW):

- ``Namespaces`` factory with canonical tuples
  (``(sid, "research")``, ``(sid, "tool_results")``,
  ``("project", project_id)``, ``("user", user_id)``). Hand-rolled
  tuples are an easy way to silently split the store; always go
  through these.
- ``ProviderBackedStore(BaseStore)`` — concrete adapter wrapping any
  `claude_hooks` provider that exposes ``store(content, metadata)``
  + ``recall_hybrid(query, k)``. Both ``PgvectorProvider`` and
  ``SqliteVecProvider`` (post-v1.7 parity) already implement this
  duck-typed surface — no provider changes needed.
  * ``put`` writes to both an in-process ``(ns, key) → Item`` index
    AND the provider (for vector recall).
  * ``get`` / ``delete`` / ``list_namespaces`` hit the in-process
    index (O(1), session-scoped; durability is the provider's job).
  * ``search`` with a query goes to ``provider.recall_hybrid`` and
    post-filters by namespace prefix + `_consultants_store` marker
    (defensive against the same provider being shared with the
    general claude-hooks recall pipeline).
  * ``search`` without a query falls back to in-process scan ranked
    by ``updated_at`` descending — useful for "list everything
    under this namespace" patterns without polluting the vector
    index.
- ``make_consultants_store(cfg, *, sid, project_id, user_id,
  effort, provider_loader)`` factory — returns ``None`` on every
  short-circuit (langgraph missing, ``cfg.store.enabled = False``,
  ``effort`` below the gate, unknown backend, provider load
  failure). Recall + record helpers tolerate ``None`` so callers
  use the same code path either way.
- ``recall_research(store, sid, query, limit)`` /
  ``record_research(store, sid, lane_idx, plan_item, finding)`` /
  ``recall_for_follow_up(store, parent_sid, question, limit)`` —
  convenience helpers. Every one is a no-op when the store is
  ``None`` or the input is empty.
- ``format_findings_block(items)`` — renders a SearchItem list into
  the markdown block the researcher prompt embeds. Truncates each
  finding at 800 chars by default and caps at 8 items; dedups on
  identical text so a noisy hybrid index doesn't repeat itself.

`consultants/config.py`:

- New ``StoreConfig`` dataclass on ``ConsultantsConfig.store``:
  * ``enabled`` (default ``False``) — zero-cost path is the default.
  * ``backend`` (default ``"memory"``) — ``memory`` (InMemoryStore) |
    ``pgvector`` (PgvectorProvider) | ``sqlite_vec``
    (SqliteVecProvider).
  * ``enable_at_efforts`` (default
    ``("high", "max", "xmedium", "xhigh", "xmax", "xauto")``) —
    effort tiers at which the store is wired in. Lower tiers stay
    free.
  * ``recall_limit`` (default ``5``).
  * ``pgvector_dsn`` / ``pgvector_table`` / ``sqlite_vec_path`` —
    backend-specific endpoints.
- TOML parser reads ``[store]`` block; TOML emitter writes it back
  with hint comments for the optional DSN / path fields.

`consultants/engine/graph.py`:

- ``GraphDeps.store: Optional[Any]`` + ``GraphDeps.sid:
  Optional[str]`` — wired through both ``_wrap_researcher``
  (so the node sees them) and ``.compile(store=...)`` (so LangGraph
  registers the store for any future code path that prefers the
  runtime ``get_store()`` helper). Follow-up builder shares the
  same plumbing.

`consultants/engine/council.py`:

- ``researcher_node`` accepts new ``store=None, sid=None`` kwargs.
  * **Recall** — before message build (lane-focused + full-plan
    paths both), call ``recall_research`` with the focused plan
    item (or the full plan when there's no fanout). Skip findings
    from this same lane (dedup against the lane's own
    ``prior_rounds``). Render via ``format_findings_block`` into a
    new ``peer_findings`` kwarg on ``build_researcher_messages``.
  * **Record** — closure ``_record_finding_to_store`` writes the
    final report to ``(sid, "research")`` at all three "report
    produced" return sites (v1 inline-agent-loop success path, M6
    REPORT-mode success, M6 PLAN-mode empty-plan fallback). PLAN-
    mode plan-only returns do NOT record (no report produced).
- ``build_researcher_messages`` grows ``peer_findings:
  Optional[str]`` kwarg — surfaced after ``prior_rounds`` and
  before ``additional_context``. Empty/None → no block (zero-cost).

`consultants/server/runner.py`:

- Both the primary runner and the follow-up runner build the store
  via ``make_consultants_store(cfg, sid=state.sid, effort=cfg.effort)``
  and thread it through ``GraphDeps``. Factory failures (import
  errors, provider init errors) log + fall back to ``None`` so a
  broken store config never breaks a consultation.

Tests:

- ``tests/test_consultants_v2_store.py`` (42 tests):
  * Namespaces canonical tuples (5)
  * ``recall_research`` / ``record_research`` no-op on ``None``
    store (5)
  * ``format_findings_block`` empty / dedup / truncation /
    metadata rendering / max_items (6)
  * Factory short-circuits — no cfg.store / disabled / below-gate /
    InMemoryStore at enabled effort / unknown backend / provider
    loader injection (7, langgraph-gated paths skip on main env)
  * ``ProviderBackedStore`` full op surface — put/get/delete,
    overwrite preserves created_at, search namespace-prefix filter,
    search ignores rows without marker (provider-shared safety),
    no-query falls back to index scan, recall failure returns empty,
    dedup, list_namespaces with max_depth, recall_research +
    record_research integration, deterministic key, provider.store
    failure doesn't break put (19)
- ``tests/test_consultants_v2_store_e2e.py`` (3 tests, consultants
  env only): pre-seeded peer finding lands in the researcher's
  prompt; researcher's report lands in the store after invoke;
  no store → no peer-findings block (zero-cost path verified e2e).

Verification:

- Main env: 3166 pass / 82 skip — up from 3147 (+19 store unit
  tests now run on main env, langgraph-gated tests skip cleanly).
- Consultants env: 404 v2 tests pass — up from 384 (+45 added for
  M8: 42 unit + 3 e2e), zero regressions.

### Added — `/consultants` v2 xauto adaptive-effort escalation (M7)

Closes M7 — a new ``xauto`` effort tier that starts at the
``xmedium`` topology + caps and grows toward ``xhigh`` / ``xmax``
mid-flight when the council needs more compute. The consultation
discovers it's harder than expected and dials itself up; the user
never has to pre-commit to a single tier.

`consultants/config.py`:

- ``EFFORT_BUDGETS["xauto"] = 25`` — worst-case escalation needs
  follow-up budget compatible with the final tier reached.
- ``base_effort("xauto") → "medium"`` — starting caps the
  escalator grows from.
- ``extras_active("xauto") → True`` — xauto IS an x-tier by
  definition; researcher.extra_models fanout is active from the
  first round.

`consultants/engine/escalation.py` (NEW):

- ``EscalationDecision(from_tier, to_tier, signal, reason,
  runtime_control_delta)`` frozen dataclass — the escalator's
  proposed transition. ``runtime_control_delta`` is the diff
  between the from-tier's and to-tier's TIER_TOPOLOGIES entries
  (only fields that actually changed) plus ``xauto_tier`` so
  the next ``current_tier(state)`` call returns the new stage.
- ``TIER_TOPOLOGIES[XautoTier] → TierTopology(max_rounds,
  max_reroutes, confidence_target, multi_critic)`` static table.
  Confidence targets tighten monotonically (xmedium 0.60 →
  xhigh 0.70 → xmax 0.75); max_rounds and max_reroutes monotonic
  non-decreasing; multi_critic only at xmax (matches Phase 10).
- ``next_escalation(state, *, min_round_for_escalation=1)`` —
  pure decision function. Pre-conditions (any failure returns
  None silently): xauto run, current tier has forward transition,
  at least one round completed, not under critical time
  pressure (≥ 70% of soft budget). Signal priority,
  most-specific first: ``gap_named`` → ``critic_dissent`` →
  ``low_confidence`` → ``time_pressure``. The first three
  SUPPRESS under time pressure; the fourth is the carve-out
  that ONLY fires when no critic has run yet and we'd otherwise
  miss the deadline without critic review.
- ``apply_escalation(state, decision) → state_delta`` returns
  the ``{"runtime_control": {...}}`` shape with
  ``xauto_escalations`` incremented for post-mortem accounting.
- ``runtime_mutation_event_data(decision)`` shapes the payload
  for the streaming ``RuntimeMutation`` event the SSE consumer
  + recorder see.
- ``_is_time_pressure(state, *, threshold=0.70)`` anchors on
  ``runtime_control.started_ts`` + ``soft_target_ts``; returns
  False conservatively when either is absent. Threshold is
  configurable; default matches the plan §M7 spec.
- ``current_tier(state)`` reads ``runtime_control.xauto_tier``,
  defaults to xmedium. Unknown values fall back to xmedium with
  a warning log.

`consultants/engine/graph.py`:

- ``CouncilState`` grows a ``runtime_control: Annotated[dict,
  merge_runtime_control]`` channel so partial updates from any
  node (the escalator's delta, the M5 /control HTTP route's
  update_state call) deep-merge into existing fields.
  ``_wire_v2_reducers()`` patches the string forward-ref at
  import time, same pattern as M6's tool_plan / tool_results.
  Without this, the escalator's delta was silently dropped by
  the same TypedDict-channel-declaration bug M6 hit.
- ``_wrap_xauto_escalator(deps)`` builds the pass-through node:
  inspect state via ``next_escalation``, emit
  ``RuntimeMutation`` event (defensive — no-op outside runnable
  context), record the event to the recorder when present, and
  return the state delta. Returns ``{}`` (pass-through) when no
  escalation is warranted — safe to wire unconditionally.
- ``build_council_graph`` inserts the escalator between
  critic/meta_critic and ``route_after_critic`` when both
  critic and researcher are enabled. The unconditional edge
  critic → escalator barriers the (possibly Send-multiplexed)
  critic before the escalator fires; the escalator's
  state-delta merges into runtime_control before the
  conditional reads it. Non-xauto runs see ``next_escalation``
  return ``None`` and the node short-circuits to ``{}`` — no
  cost beyond one dict read.

**Tests:** 40 new across two files, all green on both envs.

- ``tests/test_consultants_v2_escalation.py`` (38) — config
  surface (``xauto`` in EFFORT_BUDGETS,
  ``base_effort("xauto") == "medium"``,
  ``extras_active("xauto")``); ``is_xauto_run`` + tier
  fallback; allowed-forward-only transitions; per-signal
  triggers (gap_named, critic_dissent, low_confidence,
  time_pressure carve-out); pre-condition guards (non-xauto
  run, before first round, ceiling, time-pressure
  suppression); topology delta only emits changed fields;
  signal priority order (gap_named > critic_dissent >
  low_confidence); apply_escalation increments
  xauto_escalations; RuntimeMutation event payload shape;
  TIER_TOPOLOGIES static sanity (monotonic growth, tightening
  confidence target, multi_critic only at xmax,
  ceiling==xmax).
- ``tests/test_consultants_v2_xauto_e2e.py`` (2,
  consultants env only) — full PLAN → critic-dissent →
  escalator-mutates-runtime_control → researcher round 2
  → critic-ready → synthesizer cycle. Verifies
  runtime_control.xauto_tier advances, max_rounds /
  max_reroutes / confidence_target match xhigh's topology,
  xauto_escalations == 1, researcher fired twice. Regression
  guard: a plain medium run with the same shape produces no
  escalation (xauto_escalations stays absent / 0).

Test counts: 3109 → 3147 on the main env (+38);
consultants-env at 384 (+40). Zero regressions on either env.

### Added — `/consultants` v2 tool_executor wiring (M6a + M6b)

Closes M6 — the engine now supports an opt-in dedicated
``tool_executor`` role that offloads the researcher's tool-call
subloop into parallel ``Send`` fanout lanes. Addresses the user's
trace observation that frontier models (kimi-k2.6, glm-5.1,
deepseek-v4) sometimes mishandle multi-tool sequences; the
specialist (default gemma4:31b-cloud) runs the mechanics while
the researcher's frontier model owns the semantic planning.

**M6a — Infrastructure (state + node):**

`consultants/config.py`:

- ``ROLES`` grows from 4 to 5; ``tool_executor`` joins as opt-in
  with disabled-by-default. Order matters because
  ``cc.enabled_roles()`` returns roles in ROLES order and the
  runner builds graph topology accordingly: planner → researcher
  → tool_executor (optional) → critic (optional) → synthesizer.
- ``DEFAULT_MODEL_BY_ROLE`` / ``DEFAULT_ENABLED_BY_ROLE`` lookup
  tables drive a ``_default_role_config(role)`` factory. Every
  role except tool_executor keeps the global ``DEFAULT_MODEL`` and
  ships enabled — v1 byte-parity. tool_executor uniquely defaults
  to ``gemma4:31b-cloud`` (M11c will decide whether to flip the
  default) and ships disabled.
- ``DEFAULT_THINK_BY_ROLE["tool_executor"] = False`` — gemma4 is
  non-reasoning; disabling think on the tool-call ChatClient
  keeps the response shape clean for the agent_loop runner.

`consultants/engine/state_v2.py`:

- ``ToolPlanItem(intent, why, lane_idx, parent_round,
  suggested_tools)`` — one entry in the researcher's semantic
  tool_plan.
- ``ToolResult(intent, content, transcript_summary, tools_called,
  lane_idx, parent_round, duration_ms, error)`` — one
  tool_executor lane's output; tombstone shape is intent + error.
- ``CouncilStateV2`` grows three channels: ``tool_plan``
  (additive list[ToolPlanItem]), ``tool_results`` (additive
  list[ToolResult] across Send lanes), and the per-lane
  Send-injected ``tool_plan_item``. ``awaiting_tool_results``
  non-additive flag flips True after PLAN mode, False after
  REPORT mode — the graph's route_after_researcher reads it.
- ``tool_results_for_round(state, round)`` helper filters by
  ``parent_round`` so the researcher's round-2 prompt only sees
  round-1 evidence (no cross-round leakage on critic re-routes).

`consultants/engine/tool_executor.py` (NEW):

- ``TOOL_EXECUTOR_SYSTEM`` — short specialist instructions
  (execute one intent, smallest tool sequence, cite path:line,
  do NOT write the report).
- ``build_tool_executor_messages(item, grounding_msgs, *,
  question)`` composes the lane's conversation seed: grounding
  first (matches researcher pattern), then system prompt, then
  user message carrying INTENT + optional WHY + SUGGESTED TOOLS
  + PARENT QUESTION blocks.
- ``tool_executor_node(state, *, chat_client, tool_executor,
  tool_specs, grounding_msgs, model, cwd, think=False,
  loop_runner=None, recorder=None)`` — runs one lane via
  ``agent_loop.runner.run_loop``. Returns ``{"tool_results":
  [ToolResult(...)]}`` for additive merge. Tombstones cleanly on
  missing/empty intent or loop_runner exception; preserves
  parent_round + lane_idx so the researcher's round filter
  surfaces the gap. Recorder rows tagged ``role="tool_executor"``
  — the audit-trail separation that motivated the dedicated
  role.

**M6b — Researcher prompt-mode + graph wiring:**

`consultants/engine/tool_executor.py`:

- ``parse_tool_plan(text, *, parent_round)`` extracts the
  researcher's PLAN-mode JSON output. Tolerant across three
  fence styles: fenced with ``json``, bare fence, bare JSON
  object/array. Items missing intent or with empty intent are
  skipped silently; malformed JSON returns ``[]``. Lane indexes
  assigned in parse order.
- ``RESEARCHER_PLAN_MODE_BLOCK`` — prompt fragment appended to
  the researcher's user message in PLAN mode. Declares
  DO-NOT-CALL-TOOLS, names the output shape, bounds to 1-6
  items.
- ``build_tool_plan_user_appendix(prior_results)`` — renders the
  PRIOR TOOL RESULTS block for REPORT-mode entry. Renders each
  ToolResult as intent + tools_called summary + truncated
  content (2K char cap per result). Tombstones in compact
  ``FAILED: error`` form.

`consultants/engine/council.py`:

- ``researcher_node`` grows a ``tool_executor_enabled: bool``
  kwarg. When True, two-phase alternation:
  - PLAN mode (first entry of cycle): append PLAN_MODE_BLOCK to
    user msg, single _single_shot call (no agent_loop), parse
    tool_plan, return ``{"tool_plan": items,
    "awaiting_tool_results": True}``.
  - REPORT mode (re-entry after lanes complete): append
    PRIOR TOOL RESULTS to user msg from
    ``tool_results_for_round(state, this_round)``, single chat
    call, return v1-shape ``{"research": [text],
    "research_rounds_used": 1, "awaiting_tool_results":
    False}``.
- ``tool_executor_enabled=False`` (default) preserves v1
  bit-for-bit: full inline agent_loop subloop.
- Empty/unparseable plan in PLAN mode degrades gracefully to
  the v1 inline-report shape so the council doesn't loop forever
  on a model that won't emit JSON.
- ``t0`` anchor hoisted to the top of the function so both M6
  branches and the legacy inline-loop branch share one timing
  origin.

`consultants/engine/graph.py`:

- ``CouncilState`` TypedDict extended with the v2 channels —
  ``additional_context`` (M5), ``tool_plan`` / ``tool_results``
  / ``tool_plan_item`` / ``awaiting_tool_results`` (M6). The
  string forward-refs in the ``Annotated`` metadata are resolved
  at import time by ``_wire_v2_reducers()`` so LangGraph's
  ``get_type_hints``-based schema introspection sees the real
  reducer callables. Without this, the graph silently dropped
  M6 channels from node return deltas — the bug surfaced as
  "researcher M6 PLAN-mode returned empty/unparseable tool_plan"
  in the e2e test even though the parser worked standalone.
- ``_wrap_tool_executor(deps)`` wraps the node for LangGraph.
- ``_wrap_researcher(deps)`` reads ``"tool_executor" in
  deps.enabled_roles`` at compile time and passes the flag to
  the researcher node — one boolean read per invocation.
- ``build_council_graph`` adds the ``tool_executor`` node when
  the role is enabled, skips the researcher → next-role
  unconditional edge in that case, and installs a conditional
  ``route_after_researcher`` that fans out one Send per pending
  ``tool_plan`` item (filtered to the current round + lanes not
  already completed) OR falls through to the natural next role
  on REPORT-mode completion. An unconditional edge
  ``tool_executor → researcher`` provides the REPORT loop.
  Send-multiplexed barrier semantics from LangGraph mean all
  tool_executor lanes complete before researcher re-enters.

**Scope note for M6b graph wiring:** the initial wiring is for
the non-fanout researcher path (single researcher, no x-tier
Phase 9 multi-model). Proper composition with x-tier fanout is
the design goal — Phase 9's N×M multi-model researcher diversity
is a core feature of the engine and must be preserved end-to-end
when tool_executor is enabled. The M11c tool-executor benchmark
will drive the architectural choice: per-lane subgraphs vs
lane-tagged tool_results + manual REPORT-mode dispatcher. If the
benchmark shows proper composition is unaffordable, an auto-gate
(disable tool_executor when ``extras_active(effort)``) is the
fallback — last-resort only, never the recommended path.
Disabled-by-default in M6 reflects deferred wiring, not an
intentional combination boundary.

**Tests:** 48 new across two files, all green on both envs.

- ``tests/test_consultants_v2_tool_executor.py`` (46) — M6a
  dataclass construction + frozen contract, round-filter
  helper, prompt-builder shape + optional-block omission,
  happy-path node returns populated ToolResult +
  transcript_summary + tools_called, recorder callbacks fire
  with role="tool_executor", tombstones for
  missing/empty/exception paths, ``_extract_final_content``
  tolerance, ``_summarize_tools`` ordering + duplicate
  collapse, additive channel reducer merge; M6b parser tests
  (empty / fenced+json / fenced bare / bare object / bare
  array / unparseable / malformed / missing-intent /
  suggested_tools optional + filter / multi-fence preference),
  PLAN-mode prompt anchors, REPORT-mode appendix renderer
  (empty / single / tombstone / truncate / multi-numbered).
- ``tests/test_consultants_v2_tool_executor_e2e.py`` (2,
  consultants env only) — end-to-end PLAN → fanout → REPORT
  cycle with a 2-item tool_plan that produces 2 lanes merging
  cleanly via the additive reducer; regression guard verifying
  the role-disabled path keeps the v1 inline tool subloop
  unchanged.
- ``tests/test_consultants_config.py`` (+2) — ROLES order
  assertion updated to include tool_executor in position 3;
  disabled-by-default + default-model assertions lock the M6
  contract into the pre-existing defaults test.

Test counts: 3091 → 3109 on the main env (+18); consultants-env
total at 344 (+18 vs M5 close). Zero regressions on either env.

### Added — `/consultants` v2 mid-flight injection + HITL interrupts (M5)

Closes M5 — the engine now accepts mid-flight context injects, the
HTTP control surface has a typed payload-builder layer for the M9
FastAPI routes to wrap, and the graph builder honors a static
`interrupt_before=["synthesizer"]` review when the user opts in.

`consultants/engine/interrupt_policy.py` (NEW):

- `InterruptDecision(kind, prompt, payload, urgent)` frozen
  dataclass. `kind` is one of `review` / `low_confidence` /
  `tool_permission` / `user_pause` and doubles as the SSE event
  discriminator + `InterruptState.kind` literal. `to_payload()`
  builds the dict the node hands to `langgraph.types.interrupt()`;
  `to_interrupt_state(posted_at)` materializes the matching
  `InterruptState` channel value the server's `GET /state` exposes.
- `should_interrupt_before_synthesis(state, *, cfg)` — static review
  fires when either `runtime_control.review_before_synthesis` OR
  `cfg.runtime.review_before_synthesis` is set, and re-fires
  suppression is keyed on `state.interrupt_state` being already
  populated (no double-pause after resume).
- `should_interrupt_on_low_confidence(state, *, threshold)` — opt-in
  dynamic interrupt. Pre-conditions are conservative: an empty
  confidence series never fires; the active threshold is the
  explicit arg if given, else `runtime_control.confidence_target`,
  else 0.5; off by default because the same signal normally drives
  xauto escalation.
- `should_interrupt_on_tool_permission(state, tool_name, *,
  args_preview)` — fires only when
  `runtime_control.tool_permissions[tool_name] == "ask"`. `"deny"`
  is handled by the caller's separate skip path; `"allow"` /
  missing → no interrupt.
- `should_interrupt_on_user_pause(state, *, role)` — cooperative
  pause flag check; honored by the next node entering after the
  HTTP `/interrupt` route flips `runtime_control.pause_requested`.
- `clear_interrupt(state)` — composes the state-delta that clears
  `interrupt_state` and the pause flag, used by nodes that consume
  a `Command(resume=...)`.

`consultants/server/control.py` (NEW):

- Pure-Python builders for every control-endpoint payload — no
  FastAPI / langgraph imports at module top so the layer
  unit-tests cleanly on the main `claude-hooks` env and the M9
  route plumbing doesn't have to re-test shapes.
- `build_inject_delta(*, role, text, source, ts)` — validates the
  role is one of `VALID_INJECT_ROLES` and text is non-empty
  (after strip), 50K-char ceiling. Returns
  `{"additional_context": [Doc(...)]}` ready for
  `graph.update_state(..., delta, as_node=...)`. The
  state-channel reducer (`append_doc`) hash-dedups so inject
  retries are idempotent.
- `build_runtime_control_delta(changes)` — per-key validation
  for every RuntimeControl field (`deadline_ts` float,
  `max_rounds` non-negative int, `confidence_target` in [0,1],
  `critic_strictness` in `lax/normal/strict`, `enabled_roles`
  list[str], `tool_permissions` dict[str,allow/deny/ask], boolean
  flags for review-before-synthesis + low-confidence interrupt).
  Unknown keys are rejected (rather than silently dropped) so the
  caller knows their request didn't take effect.
- `build_interrupt_delta(*, reason)` — flips
  `runtime_control.pause_requested` + records the reason.
- `build_resume_command(value, *, decision)` → `InterruptResume`
  value object the M9 layer hands to `Command(resume=...)`.
- `build_cancel_request(*, discard_partial, reason)` →
  `CancelRequest(state_delta, discard_partial)` carrying both
  the cooperative-cancel flag and the checkpoint-keep/-delete
  intent.
- `summarize_state_for_get(raw, *, sid)` — turns a LangGraph
  `StateSnapshot` (or any dict-shaped state) into the user-facing
  `GET /v1/consult/<sid>/state` body. Drops bulky channels
  (full research reports), surfaces the high-signal fields, and
  tolerantly converts either a live `InterruptState` dataclass
  or a re-loaded-from-checkpoint dict.
- `ControlInputError(ValueError)` — every validator raises this on
  bad shape; the M9 layer maps it to a 400 response.

`consultants/engine/council.py`:

- `_additional_context_block(docs)` (pure renderer) and
  `_additional_context_for(state, role)` (state→Doc lookup
  delegating to `state_v2.unconsumed_context_for` with defensive
  fallback) — single source for the prompt-side wiring.
- All four message builders (`build_planner_messages` /
  `build_researcher_messages` / `build_critic_messages` /
  `build_synthesizer_messages`) grow a keyword-only optional
  `additional_context=None` parameter. When non-empty, an
  `ADDITIONAL CONTEXT (injected after session start, in order
  received): 1. ...` block is appended to the user message; when
  empty/absent the rendered output is byte-identical to v1.
  For the synthesizer the block lands BEFORE the "Now write the
  final answer..." directive so the last-instruction primacy holds.
- The four node functions (`planner_node`, `researcher_node` —
  both lane-focused and full-plan paths, `critic_node`,
  `synthesizer_node`) now call `_additional_context_for(state,
  <role>)` and pass the filtered Doc list into their message
  builder. Roles see their own targeted docs + `"any"` docs;
  irrelevant docs (e.g. researcher-only when planning) are
  filtered out.

`consultants/engine/graph.py`:

- `build_council_graph(...)` grows an optional `interrupt_before:
  list[str] | None` kwarg. When set, the names are filtered
  against the actually-compiled node set (so passing
  `["synthesizer"]` when synthesizer is disabled doesn't crash)
  and forwarded to `sg.compile(interrupt_before=...)`. Cache /
  no-cache fallthrough is now collected via a `compile_kwargs`
  dict so the option-handling logic lives in one place.

`consultants/config.py`:

- New `RuntimeConfig(review_before_synthesis: bool,
  interrupt_on_low_confidence: bool)` dataclass; both default
  `False` (v1 parity). `ConsultantsConfig.runtime` holds an
  instance.
- TOML loader recognizes a `[runtime]` block with the two flags.
- TOML emitter round-trips the block with inline comments
  documenting each flag.

`consultants/server/runner.py`:

- The council runner reads `cfg.runtime.review_before_synthesis`
  and passes `interrupt_before=["synthesizer"]` to
  `build_council_graph` when set. `AttributeError` fallthrough
  preserves v1 behavior on older configs without the runtime
  block.

**Tests:** 80 new tests across four files, all green on both
envs:

- `tests/test_consultants_v2_interrupt_policy.py` (24 tests) —
  `InterruptDecision.to_payload`/`to_interrupt_state` shape,
  review-before-synthesis static + cfg flag + interrupt-active
  guard + payload contents, low-confidence opt-in + threshold
  resolution + empty-series guard + latest-score reading,
  tool-permission ask/deny/allow distinctions + args-preview
  truncation, user-pause flag + re-fire guard, `clear_interrupt`
  delta shape.
- `tests/test_consultants_v2_inject.py` (16 tests) — block-renderer
  shape (empty / single / multi / whitespace strip),
  message-builder v1 parity when channel absent, message-builder
  appended-block shape (synth: ordering vs final directive,
  planner: question first), node-level integration via stub
  chat_client (planner / synthesizer surface injected docs +
  role-filter `researcher-only` out of planner's prompt), reducer
  idempotency (hash-dedup on retry).
- `tests/test_consultants_v2_control_api.py` (37 tests) —
  per-builder validation: inject (role / text / oversize),
  runtime_control (every key + range + type + unknown-key reject +
  partial-merge shape), interrupt (default reason), resume (value
  + decision passthrough), cancel (discard_partial flag),
  state-summarizer (basic shape / InterruptState round-trip /
  None handling / LangGraph snapshot wrap / final_answer_ready
  truthiness).
- `tests/test_consultants_v2_hitl.py` (3 tests, consultants env
  only) — end-to-end pause→inject→resume with a real
  LangGraph compiled with `interrupt_before=["synthesizer"]`
  (verifies the synth's final answer reflects the injected doc),
  dynamic-interrupt with `Command(resume=...)` round-trip
  (`should_interrupt_on_low_confidence` decision → `interrupt()`
  → state.tasks[].interrupts inspection → resume value lands on
  the node's `interrupt()` return), `InterruptDecision`
  payload-shape integrity (the kind/prompt/payload round-trips
  through `interrupt()` and surfaces on `state.tasks[i]
  .interrupts[0].value` unchanged).

Test counts: 3024 → 3064 on the main env (+40 main-env-runnable
tests; the 3 HITL e2e tests skip-here / pass on the consultants
env, M5 totals 80 in absolute terms with the HITL trio counted
on the consultants env's 304-test sweep).

### Added — `/consultants` v2 SSE bridge + node instrumentation (M4b)

Closes M4 — real council nodes now emit typed events that surface
on `compiled.astream_events(version="v2")` as `on_custom_event`
records, and the SSE bridge demuxes them into a `text/event-stream`
response the M9 endpoint will hand to consumers.

`consultants/engine/events.py`:

- `emit(event)` switched from `langgraph.config.get_stream_writer`
  (which routes to `astream(stream_mode="custom")`) to
  `langchain_core.callbacks.manager.dispatch_custom_event` (which
  routes to `astream_events(version="v2")` as `on_custom_event`).
  Same defensive-no-op behavior outside a runnable context;
  sync-callable so it works from sync nodes without an event-loop
  hop.

`consultants/engine/council.py` — node instrumentation:

- Every role node (`planner_node`, `researcher_node`,
  `critic_node`, `meta_critic_node`, `synthesizer_node`) now
  emits `NodeStarted` at entry and `NodeFinished` at exit. Both
  happy-path and tombstone returns emit `NodeFinished` with
  `ok=False` + `error="<Type>: <msg>"` when the role failed.
- Two helpers `_emit_started` / `_emit_finished` near the top of
  the module are catch-all wrappers that never raise — defensive
  emit() means plain-Python tests (test_consultants_council.py's
  109 tests stay green without modification) see them as no-ops,
  while live consumers get every transition.

`consultants/server/events_sse.py` (NEW):

- `format_sse_event(*, event_id, event_type, data, retry_ms)` —
  pure formatter that builds the wire-format bytes per the SSE
  spec (id/event/retry/data lines with trailing blank).
- `format_sse_heartbeat()` — SSE comment-line heartbeat that
  keeps reverse-proxies from closing idle connections.
- `classify_astream_event(raw, *, sid)` — pure demultiplexer
  over one `astream_events` v2 record. Maps `on_custom_event` to
  the event's `kind`, `on_chat_model_stream` to a `token` event
  with `{"role", "delta"}`, `on_chain_start`/`on_chain_end` to
  `lifecycle` events with `{"phase", "name"}`, drops everything
  else.
- `sse_from_astream_events(astream_iter, *, sid, heartbeat_s,
  start_event_id, initial_retry_ms)` — async iterator. Races
  the upstream `__anext__()` task against a heartbeat deadline
  using `asyncio.wait` (not `wait_for`, which would cancel the
  inner async generator). Yields SSE bytes ready for FastAPI's
  `StreamingResponse`. Cleans up the pending task on consumer
  disconnect via `try/finally`.
- `sse_replay_from_rows(rows, *, start_event_id)` — Last-Event-ID
  resume path. Replays recorded `runtime_events` rows before
  attaching the live stream. Caller passes
  `highest_event_id(rows)` as `start_event_id` to
  `sse_from_astream_events` so numbering stays monotonic across
  replay + live.

**Tests:** 32 new tests across two files, all green on both
envs:

- `tests/test_consultants_v2_events_sse.py` (28 tests) — wire
  formatters (basic shape, retry hint, JSON one-line,
  empty-event-type rejection, non-ASCII, default-str fallback,
  comment-line heartbeat); demux (custom event uses kind as
  type, sid injection, sid preservation, chat-model-stream →
  token, empty chunk dropped, chain start/end → lifecycle,
  other events dropped, non-dict dropped); live iterator
  (events in order, retry hint on first event only,
  uninteresting events dropped, start_event_id offset,
  heartbeat fires when upstream silent); replay iterator
  (rows in order, since_event_id skip, empty rows yields
  nothing); highest_event_id (max, empty=0, missing field
  treated as 0).
- `tests/test_consultants_v2_events_integration.py` (2 tests,
  consultants env only) — end-to-end: a real LangGraph node
  calling `emit(NodeStarted(...))` surfaces on the bridge as
  `event: node_started` in SSE wire format, with sid injection
  + all dataclass fields preserved.

Full-suite count: 2984 passing on the main `claude-hooks` env
(+28 from M3b close). Zero regressions.

### Added — `/consultants` v2 typed event taxonomy + recorder runtime_events table (M4a)

The streaming-events plumbing layer for v2. M4a lands the
event dataclasses + recorder persistence; M4b will wire them up
to node entry/exit + the SSE bridge.

`consultants/engine/events.py`:

- Nine frozen dataclasses for the council's streaming-event
  taxonomy: `NodeStarted`, `NodeFinished`, `ToolCall`,
  `PartialSynthesis`, `ConfidenceUpdate`, `DeadlineWarning`,
  `RuntimeMutation`, `Interrupt`, `Resumed`. Each carries a
  per-class `kind` discriminator (used as the SSE `event:` name
  + the recorder row type), a `ts` default of `time.time()`, and
  an optional `sid` for cross-thread aggregation. Common base
  class `CouncilEvent` exposes `to_dict()` → JSON-serializable
  shallow dict (round-trips through `json.dumps`).
- `emit(event)` — defensive bridge to LangGraph's
  `get_stream_writer()`. Catches `ImportError` (when langgraph
  isn't installed in the test env), `RuntimeError` (when called
  outside a runnable context — tests running nodes as plain
  Python), and `Exception` from the writer (downstream consumer
  crash). Returns `True` on success, `False` otherwise — callers
  use this as a should-still-record signal so the recorder path
  fires regardless of live stream delivery.

`consultants/engine/recorder.py` — `MessageRecorder` extensions:

- Schema bump v1 → v2: adds `runtime_events` table mirroring the
  CouncilEvent dataclasses (`event_id`, `ts`, `kind`, `role`,
  `round`, `lane_idx`, `payload` JSON blob). Separate from the
  existing `events` table so the LLM/tool/node-boundary rows
  stay untouched and old post-mortem tooling keeps working. Two
  indices (`kind`, `ts`) for fast filtered scans.
- `record_event(*, kind, role, round, lane_idx, payload)` — the
  M3 stall layer's `on_event` sink and the M4 SSE bridge both
  call this. Belt-and-braces: backfills `kind` into the payload
  dict so a consumer reading just the JSON blob doesn't need to
  cross-reference the indexed `kind` column. No-op when the
  recorder is closed (mirrors the rest of the recorder's API).
- `list_runtime_events(*, since_event_id=0, limit=1000)` — the
  SSE bridge's `Last-Event-ID` resume path. Returns parsed-payload
  dicts in insertion order; corrupt JSON gets a sentinel
  `__parse_error__` rather than crashing the lister.

**Tests:** 24 new tests, all green on both `claude-hooks` and
`claude-hooks-consultants` envs:

- `tests/test_consultants_v2_events.py` — event dataclass shape
  (every type's `kind` default + field accessors), frozen-ness,
  JSON round-trip, ts override, kind override; emit() defensive
  paths (outside runnable context → False, writer succeeds →
  True, writer raises → False); record_event persistence
  + kind-required + payload-optional + no-op when closed;
  list_runtime_events paging + parse-error tolerance + dataclass
  round-trip; stall-layer integration (a `chat_with_stall_protection`
  call with on_event=record_event lands `stall.attempt.ok` in
  `runtime_events`).

Full-suite count: 2956 passing on the main env, +24 from M3
close. Zero regressions.

### Added — `/consultants` v2 streaming chat + researcher stall wire-up (M3b)

Closes M3 — real LLM calls now route through the stall watchdog
when `runtime_control` is on state. Three concrete pieces:

`claude_hooks/get_advice/chat_client.py` — `ChatClient.chat_streamed`:

- Streaming counterpart to `chat()`. POSTs `/api/chat` with
  `stream=true`, reads NDJSON line by line, calls `on_token(delta)`
  per content chunk, returns the same OpenAI-shape dict `chat()`
  would have returned (`{"choices": [...], "usage": {...}}`).
- `cancel_check` callable polled between lines for cooperative
  abort; raises `CancelledByOrchestrator` when the stall watchdog
  asks the call to stop.
- Same retry policy as `chat()`: transient 5xx / retryable
  4xx-bodies / `URLError`s restart the stream from scratch with
  exponential backoff. Think-rejection 400s still trigger the
  graceful-degrade path.
- Tool-call deltas accumulate the same way `_from_ollama` already
  normalizes them, so a model emitting tool calls mid-stream OR on
  the final `done=true` record both produce identical agent-loop
  dict shapes.

`consultants/engine/stall_chat.py` — adapter factories:

- `make_stall_protected_chat_fn(chat_streamed, *, stall_threshold_s,
  hard_cap_s, retries, ...) -> chat_fn` — wraps a `chat_streamed`
  method in a `StallMonitor` so each invocation gets a fresh
  `StallController`, watchdog, and retry budget. Returns a sync
  `chat(payload) -> dict` callable the agent loop runner consumes
  unchanged.
- `make_hard_cap_only_chat_fn(chat, *, hard_cap_s) -> chat_fn` —
  fallback for clients without streaming. Worker thread + absolute
  hard-cap timer. No stall detection (no token visibility) but
  the wall-clock ceiling still fires. Good enough for local
  llamafile where stalls are rare.
- `stall_protected_chat_fn_for(chat_client, ...)` — dispatcher.
  Picks the streaming protector when `chat_streamed` is available,
  hard-cap-only otherwise. Caller passes live thresholds derived
  from `RuntimeControl` so mid-flight mutation rebuilds the
  protector at the next round.

`consultants/engine/council.py` — `researcher_node` wire-up:

- When `state["runtime_control"]` is set AND the chat_client
  exposes `chat_streamed`, the bound chat callable handed to the
  loop runner is the protected wrapper. The cloud
  gemini-3-flash stall pathology (csl-2026-05-15-1439-4ff0: two
  lanes that held 31min / 27min single-call wall time with no
  useful output) is now caught at `stall_threshold_s` and retried
  once before tombstoning.
- When `runtime_control` is absent (v1 legacy sessions), the loop
  runner receives `chat_client.chat` unwrapped — v1 behavior
  bit-for-bit. The 462 existing consultants tests in the
  consultants env stay green without modification.
- A stall-event sink wired to `recorder.record_event` lets every
  retry / hard-cap fire land in the events table for post-mortem
  visibility. Best-effort — a sink failure never breaks the
  researcher.

**Tests:** 28 new tests across three files; all 268 M3-adjacent
tests (M3a + M3b + the existing consultants suite) green:

- `tests/test_chat_streamed.py` (10 tests) — NDJSON happy path
  (assembly, per-token callbacks, tool-call passthrough,
  malformed-line tolerance, blank-line ignore), cooperative
  cancel raises `CancelledByOrchestrator`, retry policy
  (503-then-success, exhausted-after-max, 4xx-non-retryable
  fails-fast), shape-parity with `chat()` for the same canonical
  Ollama response. Uses `urllib.request.urlopen` patching with a
  `_FakeResponse` that yields lines from a list — fast (~40ms
  total) and no network dependency.
- `tests/test_consultants_v2_stall_chat.py` (13 tests) — both
  factories (happy path, stall+retry, retry-exhausted, hard-cap-
  fires, error-propagation, event-sink-feedback) and the
  dispatcher (streaming client → streaming protector,
  non-streaming → hard-cap-only, hard-cap actually fires on slow
  inner call).
- `tests/test_consultants_v2_researcher_stall_wire.py` (5 tests)
  — researcher_node integration parity (no runtime_control →
  plain `chat_fn`; legacy client → also plain even with
  runtime_control; streaming client + runtime_control → wrapped;
  wrapped chat_fn actually returns sensible response when
  invoked through the loop runner; `StallRetryExhausted` inside
  the loop runner tombstones the lane via the existing exception
  handler).

Full-suite count: 2932 passing on the main `claude-hooks` env;
the 47 skips are langgraph-dependent tests that pass on the
`claude-hooks-consultants` env. Zero regressions across both.

### Added — `/consultants` v2 stall-detection + soft time-target prompts (M3 building blocks)

Two new pure-Python modules under `consultants/engine/` deliver the
M3 watchdog + budget-injection primitives. Wire-up into the
researcher node + actual streaming on `chat_client.py` comes in the
M3b follow-up commit; this commit lands the orchestrator + tests so
the contract is locked before any real LLM call runs through it.

`consultants/engine/stall.py` — the stall detector + retry
orchestrator:

- `classify_stall(*, started_ts, last_token_ts, tokens_emitted,
  now_ts, stall_threshold_s, hard_cap_s) -> StallOutcome` —
  pure decision function. Returns `PROGRESSING` / `STARTUP_STALL`
  (no first token past threshold) / `MID_STREAM_STALL` (token
  cadence broke) / `HARD_CAP_EXCEEDED`. Hard cap wins over stall —
  once we're past the per-lane ceiling, there's no point retrying.
- `StallController` — cooperation primitive passed into the
  streaming chat callable. Thread-safe. The chat fn calls
  `mark_token()` per token and polls `is_cancelled()` between
  chunks. `progress()` snapshots the (started_ts, last_token_ts,
  tokens_emitted) tuple for the watchdog.
- `StallMonitor(cfg)` — orchestrator. Runs `chat_streamed_fn` in a
  worker thread + a watchdog that wakes every `check_interval_s`,
  classifies the call's progress, and either lets it run, retries
  on stall, or raises `HardCapExceeded` / `StallRetryExhausted`.
  Per-attempt audit trail on `.attempts`. Best-effort `on_event`
  sink lets the recorder log every stall/retry without coupling
  the orchestrator to a specific event bus.
- `CancelledByOrchestrator` — well-behaved chat fns raise this
  when they notice `controller.is_cancelled()` mid-stream so the
  orchestrator can distinguish cooperative aborts from real
  upstream errors. Uncoop chat fns that ignore the cancel flag
  don't block the orchestrator either — the worker is `daemon=True`
  and the watchdog moves on after `join_grace_s`.
- `chat_with_stall_protection(fn, payload, *, stall_threshold_s,
  hard_cap_s, retries=1, ...)` — thin sync wrapper the researcher
  node will call from inside `asyncio.to_thread`.

Default thresholds (300 s stall / 3600 s hard cap / 1 retry) match
the conservative-wide values pinned in `engine/control.py` from M2.
M11a benchmarks will produce per-model tighter values in a
follow-up.

`consultants/engine/timing.py` — pure prompt-injection helpers:

- `planner_soft_target_block(state)` — composes the "SOFT TIME
  TARGET: aim to finish in **N min**. The hard cap is M min;
  past that the consultation is cancelled..." block from
  `runtime_control.soft_target_ts` + `deadline_ts`. Returns `""`
  when no budget is on state (legacy v1 path).
- `researcher_remaining_block(state)` — live "REMAINING TIME
  BUDGET" computed at researcher entry. Each round sees the
  current value, so a 15-min budget at planner time renders as
  "7 min until the hard cap" by researcher round 2.
- `time_pressure_signal(state) -> "ample"|"normal"|"tight"
  |"critical"|"none"` — categorical pressure classifier the M7
  xauto escalator + critic strictness chooser will consume. Keeps
  consumers side-effect-free and trivially testable.

Both modules are pure Python; no langgraph dependency. The
orchestrator uses `threading` + `time.monotonic` only.

**Tests:** 48 new tests, all passing on both `claude-hooks` and
`claude-hooks-consultants` envs:

- `tests/test_consultants_v2_timing.py` (24 tests) — covers the
  duration formatter (sub-second clamp, hour split, rounding),
  planner / researcher block rendering, time-pressure signal
  classification including the no-budget / no-soft-target cases.
- `tests/test_consultants_v2_stall.py` (24 tests) — covers
  `classify_stall` in every state (progressing, startup stall,
  mid-stream stall, hard-cap-wins-over-stall, defensive
  none-handling), `StallController` thread-safety under
  concurrent marks, and `StallMonitor` orchestration with real
  threads + stub chat fns (happy path, retry-on-stall-then-ok,
  retry-also-stalls-raises-exhausted, startup-stall-detection,
  zero-retries=one-attempt, hard-cap-no-retry,
  upstream-error-propagation, event-sink-failure-doesn't-break,
  uncoop-chat-fn-doesn't-block).

Threaded stall tests use sub-second thresholds (0.3-1.0s
`stall_threshold_s`, 0.05s `check_interval_s`) so the full
24-test suite runs in ~4 seconds.

### Added — `/consultants` v2 RuntimeControl defaults + node reads (M2)

`consultants/engine/control.py` is the single source of truth for
runtime-knob defaults and the node-side accessor helpers:

- `time_target_for(effort, n_fanout_extras) -> (soft_s, hard_s)` —
  the timing formula as a function over per-effort `(base_s,
  per_extra_s)` tuples. Hard multiplier is 3× for base/x tiers, 4×
  for `xauto` (the escalator may grow topology mid-session).
- `runtime_control_defaults(cfg, effort, n_fanout_extras)` — boot-time
  `RuntimeControl` from the effort tier. Used by the runner at
  session start; the M9 HTTP `/control` endpoint mutates it via
  `graph.update_state`.
- Accessor helpers: `runtime_get(state, key, default)`,
  `runtime_max_rounds(state, fallback=...)`,
  `runtime_max_reroutes(state, fallback=...)`,
  `runtime_deadline_passed(state)`,
  `runtime_enabled_roles(state, fallback=...)`,
  `runtime_critic_strictness(state, fallback="normal")`. Node code
  uses these to read live values with the v1 effort-cap fallback.

`consultants/engine/council.route_after_critic` is the first v1
function to consume RuntimeControl: it now reads `max_rounds` and
`max_reroutes` from `state["runtime_control"]` when present (else
the v1 `caps_for(effort)`), and short-circuits to synthesizer when
`deadline_ts` has passed. The conditional edge is the most-mutated
control point — making it RuntimeControl-aware means a mid-flight
`POST /v1/consult/<sid>/control` body `{"runtime_control":
{"max_rounds": 5}}` *immediately* tightens the loop without waiting
for the next session.

Timing formula data (grounded in the 2026-05-16 historical analysis
across 35 csl-* sessions; see
`/root/.claude/plans/recursive-petting-planet.md` § "time-target
formula"):

  effort   base_s  per_extra_s  hard_mult  per_lane_hard_s
  low          60            0       3.0      3600
  medium      180            0       3.0      3600
  high        480            0       3.0      3600
  max         900            0       3.0      3600
  xmedium     240           90       3.0      3600
  xhigh       600          180       3.0      3600
  xmax       1080          300       3.0      3600
  xauto       720          180       4.0      3600

Async migration deferred to M3 — streaming chat (and the stall
detector that wraps it) is where async actually pays off.
RuntimeControl reads work the same in sync code as async, so the
M2 split is clean.

**Tests:** 31 new tests across timing formula + defaults +
accessor helpers + the route_after_critic v1/v2 paths. 462
consultants tests + 12 smoke pass on the consultants env. The full
suite picks up 31 tests on the main `claude-hooks` env too — the
control module is pure Python and runs without langgraph.

### Added — `/consultants` v2 state schema + checkpointer factory (M1)

`consultants/engine/state_v2.py` defines `CouncilStateV2`, the
forward-compatible TypedDict schema that the v2 overhaul will swap
the v1 graph onto incrementally. Every v1 channel keeps its name +
reducer semantics so v1 nodes continue reading the same shape during
the milestone-by-milestone migration. New v2 channels:

- `runtime_control: Annotated[RuntimeControl, merge_runtime_control]`
  — per-session mutable knobs (`deadline_ts`, `max_rounds`,
  `max_reroutes`, `enabled_roles`, `confidence_target`,
  `critic_strictness`, `stall_threshold_s`, `tool_permissions`,
  `xauto_tier`). The merge reducer means partial
  `graph.update_state` mutations preserve unmentioned keys instead
  of clobbering them — the M9 control endpoint relies on this for
  clean partial-update UX.
- `additional_context: Annotated[list[Doc], append_doc]` — mid-flight
  inject payloads. Append-only with `Doc.content_hash` dedup so a
  retried inject is idempotent. Reducer drops `None` sides cleanly
  and preserves first-occurrence order across left + right merges
  (concurrent inject during fanout merges cleanly).
- `confidence: Annotated[list[float], latest_or_none]` — synthesizer
  + critic self-rating series. `latest_confidence(state)` helper for
  the xauto escalator (reads the last entry).
- `partial_synthesis: Optional[str]` — synthesizer work-in-progress
  for the HITL `interrupt_before=["synthesizer"]` preview path.
- `interrupt_state: Optional[InterruptState]` — what the dynamic
  `interrupt()` posted. Set by the node serializing the interrupt,
  cleared on `Command(resume=...)`. Lets `GET /v1/consult/<sid>/state`
  return a usable "council is waiting for X" payload without scraping
  the event stream.

Helpers: `latest_confidence(state)`, `unconsumed_context_for(state,
role)`, `time_remaining_s(state)`. All pure-Python — the module
imports cleanly without langgraph (the main `claude-hooks` test env
exercises the reducers via the 26 new state tests).

`consultants/engine/checkpointer.py` is the durable-execution factory:

- `make_checkpointer(cfg, cwd, sid)` returns a `CheckpointerHandle`
  the runner holds for the session's lifetime.
- **SQLite (default)** — per-session file at
  `<cwd>/.claude-hooks/consultants/<sid>/checkpoints.db` with
  `PRAGMA journal_mode=WAL` + `busy_timeout=5000`. Zero new
  dependency (sqlite3 is stdlib).
- **Postgres (opt-in)** — `make_postgres_pool(cfg)` at app startup
  builds a shared `psycopg_pool.ConnectionPool`; every per-session
  `make_checkpointer(cfg, ..., postgres_pool=pool)` wraps a
  `PostgresSaver` around it. Requires the `[postgres]` extra
  (`pip install -e 'consultants[postgres]'`); a clean
  `RuntimeError` with a copy-pasteable install hint fires when the
  extra is missing.
- `CheckpointerHandle` wraps the saver with explicit `close()` so
  SQLite per-session conns clean up; Postgres handles' `close()` is
  a no-op (pool lifecycle is app-wide).

Config gains a `[checkpointer]` block in
`<cwd>/.claude-hooks/consultants.toml` and the user-global
`~/.claude/consultants-config.toml`. Default config produces
`backend = "sqlite"`; the TOML round-trip preserves a `postgres` +
`url` config. Invalid backend values fall back to `sqlite` with a
DEBUG log (matches the lenient-loader pattern the other config
sections use).

**Tests:** 26 state tests + 14 checkpointer tests + 4 config
integration tests = 44 new tests. Pre-existing 391 consultants
tests stay green. The state tests run in the main `claude-hooks`
env (no langgraph dep); the checkpointer tests gate on langgraph
import and skip cleanly when absent.

### Changed — `/consultants` env: LangGraph 1.2 pin (v2 overhaul M0)

The `claude-hooks-consultants` conda env now pins the LangGraph 1.x
stack (1.0 GA Oct 2025, 1.2.0 May 2026) instead of the previous
0.3.x line:

- `langgraph>=1.2,<2.0` (was `>=0.2,<0.4`)
- `langgraph-checkpoint>=4.1,<5.0` (new explicit pin)
- `langgraph-checkpoint-sqlite>=3.1,<4.0` (was `>=2.0,<3.0`)
- `langgraph-prebuilt>=1.1,<2.0` (new explicit pin)
- `langchain-core>=1.4,<2.0` (transitive bump from 0.3.x)
- `langchain-ollama>=1.0,<2.0` (was `>=0.2,<0.4`)
- new optional `[postgres]` extra:
  `langgraph-checkpoint-postgres>=3.1,<4.0` + `psycopg[binary,pool]`
- dropped: `langchain` + `langchain-community` (declared but never
  imported by the engine)

All 380 existing consultants tests continue to pass on 1.2.0 — the
v1 graph code is forward-compatible. The bump unlocks the
1.2-specific features (`TimeoutPolicy`, `RunControl`, `astream_events`
v3, `DeltaChannel`) that the v2 council overhaul plan
[`/root/.claude/plans/recursive-petting-planet.md`] depends on.

New `tests/test_langgraph_smoke.py` pins the API contracts the v2
plan relies on: version checks, trivial graph + Send fanout
reducers, `Command(goto/update)` routing, interrupt + resume across
checkpointer backends (`InMemorySaver`, `SqliteSaver` in-memory,
`SqliteSaver` cross-process file resume, `PostgresSaver` import-only
when the `[postgres]` extra is installed), `update_state` /
`get_state` / `get_state_history`, `astream_events(version="v2")`
shape, and custom event emission via `get_stream_writer()`. Future
framework bumps that break any of these tests means the v2 plan
needs revisiting — failing loudly beats silently miscompiling.

### Added — multi-root tool sandbox for `/get-advice`, `/consultants`, and `caliber-grounding-proxy`

All three tool-using runners now align with Claude Code's own
session allow-list. The shared read-only tool layer at
`claude_hooks/caliber_proxy/tools.py` accepts paths whose realpath
sits under **any** allowed root, where "allowed" is the union of:

- the runner's primary `cwd`;
- `permissions.additionalDirectories` from `~/.claude/settings.json`
  (user-global);
- `permissions.additionalDirectories` from
  `<cwd>/.claude/settings.json` (project-shared);
- `permissions.additionalDirectories` from
  `<cwd>/.claude/settings.local.json` (project-local, gitignored);
- runner-specific opt-ins: `--add-dir <path>` on the advisor +
  consultants CLIs (repeatable); `CALIBER_GROUNDING_ADD_DIRS` env
  var (`os.pathsep`-separated) on the grounding proxy.

The advisor caught the bug first — a session whose primary cwd
sat under `backup_models/` couldn't read
`/srv/.../vllm-source/...`, and the skill's "no inline code blocks"
rule turned the limit into an unbreakable wall. The grounding proxy
had the identical sandbox; it just hadn't tripped yet.

* **New shared helper** `claude_hooks/allowed_roots.py` with
  `discover_allowed_roots(cwd, *, add_dirs=(), settings_files=None)`.
  Single read path so every runner sees the same allow-list.
  Missing files, malformed JSON, and wrong-shape values are logged
  at DEBUG and silently skipped — discovery never fails the runner.
* **Tool layer** gains `resolve_in_roots(raw, primary_cwd, extra_roots)`
  and `make_executor(extra_roots) -> ToolExecutor` (closure
  factory). The four path-aware tools (`list_files`, `read_file`,
  `glob`, `grep`) accept an `extra_roots` kwarg; `glob` still walks
  only the primary cwd (cross-root glob is out of scope). The
  legacy `resolve_in_cwd(raw, cwd)` is now a thin shim around
  `resolve_in_roots(raw, cwd, ())` so external callers keep working.
  Error message renamed from `path escapes cwd` to
  `path escapes allowed roots: ...` with the full allow-list
  rendered, so the model can self-correct on retry.
* **The three-arg `ToolExecutor` type alias is unchanged.** Empty
  `extra_roots` returns the bare `execute` function (closure-free
  fast path), so any caller that doesn't opt in stays byte-identical.
* **`/get-advice`**: `claude-advisor turn` gains `--add-dir <path>`
  (repeatable). Resolved roots logged at INFO once per turn.
* **`/consultants`**: `consult` and `follow-up` subcommands gain
  `--add-dir` (repeatable). `SessionState` persists `extra_roots`
  so follow-ups inherit the parent's reach and may extend it
  (parent's list first, then this turn's, dedup'd).
* **`caliber-grounding-proxy`**: per-request executor is built from
  the union of `CALIBER_GROUNDING_ADD_DIRS` (operator-trusted env
  var) and settings-file discovery. **Deliberately does NOT** honor
  body-supplied roots — same posture as `_cwd_for_request`: the
  body is constructed by the LLM and is prompt-injectable.

### Tests

44 new tests across `test_allowed_roots.py` (18),
`test_caliber_proxy_multi_root.py` (20),
`test_caliber_proxy_server_multi_root.py` (6),
`test_get_advice_multi_root.py` (7), and
`test_consultants_multi_root.py` (10). Full suite: 2794 passed,
25 skipped.

### Out of scope (deferred)

- **Cross-root `glob` / `grep`** that walks every allowed root.
  Would need a separate output budget; out of scope for this PR.
- **Upward `.claude/` discovery** from a sub-directory of the
  project. Matches Claude Code's own contract (it only reads
  settings from the cwd's `.claude/`). Users in sub-dirs pass
  `--add-dir <project-root>` or `cd` first.

### Notes

This entry is in `[Unreleased]` — no version bump, no tag, no
release notes finalisation. v1.8.0 will batch this with other
pending work before the cut.

## [1.7.0] — 2026-05-15

### Added — full pgvector parity for `sqlite_vec`

`sqlite_vec` (and the bundled `sqlite-vec-mcp` launcher) now expose
the same eight semantic operations `pgvector` does — `recall`,
`recall_hybrid`, `store` (idempotent), `count`, plus the knowledge
graph: `kg_create_entities`, `kg_add_observations`,
`kg_create_relations`, `kg_search_nodes`. External MCP clients
(Cursor, Codex, OpenWebUI, Claude Desktop) get a complete
local-file analogue to pgvector — zero infra, same surface.

* **Hybrid recall** (`SqliteVecProvider.recall_hybrid`) — two-pass
  Reciprocal Rank Fusion over vector cosine (sqlite-vec) and BM25
  (FTS5 with `unicode61 remove_diacritics 2` tokenizer). Same RRF
  formula, alpha/k/rrf_k defaults, score/distance/rank metadata
  shape as pgvector.
* **Idempotent store** — `INSERT … ON CONFLICT(content_hash)
  DO NOTHING RETURNING rowid`. Re-storing whitespace-normalised
  identical content is a silent no-op; same `content_hash`
  algorithm as pgvector for cross-store dedup.
* **Knowledge graph** — full set of `kg_*` methods returning the
  same shape (`{name, entity_type, metadata, observations[],
  _score, _match}`). Name-fuzzy via FTS5 trigram tokenizer
  (SQLite ≥3.34, with `LIKE %query%` fallback for older builds,
  probed once at migration); observation hybrid via the same RRF
  body as `recall_hybrid`; three-pass search (name → obs hybrid
  → obs-fill).
* **In-place schema migration**
  (`claude_hooks/providers/sqlite_vec_schema.py`) — one-shot lazy
  migration on first `_ensure_ready()` after upgrade. v0 (legacy
  v1.6.x) → v1 in a single transaction: ALTER TABLE for
  `content_hash`, backfill from existing rows
  (whitespace-normalised SHA-256), partial UNIQUE index, FTS5
  external-content tables + AFTER triggers, the KG cluster
  (entities + relations + observations + their vec/fts mirrors),
  and a `claude_hooks_schema` bookkeeping table. Idempotent —
  re-runs that find v1 are no-ops. **Non-destructive** — the
  original `<table>` + `<table>_vec` are never dropped, so
  downgrade to v1.6.x keeps recall + store working on the legacy
  surface.
* **`sqlite-vec-mcp` tool catalog: 3 → 8 tools.** Adds
  `sqlite-vec-find-hybrid`, `sqlite-vec-kg-search`,
  `sqlite-vec-kg-create`, `sqlite-vec-kg-observe`,
  `sqlite-vec-kg-relate`. Renders byte-identical output to
  `pgvector-mcp` via shared `claude_hooks/mcp_format.py`.

### Changed

* `Provider` ABC gains default `kg_*` + `recall_hybrid` stubs that
  raise `NotImplementedError` (or fall back to `recall` for the
  hybrid case). Providers without KG support (`qdrant`,
  `memory_kg`) keep working unchanged.
* Shared `content_hash` helper extracted to
  `claude_hooks/providers/_content_hash.py`. Same SHA-256-of-
  whitespace-normalised-UTF-8 used by both pgvector + sqlite_vec
  + the cross-store migration tool.
* `pgvector_mcp/server.py` formatters now live in
  `claude_hooks/mcp_format.py` — sourced by both MCP servers.

### Migration notes

Existing v1.6.x sqlite_vec `.db` files migrate **in place** on
first `_ensure_ready()` after upgrade. No re-embedding, no row
loss; the legacy table + vec tables are extended, never rewritten.
A re-run of `install.py` triggers the migration immediately. If
the host's stdlib SQLite lacks the FTS5 trigram tokenizer (very
rare on modern builds), `kg_search_nodes` falls back to a
`LIKE %query%` name match — documented in
`docs/sqlite-vec-runbook.md`.

### Tests

75 new tests (21 migration, 13 hybrid, 18 KG, 14 MCP full-tools,
9 content_hash). Full suite: 2733 passed, 25 skipped.

## [1.6.1] — 2026-05-15

PATCH — three small UX fixes that surfaced during the v1.6.0
deploy. No schema changes; safe in-place upgrade from v1.6.x.

### Fixed

- **`install.py` API-proxy dialog: split into install + use**
  (`7c52980`). Before this commit the single
  ``Use the API proxy? (current: yes/no)`` question used
  ``cfg.proxy.enabled`` (which means *"is the proxy installed
  locally on this host"*) as the "current state" probe. Hosts
  pointing at a **remote** proxy via ``ANTHROPIC_BASE_URL`` (e.g.
  pandorum routing through solidpc:38080) saw ``current: no``
  even though they were clearly using a proxy. The detection was
  conflating two orthogonal concerns.

  New shape, two questions:

  - **Q1 — install locally?** ``Install the API proxy locally? [y/N]``
    (or, when the service is already on disk, ``[V]erify /
    [R]e-install / [S]kip? [V/r/s]`` with V default running a
    health probe).
  - **Q2 — use the API proxy?** ``Use the API proxy? (current:
    <label>) [Y/n or y/N]`` where ``<label>`` reflects what's in
    ``settings.json``: ``remote @ <url>`` / ``local @ <url>`` /
    ``will install local @ <url>, not wired yet`` / ``no``. If Y,
    asks for the endpoint with a sensible default (currently
    configured URL, else local address if just installed).

  New helpers: ``_proxy_locally_installed()``,
  ``_read_current_anthropic_base_url(settings_path)``,
  ``_classify_proxy_url(url)``, ``_verify_proxy_health(url)``.

- **`install.py` companion-tools: don't flag episodic-memory as
  MISSING on CLIENT-mode hosts** (`e3dfff4`). Same shape as the
  proxy fix above — ``shutil.which("episodic-memory")`` was
  asserting *"is this binary on PATH?"* while the meaningful
  question is *"is this host supposed to have it?"*. On
  CLIENT-mode hosts (the install POSTs to a remote episodic
  server), the local Node binary is never invoked, only the
  server host needs it. Detector now reads
  ``cfg.episodic.mode`` and reports ``n/a (CLIENT)`` with the
  ``[ok]`` marker instead of the ``MISSING`` warning. Server mode
  and the never-configured ``off`` state still warn so operators
  who lost the binary or haven't discovered episodic yet still
  see it.

- **`sqlite_vec.recall`: populate `_table` metadata for MCP
  formatter symmetry** (`9a79f3f`). The sqlite-vec-mcp formatter
  is shared with pgvector_mcp and renders results as
  ``[<table> dist=X] <text>``. pgvector populates ``_table`` per
  hit because it queries across multiple tables; sqlite_vec only
  ever queries the one configured table, but the formatter still
  expected ``_table`` — so without it we got ``[? dist=X]``.
  Trivial one-line fix; cosmetic only.

### Tests

- 22 new tests in ``tests/test_install_proxy_dialog.py`` covering
  the new dialog shape (classifier, current-URL reader,
  install-detection, Q1 prompts for both states, Q2 labels for
  all four states, endpoint-default priority, skip-path
  preserves settings.json).
- 6 new tests in ``tests/test_install_companion_tools.py``
  covering the episodic-memory CLIENT/SERVER/off branches and
  the special case's scope (other tools still warn).
- 1 new regression test in
  ``tests/test_sqlite_vec_integration.py`` pinning the
  ``_table`` metadata field.
- ``tests/test_install_proxy_orchestrator.py`` trimmed of the
  obsolete ``[1/2]`` two-mode tests (legacy dialog shape from
  v1.5 and earlier).
- Full suite: **2659 passed**, 25 skipped on solidpc.

### Upgrade

```bash
git pull --tags
git checkout v1.6.1
python install.py
```

Interactive re-run shows the new two-question proxy shape with
your existing ``ANTHROPIC_BASE_URL`` reflected in the Q2 label.
Non-interactive upgrades are silently safe — no destructive
changes, no schema migrations.

## [1.6.0] — 2026-05-15

MINOR — `sqlite-vec-mcp` system-wide launcher achieves full parity
with `pgvector-mcp`. External MCP clients (Cursor, Codex, OpenWebUI,
Claude Desktop) can now recall + store against the **same**
sqlite-vec `.db` file the claude-hooks hook pipeline reads
in-process. No new schema; one store, two access paths.

### Added

- New module `claude_hooks/sqlite_vec_mcp/` (`__init__.py` +
  `__main__.py` + `server.py`) — mirrors `pgvector_mcp/` in shape.
  Same `McpServer` JSON-RPC dispatcher, same stdio + HTTP transport,
  same OPTIONS/CORS/batch/413 plumbing. Trimmed to 3 tools for v1.6
  (memory only — no KG, no FTS5 hybrid):
  - `sqlite-vec-find` → `SqliteVecProvider.recall(query, k)`
  - `sqlite-vec-store` → `SqliteVecProvider.store(content, metadata)`
  - `sqlite-vec-count` → `SqliteVecProvider.count()`
- `pyproject.toml` `[project.scripts]` entry
  `sqlite-vec-mcp = "claude_hooks.sqlite_vec_mcp.__main__:main"` —
  `pip install claude-hooks` now exposes the launcher as a real
  console-script on PATH.
- `bin/claude-hook-sqlite-vec-mcp` + `.cmd` — POSIX + Windows shims
  matching the pgvector launcher pattern. Resolve the conda env's
  Python via `bin/_resolve_python.sh` (POSIX) or the same fallback
  chain as `claude-hook-pgvector-mcp.cmd` (Windows).
- `systemd/claude-hooks-sqlite-vec-mcp.service` — optional HTTP
  daemon, default port **32777**, env-var overrides
  `SQLITE_VEC_MCP_HTTP_HOST` / `SQLITE_VEC_MCP_HTTP_PORT`. Drop-in
  under `/etc/systemd/system/claude-hooks-sqlite-vec-mcp.service.d/`
  for per-host customisation.
- `install.py` gets three new helpers mirroring the pgvector ones:
  - `_sqlite_vec_launcher_path()` — chooses `~/.local/bin/sqlite-vec-mcp`
    (POSIX) or `%LOCALAPPDATA%\claude-hooks\bin\sqlite-vec-mcp.cmd`
    (Windows).
  - `_write_sqlite_vec_launcher(path, *, py, repo)` — emits the
    launcher script with interpreter + PYTHONPATH baked in.
  - `_register_sqlite_vec_mcp_in_claude_json(launcher_path)` —
    registers the launcher under `mcpServers.sqlite_vec` at the
    root of `~/.claude.json`; semantic backup written first
    (`.claude.json.bak-<ts>-sqlite-vec-mcp`).
  - `_validate_sqlite_vec_launcher(launcher_path)` — read-only
    `initialize` round-trip for the V/r/s re-run path.
- `_setup_sqlite_vec_mcp` extended with the launcher dialog
  (`Install system-wide MCP launcher? [Y/n]:`) and the v1.5.4-style
  `[V]alidate only / [R]e-install / [S]kip? [V/r/s]:` prompt for
  re-runs. `--non-interactive` installs the launcher when sqlite_vec
  is enabled.
- New runbook `docs/sqlite-vec-mcp.md` — install flow, external
  client wire-up, port table, known limitations.

### Fixed

- The generic `pick_provider` loop in `install.py` no longer asks
  "Enter MCP URL for SQLite + sqlite-vec" before the bespoke
  `_setup_sqlite_vec_mcp` dialog. The dead prompt is gone; sqlite_vec
  joins pgvector on the `pick_provider` skip-list. The
  `_setup_sqlite_vec_mcp` dialog owns the URL via the launcher path
  now.

### Tests

- `tests/test_sqlite_vec_mcp.py` — 24 unit tests for the dispatcher
  (handshake, tools/list shape, tools/call dispatch for each tool,
  error paths, formatters, full HTTP transport coverage). Mirrors
  the shape of `tests/test_pgvector_mcp.py`.
- `tests/test_install_sqlite_vec_launcher.py` — 12 tests for the new
  launcher path helpers (POSIX + Windows path shape via source
  inspection, writer body, ~/.claude.json registration with semantic
  backup, non-interactive setup drops launcher, dry-run skips writes,
  interactive yes/no paths, pick_provider skip-list guard).
- `tests/test_install_sqlite_vec_mcp.py` updated with an autouse
  fixture stubbing the new launcher helpers + one extra "n" answer
  in each interactive script (skip the launcher prompt; the launcher
  itself is covered by the new test file).
- Full suite: **2643 passed**, 24 skipped on solidpc (+36 vs v1.5.4).

### Upgrade

```bash
git pull --tags
git checkout v1.6.0
python install.py
```

On re-run, the existing sqlite_vec config is preserved; if the
launcher isn't already present the dialog asks whether to install
it (Y default). External clients (Cursor, Codex, ...) need their
own MCP-server config pointing at the new launcher path — see
`docs/sqlite-vec-mcp.md` for the recipe.

## [1.5.4] — 2026-05-15

PATCH — fixes a UX paper-cut in ``install.py``'s pgvector
sub-dialog. When pgvector was already fully configured (DSN set,
enabled, system-wide launcher present), running ``install.py``
interactively still asked ``Set up pgvector? [Y/n]`` with **Y** as
the default — implying a fresh setup was about to overwrite the
working configuration. The only way out was to type ``n`` even
though the install was working.

### Fixed

- ``install.py:_setup_pgvector_mcp`` now detects the
  fully-configured state (DSN + enabled + launcher) and offers a
  ``[V]alidate only / [R]e-install / [S]kip? [V/r/s]`` prompt with
  **V** as the default. ``V`` runs a read-only round-trip against
  the configured DSN and reports the result; ``R`` falls through
  to the existing re-install flow; ``S`` exits the sub-dialog
  untouched. Partially configured states (DSN-without-enabled,
  enabled-without-launcher) keep the legacy ``[Y/n]`` prompt so
  the install dialog still walks the user through completing the
  setup.
- New ``install._validate_pgvector_only(cfg)`` helper performs the
  read-only check so the validate path doesn't touch the running
  configuration.

### Tests

- ``tests/test_install_pgvector_validate.py`` — 14 tests covering
  the fully-configured / partially-configured / launcher-missing
  branches, each interactive choice (``V`` / ``R`` / ``S`` plus
  empty-default), the validate-only round-trip, and the
  non-interactive fast-path (which stays at ``assume yes`` for
  scripted installs that genuinely want a re-install).
- Full suite: 2607 passed, 24 skipped on solidpc.

## [1.5.3] — 2026-05-15

PATCH — emergency hotfix for v1.5.2. The v1.5.2 prep accidentally
introduced a duplicate ``_wait_for_consultants_health`` function in
``install.py``. The original at line 4732 has the signature
``(port: int, *, timeout: float = 30.0) -> bool``; the new one I
added at line 1968 had ``(*, timeout: float = 15.0) -> None``.
Python's last-def-wins overrode the new one with the original, so
my caller at line 1965 (``_wait_for_consultants_health(timeout=15.0)``)
raised ``TypeError: missing 1 required positional argument: 'port'``
on any host with the consultants engine installed.

Test coverage missed it because the unit tests
``patch.object(install, "_wait_for_consultants_health")`` — patching
replaces whichever def Python resolved, so the wrong signature
slipped through.

### Fixed

- Removed the duplicate ``_wait_for_consultants_health`` definition.
  ``_restart_consultants_service`` now reuses the existing
  ``_wait_for_consultants_health(port, timeout)`` helper (which polls
  ``/v1/health``, matching what the consultants engine actually
  serves) instead of duplicating the loop with a different endpoint.
- Restart still completes successfully; the success / timeout
  messages are formatted by the caller now that the helper returns
  bool rather than printing itself.

### Tests

New regression guard in ``tests/test_install_service_restart.py``:
``test_consultants_restart_invokes_health_probe_cleanly`` calls the
real ``_restart_consultants_service`` end-to-end with the real
``_wait_for_consultants_health`` patched only at the return value.
Asserts the call uses the ``(port, *, timeout=...)`` signature so
any future signature drift raises in CI instead of in production.

Full suite: 2593 passing, 24 skipped (+1 over v1.5.2).

## [1.5.2] — 2026-05-15

PATCH — install.py end-of-install service restart. Closes a gap
that bit the 2026-05-15 v1.5.0 deploy: install.py declared
"daemon: responding ✓" while the running daemon process was still
executing pre-pull bytecode (newly-pulled code was on disk but
never imported until the daemon was killed manually). After this
fix, `git pull && python install.py` consistently picks up new
code without any manual restart step.

### Added

- `_restart_managed_services(dry_run, skip)` runs at end of
  `main()` and restarts both `claude-hooks-daemon` and the always-on
  `claude-hooks-consultants` service when they exist.
- `_restart_claude_hooks_daemon()` — cross-platform restart:
  - **Linux**: `systemctl restart claude-hooks-daemon.service` if
    `/etc/systemd/system/claude-hooks-daemon.service` exists.
  - **macOS**: `launchctl unload + load -w` on
    `com.claude-hooks.daemon.plist`.
  - **Windows**: `schtasks /End /TN claude-hooks-daemon` then
    `/Run /TN claude-hooks-daemon`. No UAC prompt since the task
    is owned by the current user.
  - After restart, polls the daemon's HMAC port (47018) for up to
    20 s. Prints `restarted + responding` on success; warns
    without failing the install on timeout.
- `_restart_consultants_service()` — same shape, targets the
  consultants engine. Linux uses `systemctl --user restart
  claude-hooks-consultants.service`; Windows uses
  `schtasks /End + /Run` on `claude-hooks-consultants`. Health
  check polls `http://127.0.0.1:38095/health` for up to 15 s.
  Smart-start mode (lazy-spawn) has nothing long-lived to recycle
  so it's silently skipped — the next cold spawn picks up new code.
- New `--skip-daemon-restart` flag for the rare case the user
  wants install.py to leave running processes alone. Prints a hint
  reminding them to run `claude-hooks-daemon-ctl restart` manually
  when they want the new code loaded.

### Fixed

- Both Linux (`_setup_systemd_daemon`, install.py:1573-1581) and
  Windows (`_install_daemon_windows_steps`, install.py:2044-2077)
  previously had a "if service already exists, just probe port +
  return" branch with no restart. That meant a re-run of
  `install.py` after `git pull` left the daemon running stale
  Python bytecode until something else killed the process.
  v1.5.2 layers the unconditional end-of-install restart on top,
  preserving the existing "don't recreate the systemd unit / task
  file if it's identical" idempotency.

### Tests

15 new tests in `tests/test_install_service_restart.py`:
`--dry-run` skips, `--skip-daemon-restart` skips + prints hint,
default calls both restarts; Linux systemctl path (skip when unit
missing, call with unit present, warn when systemctl fails, warn
when daemon doesn't come back); Windows schtasks path (skip when
task missing, /End-then-/Run sequencing, warn when /Run fails);
consultants service (skip when missing on Linux/Windows, restart
when present on both platforms); argparse flag wiring.

Full suite: 2592 passing, 24 skipped (+15 new, +0 regressions).

## [1.5.1] — 2026-05-15

PATCH — install.py hook-path drift safeguard + every-write backup
trail. Closes a silent destructive-rewrite bug that bit a real
deployment: running `install.py` from a second clone at a
different filesystem path used to rewrite all existing
`_managedBy: claude-hooks` hook entries in `~/.claude/settings.json`
to point at the new location with no warning, effectively
un-deploying the working install.

### Added

- **Path-drift detection** in `install_hooks`: compares the existing
  `_managedBy` hook commands' repo path against the current
  install.py invocation's repo path. On mismatch, raises
  `HookPathDrift` (exit 2) in `--non-interactive` mode; in
  interactive mode, prompts with a side-by-side path diff and only
  rewrites on explicit `y`. New `--rewire` flag overrides the
  refusal when an intentional clone migration is desired.
- **Semantic backup names**: `backup_path(p, reason="...")` now
  embeds a kebab-case reason tag in the timestamped backup filename
  (e.g. `settings.json.bak-20260515-074559-hook-rewrite`,
  `...-plugin-marketplace`, `...-env-vars`, `...-uninstall`). A
  directory of backups becomes readable at a glance.
- New `_backed_up_save_json(path, data, *, reason, dry_run=False)`
  helper that funnels every settings.json save through the
  backup-then-write path. Replaces three previously-unbacked
  `_save_json(settings_path, ...)` call sites (plugin marketplace
  registration, recommended-plugin enable, uninstall).
- 19 new tests in `tests/test_install_path_drift.py`:
  `backup_path` reason sanitization + suffix, `_backed_up_save_json`
  behaviour (writes / no-write / dry-run / backup-content),
  `_extract_existing_hook_repo_path` (empty / no-managed / POSIX /
  Windows / backslash command / most-common tie-break),
  `install_hooks` drift (non-interactive refuse, --rewire override,
  interactive Y/N, same-path idempotent, fresh-install no-prompt,
  semantic backup name verification).

### Fixed

- `_save_json(settings_path, ...)` calls at three sites that
  previously wrote without backing up (plugin marketplace
  registration, recommended-plugin enable, uninstall) now go
  through `_backed_up_save_json` so every mutation leaves a
  recovery trail.

### Background

The 2026-05-12 incident on pandorum: an install.py run from
`C:\Users\manni\dev\claude-hooks` (a second clone created
inadvertently) silently rewrote all 6 hook entries in settings.json
to point at the `\dev\` path. The scheduled tasks still ran the
daemon from `C:\Users\manni\claude-hooks`, leaving hooks and the
daemon out of sync for three days. Repaired manually 2026-05-15;
this patch makes the regression impossible in non-interactive mode
and loud in interactive mode.

Test count: 2577 passing, 24 skipped (+19 new, +0 regressions).

## [1.5.0] — 2026-05-14

MINOR — extends v1.4's llamafile integration from embedding-only to
the **chat-completion side**. HyDE, `/reflect`, `/consolidate`,
`/get-advice`, `/consultants`, and the `caliber-grounding-proxy`
can now route to a daemon-supervised local llamafile via a new
`llamafile://<label>` model identifier prefix. Bare-Ollama
identifiers and `:cloud` suffix continue to route to Ollama
unchanged — opt-in for existing installs.

### Added

- **Chat-model registry**: `~/.claude/llamafile-models.json`
  (schema v1), `claude_hooks/chat_model_registry.py` (load / save /
  list / add / remove / rename / copy with port-collision +
  GGUF-magic validation, schema migration).
- **Daemon-side `ChatModelManager`**: multi-instance variant of
  v1.4's `EmbeddingManager`. `dict[label, ProcessHandle]`, LRU
  eviction at `max_concurrent_loaded`, per-label idle reap
  (default 600 s; streaming chat calls update `last_activity_at`
  per chunk so long generations can't be reaped mid-call),
  per-label sticky CPU fallback on GPU spawn failure, registry
  mtime hot-reload, orphan GC.
- **Daemon RPC ops** (`_chat_model_ensure / _chat_model_status /
  _chat_model_shutdown / _chat_model_gc`) with typed wrappers in
  `daemon_client.py`. Best-effort semantics match v1.4 embedding ops.
- **Shared chat backend** (`claude_hooks/chat_backend.py`):
  `parse_model_ref`, `OllamaChatClient` (extracted from pre-v1.5
  `_call_ollama`), `LlamafileChatClient` (daemon-ensured, OpenAI
  `/v1/chat/completions`, port-cache TTL, retry-on-failure with
  re-ensure), `make_chat_client` factory, `call()` one-shot helper.
- **Agent-loop chat client factory** in `get_advice/chat_client.py`:
  `LlamafileAgentChatClient` (same `chat(payload) -> dict` interface
  as `ChatClient`, talks OpenAI `/v1/chat/completions` directly,
  maps OpenAI `usage` -> Ollama `last_usage` field names) +
  `make_agent_chat_client` factory. Both `/get-advice` CLI and
  `/consultants` runner construction sites use it.
- **caliber-grounding-proxy openai_compat mode**: new env var
  `CALIBER_GROUNDING_UPSTREAM_BACKEND=openai_compat` skips the
  OpenAI ↔ Ollama translation entirely (targets
  `<upstream>/v1/chat/completions`). Retry budget, empty-content
  detection, and FlapCounters still apply. Surfaced at `/health`
  for ops visibility.
- **`claude-hooks-models` CLI** (`bin/claude-hooks-models` +
  `.cmd`, `claude_hooks/models_cli.py`). Subcommands:
  `list / add / remove / rename / copy / show / path / probe /
  gc`. Daemon-talking subcommands degrade gracefully when the
  daemon is down.
- **`install.py` chat-backend dialog**: new
  `_setup_chat_backends` dispatcher wraps the existing
  `_setup_ollama_chat` and a new `_setup_llamafile_chat_models`
  sub-dialog (GGUF path + label + ctx + mode + port, optional
  wiring of `hyde_model_ref` / `reflect.model_ref` /
  `consolidate.model_ref`).
- **`*_model_ref` config keys**: `hooks.user_prompt_submit.{hyde_model_ref,
  hyde_fallback_model_ref}`, `reflect.model_ref`,
  `consolidate.model_ref`. Take precedence over the legacy
  `*_model` / `*_url` keys when set.

### Changed

- `hyde.py` / `reflect.py` / `consolidate.py`: bare-ref calls keep
  using the existing `_call_ollama` helper (so tests that
  monkeypatch it stay valid); `llamafile://<label>` refs dispatch
  through `chat_backend.call`.
- `Registry.__init__` resolves `DEFAULT_REGISTRY_PATH` at call time
  (was function-definition time) so test fixtures and installers
  can monkeypatch the constant.
- README `Where the system listens` table adds the 38093-38099
  chat-llamafile port range.
- CLAUDE.md status banner v1.4.0 -> v1.5.0; new Key directories
  bullet for the chat engine.

### Tests

2558 passing, 24 skipped (+225 from v1.4's 2333). Coverage:
registry CRUD + schema migration (61), `ChatModelManager`
lifecycle (36), daemon chat RPC (28), `chat_backend` (32), HyDE /
reflect / consolidate dispatch (10), agent-loop factory (12),
caliber openai_compat (7), models CLI (29), install dialog (10).

### Known limitations

- Cross-backend `extra_models` fan-out in `/consultants` reuses
  the role's primary client; same-backend fan-out works. Deferred
  to v1.5.1.
- llamafile picks one device per process; multi-GPU placement
  deferred.
- Registry is per-host; remote `llamafile://<host>/<label>` deferred.

### Docs

- New: [`docs/llamafile-chat-models.md`](docs/llamafile-chat-models.md),
  [`docs/whats-new.md`](docs/whats-new.md) (v1.5).
- Archived: `docs/whats-new.md` (v1.4) -> `docs/whats-new-v1.4.md`.
- Updated: [`docs/daemon.md`](docs/daemon.md) (new RPC ops table +
  chat-model lifecycle section),
  [`docs/caliber-proxy.md`](docs/caliber-proxy.md) (new
  "Pointing at llamafile" section),
  [`docs/llamafile-integration.md`](docs/llamafile-integration.md)
  (scope clarification), [`README.md`](README.md), [`CLAUDE.md`](CLAUDE.md).

## [1.4.0] — 2026-05-14

MINOR — adds **mozilla-ai/llamafile@0.10.1** as a fallback-capable
embedding engine for the local-embed providers (`pgvector` and
`sqlite_vec`), supervised by the existing `claude-hooks-daemon`.
A healthy install can now survive an Ollama outage; a fresh
install can run without an Ollama dependency at all.

Opt-in: existing installs keep their Ollama-only embedder until
`install.py` is re-run.

### Added

- **`LlamafileEmbedder` + `CompositeEmbedder`**
  (`claude_hooks/embedders.py`). The composite tries the primary
  (Ollama / OpenAI-compatible) on every embed and drops to the
  fallback on `EmbedderError`, with a dim-mismatch guard so the
  vector space stays stable across failover.
- **`EmbeddingManager`** (`claude_hooks/embedding_manager.py`) —
  daemon-side llamafile lifecycle. Spawn-on-demand, 5-minute idle
  reap (matches Ollama's `OLLAMA_KEEP_ALIVE=5m`), SIGTERM →
  10 s → SIGKILL ladder, PID-file at
  `~/.claude/embedding-server.pid` for re-adoption across daemon
  restarts. APE-binary `/bin/sh` shim on POSIX so the
  Cosmopolitan-Libc binary boots without binfmt_misc registration.
- **`gpu_probe`** (`claude_hooks/gpu_probe.py`) — `nvidia-smi` /
  `rocm-smi` / `vulkaninfo` chain with 2-second timeout. Used at
  install time to suggest defaults and at runtime to decide the
  `-ngl 99` vs `--gpu disable` spawn flag.
- **Daemon RPC ops** (`claude_hooks/daemon.py`,
  `claude_hooks/daemon_client.py`): `_embedding_ensure`,
  `_embedding_status`, `_embedding_shutdown` with typed
  best-effort wrappers.
- **HyDE / reflect / consolidate installer dialog**
  (`install._setup_ollama_chat`). Until v1.4 these sections had
  zero interactive prompts (hard-coded defaults in `config.py`).
  The dialog asks for the Ollama chat URL, HyDE model + fallback
  + `num_ctx`, and offers a shared-skills shortcut so reflect +
  consolidate inherit by default.
- **`install._setup_embedding_engine`** — parameterized embedder
  dialog now drives **both** pgvector and sqlite_vec; "use the
  previous provider's choice?" shortcut on the second invocation.
  OpenAI-compatible primary supported alongside Ollama-primary
  and llamafile-primary.
- **`install._setup_sqlite_vec_mcp`** — sqlite_vec previously had
  **zero** installer code; v1.4 pays back that latent gap.
- **`install._validate_qdrant_embedding` /
  `_validate_memory_kg_embedding`** — validate-only branches for
  the server-side-embedding MCPs. Probe connectivity, surface a
  one-line note about where the embedding model lives, never
  mutate `cfg`.
- **`vendor/llamafile/dist/Makefile`** — reproducible
  composite-build recipe. Two consecutive
  `make clean && make` invocations produce byte-identical output
  (verified SHA `414f6166...` for the canonical
  qwen3-embedding-0.6b-16k composite).
- **`vendor/llamafile/dist/SHA256SUMS.composite`** — committed
  in-tree; `install.py` verifies the GH-Release-downloaded asset
  against it (hard error with `redownload or rebuild` breadcrumb
  on mismatch).
- **`docs/llamafile-integration.md`** — architecture + installer
  flow + ops runbook.
- **~187 new tests** (2333 passed + 24 skipped at cut, up from v1.3.2's 2146):
  `test_embedders_llamafile.py`, `test_gpu_probe.py`,
  `test_embedding_manager.py` (incl. APE-wrap regressions),
  `test_daemon_embedding_rpc.py`,
  `test_install_embedding_engine.py`,
  `test_install_sqlite_vec_mcp.py`, `test_install_ollama_chat.py`,
  `test_install_validate_mcp_embedding.py`.

### Changed

- **`install._setup_pgvector_mcp`** now delegates its embedder
  dialog to `_setup_embedding_engine`; the DSN/schema/init path
  is unchanged. The Ollama-side model-pull stays in the
  Ollama-primary branch only.
- **`main()` ordering**: `_setup_ollama_chat` →
  `_setup_pgvector_mcp` → `_setup_sqlite_vec_mcp` →
  `_validate_qdrant_embedding` → `_validate_memory_kg_embedding`
  → `_setup_proxy_orchestrator`. The chat URL is settled before
  the embedder dialog uses it; the validate-only providers
  report after the client-embed providers are configured.
- **Canonical embedding port `38092`** — adjacent to caliber-proxy
  (38090) and consultants (38095).

### Fixed

- **Windows console-window detachment** in
  `EmbeddingManager._spawn_once` (`ff14f3a`). The spawned
  llamafile was inheriting a console on Windows because the code
  only passed POSIX `start_new_session=True`. v1.4 ships with
  `CREATE_NO_WINDOW | DETACHED_PROCESS` on Windows + stdin=DEVNULL,
  matching the pattern used by `claudemem_reindex._spawn_reindex`
  and `lsp_engine.client`. Verified on pandorum: the new spawn
  reports `Window Title: N/A` and no cmd window appears.

### Distribution

- **GitHub Release asset** for the composite (~1.5 GB) —
  `qwen3-embedding-0.6b-16k.llamafile` attached to the `v1.4.0`
  release. Clones stay small (~10 MB); `install.py` fetches the
  asset only when needed, verifies against the committed SHA, and
  falls back to `urllib.request` if `gh` is absent.

### Verified

- Reproducible composite builds on solidpc (Linux + RTX 3090).
- End-to-end through standalone daemon: cold-spawn 1.2 s, 1024-dim
  L2-normalized vector, idle reap clean.
- Full test suite green: 2333 passed + 24 skipped (final pre-cut run).

## [1.3.2] — 2026-05-13

PATCH — fixes a long-standing version-drift bug that caused the
update-check banner to misreport every release since v1.0.3.

### Fixed

- **`claude_hooks.__version__` no longer drifts from `pyproject.toml`.**
  The constant was hard-coded to `"1.0.3"` and never bumped during
  the v1.0.4 / v1.1.0 / v1.2.0 / v1.3.0 / v1.3.1 cuts (only
  `pyproject.toml`, `CHANGELOG.md`, and the `CLAUDE.md` banner were
  updated each time). The Stop-hook update-check banner reads
  `CURRENT_VERSION` from this constant, so every install reported
  itself as `current 1.0.3` — visible to users as e.g.
  `[claude-hooks] update available: v1.3.1 (current 1.0.3)` on a
  host that was actually running v1.3.1.
- **`claude_hooks/__init__.py`** now resolves `__version__` at import
  time via a three-step chain:
  1. `importlib.metadata.version("claude-hooks")` — canonical when
     pip-installed (editable or wheel).
  2. Walk up from `__file__` looking for `pyproject.toml`, parse
     `[project].version` with a tiny hand-rolled scanner (no
     `tomllib` import, keeps the 3.9 floor). This is the path the
     `bin/claude-hook` shim install model hits.
  3. Final string fallback (`"0.0.0+unknown"`) — only reached on a
     broken deploy; conservative so update-check reports "no update
     available" rather than hallucinating a build number.
- **`tests/test_version_no_drift.py`** pins the contract: a new
  test asserts `claude_hooks.__version__` equals the
  `pyproject.toml::[project].version` value. The cut procedure no
  longer relies on remembering to edit two files in lock-step.

## [1.3.1] — 2026-05-13

PATCH — single-bug fix for the `sqlite_vec` backend.

### Fixed

- **sqlite_vec is no longer silently skipped on every event.**
  `claude_hooks/dispatcher.py:build_providers` only checked for
  `mcp_url` (HTTP MCP backends) or `dsn` (pgvector) when extracting
  the per-provider URL it hands to `ServerCandidate.url`. The
  sqlite_vec provider — and its example config — write the path
  under `db_path`, so the dispatcher saw an empty URL and skipped
  the provider unconditionally with
  `provider sqlite_vec has no mcp_url/dsn configured — skipping`.
  Net effect on a sqlite_vec-only install: no DB was ever created,
  recall and storage were both no-ops for the lifetime of the
  install. The dispatcher now also accepts `db_path` and the log
  message reflects all three field names. Regression test in
  `tests/test_coverage_phase8.py::TestBuildProviders::
  test_sqlite_vec_db_path_accepted_as_url`.
  Reported and diagnosed end-to-end by
  [@JGFSnyman](https://github.com/JGFSnyman) in
  [#2](https://github.com/mann1x/claude-hooks/issues/2) — thanks!

## [1.3.0] — 2026-05-12

MINOR bump for a **user-facing slash-command vocabulary change**
— the per-verb skills shipped at v1.1 (`/get-advice--model`,
`/get-advice--effort`, `/get-advice--tools`,
`/consultants--config`, `/consultants--list`, `/consultants--show`,
`/consultants--followup`) are collapsed into two dispatcher
skills. Backing CLIs (`claude-advisor`, `claude-consultants`)
already subcommand-dispatch internally; the skill-file split was
pure duplication of that CLI shape and burned 9 entries in the
Claude Code slash-command menu (each with its own description).
The dispatcher pattern cuts that to 2 entries while keeping all
functionality.

### Changed (breaking — slash-command shape)

- **`/get-advice <query>`** is now a dispatcher with verbs:
  - `ask <query>` — run / continue an advisor conversation
    (default; **implicit** — bare `/get-advice <query>` works).
  - `model [NAME [CTX]]` — report or set the advisor's Ollama
    model and pinned context length. Replaces `/get-advice--model`.
  - `effort [tier]` — report or set the sessions-per-invocation
    budget (`low`/`medium`/`high`/`max`). Replaces
    `/get-advice--effort`.
  - `tools [csv|all|none]` — report or set the tool list exposed
    to the advisor. Replaces `/get-advice--tools`.
- **`/consultants <query>`** is now a dispatcher with verbs:
  - `ask <query>` — run a fresh council on a question (default;
    **implicit** — bare `/consultants <query>` works).
  - `followup [<sid>] <question>` — iterate on a prior session,
    failed-session-aware. Replaces `/consultants--followup`.
  - `list [--limit N]` — past sessions. Replaces
    `/consultants--list`.
  - `show <sid> [--raw]` — re-read a stored summary. Replaces
    `/consultants--show`.
  - `config [args...]` — interactive role/model/effort/service-
    mode walk-through, or passthrough sub-args. Replaces
    `/consultants--config`.
- The seven per-verb slash commands are **removed cold-turkey**;
  no aliases retained. Net upfront menu cost drops by ~7 skill
  descriptions per session; total skill body 42 KB → 28 KB.

### Added

- **Idempotent legacy-cleanup pass in `install.py`**
  (`_install_skills` → `LEGACY_SKILL_DIRS`). On upgrade, removes
  `~/.claude/skills/get-advice--{model,effort,tools}/` and
  `~/.claude/skills/consultants--{list,show,config,followup}/`
  so the old slash commands stop appearing in the menu. Runs
  unconditionally — no-op on fresh installs, removes on first
  v1.3 run, no-op on re-runs. Respects `--dry-run`.
- **6 new tests** at `tests/test_install_skills_legacy_cleanup.py`
  covering the cleanup contract (constant enumerates all v1.2
  variants, removes pre-seeded stale dirs, no-op fresh,
  idempotent re-run, dry-run prints but doesn't touch, SKILLS
  list registers only the two dispatchers).

### Documentation

- **`docs/get-advice.md`** — rewrites slash-command usage section
  to the verb form, adds a v1.3 migration note.
- **`docs/consultants.md`** — rewrites all `/consultants--*`
  references to `/consultants <verb>` form, adds a v1.3 migration
  note.
- **`docs/whats-new.md`** — preserved as historical v1.1 record;
  callout at top points readers at the v1.3 dispatcher shape for
  the up-to-date invocations.
- **`README.md`** — collapses the 9-row skills table section to
  2 rows showing the dispatchers with their verb lists.
- **`CLAUDE.md`** — status banner extended with the v1.3 paragraph.
- **`.wolf/anatomy.md`** — collapses the 9 skill entries to 2 with
  verb summaries.

## [1.2.0] — 2026-05-09

MINOR bump for the **caliber-grounding-proxy cloud-resilience
layer** — a new opt-in retry subsystem visible to any `caliber init`
run against a flapping cloud Ollama. Also ships the v1.2 of the
`/consultants` benchmark protocol (Q3 actionability sub-rubric, first
confirmed heterogeneous PROD-READY label) and the first caliber-eval
cohort published in-repo (six labels graded against the `claude-cli`
reference).

### Added

- **caliber-grounding-proxy cloud-resilience retry layer**
  (`claude_hooks/caliber_proxy/ollama.py`, shared
  `claude_hooks/_chat_retry.py`). Two parallel retry budgets
  protect every chat completion to upstream Ollama:
  - **15-attempt HTTP/network budget** with exponential backoff
    (base 1.5 s, cap 90 s, ≈ 15 min total). Catches `408 / 429 /
    500 / 502 / 503 / 504` plus a curated list of retryable 4xx
    body substrings (the same set the consultants engine
    already proved against `kimi-k2.6:cloud` flapping).
  - **5-attempt empty-content budget** for `200 OK` responses
    with empty `content`, no `tool_calls`, and
    `finish_reason ≠ length` — the "throat-clearing" pattern
    every cloud-tagged Ollama model exhibits on heavy initial
    prompts.

  Tunable via env vars: `CALIBER_PROXY_RETRY_MAX_ATTEMPTS`,
  `CALIBER_PROXY_RETRY_BASE_DELAY_S`, `CALIBER_PROXY_RETRY_MAX_DELAY_S`,
  `CALIBER_PROXY_EMPTY_RETRY_MAX`. Defaults match the consultants
  engine, so behavior is consistent across both cloud paths.
- **`FlapCounters` exposed at `/health.upstream_flaps`**. Five
  process-scoped counters surfaced in the `/health` JSON:
  `upstream_5xx_total`, `upstream_retryable_4xx_total`,
  `upstream_empty_total`, `upstream_retry_succeeded_total`,
  `upstream_retry_exhausted_total`. Lets `claude-hooks-rollup`
  (and any operator dashboard) detect cloud-weather degradation
  before it fails a bench.
- **Generic tool-call passthrough on the proxy round-trip.**
  Provider extras like Gemini's `thought_signature` are now
  preserved verbatim across both legs of the round-trip instead
  of being stripped — the earlier targeted strip broke
  `gemini-3-flash-preview:cloud` with
  `400 missing thought_signature in functionCall parts`. A small
  denylist (`function.index` for deepseek/qwen; empty at top
  level) handles the inverse case where an upstream field would
  confuse the OpenAI-compat client. Net effect: every cloud model
  that ships a custom tool-call extra works without per-model
  patches.
- **caliber-eval cohort published** at
  [`docs/caliber-eval-results/`](docs/caliber-eval-results/) —
  six labels graded against the `claude-cli` reference:
  `gemma-native-tools-v3`, `gemma4-31b-cloud`,
  `gemini-3-flash-preview-cloud`, `deepseek-v4-flash-cloud`,
  `glm-5-1-cloud`. Each label ships its `score.py` JSON + a
  narrative summary comparing to baseline. The workbench (full
  rsynced workspaces, run logs, fake-HOMEs) stays off-repo at
  `/srv/dev-disk-by-label-opt/dev/caliber-eval/` per
  `PROTOCOL.md`, which documents the reproduce + publish recipe.
- **`docs/caliber-eval.md`** — in-repo entry-point pointing at the
  off-repo workbench and the published-results dir.
- **`docs/PLAN-caliber-proxy-cloud-resilience.md`** —
  implementation plan that drove the resilience port (marked
  "shipped 2026-05-09").
- **27 new tests** at `tests/test_caliber_proxy_retry.py`
  covering the decision helpers (`is_retryable_status`,
  `is_retryable_empty_response`, `compute_backoff`) and an
  end-to-end mocked `httpx` harness exercising both budgets.
- **Updated `tests/test_caliber_proxy.py`** with passthrough
  coverage: `test_assistant_tool_calls_passthrough_unknown_fields`,
  `test_assistant_tool_calls_function_index_stripped`,
  `test_response_tool_call_extras_passthrough`,
  `test_response_tool_call_preserves_upstream_id`,
  `test_round_trip_preserves_provider_extras`.

### Fixed

- **consultants synthesizer no longer silently synthesizes over a
  failure tombstone.** The SYNTHESIZER prompts now refuse to render
  a coherent answer when an upstream role marked the section as
  failed, surfacing the failure in the final synthesis instead.
  Caught by the v1.2 protocol's Q3 actionability sub-rubric.

### Documentation

- **`/consultants` benchmark sweeps** — 2026-05-09 cloud screening
  (7 new models), N=3 aggregate runs of the 3 PROD-READY
  candidates, Q3 actionability re-grade against protocol v1.2,
  per-role recommendation refresh, and the first confirmed
  heterogeneous PROD-READY label
  ([`mix-gemini-PRC-gemma4-S-2026-05-09`](docs/benchmarks/mix-gemini-PRC-gemma4-S-2026-05-09/)).
- **`docs/benchmarks/index.md`** — refreshed TL;DR (5 PROD-READY
  labels at v1.1.0 engine HEAD), per-role token/wall winner
  matrix, cross-reference to the caliber cohort with the
  caliber-init verdict (`claude-cli` stays default; `glm-5.1:cloud`
  is the recommended non-claude-cli fallback).
- **`docs/caliber-eval-results/README.md`** — `tl;dr — verdict`
  section with the pick-when table and explicit disqualifications
  (`deepseek-v4-flash:cloud` and `gemini-3-flash-preview:cloud`
  both fail the references-point-to-real-files rubric — the same
  grounding-discipline weakness they show on consultants Q3).

## [1.1.0] — 2026-05-08

MINOR bump for several new opt-in subsystems landed since v1.0.3:
the `/get-advice` LLM-to-LLM advisor skill (multi-turn second
opinions via local Ollama), the shared `agent_loop.runner` that
backs both caliber and the advisor, the stop_guard stall check, a
full pgvector backup + canary stack, and the v1.1 of the
`/consultants` agentic engine — full per-role LLM message-history
persistence so a session closed and reopened from disk produces
identical follow-up answers to a warm one, plus multi-model
researcher (xmedium / xhigh) and multi-critic + meta-critic
(xmax) fan-out tiers for hard architectural questions where
diverse cloud-model perspectives matter. Ten phases on `dev`
(`9f71c9d`..`cdea074`) plus the planning commit (`5cb6738`).

### Added

- **/consultants v1.1 — multi-model x-tiers (xmedium / xhigh / xmax)**
  — three new effort tiers that fan out fan-outable roles across
  multiple Ollama models per plan-item lane, so the synthesizer
  (or meta-critic at xmax) sees diverse perspectives from
  different model trainings on the same evidence. Configured via
  per-role `extra_models = [...]` in the role's TOML block;
  silently ignored at every base tier (a benchmark labeled
  `high` is never accidentally 3× the cost — opting into x-tiers
  requires the explicit tier name). xmedium / xhigh only fan
  out the researcher; xmax additionally fans out the critic and
  adds a meta-critic node that synthesizes the C parallel-critic
  verdicts into one consolidated decision (anonymized as
  `Critic 1` / `Critic 2` / ... in the prompt to avoid biasing
  toward a model the meta-critic "knows" performs better; the
  recorder's per-row `model` column is the audit map). Cost-of-
  fan-out warning fires once at consultation start with the
  expected token-cost multiplier. Skill (`/consultants--config`)
  gains a "Manage extra models" sub-action under researcher /
  critic; CLI gains `set-role <role> --add-model X --remove-model
  Y --clear-extras`. Live-verified on solidpc — xmax with 2
  researcher models and 3 critic models produces 3 distinct
  critic verdicts (one per model) that the meta-critic
  consolidates. Ten phases on `dev` (`9f71c9d`..`cdea074`).

- **/consultants v1.1 — full message-history persistence** — every
  consultation now produces a SQLite `transcript.db` sidecar at
  `<cwd>/.claude-hooks/consultants/<sid>/transcript.db` alongside
  the existing `summary.md`, `transcript.md`, and `metadata.json`.
  The recorder writes one row per LLM call, tool execution, and
  node enter/exit boundary in WAL mode (concurrent fan-out lanes
  write through per-thread connections). When a session is
  reopened from disk after engine restart or eviction, the
  per-role LLM message threads are reconstructed via SQL —
  follow-ups against disk-reopened parents now extend those
  threads with the new question instead of rebuilding prompts
  from scratch. Live-verified: a follow-up against a closed
  parent issued **0 tool calls vs. the parent's 5** because the
  model could lean on prior tool results in context (the explicit
  v1.1-is-done criterion from the plan). SQLite was chosen over
  JSONL for opacity to text indexers (`claudemem reindex`,
  ripgrep, RAG ingestors) since `transcript.db` carries full LLM
  payloads. Schema documented at
  [`docs/consultants-transcript-db-schema.md`](docs/consultants-transcript-db-schema.md);
  inspect a session via `claude-consultants show --raw <sid>` with
  optional `--filter role=researcher --filter kind=tool_call
  --limit N`. Backward-compatible: v1.0 sessions without a `.db`
  reopen via the existing turn-content fallback. The legacy v1.0
  JSONL trace at `~/.claude/consultants-traces/<sid>.jsonl` is
  decommissioned; `CONSULTANTS_TRACE` and the `--trace` /
  `--no-trace` CLI flags are now no-ops with a one-shot
  deprecation warning (will be removed in v1.2). Plan and
  pre-implementation log: [`docs/PLAN-consultants-v1.1-message-history.md`](docs/PLAN-consultants-v1.1-message-history.md).

- **/get-advice — LLM-to-LLM advisor skill** — Claude Code can now consult
  a configured Ollama model (default `qwen3.5:cloud`) for a multi-turn
  second opinion via the `/get-advice <query>` skill. Three helper
  skills (`/get-advice--model`, `/get-advice--effort`,
  `/get-advice--tools`) configure model + ctx, effort tier (low=1
  session / medium=3 / high=5 / max=25), and the per-tool gate
  (CSV / `all` / `none`) without editing JSON. Settings persist to
  `~/.claude/get-advice-config.json`. New CLI `bin/claude-advisor`
  drives the conversation: `turn`, `reset`, `cleanup`, get/set
  subcommands. Per-turn JSON exposes `prompt_eval_count` /
  `eval_count` so Claude knows when to summarize and reset before the
  advisor's context fills (default threshold 85%). Reuses caliber-proxy
  grounding (project anchors + structure map) and the same six tools
  (`read_file`, `grep`, `glob`, `list_files`, `survey_project`,
  `recall_memory`) when enabled.

- **agent_loop.runner — shared tool-use loop** — extracted the agent
  loop from `claude_hooks.caliber_proxy.server.run_agent_loop` into a
  reusable `claude_hooks.agent_loop.runner.run_loop` function with a
  `LoopConfig` dataclass. Both the caliber grounding proxy and the new
  `/get-advice` advisor drive their conversations through this single
  loop, so every gemma4-era quirk (force-first-tool-call,
  force-answer-after, tool-call burst dedup + cap, preseed survey)
  benefits both consumers consistently. The runner is transport-
  agnostic: callers pass their own `chat_fn` and `tool_executor`.
  `caliber_proxy/server.py:run_agent_loop` is now a thin shim that
  reads env vars, builds the `LoopConfig`, prepends grounding, calls
  the runner, and applies the caliber-specific
  `sanitize_assistant_json` post-processor. Behavior unchanged — the
  full caliber-proxy test suite (91 tests across `TestAgentLoop` /
  `TestPreseedSurvey` / etc.) passes against the refactored path.
  v1.1 added optional `on_iter` / `on_tool` callbacks so consumers
  (notably the consultants `MessageRecorder`) can observe every
  chat round and tool execution without sub-classing the runner.

- **stop_guard: stall-after-commitment check** — catches a new failure
  mode observed on `claude-opus-4-7` (1M context): the model writes a
  paragraph ending with an action-commitment phrase ("Diving in now",
  "Writing the script now", "On it.") and then ends the turn WITHOUT
  calling any tool. The user has to nudge the session to unstall it.
  Three independent conditions stack so false-positive risk is low:
  (1) `stop_reason=end_turn`, (2) zero `tool_use` blocks in the
  message content, (3) one of the commitment phrases appears in the
  last ~250 chars of the message text. The Stop hook returns
  `decision=block` with a correction asking the model to either
  execute the action it described or ask a specific question. Honours
  the same user-wrap-up bypass as the prose-pattern guard so an
  "All done. On it." closing after the user said "wrap up" doesn't
  trigger. Default on when stop_guard itself is enabled; opt out via
  `hooks.stop_guard.stall_check_enabled = false`. New module entry
  points: `claude_hooks.stop_guard.check_stall_after_commitment`,
  `COMMITMENT_PATTERNS`, `STALL_CORRECTION`. 15 unit tests in
  `tests/test_stop_guard.py::StallAfterCommitmentTests` plus an
  end-to-end smoke through `_run_stop_guard`.
- **pgvector backup-validity canary** — new
  `claude-hooks-pgvector-backup-check.{service,timer}` runs every
  Monday at 02:43 local and walks each retention tier
  (daily/weekly/monthly), validating the most recent dump in two
  layers: (1) `pg_restore -l` for the TOC + metadata, (2)
  `pg_restore -f /dev/null` for a full byte-read of the archive
  (catches mid-file corruption that the TOC scan misses). Both
  layers run inside the `mcp-pgvector` container so the
  pg_restore version always matches whatever wrote the dump.
  Exits non-zero on any failure → wireable into `OnFailure=`.
  New script: `scripts/pgvector_backup_check.sh`.
- **pgvector daily backup timer** — new
  `claude-hooks-pgvector-backup.{service,timer}` runs
  `pg_dump -Fc` inside the `mcp-pgvector` container at 01:17 local
  every day and writes to `/shared/config/mcp-pgvector/backups/`
  with three retention tiers: 7 daily, 4 weekly (promoted on
  Sunday by hardlink), 3 monthly (promoted on day 1 by hardlink).
  `pg_dump` takes only `ACCESS SHARE` locks so reads + writes are
  not blocked during the backup. New scripts:
  `scripts/pgvector_backup.sh` (the worker) and
  `scripts/pgvector_restore.sh` (interactive restore helper with
  `latest_daily` / `latest_weekly` / `latest_monthly` shortcuts).
  Wired into `install.py` — installed when `providers.pgvector.enabled`
  is true. Tunables: `CONTAINER`, `PG_USER`, `PG_DB`, `BACKUP_DIR`,
  `KEEP_DAILY`, `KEEP_WEEKLY`, `KEEP_MONTHLY`, `WEEKLY_DOW`.

### Fixed

- **/consultants xmax — critic-fanout 6× cost overshoot** — the
  Phase 10 multi-critic dispatcher wired its conditional fan-out
  edge directly to `researcher`, which is itself Send-multiplexed
  by the Phase 9 researcher fan-out (N×M parallel invocations at
  x-tiers). LangGraph's `add_conditional_edges` from a
  Send-multiplexed source fires PER UPSTREAM SEND INVOCATION,
  not per-barrier-merge — so 6 researcher lanes spawned 6 ×
  C critic invocations instead of C. Caught on the first live
  xmax smoke (`csl-2026-05-07-1707-2a8f`): 18 critic LLM calls
  against an intended 3. Correctness wasn't affected — every
  critic still saw the same merged research and meta-critic
  consolidated correctly — but token cost was 6× the design.
  Fix inserts a single-invocation pass-through `research_barrier`
  node between researcher and the critic-fanout dispatcher.
  Unconditional edges from Send-multiplexed sources DO barrier-
  merge (this is how the legacy single-critic edge always worked),
  so routing through the barrier node forces the conditional
  fan-out to fire exactly once. Same question post-fix: 3 critic
  invocations, wall time 374s → 174s (54% faster). Test pins the
  count invariant: regardless of how many researcher lanes fan
  out, the critic fires exactly `1 + len(extra_models)` times.

- **axon-host crash-loop after host restart** — two compounding
  issues that put the unit into a 5s `Restart=on-failure` loop
  forever. (1) `/root/.axon` had been deleted between installs;
  the unit's `ReadWritePaths=/root/.axon` directive failed
  systemd's namespace bind-mount with `status=226/NAMESPACE`
  ("Failed to set up mount namespacing"). (2) The `claude-hooks`
  conda env had drifted — `uvicorn`, `httpx-sse`,
  `pydantic-settings`, and `sse-starlette` were silently dropped
  (likely from a partial reinstall during another env's build),
  and once the namespace bug was fixed axon crashed on import
  with `ModuleNotFoundError: No module named 'uvicorn'`. The
  loop just moved one step deeper. install.py now does two
  pre-flight checks before enabling the unit: `_ensure_axon_
  registry_dir` mkdir's `~/.axon/repos/` so the bind-mount has a
  target, and `_ensure_axon_deps` probes the env's import
  surface and pip-installs `requirements-axon.txt` (new file
  pinning the runtime deps) when anything is missing. Refuses to
  enable the unit when either pre-flight fails, so future drift
  becomes a clear `install.py` re-run rather than a silent
  service-loop.

- **PreCompact: stop emitting hookSpecificOutput** — Claude Code's
  PreCompact event schema does NOT accept `hookSpecificOutput`
  (only the universal `continue` / `stopReason` / `suppressOutput`
  envelope). Returning the wrap-up markdown as
  `hookSpecificOutput.additionalContext` failed CC's JSON validator
  with `(root): Invalid input` — the disk write succeeded but the
  hook was reported as failed every time the user resumed a session
  that had auto-compacted. The wrap-up file on disk is the sole
  delivery channel; `wrapup_recovery` already surfaces the pointer
  on the next post-compaction `UserPromptSubmit`, so dropping the
  inline context loses nothing. Handler now returns `None` on
  success. Existing `test_pre_compact.py` updated to pin the new
  contract.

### Added

- **/get-advice — LLM-to-LLM advisor skill** — Claude Code can now consult
  a configured Ollama model (default `qwen3.5:cloud`) for a multi-turn
  second opinion via the `/get-advice <query>` skill. Three helper
  skills (`/get-advice--model`, `/get-advice--effort`,
  `/get-advice--tools`) configure model + ctx, effort tier (low=1
  session / medium=3 / high=5 / max=25), and the per-tool gate
  (CSV / `all` / `none`) without editing JSON. Settings persist to
  `~/.claude/get-advice-config.json`. New CLI `bin/claude-advisor`
  drives the conversation: `turn`, `reset`, `cleanup`, get/set
  subcommands. Per-turn JSON exposes `prompt_eval_count` /
  `eval_count` so Claude knows when to summarize and reset before the
  advisor's context fills (default threshold 85%). Reuses caliber-proxy
  grounding (project anchors + structure map) and the same six tools
  (`read_file`, `grep`, `glob`, `list_files`, `survey_project`,
  `recall_memory`) when enabled.

- **agent_loop.runner — shared tool-use loop** — extracted the agent
  loop from `claude_hooks.caliber_proxy.server.run_agent_loop` into a
  reusable `claude_hooks.agent_loop.runner.run_loop` function with a
  `LoopConfig` dataclass. Both the caliber grounding proxy and the new
  `/get-advice` advisor drive their conversations through this single
  loop, so every gemma4-era quirk (force-first-tool-call,
  force-answer-after, tool-call burst dedup + cap, preseed survey)
  benefits both consumers consistently. The runner is transport-
  agnostic: callers pass their own `chat_fn` and `tool_executor`.
  `caliber_proxy/server.py:run_agent_loop` is now a thin shim that
  reads env vars, builds the `LoopConfig`, prepends grounding, calls
  the runner, and applies the caliber-specific
  `sanitize_assistant_json` post-processor. Behavior unchanged — the
  full caliber-proxy test suite (91 tests across `TestAgentLoop` /
  `TestPreseedSurvey` / etc.) passes against the refactored path.

- **stop_guard: stall-after-commitment check** — catches a new failure
  mode observed on `claude-opus-4-7` (1M context): the model writes a
  paragraph ending with an action-commitment phrase ("Diving in now",
  "Writing the script now", "On it.") and then ends the turn WITHOUT
  calling any tool. The user has to nudge the session to unstall it.
  Three independent conditions stack so false-positive risk is low:
  (1) `stop_reason=end_turn`, (2) zero `tool_use` blocks in the
  message content, (3) one of the commitment phrases appears in the
  last ~250 chars of the message text. The Stop hook returns
  `decision=block` with a correction asking the model to either
  execute the action it described or ask a specific question. Honours
  the same user-wrap-up bypass as the prose-pattern guard so an
  "All done. On it." closing after the user said "wrap up" doesn't
  trigger. Default on when stop_guard itself is enabled; opt out via
  `hooks.stop_guard.stall_check_enabled = false`. New module entry
  points: `claude_hooks.stop_guard.check_stall_after_commitment`,
  `COMMITMENT_PATTERNS`, `STALL_CORRECTION`. 15 unit tests in
  `tests/test_stop_guard.py::StallAfterCommitmentTests` plus an
  end-to-end smoke through `_run_stop_guard`.
- **pgvector backup-validity canary** — new
  `claude-hooks-pgvector-backup-check.{service,timer}` runs every
  Monday at 02:43 local and walks each retention tier
  (daily/weekly/monthly), validating the most recent dump in two
  layers: (1) `pg_restore -l` for the TOC + metadata, (2)
  `pg_restore -f /dev/null` for a full byte-read of the archive
  (catches mid-file corruption that the TOC scan misses). Both
  layers run inside the `mcp-pgvector` container so the
  pg_restore version always matches whatever wrote the dump.
  Exits non-zero on any failure → wireable into `OnFailure=`.
  New script: `scripts/pgvector_backup_check.sh`.
- **pgvector daily backup timer** — new
  `claude-hooks-pgvector-backup.{service,timer}` runs
  `pg_dump -Fc` inside the `mcp-pgvector` container at 01:17 local
  every day and writes to `/shared/config/mcp-pgvector/backups/`
  with three retention tiers: 7 daily, 4 weekly (promoted on
  Sunday by hardlink), 3 monthly (promoted on day 1 by hardlink).
  `pg_dump` takes only `ACCESS SHARE` locks so reads + writes are
  not blocked during the backup. New scripts:
  `scripts/pgvector_backup.sh` (the worker) and
  `scripts/pgvector_restore.sh` (interactive restore helper with
  `latest_daily` / `latest_weekly` / `latest_monthly` shortcuts).
  Wired into `install.py` — installed when `providers.pgvector.enabled`
  is true. Tunables: `CONTAINER`, `PG_USER`, `PG_DB`, `BACKUP_DIR`,
  `KEEP_DAILY`, `KEEP_WEEKLY`, `KEEP_MONTHLY`, `WEEKLY_DOW`.

### Late additions (post-2026-05-07 cut-prep work, landed 2026-05-08)

The 2026-05-07 batch above was complete but uncut — pyproject.toml
was bumped to 1.1.0 with a "prep for tag, not yet cut" commit
(`f984d73`). The day before the actual cut produced four more sets
of changes that landed under the same MINOR version because they're
all extensions of the v1.1 work above (cloud-flap recovery for the
new `/consultants` engine, install.py glue so the new skill CLIs
resolve on every platform, and a documentation pass for the v1.1
surface).

#### Added (2026-05-08)

- **/consultants — synthesizer fallback chain on persistent
  failure** — when the primary synthesizer model exhausts its
  ChatClient retry budget on a cloud flap (HTTP 500 / 502 / 503 /
  504 / 408 / 429), the engine now walks `synthesizer.extra_models`
  in order before declaring the consultation failed. Same
  `chat_client` (so the same proxy + connection pool); only the
  `model` field of the payload changes per attempt. First success
  wins. Each attempt records an `llm_call` event with the actual
  model used, so post-hoc audit via `/consultants--show <sid> --raw`
  reveals which model produced the final answer. Configure with
  `claude-consultants config set-role synthesizer --add-model
  <tag>`. Active at every effort tier (not gated by the x-prefix —
  cloud flaps don't care about effort).

- **/consultants — degraded-answer composer on synthesizer
  failure** — when every model in the fallback chain fails, the
  council now writes a `summary.md` whose `final_answer` field
  surfaces the researcher's full reports + the critic's verdict
  rather than `(consultation incomplete: synthesizer error: ...)`.
  Researcher reports often run 3-5k tokens of analysis at xhigh
  effort, and the critic verdict adds another 1k of structured
  decision text — that's the most expensive work in a consultation
  and now survives the synthesizer's failure to the user. The
  banner explains it's a degraded answer (not a synthesized one)
  and points the user at `claude-consultants follow-up <THIS_SID>
  --message "compose a final answer..."` to recover cheaply (the
  next synthesizer attempt inherits research + critic warm and
  costs one more call, not a full re-run).

- **/consultants--followup — failed-session-aware parent picker**
  — the skill now defaults to the most recent session of *any*
  status (was: most recent `completed` only). When the most recent
  is `failed`, AskUserQuestion offers two paths: (1) chain off the
  failed sid (cheapest — researcher + critic threads inherit from
  disk and only the synthesizer re-runs) or (2) chain off the
  failed sid's `parent_sid` (start over from a known-good thread).
  Pairs with the engine-side fallback chain + degraded answer above
  to make recovery from a cloud flap a one-step user action.

- **/consultants--followup — dedicated sub-skill** — the new fifth
  member of the `/consultants` skill family, exposes
  `claude-consultants follow-up` directly. Previously only
  reachable via the underlying CLI or by asking Claude to dispatch
  it manually; now `/consultants--followup [<sid>] <question>` is
  a first-class skill with its own SKILL.md + activation guard +
  failed-session handling.

- **Cloud-model evaluation suite — full grading pass** — every
  label in the 2026-05-07 sweep now carries Claude-graded per-query
  + per-role grades + a verdict (PROD-READY / EVALUATED-ONLY) per
  the [`docs/benchmarks/EVALUATION.md`](docs/benchmarks/EVALUATION.md)
  rubric. Three labels are PROD-READY at single-run with
  `P:A R:A C:A S:A`: `kimi-k2.6-cloud`, `gemma4-31b-cloud`,
  `glm-5-1-cloud`. Three are EVALUATED-ONLY usable in mixes for
  specific roles where the per-role grade is A:
  `minimax-m2-7-cloud` (strong critic), `qwen3-5-397b-cloud`
  (strong planner + critic), `qwen3-5-cloud` (cheap sibling).
  Headline matrix lives at the top of [`docs/benchmarks/index.md`](docs/benchmarks/index.md).
  EVALUATION.md §3.5 was updated to clarify the grader is Claude
  reading transcripts, not the human (the original "grader is the
  human" wording contradicted the LLM-to-LLM workflow).

- **User-facing v1.1 documentation pass** — three new top-level
  user runbooks landed: [`docs/get-advice.md`](docs/get-advice.md)
  (351 lines: when to use, prereqs, the four sub-skills, model
  picking, effort tiers, tools, common workflows, troubleshooting),
  [`docs/consultants.md`](docs/consultants.md) (639 lines: the
  four-role council, x-tier multi-model fan-out semantics, service
  modes, follow-ups + chaining + failed-session recovery, the
  three-layer cloud-flap recovery story, configuration via
  `/consultants--config`, picking models with explicit benchmark
  links, troubleshooting), and [`docs/whats-new.md`](docs/whats-new.md)
  (276 lines: human-readable v1.1 highlights with the full benchmark
  verdict matrix). README.md grew from 8 to 16 slash-command rows
  with a new "Since" column flagging v1.1 additions, and a CLI
  block per skill family. Install section grew from 6 to 8
  numbered steps to cover the new bin/* PATH wrappers and the
  opt-in /consultants conda env.

#### Changed (2026-05-08)

- **ChatClient retry budget bumped from 8 attempts / ~136 s to 15
  attempts / ~905 s (~15 min)** — `DEFAULT_MAX_RETRIES` 8 → 15 and
  `DEFAULT_RETRY_MAX_DELAY_S` 30 → 90 in
  `claude_hooks/get_advice/chat_client.py`. The motivating session
  (`csl-2026-05-07-2158-7c75`, xhigh effort) burned the whole
  pre-bump budget on a 2+ minute Ollama Cloud 500 window and lost
  the synthesizer outright; with the new budget a flap of that
  shape is absorbed by the retry loop and the consultation
  completes. A 5-15 minute consultation can now tolerate up to ~15
  minutes of cloud unavailability without failing — the trade-off
  being that an actual permanent outage takes longer to surface as
  a user-visible error. Affects both `/consultants` (synthesizer
  + every other role's ChatClient) and `/get-advice` (the advisor
  itself). Override via `ChatClient(..., max_retries=N,
  retry_max_delay_s=S)` per call site if a cheaper budget is
  desirable.

#### Fixed (2026-05-08)

- **install.py — bin/* shim PATH wrappers (cross-platform)** —
  skill CLIs (`claude-consultants`, `claude-advisor`, …) invoked by
  bare name from a `/consultants--config` or `/get-advice` skill
  failed with `command not found` because Claude Code's bash
  subprocess does not include the repo's `bin/` on PATH on any
  platform. Symlinks don't fix it either: the shims resolve `REPO`
  via `dirname "$0"`, which through a symlink points at the symlink
  dir (e.g. `/usr/local/bin/..`) and the helper sourcing breaks.
  Installer now drops thin exec-wrappers in a known PATH-friendly
  location for all 11 shims (`claude-hook`, `claude-consultants`,
  `claude-advisor`, `claude-hooks-daemon`, `claude-hooks-daemon-ctl`,
  `claude-hooks-proxy`, `claude-hooks-dashboard`,
  `claude-hooks-rollup`, `caliber-grounding-proxy`, `caliber-smart`,
  `claude-hook-pgvector-mcp`):
  - **POSIX (Linux + macOS)**: `~/.local/bin/<shim>` — POSIX sh
    wrapper that `exec`s the absolute repo path.
  - **Windows**: `%LOCALAPPDATA%\claude-hooks\bin\<shim>` (POSIX sh
    for the MSYS bash that Claude Code uses) plus a `.cmd` sibling
    for native cmd / PowerShell users.
  Wrappers carry an install-time tag in their first comment line,
  so `python install.py` is fully idempotent and won't clobber a
  hand-rolled wrapper. `python install.py --uninstall` removes only
  tagged wrappers. Same root cause as the 2026-05-02 ruff PATH fix
  — once the wrappers land, every bare-name invocation from a skill
  resolves on every platform.

- **install.py — Windows User PATH auto-prepend via `reg add`** —
  for `/consultants` and `/get-advice` skills to actually resolve
  on Windows the wrapper directory needs to be on User PATH that
  Claude Code's bash subprocess inherits. Installer now prepends
  `%LOCALAPPDATA%\claude-hooks\bin` to `HKCU\Environment\PATH`
  using `reg add` (NOT `setx` — `setx` silently truncates User
  PATH to 1024 chars, which is destructive on any developer
  machine), then broadcasts `WM_SETTINGCHANGE` so new processes
  pick it up without a logoff. Defensive 16 KB ceiling on the
  resulting PATH.

## [1.0.3] — 2026-05-03

Continuation of the v1.0.2 soak: the PreCompact wrap-up surfaced two
gaps in the field (lost connection state after auto-compaction; the
post-compaction model didn't pick up the saved state file), plus a
token-cost regression from the always-on `## Now` block + wrap-up
recovery pointer. PATCH bump per the project precedent for opt-in
additions and perf fixes.

### Changed

- **Now-block + wrap-up recovery: ~90% token reduction.** Both blocks
  were stacking on every `UserPromptSubmit` (now-block ~59 tok, recovery
  pointer ~103 tok), and `additionalContext` from prior turns stays in
  the conversation history forever — so the cost compounded across the
  session (≈14k tokens after 85 turns). Two cuts:
  1. Now-block dropped its inline anchor reminder (the rule lives in
     the user's feedback memory, no need to repeat it every turn) —
     59 → ~16 tok/turn.
  2. Recovery pointer is now one-shot per wrap-up file via a `.seen`
     sidecar marker written next to the file on first surfacing —
     103 tok × every turn for 24h → ~26 tok exactly once. Also shorter:
     just heading + path instead of the full rationale.
  Combined: ~163 tok/turn (compounding) → 16 tok/turn ongoing + 26
  once. Knobs unchanged.

### Added

- **Wrap-up recovery + endpoint extraction** — two-part fix for the
  failure mode the user hit on the backup_models pod after auto-
  compaction (lost the training pod ID/IP and didn't read the saved
  state summary file):
  1. `claude_hooks/wrapup_synth.collect_endpoints()` now sweeps both
     transcript text blocks AND bash commands for URLs, IPv4/IPv6
     addresses, and pod-style hostnames (RunPod / Modal / Vast.ai /
     Lambda Labs / Paperspace). The previous synth only looked at
     `ssh` bash commands, which missed RunPod proxy URLs and any IP
     mentioned only in prose. Section 7 of the synthesised wrap-up
     is now "Connection state (re-attach targets)" with separate
     subsections for pod hostnames, ssh targets, URLs, and IPs.
  2. New `claude_hooks/wrapup_recovery.py` scans the three known
     wrap-up output dirs (`<cwd>/.wolf/`, `<cwd>/docs/wrapup/`,
     `~/.claude/wrapup-pre-compact/`) on every `UserPromptSubmit`
     and prepends a short pointer block to `additionalContext` if a
     pre-compact summary was modified within the last 24h. Survives
     the compaction boundary even when the inline context gets
     trimmed. Knobs: `hooks.wrapup_recovery.enabled` (default true),
     `hooks.wrapup_recovery.max_age_seconds` (default 86400). 16
     unit tests in `tests/test_wrapup_recovery.py`.
- **`## Now` block injection** — every `UserPromptSubmit` and
  `SessionStart` now prepends a one-line markdown block with the
  current local-TZ timestamp, IANA zone, UTC offset, and weekday.
  Reason: the assistant has no internal clock, and most of our
  internal code uses `datetime.now(timezone.utc)` (correct for
  storage but UTC leaks into user-facing output); ETAs and
  scheduled-trigger times also drifted because the model anchored
  on stale timestamps from earlier tool output. The injected line
  becomes the authoritative "now" for the turn. ~30 tokens per
  surface. New module `claude_hooks/now_block.py`; config knobs
  `system.now_block.enabled` (default true) and
  `system.now_block.timezone` (default null = host
  `/etc/localtime`). 16 unit tests in `tests/test_now_block.py`
  plus integration coverage in `tests/test_handlers.py` and
  `tests/test_dispatcher.py`.

## [1.0.2] — 2026-05-02

Soak release for the PreCompact wrap-up synth + the operational
fixes that surfaced while exercising it on solidpc and pandorum.
Per the precedent set in v1.0.1, the bump stays PATCH for low-risk
opt-in additions plus stability fixes.

### Added

- **PreCompact hook → wrap-up synthesiser** — new
  `claude_hooks/hooks/pre_compact.py` handler fires before Claude
  Code auto-compacts the conversation. Reads the session transcript,
  produces a deterministic eight-section `/wrapup`-shaped summary
  (mechanically-extractable parts filled in; model-judgment parts
  marked as `needs model`), persists it to disk (preferring `.wolf/`
  → `docs/wrapup/` → `~/.claude/wrapup-pre-compact/`), and emits the
  markdown as `additionalContext` so it lands inside the compaction
  window. Self-gates on (1) `hooks.pre_compact.enabled` (default
  true) and (2) the `/wrapup` skill being installed at
  `~/.claude/skills/wrapup/SKILL.md`. 17 unit tests in
  `tests/test_pre_compact.py`.
- **`/wrapup` skill: last-line file pointer** — the skill now always
  saves a copy to disk and ends its output with the exact pointer
  `**State summary saved to:** <abs-path> — Read this file to
  recover full session state.` Auto-compaction sometimes drops the
  inline output before the next session can read it; the file on
  disk is the only fully reliable carrier across the boundary, and
  the last-line position maximises the odds the post-compaction
  assistant sees the path. Edit applied to the canonical
  `.claude/skills/wrapup/SKILL.md` in the repo (deployed via
  `install.py`).

### Changed

- **Dispatcher table** — `PreCompact` event now routes to the new
  `pre_compact` handler.
- **`install.py`** — new `PRE_COMPACT_TEMPLATE` wires the hook into
  `~/.claude/settings.json`; `install_hooks()` gains
  `include_pre_compact` (defaults true).

### Fixed

- **Daemon stdout race in concurrent dispatches** — the daemon's
  `_run_handler` redirected the process-global `sys.stdout` to a
  per-call StringIO buffer and ran `dispatch()` to capture output.
  Because the daemon is multi-threaded (`ThreadingTCPServer`), two
  concurrent hook calls clobbered each other's redirects — one
  thread's handler output landed in the other thread's buffer.
  Symptom on the user side: a `Stop` hook receiving a
  UserPromptSubmit recall payload (`hookEventName: "UserPromptSubmit"`),
  rejected by Claude Code with "Hook returned incorrect event name:
  expected 'Stop' but got 'UserPromptSubmit'". Refactored
  `dispatcher.py` to expose `dispatch_capture(event, payload) -> dict`
  that returns the handler output directly without touching
  `sys.stdout`; the daemon now calls that. The legacy
  `dispatch(event, payload)` (stdout-write) is retained for the
  inline `run.py` single-process path. Two new regression tests in
  `tests/test_dispatcher.py` (`TestDispatchCaptureThreadSafety`)
  pin the contract — one asserts `dispatch_capture` never touches
  `sys.stdout`, the other runs UserPromptSubmit + Stop concurrently
  20× and asserts neither thread receives the other's payload.
- **Ollama `num_ctx` for gemma4 callers** — HyDE (`hyde.py`),
  `/reflect` (`reflect.py`), and `/consolidate` (`consolidate.py`)
  all use `gemma4:e2b` but none set `num_ctx` in the request body.
  Ollama keeps the FIRST loader's `num_ctx` sticky for the
  duration the model stays resident, so on a cold load the model
  inherited the 4k Modelfile default — and a different caller
  passing a different value would force a full reload + KV-cache
  rebuild. All three callers now pass `num_ctx=16384` (matching
  the pgvector embedder's existing 16k pin), with config knobs
  `user_prompt_submit.hyde_num_ctx`, `reflect.num_ctx`, and
  `consolidate.num_ctx` for overrides. Set them in lockstep —
  mismatched values across the three thrash the resident model.

## [1.0.1] — 2026-05-01

> Note on the version bump: by the SemVer rules in
> `docs/RELEASING.md`, "new opt-in subsystem" is normally a **MINOR**
> bump. v1.0.1 was chosen here as a deliberate exercise of the
> release workflow on a small, low-risk delta — treat this as
> precedent for "first follow-up release after the 1.0 cut," not
> as a recategorization of the SemVer rules.

### Added

- **Self-update check** — opt-in periodic poll of GitHub
  `releases/latest`. The daemon thread runs the check at most once
  every 24 hours (configurable). The Stop hook surfaces a
  `[claude-hooks] update available: vX.Y.Z` notice in its
  `systemMessage` when a newer tag is published.
  - Runs on the long-lived `claude-hooks-daemon` thread so the
    Stop hook never blocks on network I/O.
  - Failed checks retry up to 5 times at 5-minute intervals, then
    defer to the next 24-hour window.
  - Notification budget: the notice surfaces at most 10 times per
    discovered release before going silent until the next check
    finds a newer tag.
  - Silent on failure: timeouts, DNS errors, and HTTP errors all
    resolve to "no update" without raising or logging at info level.
  - Disable at runtime by setting `update_check.enabled` to `false`
    in `config/claude-hooks.json` — both the daemon poll and the
    Stop-hook notice stop immediately, no restart needed.
  - State persists in `~/.claude/claude-hooks-update-state.json`.
  - 35 unit tests in `tests/test_update_check.py`.
- **`install.py` self-update prompt** — installer asks
  "Do you want to automatically check every 24 hours for a new
  release?" and persists the answer to `update_check.enabled`.
  Warns when the daemon is disabled (the feature requires it).

### Fixed

- `claude_hooks/__init__.py` `__version__` was stale at `0.4.0`;
  bumped to match the package release (1.0.1).

## [1.0.0] — 2026-05-01

First tagged release. Consolidates all work prior to the move to a
proper branch + release workflow. The codebase has been operating in
production on solidpc and pandorum for months; v1.0.0 is the formal
cut, not a feature break.

### Highlights

- **Memory recall + storage** — deterministic `UserPromptSubmit`
  recall and `Stop` storage across pluggable providers (Qdrant,
  Memory KG, pgvector, sqlite-vec).
- **HyDE-expanded recall** — local Ollama (`gemma4:e2b` primary,
  `gemma4:e4b` fallback) generates hypothetical-document queries with
  on-disk caching.
- **Tier 1.3 detached store** — fork-and-return so the `Stop` hook
  doesn't block on provider writes.
- **Tier 3.8 daemon stack** — single long-lived Python process owns
  providers + config; each hook answers in milliseconds.
- **Transparent api.anthropic.com proxy** — opt-in HTTP proxy with
  SSE tail, rate-limit state file, retry-on-5xx, and SQLite
  rollups (schema v5).
- **Read-only stats dashboard** (port 38081) — JSON API + embedded
  HTML view; per-effort × per-day stop-phrase canary panel
  (stellaraccident #42796).
- **Stop-phrase canary** — in-stream scanner with 8 behavior
  categories from `config/stop_phrases.yaml`; daily health line via
  `claude-hooks-health.timer`.
- **In-process AST code-graph** — Python stdlib `ast`-driven by
  default; optional tree-sitter, Louvain clustering, and an MCP
  server for cross-tool integration.
- **Session-scoped LSP engine** — per-project daemon, Windows IPC
  parity (UNIX socket + named pipes), session-affinity locks,
  adaptive preload from the code-graph hot set, and opt-in
  compile-aware diagnostics merging `cargo check` / `tsc --noEmit`
  / `mypy` / `go vet` on top of the LSP layer.
- **PostToolUse ruff hook** — IDE-style diagnostics surfaced as
  `additionalContext` after Edit/Write/MultiEdit on Python files.
- **Caliber grounding proxy** — native-tools agent loop,
  `survey_project`, recall integration; full multi-harness skill
  mirroring across `.claude/`, `.agents/`, `.cursor/`.
- **Companion integrations** — OpenWolf (`.wolf/cerebrum.md`,
  `buglog.json`), axon, gitnexus, claudemem-reindex.
- **Cross-platform installer** — Linux, macOS, Windows; idempotent;
  preserves `_managedBy`-tagged hook entries on re-run.
- **System-wide `pgvector-mcp`** — stdio MCP server exposing pgvector
  recall + KG ops to any MCP-aware client.
- **Operator tooling** — `proxy_health_oneliner.py`, weekly token
  usage report, statusline segment, bench harnesses for recall and
  the LSP engine.

### Subsystem milestones (internal versioning, pre-1.0)

| Internal tag | Capability                                                                  |
|--------------|------------------------------------------------------------------------------|
| v0.2         | Recall pipeline (HyDE, decay, dedup), instincts, reflect, consolidate       |
| v0.4         | Pgvector + sqlite-vec providers, Caliber proxy, daemon stack                |
| v0.5         | Transparent API proxy, SQLite rollups, dashboard, stop-phrase canary        |
| v0.6         | In-process AST code-graph, MCP server, optional clustering                  |
| v0.7         | LSP engine (Phases 0-4), Windows IPC parity, compile-aware diagnostics      |
| **v1.0.0**   | Formal release cut + CHANGELOG + dev-branch workflow                         |

### Test coverage

~1.5k tests in `tests/` (run
`/root/anaconda3/envs/claude-hooks/bin/python -m pytest tests/ -q`).
Run `pytest --collect-only -q | tail -1` for the current count.

### Known issues at release

- Caliber 1.45.2 has a hook-recursion bug on `init`; use 1.45.3+ or
  see `memory/reference_caliber_timeouts.md`.
- Claude Code at `/effort xhigh` exhibits elevated
  ownership-dodging (~29/1k vs medium's ~2/1k) per the proxy canary;
  upstream issue [anthropics/claude-code#55301](https://github.com/anthropics/claude-code/issues/55301).
  Recommend `/effort medium` until upstream resolves.

### Upgrade notes

This is the first tagged release; there is no upgrade path from a
prior tag. From any unreleased checkout, just `git pull` on `main`
once `v1.0.0` is published. The on-disk config schema
(`config/claude-hooks.json` version 2) is unchanged from late-v0.7.

[Unreleased]: https://github.com/mann1x/claude-hooks/compare/v1.0.3...HEAD
[1.0.3]: https://github.com/mann1x/claude-hooks/compare/v1.0.2...v1.0.3
[1.0.2]: https://github.com/mann1x/claude-hooks/compare/v1.0.1...v1.0.2
[1.0.1]: https://github.com/mann1x/claude-hooks/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/mann1x/claude-hooks/releases/tag/v1.0.0
