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
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePath
from typing import Iterable, Optional


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
    #: External tools the server needs to *produce diagnostics*, as
    #: :data:`TOOL_SPECS` keys. Distinct from ``installers``: the server
    #: starts, handshakes and reports itself healthy without these, and
    #: then returns an empty diagnostic list forever — which is
    #: indistinguishable from a clean file. bash-language-server is the
    #: case that prompted this: it shells out to ``shellcheck`` for
    #: every diagnostic it emits, and neither host had it.
    requires: tuple[str, ...] = ()


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
        # rustup first (it is the toolchain's own component, so it
        # tracks the installed Rust), then winget ahead of scoop on
        # Windows — in-box, no opt-in manager. `Rustlang.rust-analyzer`
        # confirmed present 2026-09-13.
        installers=(Installer.RUSTUP, Installer.BREW,
                    Installer.WINGET, Installer.SCOOP),
        docs_url="https://rust-analyzer.github.io/",
    ),
    LangServerSpec(
        name="clangd",
        display="clangd (C/C++)",
        bin="clangd",
        extensions=("c", "cc", "cpp", "cxx", "h", "hh", "hpp", "hxx",
                    # clangd handles CUDA. These were missing from every
                    # config until 2026-09-16, so .cu/.cuh had no server
                    # at all — which reads as "no problems found".
                    "cu", "cuh"),
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
        requires=("shellcheck",),
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
    # vscode-langservers-extracted — one npm package, three binaries.
    # Microsoft ships these as part of VS Code and does not publish them
    # standalone; hrsh7th's extraction is the canonical source and what
    # every editor distribution uses. Installing any one of the three
    # installs all of them, so the install command repeats by design.
    LangServerSpec(
        name="vscode-html-language-server",
        display="vscode-html-language-server (HTML)",
        bin="vscode-html-language-server",
        extensions=("html", "htm"),
        cclsp_command=("vscode-html-language-server", "--stdio"),
        tier=2,
        installers=(Installer.NPM,),
        docs_url="https://github.com/hrsh7th/vscode-langservers-extracted",
    ),
    LangServerSpec(
        name="vscode-css-language-server",
        display="vscode-css-language-server (CSS/SCSS/Less)",
        bin="vscode-css-language-server",
        extensions=("css", "scss", "less"),
        cclsp_command=("vscode-css-language-server", "--stdio"),
        tier=2,
        installers=(Installer.NPM,),
        docs_url="https://github.com/hrsh7th/vscode-langservers-extracted",
    ),
    LangServerSpec(
        name="vscode-json-language-server",
        display="vscode-json-language-server (JSON)",
        bin="vscode-json-language-server",
        extensions=("json", "jsonc"),
        cclsp_command=("vscode-json-language-server", "--stdio"),
        tier=2,
        installers=(Installer.NPM,),
        docs_url="https://github.com/hrsh7th/vscode-langservers-extracted",
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
# Version probing
#
# A language server too old for the standard in use does not fail
# loudly: it rejects the compilation command and abandons the
# translation unit, which reads as "no diagnostics" — the same thing
# clean code produces.
#
# The trap is that the *default* is the bad one. On Debian bullseye
# /usr/bin/clangd is clangd 11 (2020), and clang only learned the
# `c++23`/`gnu++23` spelling in clang 17 — before that the same standard
# is spelled `c++2b`. Any modern C++ tree therefore dies at line 1
# against the distro default, silently. Observed on solidpc 2026-09-16.
# --------------------------------------------------------------------- #

_VERSION_RE = re.compile(r"version\s+(\d+)(?:\.(\d+))?", re.IGNORECASE)

#: Minimum major version that can analyse a contemporary tree at all.
#: clangd only grew the ``c++23``/``gnu++23`` spelling in **clang 17**;
#: before that the same standard is ``c++2b``, so anything older rejects
#: the -std flag outright and abandons the translation unit.
MIN_USEFUL_VERSION = {
    "clangd": 17,
}

#: Extensions whose analysis needs a *newer* server than the general
#: floor. Measured on solidpc 2026-09-16 against CUDA 13.3 headers:
#: clangd 19.1.7 produced 20 hard errors on a .cu TU that clangd 22.1.6
#: parses clean. 19 fixed the ``gnu++23`` half and still could not read
#: the CUDA headers, so "new enough for C++" is not "new enough for
#: CUDA" — and the failure mode is a screen of fabricated errors about
#: std::atomic and std::_Vector_base that look like real code bugs.
EXTENSION_MIN_VERSION = {
    "cu": ("clangd", 22),
    "cuh": ("clangd", 22),
}


def server_version(binary: str, *, timeout: float = 10.0) -> Optional[str]:
    """Return the first line of ``<binary> --version``, or ``None``."""
    resolved = shutil.which(binary) or binary
    try:
        out = subprocess.run(
            [resolved, "--version"], capture_output=True, text=True,
            timeout=timeout,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    text = (out.stdout or out.stderr or "").strip()
    return text.splitlines()[0] if text else None


def version_major(version_line: Optional[str]) -> Optional[int]:
    m = _VERSION_RE.search(version_line or "")
    return int(m.group(1)) if m else None


#: ``clangd-16``, ``clangd-22`` … Distros ship versioned siblings next
#: to an unversioned default that is often much older.
_VERSIONED_BIN_RE = re.compile(r"^(?P<stem>[A-Za-z_][\w.+-]*?)-(?P<major>\d+)$")


def versioned_siblings(binary: str) -> list[tuple[int, str]]:
    """Find ``<binary>-<N>`` executables on PATH, newest first.

    Pinning a config at a specific version is the only way to escape an
    ancient distro default, and it is also how a config goes stale: the
    pin keeps working while a much newer server sits unused beside it.
    The major version is read from the *name*, so this costs a directory
    scan rather than N subprocess launches.
    """
    stem = PurePath(binary).name
    for suffix in (".exe", ".cmd", ".bat"):
        if stem.lower().endswith(suffix):
            stem = stem[: -len(suffix)]
    m = _VERSIONED_BIN_RE.match(stem)
    if m:                       # already versioned — compare siblings
        stem = m.group("stem")

    found: dict[int, str] = {}
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory:
            continue
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        for entry in entries:
            match = _VERSIONED_BIN_RE.match(entry.name)
            if not match or match.group("stem") != stem:
                continue
            major = int(match.group("major"))
            if major not in found and os.access(entry.path, os.X_OK):
                found[major] = entry.path
    return sorted(found.items(), key=lambda kv: kv[0], reverse=True)


def newer_sibling_note(configured_bin: str,
                       configured_version: Optional[str]) -> Optional[str]:
    """Advise when a newer versioned server is installed but unused."""
    current = version_major(configured_version)
    if current is None:
        return None
    siblings = versioned_siblings(configured_bin)
    if not siblings:
        return None
    best_major, best_path = siblings[0]
    if best_major <= current:
        return None
    return (
        f"{PurePath(configured_bin).name} is v{current}, but v{best_major} is "
        f"installed at {best_path} and unused. A pinned version does not "
        f"follow upgrades — repoint the config if the newer one is intended."
    )


def version_warning(spec_name: str,
                    version_line: Optional[str]) -> Optional[str]:
    """Warn when a server is too old to be *trusted*, not merely old."""
    floor = MIN_USEFUL_VERSION.get(spec_name)
    if floor is None or not version_line:
        return None
    major = version_major(version_line)
    if major is None or major >= floor:
        return None
    return (
        f"{spec_name} is version {major} (< {floor}). It cannot parse a "
        f"modern C/C++ tree: the c++23/gnu++23 spelling only arrived in "
        f"clang 17, so an older server rejects the -std flag and abandons "
        f"the translation unit — which surfaces as ZERO diagnostics, "
        f"indistinguishable from clean code. Debian bullseye's default is "
        f"clangd 11. Official standalone builds (no distro packaging) are "
        f"at https://github.com/clangd/clangd/releases; point the config "
        f"at one by absolute path."
    )


def extension_version_warning(
        extensions: Iterable[str], spec_name: str,
        version_line: Optional[str]) -> Optional[str]:
    """Warn when a server is new enough generally but not for a lane.

    Being past the general floor is not sufficient everywhere: clangd 19
    parses ``gnu++23`` fine and still cannot read CUDA 13.3 headers.
    """
    major = version_major(version_line)
    if major is None:
        return None
    worst: Optional[tuple[str, int]] = None
    for ext in extensions:
        entry = EXTENSION_MIN_VERSION.get(ext)
        if entry and entry[0] == spec_name and major < entry[1]:
            if worst is None or entry[1] > worst[1]:
                worst = (ext, entry[1])
    if worst is None:
        return None
    return (
        f"{spec_name} v{major} claims .{worst[0]} but CUDA analysis needs "
        f"v{worst[1]}+. Measured against CUDA 13.3 headers: v19 emitted 20 "
        f"hard errors on a .cu file that v22 parses clean. The errors are "
        f"fabricated — they name std::atomic and std::_Vector_base and read "
        f"exactly like real code bugs."
    )


# --------------------------------------------------------------------- #
# External tool dependencies
#
# Not language servers — tools a server shells out to. A server whose
# dependency is missing is the worst failure shape in this subsystem:
# it installs, starts, handshakes, and reports healthy, then returns an
# empty diagnostic list for every file. bash-language-server without
# shellcheck was dead on *both* hosts and neither the engine status nor
# the install table said a word.
# --------------------------------------------------------------------- #

@dataclass(frozen=True)
class ToolSpec:
    name: str
    display: str
    bin: str
    why: str                        # what breaks without it, in one line
    installers: tuple[Installer, ...]
    docs_url: Optional[str] = None


TOOL_SPECS: dict[str, ToolSpec] = {
    "shellcheck": ToolSpec(
        name="shellcheck",
        display="ShellCheck",
        bin="shellcheck",
        why="bash-language-server emits no diagnostics at all without it",
        # msys2 is deliberately absent: its package db carries no
        # shellcheck (checked 2026-09-13 against a synced db — the
        # Haskell build is not packaged there), so offering pacman
        # would be an install that cannot succeed. scoop `main` and
        # winget `koalaman.shellcheck` both carry 0.11.0.
        # winget before scoop on Windows: it is in-box on Win10 1909+
        # and needs no opt-in package manager, same rationale as clangd.
        installers=(Installer.APT, Installer.DNF, Installer.BREW,
                    Installer.WINGET, Installer.SCOOP),
        docs_url="https://www.shellcheck.net/",
    ),
}


TOOL_INSTALL_COMMANDS: dict[Installer, dict[str, list[str]]] = {
    Installer.APT: {"shellcheck": ["apt-get", "install", "-y", "shellcheck"]},
    Installer.DNF: {"shellcheck": ["dnf", "install", "-y", "ShellCheck"]},
    Installer.BREW: {"shellcheck": ["brew", "install", "shellcheck"]},
    Installer.SCOOP: {"shellcheck": ["scoop", "install", "shellcheck"]},
    Installer.WINGET: {"shellcheck": [
        "winget", "install", "--id", "koalaman.shellcheck",
        "--silent", "--accept-source-agreements", "--accept-package-agreements",
    ]},
}


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
        # One package, three binaries — see the SPECS note.
        "vscode-html-language-server": [
            "npm", "install", "-g", "vscode-langservers-extracted",
        ],
        "vscode-css-language-server": [
            "npm", "install", "-g", "vscode-langservers-extracted",
        ],
        "vscode-json-language-server": [
            "npm", "install", "-g", "vscode-langservers-extracted",
        ],
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
        "rust-analyzer": [
            "winget", "install", "--id", "Rustlang.rust-analyzer",
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


#: Where source/release installs land when no package manager has the
#: package. Overridable per host via ``CLAUDE_HOOKS_LSP_PREFIX`` — on
#: solidpc that is ``/shared/dev/lsp-servers``. Binaries are symlinked
#: into :data:`RELEASE_BIN_DIR` so they reach ``PATH`` the normal way.
DEFAULT_RELEASE_PREFIX = "/usr/local/lib/lsp-servers"
RELEASE_BIN_DIR = "/usr/local/bin"


def release_prefix() -> str:
    return os.environ.get("CLAUDE_HOOKS_LSP_PREFIX") or DEFAULT_RELEASE_PREFIX


@dataclass(frozen=True)
class ReleaseSpec:
    """Upstream release tarball, for packages no manager carries.

    Distro packaging for language servers is thin and uneven —
    Debian 11 has no ``lua-language-server`` and no ``zls`` at all,
    while Debian 13 and Ubuntu 24.04 do carry the former. Hardcoding
    either answer is wrong, which is why :func:`apt_has_package` asks
    the host instead of assuming.
    """
    repo: str                 # "LuaLS/lua-language-server"
    asset_contains: tuple[str, ...]   # all must appear in the asset name
    asset_excludes: tuple[str, ...] = ()
    bin_subpath: str = ""     # path to the binary inside the extracted tree


#: Keyed by spec name. Only consulted when no package manager works.
RELEASE_SOURCES: dict[str, ReleaseSpec] = {
    "lua-language-server": ReleaseSpec(
        repo="LuaLS/lua-language-server",
        asset_contains=("linux-x64", ".tar.gz"),
        asset_excludes=("musl",),
        bin_subpath="bin/lua-language-server",
    ),
    "zls": ReleaseSpec(
        repo="zigtools/zls",
        asset_contains=("x86_64-linux", ".tar.xz"),
        asset_excludes=(".minisig",),
        bin_subpath="zls",
    ),
}


def apt_has_package(pkg: str, *, timeout: float = 20.0) -> Optional[bool]:
    """Does this host's apt actually offer ``pkg``?

    Asked, never assumed. The installer runs on whatever Debian or
    Ubuntu the user has, and the answer genuinely differs between
    them — proposing ``apt-get install lua-language-server`` on
    Debian 11 produces a confident-looking command that cannot work.
    """
    exe = shutil.which("apt-cache")
    if not exe:
        return None          # cannot check — not the same as "absent"
    try:
        proc = subprocess.run([exe, "policy", pkg], capture_output=True,
                              text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    if not proc.stdout.strip():
        return False         # apt-cache answered, and the answer is "no"
    for line in proc.stdout.splitlines():
        if "Candidate:" in line:
            cand = line.split("Candidate:", 1)[1].strip()
            return bool(cand) and cand != "(none)"
    return False


def dnf_has_package(pkg: str, *, timeout: float = 30.0) -> Optional[bool]:
    """dnf counterpart of :func:`apt_has_package`. ``None`` when the
    question could not be asked."""
    exe = shutil.which("dnf")
    if not exe:
        return None
    try:
        proc = subprocess.run([exe, "list", "--available", pkg],
                              capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return False
    return pkg.lower() in proc.stdout.lower()


def _repo_has_package(installer: Installer, cmd: list[str]) -> bool:
    """Availability probe for repo-backed managers.

    Only a definitive "this distro does not carry it" vetoes the
    installer. A probe that could not run — ``apt-cache`` absent, the
    command erroring — returns ``None`` and is treated as *allow*:
    "could not check" is not "unavailable", and turning an unanswerable
    question into a veto would silently strip apt from hosts that have
    it. Non-repo managers name a package id already verified by hand,
    and probing each would cost a network round trip per candidate.
    """
    if installer is Installer.APT:
        return apt_has_package(cmd[-1]) is not False
    if installer is Installer.DNF:
        return dnf_has_package(cmd[-1]) is not False
    return True


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
        cmd = INSTALL_COMMANDS.get(inst, {}).get(spec.name)
        if not cmd:
            continue
        if not _repo_has_package(inst, cmd):
            # The manager is present but this distro release does not
            # carry the package — keep looking rather than proposing a
            # command that will fail.
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
    return _execute_install(cmd, installer, dry_run=dry_run, timeout=timeout)


def _execute_install(
    cmd: list[str],
    installer: Installer,
    *,
    dry_run: bool = False,
    timeout: float = 600.0,
) -> tuple[bool, str]:
    """Run one install command and classify the result.

    Shared by :func:`install_language_server` and :func:`install_tool`
    so the manager-specific traps below — scoop exiting 0 on a missing
    manifest, winget exiting non-zero when nothing needs upgrading —
    are handled identically no matter what is being installed.
    """

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
# Release (from-source) install — for what no package manager carries
# --------------------------------------------------------------------- #

def _latest_release_asset(rel: ReleaseSpec, *, timeout: float = 30.0):
    """Return ``(tag, asset_name, url)`` for the newest matching asset."""
    import json as _json
    import urllib.request

    url = f"https://api.github.com/repos/{rel.repo}/releases/latest"
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "claude-hooks-installer",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = _json.loads(r.read().decode("utf-8"))
    for asset in data.get("assets", []):
        name = asset.get("name", "")
        if all(t in name for t in rel.asset_contains) and not any(
            x in name for x in rel.asset_excludes
        ):
            return data.get("tag_name", "latest"), name, asset["browser_download_url"]
    raise RuntimeError(
        f"no asset in {rel.repo}@{data.get('tag_name')} matched "
        f"{rel.asset_contains}")


def _safe_extract(archive: str, dest: str) -> None:
    """Extract without letting a member escape ``dest``.

    A tar member may name ``../`` or an absolute path; honouring that
    writes outside the prefix the operator chose.
    """
    import tarfile

    with tarfile.open(archive) as tf:
        base = os.path.realpath(dest)
        for m in tf.getmembers():
            target = os.path.realpath(os.path.join(dest, m.name))
            if not (target == base or target.startswith(base + os.sep)):
                raise RuntimeError(f"unsafe tar member: {m.name}")
        try:
            tf.extractall(dest, filter="data")   # py3.12+/3.11.4+
        except TypeError:                        # pragma: no cover
            tf.extractall(dest)


def install_from_release(
    name: str,
    *,
    prefix: Optional[str] = None,
    bin_dir: Optional[str] = None,
    dry_run: bool = False,
    timeout: float = 600.0,
) -> tuple[bool, str]:
    """Install ``name`` from its upstream release tarball.

    The fallback for packages no manager on this host carries — which
    is the common case for language servers on stable distros, not an
    edge case. Everything lands under ``prefix`` (one directory per
    version, so an upgrade is additive and reversible) with a symlink
    from ``bin_dir`` so it reaches ``PATH`` the ordinary way.
    """
    rel = RELEASE_SOURCES.get(name)
    if rel is None:
        return False, f"no release source registered for {name}"
    if sys.platform not in ("linux", "linux2"):
        return False, (
            f"release install for {name} is wired for Linux assets only; "
            f"use a package manager on {sys.platform}")

    prefix = prefix or release_prefix()
    bin_dir = bin_dir or RELEASE_BIN_DIR

    try:
        tag, asset, url = _latest_release_asset(rel)
    except Exception as e:
        return False, f"could not resolve latest release: {e}"

    target = os.path.join(prefix, f"{name}-{tag}")
    link = os.path.join(bin_dir, name)
    if dry_run:
        return True, (f"[dry-run] would install {name} {tag} from {url} "
                      f"into {target}, symlink {link}")

    import tempfile
    import urllib.request

    try:
        os.makedirs(target, exist_ok=True)
        with tempfile.TemporaryDirectory() as td:
            archive = os.path.join(td, asset)
            req = urllib.request.Request(
                url, headers={"User-Agent": "claude-hooks-installer"})
            with urllib.request.urlopen(req, timeout=timeout) as r, \
                    open(archive, "wb") as f:
                shutil.copyfileobj(r, f)
            _safe_extract(archive, target)
    except Exception as e:
        return False, f"download/extract failed: {e}"

    binary = os.path.join(target, rel.bin_subpath)
    if not os.path.isfile(binary):
        found = None
        for root, _dirs, files in os.walk(target):
            if os.path.basename(rel.bin_subpath) in files:
                found = os.path.join(root, os.path.basename(rel.bin_subpath))
                break
        if not found:
            return False, f"binary {rel.bin_subpath} not found under {target}"
        binary = found
    try:
        os.chmod(binary, 0o755)
        tmp_link = link + ".new"
        if os.path.lexists(tmp_link):
            os.unlink(tmp_link)
        os.symlink(binary, tmp_link)
        os.replace(tmp_link, link)
    except OSError as e:
        return False, (f"installed to {target} but could not link {link}: {e} "
                       f"(need write access to {bin_dir}?)")
    return True, f"{name} {tag} -> {binary} (linked at {link})"


# --------------------------------------------------------------------- #
# External tool dependencies — detection + install
# --------------------------------------------------------------------- #

@dataclass(frozen=True)
class ToolState:
    spec: ToolSpec
    installed: bool
    path: Optional[str]
    installer_for_missing: Optional[Installer]
    #: Language servers that need this tool and are themselves
    #: installed — i.e. the ones currently producing nothing.
    needed_by: tuple[str, ...] = ()


def select_installer_for_tool(spec: ToolSpec) -> Optional[Installer]:
    """First installer that is allowed on this OS, present on PATH, and
    has a command registered for ``spec``. Mirrors
    :func:`select_installer_for`."""
    plat = _current_platform()
    for inst in spec.installers:
        if inst is Installer.MANUAL:
            continue
        if not _installer_allowed_on(inst, plat):
            continue
        if not _manager_available(inst):
            continue
        cmd = TOOL_INSTALL_COMMANDS.get(inst, {}).get(spec.name)
        if not cmd:
            continue
        if not _repo_has_package(inst, cmd):
            continue
        return inst
    return None


def detect_tools(
    server_state: Optional[dict[str, "InstalledState"]] = None,
) -> dict[str, ToolState]:
    """Which external dependencies are present, and who is waiting.

    ``needed_by`` lists only *installed* servers, because a missing
    dependency for a server you don't have is not a problem — while a
    missing one for a server you do have is a server that reports
    healthy and returns nothing.
    """
    if server_state is None:
        server_state = detect_language_servers()
    waiting: dict[str, list[str]] = {}
    for st in server_state.values():
        if not st.installed:
            continue
        for dep in st.spec.requires:
            waiting.setdefault(dep, []).append(st.spec.name)

    out: dict[str, ToolState] = {}
    for name, spec in TOOL_SPECS.items():
        path = shutil.which(spec.bin)
        out[name] = ToolState(
            spec=spec,
            installed=bool(path),
            path=path,
            installer_for_missing=None if path else select_installer_for_tool(spec),
            needed_by=tuple(sorted(waiting.get(name, ()))),
        )
    return out


def install_tool(
    spec: ToolSpec,
    installer: Installer,
    *,
    dry_run: bool = False,
    timeout: float = 600.0,
) -> tuple[bool, str]:
    """Install one external dependency. Same contract as
    :func:`install_language_server`."""
    cmd = TOOL_INSTALL_COMMANDS.get(installer, {}).get(spec.name)
    if not cmd:
        return False, (
            f"no install command registered for {spec.name} "
            f"via {installer.value}"
        )
    return _execute_install(cmd, installer, dry_run=dry_run, timeout=timeout)


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
