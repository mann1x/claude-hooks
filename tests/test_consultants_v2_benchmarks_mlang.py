"""Tests for the multi-language coder suite oracle infrastructure
(``benchmarks/consultants/oracles_mlang.py``).

The full suite ships in a follow-up commit. These tests pin the
shared compile-and-run helper now so signature drift can't bite a
future oracle silently (the 2026-05-16 ``coder@1.0`` smoke run
spent 8 trials writing zero useful data on a missed signature —
the protocol now leans on smoke tests like these to catch the
same shape of bug before live spend).
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# Repo root must be on sys.path for the harness imports.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from benchmarks.consultants.oracles_mlang import (  # noqa: E402
    CompileError,
    SOURCE_EXT_BY_LANG,
    SUPPORTED_LANGUAGES,
    compile_and_run,
    probe_toolchain_versions,
    toolchain_required,
)


# ============================================================== #
# Public surface — sanity checks
# ============================================================== #

class TestPublicSurface(unittest.TestCase):

    def test_supported_languages_set(self):
        # The 6 languages the design doc names; tests downstream
        # rely on this exact set.
        self.assertEqual(
            set(SUPPORTED_LANGUAGES),
            {"python", "rust", "go", "c", "cpp", "csharp"},
        )

    def test_source_ext_mapping_complete(self):
        for lang in SUPPORTED_LANGUAGES:
            self.assertIn(lang, SOURCE_EXT_BY_LANG)

    def test_source_ext_values(self):
        # Pin the exact extensions — the SUITE.md ``sandbox_path``
        # field encodes them.
        self.assertEqual(SOURCE_EXT_BY_LANG["python"], ".py")
        self.assertEqual(SOURCE_EXT_BY_LANG["rust"],   ".rs")
        self.assertEqual(SOURCE_EXT_BY_LANG["go"],     ".go")
        self.assertEqual(SOURCE_EXT_BY_LANG["c"],      ".c")
        self.assertEqual(SOURCE_EXT_BY_LANG["cpp"],    ".cpp")
        self.assertEqual(SOURCE_EXT_BY_LANG["csharp"], ".cs")

    def test_toolchain_required_complete(self):
        for lang in SUPPORTED_LANGUAGES:
            self.assertIsInstance(toolchain_required(lang), str)


# ============================================================== #
# Toolchain probing
# ============================================================== #

class TestProbeToolchain(unittest.TestCase):

    def test_returns_dict_with_known_keys(self):
        result = probe_toolchain_versions()
        for key in ("python", "rustc", "go", "gcc", "g++", "dotnet"):
            self.assertIn(key, result)
            # Either a non-empty string (toolchain present) or None
            # (toolchain absent). Never raises.
            v = result[key]
            self.assertTrue(v is None or isinstance(v, str))


# ============================================================== #
# compile_and_run — error paths
# ============================================================== #

class TestCompileAndRunErrorPaths(unittest.TestCase):

    def test_unsupported_lang_raises_value_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "x.py"
            src.write_text("pass\n")
            with self.assertRaises(ValueError) as cx:
                compile_and_run(lang="python", source=src)
            # Python isn't in _COMPILE_BY_LANG (Python oracles
            # import the module directly — different code path).
            self.assertIn("unsupported", str(cx.exception))

    def test_unknown_lang_raises_value_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "x"
            src.write_text("")
            with self.assertRaises(ValueError):
                compile_and_run(lang="bash", source=src)

    def test_missing_source_raises_assertion(self):
        with tempfile.TemporaryDirectory() as tmp:
            absent = Path(tmp) / "nope.rs"
            with self.assertRaises(AssertionError) as cx:
                compile_and_run(lang="rust", source=absent)
            self.assertIn("missing", str(cx.exception))

    def test_compile_error_carries_stderr(self):
        # Rust source that won't compile — missing semicolon /
        # unknown identifier. CompileError must expose stderr.
        if shutil.which("rustc") is None:
            self.skipTest("rustc not on PATH")
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "broken.rs"
            src.write_text("fn main() { let x = ; }\n")
            with self.assertRaises(CompileError) as cx:
                compile_and_run(lang="rust", source=src)
            self.assertIn("rust", str(cx.exception).lower())
            self.assertNotEqual(cx.exception.stderr, "")


# ============================================================== #
# compile_and_run — happy paths (gated on toolchain availability)
# ============================================================== #

class TestCompileAndRunHappyPaths(unittest.TestCase):

    def test_rust_hello_world(self):
        if shutil.which("rustc") is None:
            self.skipTest("rustc not on PATH")
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "hello.rs"
            src.write_text(
                'fn main() { println!("hello mlang"); }\n'
            )
            rc, out, err = compile_and_run(lang="rust", source=src)
            self.assertEqual(rc, 0, f"runtime err: {err}")
            self.assertEqual(out.strip(), "hello mlang")

    def test_rust_reads_stdin(self):
        if shutil.which("rustc") is None:
            self.skipTest("rustc not on PATH")
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "echo.rs"
            src.write_text(
                'use std::io::{self, BufRead};\n'
                'fn main() {\n'
                '    let s = io::stdin().lock().lines().next().unwrap().unwrap();\n'
                '    println!("got: {}", s);\n'
                '}\n'
            )
            rc, out, _ = compile_and_run(
                lang="rust", source=src,
                stdin_input="abc\n",
            )
            self.assertEqual(rc, 0)
            self.assertEqual(out.strip(), "got: abc")

    def test_c_hello_world(self):
        if shutil.which("gcc") is None:
            self.skipTest("gcc not on PATH")
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "h.c"
            src.write_text(
                '#include <stdio.h>\n'
                'int main(void) { printf("c says hi\\n"); return 0; }\n'
            )
            rc, out, _ = compile_and_run(lang="c", source=src)
            self.assertEqual(rc, 0)
            self.assertEqual(out.strip(), "c says hi")

    def test_cpp_hello_world(self):
        if shutil.which("g++") is None:
            self.skipTest("g++ not on PATH")
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "h.cpp"
            src.write_text(
                '#include <iostream>\n'
                'int main() { std::cout << "cpp here\\n"; return 0; }\n'
            )
            rc, out, _ = compile_and_run(lang="cpp", source=src)
            self.assertEqual(rc, 0)
            self.assertEqual(out.strip(), "cpp here")

    def test_go_hello_world(self):
        if shutil.which("go") is None:
            self.skipTest("go not on PATH")
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "h.go"
            src.write_text(
                'package main\n'
                'import "fmt"\n'
                'func main() { fmt.Println("go online") }\n'
            )
            rc, out, _ = compile_and_run(lang="go", source=src)
            self.assertEqual(rc, 0)
            self.assertEqual(out.strip(), "go online")

    def test_runtime_timeout_returns_124(self):
        if shutil.which("rustc") is None:
            self.skipTest("rustc not on PATH")
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "spin.rs"
            src.write_text(
                'fn main() { loop {} }\n'
            )
            rc, _out, err = compile_and_run(
                lang="rust", source=src, timeout_s=2,
            )
            # 124 is the conventional timeout exit code used by the
            # ``timeout`` GNU utility — our helper adopts the same.
            self.assertEqual(rc, 124)
            self.assertIn("timeout", err.lower())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
