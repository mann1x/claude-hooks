"""Every arm of every LSP union the navigation surface can receive.

These are the shapes we would otherwise only discover in production,
against a server we do not have installed. A parser that reads one arm
and silently returns [] for the others is indistinguishable from a
working one until someone runs the other server — which is the failure
mode this whole subsystem exists to stop rendering as success.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from claude_hooks.lsp_engine import protocol as P  # noqa: E402


def rng(l1, c1, l2, c2):
    return {"start": {"line": l1, "character": c1},
            "end": {"line": l2, "character": c2}}


class SymbolKindTests(unittest.TestCase):

    def test_known_kinds_round_trip(self):
        for num, name in P.SYMBOL_KINDS.items():
            self.assertEqual(P.symbol_kind_name(num), name)
            self.assertEqual(P.parse_symbol_kind(name), num)

    def test_unknown_kind_still_renders(self):
        """A caller is reading this; "" would look like a missing field."""
        self.assertEqual(P.symbol_kind_name(99), "kind_99")
        self.assertEqual(P.symbol_kind_name(None), "unknown")
        self.assertEqual(P.symbol_kind_name(True), "unknown")

    def test_aliases_and_spellings(self):
        for spelling in ("function", "Function", "fn", "func", "def"):
            self.assertEqual(P.parse_symbol_kind(spelling), 12, spelling)
        for spelling in ("enum_member", "enumMember", "enum member"):
            self.assertEqual(P.parse_symbol_kind(spelling), 22, spelling)
        self.assertEqual(P.parse_symbol_kind("type_parameter"), 26)
        self.assertEqual(P.parse_symbol_kind(5), 5)
        self.assertEqual(P.parse_symbol_kind("5"), 5)

    def test_no_filter_and_nonsense_are_both_none(self):
        self.assertIsNone(P.parse_symbol_kind(None))
        self.assertIsNone(P.parse_symbol_kind(""))
        self.assertIsNone(P.parse_symbol_kind("banana"))
        self.assertIsNone(P.parse_symbol_kind(999))
        self.assertIsNone(P.parse_symbol_kind(True))


class LocationTests(unittest.TestCase):

    def test_single_location_object(self):
        out = P.parse_locations({"uri": "file:///a.py", "range": rng(2, 4, 2, 9)})
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].uri, "file:///a.py")
        self.assertEqual(out[0].range.start.line, 2)

    def test_location_array(self):
        out = P.parse_locations([
            {"uri": "file:///a.py", "range": rng(1, 0, 1, 3)},
            {"uri": "file:///b.py", "range": rng(9, 2, 9, 5)},
        ])
        self.assertEqual([l.uri for l in out], ["file:///a.py", "file:///b.py"])

    def test_location_link_uses_target_fields(self):
        """The 3.14 shape. A parser reading only `uri`/`range` returns
        nothing here, which looks exactly like "no definition found"."""
        out = P.parse_locations([{
            "originSelectionRange": rng(0, 0, 0, 4),
            "targetUri": "file:///t.ts",
            "targetRange": rng(10, 0, 20, 1),
            "targetSelectionRange": rng(10, 6, 10, 12),
        }])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].uri, "file:///t.ts")
        self.assertEqual(out[0].range.start.character, 6,
                         "should prefer the selection range (the name)")

    def test_location_link_without_selection_falls_back_to_target_range(self):
        out = P.parse_locations([{
            "targetUri": "file:///t.ts", "targetRange": rng(3, 0, 8, 1),
        }])
        self.assertEqual(out[0].range.start.line, 3)

    def test_null_and_garbage(self):
        self.assertEqual(P.parse_locations(None), [])
        self.assertEqual(P.parse_locations([]), [])
        self.assertEqual(P.parse_locations("nope"), [])
        self.assertEqual(P.parse_locations([{"uri": "file:///a"}]), [],
                         "no range -> not a usable location")
        self.assertEqual(P.parse_locations([{"range": rng(0, 0, 0, 1)}]), [])

    def test_mixed_valid_and_invalid_keeps_the_valid(self):
        out = P.parse_locations([
            {"uri": "file:///a.py", "range": rng(1, 0, 1, 3)},
            {"bogus": True},
        ])
        self.assertEqual(len(out), 1)


class HoverTests(unittest.TestCase):

    def test_markup_content(self):
        self.assertEqual(
            P.parse_hover({"contents": {"kind": "markdown", "value": "# T"}}),
            "# T")

    def test_bare_marked_string(self):
        self.assertEqual(P.parse_hover({"contents": "plain text"}), "plain text")

    def test_marked_string_object(self):
        self.assertEqual(
            P.parse_hover({"contents": {"language": "python", "value": "def f()"}}),
            "def f()")

    def test_array_of_mixed_marked_strings(self):
        """clangd sends exactly this: a code block plus prose."""
        out = P.parse_hover({"contents": [
            {"language": "cpp", "value": "auto f() → bool"},
            "Returns whether it worked.",
        ]})
        self.assertIn("→ bool", out)
        self.assertIn("Returns whether", out)

    def test_empty_states(self):
        self.assertEqual(P.parse_hover(None), "")
        self.assertEqual(P.parse_hover({}), "")
        self.assertEqual(P.parse_hover({"contents": []}), "")
        self.assertEqual(P.parse_hover({"contents": {"kind": "markdown"}}), "")


class DocumentSymbolTests(unittest.TestCase):

    URI = "file:///proj/engine.py"

    def test_hierarchical_flattens_with_container_path(self):
        out = P.parse_document_symbols([{
            "name": "Engine", "kind": 5,
            "range": rng(0, 0, 50, 0), "selectionRange": rng(0, 6, 0, 12),
            "children": [{
                "name": "did_open", "kind": 6,
                "range": rng(10, 4, 20, 0), "selectionRange": rng(10, 8, 10, 16),
                "children": [{
                    "name": "inner", "kind": 12,
                    "range": rng(12, 8, 14, 0),
                    "selectionRange": rng(12, 12, 12, 17),
                }],
            }],
        }], uri=self.URI)

        self.assertEqual([s.name for s in out], ["Engine", "did_open", "inner"])
        self.assertEqual(out[1].container, "Engine")
        self.assertEqual(out[2].container, "Engine.did_open")
        self.assertEqual(out[1].kind_name, "method")

    def test_selection_range_is_the_name_not_the_body(self):
        """Requests must be positioned on the name; the body start is a
        different question with a different answer."""
        out = P.parse_document_symbols([{
            "name": "f", "kind": 12,
            "range": rng(4, 0, 9, 0), "selectionRange": rng(4, 4, 4, 5),
        }], uri=self.URI)
        self.assertEqual(out[0].range.start.character, 0)
        self.assertEqual(out[0].selection.start.character, 4)

    def test_flat_symbol_information(self):
        out = P.parse_document_symbols([{
            "name": "helper", "kind": 12, "containerName": "utils",
            "location": {"uri": "file:///other.py", "range": rng(3, 0, 3, 6)},
        }], uri=self.URI)
        self.assertEqual(out[0].uri, "file:///other.py",
                         "SymbolInformation carries its own URI")
        self.assertEqual(out[0].container, "utils")
        self.assertEqual(out[0].selection, out[0].range,
                         "flat form has no selection range")

    def test_missing_selection_range_falls_back_to_range(self):
        out = P.parse_document_symbols(
            [{"name": "x", "kind": 13, "range": rng(1, 2, 1, 3)}], uri=self.URI)
        self.assertEqual(out[0].selection.start.line, 1)

    def test_garbage_entries_are_skipped_not_fatal(self):
        out = P.parse_document_symbols(
            ["nope", {}, {"name": ""}, {"name": "ok", "kind": 12,
                                        "range": rng(0, 0, 0, 2)}],
            uri=self.URI)
        self.assertEqual([s.name for s in out], ["ok"])

    def test_non_list_result(self):
        self.assertEqual(P.parse_document_symbols(None, uri=self.URI), [])


class WorkspaceSymbolTests(unittest.TestCase):

    def test_symbol_information_shape(self):
        out = P.parse_workspace_symbols([{
            "name": "Engine", "kind": 5, "containerName": "lsp_engine",
            "location": {"uri": "file:///e.py", "range": rng(0, 6, 0, 12)},
        }])
        self.assertEqual(out[0].name, "Engine")
        self.assertEqual(out[0].container, "lsp_engine")

    def test_workspace_symbol_without_a_range_is_kept(self):
        """LSP 3.17 lets a server answer with only a URI so it need not
        read the file. That is an unknown position, not a bad result."""
        out = P.parse_workspace_symbols([
            {"name": "Lazy", "kind": 5, "location": {"uri": "file:///l.py"}},
        ])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].range.start.line, 0)

    def test_entries_without_a_uri_are_dropped(self):
        self.assertEqual(P.parse_workspace_symbols([{"name": "x", "kind": 5}]), [])
        self.assertEqual(P.parse_workspace_symbols([
            {"name": "x", "kind": 5, "location": {"uri": ""}}]), [])


class CallHierarchyTests(unittest.TestCase):

    ITEM = {"name": "run", "kind": 12, "uri": "file:///r.py",
            "range": rng(5, 0, 9, 0), "selectionRange": rng(5, 4, 5, 7)}

    def test_prepare_items(self):
        out = P.parse_call_hierarchy_items([self.ITEM])
        self.assertEqual(out[0].name, "run")
        self.assertEqual(out[0].selection.start.character, 4)

    def test_incoming_reads_from(self):
        out = P.parse_calls(
            [{"from": self.ITEM, "fromRanges": [rng(7, 8, 7, 11)]}],
            direction="incoming")
        self.assertEqual(out[0].item.name, "run")
        self.assertEqual(len(out[0].ranges), 1)
        self.assertEqual(out[0].ranges[0].start.line, 7)

    def test_outgoing_reads_to(self):
        out = P.parse_calls([{"to": self.ITEM, "fromRanges": []}],
                            direction="outgoing")
        self.assertEqual(out[0].item.name, "run")
        self.assertEqual(out[0].ranges, ())

    def test_wrong_direction_finds_nothing(self):
        """`from` and `to` are the only difference between the two
        response types, so reading the wrong one yields a clean empty
        list rather than an error."""
        self.assertEqual(P.parse_calls([{"from": self.ITEM}],
                                       direction="outgoing"), [])

    def test_missing_ranges_and_bad_entries(self):
        out = P.parse_calls([{"from": self.ITEM}, "junk", {}],
                            direction="incoming")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].ranges, ())


class WorkspaceEditTests(unittest.TestCase):

    def test_changes_map(self):
        edit = P.parse_workspace_edit({"changes": {
            "file:///a.py": [{"range": rng(1, 0, 1, 3), "newText": "new"}],
            "file:///b.py": [{"range": rng(2, 0, 2, 3), "newText": "new"},
                             {"range": rng(5, 0, 5, 3), "newText": "new"}],
        }})
        self.assertEqual(len(edit.edits), 3)
        self.assertEqual(sorted(edit.files), ["file:///a.py", "file:///b.py"])

    def test_document_changes_preferred_over_changes(self):
        """A server sending both is describing one rename twice; merging
        would double every edit and corrupt the file."""
        edit = P.parse_workspace_edit({
            "changes": {"file:///a.py": [
                {"range": rng(1, 0, 1, 3), "newText": "X"}]},
            "documentChanges": [{
                "textDocument": {"uri": "file:///a.py", "version": 3},
                "edits": [{"range": rng(1, 0, 1, 3), "newText": "X"}],
            }],
        })
        self.assertEqual(len(edit.edits), 1)

    def test_file_operations_are_reported_not_silently_dropped(self):
        """Renaming a Java class renames its file. Dropping that leaves
        the workspace half-renamed with no indication."""
        edit = P.parse_workspace_edit({"documentChanges": [
            {"textDocument": {"uri": "file:///A.java"},
             "edits": [{"range": rng(0, 6, 0, 7), "newText": "B"}]},
            {"kind": "rename", "oldUri": "file:///A.java",
             "newUri": "file:///B.java"},
            {"kind": "create", "uri": "file:///C.java"},
            {"kind": "delete", "uri": "file:///D.java"},
        ]})
        self.assertEqual(len(edit.edits), 1)
        self.assertEqual(len(edit.file_operations), 3)
        self.assertIn("A.java -> file:///B.java", edit.file_operations[0])
        self.assertTrue(edit.file_operations[1].startswith("create "))

    def test_deletion_edit_has_empty_new_text(self):
        edit = P.parse_workspace_edit({"changes": {
            "file:///a.py": [{"range": rng(1, 0, 2, 0), "newText": ""}]}})
        self.assertEqual(len(edit.edits), 1)
        self.assertEqual(edit.edits[0].new_text, "")

    def test_annotated_text_edit_is_still_a_text_edit(self):
        edit = P.parse_workspace_edit({"changes": {"file:///a.py": [
            {"range": rng(1, 0, 1, 3), "newText": "n", "annotationId": "a1"}]}})
        self.assertEqual(len(edit.edits), 1)

    def test_empty_and_malformed(self):
        for payload in (None, {}, "no", {"changes": None},
                        {"documentChanges": "no"}):
            self.assertEqual(P.parse_workspace_edit(payload).edits, ())

    def test_files_preserves_order_and_dedupes(self):
        edit = P.parse_workspace_edit({"documentChanges": [
            {"textDocument": {"uri": "file:///b.py"},
             "edits": [{"range": rng(0, 0, 0, 1), "newText": "x"}]},
            {"textDocument": {"uri": "file:///a.py"},
             "edits": [{"range": rng(0, 0, 0, 1), "newText": "x"}]},
            {"textDocument": {"uri": "file:///b.py"},
             "edits": [{"range": rng(1, 0, 1, 1), "newText": "x"}]},
        ]})
        self.assertEqual(edit.files, ["file:///b.py", "file:///a.py"])


class PositionTests(unittest.TestCase):

    def test_human_line_is_one_based(self):
        self.assertEqual(P.Position(0, 0).human_line, 1)

    def test_negative_and_missing_coordinates(self):
        out = P.parse_locations([{"uri": "file:///a", "range": {
            "start": {"line": -3, "character": -1}, "end": {"line": 0}}}])
        self.assertEqual(out[0].range.start.line, 0)
        self.assertEqual(out[0].range.start.character, 0)

    def test_missing_character_defaults_to_zero(self):
        out = P.parse_locations([{"uri": "file:///a",
                                  "range": {"start": {"line": 4},
                                            "end": {"line": 4}}}])
        self.assertEqual(out[0].range.start.character, 0)

    def test_bool_is_not_an_int_here(self):
        """`True` is an int in Python and would sail through an isinstance
        check, producing line 1 from a field that was never a number."""
        self.assertEqual(P.parse_locations(
            [{"uri": "file:///a", "range": {"start": {"line": True}}}]), [])


if __name__ == "__main__":
    unittest.main()
