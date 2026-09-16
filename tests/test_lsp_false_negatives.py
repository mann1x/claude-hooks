"""An empty diagnostic list is not evidence of anything.

From the 2026-09-16 handover (opencoti → claude-hooks): a large C++ tree
answered "No diagnostics found" while every translation unit was in fact
being abandoned at line 1. clangd 11 (Debian bullseye's default) rejects
``-std=gnu++23`` — clang only learned that spelling in 17 — so the file
never parsed. At the tool surface that is indistinguishable from clean
code, and it was acted on as clean for a while.

Three states share one rendering unless something separates them:

    parsed and clean        -> trustworthy silence
    never parsed            -> driver error at 1:1, everything else unknown
    no compile database     -> invented flags; navigation works, diagnostics lie

These tests pin the separation.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from claude_hooks import lang_servers as ls  # noqa: E402
from claude_hooks import lsp_integration as li  # noqa: E402


def _diag(msg, *, line=0, char=0, severity=1, source="clang"):
    return {"line": line, "character": char, "severity": severity,
            "message": msg, "source": source}


class TranslationUnitFailureTests(unittest.TestCase):

    def test_std_rejection_is_a_tu_failure(self):
        """The exact observed message."""
        self.assertTrue(li.is_translation_unit_failure(
            _diag("Invalid value 'gnu++23' in '-std=gnu++23'")))

    def test_missing_include_at_origin_is_a_tu_failure(self):
        self.assertTrue(li.is_translation_unit_failure(
            _diag("'stdio.h' file not found")))

    def test_unknown_argument_is_a_tu_failure(self):
        self.assertTrue(li.is_translation_unit_failure(
            _diag("unknown argument: '-fno-foo'")))

    def test_a_real_finding_at_line_one_is_not_a_tu_failure(self):
        """Position alone must not classify — real code can be wrong on
        its first line, and calling that 'unanalysed' would hide it."""
        self.assertFalse(li.is_translation_unit_failure(
            _diag("expected ';' after top level declarator")))

    def test_a_finding_elsewhere_is_not_a_tu_failure(self):
        self.assertFalse(li.is_translation_unit_failure(
            _diag("Invalid value 'x'", line=42)))

    def test_a_warning_is_not_a_tu_failure(self):
        """Severity matters: a driver *warning* still parsed the file."""
        self.assertFalse(li.is_translation_unit_failure(
            _diag("unknown argument: '-Wfoo'", severity=2)))


class CompileDbTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_missing_db_is_detected_for_cpp(self):
        f = self.root / "a" / "b" / "x.cpp"
        f.parent.mkdir(parents=True)
        f.write_text("int main(){}", encoding="utf-8")
        self.assertTrue(li.missing_compile_db(f))

    def test_db_in_an_ancestor_counts(self):
        f = self.root / "a" / "b" / "x.cpp"
        f.parent.mkdir(parents=True)
        f.write_text("int main(){}", encoding="utf-8")
        (self.root / "a" / "compile_commands.json").write_text("[]",
                                                               encoding="utf-8")
        self.assertFalse(li.missing_compile_db(f))

    def test_compile_flags_txt_also_counts(self):
        f = self.root / "x.c"
        f.write_text("int main(){}", encoding="utf-8")
        (self.root / "compile_flags.txt").write_text("-I.", encoding="utf-8")
        self.assertFalse(li.missing_compile_db(f))

    def test_cuda_is_covered(self):
        """`.cu`/`.cuh` had no server at all until 2026-09-16."""
        f = self.root / "k.cu"
        f.write_text("__global__ void k(){}", encoding="utf-8")
        self.assertTrue(li.missing_compile_db(f))

    def test_python_is_not_subject_to_this(self):
        f = self.root / "x.py"
        f.write_text("pass", encoding="utf-8")
        self.assertFalse(li.missing_compile_db(f))


class BlockRenderingTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _cpp(self, with_db: bool) -> Path:
        f = self.root / "x.cpp"
        f.write_text("int main(){}", encoding="utf-8")
        if with_db:
            (self.root / "compile_commands.json").write_text("[]",
                                                              encoding="utf-8")
        return f

    def test_tu_failure_renders_as_failure_not_as_findings(self):
        block = li.format_diagnostics_block(
            path=self._cpp(True),
            diagnostics=[_diag("Invalid value 'gnu++23' in '-std=gnu++23'")],
            stale=False,
        )
        self.assertIn("FAILED", block)
        self.assertIn("was not analysed", block)
        self.assertIn("gnu++23", block)

    def test_tu_failure_names_the_version_trap(self):
        block = li.format_diagnostics_block(
            path=self._cpp(True),
            diagnostics=[_diag("Invalid value 'gnu++23' in '-std=gnu++23'")],
            stale=False,
        )
        self.assertIn("c++2b", block)

    def test_empty_without_a_compile_db_warns_instead_of_going_silent(self):
        block = li.format_diagnostics_block(
            path=self._cpp(False), diagnostics=[], stale=False)
        self.assertIsNotNone(block)
        self.assertIn("not trustworthy", block)
        self.assertIn("compile_commands.json", block)

    def test_empty_with_a_compile_db_stays_silent(self):
        """Trustworthy silence must remain silent, or the warning is
        noise and gets ignored when it matters."""
        self.assertIsNone(li.format_diagnostics_block(
            path=self._cpp(True), diagnostics=[], stale=False))

    def test_empty_for_python_stays_silent(self):
        f = self.root / "x.py"
        f.write_text("pass", encoding="utf-8")
        self.assertIsNone(li.format_diagnostics_block(
            path=f, diagnostics=[], stale=False))

    def test_ordinary_findings_still_render_normally(self):
        block = li.format_diagnostics_block(
            path=self._cpp(True),
            diagnostics=[_diag("use 'contains'", line=2137, char=38,
                               severity=2, source="clang-tidy")],
            stale=False,
        )
        self.assertIn("LSP diagnostics", block)
        self.assertIn("2138:39", block)          # 0-based -> 1-based
        self.assertNotIn("FAILED", block)


class DeadCommandTests(unittest.TestCase):
    """A pinned path is the standard escape from an ancient distro
    default — and the thing that silently breaks when the pin's binary
    is removed. Observed 2026-09-16: a concurrent toolchain install
    deleted /usr/bin/clangd-16 while two configs still named it.
    """

    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "sync_cclsp_dead", REPO / "scripts" / "sync_cclsp.py")
        self.mod = importlib.util.module_from_spec(spec)
        sys.modules["sync_cclsp_dead"] = self.mod
        spec.loader.exec_module(self.mod)
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_absolute_path_that_exists_resolves(self):
        f = self.dir / "clangd"
        f.write_text("#!/bin/sh\n", encoding="utf-8")
        self.assertTrue(self.mod._command_resolves(str(f)))

    def test_absolute_path_that_vanished_does_not_resolve(self):
        self.assertFalse(
            self.mod._command_resolves(str(self.dir / "clangd-16")))

    def test_bare_name_on_path_resolves(self):
        self.assertTrue(self.mod._command_resolves("sh"))

    def test_bare_name_not_on_path_does_not_resolve(self):
        self.assertFalse(
            self.mod._command_resolves("definitely-not-a-real-binary-xyz"))


class VersionFloorTests(unittest.TestCase):
    """Floors come from measurement, not from "newer is better".

    clang 17 is where `c++23`/`gnu++23` became spellable, so anything
    older abandons a modern TU at line 1. CUDA needs a *higher* floor
    than C++: measured on solidpc 2026-09-16, clangd 19.1.7 emitted 20
    hard errors on a .cu TU that 22.1.6 parses clean — 19 fixed the
    gnu++23 half and still could not read CUDA 13.3 headers.
    """

    def test_clangd_11_is_flagged(self):
        self.assertIn("< 17", ls.version_warning(
            "clangd", "Debian clangd version 11.0.1-2"))

    def test_clangd_16_is_flagged(self):
        """16 was the first fix attempt and is still too old."""
        self.assertIsNotNone(ls.version_warning(
            "clangd", "Debian clangd version 16.0.6"))

    def test_clangd_17_clears_the_general_floor(self):
        self.assertIsNone(ls.version_warning("clangd", "clangd version 17.0.1"))

    def test_clangd_19_clears_cpp_but_not_cuda(self):
        self.assertIsNone(ls.version_warning("clangd", "clangd version 19.1.7"))
        warn = ls.extension_version_warning(
            ("cpp", "cu", "cuh"), "clangd", "clangd version 19.1.7")
        self.assertIsNotNone(warn)
        self.assertIn("v22", warn)

    def test_clangd_22_clears_both(self):
        self.assertIsNone(ls.version_warning("clangd", "clangd version 22.1.6"))
        self.assertIsNone(ls.extension_version_warning(
            ("cpp", "cu"), "clangd", "clangd version 22.1.6"))

    def test_a_server_without_cuda_extensions_is_not_warned(self):
        self.assertIsNone(ls.extension_version_warning(
            ("cpp", "h"), "clangd", "clangd version 19.1.7"))

    def test_unparseable_version_never_warns(self):
        """A probe that failed must not manufacture a verdict."""
        self.assertIsNone(ls.version_warning("clangd", None))
        self.assertIsNone(ls.extension_version_warning(
            ("cu",), "clangd", None))


class CompileDbInBuildDirTests(unittest.TestCase):
    """CMake writes compile_commands.json into the build directory, not
    the source root — the overwhelmingly common layout.

    Searching only the file's own ancestors reported "no compile DB" for
    a correctly-configured project, and that warning says results are
    not trustworthy. Firing it on every CMake project teaches the reader
    to skip it, which costs the honest warnings their meaning. Found by
    scripts/lsp_conformance.py against llama.cpp.
    """

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.src = self.root / "ggml" / "src" / "cuda"
        self.src.mkdir(parents=True)
        self.file = self.src / "acc.cu"
        self.file.write_text("// cuda\n", encoding="utf-8")

    def test_build_subdirectory_counts(self):
        (self.root / "build").mkdir()
        (self.root / "build" / "compile_commands.json").write_text(
            "[]", encoding="utf-8")
        self.assertFalse(li.missing_compile_db(self.file))

    def test_other_common_build_dirs(self):
        for name in ("out", "cmake-build-debug", "cmake-build-release",
                     "builddir", ".build"):
            with self.subTest(dir=name):
                d = self.root / name
                d.mkdir()
                (d / "compile_commands.json").write_text("[]", encoding="utf-8")
                self.assertFalse(li.missing_compile_db(self.file))
                (d / "compile_commands.json").unlink()
                d.rmdir()

    def test_source_root_still_counts(self):
        (self.root / "compile_commands.json").write_text("[]", encoding="utf-8")
        self.assertFalse(li.missing_compile_db(self.file))

    def test_genuinely_absent_is_still_reported(self):
        self.assertTrue(li.missing_compile_db(self.file))

    def test_non_c_files_are_unaffected(self):
        py = self.src / "x.py"
        py.write_text("x = 1\n", encoding="utf-8")
        self.assertFalse(li.missing_compile_db(py))


class DiagnosticDataclassTests(unittest.TestCase):
    """The helper must read a Diagnostic as well as a wire dict.

    The hook path carries dicts; Engine.get_diagnostics returns parsed
    objects. Understanding only dicts meant returning "not a
    translation-unit failure" for one that was.
    """

    def test_dataclass_is_understood(self):
        from claude_hooks.lsp_engine.lsp import Diagnostic
        d = Diagnostic(uri="file:///a.cu", line=0, character=0, severity=1,
                       message="unable to handle compilation",
                       source="clangd", code="drv")
        self.assertTrue(li.is_translation_unit_failure(d))

    def test_dict_still_understood(self):
        self.assertTrue(li.is_translation_unit_failure(
            {"line": 0, "character": 0, "severity": 1,
             "message": "unable to handle compilation"}))

    def test_dataclass_ordinary_error_is_not_a_tu_failure(self):
        from claude_hooks.lsp_engine.lsp import Diagnostic
        d = Diagnostic(uri="file:///a.cu", line=42, character=8, severity=1,
                       message="no member named 'foo'",
                       source="clangd", code="err")
        self.assertFalse(li.is_translation_unit_failure(d))


if __name__ == "__main__":
    unittest.main()
