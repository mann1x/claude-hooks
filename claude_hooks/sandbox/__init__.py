"""The council sandbox: where a lane's shell command runs.

Plan Milestone E (``docs/PLAN-council-cerebriline.md`` §5), design in
``docs/research/cerebriline-map/04-sandbox-shell.md`` §5. A backend owns
isolation and nothing else; :mod:`claude_hooks.tool_registry.shell_provider`
owns policy, lanes and logs.

:func:`backend_for` picks the backend for this host. A host with none
gets an :class:`UnavailableBackend` whose preflight says why — a shell
is withheld, never run weaker than configured.
"""
from __future__ import annotations

import sys

from claude_hooks.sandbox.spec import (HOME, SCRATCH, Limits, Preflight,
                                       RunResult, RunSpec)

BACKENDS = ("auto", "bwrap")


class UnavailableBackend:
    """A backend that runs nothing and says why."""

    def __init__(self, name: str, detail: str):
        self.name, self.detail = name, detail

    def preflight(self, *, refresh: bool = False) -> Preflight:
        return Preflight(backend=self.name, ok=False, detail=self.detail)

    def run(self, spec: RunSpec) -> RunResult:
        return RunResult(exit_code=None, killed_by="setup",
                         setup_error=self.preflight().reason())


def backend_for(name: str = "auto"):
    """The backend ``name`` names on this OS (``auto``: the best one)."""
    if name not in BACKENDS:
        return UnavailableBackend(name, f"unknown sandbox backend {name!r}; "
                                  f"choose one of {', '.join(BACKENDS)}")
    if sys.platform.startswith("linux"):
        from claude_hooks.sandbox.linux_bwrap import BwrapBackend
        return BwrapBackend()
    if sys.platform == "win32":
        return UnavailableBackend(
            "appcontainer", "the Windows sandbox (AppContainer + Job Object, "
            "plan E7) is not built yet, so this host has no council shell")
    if sys.platform == "darwin":
        return UnavailableBackend(
            "seatbelt", "the macOS sandbox (Seatbelt + rlimits, plan E7) is "
            "not built yet, so this host has no council shell")
    return UnavailableBackend("none", f"no council sandbox for {sys.platform}")


__all__ = ["backend_for", "UnavailableBackend", "BACKENDS", "Limits",
           "Preflight", "RunResult", "RunSpec", "SCRATCH", "HOME"]
