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
