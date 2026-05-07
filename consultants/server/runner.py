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
    from claude_hooks.get_advice.chat_client import ChatClient
    from claude_hooks.caliber_proxy.tools import (
        openai_tool_specs, execute as tool_execute,
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
        if cfg.effort in ("low", "medium") and "critic" in enabled:
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
        chat_clients = {
            r: TracedChat(ChatClient(ollama_base_url),
                          role=r, tracer=tracer)
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

        deps = GraphDeps(
            chat_clients=chat_clients,
            models=models,
            enabled_roles=enabled,
            cwd=cwd,
            tool_executor=traced_tool(tool_execute, tracer=tracer),
            tool_specs=openai_tool_specs(),
            grounding_msgs=grounding_msgs,
            think_by_role=think_by_role,
            synthesizer_self_critic=synthesizer_self_critic,
            disable_cache=disable_cache,
            recorder=recorder,
        )
        compiled = build_council_graph(deps, tracer=tracer)

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
                    initial, stream_mode=["updates", "values"]):
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
    from claude_hooks.get_advice.chat_client import ChatClient
    from claude_hooks.caliber_proxy.tools import (
        openai_tool_specs, execute as tool_execute,
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

        # Topology: researcher + synthesizer always; critic only at
        # high/max effort (matches the main runner's gate). The
        # parent's ``enabled_roles`` may have included critic but
        # the follow-up topology decides independently based on
        # this follow-up's effort.
        enabled = ["researcher", "synthesizer"]
        if cfg.effort in ("high", "max") and "critic" in cc.enabled_roles(cfg):
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
                raw = ChatClient(ollama_base_url)
                log.info(
                    "follow-up %s: cold ChatClient for role=%s "
                    "(no warm parent client)", state.sid, role,
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

        deps = GraphDeps(
            chat_clients=chat_clients,
            models=models,
            enabled_roles=enabled_t,
            cwd=cwd,
            tool_executor=traced_tool(tool_execute, tracer=tracer),
            tool_specs=openai_tool_specs(),
            grounding_msgs=grounding_msgs,
            think_by_role=think_by_role,
            synthesizer_self_critic=False,  # follow-ups never
            disable_cache=True,             # caching not useful here
            recorder=recorder,
        )
        compiled = build_follow_up_graph(deps, tracer=tracer)

        # Initial state for the follow-up. Pre-populates the
        # parent's plan + research as ``prior_rounds`` so the
        # researcher node sees them; ``plan_item`` carries the
        # focused follow-up question; the parent's final_answer is
        # appended as a ``Prior synthesizer answer:`` block so the
        # researcher knows what's already been said.
        prior_research: list[str] = []
        if parent_state is not None:
            prior_research = list(parent_state.research or [])
            if parent_state.final_answer:
                prior_research.append(
                    "## Prior synthesizer answer\n\n"
                    + parent_state.final_answer.strip()
                )

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
        initial["research"] = prior_research

        if enabled:
            state.progress[enabled[0]] = "in_progress"

        # Same dual-mode streaming pattern as run_council.
        final_state: dict = dict(initial)
        try:
            for mode, payload in compiled.stream(
                    initial, stream_mode=["updates", "values"]):
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
