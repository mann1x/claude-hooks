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

    Forgives common quoting mistakes from the model: leading/trailing
    backticks (gemma sometimes copies the markdown-quoted form straight
    out of the system prompt) and surrounding whitespace are stripped
    before the path is resolved. A path like `` `claude_hooks/` `` —
    backticks and trailing slash included — becomes ``claude_hooks``.
    """
    cleaned = raw.strip().strip("`").strip().strip("'").strip('"').strip()
    cwd_real = os.path.realpath(cwd)
    joined = (
        os.path.join(cwd_real, cleaned)
        if not os.path.isabs(cleaned) else cleaned
    )
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


# -- Tool: recall_memory ---------------------------------------------- #
def recall_memory(args: dict, cwd: str) -> str:  # noqa: ARG001
    """Cross-provider memory recall for caliber's grounding loop.

    Fans out to every enabled memory provider (Qdrant, Memory KG,
    pgvector, sqlite_vec) via the standard ``Provider.recall`` interface.
    Returns a markdown block ready to be pasted into the model's context
    or empty string when nothing relevant is found.

    The ``cwd`` argument is unused — recall operates over the user's
    memory store, not the project filesystem.
    """
    query = str(args.get("query") or "").strip()
    if not query:
        return "error: query is required"
    k_arg = args.get("k")
    try:
        k = int(k_arg) if k_arg is not None else None
    except (TypeError, ValueError):
        k = None
    # Lazy import — keeps tools module importable in tests that mock
    # the provider stack.
    from claude_hooks.caliber_proxy import recall
    hits = recall.recall_hits(query, k=k)
    if not hits:
        return f"(no recalled memory for {query!r})"
    return recall.format_hits(hits, header="# Recalled memory")


# -- Tool: survey_project --------------------------------------------- #
# Per-cwd cache. Caliber's init flow invokes the proxy ~10 times for the
# same cwd within minutes; computing a fresh survey on each call wastes
# work and pollutes the prompt cache. The tree doesn't change mid-run.
_SURVEY_CACHE: dict[str, str] = {}

# Hard cap on the survey body. ~8 KB ≈ 2k tokens — bounded so big
# projects (1000+ files) don't blow up the model's context.
_SURVEY_MAX_BYTES = 8 * 1024

# Skip these dirs entirely — they bloat the histogram without signal.
_SURVEY_SKIP_DIRS = frozenset({
    ".git", "node_modules", "__pycache__", ".venv", "venv", ".tox",
    "dist", "build", ".mypy_cache", ".pytest_cache", "target",
    ".next", ".cache", ".coverage", "htmlcov", ".idea", ".vscode",
})


def clear_survey_cache() -> None:
    """Test hook — drop the per-cwd survey cache."""
    _SURVEY_CACHE.clear()


def survey_project(args: dict, cwd: str) -> str:  # noqa: ARG001
    """Hierarchical project map: top-level directory file counts +
    extension histogram + root-level files. Memoized per cwd. Capped
    at ~2k tokens. Always call this FIRST so subsequent grep / glob
    calls can target real directories instead of guessing.
    """
    cwd_real = os.path.realpath(cwd)
    cached = _SURVEY_CACHE.get(cwd_real)
    if cached is not None:
        return cached
    try:
        out = _build_survey(cwd_real)
    except OSError as e:
        return f"error surveying {cwd_real}: {e}"
    _SURVEY_CACHE[cwd_real] = out
    return out


def _build_survey(cwd: str) -> str:
    notable_files: list[str] = []
    top_dirs: dict[str, dict[str, int]] = {}
    # Per-dir representative filenames (top by extension frequency).
    top_dir_examples: dict[str, list[str]] = {}
    ext_counts: dict[str, int] = {}

    for name in sorted(os.listdir(cwd)):
        if name in _SURVEY_SKIP_DIRS:
            continue
        full = os.path.join(cwd, name)
        if os.path.isfile(full):
            notable_files.append(name)
            ext = os.path.splitext(name)[1].lower()
            if ext:
                ext_counts[ext] = ext_counts.get(ext, 0) + 1
            continue
        if not os.path.isdir(full):
            continue
        per_dir: dict[str, int] = {"_total": 0}
        # Track filenames seen in this dir for representative examples.
        # Prefer files with the dominant extension; cap at 5 per dir.
        per_dir_files: list[tuple[str, str]] = []  # (rel_path, ext)
        for dirpath, dirnames, filenames in os.walk(full):
            # In-place filter so os.walk doesn't descend into skipped dirs
            dirnames[:] = [d for d in dirnames if d not in _SURVEY_SKIP_DIRS]
            per_dir["_total"] += len(filenames)
            for fn in filenames:
                ext = os.path.splitext(fn)[1].lower()
                if ext:
                    per_dir[ext] = per_dir.get(ext, 0) + 1
                    ext_counts[ext] = ext_counts.get(ext, 0) + 1
                # Path relative to project root, e.g. "claude_hooks/dispatcher.py"
                rel = os.path.relpath(os.path.join(dirpath, fn), cwd)
                per_dir_files.append((rel, ext))
        top_dirs[name] = per_dir
        # Pick up to 5 representative filenames: dominant extension first,
        # alphabetical within that extension. Skips dotfiles like __init__.py
        # and noisy generic names so the model sees signal.
        dominant_ext = next(
            (e for e in (
                sorted(((k, v) for k, v in per_dir.items() if k != "_total"),
                       key=lambda kv: -kv[1])
            )), (None, 0),
        )[0]
        skip_names = {"__init__.py"}
        candidates = sorted(
            (r for r, e in per_dir_files
             if e == dominant_ext and os.path.basename(r) not in skip_names),
        )
        # Fallback if dominant-ext filtering empties the list (e.g. only
        # __init__.py files): fall back to any file.
        if not candidates:
            candidates = sorted(
                r for r, _ in per_dir_files
                if os.path.basename(r) not in skip_names
            ) or sorted(r for r, _ in per_dir_files)
        top_dir_examples[name] = candidates[:5]

    lines: list[str] = []
    lines.append(f"# Project survey: {os.path.basename(cwd) or cwd}")
    lines.append("")
    lines.append(
        "## Top-level directories (sorted by file count)"
    )
    if not top_dirs:
        lines.append("- (no subdirectories)")
    for name, counts in sorted(
        top_dirs.items(), key=lambda kv: -kv[1]["_total"],
    ):
        total = counts["_total"]
        exts = sorted(
            ((k, v) for k, v in counts.items() if k != "_total"),
            key=lambda kv: -kv[1],
        )[:3]
        ext_str = ", ".join(f"{e}={n}" for e, n in exts) or "(no files)"
        ex_files = top_dir_examples.get(name) or []
        if ex_files:
            ex_str = " · ".join(f"`{p}`" for p in ex_files)
            lines.append(
                f"- `{name}/` — {total} files ({ext_str}) — e.g. {ex_str}"
            )
        else:
            lines.append(f"- `{name}/` — {total} files ({ext_str})")

    lines.append("")
    lines.append("## File extensions (project-wide, top 15)")
    if not ext_counts:
        lines.append("- (no files)")
    for ext, n in sorted(ext_counts.items(), key=lambda kv: -kv[1])[:15]:
        lines.append(f"- {ext}: {n}")

    lines.append("")
    lines.append("## Root-level files")
    if not notable_files:
        lines.append("- (none)")
    for name in notable_files:
        lines.append(f"- {name}")

    out = "\n".join(lines)
    if len(out) > _SURVEY_MAX_BYTES:
        cutoff = _SURVEY_MAX_BYTES - 120
        out = out[:cutoff].rsplit("\n", 1)[0] + (
            "\n\n…(truncated; call `list_files` on specific dirs to drill in)"
        )
    return out


# -- Dispatch --------------------------------------------------------- #
TOOL_IMPLS = {
    "survey_project": survey_project,
    "list_files": list_files,
    "read_file": read_file,
    "glob": glob_files,
    "grep": grep,
    "recall_memory": recall_memory,
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
    """OpenAI function-calling schemas for the configured tools."""
    return [
        {
            "type": "function",
            "function": {
                "name": "survey_project",
                "description": (
                    "Get a one-shot hierarchical map of the project: "
                    "top-level directories with file counts, an extension "
                    "histogram, and root-level files. Always call this "
                    "FIRST — before any other tool — so subsequent "
                    "list_files / grep / glob calls target real "
                    "directories instead of guessing. Result is cached "
                    "per project for the proxy lifetime, so calling it "
                    "again is free. Capped at ~2k tokens."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {},
                },
            },
        },
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
        {
            "type": "function",
            "function": {
                "name": "recall_memory",
                "description": (
                    "Search the user's persistent memory stores (Qdrant, "
                    "Memory KG, pgvector, sqlite_vec — whichever are "
                    "configured) for prior decisions, fixes, conventions, "
                    "or non-obvious facts about this project. Returns a "
                    "markdown block of relevant snippets. Use BEFORE "
                    "writing skills or instructions that depend on past "
                    "decisions, when the user references prior work, or "
                    "when read_file alone won't surface the why."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Free-text query, e.g. "
                            "'caliber init failure modes' or "
                            "'why is block_warmup default true'.",
                        },
                        "k": {
                            "type": "integer",
                            "description": "Per-provider top-k, default 5.",
                        },
                    },
                    "required": ["query"],
                },
            },
        },
    ]
