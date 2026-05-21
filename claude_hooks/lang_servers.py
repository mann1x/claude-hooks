"""Language-server detection + per-OS installer matrix (v1.9+).

The bundled LSP engine (``claude_hooks.lsp_engine``) reads a
``cclsp.json`` mapping file extensions to LSP command vectors —
``pyright-langserver --stdio`` for ``.py``, ``gopls`` for ``.go``,
and so on. This module curates the set of LSPs the installer offers
to set up, and dispatches per-OS install commands when the user
opts in.

Two tiers:

- **Tier 1** — universal, well-supported, has a clean install path
  on Linux / macOS / Windows. ``install.py`` offers ``[Y/n]`` per
  missing server. Currently: ``pyright``, ``gopls``,
  ``rust-analyzer``, ``clangd``, ``typescript-language-server``,
  ``bash-language-server``.
- **Tier 2** — detection only. The installer reports MISSING with a
  pointer to ``docs/lsp-engine.md`` but does not offer to run
  anything. Currently: ``lua-language-server``, ``zls``,
  ``omnisharp``.

Design choices:

- We **never** install the toolchain underneath (Node, Go, Rust).
  When a Tier 1 LS depends on a manager that's not on ``$PATH``,
  ``select_installer_for`` returns ``None`` and the caller prints
  a manual-install message.
- Per-OS dispatch lives in ``Installer`` enum + filters in
  ``select_installer_for``. ``apt`` / ``dnf`` only on Linux, ``brew``
  only on macOS, ``scoop`` / ``winget`` only on Windows.
- ``npm`` / ``go`` / ``rustup`` work cross-platform if the toolchain
  is installed.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional


log = logging.getLogger("claude_hooks.lang_servers")


# --------------------------------------------------------------------- #
# Installer enum
# --------------------------------------------------------------------- #

class Installer(str, Enum):
    """Package-manager identifiers for the installer dispatch table.

    Order in :class:`LangServerSpec.installers` defines preference —
    the first installer whose underlying binary is on ``$PATH``
    (and whose OS restriction matches the current platform) wins.
    """
    NPM = "npm"          # cross-platform; needs Node
    GO = "go"            # cross-platform; needs Go toolchain
    RUSTUP = "rustup"    # cross-platform; needs rustup
    APT = "apt"          # Linux only (Debian / Ubuntu derivatives)
    DNF = "dnf"          # Linux only (Fedora / RHEL derivatives)
    BREW = "brew"        # macOS only
    SCOOP = "scoop"      # Windows only
    WINGET = "winget"    # Windows only
    MANUAL = "manual"    # placeholder for Tier 2 specs


# Underlying binary name for each Installer — kept separate from the
# enum so the enum stays a stable wire identifier even if a manager's
# CLI binary renames.
_MANAGER_BIN: dict[Installer, str] = {
    Installer.NPM: "npm",
    Installer.GO: "go",
    Installer.RUSTUP: "rustup",
    Installer.APT: "apt-get",
    Installer.DNF: "dnf",
    Installer.BREW: "brew",
    Installer.SCOOP: "scoop",
    Installer.WINGET: "winget",
    Installer.MANUAL: "",  # never invoked
}


# --------------------------------------------------------------------- #
# Per-LS spec
# --------------------------------------------------------------------- #

@dataclass(frozen=True)
class LangServerSpec:
    name: str                       # canonical id ("pyright")
    display: str                    # user-facing label ("Pyright (Python)")
    bin: str                        # binary name on $PATH
    extensions: tuple[str, ...]     # claimed file extensions, no leading dot
    cclsp_command: tuple[str, ...]  # populates cclsp.json "command" array
    tier: int                       # 1 (auto-install offered) or 2 (manual only)
    installers: tuple[Installer, ...]  # in preference order
    docs_url: Optional[str] = None  # link surfaced for Tier 2 / blocked Tier 1


SPECS: tuple[LangServerSpec, ...] = (
    # Tier 1 — auto-install offered.
    LangServerSpec(
        name="pyright",
        display="Pyright (Python)",
        bin="pyright-langserver",
        extensions=("py", "pyi"),
        cclsp_command=("pyright-langserver", "--stdio"),
        tier=1,
        installers=(Installer.NPM,),
        docs_url="https://github.com/microsoft/pyright",
    ),
    LangServerSpec(
        name="gopls",
        display="gopls (Go)",
        bin="gopls",
        extensions=("go",),
        cclsp_command=("gopls",),
        tier=1,
        installers=(Installer.GO,),
        docs_url="https://github.com/golang/tools/tree/master/gopls",
    ),
    LangServerSpec(
        name="rust-analyzer",
        display="rust-analyzer (Rust)",
        bin="rust-analyzer",
        extensions=("rs",),
        cclsp_command=("rust-analyzer",),
        tier=1,
        installers=(Installer.RUSTUP, Installer.BREW, Installer.SCOOP),
        docs_url="https://rust-analyzer.github.io/",
    ),
    LangServerSpec(
        name="clangd",
        display="clangd (C/C++)",
        bin="clangd",
        extensions=("c", "cc", "cpp", "cxx", "h", "hh", "hpp"),
        cclsp_command=("clangd",),
        tier=1,
        installers=(Installer.APT, Installer.DNF, Installer.BREW, Installer.SCOOP),
        docs_url="https://clangd.llvm.org/",
    ),
    LangServerSpec(
        name="typescript-language-server",
        display="typescript-language-server (TS/JS)",
        bin="typescript-language-server",
        extensions=("ts", "tsx", "js", "jsx", "mts", "cts"),
        cclsp_command=("typescript-language-server", "--stdio"),
        tier=1,
        installers=(Installer.NPM,),
        docs_url="https://github.com/typescript-language-server/typescript-language-server",
    ),
    LangServerSpec(
        name="bash-language-server",
        display="bash-language-server (Shell)",
        bin="bash-language-server",
        extensions=("sh", "bash"),
        cclsp_command=("bash-language-server", "start"),
        tier=1,
        installers=(Installer.NPM,),
        docs_url="https://github.com/bash-lsp/bash-language-server",
    ),
    # Tier 2 — detection only.
    LangServerSpec(
        name="lua-language-server",
        display="lua-language-server (Lua)",
        bin="lua-language-server",
        extensions=("lua",),
        cclsp_command=("lua-language-server",),
        tier=2,
        installers=(Installer.MANUAL,),
        docs_url="https://luals.github.io/",
    ),
    LangServerSpec(
        name="zls",
        display="zls (Zig)",
        bin="zls",
        extensions=("zig",),
        cclsp_command=("zls",),
        tier=2,
        installers=(Installer.MANUAL,),
        docs_url="https://github.com/zigtools/zls",
    ),
    LangServerSpec(
        name="omnisharp",
        display="OmniSharp (C#)",
        bin="omnisharp",
        extensions=("cs",),
        cclsp_command=("omnisharp", "-lsp"),
        tier=2,
        installers=(Installer.MANUAL,),
        docs_url="https://github.com/OmniSharp/omnisharp-roslyn",
    ),
)


# --------------------------------------------------------------------- #
# Install command matrix
#
# Keyed by Installer, then spec.name. An entry's absence means the
# combination is unsupported (e.g. there is no APT package for
# pyright). ``select_installer_for`` only returns Installers that
# have an entry here for the given spec.
# --------------------------------------------------------------------- #

INSTALL_COMMANDS: dict[Installer, dict[str, list[str]]] = {
    Installer.NPM: {
        "pyright": ["npm", "install", "-g", "pyright"],
        "typescript-language-server": [
            "npm", "install", "-g",
            "typescript-language-server", "typescript",
        ],
        "bash-language-server": ["npm", "install", "-g", "bash-language-server"],
    },
    Installer.GO: {
        "gopls": ["go", "install", "golang.org/x/tools/gopls@latest"],
    },
    Installer.RUSTUP: {
        "rust-analyzer": ["rustup", "component", "add", "rust-analyzer"],
    },
    Installer.APT: {
        "clangd": ["apt-get", "install", "-y", "clangd"],
    },
    Installer.DNF: {
        "clangd": ["dnf", "install", "-y", "clang-tools-extra"],
    },
    Installer.BREW: {
        "clangd": ["brew", "install", "llvm"],
        "rust-analyzer": ["brew", "install", "rust-analyzer"],
    },
    Installer.SCOOP: {
        "clangd": ["scoop", "install", "llvm"],
        "rust-analyzer": ["scoop", "install", "rust-analyzer"],
    },
    Installer.WINGET: {},
    Installer.MANUAL: {},
}


# --------------------------------------------------------------------- #
# OS gating
# --------------------------------------------------------------------- #

_LINUX_ONLY = {Installer.APT, Installer.DNF}
_DARWIN_ONLY = {Installer.BREW}
_WIN_ONLY = {Installer.SCOOP, Installer.WINGET}


def _current_platform() -> str:
    """Wrap ``sys.platform`` so tests can monkey-patch one place. Returns
    one of ``"linux"`` / ``"darwin"`` / ``"win32"`` / ``"other"``."""
    p = sys.platform
    if p.startswith("linux"):
        return "linux"
    if p == "darwin":
        return "darwin"
    if p == "win32":
        return "win32"
    return "other"


def _installer_allowed_on(installer: Installer, plat: str) -> bool:
    if installer in _LINUX_ONLY:
        return plat == "linux"
    if installer in _DARWIN_ONLY:
        return plat == "darwin"
    if installer in _WIN_ONLY:
        return plat == "win32"
    # Cross-platform installers (NPM, GO, RUSTUP, MANUAL) work anywhere.
    return True


def _manager_available(installer: Installer) -> bool:
    """Is the package-manager CLI on ``$PATH``?"""
    bin_name = _MANAGER_BIN.get(installer, "")
    if not bin_name:
        return False
    return shutil.which(bin_name) is not None


def select_installer_for(spec: LangServerSpec) -> Optional[Installer]:
    """Per-OS dispatch — first installer in ``spec.installers`` that
    is (a) allowed on the current platform, (b) has its package
    manager on ``$PATH``, and (c) has an entry in
    :data:`INSTALL_COMMANDS` for this spec. ``None`` if nothing
    matches — caller should print a manual-install pointer."""
    plat = _current_platform()
    for inst in spec.installers:
        if inst is Installer.MANUAL:
            continue
        if not _installer_allowed_on(inst, plat):
            continue
        if not _manager_available(inst):
            continue
        if spec.name not in INSTALL_COMMANDS.get(inst, {}):
            continue
        return inst
    return None


# --------------------------------------------------------------------- #
# Detection + install
# --------------------------------------------------------------------- #

@dataclass(frozen=True)
class InstalledState:
    """Per-spec detection result returned by :func:`detect_language_servers`."""
    spec: LangServerSpec
    installed: bool
    binary_path: Optional[str]
    installer_for_missing: Optional[Installer]


def detect_language_servers() -> dict[str, InstalledState]:
    """Walk the SPECS list and probe ``shutil.which`` for each binary.

    Returns a dict keyed by spec name. Each value is an
    :class:`InstalledState` with the resolved binary path (when
    installed) and the recommended installer (when missing).
    """
    out: dict[str, InstalledState] = {}
    for spec in SPECS:
        path = shutil.which(spec.bin)
        installer = None if path else select_installer_for(spec)
        out[spec.name] = InstalledState(
            spec=spec,
            installed=bool(path),
            binary_path=path,
            installer_for_missing=installer,
        )
    return out


def install_language_server(
    spec: LangServerSpec,
    installer: Installer,
    *,
    dry_run: bool = False,
    timeout: float = 600.0,
) -> tuple[bool, str]:
    """Run the install command for ``spec`` via ``installer``.

    Returns ``(success, message)``. ``message`` is the captured stdout
    (truncated) on success, or the error / stderr line on failure.
    """
    cmd = INSTALL_COMMANDS.get(installer, {}).get(spec.name)
    if not cmd:
        return False, f"no install command registered for {spec.name} via {installer.value}"

    if dry_run:
        return True, f"[dry-run] would run: {' '.join(cmd)}"

    log.info("install_language_server: %s", " ".join(cmd))
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, check=False, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, f"timeout after {timeout:.0f}s"
    except FileNotFoundError as e:
        return False, f"binary not found: {e}"
    except OSError as e:
        return False, f"invocation failed: {e}"

    if proc.returncode == 0:
        tail = (proc.stdout or "").strip().splitlines()
        last = tail[-1] if tail else "ok"
        return True, last[:200]

    err = (proc.stderr or proc.stdout or "").strip().splitlines()
    return False, (err[-1] if err else f"exit={proc.returncode}")[:200]


# --------------------------------------------------------------------- #
# Starter cclsp.json
# --------------------------------------------------------------------- #

def starter_cclsp_json(state: dict[str, InstalledState]) -> dict:
    """Build the ``cclsp.json`` dict from currently-detected LSPs.

    Only installed servers contribute an entry — never adds a
    ``"command"`` for a binary the user doesn't have. Extensions
    are normalised lowercase no-leading-dot, matching the engine's
    lookup at ``claude_hooks/lsp_engine/config.py:158``.
    """
    servers: list[dict] = []
    for st in state.values():
        if not st.installed:
            continue
        servers.append({
            "extensions": list(st.spec.extensions),
            "command": list(st.spec.cclsp_command),
        })
    return {"servers": servers}


def write_starter_cclsp_json(
    state: dict[str, InstalledState],
    target: str | os.PathLike,
    *,
    dry_run: bool = False,
) -> tuple[bool, str]:
    """Write the starter cclsp.json to ``target``. Refuses to overwrite
    an existing file — callers should check before this and surface
    the "file exists, leaving untouched" message themselves.

    Returns ``(written, message)``.
    """
    tp = Path(str(target))
    if tp.exists():
        return False, f"refusing to overwrite existing file at {tp}"

    blob = starter_cclsp_json(state)
    if not blob.get("servers"):
        return False, "no language servers detected; nothing to write"

    if dry_run:
        return True, f"[dry-run] would write {len(blob['servers'])} servers to {tp}"

    tp.parent.mkdir(parents=True, exist_ok=True)
    tp.write_text(json.dumps(blob, indent=2) + "\n", encoding="utf-8")
    return True, f"wrote {len(blob['servers'])} servers to {tp}"


__all__ = [
    "INSTALL_COMMANDS",
    "InstalledState",
    "Installer",
    "LangServerSpec",
    "SPECS",
    "_current_platform",
    "_installer_allowed_on",
    "_manager_available",
    "detect_language_servers",
    "install_language_server",
    "select_installer_for",
    "starter_cclsp_json",
    "write_starter_cclsp_json",
]
