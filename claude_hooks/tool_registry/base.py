"""``ToolProvider`` — the contract a tool source implements.

Deliberately shaped like ``claude_hooks/providers/base.py``, the memory
backend ABC: this repo already teaches "one file per backend, four
methods, register it, done", and a second unrelated plugin idiom would
be a tax on everyone who has learned the first.

A provider answers three questions: which tools do you offer, what does
each one default to on the permission ladder, and how do I run one.

Namespacing
===========

Built-in tools keep bare names (``read_file``, ``grep``) because they
are already in every prompt, every transcript and every benchmark
fixture in the repo; renaming them would invalidate the M11c corpus for
no gain. Providers that wrap third-party sources — MCP servers above
all — must namespace (``<server>__<tool>``), because their tool names
are outside our control and *will* collide eventually.

``prefix`` handles that: empty for built-ins, set for everything whose
names come from elsewhere.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from claude_hooks.tool_registry.policy import AUTO


class ToolProvider(ABC):
    """One source of tools for the council."""

    #: Stable identifier, used in logs and config keys.
    name: str = "provider"

    #: Prepended to every tool name as ``<prefix>__<tool>``. Empty means
    #: the provider owns its bare names (built-ins only).
    prefix: str = ""

    # ------------------------------------------------------------------ #
    @abstractmethod
    def specs(self) -> list[dict]:
        """OpenAI function-calling schemas, with names already
        namespaced. Return ``[]`` when the provider is configured but
        has nothing to offer — an unreachable MCP server, say. Never
        raise: a provider that cannot enumerate its tools must degrade
        to offering none, or one broken server takes down the council.
        """

    @abstractmethod
    def execute(self, tool: str, raw_args: str, cwd: str) -> str:
        """Run ``tool`` (namespaced name) and return its output.

        Errors are returned as ``error: ...`` strings rather than
        raised, matching ``caliber_proxy.tools.execute``. That
        convention is load-bearing: the model sees the failure as a
        tool result and reroutes, where an exception would kill the
        lane.
        """

    # ------------------------------------------------------------------ #
    def default_level(self, tool: str) -> str:
        """This tool's default rung. Override for effectful tools."""
        return AUTO

    def is_read_only(self, tool: str) -> bool:
        """Whether ``tool`` only observes.

        Read-only tools are exempt from taint escalation, so this is a
        safety assertion, not a hint: a provider that marks an
        effectful tool read-only removes it from the backstop. When
        unsure, say False.
        """
        return True

    def taints(self) -> bool:
        """Whether merely activating this provider taints the session.

        True for providers whose *tool descriptions* are third-party
        text — an unvalidated MCP server — because the model reads
        those descriptions whether or not it ever calls the tool.
        Providers whose untrusted content only arrives in results (the
        network provider) return False here and taint on first result
        instead.
        """
        return False

    # ------------------------------------------------------------------ #
    def owns(self, tool: str) -> bool:
        """Whether ``tool`` (namespaced) belongs to this provider."""
        if self.prefix:
            return tool.startswith(f"{self.prefix}__")
        return any(
            s.get("function", {}).get("name") == tool for s in self.specs()
        )

    def strip_prefix(self, tool: str) -> str:
        if self.prefix and tool.startswith(f"{self.prefix}__"):
            return tool[len(self.prefix) + 2:]
        return tool

    def close(self) -> None:
        """Release any resources. Default no-op; MCP overrides it."""


def namespaced(prefix: str, spec: dict) -> dict:
    """Return ``spec`` with its function name prefixed.

    Copies rather than mutating — provider ``specs()`` implementations
    routinely return cached or module-level dicts, and rewriting those
    in place would corrupt them for the next caller.
    """
    if not prefix:
        return spec
    fn = dict(spec.get("function") or {})
    name = fn.get("name")
    if not isinstance(name, str) or not name:
        return spec
    fn["name"] = f"{prefix}__{name}"
    out = dict(spec)
    out["function"] = fn
    return out


__all__ = ["ToolProvider", "namespaced", "Optional"]
