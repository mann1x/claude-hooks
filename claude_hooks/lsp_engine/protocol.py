"""Parsing for the LSP payloads the navigation requests return.

Every request in the navigation surface answers with a union type, and
most of them with a union whose arms were added in different protocol
versions and are all still in use:

``textDocument/definition``
    ``Location``, ``Location[]``, ``LocationLink[]``, or ``null`` — and
    a ``LocationLink`` spells its target ``targetUri`` / ``targetRange``
    rather than ``uri`` / ``range``, so a parser that reads only the
    older shape returns *nothing* against a modern server rather than
    failing.
``textDocument/hover``
    ``contents`` is ``MarkupContent``, ``MarkedString``, or an array of
    ``MarkedString`` — where ``MarkedString`` is *either* a bare string
    *or* ``{language, value}``. Four shapes for one field.
``textDocument/documentSymbol``
    hierarchical ``DocumentSymbol[]`` or flat ``SymbolInformation[]``,
    chosen by the server, with different field names for the range.
``textDocument/rename``
    a ``WorkspaceEdit`` carrying ``changes`` (a URI→edits map) or
    ``documentChanges`` (an ordered list that may also contain *file*
    operations, which are not text edits at all).

These are pure functions over already-decoded JSON so they can be tested
exhaustively without a language server, which matters because the arms
we would otherwise never exercise are exactly the ones a server we do
not have installed will send.

The rule throughout: **an unparseable payload is empty, never partial.**
A half-read rename is worse than a refused one.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

# ``SymbolKind`` from the specification. The names are the ones clients
# show and the ones a caller will type, so the mapping is also the
# vocabulary for ``symbol_kind`` filters.
SYMBOL_KINDS: dict[int, str] = {
    1: "file", 2: "module", 3: "namespace", 4: "package", 5: "class",
    6: "method", 7: "property", 8: "field", 9: "constructor", 10: "enum",
    11: "interface", 12: "function", 13: "variable", 14: "constant",
    15: "string", 16: "number", 17: "boolean", 18: "array", 19: "object",
    20: "key", 21: "null", 22: "enum_member", 23: "struct", 24: "event",
    25: "operator", 26: "type_parameter",
}
_KIND_BY_NAME = {v: k for k, v in SYMBOL_KINDS.items()}
# Tolerated spellings, so a caller is not required to know ours.
_KIND_ALIASES = {
    "enummember": 22, "enum_member": 22, "typeparameter": 26,
    "type_parameter": 26, "func": 12, "fn": 12, "def": 12, "var": 13,
    "const": 14, "struct": 23, "type": 5, "iface": 11,
}


def symbol_kind_name(kind: Any) -> str:
    """Never raises and never returns empty — an unknown kind still has
    to render as *something* in a result the caller is reading."""
    if isinstance(kind, bool) or not isinstance(kind, int):
        return "unknown"
    return SYMBOL_KINDS.get(kind, f"kind_{kind}")


def parse_symbol_kind(value: Any) -> Optional[int]:
    """Accept a number, a spec name, or a common alias.

    Returns None for "no filter"; callers must not confuse that with a
    parse failure, so an *unrecognised* name also returns None and the
    caller is expected to have validated first if it cares.
    """
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value in SYMBOL_KINDS else None
    if isinstance(value, str):
        key = value.strip().lower().replace(" ", "_").replace("-", "_")
        if key.isdigit():
            n = int(key)
            return n if n in SYMBOL_KINDS else None
        return _KIND_BY_NAME.get(key) or _KIND_ALIASES.get(key.replace("_", ""))
    return None


# ───────────────────────── value types ──────────────────────────────


@dataclass(frozen=True)
class Position:
    line: int
    character: int

    @property
    def human_line(self) -> int:
        """1-based, for display. LSP counts from 0 and every editor
        counts from 1; picking one and converting at the boundary is
        cheaper than auditing which convention a given number is in."""
        return self.line + 1


@dataclass(frozen=True)
class Range:
    start: Position
    end: Position


@dataclass(frozen=True)
class Location:
    uri: str
    range: Range


@dataclass(frozen=True)
class Symbol:
    name: str
    kind: int
    uri: str
    range: Range
    #: Where the *name* is, as opposed to the whole body. Requests must
    #: be positioned here: asking for references at the start of a
    #: class body is not asking about the class.
    selection: Range
    container: str = ""
    detail: str = ""

    @property
    def kind_name(self) -> str:
        return symbol_kind_name(self.kind)


@dataclass(frozen=True)
class CallHierarchyItem:
    name: str
    kind: int
    uri: str
    range: Range
    selection: Range
    detail: str = ""

    @property
    def kind_name(self) -> str:
        return symbol_kind_name(self.kind)


@dataclass(frozen=True)
class CallHierarchyCall:
    item: CallHierarchyItem
    #: The call sites, in the *caller's* file for incoming calls and in
    #: the *callee's* for outgoing. The spec's asymmetry, not ours.
    ranges: tuple[Range, ...] = ()


@dataclass(frozen=True)
class TextEdit:
    uri: str
    range: Range
    new_text: str


@dataclass(frozen=True)
class WorkspaceEdit:
    edits: tuple[TextEdit, ...] = ()
    #: Create/rename/delete operations, which a rename can legitimately
    #: include (renaming a Java class renames its file). We do not apply
    #: them; we report them, because silently dropping them would leave
    #: the workspace half-renamed.
    file_operations: tuple[str, ...] = ()

    @property
    def files(self) -> list[str]:
        seen: list[str] = []
        for e in self.edits:
            if e.uri not in seen:
                seen.append(e.uri)
        return seen


# ───────────────────────── primitives ───────────────────────────────


def _as_position(raw: Any) -> Optional[Position]:
    if not isinstance(raw, dict):
        return None
    line, char = raw.get("line"), raw.get("character")
    if not isinstance(line, int) or isinstance(line, bool):
        return None
    if not isinstance(char, int) or isinstance(char, bool):
        char = 0
    return Position(line=max(0, line), character=max(0, char))


def _as_range(raw: Any) -> Optional[Range]:
    if not isinstance(raw, dict):
        return None
    start = _as_position(raw.get("start"))
    if start is None:
        return None
    end = _as_position(raw.get("end")) or start
    return Range(start=start, end=end)


def _zero_range() -> Range:
    return Range(start=Position(0, 0), end=Position(0, 0))


# ───────────────────────── locations ────────────────────────────────


def parse_locations(result: Any) -> list[Location]:
    """``Location | Location[] | LocationLink[] | null`` → list.

    Shared by definition / declaration / type-definition / implementation
    / references, which all answer with this union.
    """
    if result is None:
        return []
    items: Iterable[Any] = result if isinstance(result, list) else [result]
    out: list[Location] = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        # LocationLink: the 3.14 shape, which most modern servers prefer.
        # targetSelectionRange points at the name, targetRange at the
        # whole definition; the name is what a reader wants.
        if "targetUri" in raw:
            uri = raw.get("targetUri")
            rng = (_as_range(raw.get("targetSelectionRange"))
                   or _as_range(raw.get("targetRange")))
        else:
            uri = raw.get("uri")
            rng = _as_range(raw.get("range"))
        if isinstance(uri, str) and uri and rng is not None:
            out.append(Location(uri=uri, range=rng))
    return out


# ───────────────────────── hover ────────────────────────────────────


def parse_hover(result: Any) -> str:
    """Flatten ``Hover.contents`` to plain text; "" when there is none.

    An empty string is a real answer here ("the server has nothing to
    say about this position"), which is why this returns str rather than
    Optional[str] — there is no third state to distinguish.
    """
    if not isinstance(result, dict):
        return ""
    return _marked(result.get("contents")).strip()


def _marked(raw: Any) -> str:
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        parts = [_marked(x) for x in raw]
        return "\n\n".join(p for p in parts if p.strip())
    if isinstance(raw, dict):
        # MarkupContent {kind, value} and MarkedString {language, value}
        # are distinguished only by their other key; both carry `value`.
        value = raw.get("value")
        return value if isinstance(value, str) else ""
    return ""


# ───────────────────────── symbols ──────────────────────────────────


def parse_document_symbols(result: Any, *, uri: str) -> list[Symbol]:
    """``DocumentSymbol[]`` (nested) or ``SymbolInformation[]`` (flat).

    The nested form is flattened depth-first with the ancestor names
    joined into ``container``, so ``Engine.did_open`` is findable as
    ``did_open`` with container ``Engine`` — which is what makes
    disambiguating two same-named methods possible at all.
    """
    if not isinstance(result, list):
        return []
    out: list[Symbol] = []
    for raw in result:
        _flatten_symbol(raw, uri=uri, container="", out=out)
    return out


def _flatten_symbol(raw: Any, *, uri: str, container: str,
                    out: list[Symbol]) -> None:
    if not isinstance(raw, dict):
        return
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        return
    kind = raw.get("kind")
    kind = kind if isinstance(kind, int) and not isinstance(kind, bool) else 0

    if "location" in raw:
        # SymbolInformation: one range, no selection range, and the URI
        # travels with the symbol (workspace/symbol reuses this shape
        # for results from files we never opened).
        loc = raw.get("location")
        loc_uri = loc.get("uri") if isinstance(loc, dict) else None
        rng = _as_range(loc.get("range")) if isinstance(loc, dict) else None
        rng = rng or _zero_range()
        sym_uri = loc_uri if isinstance(loc_uri, str) and loc_uri else uri
        own_container = raw.get("containerName")
        out.append(Symbol(
            name=name, kind=kind, uri=sym_uri, range=rng, selection=rng,
            container=own_container if isinstance(own_container, str) else container,
            detail=_str(raw.get("detail")),
        ))
        return

    rng = _as_range(raw.get("range")) or _zero_range()
    sel = _as_range(raw.get("selectionRange")) or rng
    out.append(Symbol(
        name=name, kind=kind, uri=uri, range=rng, selection=sel,
        container=container, detail=_str(raw.get("detail")),
    ))
    children = raw.get("children")
    if isinstance(children, list):
        nested = f"{container}.{name}" if container else name
        for child in children:
            _flatten_symbol(child, uri=uri, container=nested, out=out)


def parse_workspace_symbols(result: Any) -> list[Symbol]:
    """``SymbolInformation[]`` or the 3.17 ``WorkspaceSymbol[]``.

    The newer shape may carry ``location`` as ``{uri}`` with **no
    range** — a deliberate optimisation letting a server answer without
    reading the file. That is a symbol at an unknown position, not a
    malformed one, so it is kept with a zero range rather than dropped.
    """
    if not isinstance(result, list):
        return []
    out: list[Symbol] = []
    for raw in result:
        if not isinstance(raw, dict):
            continue
        name = raw.get("name")
        if not isinstance(name, str) or not name:
            continue
        loc = raw.get("location")
        if not isinstance(loc, dict):
            continue
        uri = loc.get("uri")
        if not isinstance(uri, str) or not uri:
            continue
        rng = _as_range(loc.get("range")) or _zero_range()
        kind = raw.get("kind")
        out.append(Symbol(
            name=name,
            kind=kind if isinstance(kind, int) and not isinstance(kind, bool) else 0,
            uri=uri, range=rng, selection=rng,
            container=_str(raw.get("containerName")),
            detail=_str(raw.get("detail")),
        ))
    return out


# ───────────────────────── call hierarchy ───────────────────────────


def parse_call_hierarchy_items(result: Any) -> list[CallHierarchyItem]:
    if not isinstance(result, list):
        return []
    out: list[CallHierarchyItem] = []
    for raw in result:
        item = _as_call_item(raw)
        if item is not None:
            out.append(item)
    return out


def _as_call_item(raw: Any) -> Optional[CallHierarchyItem]:
    if not isinstance(raw, dict):
        return None
    name, uri = raw.get("name"), raw.get("uri")
    if not isinstance(name, str) or not isinstance(uri, str) or not uri:
        return None
    rng = _as_range(raw.get("range")) or _zero_range()
    sel = _as_range(raw.get("selectionRange")) or rng
    kind = raw.get("kind")
    return CallHierarchyItem(
        name=name,
        kind=kind if isinstance(kind, int) and not isinstance(kind, bool) else 0,
        uri=uri, range=rng, selection=sel, detail=_str(raw.get("detail")),
    )


def parse_calls(result: Any, *, direction: str) -> list[CallHierarchyCall]:
    """``CallHierarchyIncomingCall[]`` / ``…OutgoingCall[]``.

    The two differ by one field name — ``from`` versus ``to`` — and
    ``from`` is a Python keyword, which is precisely why this is parsed
    into a named type here rather than passed around as a dict.
    """
    key = "from" if direction == "incoming" else "to"
    if not isinstance(result, list):
        return []
    out: list[CallHierarchyCall] = []
    for raw in result:
        if not isinstance(raw, dict):
            continue
        item = _as_call_item(raw.get(key))
        if item is None:
            continue
        ranges = raw.get("fromRanges")
        spans = tuple(
            r for r in (_as_range(x) for x in ranges) if r is not None
        ) if isinstance(ranges, list) else ()
        out.append(CallHierarchyCall(item=item, ranges=spans))
    return out


# ───────────────────────── workspace edit ───────────────────────────


def parse_workspace_edit(result: Any) -> WorkspaceEdit:
    """``WorkspaceEdit`` → a flat, ordered edit list.

    Both containers are read. ``documentChanges`` wins when present,
    because the spec says a client that supports it must ignore
    ``changes`` — and a server sending both is describing one rename
    twice, so merging them would double every edit.
    """
    if not isinstance(result, dict):
        return WorkspaceEdit()

    doc_changes = result.get("documentChanges")
    if isinstance(doc_changes, list):
        edits: list[TextEdit] = []
        ops: list[str] = []
        for entry in doc_changes:
            if not isinstance(entry, dict):
                continue
            kind = entry.get("kind")
            if kind in ("create", "rename", "delete"):
                ops.append(_describe_file_op(kind, entry))
                continue
            doc = entry.get("textDocument")
            uri = doc.get("uri") if isinstance(doc, dict) else None
            if not isinstance(uri, str) or not uri:
                continue
            edits.extend(_text_edits(uri, entry.get("edits")))
        return WorkspaceEdit(edits=tuple(edits), file_operations=tuple(ops))

    changes = result.get("changes")
    if isinstance(changes, dict):
        edits = []
        for uri, raw in changes.items():
            if isinstance(uri, str) and uri:
                edits.extend(_text_edits(uri, raw))
        return WorkspaceEdit(edits=tuple(edits))

    return WorkspaceEdit()


def _text_edits(uri: str, raw: Any) -> list[TextEdit]:
    if not isinstance(raw, list):
        return []
    out: list[TextEdit] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        rng = _as_range(item.get("range"))
        if rng is None:
            continue
        new_text = item.get("newText")
        # An AnnotatedTextEdit adds a field but is otherwise a TextEdit;
        # an empty newText is a deletion, which is valid.
        out.append(TextEdit(
            uri=uri, range=rng,
            new_text=new_text if isinstance(new_text, str) else "",
        ))
    return out


def _describe_file_op(kind: str, entry: dict) -> str:
    if kind == "rename":
        return f"rename {_str(entry.get('oldUri'))} -> {_str(entry.get('newUri'))}"
    return f"{kind} {_str(entry.get('uri'))}"


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""
