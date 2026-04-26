"""
SessionStart handler — inject a brief status line listing which memory
providers are active.

On ``source == "compact"`` (context compaction), also runs the full recall
pipeline so the model recovers its memory after compaction.
"""

from __future__ import annotations

import logging
from typing import Optional

from claude_hooks.providers import Provider

log = logging.getLogger("claude_hooks.hooks.session_start")


def handle(*, event: dict, config: dict, providers: list[Provider]) -> Optional[dict]:
    hook_cfg = (config.get("hooks") or {}).get("session_start") or {}
    if not hook_cfg.get("enabled", True):
        return None

    cwd = event.get("cwd", "") or ""

    # Code-structure graph: read pre-built GRAPH_REPORT.md to inject as
    # additionalContext, and spawn a detached rebuild if stale. Silent
    # no-op when the project doesn't look like a code repo.
    cg_cfg = (config.get("hooks") or {}).get("code_graph") or {}
    cg_block = ""
    if cg_cfg.get("enabled", True):
        try:
            from claude_hooks.code_graph import (
                build_session_block,
                project_root as _cg_root,
            )
            from claude_hooks.code_graph.__main__ import build_async as _cg_build_async

            root = _cg_root(cwd)
            if root is not None:
                block = build_session_block(
                    root,
                    max_chars=int(cg_cfg.get("max_inject_chars", 4000)),
                )
                if block:
                    cg_block = block
                # Append hints pointing at heavier code-graph engines
                # (axon, gitnexus) when present. Cheap detection —
                # filesystem checks only, never spawns.
                comp_cfg = (config.get("hooks") or {}).get("companions") or {}
                if comp_cfg.get("enabled", True):
                    try:
                        from claude_hooks.companion_integration import (
                            session_start_hint,
                        )
                        hint = session_start_hint(root)
                        if hint:
                            cg_block = (cg_block + "\n\n" + hint) if cg_block else hint
                    except Exception as e:
                        log.debug("companion hint skipped: %s", e)
                if cg_cfg.get("rebuild_on_session_start", True):
                    _cg_build_async(
                        cwd=cwd,
                        cooldown_minutes=int(cg_cfg.get("staleness_minutes", 10)),
                        min_source_files=int(cg_cfg.get("min_source_files", 5)),
                        max_files_to_scan=int(cg_cfg.get("max_files_to_scan", 2000)),
                        lock_min_age_seconds=int(cg_cfg.get("lock_min_age_seconds", 60)),
                    )
        except Exception as e:
            log.debug("code_graph SessionStart skipped: %s", e)

    # Claudemem freshness: if the index is stale compared to the newest
    # source file, kick off a detached reindex. Silent no-op if claudemem
    # is not installed or the project has no .claudemem directory.
    reindex_cfg = (config.get("hooks") or {}).get("claudemem_reindex") or {}
    if reindex_cfg.get("enabled", True) and reindex_cfg.get("check_on_session_start", True):
        try:
            from claude_hooks.claudemem_reindex import (
                _DEFAULT_IGNORED_DIRS,
                reindex_if_stale_async,
            )
            extra_ignored = reindex_cfg.get("ignored_dirs") or []
            if extra_ignored:
                ignored = frozenset(_DEFAULT_IGNORED_DIRS | set(extra_ignored))
            else:
                ignored = _DEFAULT_IGNORED_DIRS
            reindex_if_stale_async(
                cwd=cwd,
                staleness_minutes=int(reindex_cfg.get("staleness_minutes", 10)),
                max_files_to_scan=int(reindex_cfg.get("max_files_to_scan", 2000)),
                ignored_dirs=ignored,
                lock_min_age_seconds=int(reindex_cfg.get("lock_min_age_seconds", 60)),
            )
        except Exception as e:
            log.debug("claudemem stale-check skipped: %s", e)

    if not providers:
        return None

    labels = [(p.display_name or p.name) for p in providers]
    source = (event.get("source") or "startup").lower()
    verb = {"resume": "Resumed", "compact": "Compacted", "startup": "Started"}.get(source, "Started")

    status_line = (
        f"_{verb} with claude-hooks recall enabled "
        f"({len(providers)} provider(s): {', '.join(labels)})._"
    )

    # On compaction, re-inject full recalled context so the model recovers
    # its memory. Without this, all prior hook injections are lost.
    if source == "compact" and hook_cfg.get("compact_recall", True):
        try:
            from claude_hooks.recall import run_recall

            query = hook_cfg.get(
                "compact_recall_query",
                "session context, key decisions, and important patterns",
            )
            recalled = run_recall(
                query,
                config=config,
                providers=providers,
                hook_name="user_prompt_submit",
                cwd=cwd,
            )
            if recalled:
                parts = [status_line, recalled]
                if cg_block:
                    parts.append(cg_block)
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "SessionStart",
                        "additionalContext": "\n\n".join(parts),
                    }
                }
        except Exception as e:
            log.warning("compact recall failed: %s", e)

    show_status = hook_cfg.get("show_status_line", True)
    parts = []
    if show_status:
        parts.append(status_line)
    if cg_block:
        parts.append(cg_block)
    if not parts:
        return None

    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": "\n\n".join(parts),
        }
    }
