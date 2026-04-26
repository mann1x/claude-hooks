"""
tree-sitter backend — extends ``code_graph`` to non-Python languages.

Optional dependency: install ``tree-sitter-language-pack`` (preferred) or
``tree-sitter-languages``. Both expose a ``get_parser(name)`` factory that
returns a fully-built parser without per-grammar compilation hassle.

If neither package is importable the backend is silently disabled and
the builder falls back to Python-only extraction. No ImportError ever
escapes — we cap the cost of "tree-sitter is missing" at a debug log.

Schema:

The extractor emits the same node/edge shape as
``builder._PyExtractor`` so the linker pass and report renderer don't
care which backend produced what:

- module nodes  : ``module:<dotted-path>``
- class nodes   : ``class:<dotted-path>.<name>``
- function nodes: ``func:<dotted-path>.<name>``
- edges         : ``contains`` / ``imports`` / ``calls`` (calls remain
                  ``unresolved:<name>`` until the linker pass joins them
                  to defs by qualified or bare name).

Per-language extraction is driven by tree-sitter S-expression queries
in ``_LANG_QUERIES``. Adding a language is purely additive: write a
query, register it in ``_LANG_QUERIES`` and ``EXTENSIONS_BY_LANG``,
done. No code changes anywhere else.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger("claude_hooks.code_graph.tree_sitter_backend")


# ---------------------------------------------------------------------------
# Lazy import of one of two upstream packages.
# ---------------------------------------------------------------------------

_get_parser = None
_get_language = None
_backend_name: Optional[str] = None
_import_error: Optional[str] = None


def _try_imports() -> None:
    global _get_parser, _get_language, _backend_name, _import_error
    if _get_parser is not None or _import_error is not None:
        return  # already tried
    # Prefer the actively-maintained fork.
    for mod_name in ("tree_sitter_language_pack", "tree_sitter_languages"):
        try:
            mod = __import__(mod_name, fromlist=["get_parser", "get_language"])
            _get_parser = getattr(mod, "get_parser")
            _get_language = getattr(mod, "get_language")
            _backend_name = mod_name
            return
        except (ImportError, AttributeError) as e:
            _import_error = f"{mod_name}: {e}"
    log.debug("tree-sitter backend disabled: no compatible package installed")


def is_available() -> bool:
    """True iff a tree-sitter packages can be imported."""
    _try_imports()
    return _get_parser is not None


def backend_name() -> Optional[str]:
    _try_imports()
    return _backend_name


# ---------------------------------------------------------------------------
# Language registry — add a row here to support a new language.
# ---------------------------------------------------------------------------

# Map file extension -> tree-sitter language name.
EXTENSIONS_BY_LANG: dict[str, str] = {
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".rb": "ruby",
}


def supported_extensions() -> frozenset[str]:
    if not is_available():
        return frozenset()
    return frozenset(EXTENSIONS_BY_LANG.keys())


# tree-sitter S-expression queries — kept small and resilient. We don't
# attempt to be exhaustive (no decorator capture, no JSX call resolution,
# etc.). The bar is "the report shows the right god nodes", not "perfect
# call graph". Each query captures four kinds of things; missing groups
# are simply not extracted for that language.
_LANG_QUERIES: dict[str, str] = {
    # JavaScript / JSX / mjs / cjs
    "javascript": """
        (function_declaration name: (identifier) @function)
        (method_definition name: (property_identifier) @method)
        (class_declaration name: (identifier) @class)
        (call_expression function: (identifier) @call)
        (call_expression function: (member_expression
            property: (property_identifier) @call_member))
        (import_statement source: (string) @import)
        (call_expression
            function: (identifier) @require_kw
            arguments: (arguments (string) @require)
            (#eq? @require_kw "require"))
    """,
    # TypeScript — same shape as JS plus interfaces (treated as classes).
    "typescript": """
        (function_declaration name: (identifier) @function)
        (method_definition name: (property_identifier) @method)
        (class_declaration name: (type_identifier) @class)
        (interface_declaration name: (type_identifier) @class)
        (call_expression function: (identifier) @call)
        (call_expression function: (member_expression
            property: (property_identifier) @call_member))
        (import_statement source: (string) @import)
    """,
    # TSX — same as TypeScript; tsx parser superset handles JSX nodes.
    "tsx": """
        (function_declaration name: (identifier) @function)
        (method_definition name: (property_identifier) @method)
        (class_declaration name: (type_identifier) @class)
        (interface_declaration name: (type_identifier) @class)
        (call_expression function: (identifier) @call)
        (call_expression function: (member_expression
            property: (property_identifier) @call_member))
        (import_statement source: (string) @import)
    """,
    # Go
    "go": """
        (function_declaration name: (identifier) @function)
        (method_declaration name: (field_identifier) @method)
        (type_declaration (type_spec name: (type_identifier) @class))
        (call_expression function: (identifier) @call)
        (call_expression function: (selector_expression
            field: (field_identifier) @call_member))
        (import_spec path: (interpreted_string_literal) @import)
    """,
    # Rust
    "rust": """
        (function_item name: (identifier) @function)
        (struct_item name: (type_identifier) @class)
        (enum_item name: (type_identifier) @class)
        (impl_item type: (type_identifier) @class)
        (trait_item name: (type_identifier) @class)
        (call_expression function: (identifier) @call)
        (call_expression function: (field_expression
            field: (field_identifier) @call_member))
        (use_declaration argument: (scoped_identifier) @import)
    """,
    # Java
    "java": """
        (method_declaration name: (identifier) @method)
        (class_declaration name: (identifier) @class)
        (interface_declaration name: (identifier) @class)
        (method_invocation name: (identifier) @call)
        (import_declaration (scoped_identifier) @import)
    """,
    # Ruby
    "ruby": """
        (method name: (identifier) @function)
        (class name: (constant) @class)
        (module name: (constant) @class)
        (call method: (identifier) @call)
    """,
}


# ---------------------------------------------------------------------------
# Extractor
# ---------------------------------------------------------------------------

# Reuse the id-builder helpers from the Python backend to keep ids stable.
from claude_hooks.code_graph.builder import (  # noqa: E402  (import after lazy init)
    _class_id,
    _func_id,
    _module_id,
)


def extract_file(
    *,
    file: Path,
    rel: str,
    dotted: str,
    text: bytes,
) -> tuple[list[dict], list[dict]]:
    """Parse ``file`` with tree-sitter, return (nodes, edges).

    Returns ``([], [])`` and logs at debug level when the language isn't
    supported, the parser is missing, or parsing fails.
    """
    if not is_available():
        return ([], [])
    ext = file.suffix.lower()
    lang = EXTENSIONS_BY_LANG.get(ext)
    if lang is None:
        return ([], [])

    try:
        parser = _get_parser(lang)  # type: ignore[misc]
        language = _get_language(lang)  # type: ignore[misc]
    except Exception as e:
        log.debug("tree-sitter parser load failed for %s: %s", lang, e)
        return ([], [])

    try:
        tree = parser.parse(text)
    except Exception as e:
        log.debug("tree-sitter parse failed for %s: %s", rel, e)
        return ([], [])

    try:
        query_src = _LANG_QUERIES[lang]
        # tree_sitter API has shifted across releases. Newest (0.25+)
        # wants ``Query(language, src)``; older (≤0.20) only had
        # ``language.query(src)``. Try the newer first to avoid the
        # deprecation warning, then fall back.
        try:
            from tree_sitter import Query  # type: ignore
            query = Query(language, query_src)
        except (ImportError, TypeError):
            query = language.query(query_src)
    except Exception as e:
        log.debug("tree-sitter query compile failed for %s: %s", lang, e)
        return ([], [])

    nodes: list[dict] = []
    edges: list[dict] = []

    # Module node
    mod_id = _module_id(dotted)
    nodes.append({
        "id": mod_id,
        "type": "module",
        "name": dotted,
        "file": rel,
        "line": 1,
        "tag": "EXTRACTED",
    })

    # Materialise — _safe_captures is a generator and we iterate it twice.
    captures = list(_safe_captures(query, tree.root_node))

    # First pass: emit defs (function/method/class). These land directly
    # under the module — tree-sitter Pass-1 doesn't bother with nested
    # scopes for non-Python languages (the graph stays flat-ish but the
    # god-node metric still works because edges count).
    def_names_by_kind: dict[str, list[tuple[str, int]]] = {
        "function": [], "method": [], "class": [],
    }
    for cap_name, ts_node in captures:
        if cap_name in ("function", "method", "class"):
            name = _node_text(text, ts_node)
            if not name:
                continue
            def_names_by_kind[cap_name].append((name, ts_node.start_point[0] + 1))

    for kind in ("class", "function", "method"):
        for name, line in def_names_by_kind[kind]:
            qual = f"{dotted}.{name}"
            nid = _class_id(qual) if kind == "class" else _func_id(qual)
            nodes.append({
                "id": nid,
                "type": "method" if kind == "method" else kind,
                "name": name,
                "qualname": qual,
                "file": rel,
                "line": line,
                "tag": "EXTRACTED",
                "doc": None,
            })
            edges.append({
                "source": mod_id,
                "target": nid,
                "type": "contains",
                "tag": "EXTRACTED",
            })

    # Second pass: imports + calls (no scope tracking — calls are
    # attributed to the module they live in for non-Python langs).
    for cap_name, ts_node in captures:
        raw = _node_text(text, ts_node)
        if not raw:
            continue
        if cap_name == "import":
            target = _normalize_import_target(raw)
            if target:
                edges.append({
                    "source": mod_id,
                    "target": _module_id(target),
                    "type": "imports",
                    "tag": "EXTRACTED",
                })
        elif cap_name == "require":
            target = _normalize_import_target(raw)
            if target:
                edges.append({
                    "source": mod_id,
                    "target": _module_id(target),
                    "type": "imports",
                    "tag": "EXTRACTED",
                })
        elif cap_name in ("call", "call_member"):
            edges.append({
                "source": mod_id,
                "target": f"unresolved:{raw}",
                "type": "calls",
                "tag": "EXTRACTED",
            })

    return nodes, edges


def _safe_captures(query, root_node):
    """Yield (capture_name, ts_node) pairs across tree-sitter API versions.

    - tree_sitter <= 0.20: ``query.captures(node)`` returns
      ``list[tuple[Node, str]]``
    - tree_sitter 0.21–0.24: ``query.captures(node)`` returns
      ``dict[str, list[Node]]``
    - tree_sitter >= 0.25: ``query.captures`` is gone; use
      ``QueryCursor(query).captures(node)`` which returns the dict shape.
    """
    result = None
    # Newest API first.
    try:
        from tree_sitter import QueryCursor  # type: ignore
        result = QueryCursor(query).captures(root_node)
    except (ImportError, TypeError):
        result = None
    # Fall back to the older method-on-query API.
    if result is None:
        try:
            result = query.captures(root_node)
        except Exception as e:
            log.debug("query.captures failed: %s", e)
            return
    if isinstance(result, dict):
        for name, ts_nodes in result.items():
            for n in ts_nodes:
                yield name, n
    else:
        for ts_node, name in result:
            yield name, ts_node


def _node_text(source: bytes, node) -> str:
    try:
        return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")
    except Exception:
        return ""


def _normalize_import_target(raw: str) -> Optional[str]:
    """Best-effort: turn a quoted/raw import string into a dotted module name.

    - ``"./foo/bar"`` → ``foo.bar``
    - ``"@scope/pkg"`` → ``@scope.pkg``  (preserves scope distinction)
    - ``"github.com/x/y"`` → ``github.com.x.y``
    - ``Pkg::Sub::Mod`` (Rust use) → ``Pkg.Sub.Mod``
    - ``com.example.Foo`` (Java) → kept as-is
    """
    s = raw.strip()
    if s.startswith(("'", '"')) and s.endswith(("'", '"')) and len(s) >= 2:
        s = s[1:-1]
    if not s:
        return None
    s = s.replace("\\", "/")
    # Strip relative prefixes
    while s.startswith(("./", "../")):
        s = s[2:] if s.startswith("./") else s[3:]
    s = s.lstrip("/")
    # Rust :: separators
    s = s.replace("::", ".")
    # Path separators -> dots
    s = s.replace("/", ".")
    # Drop file extensions like ".js"
    for ext in (".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs"):
        if s.endswith(ext):
            s = s[: -len(ext)]
            break
    return s or None
