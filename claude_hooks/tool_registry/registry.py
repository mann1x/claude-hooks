"""``ToolRegistry`` — merge providers into one surface, gate every call.

This is what replaces the two hardcoded ``openai_tool_specs()`` call
sites in ``consultants/server/runner.py``. Everything the council can
reach passes through :meth:`ToolRegistry.dispatch`, which is the single
place the :class:`~claude_hooks.tool_registry.policy.PolicyGate` runs.

One dispatch path, not one per provider: that is the whole point. Wire
the gate here once, and a provider added in six months inherits
approval, denial and audit without knowing the gate exists.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from claude_hooks.tool_registry.base import ToolProvider
from claude_hooks.tool_registry.policy import (
    GateDecision,
    PolicyGate,
)

log = logging.getLogger("claude_hooks.tool_registry")

#: Called with a :class:`GateDecision` when a tool needs approval.
#: Returns True to allow, False to refuse. ``None`` means the caller has
#: no approval channel at all.
ApprovalFn = Callable[[GateDecision], bool]


class ToolRegistry:
    """Composed tool surface for one consultation.

    Not shared between sessions: the gate carries per-session taint, so
    a registry outliving its session would leak that state into the
    next one — in the wrong direction, since taint is sticky.
    """

    def __init__(self, providers: list[ToolProvider], *,
                 gate: Optional[PolicyGate] = None,
                 approval_fn: Optional[ApprovalFn] = None):
        self.providers = list(providers)
        self.gate = gate or PolicyGate()
        self.approval_fn = approval_fn
        self._spec_cache: Optional[list[dict]] = None
        self._owner_cache: dict[str, ToolProvider] = {}
        self._seed_gate()

    # ------------------------------------------------------------------ #
    def _seed_gate(self) -> None:
        """Populate provider-declared defaults + read-only marks.

        Done once at construction rather than per call: ``specs()`` may
        hit the network for an MCP provider, and the gate consults this
        on every single tool call.
        """
        for p in self.providers:
            for spec in self._provider_specs(p):
                name = (spec.get("function") or {}).get("name")
                if not isinstance(name, str) or not name:
                    continue
                # First provider wins, matching ``specs()``. Claiming
                # ownership unconditionally here would advertise the
                # first provider's schema while executing the last
                # provider's implementation — the model would see one
                # tool and call another.
                if name in self._owner_cache:
                    continue
                bare = p.strip_prefix(name)
                self.gate.provider_defaults.setdefault(
                    name, p.default_level(bare))
                if p.is_read_only(bare):
                    self.gate.read_only_tools = (
                        self.gate.read_only_tools | {name})
                self._owner_cache[name] = p
            if p.taints():
                self.gate.taint(f"provider {p.name!r} carries "
                                f"third-party tool descriptions")

    @staticmethod
    def _provider_specs(p: ToolProvider) -> list[dict]:
        """``specs()`` with a hard guarantee it cannot raise.

        The ABC already says never raise; this enforces it, because one
        misbehaving provider must not be able to take down a council
        that would have worked fine without it.
        """
        try:
            specs = p.specs()
        except Exception:
            log.exception("provider %s specs() raised; offering no tools",
                          getattr(p, "name", "?"))
            return []
        return [s for s in (specs or []) if isinstance(s, dict)]

    # ------------------------------------------------------------------ #
    def specs(self) -> list[dict]:
        """Merged schemas across every provider, first wins on collision."""
        if self._spec_cache is not None:
            return list(self._spec_cache)
        merged: list[dict] = []
        seen: set[str] = set()
        for p in self.providers:
            for spec in self._provider_specs(p):
                name = (spec.get("function") or {}).get("name")
                if not isinstance(name, str) or not name:
                    continue
                if name in seen:
                    log.warning(
                        "tool name collision on %r; keeping the first "
                        "provider's version", name)
                    continue
                seen.add(name)
                merged.append(spec)
        self._spec_cache = merged
        return list(merged)

    def tool_names(self) -> list[str]:
        return [(s.get("function") or {}).get("name") for s in self.specs()]

    # ------------------------------------------------------------------ #
    def dispatch(self, tool: str, raw_args: str, cwd: str, *,
                 runtime_permissions: Optional[dict] = None) -> str:
        """Gate, then run. Returns the tool result string.

        Every refusal is returned as an ``error: ...`` string rather
        than raised, so the model treats it as a tool result and picks
        another route. A denial that killed the lane would turn a
        policy decision into an outage.
        """
        owner = self._owner_cache.get(tool)
        if owner is None:
            owner = next((p for p in self.providers if p.owns(tool)), None)
        if owner is None:
            return f"error: unknown tool {tool!r}"

        decision = self.gate.resolve(
            tool, runtime_permissions=runtime_permissions, raw_args=raw_args)

        if decision.denied:
            log.info("tool %s denied: %s", tool, decision.reason)
            return f"error: tool {tool!r} is not permitted — {decision.reason}"

        if decision.needs_approval:
            approved = self._seek_approval(decision)
            if not approved:
                return (f"error: tool {tool!r} was not approved — "
                        f"{decision.reason}. Try a different approach.")

        try:
            return owner.execute(tool, raw_args, cwd)
        except Exception as e:  # pragma: no cover - providers shouldn't raise
            log.exception("provider %s raised executing %s",
                          getattr(owner, "name", "?"), tool)
            return f"error: tool raised: {e}"

    # ------------------------------------------------------------------ #
    def _seek_approval(self, decision: GateDecision) -> bool:
        """Ask the approval channel, or refuse if there isn't one.

        ``/get-advice`` and the caliber proxy have no channel. Refusing
        is the only safe default there — the alternative is running an
        ``ask_human`` action because nobody was listening, which is the
        exact failure the ladder exists to prevent. It costs those
        callers nothing today: their tools are all ``auto``.
        """
        if self.approval_fn is None:
            log.warning(
                "tool %s needs %s but this caller has no approval "
                "channel; refusing", decision.tool, decision.level)
            return False
        try:
            return bool(self.approval_fn(decision))
        except Exception:
            log.exception("approval channel raised for %s; refusing",
                          decision.tool)
            return False

    # ------------------------------------------------------------------ #
    def as_executor(self, *, runtime_permissions: Optional[dict] = None):
        """Adapt to the ``(name, args, cwd) -> str`` executor signature
        that ``agent_loop.runner`` and every existing call site expect.

        This is what keeps M-A a drop-in: callers keep passing an
        executor callable and never learn the registry exists.
        """
        def _executor(name: str, raw_args: str, cwd: str) -> str:
            return self.dispatch(name, raw_args, cwd,
                                 runtime_permissions=runtime_permissions)
        return _executor

    def close(self) -> None:
        for p in self.providers:
            try:
                p.close()
            except Exception:  # pragma: no cover - defensive
                log.exception("provider %s close() raised",
                              getattr(p, "name", "?"))


__all__ = ["ToolRegistry", "ApprovalFn"]
