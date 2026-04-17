"""Grounding injection: anchor-file pre-stuffing + system addendum
that instructs the model to use tools and cite `path:line` references.

Pre-stuffed anchors are short, always-relevant files (pyproject.toml,
.wolf/cerebrum.md) that give the model immediate context without having
to spend tool-call rounds on them. Everything else is discovered via
the tool loop.
"""

from __future__ import annotations

import fnmatch
import logging
import os
from typing import Optional

log = logging.getLogger("claude_hooks.caliber_proxy.prompt")

# Files we always try to pre-stuff if present. Cheap + always informative.
# Ordered by priority — we stop at the first ``ANCHOR_MAX_TOTAL_BYTES``
# worth of content.
ANCHOR_PATHS = [
    ".wolf/cerebrum.md",
    "pyproject.toml",
    "package.json",
    "go.mod",
    "Cargo.toml",
    "CLAUDE.md",
    "AGENTS.md",
    "README.md",
]


def _default_anchor_bytes() -> int:
    try:
        return int(os.environ.get("CALIBER_GROUNDING_ANCHOR_MAX_BYTES", "50000"))
    except ValueError:
        return 50000


SYSTEM_ADDENDUM_WITH_TOOLS = """\
GROUNDING PROTOCOL — read this carefully.

You are operating in a coding project. To produce high-quality output you
MUST use the filesystem tools provided (list_files, read_file, glob, grep)
to ground every claim you make.

Non-negotiable rules:
1. When citing code, ALWAYS use `path/to/file.py:42` format — a real
   relative path plus a real line number from the file. Never invent
   paths. Never cite a symbol you have not read.
2. If you are asked to describe project structure, call `list_files`
   first. If you need to understand a specific module, `read_file` the
   module (use start_line/end_line for large files).
3. To find occurrences of a symbol or pattern, use `grep` with a regex.
   To find files by name pattern, use `glob`.
4. Before generating any skill, agent, or config entry that references a
   file, verify the file exists (`list_files` the parent directory or
   `read_file` the file). Do not list files in your output that do not
   exist in this project.
5. Prefer reading real files over assuming conventions. The project may
   use conda, not venv; pytest-asyncio, not trio; etc.
6. Tool results are expensive — batch reads and be specific with
   patterns instead of making many tiny calls.

If the task is deterministic (e.g. checking whether docs need refresh)
and the current prompt already contains enough information to answer,
you may answer directly without calling tools.
"""


SYSTEM_ADDENDUM_NO_TOOLS = """\
GROUNDING PROTOCOL — read this carefully.

You are operating in a coding project. No filesystem tools are available
in this session — all project context you need is ALREADY included below
under the PROJECT ANCHOR FILES and EXTENDED SOURCE FILES sections. You
MUST ground every claim in that material.

Non-negotiable rules:
1. When citing code, ALWAYS use `path/to/file.py:42` format — a real
   relative path plus a real line number as shown in the pre-loaded
   sources. Never invent paths. Never cite a path you have not seen
   appear in the pre-stuffed material.
2. If you cannot find a file referenced by the task in the pre-stuffed
   material, say so explicitly rather than guessing.
3. Prefer quoting short excerpts from the pre-loaded source blocks over
   making claims unsupported by them. If you quote, include the
   filename and a line number.
4. Do not list files in your output that do not appear in the pre-
   stuffed material. The project may use conda, not venv; pytest-
   asyncio, not trio; etc. — follow what the pre-loaded files show.
"""


# Back-compat alias used by tests / existing callers.
SYSTEM_ADDENDUM = SYSTEM_ADDENDUM_WITH_TOOLS


def read_anchor_files(cwd: str, max_bytes: Optional[int] = None) -> dict[str, str]:
    """Read each file in ANCHOR_PATHS that exists under ``cwd``, stopping
    once the cumulative size reaches ``max_bytes``. Returns
    ``{rel_path: content}``."""
    if max_bytes is None:
        max_bytes = _default_anchor_bytes()
    out: dict[str, str] = {}
    used = 0
    for rel in ANCHOR_PATHS:
        abs_path = os.path.join(cwd, rel)
        if not os.path.isfile(abs_path):
            continue
        try:
            with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except OSError as e:
            log.debug("skip anchor %s: %s", rel, e)
            continue
        remaining = max_bytes - used
        if remaining <= 0:
            break
        if len(content) > remaining:
            content = content[:remaining] + "\n\n[truncated to fit grounding budget]"
        out[rel] = content
        used += len(content)
    return out


_SOURCE_GLOBS_DEFAULT = (
    "*.py",
    "*.js",
    "*.ts",
    "*.go",
    "*.rs",
    "*.java",
    "*.cs",
    "Makefile",
)

# Directories we skip when walking the tree for extended pre-stuffing.
_WALK_SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    ".claude", ".caliber", ".wolf", "dist", "build", ".cache",
    "target", "vendor", ".mypy_cache", ".pytest_cache",
}


def _default_extended_bytes() -> int:
    # 80 KB default keeps inference latency manageable; at 200 KB the
    # main "Generating configs" call tends to breach nginx's 5-min
    # gateway timeout in front of Ollama. Raise via env if you're
    # running a bigger model with more VRAM or a direct Ollama endpoint.
    try:
        return int(os.environ.get(
            "CALIBER_GROUNDING_EXTENDED_MAX_BYTES", "80000",
        ))
    except ValueError:
        return 80000


def _default_extended_source_glob() -> str:
    return os.environ.get("CALIBER_GROUNDING_SOURCE_GLOB", "")


def _walk_source_files(cwd: str, patterns: tuple[str, ...]) -> list[tuple[str, int]]:
    """Return ``[(rel_path, size), ...]`` for matching files, sorted by
    size ascending so small, high-signal files (e.g. __init__.py) come
    first. Caller decides how many to actually include."""
    hits: list[tuple[str, int]] = []
    cwd_real = os.path.realpath(cwd)
    for root, dirs, files in os.walk(cwd_real):
        dirs[:] = [d for d in dirs if d not in _WALK_SKIP_DIRS]
        for name in files:
            if not any(fnmatch.fnmatch(name, p) for p in patterns):
                continue
            full = os.path.join(root, name)
            try:
                sz = os.path.getsize(full)
            except OSError:
                continue
            rel = os.path.relpath(full, cwd_real)
            hits.append((rel, sz))
    hits.sort(key=lambda x: x[1])
    return hits


def read_extended_sources(cwd: str,
                          max_bytes: Optional[int] = None,
                          ) -> dict[str, str]:
    """Read project source files up to ``max_bytes`` total. Used for
    models whose tool-use is weak — pre-stuff rather than agent-loop.
    Returns ``{rel_path: content}``."""
    if max_bytes is None:
        max_bytes = _default_extended_bytes()
    glob_env = _default_extended_source_glob()
    patterns = tuple(glob_env.split(",")) if glob_env else _SOURCE_GLOBS_DEFAULT
    out: dict[str, str] = {}
    used = 0
    for rel, sz in _walk_source_files(cwd, patterns):
        if used >= max_bytes:
            break
        # Skip huge files entirely — they blow the budget alone.
        if sz > max_bytes // 4:
            continue
        abs_path = os.path.join(cwd, rel)
        try:
            with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except OSError:
            continue
        remaining = max_bytes - used
        if len(content) > remaining:
            content = content[:remaining] + "\n\n[truncated to fit grounding budget]"
        out[rel] = content
        used += len(content)
    return out


def build_grounding_messages(cwd: str,
                             max_anchor_bytes: Optional[int] = None,
                             extended_sources: bool = False,
                             max_extended_bytes: Optional[int] = None,
                             tools_available: bool = True,
                             ) -> list[dict[str, str]]:
    """Return the grounding system messages to prepend to the model's
    conversation. Always emits an addendum; anchor block is emitted
    only when at least one file was readable.

    Parameters:
        tools_available: when False, use the no-tools addendum that
            tells the model all grounding material is pre-loaded.
            Pairs naturally with ``extended_sources=True``.
        extended_sources: include a curated subset of project source
            files (capped at ``max_extended_bytes``, default 200 KB).
            Recommended when ``tools_available=False`` so the model
            has more than just the anchor files to work from.
    """
    addendum = (
        SYSTEM_ADDENDUM_WITH_TOOLS if tools_available
        else SYSTEM_ADDENDUM_NO_TOOLS
    )
    messages: list[dict[str, str]] = [
        {"role": "system", "content": addendum},
    ]
    anchors = read_anchor_files(cwd, max_anchor_bytes)
    if anchors:
        body_parts = [
            f"### {rel}\n```\n{content}\n```"
            for rel, content in anchors.items()
        ]
        block = (
            "PROJECT ANCHOR FILES — included for immediate context. Use "
            "tools to read other files as needed.\n\n" + "\n\n".join(body_parts)
        )
        messages.append({"role": "system", "content": block})
    if extended_sources:
        sources = read_extended_sources(cwd, max_extended_bytes)
        if sources:
            body_parts = [
                f"### {rel}\n```\n{content}\n```"
                for rel, content in sources.items()
            ]
            block = (
                "EXTENDED SOURCE FILES — a curated subset of project "
                "source is included below. Cite `path:line` references "
                "from these files rather than inventing paths.\n\n"
                + "\n\n".join(body_parts)
            )
            messages.append({"role": "system", "content": block})
    return messages
