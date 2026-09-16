"""JSON encoding for the navigation surface, so the daemon can serve it.

The daemon owns one :class:`Engine` per project, and that was always
meant to be *the* engine. The MCP server built its own in-process
instead, which on this host meant a second fleet of language servers per
project: 12 servers across three fleets holding 357 MB, each tsserver
indexing the same tree, each warming up separately — and two diagnostic
caches that could disagree about the same file. Sharing the daemon fixes
all of that at once, and makes de-duplication between the hook and the
MCP possible at all, since they finally look at the same state.

What the daemon could already serve was ``did_open`` / ``did_change`` /
``did_close`` / ``diagnostics``: enough for the PostToolUse hook, which
is what it was built for. Navigation — definitions, references, hover,
symbols, call hierarchy, rename — had no wire representation, which is
the gap this module fills.

The shapes mirror :mod:`claude_hooks.lsp_engine.protocol` exactly rather
than inventing a second vocabulary. Each encoder is the inverse of its
decoder and round-trips through ``json.dumps``; ``tests/
test_lsp_engine_wire.py`` pins that, because a silently lossy field here
would surface as a navigation result that is subtly wrong rather than
one that fails.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from claude_hooks.lsp_engine import protocol as P
from claude_hooks.lsp_engine.engine import NavResponse

# ─── leaves ──────────────────────────────────────────────────────────


def position_to_json(p: P.Position) -> dict:
    return {"line": p.line, "character": p.character}


def position_from_json(d: dict) -> P.Position:
    return P.Position(line=int(d["line"]), character=int(d["character"]))


def range_to_json(r: P.Range) -> dict:
    return {"start": position_to_json(r.start), "end": position_to_json(r.end)}


def range_from_json(d: dict) -> P.Range:
    return P.Range(start=position_from_json(d["start"]),
                   end=position_from_json(d["end"]))


def location_to_json(loc: P.Location) -> dict:
    return {"uri": loc.uri, "range": range_to_json(loc.range)}


def location_from_json(d: dict) -> P.Location:
    return P.Location(uri=d["uri"], range=range_from_json(d["range"]))


def symbol_to_json(s: P.Symbol) -> dict:
    return {
        "name": s.name, "kind": s.kind, "uri": s.uri,
        "range": range_to_json(s.range),
        "selection": range_to_json(s.selection),
        "container": s.container, "detail": s.detail,
    }


def symbol_from_json(d: dict) -> P.Symbol:
    return P.Symbol(
        name=d["name"], kind=int(d["kind"]), uri=d["uri"],
        range=range_from_json(d["range"]),
        selection=range_from_json(d["selection"]),
        container=d.get("container", ""), detail=d.get("detail", ""),
    )


def call_item_to_json(i: P.CallHierarchyItem) -> dict:
    return {
        "name": i.name, "kind": i.kind, "uri": i.uri,
        "range": range_to_json(i.range),
        "selection": range_to_json(i.selection),
        "detail": i.detail,
    }


def call_item_from_json(d: dict) -> P.CallHierarchyItem:
    return P.CallHierarchyItem(
        name=d["name"], kind=int(d["kind"]), uri=d["uri"],
        range=range_from_json(d["range"]),
        selection=range_from_json(d["selection"]),
        detail=d.get("detail", ""),
    )


def call_to_json(c: P.CallHierarchyCall) -> dict:
    return {"item": call_item_to_json(c.item),
            "ranges": [range_to_json(r) for r in c.ranges]}


def call_from_json(d: dict) -> P.CallHierarchyCall:
    return P.CallHierarchyCall(
        item=call_item_from_json(d["item"]),
        ranges=tuple(range_from_json(r) for r in d.get("ranges", ())),
    )


def call_item_only_to_json(i: P.CallHierarchyItem) -> dict:
    """``prepare_call_hierarchy`` returns bare items, not calls."""
    return call_item_to_json(i)


def call_item_only_from_json(d: dict) -> P.CallHierarchyItem:
    return call_item_from_json(d)


def diagnostic_to_json(d) -> dict:
    return {"uri": d.uri, "severity": d.severity, "line": d.line,
            "character": d.character, "message": d.message,
            "code": d.code, "source": d.source}


def diagnostic_from_json(d: dict):
    from claude_hooks.lsp_engine.lsp import Diagnostic
    return Diagnostic(uri=d["uri"], severity=int(d["severity"]),
                      line=int(d["line"]), character=int(d["character"]),
                      message=d["message"], code=d.get("code"),
                      source=d.get("source"))


def text_edit_to_json(e: P.TextEdit) -> dict:
    return {"uri": e.uri, "range": range_to_json(e.range),
            "new_text": e.new_text}


def text_edit_from_json(d: dict) -> P.TextEdit:
    return P.TextEdit(uri=d["uri"], range=range_from_json(d["range"]),
                      new_text=d["new_text"])


def workspace_edit_to_json(w: P.WorkspaceEdit) -> dict:
    return {"edits": [text_edit_to_json(e) for e in w.edits],
            "file_operations": list(w.file_operations)}


def workspace_edit_from_json(d: dict) -> P.WorkspaceEdit:
    return P.WorkspaceEdit(
        edits=tuple(text_edit_from_json(e) for e in d.get("edits", ())),
        file_operations=tuple(d.get("file_operations", ())),
    )


# ─── responses ───────────────────────────────────────────────────────

#: item kind -> (encode, decode). The kind travels with the payload so
#: the receiver does not have to infer it from the op it called, which
#: would couple the two in a way that breaks quietly when an op changes
#: what it returns.
_ITEM_CODECS: dict[str, tuple[Callable[[Any], Any], Callable[[Any], Any]]] = {
    "location": (location_to_json, location_from_json),
    "symbol": (symbol_to_json, symbol_from_json),
    "call": (call_to_json, call_from_json),
    "call_item": (call_item_only_to_json, call_item_only_from_json),
    "edit": (workspace_edit_to_json, workspace_edit_from_json),
    # Hover comes back as plain markdown strings.
    "text": (lambda s: s, lambda s: s),
}


def nav_to_json(res: NavResponse, *, item_kind: str) -> dict:
    """Encode a :class:`NavResponse`, provenance included.

    The provenance fields are the point of the type — they are what
    stops an empty ``items`` being read as a fact about the code — so
    dropping them on the wire would reintroduce exactly the bug the
    engine exists to prevent.
    """
    encode, _ = _ITEM_CODECS[item_kind]
    return {
        "item_kind": item_kind,
        "items": [encode(i) for i in res.items],
        "consulted": list(res.consulted),
        "failures": [[n, m] for n, m in res.failures],
        "progress": res.progress,
        "not_running": list(res.not_running),
        "scan_truncated_at": res.scan_truncated_at,
    }


def nav_from_json(d: dict) -> NavResponse:
    _, decode = _ITEM_CODECS[d["item_kind"]]
    return NavResponse(
        items=[decode(i) for i in d.get("items", [])],
        consulted=tuple(d.get("consulted", ())),
        failures=tuple((n, m) for n, m in d.get("failures", ())),
        progress=d.get("progress"),
        not_running=tuple(d.get("not_running", ())),
        scan_truncated_at=int(d.get("scan_truncated_at", 0) or 0),
    )


def diagnostics_result_to_json(res: Any, to_json: Callable[[Any], dict]) -> dict:
    """Encode a ``DiagnosticsResult``, keeping ``settled``.

    ``settled`` is the field that separates "this file is clean" from
    "the server had not answered yet", so it is not optional.
    """
    return {
        "items": [to_json(d) for d in res.items],
        "settled": res.settled,
        "server": res.server,
        "timeout": res.timeout,
        "waited": getattr(res, "waited", 0.0),
        "source": getattr(res, "source", ""),
    }


def optional_str(value: Any) -> Optional[str]:
    return value if isinstance(value, str) else None
