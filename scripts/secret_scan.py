#!/usr/bin/env python3
"""Secret gate for claude-hooks commits.

    python scripts/secret_scan.py --staged        # pre-commit (fail-closed)
    python scripts/secret_scan.py --history       # every commit (CI)
    python scripts/secret_scan.py --install-hook  # wire --staged into .git/hooks/pre-commit

Two checks, both required:

1. gitleaks with ``.gitleaks.toml`` (its default rules plus URL
   credentials, secret-named keys in config files, and config / key
   files that must never be committed). The stock rules alone scanned
   all 1,034 commits clean while c2f2caa had committed the pgvector DSN
   with its password: no default rule reads a password in a URL.
2. This host's own secrets, read from where they live (the configs'
   DSNs and secret-named values, every ``key_file``, ``~/.claude``
   key files, secret-named environment variables) and searched for in
   every added line — any 12-character run of one, so a prefix or a
   value split across strings is caught too. No rule can do this: it
   knows the values. They are never printed; a finding names the file,
   the line and where the secret lives.

A missing gitleaks fails the commit: a gate that is skipped when its
tool is absent is no gate. Bypass deliberately with ``git commit
--no-verify``.
"""
from __future__ import annotations

import argparse
import glob
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable, Optional

REPO = Path(__file__).resolve().parent.parent
CONFIG = REPO / ".gitleaks.toml"
IGNORE = REPO / ".gitleaksignore"
GRAM = 12                 # a run this long of a secret is a leak
MIN_SECRET = 8            # shorter values are too common to search for
HOOK_MARK = "claude-hooks secret gate"
HOOK_END = f"# <<< {HOOK_MARK} <<<"
# Hooks are shared by every worktree; one checked out before this script
# existed uses the main checkout's copy, which scans the cwd's index.
HOOK_BLOCK = f"""# >>> {HOOK_MARK} (scripts/secret_scan.py --install-hook) >>>
if command -v python3 >/dev/null 2>&1; then _sg_py=python3; else _sg_py=python; fi
_sg=scripts/secret_scan.py
[ -f "$_sg" ] || _sg="$(git rev-parse --git-common-dir)/../scripts/secret_scan.py"
"$_sg_py" "$_sg" --staged || exit 1
{HOOK_END}
"""

_URL_PW = re.compile(r"(?i)\b[a-z][a-z0-9+.\-]{1,24}://[^\s:/@'\"<>`]{1,64}:"
                     r"([^\s@/'\"<>`]{3,200})@")
_SECRET_KEY = re.compile(
    r"(?i)[\"']?(?:[a-z0-9]+[_.\-])*(password|passwd|passphrase|pwd|secret|"
    r"token|api[_\-]?key|apikey|access[_\-]?key|secret[_\-]?key|"
    r"private[_\-]?key|client[_\-]?secret|auth[_\-]?token|bearer|"
    r"authorization)[\"']?\s*[:=]\s*[\"']?(?:bearer\s+)?([^\s\"',;#}\]]{8,})")
_KEY_FILE = re.compile(r"(?i)[\"']?[a-z_]*key_file[\"']?\s*[:=]\s*[\"']([^\"']+)[\"']")
_NOT_A_VALUE = re.compile(
    r"(?i)^(\$\{?[a-z_][a-z0-9_]*\}?|(~|\.{1,2})?/.*|[a-z]:[\\/].*|%[a-z_]+%.*|"
    r"[0-9_.]+|true|false|null|none|redacted|changeme|placeholder|"
    r"[a-z][a-z0-9+.\-]*://.*|<[^>]*>?|\{\{.*|x{3,}|\*+|your[_\-a-z0-9]*|"
    r".*(test[_\-]?only|not[_\-]?a[_\-]?real).*|-----BEGIN .*)$")
_SECRET_ENV = re.compile(r"(?i)(password|passwd|secret|token|api_?key|"
                         r"access_?key|private_?key|credential|dsn)")


# ---------------------------------------------------------------- gitleaks
def find_gitleaks() -> Optional[str]:
    exe = os.environ.get("GITLEAKS") or shutil.which("gitleaks")
    if exe:
        return exe
    for c in (os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links\gitleaks.exe"),
              os.path.expanduser("~/scoop/shims/gitleaks.exe"),
              "/usr/local/bin/gitleaks", "/usr/bin/gitleaks"):
        if c and os.path.isfile(c):
            return c
    return None


def _top() -> str:
    """The worktree being committed (the hook runs at its top), else
    this checkout."""
    out = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                         capture_output=True, text=True)
    return out.stdout.strip() if out.returncode == 0 and out.stdout.strip() \
        else str(REPO)


def run_gitleaks(mode: str) -> int:
    exe = find_gitleaks()
    if exe is None:
        print("secret_scan: gitleaks is not installed — the commit is refused "
              "(install: apt/brew `gitleaks`, Windows `winget install "
              "Gitleaks.Gitleaks`; bypass deliberately with --no-verify)",
              file=sys.stderr)
        return 2
    cmd = [exe, "git", "--config", str(CONFIG), "--redact", "--no-banner",
           "--exit-code", "1"]
    if IGNORE.is_file():
        cmd += ["--gitleaks-ignore-path", str(IGNORE)]
    if mode == "staged":
        cmd += ["--staged", "-v"]
    cmd.append(_top())
    return subprocess.run(cmd).returncode


# ----------------------------------------------------------- host secrets
def _config_files() -> list[Path]:
    home = Path.home()
    return [REPO / "config" / "claude-hooks.json",
            REPO / ".claude-hooks" / "consultants.toml",
            home / ".claude" / "consultants-config.toml",
            home / ".claude.json",
            home / ".claude" / "settings.json",
            home / ".claude" / "settings.local.json"]


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _first_line(p: Path) -> str:
    text = _read(p)
    return text.splitlines()[0].strip() if text.strip() else ""


def _is_value(v: str) -> bool:
    return len(v) >= MIN_SECRET and not _NOT_A_VALUE.match(v)


def known_secrets(config_files: Optional[Iterable[Path]] = None,
                  key_globs: Optional[Iterable[str]] = None,
                  environ: Optional[dict] = None) -> list[tuple[str, str]]:
    """``(where it lives, value)`` for every secret this host holds."""
    out: dict[str, str] = {}
    home = Path.home()

    def add(label: str, value: str) -> None:
        value = value.strip().strip("\"'")
        if _is_value(value):
            out.setdefault(value, label)

    for p in (list(config_files) if config_files is not None
              else _config_files()):
        text = _read(Path(p))
        if not text:
            continue
        label = str(p).replace(str(home), "~")
        for m in _URL_PW.finditer(text):
            add(f"a URL password in {label}", m.group(1))
        for m in _SECRET_KEY.finditer(text):
            add(f"a secret value in {label}", m.group(2))
        for m in _KEY_FILE.finditer(text):
            kf = Path(os.path.expanduser(m.group(1)))
            if kf.is_file():
                add(str(kf).replace(str(home), "~"), _first_line(kf))
    for pattern in (list(key_globs) if key_globs is not None else
                    [str(home / ".claude" / "*api-key*"),
                     str(home / ".claude" / "*.key"),
                     str(home / "*.key")]):
        for f in glob.glob(os.path.expanduser(pattern)):
            if os.path.isfile(f):
                add(f.replace(str(home), "~"), _first_line(Path(f)))
    for name, value in (environ if environ is not None else os.environ).items():
        if _SECRET_ENV.search(name):
            add(f"${name}", value)
    return [(label, value) for value, label in out.items()]


def added_lines(diff: str) -> Iterable[tuple[str, int, str]]:
    """``(file, line, text)`` for each added line of a ``-U0`` diff."""
    path, line = "", 0
    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            path = raw[6:] if raw.startswith("+++ b/") else raw[4:]
        elif raw.startswith("@@"):
            m = re.search(r"\+(\d+)", raw)
            line = int(m.group(1)) if m else 0
        elif raw.startswith("+"):
            yield path, line, raw[1:]
            line += 1


def find_known(lines: Iterable[tuple[str, int, str]],
               secrets: list[tuple[str, str]]) -> list[tuple[str, int, str]]:
    """``(file, line, where the secret lives)`` for each line that carries
    a secret or a run of :data:`GRAM` characters of one."""
    grams: dict[str, str] = {}
    for label, value in secrets:
        w = min(GRAM, len(value))
        for i in range(len(value) - w + 1):
            grams.setdefault(value[i:i + w], label)
    sizes = sorted({len(g) for g in grams}, reverse=True)
    hits = []
    for path, n, text in lines:
        found = None
        for w in sizes:
            for i in range(len(text) - w + 1):
                found = grams.get(text[i:i + w])
                if found:
                    break
            if found:
                break
        if found:
            hits.append((path, n, found))
    return hits


def check_known_staged() -> int:
    secrets = known_secrets()
    if not secrets:
        return 0
    diff = subprocess.run(
        ["git", "-C", _top(), "diff", "--cached", "-U0", "--no-color",
         "--no-ext-diff", "--text"],
        capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
    hits = find_known(added_lines(diff), secrets)
    for path, n, label in hits:
        print(f"secret_scan: {path}:{n} carries this host's secret from "
              f"{label} — the commit is refused", file=sys.stderr)
    return 1 if hits else 0


# ------------------------------------------------------------------- hook
def install_hook(git_dir: Optional[Path] = None) -> str:
    """Put the gate at the top of ``pre-commit`` (after its shebang),
    once. Returns what it did."""
    if git_dir is None:
        out = subprocess.run(["git", "-C", str(REPO), "rev-parse",
                              "--git-common-dir"], capture_output=True, text=True)
        git_dir = Path(out.stdout.strip())
        if not git_dir.is_absolute():
            git_dir = REPO / git_dir
    hook = git_dir / "hooks" / "pre-commit"
    text = _read(hook)
    if HOOK_BLOCK in text:
        return "current"
    if HOOK_MARK in text and HOOK_END in text:          # an older block
        head, _, rest = text.partition(f"# >>> {HOOK_MARK}")
        _, _, tail = rest.partition(HOOK_END + "\n")
        hook.write_text(head + HOOK_BLOCK + tail, encoding="utf-8", newline="\n")
        return "updated"
    if not text:
        new = "#!/bin/sh\n" + HOOK_BLOCK
    elif text.startswith("#!"):
        first, _, rest = text.partition("\n")
        new = f"{first}\n{HOOK_BLOCK}{rest}"
    else:
        new = HOOK_BLOCK + text
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(new, encoding="utf-8", newline="\n")
    try:
        hook.chmod(hook.stat().st_mode | 0o111)
    except OSError:
        pass
    return "installed"


def hook_installed(git_dir: Optional[Path] = None) -> bool:
    if git_dir is None:
        out = subprocess.run(["git", "-C", str(REPO), "rev-parse",
                              "--git-common-dir"], capture_output=True, text=True)
        git_dir = Path(out.stdout.strip())
        if not git_dir.is_absolute():
            git_dir = REPO / git_dir
    return HOOK_MARK in _read(git_dir / "hooks" / "pre-commit")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--staged", action="store_true")
    g.add_argument("--history", action="store_true")
    g.add_argument("--install-hook", action="store_true")
    a = ap.parse_args(argv)
    if a.install_hook:
        print(f"secret gate hook: {install_hook()}")
        return 0
    rc = run_gitleaks("staged" if a.staged else "history")
    if a.staged:
        rc = max(rc, check_known_staged())
    return 1 if rc else 0


if __name__ == "__main__":
    sys.exit(main())
