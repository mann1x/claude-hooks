"""A boundary warning that fires on complete answers gets ignored.

``describe_scope`` says a reference search stopped at a package root.
That is true and worth saying — but for a symbol nothing outside the
package can import, the narrow search saw every use that exists, and the
warning is attached to a complete answer. Enough of those and a caller
skips it on the answers that are genuinely short.

So the note is suppressed when the declaring file is provably unreachable
from the package's published entry points. Every test here is really one
of two questions: does the proof hold when it says yes, and does it fall
back to the warning for every input it cannot decide? The second set is
the larger one on purpose — a false suppression hides exactly the
incompleteness the note exists to report, while a false warning costs a
paragraph.

The holes are not hypothetical. cline (measured 2026-09-20) has
``'@cline/shared/*': ['./packages/shared/src/*']`` in ``sdk/tsconfig.json``,
so every file in that package is importable by name from a sibling
regardless of its ``exports`` map, and nothing in it can be suppressed.
``MessageWithMetadata`` there is reachable from both ``index.ts`` and
``index.browser.ts`` as well, so it would warn twice over — correctly.
"""
from __future__ import annotations

import json
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from claude_hooks.lsp_engine.config import describe_scope  # noqa: E402
from claude_hooks.lsp_engine.package_exports import (  # noqa: E402
    symbol_is_package_private,
)

EXPORTS = {
    ".": {"types": "./dist/index.d.ts", "import": "./dist/index.js"},
    "./storage": {"import": "./dist/storage/index.js"},
}


class _Fixture(unittest.TestCase):
    """A monorepo package that publishes ``dist`` and compiles ``src``."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name).resolve() / "repo"
        (self.repo / ".git").mkdir(parents=True)
        self.pkg = self.repo / "packages" / "shared"
        self.src = self.pkg / "src"
        self.src.mkdir(parents=True)
        self.manifest(exports=EXPORTS)
        self.tsconfig({"outDir": "./dist", "rootDir": "./src"})
        # A public entry and one module behind it...
        self.write("index.ts", """
            export * from "./public";
            export { Kept } from "./named";
        """)
        self.write("public.ts", 'export const Public = 1;')
        self.write("named.ts", 'export const Kept = 1;\n'
                               'export const AlsoHere = 2;')
        self.write("storage/index.ts", 'export const Store = 1;')
        # ...and one nothing re-exports.
        self.write("internal/helper.ts", 'export class Helper {}')

    # -- fixture helpers ----------------------------------------------

    def manifest(self, **fields) -> None:
        payload = {"name": "@scope/shared", "main": "dist/index.js"}
        payload.update(fields)
        (self.pkg / "package.json").write_text(json.dumps(payload),
                                              encoding="utf-8")

    def tsconfig(self, options: dict, *, where: Path | None = None) -> None:
        (where or self.pkg).mkdir(parents=True, exist_ok=True)
        (where or self.pkg).joinpath("tsconfig.json").write_text(
            "{\n  // a comment, because tsconfig is JSONC\n"
            '  "compilerOptions": ' + json.dumps(options) + ",\n}",
            encoding="utf-8")

    def write(self, rel: str, body: str) -> Path:
        path = self.src / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(body).strip() + "\n", encoding="utf-8")
        return path

    def private(self, rel: str, symbol: str | None) -> bool:
        return symbol_is_package_private(self.pkg, self.src / rel, symbol,
                                         boundary=self.repo)


class ProvedPrivateTests(_Fixture):
    """The cases the whole thing exists for."""

    def test_a_file_no_entry_reexports_is_private(self) -> None:
        self.assertTrue(self.private("internal/helper.ts", "Helper"))

    def test_and_the_scope_note_goes_away(self) -> None:
        note = describe_scope(self.pkg, declared_in=self.src / "internal" /
                              "helper.ts", symbol="Helper")
        self.assertIsNone(note)
        # The same package still warns without the symbol, so this is
        # narrowing the note rather than deleting it.
        self.assertIsNotNone(describe_scope(self.pkg))

    def test_a_symbol_beside_an_exported_one_is_still_public(self) -> None:
        # `named.ts` is reached by `export { Kept } from "./named"`, so
        # the file is in the public graph. `AlsoHere` is not itself
        # re-exported, but proving that needs per-symbol resolution
        # through the chain; file-level reachability is where this stops.
        self.assertFalse(self.private("named.ts", "AlsoHere"))

    def test_a_deep_entry_subpath_is_followed(self) -> None:
        self.assertFalse(self.private("storage/index.ts", "Store"))


class ReachabilityTests(_Fixture):
    """Every way a symbol's file can turn out to be public."""

    def test_export_star_reaches(self) -> None:
        self.assertFalse(self.private("public.ts", "Public"))

    def test_export_star_chains_reach(self) -> None:
        self.write("public.ts", 'export * from "./internal/helper";')
        self.assertFalse(self.private("internal/helper.ts", "Helper"))

    def test_export_type_star_reaches(self) -> None:
        # `export type * from` is real syntax and cline's index.ts uses
        # it; a regex that only knew `export *` would call the target
        # private.
        self.write("public.ts", 'export type * from "./internal/helper";')
        self.assertFalse(self.private("internal/helper.ts", "Helper"))

    def test_an_import_then_bare_export_reaches(self) -> None:
        # A barrel written this way names the module only in an import,
        # so nothing in its export statements points at ./internal/helper.
        self.write("index.ts", """
            import { Helper } from "./internal/helper";
            export { Helper };
        """)
        self.assertFalse(self.private("internal/helper.ts", "Helper"))

    def test_an_import_then_default_export_reaches(self) -> None:
        self.write("index.ts", """
            import { Helper } from "./internal/helper";
            export default Helper;
        """)
        self.assertFalse(self.private("internal/helper.ts", "Helper"))

    def test_an_import_alone_does_not_reach(self) -> None:
        # The distinction that makes the check useful: an entry that
        # *uses* a module without publishing it leaves it private.
        self.write("index.ts", """
            import { Helper } from "./internal/helper";
            export const made = new Helper();
        """)
        self.assertTrue(self.private("internal/helper.ts", "Helper"))

    def test_an_output_extension_specifier_resolves(self) -> None:
        # NodeNext source writes the emitted extension, so `./x.js` is
        # `x.ts` on disk. Failing to resolve it must not read as "the
        # graph ends here".
        self.write("index.ts", 'export * from "./internal/helper.js";')
        self.assertFalse(self.private("internal/helper.ts", "Helper"))

    def test_a_directory_specifier_resolves_to_its_index(self) -> None:
        self.write("index.ts", 'export * from "./internal";')
        self.write("internal/index.ts", 'export * from "./helper";')
        self.assertFalse(self.private("internal/helper.ts", "Helper"))

    def test_the_second_entry_is_searched_too(self) -> None:
        # The MessageWithMetadata shape: private per the main entry,
        # public through another one.
        self.manifest(exports={
            ".": "./dist/index.js", "./browser": "./dist/index.browser.js"})
        self.write("index.browser.ts", 'export * from "./internal/helper";')
        self.assertFalse(self.private("internal/helper.ts", "Helper"))


class BuildOutputTests(_Fixture):
    """An entry must map to a source file, never to the artifact.

    Found on cline's ``apps/cli`` (2026-09-20), which exports
    ``./dist/index.js`` and declares no ``outDir``. The fallback lookup
    found the real, built file — and a bundle has no relative re-exports
    left, so the walk reached nothing and all 55 symbols sampled in that
    package were called private. Every one of them may well be private;
    the point is that the check had stopped measuring anything.
    """

    def _build(self, rel: str = "dist/index.js") -> None:
        out = self.pkg / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("/* bundled: no re-exports survive */\n",
                       encoding="utf-8")

    def test_a_built_artifact_is_not_taken_as_the_entry(self) -> None:
        (self.pkg / "tsconfig.json").unlink()      # no outDir declared
        self._build()
        # `src/index.ts` re-exports ./public, so `public.ts` is public.
        # Reading the bundle instead would have called it private.
        self.assertFalse(self.private("public.ts", "Public"))

    def test_the_src_rebase_finds_the_real_entry(self) -> None:
        (self.pkg / "tsconfig.json").unlink()
        self._build()
        self.assertTrue(self.private("internal/helper.ts", "Helper"))

    def test_output_with_no_matching_source_bails(self) -> None:
        # Nothing to rebase onto: the answer is "cannot tell", not the
        # artifact.
        (self.pkg / "tsconfig.json").unlink()
        self.manifest(exports={".": "./build/main.js"})
        (self.pkg / "build").mkdir()
        (self.pkg / "build" / "main.js").write_text("x\n", encoding="utf-8")
        self.assertFalse(self.private("internal/helper.ts", "Helper"))

    def test_a_declared_outdir_still_wins(self) -> None:
        self._build()
        self.assertFalse(self.private("public.ts", "Public"))
        self.assertTrue(self.private("internal/helper.ts", "Helper"))

    def test_a_source_published_entry_is_not_output(self) -> None:
        # cline's cline-hub exports "./src/server.ts" directly.
        self.manifest(exports={".": "./src/index.ts"})
        self.assertFalse(self.private("public.ts", "Public"))

    def test_a_source_under_a_declared_outdir_is_refused(self) -> None:
        # A package whose outDir *is* its source dir is misconfigured;
        # refuse rather than walk output.
        self.tsconfig({"outDir": "./src", "rootDir": "./src"})
        self.manifest(exports={".": "./src/index.js"})
        self.assertFalse(self.private("internal/helper.ts", "Helper"))


class UnsureKeepsTheWarningTests(_Fixture):
    """Each of these could be public; none may be suppressed."""

    def test_no_package_json(self) -> None:
        (self.pkg / "package.json").unlink()
        self.assertFalse(self.private("internal/helper.ts", "Helper"))

    def test_unparseable_package_json(self) -> None:
        (self.pkg / "package.json").write_text("{ not json",
                                               encoding="utf-8")
        self.assertFalse(self.private("internal/helper.ts", "Helper"))

    def test_no_exports_map(self) -> None:
        # `main` with no `exports` permits any deep import, so the entry
        # graph describes nothing.
        self.manifest()
        self.assertFalse(self.private("internal/helper.ts", "Helper"))

    def test_a_wildcard_subpath(self) -> None:
        self.manifest(exports={".": "./dist/index.js",
                               "./*": "./dist/*.js"})
        self.assertFalse(self.private("internal/helper.ts", "Helper"))

    def test_a_wildcard_target(self) -> None:
        self.manifest(exports={".": "./dist/index.js",
                               "./deep": "./dist/*"})
        self.assertFalse(self.private("internal/helper.ts", "Helper"))

    def test_types_versions(self) -> None:
        self.manifest(exports=EXPORTS,
                      typesVersions={"*": {"*": ["dist/*"]}})
        self.assertFalse(self.private("internal/helper.ts", "Helper"))

    def test_a_tsconfig_paths_alias_into_the_sources(self) -> None:
        # cline's shape, and the reason `exports` alone is not enough:
        # the compiler resolves the alias and never consults `exports`.
        self.tsconfig({"baseUrl": ".", "paths": {
            "@scope/shared/*": ["./packages/shared/src/*"]}},
            where=self.repo)
        self.assertFalse(self.private("internal/helper.ts", "Helper"))

    def test_an_alias_at_another_package_is_not_ours(self) -> None:
        # No false positives in the hole check either, or one aliased
        # package in a repo would silence the whole repo.
        self.tsconfig({"baseUrl": ".", "paths": {
            "@scope/other/*": ["./packages/other/src/*"]}},
            where=self.repo)
        self.assertTrue(self.private("internal/helper.ts", "Helper"))

    def test_an_unmappable_entry_target(self) -> None:
        # An entry whose source cannot be found means the graph has a
        # root we never walked.
        self.manifest(exports={".": "./dist/index.js",
                               "./gone": "./dist/not-built/index.js"})
        self.assertFalse(self.private("internal/helper.ts", "Helper"))

    def test_a_missing_outdir_uses_the_src_convention(self) -> None:
        # This asserted False when written, on the reasoning that an
        # undeclared outDir leaves the entry unaccounted for. It was
        # measuring the wrong thing: the lookup was in fact finding the
        # *built* `dist/index.js` (see BuildOutputTests), and only the
        # fixture's missing `dist` made that look like a clean bail. The
        # `dist` -> `src` rebase now finds the real entry, so the graph
        # is walked and the answer is a real one.
        (self.pkg / "tsconfig.json").unlink()
        self.assertTrue(self.private("internal/helper.ts", "Helper"))
        self.assertFalse(self.private("public.ts", "Public"))

    def test_an_unresolvable_relative_reexport(self) -> None:
        self.write("index.ts", 'export * from "./deleted-module";')
        self.assertFalse(self.private("internal/helper.ts", "Helper"))

    def test_a_reexport_leaving_the_package(self) -> None:
        self.write("index.ts", 'export * from "../../other/src/thing";')
        (self.repo / "packages" / "other" / "src").mkdir(parents=True)
        (self.repo / "packages" / "other" / "src" / "thing.ts").write_text(
            "export const T = 1;\n", encoding="utf-8")
        self.assertFalse(self.private("internal/helper.ts", "Helper"))

    def test_a_self_referencing_specifier(self) -> None:
        self.write("index.ts", 'export * from "@scope/shared/storage";')
        self.assertFalse(self.private("internal/helper.ts", "Helper"))

    def test_an_imports_map_specifier(self) -> None:
        self.write("index.ts", 'export * from "#internal/helper";')
        self.assertFalse(self.private("internal/helper.ts", "Helper"))

    def test_a_symbol_the_file_imports(self) -> None:
        # The declaration is elsewhere, so this file being private says
        # nothing about the symbol. Without this, querying a private
        # file for a public symbol would suppress the warning.
        self.write("internal/helper.ts", """
            import { Public } from "../public";
            export const used = Public;
        """)
        self.assertFalse(self.private("internal/helper.ts", "Public"))

    def test_a_symbol_the_file_reexports_from_elsewhere(self) -> None:
        self.write("internal/helper.ts",
                   'export { Public } from "../public";')
        self.assertFalse(self.private("internal/helper.ts", "Public"))

    def test_a_file_with_an_export_star_of_its_own(self) -> None:
        # It could be re-exporting the symbol from anywhere.
        self.write("internal/helper.ts", 'export * from "../public";')
        self.assertFalse(self.private("internal/helper.ts", "Public"))

    def test_no_symbol_name(self) -> None:
        # The position-addressed tools: without a name the
        # "is it imported here" guard cannot run.
        self.assertFalse(self.private("internal/helper.ts", None))
        self.assertFalse(self.private("internal/helper.ts", "   "))

    def test_a_file_outside_the_package(self) -> None:
        outside = self.repo / "packages" / "other" / "src"
        outside.mkdir(parents=True)
        (outside / "x.ts").write_text("export class Helper {}\n",
                                      encoding="utf-8")
        self.assertFalse(symbol_is_package_private(
            self.pkg, outside / "x.ts", "Helper", boundary=self.repo))

    def test_a_file_that_does_not_exist(self) -> None:
        self.assertFalse(self.private("internal/gone.ts", "Helper"))

    def test_a_non_source_file(self) -> None:
        (self.src / "data.json").write_text("{}", encoding="utf-8")
        self.assertFalse(self.private("data.json", "Helper"))

    def test_a_graph_too_big_to_walk(self) -> None:
        from claude_hooks.lsp_engine import package_exports as PE
        chain = "\n".join(
            f'export * from "./gen/m{i}";' for i in range(PE._MAX_FILES + 10))
        self.write("index.ts", chain)
        for i in range(PE._MAX_FILES + 10):
            self.write(f"gen/m{i}.ts", f"export const v{i} = {i};")
        self.assertFalse(self.private("internal/helper.ts", "Helper"))


class NeverRaisesTests(_Fixture):
    """This runs inside a nav response; it may not fail one."""

    def test_binary_sources_are_survived(self) -> None:
        (self.src / "index.ts").write_bytes(b"\xff\xfe\x00export *")
        self.assertIsInstance(self.private("internal/helper.ts", "Helper"),
                              bool)

    def test_a_directory_passed_as_a_file(self) -> None:
        self.assertFalse(symbol_is_package_private(
            self.pkg, self.src, "Helper", boundary=self.repo))

    def test_garbage_arguments(self) -> None:
        self.assertFalse(symbol_is_package_private(
            Path("/nonexistent"), Path("/nonexistent/x.ts"), "X"))

    def test_an_unparseable_tsconfig_keeps_the_warning(self) -> None:
        # Cannot tell whether there is an alias, so assume there is.
        (self.repo / "tsconfig.json").write_text("{ broken",
                                                 encoding="utf-8")
        self.assertFalse(self.private("internal/helper.ts", "Helper"))


class DescribeScopeIntegrationTests(_Fixture):
    """The suppression must not disturb the note's other answers."""

    def test_a_declared_root_is_still_silent(self) -> None:
        from claude_hooks.lsp_engine.config import ROOT_SENTINEL
        sentinel = self.pkg / ROOT_SENTINEL
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text("", encoding="utf-8")
        self.assertIsNone(describe_scope(
            self.pkg, declared_in=self.src / "public.ts", symbol="Public"))

    def test_a_repo_root_is_still_silent(self) -> None:
        self.assertIsNone(describe_scope(
            self.repo, declared_in=self.src / "public.ts", symbol="Public"))

    def test_a_public_symbol_still_warns_in_full(self) -> None:
        note = describe_scope(self.pkg, declared_in=self.src / "public.ts",
                              symbol="Public")
        self.assertIsNotNone(note)
        assert note is not None
        self.assertIn("Sibling packages were not searched", note)

    def test_the_old_signature_is_unchanged(self) -> None:
        # Every other caller passes a root alone and must keep warning.
        self.assertIsNotNone(describe_scope(self.pkg))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
