"""Shared helpers for the ``coder_mlang`` suite's per-language
oracles.

Each oracle pytest file imports ``compile_and_run(lang, ...)`` and
calls it with stdin / argv / timeout. The helper hides the
per-language ceremony (rustc / go build / gcc / g++ / dotnet)
behind a uniform interface that returns ``(returncode, stdout,
stderr)`` — same shape `subprocess.run` exposes.

The dispatcher is intentionally tiny — the heavy lifting is per-
language `_compile_<lang>` functions that produce an executable
binary path. ``compile_and_run`` then runs the binary in a fresh
temp dir so the model's sandbox stays read-only at run time.

Toolchain probing lives in ``probe_toolchain_versions()`` — the
harness calls it once per run and writes the result into
``metadata.json`` for provenance.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional


SUPPORTED_LANGUAGES = ("python", "rust", "go", "c", "cpp", "csharp")

# Source-file extensions by language. The bench's SUITE.md
# ``sandbox_path`` field MUST agree with these.
SOURCE_EXT_BY_LANG = {
    "python": ".py",
    "rust":   ".rs",
    "go":     ".go",
    "c":      ".c",
    "cpp":    ".cpp",
    "csharp": ".cs",
}


# ============================================================== #
# Toolchain probing
# ============================================================== #

def _probe_one(cmd: list[str]) -> Optional[str]:
    """Run ``cmd``, return first line of stdout or None on failure.

    Used only for advertised-version capture; never raises so a
    missing toolchain falls back to None rather than crashing the
    bench at start-up.
    """
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    text = (r.stdout or r.stderr or "").strip()
    return text.splitlines()[0] if text else None


def probe_toolchain_versions() -> dict[str, Optional[str]]:
    """Probe every supported toolchain. Returns a dict of
    ``{toolchain: version_string_or_None}``. A None entry means
    the toolchain isn't installed — questions in that language
    will be skipped at run time with a clear diagnostic.
    """
    return {
        "python": _probe_one(["python3", "--version"]),
        "rustc":  _probe_one(["rustc", "--version"]),
        "go":     _probe_one(["go", "version"]),
        "gcc":    _probe_one(["gcc", "--version"]),
        "g++":    _probe_one(["g++", "--version"]),
        "dotnet": _probe_one(["dotnet", "--version"]),
    }


def toolchain_required(lang: str) -> str:
    """Map a language to its primary toolchain key. Used by the
    bench to skip questions when ``probe_toolchain_versions()``
    reports the toolchain is missing.
    """
    return {
        "python": "python",
        "rust":   "rustc",
        "go":     "go",
        "c":      "gcc",
        "cpp":    "g++",
        "csharp": "dotnet",
    }[lang]


# ============================================================== #
# Per-language compile steps
# ============================================================== #
#
# Each ``_compile_<lang>`` function takes the model's source
# path (read-only) + a build-dir and returns the path to the
# produced executable. They raise ``CompileError`` with the
# compiler's stderr on failure so the oracle can surface it
# directly to pytest.
# ============================================================== #

class CompileError(Exception):
    """Raised when the language toolchain rejects the model's
    source. The compiler's stderr lives in ``.stderr`` for the
    oracle to attach to its assertion message.
    """

    def __init__(self, lang: str, returncode: int, stderr: str):
        self.lang = lang
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(
            f"{lang} compile failed (rc={returncode}): "
            f"{(stderr or '')[:200]}"
        )


def _compile_rust(source: Path, build_dir: Path) -> Path:
    binary = build_dir / "sol"
    r = subprocess.run(
        ["rustc", "-O", "-o", str(binary), str(source)],
        capture_output=True, text=True, timeout=120,
    )
    if r.returncode != 0:
        raise CompileError("rust", r.returncode, r.stderr)
    return binary


def _compile_go(source: Path, build_dir: Path) -> Path:
    binary = build_dir / "sol"
    # ``go build`` doesn't accept arbitrary file extensions in
    # arbitrary locations gracefully — link the source into a
    # fresh subdir named after its basename so the package layout
    # is conventional.
    pkg_dir = build_dir / "pkg"
    pkg_dir.mkdir(parents=True, exist_ok=True)
    linked = pkg_dir / source.name
    shutil.copy(source, linked)
    r = subprocess.run(
        ["go", "build", "-o", str(binary), str(linked)],
        capture_output=True, text=True, timeout=120,
        cwd=str(pkg_dir),
    )
    if r.returncode != 0:
        raise CompileError("go", r.returncode, r.stderr)
    return binary


def _compile_c(source: Path, build_dir: Path) -> Path:
    binary = build_dir / "sol"
    r = subprocess.run(
        ["gcc", "-O2", "-Wall", "-Wextra",
         "-o", str(binary), str(source), "-lm"],
        capture_output=True, text=True, timeout=120,
    )
    if r.returncode != 0:
        raise CompileError("c", r.returncode, r.stderr)
    return binary


def _compile_cpp(source: Path, build_dir: Path) -> Path:
    binary = build_dir / "sol"
    r = subprocess.run(
        ["g++", "-O2", "-std=c++17", "-Wall", "-Wextra",
         "-o", str(binary), str(source), "-lpthread"],
        capture_output=True, text=True, timeout=120,
    )
    if r.returncode != 0:
        raise CompileError("cpp", r.returncode, r.stderr)
    return binary


_CSPROJ_TEMPLATE = (
    '<Project Sdk="Microsoft.NET.Sdk">\n'
    '  <PropertyGroup>\n'
    '    <OutputType>Exe</OutputType>\n'
    '    <TargetFramework>net8.0</TargetFramework>\n'
    '    <Nullable>enable</Nullable>\n'
    '    <ImplicitUsings>enable</ImplicitUsings>\n'
    '    <RootNamespace>Sol</RootNamespace>\n'
    '    <AssemblyName>sol</AssemblyName>\n'
    '  </PropertyGroup>\n'
    '</Project>\n'
)


def _compile_csharp(source: Path, build_dir: Path) -> Path:
    """Generate a minimal csproj that wraps ``source``, then build
    via ``dotnet publish``. Returns the publish-output binary
    path. Slow (~5-10 s the first time per host as the SDK warms),
    but correct.
    """
    proj_dir = build_dir / "csproj"
    proj_dir.mkdir(parents=True, exist_ok=True)
    (proj_dir / "solution.csproj").write_text(
        _CSPROJ_TEMPLATE, encoding="utf-8",
    )
    shutil.copy(source, proj_dir / "Program.cs")
    publish_dir = build_dir / "publish"
    r = subprocess.run(
        ["dotnet", "publish",
         "-c", "Release",
         "--nologo",
         "--verbosity", "quiet",
         "-o", str(publish_dir),
         str(proj_dir / "solution.csproj")],
        capture_output=True, text=True, timeout=180,
    )
    if r.returncode != 0:
        raise CompileError("csharp", r.returncode, r.stdout + r.stderr)
    binary = publish_dir / "sol"
    if not binary.is_file():
        # SDK builds produce e.g. ``sol.dll`` + a launcher; honor
        # whichever shape the SDK emitted.
        candidates = list(publish_dir.glob("sol*"))
        if not candidates:
            raise CompileError(
                "csharp", -1,
                f"no binary in {publish_dir}: "
                f"{[p.name for p in publish_dir.iterdir()]}"
            )
        binary = candidates[0]
    return binary


_COMPILE_BY_LANG = {
    "rust":   _compile_rust,
    "go":     _compile_go,
    "c":      _compile_c,
    "cpp":    _compile_cpp,
    "csharp": _compile_csharp,
}


# ============================================================== #
# Public API
# ============================================================== #

def compile_and_run(*,
                    lang: str,
                    source: Path,
                    stdin_input: str = "",
                    argv: Optional[list[str]] = None,
                    timeout_s: int = 15) -> tuple[int, str, str]:
    """Compile + run the model's source. Returns
    ``(returncode, stdout, stderr)`` after the run step.

    Raises ``CompileError`` if the toolchain rejects the source —
    oracles catch and surface to pytest with a useful assertion
    message.

    Raises ``AssertionError`` (well, returns rc=124 + diagnostic
    stderr) on a runtime timeout, matching pytest's expectation
    that an oracle test asserts on stdout/rc rather than reading
    runtime exceptions.

    ``lang`` must be one of ``SUPPORTED_LANGUAGES`` minus
    ``"python"`` — Python oracles import the module directly, the
    pattern that's already wired in ``coder@1.0``.
    """
    if lang not in _COMPILE_BY_LANG:
        raise ValueError(
            f"unsupported lang {lang!r}; expected one of "
            f"{tuple(_COMPILE_BY_LANG)}"
        )
    if not source.is_file():
        # Match the conventional ``__file__`` lookup error shape so
        # the rationale in trials.jsonl is informative.
        sibling = []
        try:
            sibling = sorted(p.name for p in source.parent.iterdir())
        except OSError:
            pass
        raise AssertionError(
            f"missing source {source.name}; sandbox contains: {sibling}"
        )
    # ``dotnet`` honors a global tools cache outside the build_dir
    # so the temp dir is small even for C#.
    with tempfile.TemporaryDirectory(prefix="mlang_build_") as td:
        build_dir = Path(td)
        compile_fn = _COMPILE_BY_LANG[lang]
        binary = compile_fn(source, build_dir)
        # Special-case C# — `dotnet` produces an executable but its
        # invocation needs the DLL passed through if the SDK didn't
        # emit a native launcher.
        if binary.suffix == ".dll":
            cmd = ["dotnet", str(binary), *(argv or [])]
        else:
            cmd = [str(binary), *(argv or [])]
        try:
            r = subprocess.run(
                cmd, input=stdin_input,
                capture_output=True, text=True, timeout=timeout_s,
            )
        except subprocess.TimeoutExpired as e:
            return 124, e.stdout or "", (
                f"timeout after {timeout_s}s\n"
                f"partial stdout: {(e.stdout or '')[:200]!r}\n"
                f"partial stderr: {(e.stderr or '')[:200]!r}"
            )
        return r.returncode, r.stdout, r.stderr


__all__ = [
    "CompileError",
    "SOURCE_EXT_BY_LANG",
    "SUPPORTED_LANGUAGES",
    "compile_and_run",
    "probe_toolchain_versions",
    "toolchain_required",
]
