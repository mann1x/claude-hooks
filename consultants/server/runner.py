"""Production council runner — bridges SessionState to LangGraph.

Lazy-imports the engine so this module can sit on disk without
requiring LangChain in the test env. Production code calls
``make_runner()`` to get a ``run_council`` callable suitable for
``create_app(run_council=...)``.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Optional

from consultants import config as cc
from consultants.engine import council as council_mod
from consultants.engine import polykv as polykv_mod
from consultants.engine import storage
from consultants.engine.run_control import RunSuspended

log = logging.getLogger("consultants.server.runner")


def make_runner(*, ollama_base_url: str):
    """Return a ``run_council(state, runner_input)`` callable.

    The runner builds a LangGraph at call time using the config in
    ``runner_input`` and the per-role models. Each role gets its own
    ChatClient for clean usage tracking.
    """
    # Lazy imports — none of these are present in the main test env.
    from claude_hooks.allowed_roots import (
        render_for_log,
    )
    from claude_hooks.get_advice.chat_client import make_agent_chat_client
    from claude_hooks.caliber_proxy.prompt import build_grounding_messages
    from consultants.server.shell_enable import answer_with_shell
    from consultants.server.tool_surface import build_tool_surface
    from consultants.engine.graph import (
        TOOLABLE_ROLES, GraphDeps, build_council_graph,
    )
    from consultants.engine.trace import (
        Tracer, TracedChat, traced_tool,
    )
    # #214: Default to an in-memory checkpointer so the M9 control
    # surface (state / cancel / inject / pause / resume) works in
    # the standard config. LangGraph's get_state / update_state
    # require a checkpointer; without one, every M9 endpoint that
    # calls those raises ValueError("No checkpointer set") and 500s.
    # The cost is one in-process write per superstep — bounded for
    # a council with ~10 supersteps per run. Persistence isn't a
    # requirement here: the recorder's transcript.db is the durable
    # audit log; the checkpointer is for live introspection only.
    try:
        from langgraph.checkpoint.memory import MemorySaver
    except ImportError:  # pragma: no cover — minimal langgraph
        try:
            from langgraph.checkpoint.memory import (
                InMemorySaver as MemorySaver,  # type: ignore[assignment]
            )
        except ImportError:
            MemorySaver = None  # type: ignore[assignment]

    def run_council(state, runner_input: dict) -> None:
        cfg: cc.ConsultantsConfig = runner_input["config"]
        cwd: str = runner_input["cwd"]
        question: str = runner_input["question"]
        # v1.8+: extra allowed roots for the tool sandbox. Sourced from
        # the CLI's ``--add-dir`` (already merged with settings-file
        # auto-discovery by the HTTP layer). Empty tuple → tool layer
        # falls back to the bare ``execute`` fast path.
        extra_roots = tuple(runner_input.get("extra_roots") or ())
        # Display forms (pre-realpath, e.g. ``/shared/dev/x``) for the
        # log line and for metadata.json. Resolved here rather than
        # inside the ``if extra_roots:`` log block below so the
        # artifact writer can read them unconditionally.
        cwd_display: str = runner_input.get("cwd_display") or cwd
        extra_roots_display = tuple(
            runner_input.get("extra_roots_display") or extra_roots
        )
        if len(extra_roots_display) != len(extra_roots):
            extra_roots_display = extra_roots

        # Pre-flight, before a single token is spent: can this run
        # actually read the files the question is about? A council
        # that can't doesn't fail — it answers confidently from
        # nothing and the only tell is a wall of "[unverified — file
        # not found]" at the end, 55 minutes later. See
        # :mod:`consultants.engine.preflight` for why "none of them
        # resolve" is the blocking condition and "some" only warns.
        if _preflight_refused(
            state, question, cwd=cwd, extra_roots=extra_roots,
            cwd_display=cwd_display,
            extra_roots_display=extra_roots_display,
            label="council",
            skip=bool(runner_input.get("skip_preflight")
                      or runner_input.get("resume")),
        ):
            return

        enabled = tuple(cc.enabled_roles(cfg))

        # Effort-based critic strategy:
        # - low: drop critic AND drop synthesizer self-critic. Bare
        #   pipeline planner -> researcher -> decisive synthesizer.
        #   Cheapest, fastest, no critique step.
        # - medium: drop critic AND drop synthesizer self-critic.
        #   Same as low but with the medium iter caps + fan-out.
        #   The 2026-05-07 audit v5 trace showed self-critic adding
        #   ~3.5 min on the synthesizer node for diminishing returns.
        # - high|max: keep dedicated critic; standard synthesizer.
        #   Quality-first; user explicitly opted in to the cost.
        # Critic gating mirrors the BASE tier: low/medium (and their
        # x-prefixed counterparts xmedium) drop the critic; high /
        # max / xhigh / xmax keep it. cc.base_effort strips the
        # x-prefix so we make one decision per base tier and let
        # x-tiers inherit.
        # xauto is the exception: it starts on the medium base, but
        # its escalator is wired only when a critic is (graph.py
        # ``use_escalator``) and three of its four signals are the
        # critic's. Dropping the critic there made xauto a plain
        # xmedium run that could never grow.
        if cc.base_effort(cfg.effort) in ("low", "medium") \
                and cfg.effort != "xauto" \
                and "critic" in enabled:
            enabled = tuple(r for r in enabled if r != "critic")
            log.info(
                "effort=%s: dropping critic node (cost > value at "
                "this tier)", cfg.effort,
            )
        # Self-critic synthesizer is now off by default at every
        # tier. The infrastructure stays (SYNTHESIZER_SELF_CRITIC_SYSTEM)
        # so it can be re-enabled per-config in a future PR.
        synthesizer_self_critic = False

        # Per-session tracer. No-op when both the per-request flag is
        # unset and the env var ``CONSULTANTS_TRACE`` is off, so
        # production runs pay zero overhead.
        tracer = Tracer.for_session(
            state.sid, enabled=runner_input.get("trace"),
        )

        # One ChatClient per role so retries and timing don't bleed.
        # Each is wrapped with TracedChat so every .chat() call emits
        # an llm_call span tagged with role, model, tokens, duration.
        # v1.5+: ``llamafile://<label>`` refs in the role's model spec
        # route to a daemon-ensured llamafile via
        # ``make_agent_chat_client``; bare refs continue through the
        # existing native ``/api/chat`` ChatClient. Both expose the
        # same ``chat(payload) -> dict`` + ``last_usage`` so the
        # TracedChat wrapper and agent loop are unchanged.
        pk = _polykv_manager(cfg, state, cwd, runner_input)
        chat_clients = {
            r: TracedChat(
                polykv_mod.placed(_role_client(cfg, r, ollama_base_url),
                                  pk, r),
                role=r, tracer=tracer, stop=_run_stop(state),
            )
            for r in enabled
        }
        models = {r: cfg.roles[r].model for r in enabled}

        # Task #111: per-model ChatClient dict for the coder role's
        # failover chain. Only materialised when ``coder`` is in
        # ``enabled`` AND the cfg has per-language routing OR a
        # global default route configured. Each model gets its own
        # TracedChat so per-attempt usage rolls up under the right
        # tag in the recorder.
        coder_clients_by_model: dict[str, Any] = {}
        coder_routes_by_language: dict[str, Any] = {}
        coder_default_route = None
        if "coder" in enabled:
            coder_routes_by_language = dict(
                cfg.roles["coder"].routes_by_language or {}
            )
            coder_default_route = cfg.roles["coder"].default_route
            if coder_routes_by_language or coder_default_route is not None:
                for m in cc.coder_unique_models(cfg):
                    if m == cfg.roles["coder"].model and "coder" in chat_clients:
                        # Re-use the legacy per-role client for the
                        # primary model so the ChatClient's warm
                        # /api/show probe + retry-budget state
                        # carries across.
                        coder_clients_by_model[m] = chat_clients["coder"]
                        continue
                    coder_clients_by_model[m] = TracedChat(
                        polykv_mod.placed(
                            make_agent_chat_client(m, ollama_base_url,
                                                   endpoints=cfg.endpoints),
                            pk, "coder"),
                        role="coder", tracer=tracer, stop=_run_stop(state),
                    )

        grounding_msgs = build_grounding_messages(
            cwd, tools_available=True,
        ) if "researcher" in enabled else []

        # Per-role think values: cfg.roles[role].think wins when set,
        # else DEFAULT_THINK_BY_ROLE. cc.role_think() does both.
        think_by_role = {r: cc.role_think(cfg, r) for r in enabled}

        # At high/max effort, skip the planner/synthesizer cache so
        # the user always gets fresh reasoning on the same question.
        # At low/medium, the in-memory cache de-dupes within a single
        # session (mostly defensive — the council doesn't usually
        # re-enter planner/synthesizer with identical state).
        disable_cache = cfg.effort in ("high", "max")

        # v1.1 SQLite recorder. Construction is best-effort — if the
        # filesystem refuses (read-only mount, permissions), we log
        # and continue without it so the consultation still runs.
        # The artifact dir is the same one storage.write_consultation
        # writes to.
        recorder = _build_recorder(
            sid=state.sid, cwd=cwd, question=question, cfg=cfg,
            models=models, parent_sid=None,
        )

        # Phase 9 (v1.1) — multi-model x-tier fan-out for researcher.
        # ``extras_active`` is True only at xmedium/xhigh/xmax, so
        # ``extra_models`` is silently ignored at every base tier
        # (a benchmark labeled `high` is never accidentally 3× the
        # cost). Cost-of-fan-out warning fires once at consultation
        # start so token bills aren't surprises.
        #
        # Phase 10 (v1.1) — multi-model critic fan-out, xmax-only.
        # Researcher fan-out activates at any x-tier; critic fan-out
        # activates only at xmax (per the user's design — diverse
        # critics + meta-critic combine is the most expensive
        # capability). At xhigh+critic_extras, the critic extras are
        # silently ignored.
        extra_models_by_role: dict[str, list[str]] = {}
        if cc.extras_active(cfg.effort):
            researcher_extras = list(
                cfg.roles["researcher"].extra_models or []
            )
            if researcher_extras:
                extra_models_by_role["researcher"] = researcher_extras
                _warn_extras_cost(
                    role="researcher",
                    primary=cfg.roles["researcher"].model,
                    extras=researcher_extras,
                    effort=cfg.effort,
                )
            # Critic extras only activate at xmax.
            if cfg.effort == "xmax":
                critic_extras = list(
                    cfg.roles["critic"].extra_models or []
                )
                if critic_extras and "critic" in enabled:
                    extra_models_by_role["critic"] = critic_extras
                    _warn_extras_cost(
                        role="critic",
                        primary=cfg.roles["critic"].model,
                        extras=critic_extras,
                        effort=cfg.effort,
                    )

        # 2026-05-07: synthesizer fallback chain. Universal — applies
        # at every effort tier including base ones. ``extra_models``
        # on the synthesizer role is repurposed here as a serial
        # failure-fallback list (NOT a fan-out — the synthesizer never
        # fans out, even at xmax). Empty list -> no fallback, original
        # single-model behavior.
        synthesizer_fallback = list(
            cfg.roles["synthesizer"].extra_models or []
        )

        if extra_roots:
            # 2026-05-18: render the display form (user-facing pre-realpath
            # paths) when available so /shared/dev/<x> shows in the log
            # instead of /srv/dev-disk-by-label-opt/dev/<x>. Falls back to
            # realpath rendering when the upstream didn't pass display info
            # (legacy / disk-reopened sessions). Both resolved at the top
            # of this function.
            log.info(
                "consultants sid=%s allowed roots:\n%s",
                state.sid,
                render_for_log(
                    [cwd, *extra_roots],
                    display_roots=[cwd_display, *extra_roots_display],
                ),
            )
        # M8: build the long-term-memory store if cfg.store.enabled is
        # true and the current effort tier is in the enable list.
        # The factory returns None for every short-circuit case
        # (disabled, below-gate, unknown backend, missing deps), so
        # the v1 zero-store behavior is the default. Recall + record
        # helpers downstream tolerate a None store.
        try:
            from consultants.engine.store import make_consultants_store
            consultants_store = make_consultants_store(
                cfg, sid=state.sid, effort=cfg.effort,
            )
        except Exception:  # pragma: no cover — defensive
            log.exception(
                "make_consultants_store raised; continuing with no store",
            )
            consultants_store = None

        # M-A: compose the tool surface from [tools] config. Returns the
        # same (specs, executor) shape the fixed builtin surface did, so
        # nothing downstream of GraphDeps knows a registry exists.
        _tool_specs, _tool_executor, _tool_registry = build_tool_surface(
            cfg, extra_roots=extra_roots,
            approval_fn=_make_tool_approval_fn(
                state, recorder, cfg, cwd, log_label="council"),
            runtime_permissions=_live_tool_permissions(state),
            shell=runner_input.get("shell"), sid=state.sid,
        )
        # M-B: which roles get those tools. Empty unless [tools]
        # all_roles is on, which keeps the default graph pre-M-B.
        _tooled_roles = (
            TOOLABLE_ROLES
            if getattr(getattr(cfg, "tools", None), "all_roles", False)
            else ()
        )

        deps = GraphDeps(
            chat_clients=chat_clients,
            models=models,
            enabled_roles=enabled,
            cwd=cwd,
            lane_workspace=_lane_workspace(_tool_registry, cwd),
            tool_executor=traced_tool(
                _tool_executor, tracer=tracer,
            ),
            tool_specs=_tool_specs,
            tooled_roles=_tooled_roles,
            grounding_msgs=grounding_msgs,
            think_by_role=think_by_role,
            synthesizer_self_critic=synthesizer_self_critic,
            disable_cache=disable_cache,
            recorder=recorder,
            extra_models_by_role=extra_models_by_role,
            synthesizer_fallback_models=synthesizer_fallback,
            store=consultants_store,
            sid=state.sid,
            # Plan D9: xauto reruns its infra-failed first-round lanes
            # once, before the critic. Never wired on another tier.
            xauto_lane_retry=(cfg.effort == "xauto" and bool(getattr(
                cfg.runtime, "xauto_retry_infra_lanes", True))),
            # Plan council-memory M5: challenged lanes answer before the
            # critic's verdict routes. Off: the graph is unchanged.
            cross_examination=bool(getattr(getattr(cfg, "critic", None),
                                           "cross_examination", False)),
            # Per-lane model escalation (plan D): off unless an expert
            # model is configured.
            escalation_model=getattr(getattr(cfg, "escalation", None),
                                     "expert_model", "") or "",
            escalation_max=int(getattr(getattr(cfg, "escalation", None),
                                       "max_per_council", 2)),
            # M10: coder sandbox caps. Consulted only when ``coder``
            # is in ``enabled`` (otherwise the coder node is never
            # registered, so these values are irrelevant).
            coder_max_file_bytes=cfg.coder_limits.max_file_bytes,
            coder_max_total_bytes=cfg.coder_limits.max_total_bytes,
            coder_max_files=cfg.coder_limits.max_files,
            # Task #111: per-language coder routing. Empty dicts /
            # None when not configured ⇒ _wrap_coder returns the
            # v1 single-attempt callable (full back-compat).
            coder_chat_clients_by_model=coder_clients_by_model,
            coder_routes_by_language=coder_routes_by_language,
            coder_default_route=coder_default_route,
            # M3: strictness dial for the opt-in adversary refuter.
            adversary_strictness=getattr(cfg, "adversary_strictness",
                                         "normal"),
            # Cooperative cancel / pause. Out-of-band by necessity: a
            # mid-invoke graph never re-reads its own channels, so the
            # flag has to reach the node gate off the SessionState.
            run_control=state.run_control,
            run_control_emit=_make_run_control_emit(
                state, recorder, log_label="council"),
            run_control_is_closed=lambda: bool(
                getattr(state, "closed", False)),
        )
        # #314: ALWAYS compile with interrupt_before=["synthesizer"].
        # The pause before the final-answer node is the window where a
        # mid-flight inject can rewind the council to a researcher
        # round (see _drive_council_stream). The default path
        # auto-resumes past it immediately (byte-identical artifacts —
        # M12 parity); review_before_synthesis (M5 HITL) parks for an
        # external /resume; a synthesis-phase inject rewinds.
        review_before_synthesis = False
        try:
            review_before_synthesis = bool(
                getattr(cfg.runtime, "review_before_synthesis", False))
        except AttributeError:
            review_before_synthesis = False
        interrupt_before: list[str] = ["synthesizer"]
        # #214 Fix B: attach a MemorySaver checkpointer so the M9
        # control surface (state / cancel / inject / pause / resume)
        # works in the standard config. Falls back to None if
        # langgraph's checkpoint.memory module isn't importable
        # (defensive — every langgraph release we depend on ships it).
        # Attach a serde whose msgpack allowlist covers our custom
        # CouncilState channel types (Doc, ToolPlanItem, …, RoleTurn)
        # so they survive checkpoint round-trips as real instances
        # instead of silently degrading to dicts under LangGraph's
        # coming strict-msgpack mode. See
        # state_v2.make_checkpointer_serde.
        checkpointer = _open_checkpointer(
            state, cfg, cwd, memory_saver_cls=MemorySaver)
        compiled = build_council_graph(
            deps, tracer=tracer,
            interrupt_before=interrupt_before,
            checkpointer=checkpointer,
        )

        # M9: attach the live graph + thread config + recorder to
        # SessionState so the HTTP control route handlers can read
        # state, apply injects, mutate runtime_control, interrupt,
        # resume, and replay events. Set BEFORE streaming so a fast
        # consumer that pings /state immediately gets the live
        # snapshot (not a 503).
        thread_config: dict = {"configurable": {"thread_id": state.sid}}
        state._compiled = compiled
        state._thread_config = thread_config
        state._recorder = recorder

        # Build initial state, mark planner in_progress for the first
        # progress poll (it's the entry node by default).
        initial = council_mod.initial_state(
            question=question, cwd=cwd, models=models,
            topology=cfg.topology, effort=cfg.effort,
        )
        # 2026-05-18: thread extra_roots into LangGraph state so the
        # synthesizer's post-output citation linter can resolve cites
        # under all session-allowed directories, not just cwd. Empty
        # tuple is the safe default — the linter falls back to cwd
        # alone when this key is absent.
        if extra_roots:
            initial["extra_roots"] = list(extra_roots)
        initial["runtime_control"] = _seed_runtime_control(
            cfg, enabled,
            n_fanout_extras=len(extra_models_by_role.get("researcher", [])))
        _tried = _tried_docs(runner_input)
        if _tried:
            initial["additional_context"] = _tried
        _start_progress(state, enabled, runner_input)

        # Run the graph in streaming mode so per-role progress flips
        # in real time. We use BOTH stream modes:
        #   "updates" -> {node_name: partial} per node fire;
        #                used to flip state.progress[role].
        #   "values"  -> full merged state after each superstep;
        #                used as final_state for artifact writing
        #                so additive reducers (research, turns,
        #                tokens) merge correctly across parallel
        #                fan-out lanes.
        # LangGraph yields (mode, payload) tuples when stream_mode
        # is a list.
        final_state: dict = dict(initial)
        adv_checkpoint, adv_timeout_s = _read_adversary_checkpoint_cfg(cfg)
        try:
            final_state = _drive_council_stream(
                compiled, initial, thread_config,
                state=state, enabled=enabled,
                review_before_synthesis=review_before_synthesis,
                recorder=recorder, log_label="council",
                adversary_checkpoint=adv_checkpoint,
                adversary_checkpoint_timeout_s=adv_timeout_s,
                resume=bool(runner_input.get("resume")),
            )
        except RunSuspended as e:
            _mark_suspended(state, recorder, e, log_label="council")
            return
        except Exception as e:
            log.exception("council graph invocation failed: %s", e)
            state.status = "failed"
            state.error = f"graph crashed: {e}"
            state.finished_at = time.time()
            _emit_council_complete(
                recorder, sid=state.sid, status="failed", final_answer="",
            )
            _finalize_recorder(recorder, status="failed", error=str(e))
            _write_failed_artifacts(state, cwd, question, e)
            return

        # Belt-and-braces: any role still marked in_progress after
        # the stream drained gets flipped to done so the final poll
        # reflects reality even if a node never emitted an update
        # (shouldn't happen with LangGraph but cheap to guard).
        for r in enabled:
            if state.progress.get(r) != "done":
                state.progress[r] = "done"

        # Detect mid-pipeline errors recorded by individual nodes.
        node_error = final_state.get("error")
        node_failed = final_state.get("_role_failed")
        terminal_status = "failed" if node_error else "completed"
        # A cancelled run drained rather than finished. Reporting it as
        # "completed" would be the same lie the flag itself used to
        # tell: there is no synthesized answer, because the synthesizer
        # is a node like any other and running it would be spending
        # after the user said stop.
        if state.run_control is not None and state.run_control.cancelled:
            terminal_status = "cancelled"
            log.warning(
                "council: run cancelled (%s) - %d node(s) skipped, "
                "partial state kept",
                state.run_control.cancel_reason,
                len(state.run_control.skipped),
            )

        # A cut-off deliverable is a failure even when every node
        # returned cleanly (2026-08-12). Only promoted to "failed" from
        # "completed": a cancelled or already-failed run keeps its own,
        # more specific verdict.
        truncations, trunc_error = _truncation_verdict(
            recorder, final_state.get("final_answer", "") or "")
        if trunc_error and terminal_status == "completed":
            terminal_status = "failed"
            node_error = trunc_error

        # Persist artifacts.
        result = storage.ConsultationResult(
            session_id=state.sid,
            created=time.strftime(
                "%Y-%m-%dT%H:%M:%S",
                time.localtime(state.started_at)),
            question=question,
            models=models,
            topology=cfg.topology,
            effort=cfg.effort,
            final_answer=answer_with_shell(
                final_state.get("final_answer", ""), runner_input.get("shell"),
                cwd=cwd, sid=state.sid),
            turns=list(final_state.get("turns") or []),
            duration_seconds=time.time() - state.started_at,
            status=terminal_status,
            error=node_error,
            cwd=cwd,
            total_prompt_tokens=int(final_state.get(
                "total_prompt_tokens") or 0),
            total_completion_tokens=int(final_state.get(
                "total_completion_tokens") or 0),
            retries_by_role=dict(final_state.get(
                "retries_by_role") or {}),
            truncations_by_role=truncations,
            root_sid=getattr(state, "root_sid", None) or state.sid,
            extra_roots=list(extra_roots),
            cwd_display=cwd_display,
            extra_roots_display=list(extra_roots_display),
            decision=final_state.get("decision_review") or None,
            partial=(state.run_control.partial()
                     if state.run_control is not None else None),
        )
        storage.write_consultation(result, cwd=Path(cwd))

        # Live-session retention: copy the consultation's plan +
        # research + critique + final_answer + models + warm
        # ChatClients onto the SessionState so a future follow-up
        # POST /v1/consult/<sid>/follow-up can reuse them without
        # re-loading from disk or re-probing /api/show.
        #
        # Store the RAW ChatClient instances (TracedChat._client),
        # not the TracedChat wrappers — the follow-up creates its
        # own tracer and wraps them fresh, so the warm
        # _probed_think / _unsupported_think caches survive while
        # the per-call trace plumbing rebinds correctly.
        state.plan = final_state.get("plan") or ""
        state.plan_items = list(final_state.get("plan_items") or [])
        state.research = list(final_state.get("research") or [])
        state.critique = final_state.get("critique")
        state.final_answer = answer_with_shell(
            final_state.get("final_answer") or "", runner_input.get("shell"),
            cwd=cwd, sid=state.sid)
        state.models = dict(models)
        state._chat_clients = {
            r: getattr(c, "_client", c) for r, c in chat_clients.items()
        }
        # Task #111: also stash the per-model raw clients so a
        # follow-up's coder lanes can reuse warmed-up failover
        # candidates without re-probing /api/show.
        state._coder_chat_clients_by_model = {
            m: getattr(c, "_client", c)
            for m, c in coder_clients_by_model.items()
        }
        state.bump_activity()

        state.status = terminal_status
        state.error = node_error
        state.finished_at = time.time()
        if node_failed:
            log.warning("council finished with role failure: %s",
                        node_failed)
        _emit_council_complete(
            recorder, sid=state.sid, status=terminal_status,
            final_answer=getattr(state, "final_answer", "") or "",
        )
        _finalize_recorder(
            recorder, status=terminal_status, error=node_error,
            finished_at=state.finished_at,
        )
        # v1.1: load the role-message threads off the now-finalized
        # transcript.db so the warm SessionState carries the same
        # ``_role_messages`` / ``_role_lane_messages`` shape a
        # disk-reopened session would have. Phase 5's follow-up
        # branches on these uniformly.
        _populate_role_messages(state, recorder)

    return run_council


def make_follow_up_runner(*, ollama_base_url: str):
    """Return a ``run_follow_up(child_state, runner_input)`` callable.

    Mirrors ``make_runner`` but uses the SHORTENED follow-up graph
    (researcher → [critic at high] → synthesizer → END). Reuses the
    parent SessionState's warm ``_chat_clients`` when present so
    /api/show probes don't re-fire and the per-model
    ``_unsupported_think`` cache carries over. Falls back to
    creating fresh ChatClients if the parent's are gone (e.g. the
    parent was reaped between completion and follow-up).
    """
    from claude_hooks.allowed_roots import (
        render_for_log,
    )
    from claude_hooks.get_advice.chat_client import make_agent_chat_client
    from claude_hooks.caliber_proxy.prompt import build_grounding_messages
    from consultants.server.shell_enable import answer_with_shell
    from consultants.server.tool_surface import build_tool_surface
    from consultants.engine.graph import (
        TOOLABLE_ROLES, GraphDeps, build_follow_up_graph,
    )
    from consultants.engine.trace import (
        Tracer, TracedChat, traced_tool,
    )

    def run_follow_up(state, runner_input: dict) -> None:
        cfg: cc.ConsultantsConfig = runner_input["config"]
        cwd: str = runner_input["cwd"]
        question: str = runner_input["question"]
        parent_state = runner_input.get("parent_state")
        # v1.8+: merge follow-up extras with whatever the parent had,
        # so a follow-up inherits the parent's reach plus any new
        # --add-dir on this turn. Order is preserved + dedup'd.
        follow_extras = tuple(runner_input.get("extra_roots") or ())
        parent_extras = tuple(
            getattr(parent_state, "extra_roots", None) or ()
        )
        seen: set[str] = set()
        merged: list[str] = []
        for r in parent_extras + follow_extras:
            if r and r not in seen:
                seen.add(r)
                merged.append(r)
        extra_roots = tuple(merged)

        # Pre-flight before anything is built. Placed above the
        # recorder and the chat clients so a refusal costs nothing and
        # leaves no half-open transcript.db behind. A follow-up can
        # name files the original never did and can add roots of its
        # own, so the check runs again rather than inheriting the
        # parent's verdict. Display forms aren't resolved this early —
        # the realpaths are what the search actually used, which is
        # the honest thing to print in a refusal anyway.
        if _preflight_refused(
            state, question, cwd=cwd, extra_roots=extra_roots,
            cwd_display=cwd, extra_roots_display=extra_roots,
            label="council follow-up",
            skip=bool(runner_input.get("skip_preflight")
                      or runner_input.get("resume")),
        ):
            return

        # Topology: researcher + synthesizer always; critic only at
        # high/max effort (matches the main runner's gate). x-tiers
        # mirror their base tier: xhigh / xmax keep critic, xmedium
        # drops it. The parent's ``enabled_roles`` may have
        # included critic but the follow-up topology decides
        # independently based on this follow-up's effort.
        enabled = ["researcher", "synthesizer"]
        # xauto keeps it too: its escalator exists only when a critic
        # does (see the main runner).
        if (cc.base_effort(cfg.effort) in ("high", "max")
                or cfg.effort == "xauto") \
                and "critic" in cc.enabled_roles(cfg):
            enabled = ["researcher", "critic", "synthesizer"]
        enabled_t = tuple(enabled)

        tracer = Tracer.for_session(
            state.sid, enabled=runner_input.get("trace"),
        )

        # Reuse the parent's warm raw ChatClients if available.
        warm_clients = (
            parent_state._chat_clients
            if parent_state is not None
            and parent_state._chat_clients is not None
            else {}
        )
        pk_fu = _polykv_manager(cfg, state, cwd, runner_input, adopt=True)
        chat_clients = {}
        for role in enabled_t:
            raw = warm_clients.get(role)
            # A warm client the parent's run placed is taken bare: this run
            # has its own manager (or none).
            raw = getattr(raw, "_placed_inner", raw)
            if raw is None:
                # Parent's clients went away (closed, reaped, or
                # the parent didn't run that role). Cold-start —
                # the follow-up still works but pays the
                # /api/show probe cost on first use.
                # v1.5+: dispatch based on the role's model_ref so
                # cold-start follow-ups land on the right backend.
                role_model = cfg.roles[role].model
                raw = _role_client(cfg, role, ollama_base_url)
                log.info(
                    "follow-up %s: cold ChatClient for role=%s "
                    "(no warm parent client; model=%s)",
                    state.sid, role, role_model,
                )
            chat_clients[role] = TracedChat(polykv_mod.placed(raw, pk_fu, role),
                                            role=role, tracer=tracer,
                                            stop=_run_stop(state))

        # Task #111: per-model coder clients (mirrors the primary
        # runner's logic). Re-use warm parent clients when present;
        # otherwise cold-build. Follow-ups inherit the same routing
        # configuration the parent ran with (via cfg).
        coder_clients_by_model_fu: dict[str, Any] = {}
        coder_routes_by_language_fu: dict[str, Any] = {}
        coder_default_route_fu = None
        warm_coder_clients = (
            parent_state._coder_chat_clients_by_model
            if parent_state is not None
            and parent_state._coder_chat_clients_by_model is not None
            else {}
        )
        if "coder" in enabled_t:
            coder_routes_by_language_fu = dict(
                cfg.roles["coder"].routes_by_language or {}
            )
            coder_default_route_fu = cfg.roles["coder"].default_route
            if coder_routes_by_language_fu \
                    or coder_default_route_fu is not None:
                for m in cc.coder_unique_models(cfg):
                    if m == cfg.roles["coder"].model \
                            and "coder" in chat_clients:
                        coder_clients_by_model_fu[m] = \
                            chat_clients["coder"]
                        continue
                    raw = warm_coder_clients.get(m)
                    if raw is None:
                        raw = make_agent_chat_client(
                            m, ollama_base_url, endpoints=cfg.endpoints)
                    coder_clients_by_model_fu[m] = TracedChat(
                        polykv_mod.placed(raw, pk_fu, "coder"),
                        role="coder", tracer=tracer, stop=_run_stop(state),
                    )

        # Models: prefer parent's recorded models so the follow-up
        # talks to the same models the parent used. Falls back to
        # current cfg.roles[role].model when parent didn't record.
        parent_models = (parent_state.models
                         if parent_state is not None else {})
        models = {
            r: parent_models.get(r) or cfg.roles[r].model
            for r in enabled_t
        }

        grounding_msgs = build_grounding_messages(
            cwd, tools_available=True,
        )

        # Per-role think values from current cfg (parent_state
        # didn't capture think). Defaults are role-specific so this
        # is fine.
        think_by_role = {r: cc.role_think(cfg, r) for r in enabled_t}

        # Recorder for the follow-up — separate db file under the
        # follow-up's own sid dir, with parent_sid baked into meta so
        # the chain is reconstructable from disk-only state.
        recorder = _build_recorder(
            sid=state.sid, cwd=cwd, question=question, cfg=cfg,
            models=models, parent_sid=state.parent_sid,
        )

        # Phase 5 (v1.1): if the parent has per-role message threads
        # (warm completion or disk-reopened), pass them through so
        # the follow-up's researcher and synthesizer extend the
        # parent's conversation rather than rebuild from scratch.
        # ``_role_messages`` is None for v1.0 parents — fall back to
        # today's behavior cleanly in that case.
        prior_messages_by_role: dict[str, list[dict]] = {}
        # Plan D9: a retry-lanes run re-answers the parent's question from
        # all of its research, so the synthesizer starts fresh rather than
        # continuing the parent's thread as a follow-up would.
        retry_lanes = list(runner_input.get("retry_lanes") or [])
        if (not retry_lanes and parent_state is not None
                and parent_state._role_messages is not None):
            for role in ("researcher", "synthesizer"):
                thread = parent_state._role_messages.get(role)
                if thread:
                    prior_messages_by_role[role] = thread

        # 2026-05-07: same synthesizer fallback chain as the primary
        # runner. Carries through follow-ups so a flap mid-followup
        # is rescued the same way as a flap mid-original.
        synthesizer_fallback_followup = list(
            cfg.roles["synthesizer"].extra_models or []
        )

        # Display forms, resolved unconditionally so the artifact
        # writer below can persist them even when this follow-up added
        # no extra roots (the log block only fires when it did).
        cwd_display_fu: str = runner_input.get("cwd_display") or cwd
        extra_roots_display_fu: tuple[str, ...] = ()

        if extra_roots:
            # 2026-05-18: render display form like run_council does.
            # ``extra_roots`` here is the parent ∪ follow-up merged
            # realpath list. Build the parallel display form from the
            # follow-up's body-extras-display and the parent's stored
            # display info; fall back to realpath where neither is
            # available.
            parent_state_fu = runner_input.get("parent_state")
            parent_real_to_display: dict[str, str] = {}
            if parent_state_fu is not None:
                p_real = list(getattr(parent_state_fu, "extra_roots", []) or [])
                p_disp = list(
                    getattr(parent_state_fu, "extra_roots_display", []) or []
                )
                if p_disp and len(p_disp) == len(p_real):
                    parent_real_to_display = dict(zip(p_real, p_disp))
            body_real_to_display: dict[str, str] = {}
            body_disp = runner_input.get("extra_roots_display") or []
            for raw in body_disp:
                from claude_hooks.allowed_roots import _canonical
                real = _canonical(raw)
                if real:
                    body_real_to_display.setdefault(real, raw)
            extra_roots_display_fu = tuple(
                parent_real_to_display.get(r) or body_real_to_display.get(r) or r
                for r in extra_roots
            )
            log.info(
                "consultants follow-up sid=%s allowed roots:\n%s",
                state.sid,
                render_for_log(
                    [cwd, *extra_roots],
                    display_roots=[cwd_display_fu, *extra_roots_display_fu],
                ),
            )
        # M8: follow-ups share the store config with their parent.
        # The store is keyed by the follow-up's own sid (each
        # follow-up records under its own ``(sid, "research")``
        # namespace) — but the follow-up runner's recall path can
        # also fall back to ``(parent_sid, "research")`` via
        # ``recall_for_follow_up`` when needed.
        try:
            from consultants.engine.store import make_consultants_store
            consultants_store_followup = make_consultants_store(
                cfg, sid=state.sid, effort=cfg.effort,
            )
        except Exception:  # pragma: no cover — defensive
            log.exception(
                "make_consultants_store (follow-up) raised; "
                "continuing with no store",
            )
            consultants_store_followup = None

        # M-A: compose the tool surface from [tools] config. Returns the
        # same (specs, executor) shape the fixed builtin surface did, so
        # nothing downstream of GraphDeps knows a registry exists.
        _tool_specs, _tool_executor, _tool_registry = build_tool_surface(
            cfg, extra_roots=extra_roots,
            approval_fn=_make_tool_approval_fn(
                state, recorder, cfg, cwd, log_label="council follow-up"),
            runtime_permissions=_live_tool_permissions(state),
            shell=runner_input.get("shell"), sid=state.sid,
        )
        # M-B: which roles get those tools. Empty unless [tools]
        # all_roles is on, which keeps the default graph pre-M-B.
        _tooled_roles = (
            TOOLABLE_ROLES
            if getattr(getattr(cfg, "tools", None), "all_roles", False)
            else ()
        )

        deps = GraphDeps(
            chat_clients=chat_clients,
            models=models,
            enabled_roles=enabled_t,
            cwd=cwd,
            lane_workspace=_lane_workspace(_tool_registry, cwd),
            tool_executor=traced_tool(
                _tool_executor, tracer=tracer,
            ),
            tool_specs=_tool_specs,
            tooled_roles=_tooled_roles,
            grounding_msgs=grounding_msgs,
            think_by_role=think_by_role,
            synthesizer_self_critic=False,  # follow-ups never
            disable_cache=True,             # caching not useful here
            recorder=recorder,
            prior_messages_by_role=prior_messages_by_role,
            synthesizer_fallback_models=synthesizer_fallback_followup,
            store=consultants_store_followup,
            sid=state.sid,
            # M10: coder sandbox caps. Follow-ups inherit the
            # parent's effective limits via cfg (which may have
            # been mutated by a per-session control update).
            coder_max_file_bytes=cfg.coder_limits.max_file_bytes,
            coder_max_total_bytes=cfg.coder_limits.max_total_bytes,
            coder_max_files=cfg.coder_limits.max_files,
            # Task #111: per-language coder routing (warm-aware).
            coder_chat_clients_by_model=coder_clients_by_model_fu,
            coder_routes_by_language=coder_routes_by_language_fu,
            coder_default_route=coder_default_route_fu,
            adversary_strictness=getattr(cfg, "adversary_strictness",
                                         "normal"),
            # Cooperative cancel / pause. Out-of-band by necessity: a
            # mid-invoke graph never re-reads its own channels, so the
            # flag has to reach the node gate off the SessionState.
            run_control=state.run_control,
            run_control_emit=_make_run_control_emit(
                state, recorder, log_label="council follow-up"),
            run_control_is_closed=lambda: bool(
                getattr(state, "closed", False)),
        )
        # #214/M9 parity fix: follow-ups MUST attach a checkpointer
        # too. run_council does (line ~319) but run_follow_up did not,
        # so LangGraph get_state/update_state raised
        # ValueError("No checkpointer set") and every M9 endpoint
        # (/state, /inject, /resume) 500'd on a follow-up sid. Mirror
        # the council path exactly: in-memory checkpointer for live
        # introspection (the recorder's transcript.db remains the
        # durable audit log). Import is local to this runner so the
        # core stays stdlib-only when langgraph is absent.
        try:
            from langgraph.checkpoint.memory import MemorySaver as _MemSaver
        except ImportError:  # pragma: no cover — minimal langgraph
            try:
                from langgraph.checkpoint.memory import (
                    InMemorySaver as _MemSaver,  # type: ignore[assignment]
                )
            except ImportError:
                _MemSaver = None  # type: ignore[assignment]
        # Same custom-type serde allowlist as run_council so follow-up
        # checkpoint round-trips don't degrade Doc/ToolResult/… to dicts.
        fu_checkpointer = _open_checkpointer(
            state, cfg, cwd, memory_saver_cls=_MemSaver)
        # #314: same always-on synthesizer interrupt as the council so
        # a mid-flight follow-up inject can rewind to a researcher round.
        # Plan G5: on the typed protocol one tiny call routes the
        # follow-up (a retry-lanes child has its route already).
        deps.follow_up_route = bool(getattr(getattr(cfg, "protocol", None),
                                            "typed", False)) and not retry_lanes
        compiled = build_follow_up_graph(
            deps, tracer=tracer, checkpointer=fu_checkpointer,
            interrupt_before=["synthesizer"],
            **({"retry_lanes": retry_lanes} if retry_lanes else {}),
        )
        fu_review_before_synthesis = False
        try:
            fu_review_before_synthesis = bool(
                getattr(cfg.runtime, "review_before_synthesis", False))
        except AttributeError:
            fu_review_before_synthesis = False
        # M9: follow-ups expose their own compiled graph + thread
        # config under the follow-up's own sid (not the parent's).
        # The control routes resolve a session by sid → SessionState;
        # both the parent and the follow-up have distinct entries.
        thread_config: dict = {"configurable": {"thread_id": state.sid}}
        state._compiled = compiled
        state._thread_config = thread_config
        state._recorder = recorder

        # Initial state for the follow-up. Two cases:
        #
        # (A) prior_messages_by_role populated (Phase 5 path) — the
        #     parent's research and final answer are ALREADY inlined
        #     in the per-role threads we'll feed via GraphDeps. We
        #     deliberately leave initial["research"] empty so the
        #     follow-up's researcher delta isn't mixed with stale
        #     parent rounds; the synthesizer's prior_messages branch
        #     only surfaces THIS turn's research.
        #
        # (B) prior_messages_by_role empty (v1.0 parent without a
        #     transcript.db, or recorder-disabled run) — fall back
        #     to the original behavior: pre-seed parent's research +
        #     a "## Prior synthesizer answer" block as plan_item.
        initial = council_mod.initial_state(
            question=question, cwd=cwd, models=models,
            topology=cfg.topology, effort=cfg.effort,
        )
        # 2026-05-18: same extra_roots threading as the primary
        # runner — the follow-up's synthesizer linter needs the
        # merged parent+followup allowed-roots set.
        if extra_roots:
            initial["extra_roots"] = list(extra_roots)
        initial["runtime_control"] = _seed_runtime_control(cfg, enabled_t)
        initial["plan"] = (
            parent_state.plan
            if parent_state is not None and parent_state.plan
            else "(follow-up: focus on the question above)"
        )
        initial["plan_item"] = question
        initial["lane_idx"] = 0
        if retry_lanes:
            # The parent's reports that stand; its tombstones are replaced
            # by the retried lanes' reports.
            from consultants.engine.retry_lanes import surviving_reports
            initial.pop("plan_item", None)
            initial.pop("lane_idx", None)
            initial["plan_items"] = list(
                getattr(parent_state, "plan_items", None) or [])
            initial["research"] = surviving_reports(
                getattr(parent_state, "research", None) or [])
        elif prior_messages_by_role:
            initial["research"] = []
        else:
            prior_research: list[str] = []
            if parent_state is not None:
                prior_research = list(parent_state.research or [])
                if parent_state.final_answer:
                    prior_research.append(
                        "## Prior synthesizer answer\n\n"
                        + parent_state.final_answer.strip()
                    )
            initial["research"] = prior_research

        _start_progress(state, enabled, runner_input)

        # Same dual-mode streaming + always-on synthesizer interrupt as
        # run_council, via the shared driver (auto-resume / rewind / HITL).
        final_state: dict = dict(initial)
        fu_adv_checkpoint, fu_adv_timeout_s = _read_adversary_checkpoint_cfg(cfg)
        try:
            final_state = _drive_council_stream(
                compiled, initial, thread_config,
                state=state, enabled=enabled,
                review_before_synthesis=fu_review_before_synthesis,
                recorder=recorder, log_label="follow-up",
                adversary_checkpoint=fu_adv_checkpoint,
                adversary_checkpoint_timeout_s=fu_adv_timeout_s,
                resume=bool(runner_input.get("resume")),
            )
        except RunSuspended as e:
            _mark_suspended(state, recorder, e, log_label="follow-up")
            return
        except Exception as e:
            log.exception("follow-up graph invocation failed: %s", e)
            state.status = "failed"
            state.error = f"graph crashed: {e}"
            state.finished_at = time.time()
            _emit_council_complete(
                recorder, sid=state.sid, status="failed", final_answer="",
            )
            _finalize_recorder(recorder, status="failed", error=str(e))
            _write_failed_artifacts(state, cwd, question, e)
            return

        for r in enabled:
            if state.progress.get(r) != "done":
                state.progress[r] = "done"

        node_error = final_state.get("error")
        node_failed = final_state.get("_role_failed")
        terminal_status = "failed" if node_error else "completed"
        # A cancelled run drained rather than finished. Reporting it as
        # "completed" would be the same lie the flag itself used to
        # tell: there is no synthesized answer, because the synthesizer
        # is a node like any other and running it would be spending
        # after the user said stop.
        if state.run_control is not None and state.run_control.cancelled:
            terminal_status = "cancelled"
            log.warning(
                "council follow-up: run cancelled (%s) - %d node(s) skipped, "
                "partial state kept",
                state.run_control.cancel_reason,
                len(state.run_control.skipped),
            )

        truncations, trunc_error = _truncation_verdict(
            recorder, final_state.get("final_answer", "") or "")
        if trunc_error and terminal_status == "completed":
            terminal_status = "failed"
            node_error = trunc_error

        # Persist artifacts for the FOLLOW-UP (its own sid + dir).
        # parent_sid is recorded in metadata so the chain is
        # reconstructable from disk.
        result = storage.ConsultationResult(
            session_id=state.sid,
            created=time.strftime(
                "%Y-%m-%dT%H:%M:%S",
                time.localtime(state.started_at)),
            question=question,
            models=models,
            topology=cfg.topology,
            effort=cfg.effort,
            final_answer=answer_with_shell(
                final_state.get("final_answer", ""), runner_input.get("shell"),
                cwd=cwd, sid=state.sid),
            turns=list(final_state.get("turns") or []),
            duration_seconds=time.time() - state.started_at,
            status=terminal_status,
            error=node_error,
            cwd=cwd,
            total_prompt_tokens=int(final_state.get(
                "total_prompt_tokens") or 0),
            total_completion_tokens=int(final_state.get(
                "total_completion_tokens") or 0),
            retries_by_role=dict(final_state.get(
                "retries_by_role") or {}),
            truncations_by_role=truncations,
            parent_sid=state.parent_sid,
            root_sid=getattr(state, "root_sid", None) or state.sid,
            extra_roots=list(extra_roots),
            cwd_display=cwd_display_fu,
            extra_roots_display=list(extra_roots_display_fu),
            decision=final_state.get("decision_review") or None,
            partial=(state.run_control.partial()
                     if state.run_control is not None else None),
        )
        storage.write_consultation(result, cwd=Path(cwd))

        # Inherit warm clients onto the follow-up's state so the
        # next follow-up in the chain (follow-up of follow-up)
        # also reuses them.
        state.plan = final_state.get("plan") or initial["plan"]
        state.plan_items = list(final_state.get("plan_items") or [])
        state.research = list(final_state.get("research") or [])
        state.critique = final_state.get("critique")
        state.final_answer = answer_with_shell(
            final_state.get("final_answer") or "", runner_input.get("shell"),
            cwd=cwd, sid=state.sid)
        state.models = dict(models)
        state._chat_clients = {
            r: getattr(c, "_client", c) for r, c in chat_clients.items()
        }
        # Task #111: stash per-model coder clients for the next
        # follow-up in the chain.
        state._coder_chat_clients_by_model = {
            m: getattr(c, "_client", c)
            for m, c in coder_clients_by_model_fu.items()
        }
        state.bump_activity()
        # Also bump the parent so an active iteration chain keeps
        # the whole lineage warm.
        if parent_state is not None:
            parent_state.bump_activity()

        state.status = terminal_status
        state.error = node_error
        state.finished_at = time.time()
        if node_failed:
            log.warning(
                "follow-up finished with role failure: %s (sid=%s "
                "parent=%s)",
                node_failed, state.sid, state.parent_sid,
            )
        _emit_council_complete(
            recorder, sid=state.sid, status=terminal_status,
            final_answer=getattr(state, "final_answer", "") or "",
        )
        _finalize_recorder(
            recorder, status=terminal_status, error=node_error,
            finished_at=state.finished_at,
        )
        _populate_role_messages(state, recorder)

    return run_follow_up


def _truncation_verdict(recorder, final_answer: str) -> tuple[dict, Optional[str]]:
    """Return ``(truncations_by_role, error)`` for a finished run.

    ``error`` is non-None only when the **deliverable** was cut — the
    synthesizer's completion, or a ``final_answer`` that ends inside a
    construct it opened. A researcher whose intermediate output got
    clipped is worth recording and worth a log line, but the council
    still produced a whole answer from what it had; failing the run
    over it would throw away good work.

    A truncated final answer, by contrast, must never reach the caller
    as ``status: completed, error: null``. That is exactly what
    csl-2026-08-12-0831-905a did: 43 minutes and 2.86M prompt tokens
    distilled into 655 characters ending ``of $\\text{``, filed as a
    success. Surfacing it as a failure costs the user a re-run;
    concealing it cost them a decision made on a third of an audit.
    """
    counts: dict = {}
    unrecovered: list[str] = []
    if recorder is not None:
        try:
            counts = recorder.truncations_by_role()
            unrecovered = recorder.unrecovered_truncations()
        except Exception:  # pragma: no cover — never sink a finished run
            log.exception("recorder truncation query raised; ignored")
            counts, unrecovered = {}, []

    reasons: list[str] = []
    for role in ("synthesizer", "refuter"):
        # Counts alone would fail runs the continuation loop rescued:
        # a synthesizer that overran its budget once and then finished
        # produced a whole answer, and reporting that as a failure
        # would discard 40 minutes of correct work. Only the role's
        # *last* completion being cut means the deliverable is short.
        if role in unrecovered:
            reasons.append(
                f"the {role}'s final completion was cut short and "
                f"could not be continued")
    if not reasons and final_answer:
        # Structural backstop: covers a backend that reported a clean
        # stop, and covers a recorder that failed to construct (the
        # counts are then empty and prove nothing).
        try:
            from claude_hooks.truncation import unterminated_construct
            construct = unterminated_construct(final_answer)
        except Exception:  # pragma: no cover
            construct = None
        if construct:
            reasons.append(f"the final answer ends inside an {construct}")

    if not reasons:
        if counts:
            log.warning(
                "council: %d role completion(s) were cut short but were "
                "continued to completion: %s", sum(counts.values()), counts,
            )
        return counts, None

    error = (
        "truncated output: " + "; ".join(reasons)
        + ". The answer is incomplete — re-run, or ask a narrower "
          "question so the response fits the model's output budget."
    )
    log.error("council: %s", error)
    return counts, error


def _build_recorder(*, sid: str, cwd: str, question: str,
                    cfg: cc.ConsultantsConfig, models: dict[str, str],
                    parent_sid: Optional[str]):
    """Build a MessageRecorder for the consultation. Returns ``None``
    on any construction failure so the caller can keep running without
    the SQLite sidecar (the consultation still produces summary.md /
    transcript.md / metadata.json).

    The db file lives at
    ``<cwd>/.claude-hooks/consultants/<sid>/transcript.db`` —
    same directory storage.write_consultation writes to.
    """
    from consultants.engine.recorder import MessageRecorder, RecorderMeta
    from consultants.engine import storage as _storage
    try:
        sdir = Path(cwd) / ".claude-hooks" / "consultants" / sid
        meta = RecorderMeta(
            sid=sid,
            cwd=cwd,
            question=question,
            effort=cfg.effort,
            topology=cfg.topology,
            models=dict(models),
            parent_sid=parent_sid,
        )
        recorder = MessageRecorder(
            sdir / _storage.TRANSCRIPT_DB_FILENAME,
            meta=meta,
        )
        # 2026-05-18: surface the transcript path in the session log so
        # operators can locate it without having to grep the runner
        # source for the convention.
        log.info(
            "consultants sid=%s transcript.db: %s",
            sid, sdir / _storage.TRANSCRIPT_DB_FILENAME,
        )
        return recorder
    except Exception as exc:
        log.warning(
            "MessageRecorder construction failed (sid=%s): %s; "
            "consultation will run without transcript.db", sid, exc,
        )
        return None


def _warn_extras_cost(*, role: str, primary: str,
                      extras: list[str], effort: str) -> None:
    """One-line cost-of-fan-out heads-up at consultation start.
    Fires only at x-prefixed effort tiers when the role has at
    least one extra model. The warning is logged at WARNING level
    so engine operators see it without verbose-level scraping."""
    total = 1 + len(extras)
    base = effort[1:] if effort.startswith("x") else effort
    log.warning(
        "%s: %d %s models per plan-item lane (primary=%s, "
        "extras=%s) — expect ~%dx the %s-tier %s token cost.",
        effort, total, role, primary, ", ".join(extras),
        total, base, role,
    )


def _populate_role_messages(state, recorder) -> None:
    """After ``finalize_recorder`` runs, query the just-finalized
    transcript.db and attach the per-role / per-lane message threads
    to the SessionState. Best-effort — leaves the fields at None on
    any failure (recorder=None, file missing, parse error). The
    fields default to None so a no-op leaves the warm-session shape
    matching a v1.0 reopen.
    """
    if recorder is None:
        return
    try:
        from consultants.engine.recorder import load_role_messages
        rm, rlm = load_role_messages(recorder.db_path)
        state._role_messages = rm
        state._role_lane_messages = rlm
    except Exception as exc:  # pragma: no cover — defensive
        log.warning(
            "role-message reconstruction from %s failed: %s; "
            "leaving _role_messages=None",
            getattr(recorder, "db_path", "<unknown>"), exc,
        )


def _emit_council_complete(recorder, *, sid: str, status: str,
                           final_answer: str = "") -> None:
    """Persist a durable ``council_complete`` row to runtime_events
    BEFORE the recorder is finalized/closed. The M9 SSE bridge emits a
    synthetic ``complete`` frame to live consumers at termination; this
    row is the replayable record so a consumer reconnecting with
    Last-Event-ID after the stream closed still learns the council
    finished (and whether a final answer exists). Best-effort — must
    never mask the consultation result."""
    if recorder is None or not hasattr(recorder, "record_event"):
        return
    try:
        recorder.record_event(
            kind="council_complete",
            payload={
                "sid": sid,
                "status": status,
                "final_answer_present": bool((final_answer or "").strip()),
            },
        )
    except Exception:  # pragma: no cover — defensive
        log.exception(
            "record_event(council_complete) failed for sid=%s", sid,
        )


def _lane_workspace(registry, cwd: str):
    """``lane -> (host view dir, uid)`` from the session's shell provider
    in write mode (plan E), else None — the coder writes and runs its
    tests in its own worktree view only when there is one to give."""
    for p in getattr(registry, "providers", None) or ():
        fn = getattr(p, "workspace", None)
        mode = getattr(getattr(p, "settings", None), "mode", "")
        if callable(fn) and mode == "write":
            return lambda lane, _fn=fn: _fn(cwd, lane)
    return None


def _track_task(state, running: dict, ev: dict) -> None:
    """One ``tasks`` stream event: a role is ``in_progress`` while any of
    its tasks runs and ``done`` when the last one ends."""
    name = ev.get("name")
    if not name or name not in state.progress:
        return
    if "input" in ev:                       # a task started
        running[name] = running.get(name, 0) + 1
        state.progress[name] = "in_progress"
    else:                                   # a task ended
        running[name] = max(0, running.get(name, 0) - 1)
        if not running[name]:
            state.progress[name] = "done"


def _rewind_budget_ok(snap) -> bool:
    """#314: is there room for one more researcher round? Delegates to
    the shared pure check in server.control so the inject handler and
    the runner agree on the bound."""
    from consultants.server.control import rewind_budget_ok
    return rewind_budget_ok(getattr(snap, "values", {}) or {})


def _read_adversary_checkpoint_cfg(cfg) -> tuple[bool, float]:
    """M2: pull the opt-in adversary-checkpoint knobs off ``cfg`` with a
    defensive fallback to OFF / 600 s. Returns ``(enabled, timeout_s)``.
    No effort gate — a pause the operator turned on fires on any tier
    (default OFF keeps cohort-2 parity)."""
    try:
        enabled = bool(getattr(cfg, "adversary_checkpoint", False))
    except Exception:  # pragma: no cover — defensive
        enabled = False
    try:
        timeout_s = float(getattr(cfg, "adversary_checkpoint_timeout_s", 600))
        if timeout_s < 1.0:
            timeout_s = 600.0
    except Exception:  # pragma: no cover — defensive
        timeout_s = 600.0
    return enabled, timeout_s


def _rewind_to_researcher(compiled, thread_config, state, recorder,
                          log_label: str) -> None:
    """#314: re-enter the graph at the researcher for one more pass.

    ``update_state(as_node="rewind")`` runs the graph's rewind router:
    one more researcher pass on the existing plan and every report so
    far, carrying the injected brief already written into
    ``additional_context``, then the researcher's own edges (the critic
    on high/max) and the synthesizer interrupt again. A graph built
    before the node existed falls back to ``START`` — which, on a
    council graph, means the planner (#1626: a re-plan that dropped the
    brief and the round-1 reports). Empty values: we only redirect
    flow; the inject already wrote the content."""
    from langgraph.graph import START
    from consultants.engine.graph import REWIND_NODE
    nodes = getattr(compiled, "nodes", None) or {}
    as_node = REWIND_NODE if REWIND_NODE in nodes else START
    compiled.update_state(thread_config, {}, as_node=as_node)
    try:
        if "researcher" in state.progress:
            state.progress["researcher"] = "in_progress"
    except Exception:  # pragma: no cover
        pass
    if recorder is not None and hasattr(recorder, "record_event"):
        try:
            recorder.record_event(
                kind="rewind", role="researcher",
                payload={"sid": state.sid, "as_node": str(as_node),
                         "reason": "mid-flight inject revalidation"},
            )
        except Exception:  # pragma: no cover
            log.exception("record_event(rewind) failed sid=%s", state.sid)
    log.info("%s: rewinding to researcher for revalidation (sid=%s)",
             log_label, state.sid)


def _record_awaiting_adversary(recorder, *, state, deadline_ts, timeout_s,
                               self_confidence, log_label: str) -> None:
    """Emit (best-effort) + record the M2 ``awaiting_adversary`` event.

    The recorder write is the load-bearing path: here the runner sits
    *between* graph streams, NOT inside a runnable node, so
    ``events.emit`` finds no dispatch context and no-ops. The SSE
    ``GET /events`` endpoint replays the recorder row instead (and a
    reconnecting consumer picks it up via ``Last-Event-ID``), so a lost
    live frame never strands the consumer — the runner owns the
    deadline regardless."""
    payload = {
        "sid": state.sid,
        "deadline_ts": deadline_ts,
        "timeout_s": timeout_s,
        "self_confidence": self_confidence,
        "reason": "adversary_checkpoint",
        "kind": "awaiting_adversary",
        "ts": time.time(),
    }
    try:
        from consultants.engine.events import AwaitingAdversary, emit
        emit(AwaitingAdversary(
            sid=state.sid, deadline_ts=deadline_ts, timeout_s=timeout_s,
            self_confidence=self_confidence,
        ))
    except Exception:  # pragma: no cover — emit is best-effort
        pass
    if recorder is not None and hasattr(recorder, "record_event"):
        try:
            recorder.record_event(kind="awaiting_adversary", payload=payload)
        except Exception:  # pragma: no cover
            log.exception("record_event(awaiting_adversary) failed sid=%s",
                          state.sid)


def _await_adversary_checkpoint(*, state, final_state, recorder, timeout_s,
                                log_label: str, now_fn=time.time,
                                sleep_fn=time.sleep,
                                poll_interval: float = 0.5) -> bool:
    """Park at the synthesizer interrupt for an assistant-authored
    adversarial challenge. Block until the consumer acks (early release)
    or the deadline passes (auto-resume). Returns True if acked.

    The runner is the **sole resumer** — it never hands control to
    ``POST /resume``'s executor here — so there is no double-resume
    race (the caller's ``_adversary_checkpoint_active`` flag keeps the
    /resume guard armed across the whole runner-owned span). Injections
    that arrive during the wait are applied synchronously by the
    live-graph ``POST /inject`` handler (the graph is parked, not
    streaming), so by the time this returns ``state.take_revalidation()``
    already reflects any brief the consumer pushed (``role=researcher``
    → a rewind request; ``role=synthesizer`` → in-place text the
    synthesizer reads on resume). ``now_fn`` / ``sleep_fn`` are
    injectable so tests drive a mocked clock without real sleeping.

    A ``POST /adversary-ack`` that arrives BEFORE this opens (a consumer
    that pre-decides "proceed, no challenge") is honored: the ack flag is
    NOT cleared on entry, so the first poll consumes it and releases
    immediately. Fire-once means there is no prior checkpoint whose stale
    ack could leak in."""
    deadline = now_fn() + max(1.0, float(timeout_s))
    self_conf = None
    try:
        from consultants.engine.state_v2 import latest_confidence
        self_conf = latest_confidence(final_state)
    except Exception:  # pragma: no cover — confidence is optional
        self_conf = None
    with state._inject_lock:
        state._checkpoint_deadline_ts = deadline
    _record_awaiting_adversary(
        recorder, state=state, deadline_ts=deadline, timeout_s=timeout_s,
        self_confidence=self_conf, log_label=log_label,
    )
    log.info("%s: adversary checkpoint open (sid=%s deadline=%.0f timeout=%ss)",
             log_label, state.sid, deadline, timeout_s)
    acked = False
    why = "timeout"
    while now_fn() < deadline:
        if state.take_adversary_ack():
            acked = True
            why = "ack"
            break
        # Break promptly if the session is cancelled / closed (explicit
        # /cancel sets the ack; discard-cancel and the idle reaper set
        # ``closed``). Without this the runner thread would block for the
        # full timeout on a session that's already gone — the park does
        # not bump activity, so a long pause is itself idle-reapable.
        if getattr(state, "closed", False):
            why = "closed"
            break
        # An engine shutdown ends the park; the resumed run opens the
        # checkpoint again, so the consumer still gets its window.
        rc = getattr(state, "_run_control", None)
        if rc is not None and rc.suspended:
            why = "suspended"
            break
        remaining = deadline - now_fn()
        if remaining <= 0:
            break
        sleep_fn(min(poll_interval, remaining))
    with state._inject_lock:
        state._checkpoint_deadline_ts = None
    # Run 6 (mail #1646): the record had the opening and the inject but
    # not the close, so a reader of the record saw a checkpoint open for
    # the rest of the run.
    if recorder is not None and hasattr(recorder, "record_event"):
        try:
            recorder.record_event(kind="adversary_released", payload={
                "sid": state.sid, "kind": "adversary_released",
                "reason": why, "acked": acked, "ts": time.time()})
        except Exception:  # pragma: no cover
            log.exception("record_event(adversary_released) failed sid=%s",
                          state.sid)
    log.info("%s: adversary checkpoint closed (sid=%s acked=%s why=%s)",
             log_label, state.sid, acked, why)
    return acked


def _open_checkpointer(state, cfg, cwd: str, *, memory_saver_cls):
    """The run's checkpointer: durable (``[checkpointer]`` config, SQLite
    per session by default) so an engine restart can resume it, falling
    back to the in-memory saver when the durable one cannot be opened —
    a council that cannot be resumed is still better than no council.

    The handle is kept on ``state`` so the session can release it.
    """
    from consultants.engine.state_v2 import make_checkpointer_serde
    serde = make_checkpointer_serde()
    try:
        from consultants.engine.checkpointer import (
            CheckpointerConfig as _EngineCfg, make_checkpointer,
        )
        ck = getattr(cfg, "checkpointer", None)
        engine_cfg = _EngineCfg(
            backend=getattr(ck, "backend", "sqlite") or "sqlite",
            url=getattr(ck, "url", None),
        )
        pool = None
        if engine_cfg.backend == "postgres":
            pool = _postgres_pool(engine_cfg, ck)
        handle = make_checkpointer(engine_cfg, Path(cwd), state.sid,
                                   postgres_pool=pool, serde=serde)
        state._checkpointer_handle = handle
        return handle.saver
    except Exception:
        log.exception("sid=%s: durable checkpointer unavailable; using the "
                      "in-memory one (this run will not survive an engine "
                      "restart)", state.sid)
    if memory_saver_cls is None:
        return None
    return memory_saver_cls(serde=serde)


_PG_POOL = None
_PG_POOL_LOCK = threading.Lock()


def _postgres_pool(engine_cfg, ck):
    """One pool per engine process, built on first use."""
    global _PG_POOL
    with _PG_POOL_LOCK:
        if _PG_POOL is None:
            from consultants.engine.checkpointer import make_postgres_pool
            engine_cfg.postgres_pool_min = getattr(ck, "postgres_pool_min", 1)
            engine_cfg.postgres_pool_max = getattr(ck, "postgres_pool_max", 10)
            engine_cfg.postgres_pool_timeout_s = getattr(
                ck, "postgres_pool_timeout_s", 30.0)
            _PG_POOL = make_postgres_pool(engine_cfg)
        return _PG_POOL


def _has_checkpoint(compiled, thread_config) -> bool:
    try:
        snap = compiled.get_state(thread_config)
    except Exception:
        return False
    return bool(getattr(snap, "values", None)) or bool(
        getattr(snap, "next", None))


def _mark_suspended(state, recorder, exc, *, log_label: str) -> None:
    """The run stopped for an engine shutdown. Nothing is finalized: the
    transcript stays open-ended and the checkpoint stays, so the next
    engine resumes the same sid where this one stopped."""
    log.warning("%s: sid=%s suspended (%s); it resumes when the engine "
                "restarts", log_label, state.sid, exc)
    state.status = "suspended"
    state.error = None
    if recorder is not None:
        try:
            recorder.close()
        except Exception:  # pragma: no cover — defensive
            log.exception("recorder.close raised on suspend")


def _tried_docs(runner_input: dict) -> list:
    """Plan G5: what the asking assistant already tried, as one Doc every
    role reads (``additional_context``); ``[]`` when nothing was sent."""
    tried = runner_input.get("tried") or []
    if not tried:
        return []
    from consultants.engine import council as council_mod
    from consultants.engine.state_v2 import Doc
    return [Doc("any", council_mod.render_tried(tried), source="already_tried")]


def _seed_runtime_control(cfg, enabled, *, n_fanout_extras: int = 0) -> dict:
    """The run's boot-time ``runtime_control``.

    Until 2026-09-30 nothing seeded it, and every consumer treats an
    absent record as "not configured": the researcher's stall monitor
    never wrapped a call, the deadline never fired and POST /control
    mutated caps no node had been started with. ``enabled_roles`` is the
    roles this run actually has — the runner drops the critic below high
    effort, and the config-derived default would quietly put it back for
    any reader that consults the live record.
    """
    from consultants.engine.control import runtime_control_defaults
    rc = runtime_control_defaults(cfg, effort=cfg.effort,
                                  n_fanout_extras=n_fanout_extras)
    rc["enabled_roles"] = list(enabled)
    return dict(rc)


def _live_tool_permissions(state):
    """The per-call source of ``runtime_control.tool_permissions`` for
    the tool gate: ``RunControl``'s live copy, which ``POST /control``
    writes. ``None`` for a session without one (test doubles), which
    leaves the gate exactly as it was."""
    rc = getattr(state, "run_control", None)
    fn = getattr(rc, "tool_permissions", None)
    return fn if callable(fn) else None


def _restore_tool_permissions(compiled, thread_config, state) -> None:
    """On resume, put the checkpoint's operator permissions back on the
    new process's ``RunControl``: it starts empty, and the gate reads
    only the live copy."""
    rc = getattr(state, "run_control", None)
    if rc is None or not hasattr(rc, "set_tool_permissions"):
        return
    try:
        values = compiled.get_state(thread_config).values or {}
        perms = (values.get("runtime_control") or {}).get("tool_permissions")
    except Exception:
        log.debug("no checkpoint state to restore tool permissions from",
                  exc_info=True)
        return
    if isinstance(perms, dict) and perms:
        rc.set_tool_permissions(perms)


def _drive_council_stream(compiled, initial, thread_config, *,
                          state, enabled, review_before_synthesis: bool,
                          recorder, log_label: str,
                          adversary_checkpoint: bool = False,
                          adversary_checkpoint_timeout_s: float = 600.0,
                          resume: bool = False,
                          now_fn=time.time, sleep_fn=time.sleep) -> dict:
    """Stream the compiled graph to completion through the always-on
    ``interrupt_before=["synthesizer"]`` pause (#314).

    Behaviors at the synthesizer interrupt boundary, in priority order:

    - **rewind** — a synthesis-phase inject set ``_revalidation_pending``
      and the round budget allows: re-enter the researcher for one more
      pass (``_rewind_to_researcher``), then loop. Bounded by the round
      cap; a second pending request after the cap is exhausted falls
      through to auto-resume (best-effort, the inject's Doc still
      reaches the synthesizer as text).
    - **adversary checkpoint** (M2, opt-in) — when
      ``adversary_checkpoint`` is enabled, pause ONCE per consultation
      and block in ``_await_adversary_checkpoint`` until the consumer
      acks or the deadline passes. A brief injected during the pause is
      honored via the rewind path on the way out (preserving x-tier:
      the fanned researcher+critics re-run with the brief). Default OFF
      → this branch is never entered → byte-identical to today.
    - **HITL park** — ``review_before_synthesis`` opted in and no
      rewind pending: leave the graph parked for an external /resume
      (M5 behavior, unchanged).
    - **auto-resume** — the default: ``stream(None)`` past the
      interrupt so the synthesizer runs and the graph reaches END.
      Artifacts are byte-identical to the pre-#314 single-stream path
      (M12 parity) — the only difference is the interrupt is realized
      via a second stream call.

    Stub graphs in tests whose ``stream`` runs to completion and whose
    ``get_state`` is absent / raises / returns no ``next`` fall through
    the exception/empty guards and return after one stream — identical
    to the pre-#314 loop, so existing fakes need no changes.

    Returns the last ``values`` payload (``final_state``).
    """
    final_state: dict = dict(initial)
    stream_input: Any = initial
    if resume and _has_checkpoint(compiled, thread_config):
        # Continue from the last saved superstep. Tasks of the
        # interrupted superstep that had finished are pending writes in
        # the checkpoint and are not re-run; only the rest are.
        log.warning("%s: resuming sid=%s from its checkpoint",
                    log_label, state.sid)
        stream_input = None
        _restore_tool_permissions(compiled, thread_config, state)
    elif resume:
        log.warning("%s: sid=%s has no checkpoint to resume from; "
                    "starting it again", log_label, state.sid)
    rc = getattr(state, "run_control", None)

    def _check_suspend() -> None:
        if rc is not None and getattr(rc, "suspended", False):
            raise RunSuspended("suspended at a stream boundary")

    checkpoint_fired = False  # M2: fire the adversary checkpoint once
    MAX_RESUMES = 64  # safety bound vs. a pathological rewind loop
    # A role is done when its last running task ends, not when its first
    # fanned-out lane reports (mail #1625: researcher "done" while 6 of
    # 8 lanes were still making calls). LangGraph's ``tasks`` stream
    # marks every task's start and end; a graph that emits none (a stub)
    # keeps the per-update rule below.
    running: dict[str, int] = {}
    saw_tasks = False
    try:
        for _ in range(MAX_RESUMES):
            _check_suspend()
            for mode, payload in compiled.stream(
                    stream_input, config=thread_config,
                    stream_mode=["updates", "values", "tasks"]):
                if mode == "values" and isinstance(payload, dict):
                    final_state = payload
                    continue
                if mode == "tasks" and isinstance(payload, dict):
                    saw_tasks = True
                    _track_task(state, running, payload)
                    continue
                if mode != "updates" or not isinstance(payload, dict) \
                        or saw_tasks:
                    continue
                for node, partial in payload.items():
                    if node in state.progress:
                        state.progress[node] = "done"
                        try:
                            idx = enabled.index(node)
                            for i in range(idx + 1, len(enabled)):
                                if state.progress.get(enabled[i]) == "pending":
                                    state.progress[enabled[i]] = "in_progress"
                                    break
                        except ValueError:
                            pass
            # Stream drained — where did we stop?
            try:
                snap = compiled.get_state(thread_config)
                nxt = tuple(getattr(snap, "next", ()) or ())
            except Exception:
                # Stub graph without checkpointer introspection, or no
                # checkpointer attached — treat the drain as completion
                # (identical to the pre-#314 single-stream behavior).
                return final_state
            if not nxt:
                return final_state  # reached END
            if "synthesizer" in nxt:
                # Parked at the always-on synthesizer interrupt.
                if state.take_revalidation() and _rewind_budget_ok(snap):
                    _rewind_to_researcher(
                        compiled, thread_config, state, recorder, log_label,
                    )
                    stream_input = None
                    continue
                if adversary_checkpoint and not checkpoint_fired:
                    # M2: pause ONCE for an assistant-authored adversarial
                    # challenge. The runner blocks here (sole resumer) until
                    # the consumer acks or the deadline passes.
                    #
                    # Arm the sole-resumer guard BEFORE the pause and keep
                    # it armed across the post-pause rewind / auto-resume
                    # re-stream (cleared in the finally). The deadline_ts
                    # alone is too narrow — it goes None the instant the
                    # wait ends, leaving the re-stream window where a
                    # concurrent /resume would double-resume the live
                    # runner (the adversarial-review BLOCKER).
                    checkpoint_fired = True
                    with state._inject_lock:
                        state._adversary_checkpoint_active = True
                    _await_adversary_checkpoint(
                        state=state, final_state=final_state,
                        recorder=recorder,
                        timeout_s=adversary_checkpoint_timeout_s,
                        log_label=log_label, now_fn=now_fn, sleep_fn=sleep_fn,
                    )
                    _check_suspend()
                    if getattr(state, "closed", False):
                        # /cancel --discard or the idle reaper closed the
                        # session during the park — don't re-stream a
                        # discarded checkpoint.
                        return final_state
                    # A brief injected during the pause may have requested
                    # a rewind (role=researcher). Honor it via the existing
                    # path so the fanned researcher+critics re-run with the
                    # brief (x-tier preserved); else fall through to resume,
                    # where a role=synthesizer brief is read in place.
                    # Re-read the state: a brief may have raised the round
                    # cap during the pause (rewound_extra_round), which the
                    # snapshot taken before it cannot show.
                    try:
                        snap = compiled.get_state(thread_config)
                    except Exception:  # pragma: no cover — defensive
                        pass
                    if state.take_revalidation() and _rewind_budget_ok(snap):
                        _rewind_to_researcher(
                            compiled, thread_config, state, recorder,
                            log_label,
                        )
                    stream_input = None
                    continue
                if review_before_synthesis:
                    # M5 HITL: leave parked for an external /resume.
                    return final_state
                stream_input = None  # auto-resume → synthesizer → END
                continue
            # Parked at an unexpected node — resume defensively.
            stream_input = None
        log.warning("%s: stream exceeded MAX_RESUMES (sid=%s)",
                    log_label, state.sid)
        return final_state
    finally:
        # M2: disarm the sole-resumer guard the moment the runner stops
        # owning the graph — END, HITL park, MAX_RESUMES, or a crash. Past
        # this point /resume is the legitimate resumer again (or the
        # session is terminal and /resume 409s).
        if checkpoint_fired:
            with state._inject_lock:
                state._adversary_checkpoint_active = False


def _finalize_recorder(recorder, *, status: str,
                       error: Optional[str] = None,
                       finished_at: Optional[float] = None) -> None:
    """Best-effort finalize + close. Safe to call with ``None`` (the
    construction-failed path) or after the consultation has already
    finalized — `finalize` is idempotent, `close` is too."""
    if recorder is None:
        return
    try:
        recorder.finalize(
            status=status, error=error, finished_at=finished_at,
        )
    except Exception as exc:  # pragma: no cover — recorder must not mask
        log.warning("recorder.finalize raised: %s", exc)
    try:
        recorder.close()
    except Exception as exc:  # pragma: no cover
        log.warning("recorder.close raised: %s", exc)


def _make_run_control_emit(state, recorder, *, log_label: str):
    """Bridge the cancel/pause gate's events onto the recorder.

    Same discipline as ``_make_tool_approval_fn``'s emitter: the
    recorder row is the load-bearing path because ``GET /events``
    replays rows, so a consumer that attaches late still sees that a
    node parked. Failing to record must never break the gate — the run
    control owns the decision regardless of who is listening.
    """
    def _emit(kind: str, payload: dict) -> None:
        if recorder is None or not hasattr(recorder, "record_event"):
            return
        body = dict(payload)
        body.setdefault("kind", kind)
        body.setdefault("sid", getattr(state, "sid", ""))
        body.setdefault("ts", time.time())
        try:
            recorder.record_event(kind=kind, payload=body)
        except Exception:  # pragma: no cover — defensive
            log.exception("%s: record_event(%s) failed", log_label, kind)
    return _emit


def _make_tool_approval_fn(state, recorder, cfg, cwd: str,
                           *, log_label: str):
    """Build the ``approval_fn`` the tool registry calls on an ``ask_*``.

    Without one the registry refuses every gated call — correct, but
    silent, and it was the last unwired piece of M-A
    (``docs/PLAN-council-tool-surface.md``). ``ask_assistant``
    auto-approves per the plan's ladder; ``ask_human`` parks the lane
    and denies on timeout, because absence of a human never authorizes
    spend.
    """
    from consultants.engine.tool_approval import (
        DEFAULT_APPROVAL_TIMEOUT_S, ApprovalContext, make_approval_fn,
    )

    tools_cfg = getattr(cfg, "tools", None)
    timeout_s = float(getattr(
        tools_cfg, "approval_timeout_s", DEFAULT_APPROVAL_TIMEOUT_S)
        if tools_cfg is not None else DEFAULT_APPROVAL_TIMEOUT_S)

    def _emit(kind: str, payload: dict) -> None:
        # The recorder row is the load-bearing path, exactly as in
        # ``_record_awaiting_adversary``: GET /events replays recorder
        # rows, so a consumer that reconnects (or attaches late) still
        # sees the parked request via Last-Event-ID. Failing to record
        # must never break the run — the runner owns the deadline
        # regardless of who is listening.
        if recorder is None or not hasattr(recorder, "record_event"):
            return
        body = dict(payload)
        body.setdefault("kind", kind)
        body.setdefault("sid", getattr(state, "sid", ""))
        body.setdefault("ts", time.time())
        try:
            recorder.record_event(kind=kind, payload=body)
        except Exception:  # pragma: no cover — defensive
            log.exception("%s: record_event(%s) failed", log_label, kind)

    ctx = ApprovalContext(
        broker=state.tool_approvals,
        cwd=cwd,
        timeout_s=timeout_s,
        emit=_emit,
        is_closed=lambda: bool(getattr(state, "closed", False)),
    )
    return make_approval_fn(ctx)


def _preflight_refused(state, question: str, *, cwd: str,
                       extra_roots: tuple, cwd_display: str,
                       extra_roots_display: tuple,
                       label: str, skip: bool = False) -> bool:
    """Run the path pre-flight; on a blocking verdict mark the session
    failed, write the artifacts, and return True so the caller returns
    without spending anything.

    ``skip=True`` (the CLI's ``--skip-preflight``) bypasses the check
    entirely, for the greenfield ask whose every named path is one the
    asker wants created — indistinguishable from wrong roots by
    inspection alone. It is a cost guard, not a security boundary: the
    tool sandbox still confines every read to the allowed roots.

    Never raises: a pre-flight that itself breaks must not be able to
    stop a run that would otherwise have worked — the check exists to
    save money, not to become a new failure mode.
    """
    if skip:
        # --skip-preflight. Logged, not silent: the next person reading
        # a run full of "[unverified]" cites needs to know the guard
        # was turned off rather than that it passed.
        log.warning("%s sid=%s preflight SKIPPED by request",
                    label, state.sid)
        return False
    try:
        from consultants.engine.preflight import check_question_paths
        pf = check_question_paths(
            question, roots=[cwd, *extra_roots],
        )
    except Exception:  # pragma: no cover — defensive
        log.exception("preflight raised; continuing without it")
        return False

    if not pf.blocking:
        warn = pf.warning()
        if warn:
            log.warning("%s sid=%s preflight: %s",
                        label, state.sid, warn)
        return False

    msg = pf.message(
        display_roots=[cwd_display, *extra_roots_display],
    )
    log.error("%s sid=%s %s", label, state.sid, msg)
    state.status = "failed"
    state.error = msg
    state.finished_at = time.time()
    for r in list(state.progress):
        state.progress[r] = "done"
    try:
        _write_failed_artifacts(state, cwd, question, RuntimeError(msg))
    except Exception:  # pragma: no cover — defensive
        log.exception("preflight failed-artifact write failed")
    return True


def _write_failed_artifacts(state, cwd: str, question: str,
                            exc: Exception) -> None:
    """When the graph itself crashes (vs. an individual role), still
    write summary + metadata so the user can see what happened."""
    result = storage.ConsultationResult(
        session_id=state.sid,
        created=time.strftime(
            "%Y-%m-%dT%H:%M:%S",
            time.localtime(state.started_at)),
        question=question,
        models={},
        topology=state.topology,
        effort=state.effort,
        final_answer=f"(consultation failed: {exc})",
        turns=[],
        duration_seconds=time.time() - state.started_at,
        status="failed",
        error=str(exc),
        cwd=cwd,
        total_prompt_tokens=0,
        total_completion_tokens=0,
        retries_by_role={},
        root_sid=getattr(state, "root_sid", None) or state.sid,
    )
    try:
        storage.write_consultation(result, cwd=Path(cwd))
    except Exception as e:  # pragma: no cover
        log.warning("failed-artifact write failed: %s", e)


def _start_progress(state, enabled, runner_input: dict) -> None:
    """Mark where the run starts. A resumed run starts after the roles it
    already finished: they are ``done`` from its own transcript.db, never
    the first role re-marked ``in_progress`` (the graph does not re-enter
    a finished planner, so nothing flipped it back —
    csl-2026-10-25-0559-4d81 showed the planner running to the end)."""
    if not enabled:
        return
    if not runner_input.get("resume"):
        state.progress[enabled[0]] = "in_progress"
        return
    for role in _exited_roles(state.cwd, state.sid):
        if role in state.progress:
            state.progress[role] = "done"


def _exited_roles(cwd: str, sid: str) -> set:
    """Roles with a ``node_exit`` row in the run's transcript.db; empty
    when it cannot be read."""
    import sqlite3
    from consultants.engine import storage as _storage
    db = (Path(cwd) / ".claude-hooks" / "consultants" / sid
          / _storage.TRANSCRIPT_DB_FILENAME)
    if not db.exists():
        return set()
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            rows = con.execute("SELECT DISTINCT role FROM runtime_events "
                               "WHERE kind = 'node_exit'").fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return set()
    return {r for (r,) in rows if r}


def _polykv_manager(cfg, state, cwd: str, runner_input: dict, *,
                    adopt: bool = False):
    """The run's PolyKV manager (plan F3), or ``None`` when ``[polykv]`` is
    off, which leaves every role client unwrapped and every request as it
    was. A follow-up (``adopt``) takes the manager its consultancy holds."""
    pk = getattr(cfg, "polykv", None)
    if pk is None or not pk.enabled or pk.mode == "off":
        return None
    mgr = polykv_mod.start_run(
        state.sid, mode=pk.mode, cwd=cwd,
        layout=getattr(pk, "layout", "shared"),
        resume=bool(runner_input.get("resume")),
        adopt_root=getattr(state, "root_sid", None) if adopt else None)
    state._polykv = mgr
    return mgr


def _run_stop(state):
    """The run's cancel as each call's ``cancel_check``: the node gate
    only skips nodes not yet started, so a call already retrying a dead
    server kept loading the model there for the whole retry budget
    after the council was cancelled (arm B, 2026-10-24)."""
    def stop() -> bool:
        rc = getattr(state, "run_control", None)
        return bool(rc is not None and rc.cancelled)
    return stop


def _role_client(cfg, role: str, ollama_base_url: str):
    """The chat client for ``role``. Its fan-out lanes and fallbacks
    reuse it with another ``model``, so when those models live on other
    backends (an ``@endpoint``, a ``llamafile://`` label) the client
    routes each call by its model; otherwise it is the plain client."""
    from claude_hooks.get_advice.chat_client import make_role_chat_client
    rc = cfg.roles[role]
    models = [rc.model, *(rc.extra_models or [])]
    expert = getattr(getattr(cfg, "escalation", None), "expert_model", "")
    if role == "researcher" and expert and expert not in models:
        # Per-lane escalation reruns a researcher lane on the expert model
        # through this client, so it must route there too.
        models.append(expert)
    return make_role_chat_client(
        models, ollama_base_url,
        endpoints=getattr(cfg, "endpoints", None) or {})


def default_ollama_url() -> str:
    """The Ollama base URL the consultants service uses:
    :func:`claude_hooks.ollama_upstream.url` (env override, then the
    host's ``ollama.url``, then Ollama's default)."""
    from claude_hooks import ollama_upstream
    return ollama_upstream.url()
