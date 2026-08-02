"""Compose the council's tool surface from config.

One place decides what the council can reach. Before M-A that decision
was two hardcoded ``openai_tool_specs()`` calls in ``runner.py``; this
module replaces both, so adding a provider is a config question rather
than an edit to the runner.

Returns the same ``(specs, executor)`` pair the runner already passes to
``GraphDeps``, which is what keeps M-A a drop-in: nothing downstream —
not the graph, not the nodes, not ``agent_loop.runner`` — learns that a
registry exists.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

log = logging.getLogger("consultants.tool_surface")


def build_tool_surface(cfg: Any, *, extra_roots: tuple[str, ...] = (),
                       approval_fn: Optional[Callable] = None):
    """Build ``(tool_specs, tool_executor, registry)`` for one session.

    ``approval_fn`` is the hook the interrupt wiring plugs into. Until
    it is supplied the registry refuses any ``ask_*`` tool rather than
    running it — which costs nothing today, since every tool in the
    default surface is ``auto``, and is the right failure direction the
    moment an effectful provider lands.

    Falls back to the pre-M-A fixed surface when ``[tools] enabled =
    false``, or when anything here raises: a council with the builtin
    six is strictly better than no council.
    """
    from claude_hooks.caliber_proxy.tools import make_executor, openai_tool_specs

    tools_cfg = getattr(cfg, "tools", None)
    if tools_cfg is not None and not getattr(tools_cfg, "enabled", True):
        log.info("tool registry disabled by config; using the builtin surface")
        return openai_tool_specs(), make_executor(extra_roots), None

    try:
        from claude_hooks.tool_registry import (
            BuiltinToolProvider,
            GitToolProvider,
            PolicyGate,
            ToolRegistry,
        )

        providers = [BuiltinToolProvider(extra_roots)]
        if tools_cfg is not None and getattr(tools_cfg, "git", False):
            providers.append(GitToolProvider(extra_roots))

        gate = PolicyGate(
            default_level=getattr(tools_cfg, "default_level", "auto")
            if tools_cfg is not None else "auto",
            overrides=dict(getattr(tools_cfg, "permissions", {}) or {})
            if tools_cfg is not None else {},
        )
        # Configuring an ``ask_*`` rung with no approval channel means
        # every such call is REFUSED, not asked — the registry has
        # nowhere to route it. That is the right failure direction, but
        # it is invisible from the answer: the model gets an "error:
        # not approved" string mid-loop, works around it, and the
        # operator sees a weaker answer with no explanation. Say it
        # once, at session start, naming what will be refused.
        if approval_fn is None and tools_cfg is not None:
            asks = sorted(
                name for name, level in
                (getattr(tools_cfg, "permissions", {}) or {}).items()
                if isinstance(level, str) and level.startswith("ask_")
            )
            default_asks = str(
                getattr(tools_cfg, "default_level", "auto") or ""
            ).startswith("ask_")
            if asks or default_asks:
                log.warning(
                    "[tools] requests approval (%s) but this session has "
                    "no approval channel wired — every affected call "
                    "will be REFUSED, not asked. Set those rungs to "
                    "'auto' or 'deny' to make the outcome explicit.",
                    "default_level=" + str(getattr(
                        tools_cfg, "default_level", "auto"))
                    if default_asks else "permissions: " + ", ".join(asks),
                )

        registry = ToolRegistry(providers, gate=gate, approval_fn=approval_fn)
        log.info("tool surface: %d tools from %d provider(s): %s",
                 len(registry.tool_names()), len(providers),
                 ", ".join(p.name for p in providers))
        return registry.specs(), registry.as_executor(), registry
    except Exception:
        log.exception(
            "tool registry construction failed; falling back to the "
            "builtin surface")
        return openai_tool_specs(), make_executor(extra_roots), None


__all__ = ["build_tool_surface"]
