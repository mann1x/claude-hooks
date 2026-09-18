"""Parity audit against cclsp, recorded so it cannot drift back.

Every constant here was read out of cclsp's own bundle
(`/usr/local/lib/node_modules/cclsp/dist/index.js`, v1.x, 2026-09-16)
rather than from its docs, because the docs do not mention most of it.
The rule the user set: **no regressions — same feature set and
languages, or better.** Unwanted behaviour is explicitly *not*
inherited, and each exclusion is named with its reason.

If cclsp is ever upgraded and this file needs changing, re-extract
rather than edit by hand; the point is that these are observations.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from claude_hooks.lsp_engine.config import load_cclsp_config  # noqa: E402
from claude_hooks.lsp_engine.lsp import (  # noqa: E402
    _LANGUAGE_ID_BY_EXT, client_capabilities, language_id_for,
)
from claude_hooks.lsp_engine.protocol import (  # noqa: E402
    SYMBOL_KINDS, symbol_kind_name,
)

#: cclsp's `getLanguageId` map, verbatim.
CCLSP_LANGUAGE_MAP = {
    "ts": "typescript", "tsx": "typescriptreact", "js": "javascript",
    "jsx": "javascriptreact", "py": "python", "go": "go", "rs": "rust",
    "c": "c", "cpp": "cpp", "h": "c", "hpp": "cpp", "java": "java",
    "jar": "java", "class": "java", "cs": "csharp", "php": "php",
    "rb": "ruby", "swift": "swift", "kt": "kotlin", "scala": "scala",
    "dart": "dart", "lua": "lua", "sh": "shellscript",
    "bash": "shellscript", "json": "json", "yaml": "yaml", "yml": "yaml",
    "xml": "xml", "html": "html", "css": "css", "scss": "scss",
    "vue": "vue", "svelte": "svelte", "tf": "terraform", "sql": "sql",
    "graphql": "graphql", "gql": "graphql", "md": "markdown",
    "tex": "latex", "elm": "elm", "hs": "haskell", "ml": "ocaml",
    "clj": "clojure", "fs": "fsharp", "r": "r", "toml": "toml",
    "zig": "zig",
}

#: Deliberately not inherited, with the reason. Binary formats: reading
#: one as UTF-8 and shipping it in a didOpen hands the server megabytes
#: of mojibake. cclsp mapped both to "java".
EXCLUDED = {
    "jar": "binary archive — not source text",
    "class": "compiled bytecode — not source text",
}

#: cclsp's symbolKindToString. Ours must agree exactly, because
#: `symbol_kind` is a caller-supplied string matched against these.
CCLSP_KIND_NAMES = {
    1: "file", 2: "module", 3: "namespace", 4: "package", 5: "class",
    6: "method", 7: "property", 8: "field", 9: "constructor", 10: "enum",
    11: "interface", 12: "function", 13: "variable", 14: "constant",
    15: "string", 16: "number", 17: "boolean", 18: "array", 19: "object",
    20: "key", 21: "null", 22: "enum_member", 23: "struct", 24: "event",
    25: "operator", 26: "type_parameter",
}


class LanguageCoverageTests(unittest.TestCase):

    def test_every_cclsp_extension_is_covered_or_excluded(self):
        """The regression this audit exists to prevent.

        An unmapped extension is announced as "plaintext", which most
        servers decline — so a configured server plus an unmapped
        extension yields an accepted document and an empty result, with
        nothing anywhere saying why.
        """
        missing = sorted(
            ext for ext in CCLSP_LANGUAGE_MAP
            if ext not in _LANGUAGE_ID_BY_EXT and ext not in EXCLUDED)
        self.assertEqual(missing, [], f"regressed vs cclsp: {missing}")

    def test_excluded_extensions_stay_excluded(self):
        for ext, why in EXCLUDED.items():
            with self.subTest(ext=ext, reason=why):
                self.assertNotIn(ext, _LANGUAGE_ID_BY_EXT)

    def test_language_ids_agree_where_both_map_an_extension(self):
        """A disagreement is worse than a gap: the server accepts the
        document and analyses it as the wrong language."""
        for ext, lang in CCLSP_LANGUAGE_MAP.items():
            if ext in EXCLUDED or ext not in _LANGUAGE_ID_BY_EXT:
                continue
            with self.subTest(ext=ext):
                self.assertEqual(_LANGUAGE_ID_BY_EXT[ext], lang)

    def test_we_cover_strictly_more(self):
        ours = set(_LANGUAGE_ID_BY_EXT)
        theirs = set(CCLSP_LANGUAGE_MAP) - set(EXCLUDED)
        self.assertTrue(theirs <= ours)
        self.assertGreater(len(ours), len(theirs))

    def test_the_extensions_we_added_beyond_cclsp(self):
        """Named so a future edit removing one is a visible decision."""
        for ext in ("cc", "cxx", "hh", "hxx", "cu", "cuh", "mts", "cts",
                    "pyi", "htm", "jsonc", "less"):
            with self.subTest(ext=ext):
                self.assertIn(ext, _LANGUAGE_ID_BY_EXT)

    def test_case_insensitive_lookup(self):
        self.assertEqual(language_id_for("A.PY"), "python")
        self.assertEqual(language_id_for("X.Java"), "java")

    def test_unknown_extension_is_plaintext_not_an_error(self):
        self.assertEqual(language_id_for("a.wat"), "plaintext")


class SymbolKindParityTests(unittest.TestCase):
    """`symbol_kind` is a caller-supplied string matched against these
    names, so a difference silently filters out every symbol."""

    def test_names_match_cclsp_exactly(self):
        self.assertEqual(SYMBOL_KINDS, CCLSP_KIND_NAMES)

    def test_lookup_agrees(self):
        for num, name in CCLSP_KIND_NAMES.items():
            with self.subTest(kind=num):
                self.assertEqual(symbol_kind_name(num), name)


class ConfigFieldParityTests(unittest.TestCase):
    """cclsp's server-config shape: extensions, command, rootDir,
    restartInterval, initializationOptions. We read all five.

    Silently ignoring a field a config legitimately carries is the
    quiet-mismatch class: it is present, documented, and does nothing.
    """

    def _load(self, tmp: Path, entry: dict):
        import json
        p = tmp / "cclsp.json"
        p.write_text(json.dumps({"servers": [entry]}), encoding="utf-8")
        return load_cclsp_config(p)[0]

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_initialization_options_are_read(self):
        """pylsp's plugin set, jdtls's runtimes and rust-analyzer's cargo
        settings all arrive this way and have no other channel."""
        opts = {"settings": {"pylsp": {"plugins": {"pylint": {"enabled": False}}}}}
        spec = self._load(self.tmp, {
            "extensions": ["py"], "command": ["pylsp"],
            "initializationOptions": opts})
        self.assertEqual(spec.initialization_options, opts)

    def test_restart_interval_is_read(self):
        """cclsp ships restartInterval: 5 for pylsp."""
        spec = self._load(self.tmp, {
            "extensions": ["py"], "command": ["pylsp"], "restartInterval": 5})
        self.assertEqual(spec.restart_interval_minutes, 5.0)

    def test_absent_fields_default_to_off(self):
        spec = self._load(self.tmp, {"extensions": ["py"], "command": ["x"]})
        self.assertIsNone(spec.initialization_options)
        self.assertEqual(spec.restart_interval_minutes, 0.0)

    def test_root_dir_is_read(self):
        spec = self._load(self.tmp, {
            "extensions": ["py"], "command": ["x"], "rootDir": "sub"})
        self.assertEqual(spec.root_dir, "sub")

    def test_malformed_initialization_options_are_rejected(self):
        from claude_hooks.lsp_engine.config import CclspConfigError
        with self.assertRaises(CclspConfigError):
            self._load(self.tmp, {"extensions": ["py"], "command": ["x"],
                                  "initializationOptions": "nope"})

    def test_malformed_restart_interval_is_rejected(self):
        from claude_hooks.lsp_engine.config import CclspConfigError
        with self.assertRaises(CclspConfigError):
            self._load(self.tmp, {"extensions": ["py"], "command": ["x"],
                                  "restartInterval": "soon"})

    def test_negative_restart_interval_is_clamped_to_never(self):
        spec = self._load(self.tmp, {
            "extensions": ["py"], "command": ["x"], "restartInterval": -3})
        self.assertEqual(spec.restart_interval_minutes, 0.0)


class CapabilityParityTests(unittest.TestCase):
    """What the client declares at `initialize`.

    A server only advertises a provider when the client declares the
    matching capability, so this dict *is* the feature set — not
    bookkeeping. cclsp declared several of its own tools' capabilities
    not at all and relied on servers advertising unconditionally.
    """

    def setUp(self):
        self.caps = client_capabilities()
        self.td = self.caps["textDocument"]
        self.ws = self.caps["workspace"]

    def test_everything_cclsp_declared_is_declared_here(self):
        for cap in ("synchronization", "definition", "references", "rename",
                    "documentSymbol", "hover", "diagnostic"):
            with self.subTest(capability=cap):
                self.assertIn(cap, self.td)
        self.assertIn("workspaceEdit", self.ws)
        self.assertIn("workspaceFolders", self.ws)

    def test_capabilities_cclsp_shipped_tools_for_but_never_declared(self):
        """find_implementation, prepare/incoming/outgoing_calls and
        find_workspace_symbols all existed as cclsp tools with no
        matching client capability, so each worked only against servers
        that advertise unconditionally."""
        self.assertIn("implementation", self.td)
        self.assertIn("callHierarchy", self.td)
        self.assertIn("typeDefinition", self.td)
        self.assertIn("symbol", self.ws)

    def test_work_done_progress_is_declared(self):
        """cclsp left this off *and* ignored $/progress, which is why
        'still indexing' and 'dead' were the same message."""
        self.assertTrue(self.caps["window"]["workDoneProgress"])

    def test_link_support_improves_on_cclsp(self):
        """cclsp declared linkSupport:false. True gets LocationLink,
        whose targetSelectionRange points at the name rather than the
        whole definition body."""
        for cap in ("definition", "implementation", "typeDefinition"):
            with self.subTest(capability=cap):
                self.assertTrue(self.td[cap]["linkSupport"])

    def test_prepare_rename_improves_on_cclsp(self):
        """cclsp declared prepareSupport:false, so it could not ask
        whether a symbol was renameable before editing it."""
        self.assertTrue(self.td["rename"]["prepareSupport"])

    def test_full_symbol_kind_value_set_is_offered(self):
        """A server clamps to what the client lists, so an omitted kind
        comes back as a different kind rather than as an error."""
        self.assertEqual(
            set(self.td["documentSymbol"]["symbolKind"]["valueSet"]),
            set(CCLSP_KIND_NAMES))
        self.assertEqual(
            set(self.ws["symbol"]["symbolKind"]["valueSet"]),
            set(CCLSP_KIND_NAMES))

    def test_hierarchical_document_symbols(self):
        """Without it a server may flatten to SymbolInformation, losing
        the nesting that tells Engine.start from Client.start."""
        self.assertTrue(
            self.td["documentSymbol"]["hierarchicalDocumentSymbolSupport"])

    def test_resource_operations_stay_undeclared(self):
        """Deliberate: a server told we can rename files emits file
        operations we do not apply. Not declaring keeps a rename to text
        edits we can actually perform."""
        self.assertNotIn("resourceOperations", self.ws["workspaceEdit"])
        self.assertTrue(self.ws["workspaceEdit"]["documentChanges"])


if __name__ == "__main__":
    unittest.main()
