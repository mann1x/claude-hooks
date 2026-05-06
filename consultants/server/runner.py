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
from typing import Any

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

    def run_council(state, runner_input: dict) -> None:
        cfg: cc.ConsultantsConfig = runner_input["config"]
        cwd: str = runner_input["cwd"]
        question: str = runner_input["question"]
        enabled = tuple(cc.enabled_roles(cfg))

        # One ChatClient per role so retries and timing don't bleed.
        chat_clients = {
            r: ChatClient(ollama_base_url) for r in enabled
        }
        models = {r: cfg.roles[r].model for r in enabled}

        grounding_msgs = build_grounding_messages(
            cwd, tools_available=True,
        ) if "researcher" in enabled else []

        deps = GraphDeps(
            chat_clients=chat_clients,
            models=models,
            enabled_roles=enabled,
            cwd=cwd,
            tool_executor=tool_execute,
            tool_specs=openai_tool_specs(),
            grounding_msgs=grounding_msgs,
        )
        compiled = build_council_graph(deps)

        # Build initial state, mark planner in_progress for the first
        # progress poll (it's the entry node by default).
        initial = council_mod.initial_state(
            question=question, cwd=cwd, models=models,
            topology=cfg.topology, effort=cfg.effort,
        )
        if enabled:
            state.progress[enabled[0]] = "in_progress"

        # Run the graph. LangGraph's compiled.invoke returns the final
        # merged state.
        try:
            final_state = compiled.invoke(initial)
        except Exception as e:
            log.exception("council graph invocation failed: %s", e)
            state.status = "failed"
            state.error = f"graph crashed: {e}"
            state.finished_at = time.time()
            _write_failed_artifacts(state, cwd, question, e)
            return

        # Mark all enabled roles done (LangGraph doesn't fire an
        # incremental progress callback in v1; we get done-state at
        # the end).
        for r in enabled:
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

        state.status = terminal_status
        state.error = node_error
        state.finished_at = time.time()
        if node_failed:
            log.warning("council finished with role failure: %s",
                        node_failed)

    return run_council


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
    Mirrors the precedence /get-advice and caliber-proxy use."""
    return (
        os.environ.get("CALIBER_GROUNDING_UPSTREAM")
        or os.environ.get("OLLAMA_HOST")
        or "http://192.168.178.2:11433"
    )
