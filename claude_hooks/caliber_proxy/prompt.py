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

JSON OUTPUT RULES — when your response contains a JSON object:
- Every backslash inside a JSON string value MUST be doubled. Write
  `\\w`, `\\s`, `\\d`, `\\.`, `\\\\`, not `\w`, `\s`, `\d`, `\.`, `\\`.
  This applies to regex patterns, Windows paths, and literal backslashes
  of any kind inside `"..."` strings.
- Newlines inside JSON string values MUST be written as the 2-char
  escape `\\n`, not as actual line breaks.
- Double-quotes inside strings MUST be escaped as `\\"`.
- Do not wrap the JSON in a markdown fence (no ```json / ``` around it).
- The JSON MUST be valid per RFC 8259 — a downstream parser will reject
  it otherwise and all your work is lost.

CONFIG QUALITY RUBRIC — when generating CLAUDE.md, AGENTS.md,
.cursorrules, or skill bodies, a deterministic grader re-scores your output.
The PROJECT STRUCTURE MAP system message lists the real directories and
files you can cite; pulling from it is the single biggest grounding lever.
- **Project grounding** (12 pts): mention ≥50% of the dirs and notable
  files shown in the PROJECT STRUCTURE MAP by their actual names in
  backticks. Generic prose about "your backend" scores 0 here.
- **Reference density** (8 pts): ≥40% of non-empty lines must contain
  a backtick reference (`path/`, `file.ext`, command, or identifier).
  Prefer inline refs: "Routes in `src/api/` · models in `src/models/`".
- **Executable content** (8 pts): include ≥3 fenced code blocks
  (```bash / ```python / etc.) with this project's real build/test/run
  commands drawn from the pre-loaded manifest files.
- **References valid** (8 pts): every backtick path must exist in the
  structure map or the anchor files. Invented paths cost points per ref.
- **Concreteness** (4 pts): ≥70% of non-empty, non-code lines must
  reference specific project elements (paths, commands, symbols).
- **Structure** (2 pts): ≥3 `## H2` sections and ≥3 bullet-list items
  in each generated markdown file.
- **Token budget** (6 pts): CLAUDE.md + AGENTS.md combined should stay
  under ~5000 tokens (~20 KB). Prefer dense backtick refs over prose.
- **No directory tree listings** (3 pts): do NOT use box-drawing chars
  (├ └ │ ─ ┬) in code blocks. Reference dirs inline with backticks.
- **No duplicate content** (2 pts): if both CLAUDE.md and .cursorrules
  are emitted, their bodies must be meaningfully different.
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

JSON OUTPUT RULES — when your response contains a JSON object:
- Every backslash inside a JSON string value MUST be doubled. Write
  `\\w`, `\\s`, `\\d`, `\\.`, `\\\\`, not `\w`, `\s`, `\d`, `\.`, `\\`.
  This applies to regex patterns, Windows paths, and literal backslashes
  of any kind inside `"..."` strings.
- Newlines inside JSON string values MUST be written as `\\n`, not as
  actual line breaks. Double-quotes inside strings MUST be escaped as `\\"`.
- Do not wrap the JSON in a markdown fence (no ```json / ``` around it).
- The JSON MUST be valid per RFC 8259 — a downstream parser will reject
  it otherwise and all your work is lost.

CONFIG QUALITY RUBRIC — when generating CLAUDE.md, AGENTS.md,
.cursorrules, or skill bodies, a deterministic grader re-scores your output.
The PROJECT STRUCTURE MAP system message lists the real directories and
files you can cite; pulling from it is the single biggest grounding lever.
- **Project grounding** (12 pts): mention ≥50% of the dirs and notable
  files shown in the PROJECT STRUCTURE MAP by their actual names in
  backticks. Generic prose about "your backend" scores 0 here.
- **Reference density** (8 pts): ≥40% of non-empty lines must contain
  a backtick reference (`path/`, `file.ext`, command, or identifier).
  Prefer inline refs: "Routes in `src/api/` · models in `src/models/`".
- **Executable content** (8 pts): include ≥3 fenced code blocks
  (```bash / ```python / etc.) with this project's real build/test/run
  commands drawn from the pre-loaded manifest files.
- **References valid** (8 pts): every backtick path must exist in the
  structure map or the anchor files. Invented paths cost points per ref.
- **Concreteness** (4 pts): ≥70% of non-empty, non-code lines must
  reference specific project elements (paths, commands, symbols).
- **Structure** (2 pts): ≥3 `## H2` sections and ≥3 bullet-list items
  in each generated markdown file.
- **Token budget** (6 pts): CLAUDE.md + AGENTS.md combined should stay
  under ~5000 tokens (~20 KB). Prefer dense backtick refs over prose.
- **No directory tree listings** (3 pts): do NOT use box-drawing chars
  (├ └ │ ─ ┬) in code blocks. Reference dirs inline with backticks.
- **No duplicate content** (2 pts): if both CLAUDE.md and .cursorrules
  are emitted, their bodies must be meaningfully different.
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


def _default_structure_map_bytes() -> int:
    try:
        return int(os.environ.get(
            "CALIBER_GROUNDING_STRUCTURE_MAX_BYTES", "4000",
        ))
    except ValueError:
        return 4000


# Files we highlight individually in the structure map if present. Drives
# caliber's "notable files" grounding check — the model needs to see these
# names verbatim so it can mention them in backticks.
_NOTABLE_FILE_NAMES = {
    "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt",
    "Pipfile", "package.json", "tsconfig.json", "pnpm-lock.yaml",
    "go.mod", "Cargo.toml", "Makefile", "CMakeLists.txt",
    "Dockerfile", "docker-compose.yml", "docker-compose.yaml",
    ".gitignore", "LICENSE", "README.md", "CHANGELOG.md",
    "CLAUDE.md", "AGENTS.md", ".cursorrules", "AGENTS.md",
    "conftest.py", "pytest.ini", "tox.ini", ".pre-commit-config.yaml",
}


def read_project_structure_map(cwd: str,
                                max_bytes: Optional[int] = None,
                                ) -> str:
    """Walk the top 2 directory levels and emit a compact map of dirs
    and notable files. The model gets real project paths for free —
    critical for caliber's Project-Grounding and References-Valid checks
    without burning agent-loop turns on ``list_files`` calls.

    Format:
        ./
        src/           [12 files, 3 dirs]
        src/api/       [5 files]
        tests/         [8 files]
        <notable files at root>
    """
    if max_bytes is None:
        max_bytes = _default_structure_map_bytes()
    cwd_real = os.path.realpath(cwd)
    lines: list[str] = ["./"]
    root_files: list[str] = []
    # 1st level
    try:
        for name in sorted(os.listdir(cwd_real)):
            if name in _WALK_SKIP_DIRS or name.startswith("."):
                if name not in (".github", ".claude", ".cursor", ".agents"):
                    continue
            full = os.path.join(cwd_real, name)
            if os.path.isdir(full):
                sub_file_count = 0
                sub_dir_count = 0
                try:
                    for inner in os.listdir(full):
                        if inner.startswith(".") or inner in _WALK_SKIP_DIRS:
                            continue
                        if os.path.isdir(os.path.join(full, inner)):
                            sub_dir_count += 1
                        else:
                            sub_file_count += 1
                except OSError:
                    pass
                lines.append(
                    f"{name}/  [{sub_file_count} files, {sub_dir_count} dirs]"
                )
                # Descend one level for interesting dirs
                try:
                    for inner in sorted(os.listdir(full)):
                        if inner.startswith(".") or inner in _WALK_SKIP_DIRS:
                            continue
                        inner_full = os.path.join(full, inner)
                        if os.path.isdir(inner_full):
                            try:
                                n = len([
                                    x for x in os.listdir(inner_full)
                                    if not x.startswith(".")
                                ])
                            except OSError:
                                n = 0
                            lines.append(f"{name}/{inner}/  [{n} entries]")
                except OSError:
                    pass
            elif name in _NOTABLE_FILE_NAMES:
                root_files.append(name)
    except OSError:
        return ""
    if root_files:
        lines.append("")
        lines.append("Notable files at project root: " + ", ".join(root_files))
    # Skip the map if there's nothing useful beyond the "./" header — keeps
    # the empty-project case clean and doesn't drag a useless system message.
    if len(lines) <= 1:
        return ""
    out = "\n".join(lines)
    if len(out) > max_bytes:
        out = out[:max_bytes] + "\n[truncated]"
    return out


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
    structure_map = read_project_structure_map(cwd)
    if structure_map:
        messages.append({
            "role": "system",
            "content": (
                "PROJECT STRUCTURE MAP — real directories and files in this "
                "project. Cite these by name in backticks to satisfy caliber's "
                "grounding rubric; do not invent paths outside this map.\n\n"
                "```\n" + structure_map + "\n```"
            ),
        })
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
