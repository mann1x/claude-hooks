"""LLM-to-LLM advisor skill backend.

Exposes a CLI (``claude-advisor``) that Claude Code drives from inside a
``/get-advice`` skill invocation. Reuses caliber-proxy grounding,
caliber-proxy tools, and the shared ``claude_hooks.agent_loop`` runner
to drive multi-turn conversations with a configured Ollama model.
"""

from claude_hooks.get_advice.config import (
    AdvisorConfig,
    DEFAULT_MODEL,
    DEFAULT_EFFORT,
    DEFAULT_RESET_THRESHOLD,
    KNOWN_TOOLS,
    EFFORT_BUDGETS,
    load_config,
    save_config,
)

__all__ = [
    "AdvisorConfig",
    "DEFAULT_MODEL",
    "DEFAULT_EFFORT",
    "DEFAULT_RESET_THRESHOLD",
    "KNOWN_TOOLS",
    "EFFORT_BUDGETS",
    "load_config",
    "save_config",
]
