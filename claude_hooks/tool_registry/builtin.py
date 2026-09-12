"""The six built-in tools, as a :class:`ToolProvider`.

Wraps ``claude_hooks.caliber_proxy.tools`` rather than reimplementing
it. That module stays the implementation of record — it is what
``/get-advice`` and the caliber proxy call directly, and it carries the
multi-root sandbox — so duplicating it here would create two
definitions of ``read_file`` that drift.

No prefix: these names are already in every prompt, transcript and
benchmark fixture in the repo. Renaming them would invalidate the M11c
corpus to no purpose.

All six are read-only, so none is escalated by taint. ``recall_memory``
deserves a note: it reads the memory store, which contains text
previously written by *this* system, not third-party content, so it is
not an untrusted-input source and does not taint.
"""

from __future__ import annotations

from typing import Optional

from claude_hooks.tool_registry.base import ToolProvider
from claude_hooks.tool_registry.policy import AUTO


class BuiltinToolProvider(ToolProvider):
    """``survey_project`` / ``list_files`` / ``read_file`` / ``glob`` /
    ``grep`` / ``recall_memory``."""

    name = "builtin"
    prefix = ""

    def __init__(self, extra_roots: tuple[str, ...] = ()):
        self.extra_roots = tuple(r for r in extra_roots if r)
        self._executor = None
        self._specs: Optional[list[dict]] = None

    # ------------------------------------------------------------------ #
    def specs(self) -> list[dict]:
        if self._specs is None:
            from claude_hooks.caliber_proxy import tools as caliber_tools
            self._specs = list(caliber_tools.openai_tool_specs())
        return list(self._specs)

    def execute(self, tool: str, raw_args: str, cwd: str) -> str:
        if self._executor is None:
            from claude_hooks.caliber_proxy import tools as caliber_tools
            # make_executor returns the bare ``execute`` when there are
            # no extra roots, preserving the closure-free fast path.
            self._executor = caliber_tools.make_executor(self.extra_roots)
        return self._executor(tool, raw_args, cwd)

    # ------------------------------------------------------------------ #
    def default_level(self, tool: str) -> str:
        return AUTO

    def is_read_only(self, tool: str) -> bool:
        return True

    def taints(self) -> bool:
        return False


__all__ = ["BuiltinToolProvider"]
