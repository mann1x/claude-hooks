"""systemd hardening for the consultants engine (plan E8).

The engine runs as root and hands commands to the council sandbox, so it
cannot take the daemon's ``ProtectSystem=strict``: it writes each
council's session store under whatever project the question names, and
in a user manager most protections imply ``PrivateUsers=yes``, which
takes away the real root the jail's drop-to-nobody needs. What it gets
is the set that was measured on solidpc (systemd 247, 2026-10-19)
against the live escape suite (``tests/test_council_shell_escape.py``,
42 tests, both backends) run inside a transient unit carrying it:

- every property in :data:`PROPERTIES` passed alone and together, with
  ``ProtectSystem=full`` as well, and a hardened engine served
  ``/v1/health``;
- ``ProtectHostname=yes`` made every shell test skip (preflight refused
  the sandbox), so it is left out;
- ``ProtectSystem=strict`` stopped the unit from running at all.

Deploy writes :data:`DROPIN` next to each installed engine unit (and the
smart-start forwarder, whose child is the engine) and reloads systemd;
``verify_deploy.check_engine_hardening`` FAILs on a stale or missing one.
Re-measure before adding a property: run the escape suite under
``systemd-run --user --wait --pipe -p <property> …``.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

DROPIN = "hardening.conf"
UNITS = ("claude-hooks-consultants.service",
         "claude-hooks-consultants-forwarder.service")

#: Measured to leave the council sandbox working (see the module doc).
PROPERTIES: tuple[tuple[str, str], ...] = (
    ("NoNewPrivileges", "yes"),
    ("ProtectSystem", "full"),
    ("ProtectKernelTunables", "yes"),
    ("ProtectKernelModules", "yes"),
    ("ProtectKernelLogs", "yes"),
    ("ProtectControlGroups", "yes"),
    ("ProtectClock", "yes"),
    ("RestrictSUIDSGID", "yes"),
    ("RestrictRealtime", "yes"),
    ("LockPersonality", "yes"),
    ("SystemCallArchitectures", "native"),
)

#: Measured to break it; never add.
REFUSED = ("ProtectHostname", "PrivateUsers")


def dropin_text() -> str:
    lines = ["# Written by scripts/deploy.py (plan E8, scripts/engine_hardening.py).",
             "# Measured against the council sandbox escape suite; do not add",
             "# ProtectHostname, and keep ProtectSystem at full (strict stops the unit).",
             "[Service]"]
    lines += [f"{k}={v}" for k, v in PROPERTIES]
    return "\n".join(lines) + "\n"


def unit_dirs() -> list[tuple[Path, str]]:
    return [(Path(os.path.expanduser("~/.config/systemd/user")), "user"),
            (Path("/etc/systemd/system"), "system")]


def installed_units() -> list[tuple[Path, str]]:
    return [(d / u, scope) for d, scope in unit_dirs() for u in UNITS
            if (d / u).is_file()]


def dropin_path(unit: Path) -> Path:
    return Path(f"{unit}.d") / DROPIN


def stale(unit: Path) -> bool:
    p = dropin_path(unit)
    try:
        return p.read_text(encoding="utf-8") != dropin_text()
    except OSError:
        return True


def ensure(*, dry_run: bool = False) -> list[str]:
    """Write a fresh drop-in for every installed engine unit that lacks
    one, then reload that scope. Returns what it did; raises ``OSError``
    when a drop-in cannot be written."""
    notes, reload = [], set()
    for unit, scope in installed_units():
        if not stale(unit):
            continue
        p = dropin_path(unit)
        if dry_run:
            notes.append(f"[dry-run] would write {p}")
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(dropin_text(), encoding="utf-8")
        notes.append(f"wrote {p} (the restart applies it)")
        reload.add(scope)
    for scope in sorted(reload):
        subprocess.run(["systemctl"] + (["--user"] if scope == "user" else [])
                       + ["daemon-reload"], capture_output=True, timeout=30)
    return notes


def problems() -> list[str]:
    """What ``verify_deploy`` reports: each installed unit whose drop-in
    is missing or not what deploy writes."""
    return [f"{dropin_path(u)} is missing or stale; run scripts/deploy.py"
            for u, _ in installed_units() if stale(u)]


__all__ = ["DROPIN", "UNITS", "PROPERTIES", "REFUSED", "dropin_text",
           "installed_units", "dropin_path", "stale", "ensure", "problems"]
