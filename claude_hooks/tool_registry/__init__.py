"""Composable tool surface with a permission gate in front of dispatch.

M-A of ``docs/PLAN-council-tool-surface.md``. Providers contribute
tools; the registry merges them into one schema list and one dispatch
path; the gate runs on every call.

Imports are lazy at the leaves (``builtin`` pulls ``caliber_proxy`` only
when first used) so importing this package stays cheap for hooks that
touch it incidentally.

    from claude_hooks.tool_registry import ToolRegistry, BuiltinToolProvider

    reg = ToolRegistry([BuiltinToolProvider(extra_roots)])
    specs = reg.specs()                 # -> agent_loop tool_specs
    executor = reg.as_executor()        # -> agent_loop tool_executor
"""

from __future__ import annotations

from claude_hooks.tool_registry.base import ToolProvider, namespaced
from claude_hooks.tool_registry.builtin import BuiltinToolProvider
from claude_hooks.tool_registry.git_provider import GitToolProvider
from claude_hooks.tool_registry.policy import (
    ASK_ASSISTANT,
    ASK_HUMAN,
    ASK_LEVELS,
    AUTO,
    DENY,
    LEVELS,
    GateDecision,
    PolicyGate,
    escalate,
    is_valid_level,
)
from claude_hooks.tool_registry.registry import ApprovalFn, ToolRegistry

__all__ = [
    "ToolProvider", "namespaced",
    "BuiltinToolProvider", "GitToolProvider",
    "ToolRegistry", "ApprovalFn",
    "PolicyGate", "GateDecision",
    "AUTO", "ASK_ASSISTANT", "ASK_HUMAN", "DENY",
    "LEVELS", "ASK_LEVELS",
    "escalate", "is_valid_level",
]
