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
        # Windows: prefer winget (ships clangd in the LLVM bundle) over
        # scoop because winget is in-box on Win10 1909+ and doesn't
        # require an opt-in package manager install. scoop is still
        # listed as a fallback for users who have it set up.
        installers=(Installer.APT, Installer.DNF, Installer.BREW,
                    Installer.WINGET, Installer.SCOOP),
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
    # Tier 2 — optional. Auto-install offered when a known package
    # manager is available; otherwise displayed as MISSING with the
    # docs_url. Promoted from manual-only in v1.9.x once the per-OS
    # install matrix was filled in.
    LangServerSpec(
        name="lua-language-server",
        display="lua-language-server (Lua)",
        bin="lua-language-server",
        extensions=("lua",),
        cclsp_command=("lua-language-server",),
        tier=2,
        installers=(Installer.BREW, Installer.WINGET, Installer.SCOOP),
        docs_url="https://luals.github.io/",
    ),
    LangServerSpec(
        name="zls",
        display="zls (Zig)",
        bin="zls",
        extensions=("zig",),
        cclsp_command=("zls",),
        tier=2,
        # winget first on Windows — ``zigtools.zls`` is the official
        # zig-tools-published package and doesn't need any bucket setup.
        installers=(Installer.BREW, Installer.WINGET, Installer.SCOOP),
        docs_url="https://github.com/zigtools/zls",
    ),
    LangServerSpec(
        name="omnisharp",
        display="OmniSharp (C#)",
        bin="omnisharp",
        extensions=("cs",),
        cclsp_command=("omnisharp", "-lsp"),
        tier=2,
        installers=(Installer.SCOOP,),
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
        "lua-language-server": ["brew", "install", "lua-language-server"],
        "zls": ["brew", "install", "zls"],
    },
    Installer.SCOOP: {
        # All these LSs live in scoop's ``main`` bucket — the default
        # one added at scoop install time. No ``scoop bucket add extras``
        # required. Verified via ``scoop search`` on 2026-05-21:
        #   clangd 22.1.0 (main), llvm 22.1.6 (main),
        #   rust-analyzer 2026-05-18 (main), lua-language-server 3.18.2 (main),
        #   zls 0.16.0 (main), omnisharp 1.39.15 (main).
        # Bare ``<name>`` (without ``<bucket>/`` prefix) lets scoop
        # search across all installed buckets — safest default.
        # Earlier v1.9.x shipped ``extras/<name>`` which silently
        # failed (scoop exits 0 with "Couldn't find manifest"), making
        # install.py report [ok] for a no-op install.
        "clangd": ["scoop", "install", "llvm"],
        "rust-analyzer": ["scoop", "install", "rust-analyzer"],
        "lua-language-server": ["scoop", "install", "lua-language-server"],
        "zls": ["scoop", "install", "zls"],
        "omnisharp": ["scoop", "install", "omnisharp"],
    },
    Installer.WINGET: {
        # winget is preinstalled on Win10 1909+ / Win11 and doesn't
        # require any opt-in package-bucket setup. ``--silent`` keeps
        # the install non-interactive; the two ``--accept-*`` flags
        # auto-accept EULA + source-trust prompts so the install
        # actually proceeds in a script-driven flow.
        "clangd": [
            "winget", "install", "--id", "LLVM.LLVM",
            "--silent",
            "--accept-source-agreements",
            "--accept-package-agreements",
        ],
        "lua-language-server": [
            "winget", "install", "--id", "LuaLS.lua-language-server",
            "--silent",
            "--accept-source-agreements",
            "--accept-package-agreements",
        ],
        "zls": [
            "winget", "install", "--id", "zigtools.zls",
            "--silent",
            "--accept-source-agreements",
            "--accept-package-agreements",
        ],
    },
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
    """Per-spec detection result returned by :func:`detect_language_servers`.

    Three possible states (in order of preference):

    1. ``installed=True`` + ``binary_path`` — the binary is on
       ``$PATH`` and the LSP engine can spawn it as-is.
    2. ``installed=False`` + ``on_disk_path`` set — the binary
       exists at a known install location (winget Links, scoop
       shims, ``Program Files\\LLVM\\bin``, etc.) but isn't on
       the current shell's PATH. Surfacing this lets install.py
       tell the user "restart your shell" instead of offering to
       re-install something that's already there.
    3. ``installed=False`` + ``installer_for_missing`` set — not
       installed anywhere we can detect. Offer the install.
    """
    spec: LangServerSpec
    installed: bool
    binary_path: Optional[str]
    installer_for_missing: Optional[Installer]
    on_disk_path: Optional[str] = None  # case 2 above


def _windows_extra_search_paths(spec: LangServerSpec) -> list[str]:
    """Return a list of Windows-specific paths where ``spec.bin``
    may have landed after a winget/scoop install but where the
    parent directory hasn't been added to the current shell's PATH
    yet. Empty list on non-Windows; ``shutil.which`` is the only
    reliable check on POSIX (the conventions there put installs
    on PATH immediately).

    These are checked AFTER ``shutil.which`` fails. False positives
    are harmless — we only surface "on disk, not on PATH" UI when
    one of these paths actually exists.
    """
    if os.name != "nt":
        return []

    paths: list[str] = []
    home = os.path.expanduser("~")
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    local_appdata = os.environ.get(
        "LOCALAPPDATA",
        os.path.join(home, "AppData", "Local"),
    )

    bin_name = spec.bin
    # ``shutil.which`` checks PATHEXT — we mirror that here so a
    # ``clangd.exe`` install is found whether the binary on disk is
    # ``clangd``, ``clangd.exe``, or ``clangd.cmd``.
    bin_variants = [bin_name, bin_name + ".exe", bin_name + ".cmd",
                    bin_name + ".CMD", bin_name + ".EXE"]

    # ---- Winget aliases ---------------------------------------------
    # Winget creates per-binary shims under ``Microsoft\WinGet\Links``
    # that point at the actual install — these are NOT on the default
    # PATH on every Windows install, so a fresh shell may miss them.
    if Installer.WINGET in spec.installers:
        winget_links = os.path.join(
            local_appdata, "Microsoft", "WinGet", "Links",
        )
        for v in bin_variants:
            paths.append(os.path.join(winget_links, v))

    # ---- Scoop shims -----------------------------------------------
    # Scoop ALWAYS creates ``%USERPROFILE%\scoop\shims\<bin>.<ext>``
    # for any installed app. ``scoop\shims`` should be on PATH after
    # ``install_scoop_windows`` ran, but a fresh shell that started
    # before scoop install was done won't see it.
    if Installer.SCOOP in spec.installers:
        scoop_root = os.environ.get(
            "SCOOP", os.path.join(home, "scoop"),
        )
        for v in bin_variants:
            paths.append(os.path.join(scoop_root, "shims", v))

    # ---- Spec-specific known install locations ----------------------
    # Some installers place binaries at known per-package paths that
    # the user might also know about. clangd via winget LLVM.LLVM is
    # the canonical case — installs to Program Files but doesn't
    # always update the current shell's PATH.
    if spec.name == "clangd":
        paths.extend([
            os.path.join(program_files, "LLVM", "bin", "clangd.exe"),
            os.path.join(local_appdata, "Programs", "LLVM", "bin", "clangd.exe"),
            os.path.join(home, "scoop", "apps", "llvm", "current",
                         "bin", "clangd.exe"),
        ])
    if spec.name == "omnisharp":
        paths.append(os.path.join(
            home, "scoop", "apps", "omnisharp", "current", "OmniSharp.exe",
        ))
    if spec.name == "lua-language-server":
        # Winget's package-specific install dir (long suffix is winget's
        # naming convention with the publisher GUID).
        paths.append(os.path.join(
            local_appdata, "Microsoft", "WinGet", "Packages",
            "LuaLS.lua-language-server_Microsoft.Winget.Source_8wekyb3d8bbwe",
            "bin", "lua-language-server.exe",
        ))

    return paths


def _probe_on_disk(spec: LangServerSpec) -> Optional[str]:
    """Search the spec-specific extra paths for an existing binary.
    Returns the first match or None. Skips entries that don't exist
    on disk — false positives are eliminated by the actual filesystem
    check rather than guessed install layouts."""
    for p in _windows_extra_search_paths(spec):
        if os.path.isfile(p):
            return p
    return None


def detect_language_servers() -> dict[str, InstalledState]:
    """Walk the SPECS list and probe ``shutil.which`` for each binary.

    Returns a dict keyed by spec name. Each value is an
    :class:`InstalledState` with the resolved binary path (when
    installed) and the recommended installer (when missing).

    Windows: when ``shutil.which`` doesn't find a binary on PATH,
    fall back to ``_probe_on_disk`` to surface the "installed but
    not on PATH" case — this avoids prompting the user to re-install
    something that already exists on disk.
    """
    out: dict[str, InstalledState] = {}
    for spec in SPECS:
        path = shutil.which(spec.bin)
        on_disk = None if path else _probe_on_disk(spec)
        # Only offer to install when the binary is genuinely absent
        # (not on PATH, not on disk). The on-disk case prompts the
        # user to restart their shell instead.
        installer = (
            None if (path or on_disk)
            else select_installer_for(spec)
        )
        out[spec.name] = InstalledState(
            spec=spec,
            installed=bool(path),
            binary_path=path,
            installer_for_missing=installer,
            on_disk_path=on_disk,
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

    # Windows: ``CreateProcess`` does NOT honor ``PATHEXT`` — a bare
    # argv of ``["npm", "install", ...]`` fails with
    # ``WinError 2 ("The system cannot find the file specified")``
    # because the actual binary on disk is ``npm.cmd`` (or ``.bat`` /
    # ``.ps1`` for some managers). ``shutil.which`` DOES walk
    # ``PATHEXT`` and finds the right extension. POSIX: ``shutil.which``
    # returns the absolute path of the same binary, so the substitution
    # is a no-op on Linux / macOS. If the resolution fails (manager
    # not on PATH), keep ``cmd[0]`` as-is so the caller still gets a
    # consistent FileNotFoundError they can surface.
    #
    # This is the same class of bug that bit ``claudemem_reindex.py``
    # — see its ``_resolve_npm_cmd_shim`` for the equivalent fix at
    # that spawn site.
    resolved = shutil.which(cmd[0])
    if resolved:
        cmd = [resolved, *cmd[1:]]

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

    combined = (proc.stdout or "") + "\n" + (proc.stderr or "")

    if proc.returncode == 0:
        # Scoop silent-failure trap: scoop exits 0 even when the
        # manifest isn't found ("Couldn't find manifest for 'X' from
        # 'Y' bucket."), making naive exit-code checks report success
        # for an install that did literally nothing. Catch the phrase
        # explicitly and re-classify as failure.
        if installer is Installer.SCOOP and (
            "Couldn't find manifest" in combined
            or "could not find manifest" in combined.lower()
        ):
            return False, (
                "scoop reported manifest not found (no install performed); "
                "check that the bucket containing this package is added"
            )
        tail = (proc.stdout or "").strip().splitlines()
        last = tail[-1] if tail else "ok"
        return True, last[:200]

    # Winget returns non-zero when the package is already installed at the
    # latest available version. The stdout/stderr contains a distinctive
    # phrase. Treat this as success — clangd / lua-language-server etc are
    # in fact installed. (Winget exit codes like 0x8A150006 / 0x8A150007
    # would be more rigorous but those numeric codes don't always reach
    # ``proc.returncode`` as signed/unsigned cleanly, while the phrase is
    # stable across winget versions.)
    if installer is Installer.WINGET and (
        "No newer package versions are available" in combined
        or "already installed" in combined.lower()
    ):
        return True, "already installed (winget reported no upgrade available)"

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


# --------------------------------------------------------------------- #
# Scoop bootstrap (Windows only)
#
# Scoop is the only path we have to auto-install OmniSharp on Windows
# (no winget package exists). Rather than leaving OmniSharp permanently
# in the MANUAL bucket, ``install.py`` offers a one-shot scoop bootstrap
# when the user opts in. Scoop installs entirely to the user profile —
# no admin elevation, no system-wide PATH changes — making it safe to
# do without sudo. See https://scoop.sh/.
#
# The bootstrap is gated on:
#   - We're on Windows (``os.name == "nt"``).
#   - Scoop is not already on PATH.
#   - The user opted into installing at least one LS whose only
#     installer is SCOOP (e.g. OmniSharp).
#
# ``install.py`` calls ``install_scoop_windows`` then
# ``ensure_scoop_bucket("extras")`` before the install loop dispatches
# any ``scoop install extras/<name>`` commands.
# --------------------------------------------------------------------- #


def is_scoop_installed() -> bool:
    """Return True when ``scoop`` is resolvable on $PATH."""
    return shutil.which("scoop") is not None


def install_scoop_windows(*, dry_run: bool = False) -> tuple[bool, str]:
    """Install scoop via the official PowerShell one-liner.

    Scoop's installer is documented at https://scoop.sh/:

        Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
        Invoke-RestMethod -Uri https://get.scoop.sh | Invoke-Expression

    Both commands run scoped to the current user — no admin needed, no
    machine-wide PATH mutation. Returns ``(ok, message)``; ``message``
    on success is the last stdout line (typically "Scoop was installed
    successfully!").
    """
    if os.name != "nt":
        return False, "scoop bootstrap is Windows-only"

    if is_scoop_installed():
        return True, "scoop already installed"

    if dry_run:
        return True, "[dry-run] would install scoop via PowerShell"

    pwsh = shutil.which("powershell") or shutil.which("pwsh") or "powershell"
    # The semicolon-joined form runs both commands in one PowerShell
    # invocation so the execution-policy change is in effect when
    # Invoke-Expression evaluates the downloaded installer script.
    cmd = [
        pwsh, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
        (
            "Set-ExecutionPolicy -ExecutionPolicy RemoteSigned "
            "-Scope CurrentUser -Force; "
            "Invoke-RestMethod -Uri https://get.scoop.sh | "
            "Invoke-Expression"
        ),
    ]
    log.info("install_scoop_windows: %s", " ".join(cmd))
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, check=False, timeout=600,
        )
    except subprocess.TimeoutExpired:
        return False, "scoop install timed out after 600s"
    except OSError as e:
        return False, f"PowerShell invocation failed: {e}"

    if proc.returncode == 0:
        # ``Set-EnvironmentVariable('PATH', ..., 'User')`` updates the
        # USER PATH in registry — but the running Python process took
        # a snapshot of ``os.environ`` at launch and won't see that
        # change without an explicit refresh. Without this, the very
        # next ``shutil.which("scoop")`` returns ``None`` and
        # ``ensure_scoop_bucket`` immediately reports "scoop not
        # installed", even though the binary IS on disk.
        #
        # Scoop's default layout is ``%USERPROFILE%\scoop\shims\``.
        # Append it idempotently so the rest of this install.py run
        # can find scoop. (Honor ``$SCOOP`` env override for the
        # non-default install dir.)
        scoop_root = os.environ.get(
            "SCOOP", os.path.join(os.path.expanduser("~"), "scoop"),
        )
        shims_dir = os.path.join(scoop_root, "shims")
        current = os.environ.get("PATH", "")
        path_sep = os.pathsep
        path_entries = current.split(path_sep) if current else []
        if shims_dir not in path_entries and os.path.isdir(shims_dir):
            os.environ["PATH"] = (
                shims_dir + path_sep + current if current else shims_dir
            )
            log.info("install_scoop_windows: prepended %s to PATH", shims_dir)

        tail = (proc.stdout or "").strip().splitlines()
        last = tail[-1] if tail else "scoop installed"
        return True, last[:200]

    err = (proc.stderr or proc.stdout or "").strip().splitlines()
    return False, (err[-1] if err else f"exit={proc.returncode}")[:200]


def ensure_scoop_bucket(
    bucket: str, *, dry_run: bool = False,
) -> tuple[bool, str]:
    """Idempotently add a scoop bucket. Returns (ok, message).

    Scoop ships with only the ``main`` bucket. The extras bucket
    (https://github.com/ScoopInstaller/Extras) holds OmniSharp, zls,
    lua-language-server, and most clangd builds. We add it via
    ``scoop bucket add <name>``; if already added, scoop exits with
    status 1 and a recognizable message — we treat that as success.
    """
    if not is_scoop_installed():
        return False, "scoop not installed (call install_scoop_windows first)"

    if dry_run:
        return True, f"[dry-run] would add scoop bucket {bucket}"

    scoop = shutil.which("scoop") or "scoop"
    # Check bucket list first to avoid a noisy non-zero exit.
    try:
        list_proc = subprocess.run(
            [scoop, "bucket", "list"],
            capture_output=True, text=True, check=False, timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, f"scoop bucket list failed: {e}"
    if bucket in (list_proc.stdout or ""):
        return True, f"bucket {bucket} already added"

    try:
        add_proc = subprocess.run(
            [scoop, "bucket", "add", bucket],
            capture_output=True, text=True, check=False, timeout=180,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, f"scoop bucket add failed: {e}"

    if add_proc.returncode == 0:
        return True, f"added bucket {bucket}"

    combined = (add_proc.stdout or "") + "\n" + (add_proc.stderr or "")
    # Defensive: older scoop builds print "already added" with exit-1.
    if "already" in combined.lower():
        return True, f"bucket {bucket} already added"

    err = combined.strip().splitlines()
    return False, (err[-1] if err else f"exit={add_proc.returncode}")[:200]


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
    "ensure_scoop_bucket",
    "install_language_server",
    "install_scoop_windows",
    "is_scoop_installed",
    "select_installer_for",
    "starter_cclsp_json",
    "write_starter_cclsp_json",
]
