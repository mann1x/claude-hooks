"""Filesystem tools exposed to the LLM via OpenAI tool-calling.

All paths are resolved against a single ``cwd`` (the directory caliber
is invoked in). Any attempt to escape the cwd via ``..`` or an absolute
path pointing elsewhere is rejected — the tools are deliberately read-only
and scoped, so prompt-injection that tries to exfiltrate e.g. ``/etc/passwd``
hits an explicit error instead of succeeding.

Each tool returns a plain string — the LLM sees that as the ``tool``
message content on the next turn.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

log = logging.getLogger("claude_hooks.caliber_proxy.tools")

# Per-call size caps so a chatty tool call can't pin the model or blow
# past the context window. Small enough to keep latency tolerable;
# callers can pass explicit line ranges for precision.
_READ_MAX_BYTES = 48 * 1024
_LIST_MAX_ENTRIES = 500
_GLOB_MAX_ENTRIES = 500
_GREP_MAX_MATCHES = 200
_GREP_MAX_FILE_BYTES = 2 * 1024 * 1024


# -- Path resolution --------------------------------------------------- #
def resolve_in_cwd(raw: str, cwd: str) -> str:
    """Resolve ``raw`` relative to ``cwd`` and raise if the result is
    outside the cwd. Returns the absolute real path.
    """
    cwd_real = os.path.realpath(cwd)
    joined = os.path.join(cwd_real, raw) if not os.path.isabs(raw) else raw
    resolved = os.path.realpath(joined)
    if resolved != cwd_real and not resolved.startswith(cwd_real + os.sep):
        raise ValueError(f"path escapes cwd: {raw!r}")
    return resolved


def _to_rel(abs_path: str, cwd: str) -> str:
    try:
        return os.path.relpath(abs_path, cwd)
    except ValueError:
        return abs_path


# -- Tool: list_files -------------------------------------------------- #
def list_files(args: dict, cwd: str) -> str:
    raw_path = str(args.get("path") or ".")
    try:
        abs_path = resolve_in_cwd(raw_path, cwd)
    except ValueError as e:
        return f"error: {e}"
    if not os.path.exists(abs_path):
        return f"error: path not found: {raw_path}"
    if not os.path.isdir(abs_path):
        return f"error: not a directory: {raw_path}"
    entries = []
    try:
        for name in sorted(os.listdir(abs_path)):
            full = os.path.join(abs_path, name)
            is_dir = os.path.isdir(full)
            rel = _to_rel(full, cwd)
            entries.append(f"{rel}{'/' if is_dir else ''}")
            if len(entries) >= _LIST_MAX_ENTRIES:
                entries.append(f"... (truncated at {_LIST_MAX_ENTRIES})")
                break
    except OSError as e:
        return f"error: {e}"
    if not entries:
        return "(empty directory)"
    return "\n".join(entries)


# -- Tool: read_file --------------------------------------------------- #
def read_file(args: dict, cwd: str) -> str:
    raw_path = str(args.get("path") or "")
    start = args.get("start_line")
    end = args.get("end_line")
    if not raw_path:
        return "error: path is required"
    try:
        abs_path = resolve_in_cwd(raw_path, cwd)
    except ValueError as e:
        return f"error: {e}"
    if not os.path.isfile(abs_path):
        return f"error: not a file: {raw_path}"
    try:
        with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError as e:
        return f"error: {e}"

    total = len(lines)
    s = 1 if start is None else max(1, int(start))
    e = total if end is None else min(total, int(end))
    if s > total:
        return f"error: start_line {s} exceeds file length {total}"
    if e < s:
        return f"error: end_line {e} is before start_line {s}"

    selected = lines[s - 1:e]
    # Enforce byte cap — the model doesn't need 50KB per chunk.
    body = "".join(selected)
    truncated = False
    if len(body.encode("utf-8", errors="replace")) > _READ_MAX_BYTES:
        body = body.encode("utf-8", errors="replace")[:_READ_MAX_BYTES].decode(
            "utf-8", errors="replace"
        )
        truncated = True

    header = f"{_to_rel(abs_path, cwd)}:{s}-{e}  (file has {total} lines)"
    if truncated:
        header += "  [output truncated to ~48 KB — use a smaller line range for more]"
    # Number each line so the model can cite path:line confidently.
    numbered = "\n".join(
        f"{s + i:>6}: {line.rstrip()}" for i, line in enumerate(body.splitlines())
    )
    return f"{header}\n{numbered}"


# -- Tool: glob -------------------------------------------------------- #
def glob_files(args: dict, cwd: str) -> str:
    pattern = str(args.get("pattern") or "")
    if not pattern:
        return "error: pattern is required"
    cwd_real = os.path.realpath(cwd)
    matches = []
    try:
        for root, dirs, files in os.walk(cwd_real):
            # Skip .git and other heavy dirs by convention.
            dirs[:] = [d for d in dirs if d not in
                       {".git", "node_modules", "__pycache__", ".venv",
                        "venv", ".claude", ".caliber", ".wolf", "dist",
                        "build", ".cache", "target"}]
            for name in files:
                full = os.path.join(root, name)
                rel = os.path.relpath(full, cwd_real)
                if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(name, pattern):
                    matches.append(rel)
                    if len(matches) >= _GLOB_MAX_ENTRIES:
                        matches.append(f"... (truncated at {_GLOB_MAX_ENTRIES})")
                        return "\n".join(matches)
    except OSError as e:
        return f"error: {e}"
    if not matches:
        return f"(no matches for {pattern!r})"
    return "\n".join(matches)


# -- Tool: grep -------------------------------------------------------- #
def grep(args: dict, cwd: str) -> str:
    pattern = str(args.get("pattern") or "")
    path = str(args.get("path") or ".")
    case_insensitive = bool(args.get("case_insensitive", False))
    if not pattern:
        return "error: pattern is required"
    try:
        abs_root = resolve_in_cwd(path, cwd)
    except ValueError as e:
        return f"error: {e}"
    try:
        flags = re.IGNORECASE if case_insensitive else 0
        rx = re.compile(pattern, flags)
    except re.error as e:
        return f"error: invalid regex: {e}"

    cwd_real = os.path.realpath(cwd)
    matches: list[str] = []

    def _match_file(abs_file: str) -> None:
        nonlocal matches
        try:
            sz = os.path.getsize(abs_file)
        except OSError:
            return
        if sz > _GREP_MAX_FILE_BYTES:
            return
        try:
            with open(abs_file, "r", encoding="utf-8", errors="replace") as f:
                for lineno, line in enumerate(f, 1):
                    if rx.search(line):
                        rel = os.path.relpath(abs_file, cwd_real)
                        matches.append(f"{rel}:{lineno}: {line.rstrip()[:200]}")
                        if len(matches) >= _GREP_MAX_MATCHES:
                            return
        except OSError:
            return

    if os.path.isfile(abs_root):
        _match_file(abs_root)
    elif os.path.isdir(abs_root):
        for root, dirs, files in os.walk(abs_root):
            dirs[:] = [d for d in dirs if d not in
                       {".git", "node_modules", "__pycache__", ".venv",
                        "venv", ".claude", ".caliber", ".wolf", "dist",
                        "build", ".cache", "target"}]
            for name in files:
                _match_file(os.path.join(root, name))
                if len(matches) >= _GREP_MAX_MATCHES:
                    break
            if len(matches) >= _GREP_MAX_MATCHES:
                matches.append(f"... (truncated at {_GREP_MAX_MATCHES})")
                break
    else:
        return f"error: not a file or directory: {path}"

    if not matches:
        return f"(no matches for /{pattern}/ in {path})"
    return "\n".join(matches)


# -- Dispatch --------------------------------------------------------- #
TOOL_IMPLS = {
    "list_files": list_files,
    "read_file": read_file,
    "glob": glob_files,
    "grep": grep,
}


def execute(name: str, raw_args: str, cwd: str) -> str:
    """Parse ``raw_args`` JSON, call the matching tool, return its string
    output. Never raises — errors are returned as ``error: ...`` strings
    so the model sees them and can recover.
    """
    impl = TOOL_IMPLS.get(name)
    if impl is None:
        return f"error: unknown tool {name!r}"
    try:
        args = json.loads(raw_args) if raw_args else {}
    except json.JSONDecodeError as e:
        return f"error: tool arguments not valid JSON: {e}"
    if not isinstance(args, dict):
        return "error: tool arguments must be a JSON object"
    try:
        result = impl(args, cwd)
    except Exception as e:  # pragma: no cover - defensive
        log.warning("tool %s raised: %s", name, e)
        return f"error: tool raised: {e}"
    return result if isinstance(result, str) else str(result)


# -- Tool schema sent to the model ------------------------------------ #
def openai_tool_specs() -> list[dict[str, Any]]:
    """OpenAI function-calling schemas for the 4 tools."""
    return [
        {
            "type": "function",
            "function": {
                "name": "list_files",
                "description": (
                    "List the entries in a project directory. Use this "
                    "before read_file to confirm a path exists. Scoped to "
                    "the project root; paths with .. or absolute paths "
                    "outside the project are rejected."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Relative directory path. "
                            "Defaults to project root.",
                        }
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": (
                    "Read a file's contents with line numbers prepended "
                    "so you can cite `path:line` accurately. Use start_line "
                    "and end_line to fetch specific ranges (recommended "
                    "for large files — output is capped at ~48 KB)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "start_line": {"type": "integer"},
                        "end_line": {"type": "integer"},
                    },
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "glob",
                "description": (
                    "Find files matching a glob pattern (fnmatch syntax). "
                    "Use * and ? and ** as wildcards. Returns paths "
                    "relative to project root; capped at 500 matches."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pattern": {
                            "type": "string",
                            "description": "Glob pattern, e.g. "
                            "`claude_hooks/**/*.py`",
                        }
                    },
                    "required": ["pattern"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "grep",
                "description": (
                    "Search for a regex pattern across files under a path. "
                    "Returns matching lines prefixed with `path:line:`. "
                    "Capped at 200 matches."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pattern": {
                            "type": "string",
                            "description": "Python regex (re syntax)",
                        },
                        "path": {
                            "type": "string",
                            "description": "File or directory to search. "
                            "Defaults to project root.",
                        },
                        "case_insensitive": {"type": "boolean"},
                    },
                    "required": ["pattern"],
                },
            },
        },
    ]
