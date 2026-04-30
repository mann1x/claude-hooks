"""Tests for the opt-in compile-aware layer.

Three layers:

- Parser unit tests against deterministic fixture strings.
- ``CompileRunner`` debouncing + failure-mode tests with synthetic
  Python -c commands so we don't need cargo/tsc on the test host.
- ``CompileOrchestrator`` routing by extension.

End-to-end through the daemon lives in test_lsp_engine_daemon.py.
"""

from __future__ import annotations

import json
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from claude_hooks.lsp_engine.compile import (
    CompileOrchestrator,
    CompileRunner,
    CompileSpec,
    parse_cargo_json_output,
    parse_text_output,
)


class TestParseTextOutput(unittest.TestCase):
    def test_basic_error_line(self) -> None:
        out = parse_text_output(
            "foo.py:5:10: error: bad name",
            project_root="/proj",
        )
        self.assertEqual(len(out), 1)
        d = out[0]
        self.assertTrue(d.uri.startswith("file://"))
        self.assertIn("foo.py", d.uri)
        self.assertEqual(d.severity, 1)
        self.assertEqual(d.line, 4)  # 1-indexed → 0-indexed
        self.assertEqual(d.character, 9)
        self.assertEqual(d.message, "bad name")

    def test_severity_mapping(self) -> None:
        text = (
            "a.py:1:1: error: e\n"
            "a.py:2:1: warning: w\n"
            "a.py:3:1: note: n\n"
            "a.py:4:1: hint: h\n"
        )
        out = parse_text_output(text, project_root="/proj")
        sevs = [d.severity for d in out]
        # error=1, warning=2, note=3 (info), hint=4.
        self.assertEqual(sevs, [1, 2, 3, 4])

    def test_unmatchable_lines_dropped(self) -> None:
        text = (
            "Compiling project...\n"
            "foo.py:5:10: error: real diag\n"
            "Done\n"
        )
        out = parse_text_output(text, project_root="/proj")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].message, "real diag")

    def test_relative_path_resolves_against_root(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            out = parse_text_output(
                "src/lib.py:1:1: error: x", project_root=root,
            )
            self.assertEqual(len(out), 1)
            self.assertIn(str(root / "src" / "lib.py"), out[0].uri.replace("file://", ""))

    def test_absolute_path_preserved(self) -> None:
        out = parse_text_output(
            "/abs/path.py:1:1: error: x", project_root="/proj",
        )
        self.assertEqual(len(out), 1)
        self.assertIn("/abs/path.py", out[0].uri)

    def test_empty_input_returns_empty(self) -> None:
        self.assertEqual(parse_text_output("", project_root="/proj"), [])

    def test_default_severity_when_label_missing(self) -> None:
        # Some compilers omit the severity label. Default to error.
        out = parse_text_output(
            "foo.py:1:1: something happened", project_root="/proj",
        )
        # The regex requires either a severity label or no label —
        # without a colon between path:line:col and the message, this
        # still parses but defaults severity to error.
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].severity, 1)


class TestParseCargoJsonOutput(unittest.TestCase):
    def _msg(self, *, level: str = "error", message: str = "boom",
             file_name: str = "src/lib.rs", line: int = 5, col: int = 10,
             code: str | None = "E0308", primary: bool = True) -> str:
        msg = {
            "reason": "compiler-message",
            "message": {
                "level": level,
                "message": message,
                "code": {"code": code} if code else None,
                "spans": [{
                    "file_name": file_name,
                    "line_start": line,
                    "column_start": col,
                    "is_primary": primary,
                }],
            },
        }
        return json.dumps(msg)

    def test_basic_cargo_message(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = parse_cargo_json_output(self._msg(), project_root=root)
        self.assertEqual(len(out), 1)
        d = out[0]
        self.assertEqual(d.severity, 1)
        self.assertEqual(d.line, 4)
        self.assertEqual(d.character, 9)
        self.assertEqual(d.message, "boom")
        self.assertEqual(d.code, "E0308")
        self.assertEqual(d.source, "cargo")

    def test_skips_non_primary_spans(self) -> None:
        out = parse_cargo_json_output(
            self._msg(primary=False), project_root="/proj",
        )
        self.assertEqual(out, [])

    def test_skips_non_compiler_messages(self) -> None:
        bait = json.dumps({"reason": "build-script-executed", "package_id": "foo"})
        out = parse_cargo_json_output(
            bait + "\n" + self._msg(), project_root="/proj",
        )
        self.assertEqual(len(out), 1)

    def test_invalid_json_lines_dropped(self) -> None:
        out = parse_cargo_json_output(
            "not json\n" + self._msg() + "\nalso not json",
            project_root="/proj",
        )
        self.assertEqual(len(out), 1)

    def test_warning_level(self) -> None:
        out = parse_cargo_json_output(
            self._msg(level="warning"), project_root="/proj",
        )
        self.assertEqual(out[0].severity, 2)


class TestCompileRunnerExecution(unittest.TestCase):
    """Minimal subprocess executions via ``python -c`` so the tests
    don't depend on cargo/tsc being installed."""

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _runner(self, command: tuple[str, ...], *,
                debounce: float = 0.05) -> CompileRunner:
        spec = CompileSpec(
            language="rs",
            command=command,
            debounce_seconds=debounce,
            run_timeout_s=5.0,
        )
        return CompileRunner(spec=spec, project_root=self.root)

    def test_text_output_round_trip(self) -> None:
        cmd = (
            sys.executable, "-c",
            "print('foo.rs:3:5: error: oh no')",
        )
        runner = self._runner(cmd, debounce=0.05)
        runner.start()
        try:
            runner.trigger()
            # Wait for the run to finish.
            self._wait_for_run(runner, deadline=time.monotonic() + 3.0)
            # Returncode 0 from the python -c.
            self.assertEqual(runner.last_returncode, 0)
            diags = runner.get_diagnostics(str(self.root / "foo.rs"))
            self.assertEqual(len(diags), 1)
            self.assertEqual(diags[0].message, "oh no")
        finally:
            runner.stop()

    def test_cargo_json_auto_detection(self) -> None:
        cargo_msg = json.dumps({
            "reason": "compiler-message",
            "message": {
                "level": "error",
                "message": "borrow checker blew up",
                "code": {"code": "E0599"},
                "spans": [{
                    "file_name": "src/lib.rs",
                    "line_start": 1,
                    "column_start": 1,
                    "is_primary": True,
                }],
            },
        })
        cmd = (
            sys.executable, "-c",
            f"print({cargo_msg!r})",
            "--message-format=json",  # triggers parser auto-detect
        )
        runner = self._runner(cmd, debounce=0.05)
        runner.start()
        try:
            runner.trigger()
            self._wait_for_run(runner, deadline=time.monotonic() + 3.0)
            diags = runner.get_diagnostics(str((self.root / "src/lib.rs").resolve()))
            self.assertEqual(len(diags), 1)
            self.assertEqual(diags[0].source, "python")  # script name as source
            self.assertEqual(diags[0].message, "borrow checker blew up")
        finally:
            runner.stop()

    def test_debounce_coalesces_rapid_triggers(self) -> None:
        counter = self.root / "runs.txt"
        counter.write_text("")
        cmd = (
            sys.executable, "-c",
            f"open({str(counter)!r}, 'a').write('x')",
        )
        runner = self._runner(cmd, debounce=0.20)
        runner.start()
        try:
            for _ in range(5):
                runner.trigger()
                time.sleep(0.02)  # well under the debounce window
            # Wait beyond the debounce window for the single run.
            time.sleep(0.50)
            # Exactly one run should have happened — the rapid
            # triggers coalesced into the debounce.
            self.assertEqual(counter.read_text(), "x")
        finally:
            runner.stop()

    def test_missing_binary_does_not_crash(self) -> None:
        runner = self._runner(("/no/such/binary",), debounce=0.05)
        runner.start()
        try:
            runner.trigger()
            time.sleep(0.40)
            # No diagnostics, runner thread still alive (no exception
            # propagated back). last_returncode stays None because we
            # never got far enough to record one.
            self.assertEqual(runner.all_diagnostics(), {})
        finally:
            runner.stop()

    def test_timeout_does_not_crash(self) -> None:
        cmd = (
            sys.executable, "-c",
            "import time; time.sleep(10)",
        )
        spec = CompileSpec(
            language="rs",
            command=cmd,
            debounce_seconds=0.05,
            run_timeout_s=0.20,
        )
        runner = CompileRunner(spec=spec, project_root=self.root)
        runner.start()
        try:
            runner.trigger()
            time.sleep(0.80)  # beyond debounce + timeout
            self.assertEqual(runner.all_diagnostics(), {})
        finally:
            runner.stop()

    def _wait_for_run(self, runner: CompileRunner, *, deadline: float) -> None:
        while time.monotonic() < deadline:
            if runner.last_returncode is not None:
                return
            time.sleep(0.020)
        raise AssertionError("runner did not complete in time")


class TestCompileOrchestratorRouting(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.rs_counter = self.root / "rs.txt"
        self.ts_counter = self.root / "ts.txt"
        self.rs_counter.write_text("")
        self.ts_counter.write_text("")

        rs_spec = CompileSpec(
            language="rs",
            command=(
                sys.executable, "-c",
                f"open({str(self.rs_counter)!r}, 'a').write('r')",
            ),
            debounce_seconds=0.05,
            run_timeout_s=2.0,
        )
        ts_spec = CompileSpec(
            language="ts",
            command=(
                sys.executable, "-c",
                f"open({str(self.ts_counter)!r}, 'a').write('t')",
            ),
            debounce_seconds=0.05,
            run_timeout_s=2.0,
        )
        self.orch = CompileOrchestrator(self.root, [rs_spec, ts_spec])
        self.orch.start()
        self.addCleanup(self.orch.stop)

    def test_extension_routes_to_correct_runner(self) -> None:
        self.orch.notify_change(self.root / "x.rs")
        time.sleep(0.40)
        self.assertEqual(self.rs_counter.read_text(), "r")
        self.assertEqual(self.ts_counter.read_text(), "")

    def test_unknown_extension_triggers_no_runner(self) -> None:
        self.orch.notify_change(self.root / "README.md")
        time.sleep(0.40)
        self.assertEqual(self.rs_counter.read_text(), "")
        self.assertEqual(self.ts_counter.read_text(), "")

    def test_concurrent_languages_run_independently(self) -> None:
        self.orch.notify_change(self.root / "a.rs")
        self.orch.notify_change(self.root / "b.ts")
        time.sleep(0.50)
        self.assertEqual(self.rs_counter.read_text(), "r")
        self.assertEqual(self.ts_counter.read_text(), "t")


class TestCompileOrchestratorFromConfig(unittest.TestCase):
    def test_from_engine_config_builds_specs(self) -> None:
        with TemporaryDirectory() as tmp:
            orch = CompileOrchestrator.from_engine_config(
                tmp,
                {
                    "rs": ("cargo", "check", "--message-format=json"),
                    "ts": ("tsc", "--noEmit"),
                },
                debounce_seconds=0.5,
            )
            runners = orch.runners()
            self.assertEqual(set(runners.keys()), {"rs", "ts"})
            self.assertEqual(runners["rs"].spec.debounce_seconds, 0.5)
            # Auto-detect picked cargo-json for the rs spec.
            self.assertEqual(runners["rs"].spec.resolve_parser(), "cargo-json")
            # Default text parser for tsc.
            self.assertEqual(runners["ts"].spec.resolve_parser(), "text")


if __name__ == "__main__":
    unittest.main()
