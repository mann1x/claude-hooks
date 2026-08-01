"""The permission ladder and the gate that applies it.

This module is the keystone of the council tool surface: it sits in
front of tool dispatch, so every provider added later inherits
approval, denial and audit without touching its own code. See
``docs/PLAN-council-tool-surface.md`` for the full design.

**Pure Python, no LangGraph import**, deliberately — mirroring
``consultants/engine/interrupt_policy.py``. The gate returns a
*decision*; whoever owns an approval channel decides how to pose it.
The consultants engine turns an ``ask_*`` into a LangGraph
``interrupt()``; ``/get-advice`` and the caliber proxy have no such
channel and treat it as a refusal. That split keeps this file
importable in the plain ``claude-hooks`` test env.

The ladder
==========

Four levels, decided 2026-08-01::

    auto           nobody approves -- it just runs
    ask_assistant  assistant approves (auto-approves, may escalate)
    ask_human      a person approves; the only level that can stall
    deny           refused outright

``auto`` exists for a concrete reason: routing a ``grep`` or a
``git blame`` through the assistant burns tokens for nothing. So the
gate's common path must be a dict lookup that never touches an LLM or
the interrupt machinery.

Taint
=====

Once a session has consumed untrusted external text, every *effectful*
tool moves one rung up the ladder for the rest of the session
(``auto -> ask_assistant -> ask_human``; the top two do not move). The
property this buys: after untrusted text is in play, no effectful tool
runs without *some* reviewer seeing it, while the common case stays on
the assistant rather than a person.

The plan states the rule in terms of shell commands, because shell is
the instance that exists. It is implemented here against
``ToolSpec.read_only`` instead, so a future effectful tool -- a write,
an MCP mutation -- is covered on the day it lands rather than the day
someone remembers to add it. Read-only tools are never escalated: they
are the reason ``auto`` is cheap, and escalating them would defeat the
point while adding no safety.

With only the built-in read-only providers wired, taint therefore has
no observable effect. That is correct, not a gap: nothing effectful
exists yet to escalate.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("claude_hooks.tool_registry.policy")


# ====================================================================== #
# The ladder
# ====================================================================== #
AUTO = "auto"
ASK_ASSISTANT = "ask_assistant"
ASK_HUMAN = "ask_human"
DENY = "deny"

#: Every valid permission level, in ladder order.
LEVELS: tuple[str, ...] = (AUTO, ASK_ASSISTANT, ASK_HUMAN, DENY)

#: Levels that require an approval round-trip before the tool runs.
ASK_LEVELS: frozenset[str] = frozenset({ASK_ASSISTANT, ASK_HUMAN})

# One rung up. ``ask_human`` is the ceiling for escalation and ``deny``
# is terminal -- neither moves. Note this is NOT ``LEVELS`` index + 1:
# escalating ``ask_human`` into ``deny`` would silently convert "ask a
# person" into "refuse", which is a different decision than the one the
# operator configured.
_ESCALATE: dict[str, str] = {
    AUTO: ASK_ASSISTANT,
    ASK_ASSISTANT: ASK_HUMAN,
    ASK_HUMAN: ASK_HUMAN,
    DENY: DENY,
}


def escalate(level: str) -> str:
    """Move ``level`` one rung up the ladder. Unknown -> ``deny``."""
    return _ESCALATE.get(level, DENY)


def is_valid_level(level: object) -> bool:
    return isinstance(level, str) and level in LEVELS


# ====================================================================== #
# Decision
# ====================================================================== #
@dataclass(frozen=True)
class GateDecision:
    """What the gate concluded about one pending tool call.

    ``reason`` is written to be shown to an approver verbatim, so it
    must name the tool and why this level applied. An approver cannot
    judge ``sh -c "..."`` on its own.
    """

    tool: str
    level: str
    reason: str
    escalated_by_taint: bool = False
    #: Arguments as the model supplied them, for the approval prompt.
    raw_args: str = ""

    @property
    def allowed(self) -> bool:
        return self.level == AUTO

    @property
    def needs_approval(self) -> bool:
        return self.level in ASK_LEVELS

    @property
    def denied(self) -> bool:
        return self.level == DENY

    def to_payload(self) -> dict:
        """Serializable form for an interrupt payload / SSE event."""
        return {
            "tool": self.tool,
            "level": self.level,
            "reason": self.reason,
            "escalated_by_taint": self.escalated_by_taint,
            "raw_args": self.raw_args,
        }


# ====================================================================== #
# The gate
# ====================================================================== #
@dataclass
class PolicyGate:
    """Resolve a tool call to a permission level.

    Resolution order, first match wins:

    1. ``runtime_control.tool_permissions`` -- the live HTTP control
       surface, so an operator can tighten or loosen a running council
       without restarting it.
    2. ``overrides`` -- the ``[tools.permissions]`` config block.
    3. The provider's declared default for that tool.
    4. ``default_level``.

    Then taint escalation applies on top, and it applies *after*
    resolution deliberately: an operator who pinned a tool to ``auto``
    still gets the escalation, because the taint rule is a safety
    backstop rather than a preference. The one exception is ``deny``,
    which is terminal in both directions.
    """

    #: Fallback when nothing else matches.
    default_level: str = AUTO
    #: From ``[tools.permissions]``: tool name -> level.
    overrides: dict = field(default_factory=dict)
    #: Provider-declared defaults: tool name -> level.
    provider_defaults: dict = field(default_factory=dict)
    #: Which tools are read-only (never escalated by taint).
    read_only_tools: frozenset = field(default_factory=frozenset)
    #: Set once the session has consumed untrusted external text.
    tainted: bool = False

    # ---------------------------------------------------------------- #
    def resolve(self, tool: str, *, runtime_permissions: Optional[dict] = None,
                raw_args: str = "") -> GateDecision:
        base, source = self._base_level(tool, runtime_permissions)

        if not is_valid_level(base):
            # A malformed level must fail closed. Silently treating it
            # as ``auto`` would turn a typo in config into an ungated
            # tool, which is the worst possible direction to fail.
            log.warning(
                "invalid permission level %r for tool %r (%s); denying",
                base, tool, source,
            )
            return GateDecision(
                tool=tool, level=DENY, raw_args=raw_args,
                reason=(f"tool {tool!r} has an invalid permission level "
                        f"{base!r} in {source}; refusing rather than "
                        f"guessing"),
            )

        read_only = tool in self.read_only_tools
        if self.tainted and not read_only and base != DENY:
            bumped = escalate(base)
            if bumped != base:
                return GateDecision(
                    tool=tool, level=bumped, raw_args=raw_args,
                    escalated_by_taint=True,
                    reason=(
                        f"tool {tool!r} is {base} by {source}, escalated to "
                        f"{bumped} because this session has consumed "
                        f"untrusted external content (web or an unvalidated "
                        f"MCP server)"
                    ),
                )

        return GateDecision(
            tool=tool, level=base, raw_args=raw_args,
            reason=f"tool {tool!r} is {base} by {source}",
        )

    # ---------------------------------------------------------------- #
    def _base_level(self, tool: str,
                    runtime_permissions: Optional[dict]) -> tuple[object, str]:
        if runtime_permissions:
            got = runtime_permissions.get(tool)
            if got is not None:
                return got, "runtime control"
        if tool in self.overrides:
            return self.overrides[tool], "[tools.permissions] config"
        if tool in self.provider_defaults:
            return self.provider_defaults[tool], "provider default"
        return self.default_level, "default"

    # ---------------------------------------------------------------- #
    def taint(self, reason: str = "") -> None:
        """Mark the session tainted. Sticky and irreversible.

        Irreversible on purpose: there is no point at which previously
        ingested untrusted text stops being in the model's context, so
        an ``untaint`` would be a lie.
        """
        if not self.tainted:
            log.info("tool policy: session tainted%s",
                     f" ({reason})" if reason else "")
        self.tainted = True


__all__ = [
    "AUTO", "ASK_ASSISTANT", "ASK_HUMAN", "DENY",
    "LEVELS", "ASK_LEVELS",
    "GateDecision", "PolicyGate",
    "escalate", "is_valid_level",
]
