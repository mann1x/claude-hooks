"""The twelve-tool catalog, and how results are rendered.

The catalog is deliberately **cclsp's**, name for name and parameter for
parameter. Every configured client and every model that has learned this
surface calls ``find_definition(file_path, symbol_name)``; changing the
shape to something we like better would break all of them for no gain.
What we replace is the implementation underneath.

Two inherited quirks are kept on purpose:

* ``find_definition`` and ``find_references`` address a symbol **by
  name**, while ``get_hover`` and the call-hierarchy tools address it
  **by position**. That asymmetry is not a good design, but it is the
  one in use, and the name-based form is genuinely the better ergonomic:
  a caller knows ``did_open``, not line 102 column 8.
* positions are **1-based on both axes**. LSP is 0-based on both. The
  conversion happens in exactly one place (:func:`to_lsp_position`),
  because a second conversion site is how a result ends up one line off
  in a way nobody notices until it lands on the wrong function.

Where we extend rather than copy, it is strictly additive: the
position-addressed tools also accept ``symbol_name``, so a caller that
knows only the name is not forced to guess coordinates. Existing calls
are unaffected.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import unquote, urlparse

from claude_hooks.lsp_engine.engine import NavResponse
from claude_hooks.lsp_engine.protocol import (
    CallHierarchyCall,
    Location,
    Symbol,
    WorkspaceEdit,
)

#: Shared by every position-addressed tool.
_POSITION_PROPS = {
    "file_path": {"type": "string", "description": "The path to the file"},
    "line": {"type": "number",
             "description": "The line number (1-indexed)"},
    "character": {"type": "number",
                  "description": "The character position in the line "
                                 "(1-indexed)"},
}
_SYMBOL_PROPS = {
    "file_path": {"type": "string", "description": "The path to the file"},
    "symbol_name": {"type": "string",
                    "description": "The name of the symbol"},
    "symbol_kind": {"type": "string",
                    "description": "The kind of symbol (function, class, "
                                   "variable, method, etc.)"},
}
#: The additive extension: name-or-position on the positional tools.
_NAME_ALTERNATIVE = {
    "symbol_name": {"type": "string",
                    "description": "Symbol name, as an alternative to "
                                   "line/character. Resolved in file_path."},
    "symbol_kind": {"type": "string",
                    "description": "Narrows symbol_name when several "
                                   "symbols share it."},
}


def _schema(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": dict(props), "required": required}


def tool_catalog() -> list[dict]:
    """The `tools/list` payload. Order matches cclsp's."""
    return [
        {
            "name": "find_definition",
            "description": "Find the definition of a symbol by name and kind "
                           "in a file. Returns definitions for all matching "
                           "symbols.",
            "inputSchema": _schema(_SYMBOL_PROPS, ["file_path", "symbol_name"]),
        },
        {
            "name": "find_references",
            "description": "Find all references to a symbol across the entire "
                           "workspace. Returns references for all matching "
                           "symbols.",
            "inputSchema": _schema(
                {**_SYMBOL_PROPS,
                 "include_declaration": {
                     "type": "boolean", "default": True,
                     "description": "Whether to include the declaration"}},
                ["file_path", "symbol_name"]),
        },
        {
            "name": "find_implementation",
            "description": "Find implementations of an interface or abstract "
                           "method. Returns locations of all implementations.",
            "inputSchema": _schema({**_POSITION_PROPS, **_NAME_ALTERNATIVE},
                                   ["file_path"]),
        },
        {
            "name": "get_hover",
            "description": "Get hover information (documentation, type info) "
                           "for a symbol at a specific position in a file.",
            "inputSchema": _schema({**_POSITION_PROPS, **_NAME_ALTERNATIVE},
                                   ["file_path"]),
        },
        {
            "name": "get_diagnostics",
            "description": "Get language diagnostics (errors, warnings, hints) "
                           "for a file. Uses LSP textDocument/diagnostic to "
                           "pull current diagnostics.",
            "inputSchema": _schema(
                {"file_path": {
                    "type": "string",
                    "description": "The path to the file to get diagnostics for"}},
                ["file_path"]),
        },
        {
            "name": "find_workspace_symbols",
            "description": "Search for symbols across the entire workspace by "
                           "name. Returns matching symbols from all files.",
            "inputSchema": _schema(
                {"query": {"type": "string",
                           "description": "The symbol name or pattern to "
                                          "search for"},
                 "file_path": {
                     "type": "string",
                     "description": "Any file in the project to search. "
                                    "Optional; selects which project when "
                                    "more than one is open."},
                 "start_all": {
                     "type": "boolean", "default": False,
                     "description": "Start every configured language server "
                                    "rather than querying only those already "
                                    "running. Slower, but complete."}},
                ["query"]),
        },
        {
            "name": "prepare_call_hierarchy",
            "description": "Get call hierarchy item at a position. Use this to "
                           "prepare for incoming_calls or outgoing_calls.",
            "inputSchema": _schema({**_POSITION_PROPS, **_NAME_ALTERNATIVE},
                                   ["file_path"]),
        },
        {
            "name": "get_incoming_calls",
            "description": "Find all functions/methods that call the function "
                           "at a position.",
            "inputSchema": _schema({**_POSITION_PROPS, **_NAME_ALTERNATIVE},
                                   ["file_path"]),
        },
        {
            "name": "get_outgoing_calls",
            "description": "Find all functions/methods called by the function "
                           "at a position.",
            "inputSchema": _schema({**_POSITION_PROPS, **_NAME_ALTERNATIVE},
                                   ["file_path"]),
        },
        {
            "name": "rename_symbol",
            "description": "Rename a symbol by name and kind in a file. If "
                           "multiple symbols match, returns candidate "
                           "positions and suggests using rename_symbol_strict. "
                           "By default this previews the change; pass "
                           "apply=true to write it.",
            "inputSchema": _schema(
                {**_SYMBOL_PROPS,
                 "new_name": {"type": "string",
                              "description": "The new name for the symbol"},
                 "apply": {"type": "boolean", "default": False,
                           "description": "Write the edits to disk. Default "
                                          "false: the plan is returned for "
                                          "review first."},
                 "dry_run": {"type": "boolean",
                             "description": "Deprecated alias for the inverse "
                                            "of apply; accepted for "
                                            "compatibility."}},
                ["file_path", "symbol_name", "new_name"]),
        },
        {
            "name": "rename_symbol_strict",
            "description": "Rename a symbol at a specific position in a file. "
                           "Use this when rename_symbol returns multiple "
                           "candidates. By default this previews the change; "
                           "pass apply=true to write it.",
            "inputSchema": _schema(
                {**_POSITION_PROPS,
                 "new_name": {"type": "string",
                              "description": "The new name for the symbol"},
                 "apply": {"type": "boolean", "default": False,
                           "description": "Write the edits to disk."},
                 "dry_run": {"type": "boolean",
                             "description": "Deprecated alias for the inverse "
                                            "of apply."}},
                ["file_path", "line", "character", "new_name"]),
        },
        {
            "name": "restart_server",
            "description": "Manually restart LSP servers. Can restart servers "
                           "for specific file extensions or all running "
                           "servers.",
            "inputSchema": _schema(
                {"extensions": {
                    "type": "array", "items": {"type": "string"},
                    "description": 'Array of file extensions to restart '
                                   'servers for (e.g., ["ts", "tsx"]). If not '
                                   'provided, all servers will be restarted.'},
                 "file_path": {
                     "type": "string",
                     "description": "Any file in the project whose servers "
                                    "should restart. Optional."}},
                []),
        },
    ]


TOOL_NAMES = tuple(t["name"] for t in tool_catalog())


# ─────────────────────────── positions ──────────────────────────────


class ToolError(ValueError):
    """A bad request, reported to the caller rather than logged.

    Distinct from an engine failure: this one is the caller's to fix,
    and the message says how.
    """


def to_lsp_position(line: Any, character: Any) -> tuple[int, int]:
    """1-based (the tool surface) → 0-based (LSP). The only conversion.

    Rejects 0 rather than clamping it. A caller passing 0 is either
    already 0-based — in which case every result is off by one — or has
    a bug; silently treating it as line 1 hides both.
    """
    try:
        ln = int(line)
        ch = int(character)
    except (TypeError, ValueError):
        raise ToolError(
            f"line and character must be numbers, got {line!r} and "
            f"{character!r}") from None
    if ln < 1 or ch < 1:
        raise ToolError(
            f"line and character are 1-indexed; got line={ln}, "
            f"character={ch}. (LSP is 0-indexed internally, but this "
            f"surface is not — pass what an editor shows you.)")
    return ln - 1, ch - 1


def uri_to_path(uri: str) -> str:
    """``file:///a/b.py`` → ``/a/b.py``, and pass anything else through.

    Percent-decoding matters on Windows, where servers publish
    ``file:///c%3A/x`` — and a caller handed that string cannot open it.
    """
    if not uri.startswith("file:"):
        return uri
    parsed = urlparse(uri)
    path = unquote(parsed.path)
    if os.name == "nt" and len(path) > 2 and path[0] == "/" and path[2] == ":":
        path = path[1:]
    return path


def _rel(path: str, root: Optional[Path]) -> str:
    """Project-relative when possible — absolute paths bury the part
    that identifies the file in a prefix repeated on every line."""
    if root is None:
        return path
    try:
        return str(Path(path).resolve().relative_to(root))
    except (ValueError, OSError):
        return path


# ─────────────────────────── rendering ──────────────────────────────


def provenance_note(res: NavResponse) -> str:
    """The sentence that keeps an empty result honest.

    Returns "" when the answer can be taken at face value. Otherwise it
    says which of the non-answers this is, because "no references
    found" and "the only server that could have told you is still
    indexing" must not render identically.
    """
    parts: list[str] = []
    if res.progress is not None:
        p = res.progress
        pct = f", {int(p['percentage'])}%" if p.get("percentage") is not None else ""
        eta = (f" ETA ~{p['eta_seconds']}s." if p.get("eta_seconds")
               else "")
        parts.append(
            f"INCOMPLETE — {p.get('server', 'the language server')} is still "
            f"working: {p.get('title') or 'busy'}"
            f"{(' (' + p['message'] + ')') if p.get('message') else ''}{pct}."
            f"{eta} This is NOT an empty result. Retry the same call in "
            f"{min(p['eta_seconds'], 60) if p.get('eta_seconds') else 30}s.")
    for name, msg in res.failures:
        parts.append(f"WARNING — {name} did not answer: {msg}")
    if not res.consulted and not res.failures and not res.not_running:
        parts.append(
            "NOT ANALYSED — no configured language server claims this file "
            "type, so nothing was asked. This is not a statement about the "
            "code. Check cclsp.json for the extension.")
    if res.scan_truncated_at:
        parts.append(
            f"PARTIAL — the workspace scan stopped at "
            f"{res.scan_truncated_at} files, so this covers part of the "
            f"project rather than all of it. Matches outside that set are "
            f"not listed and were not ruled out.")
    if res.not_running:
        parts.append(
            "PARTIAL — not started, so not searched: "
            + ", ".join(res.not_running)
            + ". Pass start_all=true to include them.")
    return "\n".join(parts)


def _wrap(body: str, res: NavResponse, *, empty: str) -> str:
    note = provenance_note(res)
    text = body.strip() or empty
    return f"{text}\n\n{note}".strip() if note else text


def render_locations(res: NavResponse, *, root: Optional[Path] = None,
                     title: str) -> str:
    locs: list[Location] = res.items
    seen: set[tuple[str, int, int]] = set()
    lines: list[str] = []
    for loc in locs:
        path = uri_to_path(loc.uri)
        key = (path, loc.range.start.line, loc.range.start.character)
        # Two servers claiming one file report the same definition twice;
        # a caller counting results would read that as two definitions.
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"  {_rel(path, root)}:{loc.range.start.human_line}:"
                     f"{loc.range.start.character + 1}")
    body = (f"{title} ({len(lines)}):\n" + "\n".join(lines)) if lines else ""
    return _wrap(body, res, empty=f"{title}: none found.")


def render_symbols(res: NavResponse, *, root: Optional[Path] = None,
                   title: str = "Symbols") -> str:
    syms: list[Symbol] = res.items
    lines = []
    for s in syms:
        where = f"{_rel(uri_to_path(s.uri), root)}:{s.selection.start.human_line}"
        qualified = f"{s.container}.{s.name}" if s.container else s.name
        detail = f"  {s.detail}" if s.detail else ""
        lines.append(f"  [{s.kind_name}] {qualified} — {where}{detail}")
    body = (f"{title} ({len(lines)}):\n" + "\n".join(lines)) if lines else ""
    return _wrap(body, res, empty=f"{title}: none found.")


def render_hover(res: NavResponse) -> str:
    texts = [t for t in res.items if t]
    body = "\n\n---\n\n".join(texts)
    return _wrap(body, res, empty="No hover information at this position.")


def render_calls(res: NavResponse, *, root: Optional[Path] = None,
                 direction: str) -> str:
    calls: list[CallHierarchyCall] = res.items
    label = "Callers" if direction == "incoming" else "Callees"
    lines = []
    for c in calls:
        where = f"{_rel(uri_to_path(c.item.uri), root)}:" \
                f"{c.item.selection.start.human_line}"
        sites = (f"  ({len(c.ranges)} call site"
                 f"{'s' if len(c.ranges) != 1 else ''})") if c.ranges else ""
        lines.append(f"  [{c.item.kind_name}] {c.item.name} — {where}{sites}")
    body = (f"{label} ({len(lines)}):\n" + "\n".join(lines)) if lines else ""
    return _wrap(body, res, empty=f"{label}: none found.")


def render_rename(edit: WorkspaceEdit, *, root: Optional[Path] = None,
                  applied: bool, new_name: str) -> str:
    if not edit.edits:
        return ("No rename edits were produced. The server may not support "
                "renaming this symbol, or the position may not be on one.")
    by_file: dict[str, int] = {}
    for e in edit.edits:
        by_file[uri_to_path(e.uri)] = by_file.get(uri_to_path(e.uri), 0) + 1
    verb = "Applied" if applied else "Planned (not written)"
    lines = [f"{verb}: rename to {new_name!r} — {len(edit.edits)} edit"
             f"{'s' if len(edit.edits) != 1 else ''} across "
             f"{len(by_file)} file{'s' if len(by_file) != 1 else ''}:"]
    for path, n in by_file.items():
        lines.append(f"  {_rel(path, root)} ({n})")
    if edit.file_operations:
        # Reported, never silently dropped: a rename that also needed a
        # file moved is only half done if we ignore that half.
        lines.append("")
        lines.append("NOT APPLIED — the server also asked for file "
                     "operations, which this tool does not perform:")
        for op in edit.file_operations:
            lines.append(f"  {op}")
        lines.append("The rename is INCOMPLETE until those are done by hand.")
    if not applied:
        lines.append("")
        lines.append("Nothing was written. Re-run with apply=true to make "
                     "these changes.")
    return "\n".join(lines)


def render_candidates(symbols: Iterable[Symbol], *,
                      root: Optional[Path] = None) -> str:
    """Several symbols share the name — say so and hand back positions.

    Renaming one of them arbitrarily would be a silent wrong answer in
    the most expensive place, so the tool refuses and gives the caller
    what ``rename_symbol_strict`` needs.
    """
    lines = ["Several symbols match that name. Pick one and call "
             "rename_symbol_strict with its line and character:"]
    for s in symbols:
        qualified = f"{s.container}.{s.name}" if s.container else s.name
        lines.append(
            f"  [{s.kind_name}] {qualified} — "
            f"{_rel(uri_to_path(s.uri), root)}:"
            f"{s.selection.start.human_line}:{s.selection.start.character + 1}")
    return "\n".join(lines)
