"""What a sandboxed command is given, and what it gives back.

The backend owns isolation; the caller (``ShellToolProvider``) owns
policy and lifecycle — the split of the design's §5.2. These types are
the whole interface between them, so a second backend (the static
``council-sandbox`` launcher, AppContainer, Seatbelt) slots in behind the
same two dataclasses.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Limits:
    """Per-command limits (§5.3 layers 1 and 5)."""
    memory_mib: int = 4096
    pids: int = 256
    cpu_pct: int = 400
    nofile: int = 4096
    fsize_mib: int = 1024
    #: The wall clock a call gets when it names none, and the most it may ask.
    default_wall_s: int = 120
    max_wall_s: int = 600


@dataclass
class RunSpec:
    argv: list[str]
    #: Absolute; must lie inside one of ``ro_roots`` or be ``scratch``.
    cwd: str
    #: Roots visible read-only at their real paths (the council's roots).
    ro_roots: tuple[str, ...]
    #: Host directory mounted writable at :data:`SCRATCH`.
    scratch: str
    timeout_s: int
    limits: Limits = field(default_factory=Limits)
    #: Extra read-only binds — toolchains (``[tools.shell] ro_binds``).
    ro_binds: tuple[str, ...] = ()
    #: ``none`` only in P1; ``proxy`` / ``host`` arrive with egress (P5).
    net: str = "none"
    #: A unit / job name the backend may use to find the command again.
    name: str = ""


#: Where the lane's scratch dir appears inside the sandbox.
SCRATCH = "/scratch"
#: The sandbox's ``$HOME``: a private tmpfs, so no host dotfile is reachable.
HOME = "/home/sandbox"


@dataclass
class RunResult:
    exit_code: Optional[int]
    #: ``none`` / ``timeout`` / ``signal:<n>`` / ``setup`` (a layer failed;
    #: nothing ran) / ``interrupted`` (the engine went away mid-command).
    killed_by: str = "none"
    wall_s: float = 0.0
    stdout: str = ""
    stderr: str = ""
    #: Set with ``killed_by == "setup"``: which layer, and why.
    setup_error: str = ""


@dataclass
class Preflight:
    """Whether this host can run a shell, and what it would be made of.

    ``layers`` maps each layer to ``ok`` / ``absent`` / ``failed: …``;
    ``missing`` lists the *required* ones that are not ``ok``. A
    non-empty ``missing`` means no shell — never a weaker one.
    """
    backend: str
    ok: bool
    layers: dict[str, str] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    detail: str = ""

    def reason(self) -> str:
        if self.ok:
            return f"{self.backend}: ready"
        if self.missing:
            return (f"{self.backend}: required sandbox layer(s) unavailable: "
                    + ", ".join(f"{m} ({self.layers.get(m, 'absent')})"
                                for m in self.missing))
        return f"{self.backend}: {self.detail or 'unavailable'}"


__all__ = ["Limits", "RunSpec", "RunResult", "Preflight", "SCRATCH", "HOME"]
