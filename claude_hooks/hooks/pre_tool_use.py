"""
PreToolUse handler — *opt-in*. Off by default.

Matches risky tool calls (Bash, Edit, Write by default) against patterns
the user marks as historically dangerous. For each match, queries the
configured providers for past mistakes and injects a warning so the model
can second-guess itself before running the call.

This hook NEVER denies the call — it only warns via ``additionalContext``
on the tool use. Denying would block legitimate operations and is hard
to get right; surfacing context is enough.
"""

from __future__ import annotations

import logging
from typing import Optional

from claude_hooks.providers import Provider

log = logging.getLogger("claude_hooks.hooks.pre_tool_use")


def handle(*, event: dict, config: dict, providers: list[Provider]) -> Optional[dict]:
    hook_cfg = (config.get("hooks") or {}).get("pre_tool_use") or {}
    if not hook_cfg.get("enabled", False):
        return None

    tool_name = event.get("tool_name", "")
    tool_input = event.get("tool_input") or {}

    warn_tools = set(hook_cfg.get("warn_on_tools") or [])
    if warn_tools and tool_name not in warn_tools:
        return None

    # Build a probe string from the tool's input — use whichever field is
    # most distinctive for that tool.
    probe = _probe_string(tool_name, tool_input)
    if not probe:
        return None

    patterns = hook_cfg.get("warn_on_patterns") or []
    if patterns and not any(p.lower() in probe.lower() for p in patterns):
        return None

    # Query providers for past mistakes related to this command.
    snippets: list[str] = []
    for provider in providers:
        try:
            mems = provider.recall(probe, k=3)
        except Exception as e:
            log.warning("provider %s recall failed: %s", provider.name, e)
            continue
        for m in mems:
            snippet = m.text.strip().splitlines()[0][:200]
            snippets.append(f"- ({provider.name}) {snippet}")

    if not snippets:
        return None

    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": (
                "## ⚠ Past memory matched this command\n\n"
                + "\n".join(snippets)
                + "\n\n_Hooks are advisory only — proceed if the context still warrants it._"
            ),
        }
    }


def _probe_string(tool_name: str, tool_input: dict) -> str:
    if tool_name == "Bash":
        return tool_input.get("command", "")
    if tool_name in ("Edit", "Write", "MultiEdit"):
        return tool_input.get("file_path", "")
    if tool_name == "Read":
        return tool_input.get("file_path", "")
    return ""
