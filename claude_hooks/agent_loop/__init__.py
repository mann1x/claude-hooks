"""Reusable agent loop for tool-use chat with Ollama-style chat APIs.

Extracted from ``claude_hooks.caliber_proxy.server`` so multiple callers
(the caliber grounding proxy and the ``/get-advice`` advisor skill) share
one battle-tested loop. The runner is dependency-injected: callers pass
their own ``chat_fn`` (the HTTP client) and ``tool_executor`` (the
function that runs a tool by name + args), so this module has no
hard-coded transport or tool registry.
"""

from claude_hooks.agent_loop.runner import (
    DEFAULT_FORCE_FIRST_RETRY_MESSAGE,
    LoopConfig,
    execute_tool_calls,
    merge_tools,
    run_loop,
)

__all__ = [
    "DEFAULT_FORCE_FIRST_RETRY_MESSAGE",
    "LoopConfig",
    "execute_tool_calls",
    "merge_tools",
    "run_loop",
]
