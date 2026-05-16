"""Production council runner — bridges SessionState to LangGraph.

Lazy-imports the engine so this module can sit on disk without
requiring LangChain in the test env. Production code calls
``make_runner()`` to get a ``run_council`` callable suitable for
``create_app(run_council=...)``.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

from consultants import config as cc
from consultants.engine import council as council_mod
from consultants.engine import storage

log = logging.getLogger("consultants.server.runner")


def make_runner(*, ollama_base_url: str):
    """Return a ``run_council(state, runner_input)`` callable.

    The runner builds a LangGraph at call time using the config in
    ``runner_input`` and the per-role models. Each role gets its own
    ChatClient for clean usage tracking.
    """
    # Lazy imports — none of these are present in the main test env.
    from claude_hooks.allowed_roots import (
        discover_allowed_roots, render_for_log,
    )
    from claude_hooks.get_advice.chat_client import ChatClient, make_agent_chat_client
    from claude_hooks.caliber_proxy.tools import (
        openai_tool_specs, make_executor,
    )
    from claude_hooks.caliber_proxy.prompt import build_grounding_messages
    from consultants.engine.graph import GraphDeps, build_council_graph
    from consultants.engine.recorder import MessageRecorder, RecorderMeta
    from consultants.engine.trace import (
        Tracer, TracedChat, traced_tool, traced_node,
    )

    def run_council(state, runner_input: dict) -> None:
        cfg: cc.ConsultantsConfig = runner_input["config"]
        cwd: str = runner_input["cwd"]
        question: str = runner_input["question"]
        # v1.8+: extra allowed roots for the tool sandbox. Sourced from
        # the CLI's ``--add-dir`` (already merged with settings-file
        # auto-discovery by the HTTP layer). Empty tuple → tool layer
        # falls back to the bare ``execute`` fast path.
        extra_roots = tuple(runner_input.get("extra_roots") or ())
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
        if cc.base_effort(cfg.effort) in ("low", "medium") \
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
        chat_clients = {
            r: TracedChat(
                make_agent_chat_client(cfg.roles[r].model, ollama_base_url),
                role=r, tracer=tracer,
            )
            for r in enabled
        }
        models = {r: cfg.roles[r].model for r in enabled}

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
            log.info(
                "consultants sid=%s allowed roots:\n%s",
                state.sid, render_for_log([cwd, *extra_roots]),
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

        deps = GraphDeps(
            chat_clients=chat_clients,
            models=models,
            enabled_roles=enabled,
            cwd=cwd,
            tool_executor=traced_tool(
                make_executor(extra_roots), tracer=tracer,
            ),
            tool_specs=openai_tool_specs(),
            grounding_msgs=grounding_msgs,
            think_by_role=think_by_role,
            synthesizer_self_critic=synthesizer_self_critic,
            disable_cache=disable_cache,
            recorder=recorder,
            extra_models_by_role=extra_models_by_role,
            synthesizer_fallback_models=synthesizer_fallback,
            store=consultants_store,
            sid=state.sid,
            # M10: coder sandbox caps. Consulted only when ``coder``
            # is in ``enabled`` (otherwise the coder node is never
            # registered, so these values are irrelevant).
            coder_max_file_bytes=cfg.coder_limits.max_file_bytes,
            coder_max_total_bytes=cfg.coder_limits.max_total_bytes,
            coder_max_files=cfg.coder_limits.max_files,
        )
        # M5: static review-before-synthesis interrupt. When the
        # user opted in via cfg.runtime.review_before_synthesis,
        # compile with interrupt_before=["synthesizer"] so the
        # graph pauses just before the final-answer node. The HTTP
        # /state endpoint exposes the partial research; the human
        # POSTs /inject + /resume to continue.
        #
        # The kwarg is only passed when actually opted in — this
        # keeps the call signature bit-for-bit identical to v1 for
        # the default-config path, so test stubs that mock
        # build_council_graph with a fake `(deps, tracer=...)`
        # signature don't break.
        interrupt_before: Optional[list[str]] = None
        try:
            if bool(getattr(cfg.runtime, "review_before_synthesis", False)):
                interrupt_before = ["synthesizer"]
        except AttributeError:
            interrupt_before = None
        if interrupt_before:
            compiled = build_council_graph(
                deps, tracer=tracer,
                interrupt_before=interrupt_before,
            )
        else:
            compiled = build_council_graph(deps, tracer=tracer)

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
        if enabled:
            state.progress[enabled[0]] = "in_progress"

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
        try:
            for mode, payload in compiled.stream(
                    initial, config=thread_config,
                    stream_mode=["updates", "values"]):
                if mode == "values" and isinstance(payload, dict):
                    final_state = payload
                    continue
                if mode != "updates" or not isinstance(payload, dict):
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
        except Exception as e:
            log.exception("council graph invocation failed: %s", e)
            state.status = "failed"
            state.error = f"graph crashed: {e}"
            state.finished_at = time.time()
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
            final_answer=final_state.get("final_answer", ""),
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
        state.final_answer = final_state.get("final_answer") or ""
        state.models = dict(models)
        state._chat_clients = {
            r: getattr(c, "_client", c) for r, c in chat_clients.items()
        }
        state.bump_activity()

        state.status = terminal_status
        state.error = node_error
        state.finished_at = time.time()
        if node_failed:
            log.warning("council finished with role failure: %s",
                        node_failed)
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
        discover_allowed_roots, render_for_log,
    )
    from claude_hooks.get_advice.chat_client import ChatClient, make_agent_chat_client
    from claude_hooks.caliber_proxy.tools import (
        openai_tool_specs, make_executor,
    )
    from claude_hooks.caliber_proxy.prompt import build_grounding_messages
    from consultants.engine.graph import GraphDeps, build_follow_up_graph
    from consultants.engine.recorder import MessageRecorder, RecorderMeta
    from consultants.engine.trace import (
        Tracer, TracedChat, traced_tool, traced_node,
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

        # Topology: researcher + synthesizer always; critic only at
        # high/max effort (matches the main runner's gate). x-tiers
        # mirror their base tier: xhigh / xmax keep critic, xmedium
        # drops it. The parent's ``enabled_roles`` may have
        # included critic but the follow-up topology decides
        # independently based on this follow-up's effort.
        enabled = ["researcher", "synthesizer"]
        if cc.base_effort(cfg.effort) in ("high", "max") \
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
        chat_clients = {}
        for role in enabled_t:
            raw = warm_clients.get(role)
            if raw is None:
                # Parent's clients went away (closed, reaped, or
                # the parent didn't run that role). Cold-start —
                # the follow-up still works but pays the
                # /api/show probe cost on first use.
                # v1.5+: dispatch based on the role's model_ref so
                # cold-start follow-ups land on the right backend.
                role_model = cfg.roles[role].model
                raw = make_agent_chat_client(role_model, ollama_base_url)
                log.info(
                    "follow-up %s: cold ChatClient for role=%s "
                    "(no warm parent client; model=%s)",
                    state.sid, role, role_model,
                )
            chat_clients[role] = TracedChat(raw, role=role, tracer=tracer)

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
        if (parent_state is not None
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

        if extra_roots:
            log.info(
                "consultants follow-up sid=%s allowed roots:\n%s",
                state.sid, render_for_log([cwd, *extra_roots]),
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

        deps = GraphDeps(
            chat_clients=chat_clients,
            models=models,
            enabled_roles=enabled_t,
            cwd=cwd,
            tool_executor=traced_tool(
                make_executor(extra_roots), tracer=tracer,
            ),
            tool_specs=openai_tool_specs(),
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
        )
        compiled = build_follow_up_graph(deps, tracer=tracer)
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
        initial["plan"] = (
            parent_state.plan
            if parent_state is not None and parent_state.plan
            else "(follow-up: focus on the question above)"
        )
        initial["plan_item"] = question
        initial["lane_idx"] = 0
        if prior_messages_by_role:
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

        if enabled:
            state.progress[enabled[0]] = "in_progress"

        # Same dual-mode streaming pattern as run_council.
        final_state: dict = dict(initial)
        try:
            for mode, payload in compiled.stream(
                    initial, config=thread_config,
                    stream_mode=["updates", "values"]):
                if mode == "values" and isinstance(payload, dict):
                    final_state = payload
                    continue
                if mode != "updates" or not isinstance(payload, dict):
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
        except Exception as e:
            log.exception("follow-up graph invocation failed: %s", e)
            state.status = "failed"
            state.error = f"graph crashed: {e}"
            state.finished_at = time.time()
            _finalize_recorder(recorder, status="failed", error=str(e))
            _write_failed_artifacts(state, cwd, question, e)
            return

        for r in enabled:
            if state.progress.get(r) != "done":
                state.progress[r] = "done"

        node_error = final_state.get("error")
        node_failed = final_state.get("_role_failed")
        terminal_status = "failed" if node_error else "completed"

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
            final_answer=final_state.get("final_answer", ""),
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
            parent_sid=state.parent_sid,
        )
        storage.write_consultation(result, cwd=Path(cwd))

        # Inherit warm clients onto the follow-up's state so the
        # next follow-up in the chain (follow-up of follow-up)
        # also reuses them.
        state.plan = final_state.get("plan") or initial["plan"]
        state.plan_items = list(final_state.get("plan_items") or [])
        state.research = list(final_state.get("research") or [])
        state.critique = final_state.get("critique")
        state.final_answer = final_state.get("final_answer") or ""
        state.models = dict(models)
        state._chat_clients = {
            r: getattr(c, "_client", c) for r, c in chat_clients.items()
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
        _finalize_recorder(
            recorder, status=terminal_status, error=node_error,
            finished_at=state.finished_at,
        )
        _populate_role_messages(state, recorder)

    return run_follow_up


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
        return MessageRecorder(
            sdir / _storage.TRANSCRIPT_DB_FILENAME,
            meta=meta,
        )
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
    )
    try:
        storage.write_consultation(result, cwd=Path(cwd))
    except Exception as e:  # pragma: no cover
        log.warning("failed-artifact write failed: %s", e)


def default_ollama_url() -> str:
    """Pick the Ollama base URL the consultants service should use.

    Precedence:
      1. ``CALIBER_GROUNDING_UPSTREAM`` — canonical proxy URL across
         claude-hooks. If set, we trust it verbatim.
      2. ``OLLAMA_HOST`` — only when it looks like a real URL
         (``http://`` / ``https://`` prefix). It's frequently set to a
         bare bind address (``0.0.0.0`` on pandorum) for the local
         Ollama daemon, which is not a valid base URL — silently
         ignore those.
      3. The cluster-default proxy at 192.168.178.2:11433.
    """
    upstream = os.environ.get("CALIBER_GROUNDING_UPSTREAM")
    if upstream:
        return upstream
    ollama_host = os.environ.get("OLLAMA_HOST")
    if ollama_host and ollama_host.startswith(("http://", "https://")):
        return ollama_host
    return "http://192.168.178.2:11433"
