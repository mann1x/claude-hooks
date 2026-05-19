"""Tests for :mod:`consultants.engine.coder` and the M10 state /
graph / planner extensions.

Layers covered:

1. **Dataclasses** — ``CoderTaskItem`` / ``CoderArtifact`` shapes +
   the round-filtering helper.
2. **Sandbox path normalisation + caps** — every guard arm tested:
   absolute paths, traversal, empty segments, per-file cap,
   per-total cap, per-file-count cap, successful writes update
   counters + audit list.
3. **Prompt builder** — system + user message ordering, research
   findings inlined, sandbox-cap block rendered, optional path /
   why blocks present only when supplied.
4. **Node behavior** — happy path returns a populated
   ``CoderArtifact``; missing task item tombstones; loop exception
   tombstones; recorder + event hooks fire.
5. **Planner-output parsers** — ``parse_coder_preamble`` and
   ``parse_coder_tasks`` cover fenced/bare JSON, missing keys,
   malformed inputs, empty inputs.
6. **Synthesizer integration** — ``build_coder_artifacts_block``
   renders artifacts and tombstones distinctly;
   ``build_synthesizer_messages`` surfaces the block when the
   channel is non-empty.
7. **Planner gate** — ``planner_node(coder_enabled=True)`` extends
   its system prompt with the gate block; success returns include
   the parsed declaration.

All tests use stubs so they run on the main ``claude-hooks`` env
without langgraph.
"""

from __future__ import annotations

import operator
import os
import unittest
from pathlib import Path
from unittest import mock

from consultants.engine.coder import (
    CODER_SYSTEM,
    CODER_WRITE_FILE_TOOL_SPEC,
    CoderSandbox,
    PLANNER_CODER_GATE_BLOCK,
    _normalise_sandbox_path,
    build_coder_artifacts_block,
    build_coder_messages,
    coder_node,
    make_coder_sandbox,
    make_sandbox_tool_executor,
    parse_coder_preamble,
    parse_coder_tasks,
    sandbox_root_for,
)
from consultants.engine.state_v2 import (
    CoderArtifact,
    CoderTaskItem,
    coder_artifacts_for_round,
)


# ============================================================== #
# Dataclasses
# ============================================================== #

class TestCoderTaskItem(unittest.TestCase):

    def test_minimal_construction(self):
        item = CoderTaskItem(task="Write foo()")
        self.assertEqual(item.task, "Write foo()")
        self.assertEqual(item.path, "")
        self.assertEqual(item.why, "")
        self.assertIsNone(item.lane_idx)
        self.assertEqual(item.parent_round, 1)

    def test_full_construction(self):
        item = CoderTaskItem(
            task="Write parse_iso8601",
            path="iso8601.py",
            why="user asked for date parsing",
            lane_idx=2,
            parent_round=1,
        )
        self.assertEqual(item.path, "iso8601.py")
        self.assertEqual(item.lane_idx, 2)

    def test_frozen(self):
        item = CoderTaskItem(task="x")
        with self.assertRaises(Exception):
            item.task = "y"  # type: ignore


class TestCoderArtifact(unittest.TestCase):

    def test_minimal_construction(self):
        a = CoderArtifact(task="x")
        self.assertEqual(a.task, "x")
        self.assertEqual(a.summary, "")
        self.assertEqual(a.files, [])
        self.assertIsNone(a.error)

    def test_tombstone_shape(self):
        a = CoderArtifact(
            task="audit", error="RuntimeError: boom",
            lane_idx=2, parent_round=1,
        )
        self.assertEqual(a.error, "RuntimeError: boom")
        self.assertEqual(a.files, [])


# ============================================================== #
# coder_artifacts_for_round
# ============================================================== #

class TestCoderArtifactsForRound(unittest.TestCase):

    def test_filters_by_round(self):
        state = {
            "coder_artifacts": [
                CoderArtifact(task="r1-a", parent_round=1),
                CoderArtifact(task="r2-a", parent_round=2),
                CoderArtifact(task="r1-b", parent_round=1),
            ],
        }
        r1 = coder_artifacts_for_round(state, 1)
        self.assertEqual([a.task for a in r1], ["r1-a", "r1-b"])
        r2 = coder_artifacts_for_round(state, 2)
        self.assertEqual([a.task for a in r2], ["r2-a"])

    def test_empty_state(self):
        self.assertEqual(coder_artifacts_for_round({}, 1), [])

    def test_missing_parent_round_treated_as_1(self):
        # Defensive against future shape drift — the helper falls
        # back to round 1 when parent_round is missing.
        class _RawArtifact:
            task = "x"
        state = {"coder_artifacts": [_RawArtifact()]}
        # Should not raise; should filter as round 1.
        out = coder_artifacts_for_round(state, 1)
        self.assertEqual(len(out), 1)


# ============================================================== #
# Sandbox path normalisation
# ============================================================== #

class TestNormaliseSandboxPath(unittest.TestCase):

    def test_simple_path(self):
        self.assertEqual(_normalise_sandbox_path("foo.py"), "foo.py")

    def test_nested_path(self):
        self.assertEqual(
            _normalise_sandbox_path("src/foo/bar.py"),
            "src/foo/bar.py",
        )

    def test_windows_separators_normalised(self):
        self.assertEqual(
            _normalise_sandbox_path("src\\foo\\bar.py"),
            "src/foo/bar.py",
        )

    def test_leading_dot_stripped(self):
        self.assertEqual(
            _normalise_sandbox_path("./foo.py"), "foo.py",
        )

    def test_absolute_path_rejected(self):
        with self.assertRaises(ValueError):
            _normalise_sandbox_path("/etc/passwd")

    def test_traversal_rejected(self):
        with self.assertRaises(ValueError):
            _normalise_sandbox_path("../escape.py")
        with self.assertRaises(ValueError):
            _normalise_sandbox_path("foo/../bar")

    def test_empty_rejected(self):
        with self.assertRaises(ValueError):
            _normalise_sandbox_path("")
        with self.assertRaises(ValueError):
            _normalise_sandbox_path("   ")

    def test_null_byte_rejected(self):
        with self.assertRaises(ValueError):
            _normalise_sandbox_path("foo\x00.py")


# ============================================================== #
# CoderSandbox — caps + write atomicity
# ============================================================== #

class TestCoderSandbox(unittest.TestCase):

    def _make(self, tmpdir, **kw):
        defaults = {
            "max_file_bytes": 1024,
            "max_total_bytes": 4096,
            "max_files": 4,
        }
        defaults.update(kw)
        return CoderSandbox(root=Path(tmpdir), **defaults)

    def test_write_success_updates_counters(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sb = self._make(tmp)
            msg = sb.write("hello.txt", "world")
            self.assertIn("hello.txt", msg)
            self.assertEqual(sb.bytes_written, 5)
            self.assertEqual(len(sb.writes), 1)
            entry = sb.writes[0]
            self.assertEqual(entry["path"], "hello.txt")
            self.assertEqual(entry["bytes"], 5)
            self.assertEqual(len(entry["sha256"]), 64)
            # File actually landed on disk.
            with open(Path(tmp) / "hello.txt", "rb") as f:
                self.assertEqual(f.read(), b"world")

    def test_nested_path_creates_parents(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sb = self._make(tmp)
            sb.write("a/b/c/d.txt", "x")
            self.assertTrue((Path(tmp) / "a/b/c/d.txt").exists())

    def test_per_file_cap_rejects(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sb = self._make(tmp, max_file_bytes=10)
            with self.assertRaises(ValueError) as cm:
                sb.write("big.txt", "x" * 11)
            self.assertIn("per-file cap", str(cm.exception))
            self.assertEqual(sb.bytes_written, 0)
            self.assertEqual(len(sb.writes), 0)
            self.assertEqual(len(sb.rejections), 1)

    def test_per_total_cap_rejects(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sb = self._make(tmp, max_total_bytes=10)
            sb.write("a.txt", "12345")
            sb.write("b.txt", "12345")
            with self.assertRaises(ValueError) as cm:
                sb.write("c.txt", "1")
            self.assertIn("exceeding cap", str(cm.exception))
            # First two writes succeeded.
            self.assertEqual(sb.bytes_written, 10)
            self.assertEqual(len(sb.writes), 2)

    def test_per_file_count_cap_rejects(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sb = self._make(tmp, max_files=2)
            sb.write("a.txt", "1")
            sb.write("b.txt", "1")
            with self.assertRaises(ValueError) as cm:
                sb.write("c.txt", "1")
            self.assertIn("max_files cap", str(cm.exception))

    def test_traversal_rejected(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sb = self._make(tmp)
            with self.assertRaises(ValueError):
                sb.write("../escape.txt", "x")
            self.assertFalse(
                (Path(tmp).parent / "escape.txt").exists(),
            )

    def test_absolute_path_rejected(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sb = self._make(tmp)
            with self.assertRaises(ValueError):
                sb.write("/etc/passwd", "x")

    def test_overwrite_existing_file(self):
        # A coder lane may revise an earlier file on its second
        # iteration; that's fine — we count the new bytes (not the
        # diff) because the cap is about resource consumption, not
        # net bytes.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sb = self._make(tmp, max_total_bytes=20)
            sb.write("x.txt", "first")
            sb.write("x.txt", "second-attempt")
            self.assertEqual(sb.bytes_written, 5 + 14)
            with open(Path(tmp) / "x.txt", "rb") as f:
                self.assertEqual(f.read(), b"second-attempt")


# ============================================================== #
# make_sandbox_tool_executor
# ============================================================== #

class TestSandboxToolExecutor(unittest.TestCase):

    def _exec(self, sb, name, args):
        return make_sandbox_tool_executor(sb)(name, args)

    def test_write_file_success(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sb = CoderSandbox(
                root=Path(tmp), max_file_bytes=1024,
                max_total_bytes=1024, max_files=4,
            )
            out = self._exec(sb, "write_file",
                             '{"path": "x.txt", "content": "hi"}')
            self.assertIn("wrote x.txt", out)
            self.assertEqual(len(sb.writes), 1)

    def test_unknown_tool_returns_error_string(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sb = CoderSandbox(
                root=Path(tmp), max_file_bytes=1024,
                max_total_bytes=1024, max_files=4,
            )
            out = self._exec(sb, "read_file", '{"path": "x"}')
            self.assertIn("not available to the coder", out)
            self.assertEqual(len(sb.writes), 0)

    def test_invalid_json_returns_error(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sb = CoderSandbox(
                root=Path(tmp), max_file_bytes=1024,
                max_total_bytes=1024, max_files=4,
            )
            out = self._exec(sb, "write_file", "{not valid json")
            self.assertIn("invalid JSON", out)

    def test_missing_args_returns_error(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sb = CoderSandbox(
                root=Path(tmp), max_file_bytes=1024,
                max_total_bytes=1024, max_files=4,
            )
            out = self._exec(sb, "write_file", '{"content": "hi"}')
            self.assertIn("requires", out)

    def test_cap_violation_returned_as_error_string(self):
        # Tool executor doesn't propagate the ValueError — it wraps
        # it into the OpenAI tool-result error shape so the LLM
        # sees the error inline and can self-correct.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sb = CoderSandbox(
                root=Path(tmp), max_file_bytes=4,
                max_total_bytes=1024, max_files=4,
            )
            out = self._exec(sb, "write_file",
                             '{"path": "x.txt", "content": "12345"}')
            self.assertIn("error", out)
            self.assertIn("per-file cap", out)

    def test_signature_matches_run_loop_three_positional(self):
        # Regression: ``run_loop`` in
        # ``claude_hooks/agent_loop/runner.py`` calls the executor
        # as ``tool_executor(name, args_str, cwd)`` — 3 positional
        # args. The 2026-05-16 smoke run failed every coder trial
        # because the executor only accepted ``(name, args)``. Pin
        # the 3-positional contract so this can't recur.
        import inspect
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sb = CoderSandbox(
                root=Path(tmp), max_file_bytes=1024,
                max_total_bytes=1024, max_files=4,
            )
            executor = make_sandbox_tool_executor(sb)
            sig = inspect.signature(executor)
            # First three positional names should be name/args/cwd
            # (kw names are advisory but stable).
            param_names = list(sig.parameters.keys())
            self.assertGreaterEqual(len(param_names), 3,
                f"executor must accept >= 3 positional args, "
                f"got {param_names!r}")
            # Exercise the 3-positional call shape end-to-end.
            out = executor("write_file",
                           '{"path": "x.txt", "content": "hi"}',
                           "/some/cwd")
            self.assertIn("wrote x.txt", out)
            self.assertEqual(len(sb.writes), 1)

    def test_signature_accepts_runner_kwargs(self):
        # The real ``run_loop`` doesn't currently pass extra kwargs
        # but the executor should tolerate them via ``**kw`` to
        # remain forward-compatible (Caliber's executor evolves the
        # passed metadata bag over time).
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sb = CoderSandbox(
                root=Path(tmp), max_file_bytes=1024,
                max_total_bytes=1024, max_files=4,
            )
            executor = make_sandbox_tool_executor(sb)
            out = executor("write_file",
                           '{"path": "x.txt", "content": "hi"}',
                           "/some/cwd",
                           call_id="abc",
                           role="coder")
            self.assertIn("wrote x.txt", out)


# ============================================================== #
# build_coder_messages
# ============================================================== #

class TestBuildCoderMessages(unittest.TestCase):

    def test_layout_is_grounding_system_user(self):
        item = CoderTaskItem(task="Write foo()")
        grounding = [{"role": "system", "content": "PROJECT MAP"}]
        msgs = build_coder_messages(item, grounding, question="Q")
        self.assertEqual(msgs[0]["content"], "PROJECT MAP")
        self.assertIs(msgs[1]["role"], "system")
        self.assertEqual(msgs[1]["content"], CODER_SYSTEM)
        self.assertIs(msgs[2]["role"], "user")

    def test_research_findings_inlined(self):
        item = CoderTaskItem(task="x")
        msgs = build_coder_messages(
            item, [], research=["finding A", "finding B"],
        )
        user = msgs[1]["content"]
        self.assertIn("RESEARCHER FINDINGS", user)
        self.assertIn("finding A", user)
        self.assertIn("finding B", user)

    def test_sandbox_cap_block_always_present(self):
        item = CoderTaskItem(task="x")
        msgs = build_coder_messages(
            item, [],
            max_file_bytes=10240, max_total_bytes=102400, max_files=8,
        )
        user = msgs[1]["content"]
        self.assertIn("SANDBOX CAPS", user)
        self.assertIn("10 KB", user)
        self.assertIn("100 KB", user)
        self.assertIn("8", user)

    def test_path_and_why_blocks_when_supplied(self):
        item = CoderTaskItem(
            task="x", path="foo/bar.py", why="critical helper",
        )
        msgs = build_coder_messages(item, [])
        user = msgs[1]["content"]
        self.assertIn("SUGGESTED PATH", user)
        self.assertIn("foo/bar.py", user)
        self.assertIn("WHY", user)
        self.assertIn("critical helper", user)

    def test_omits_optional_blocks_when_absent(self):
        item = CoderTaskItem(task="x")
        msgs = build_coder_messages(item, [])
        user = msgs[1]["content"]
        self.assertNotIn("SUGGESTED PATH", user)
        self.assertNotIn("WHY", user)
        self.assertNotIn("RESEARCHER FINDINGS", user)
        self.assertNotIn("USER QUESTION", user)


# ============================================================== #
# coder_node — stub loop_runner so tests skip claude_hooks deps
# ============================================================== #

class _FakeChat:
    def chat(self, payload, *, think=False):
        return {
            "choices": [{
                "message": {"role": "assistant", "content": "ok"},
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }


def _stub_loop_runner_writes(write_path: str = "out.py",
                              write_content: str = "print(1)\n"):
    """Stub run_loop that simulates the coder calling ``write_file``
    once and producing a SUMMARY final message. ``on_tool`` is
    called BY the stub so the audit + recorder paths run."""
    def _runner(payload, cwd, *, config, tool_specs, chat_fn,
                tool_executor, on_iter=None, on_tool=None,
                preseed_builder=None):
        # Simulate the model emitting a write_file call. Use the
        # supplied tool_executor (the per-lane sandbox dispatcher)
        # so the byte cap + audit list actually fill.
        import json
        args = json.dumps({"path": write_path, "content": write_content})
        out = tool_executor("write_file", args)
        if on_tool is not None:
            on_tool("write_file", args, out, 12, None)
        final_text = (
            f"WROTE {write_path} ({len(write_content)} B). "
            "Implements the task as requested."
        )
        if on_iter is not None:
            on_iter(0, payload, {"choices": [
                {"message": {"content": final_text}},
            ]}, 100)
        return {"final": {"choices": [
            {"message": {"role": "assistant", "content": final_text}},
        ]}}
    return _runner


def _stub_loop_runner_raises(exc: Exception):
    def _runner(payload, cwd, *, config, tool_specs, chat_fn,
                tool_executor, on_iter=None, on_tool=None,
                preseed_builder=None):
        raise exc
    return _runner


class _FakeRecorder:
    def __init__(self):
        self.nodes: list[dict] = []
        self.llms: list[dict] = []
        self.tools: list[dict] = []

    def record_node(self, **kw):
        self.nodes.append(kw)

    def record_llm(self, **kw):
        self.llms.append(kw)

    def record_tool(self, **kw):
        self.tools.append(kw)


class TestCoderNodeHappyPath(unittest.TestCase):

    def test_returns_populated_artifact(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            item = CoderTaskItem(
                task="Write parse_iso8601", path="iso8601.py",
                lane_idx=0, parent_round=1,
            )
            state = {
                "coder_task_item": item, "lane_idx": 0,
                "question": "parse ISO 8601",
            }
            out = coder_node(
                state, chat_client=_FakeChat(),
                grounding_msgs=[], model="kimi-k2.6:cloud",
                cwd=tmp, sid="sid-1",
                loop_runner=_stub_loop_runner_writes(
                    "iso8601.py", "def parse(s): return None\n",
                ),
            )
            artifacts = out["coder_artifacts"]
            self.assertEqual(len(artifacts), 1)
            a = artifacts[0]
            self.assertEqual(a.task, "Write parse_iso8601")
            self.assertEqual(len(a.files), 1)
            self.assertEqual(a.files[0]["path"], "iso8601.py")
            self.assertGreater(a.files[0]["bytes"], 0)
            self.assertIsNone(a.error)
            # File landed on disk under the per-session sandbox.
            sandbox_root = sandbox_root_for(tmp, "sid-1")
            self.assertTrue((sandbox_root / "iso8601.py").exists())

    def test_recorder_callbacks_fire_with_coder_role(self):
        import tempfile
        rec = _FakeRecorder()
        with tempfile.TemporaryDirectory() as tmp:
            item = CoderTaskItem(task="x", parent_round=1)
            state = {"coder_task_item": item, "lane_idx": 1}
            coder_node(
                state, chat_client=_FakeChat(),
                grounding_msgs=[], model="m",
                cwd=tmp, sid="sid-1", recorder=rec,
                loop_runner=_stub_loop_runner_writes(),
            )
            self.assertGreaterEqual(len(rec.nodes), 2)
            kinds = [n.get("kind") for n in rec.nodes]
            self.assertIn("node_enter", kinds)
            self.assertIn("node_exit", kinds)
            # Tag every row role="coder" — post-mortem audits SQL
            # by role to find the coder lanes.
            self.assertEqual(len(rec.llms), 1)
            self.assertEqual(rec.llms[0]["role"], "coder")
            self.assertEqual(len(rec.tools), 1)
            self.assertEqual(rec.tools[0]["role"], "coder")
            self.assertEqual(rec.tools[0]["tool"], "write_file")


class TestCoderNodeTombstones(unittest.TestCase):

    def test_missing_task_item_tombstones(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            out = coder_node(
                {"lane_idx": 0}, chat_client=_FakeChat(),
                grounding_msgs=[], model="m",
                cwd=tmp, sid="sid-1",
                loop_runner=_stub_loop_runner_writes(),
            )
            a = out["coder_artifacts"][0]
            self.assertEqual(a.task, "(missing)")
            self.assertIn("coder_task_item", a.error)

    def test_empty_task_tombstones(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            state = {"coder_task_item": CoderTaskItem(task="   ")}
            out = coder_node(
                state, chat_client=_FakeChat(),
                grounding_msgs=[], model="m",
                cwd=tmp, sid="sid-1",
                loop_runner=_stub_loop_runner_writes(),
            )
            self.assertIsNotNone(out["coder_artifacts"][0].error)

    def test_loop_exception_tombstones(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            item = CoderTaskItem(task="x", parent_round=2, lane_idx=3)
            state = {"coder_task_item": item, "lane_idx": 3}
            out = coder_node(
                state, chat_client=_FakeChat(),
                grounding_msgs=[], model="m",
                cwd=tmp, sid="sid-1",
                loop_runner=_stub_loop_runner_raises(
                    RuntimeError("upstream down"),
                ),
            )
            a = out["coder_artifacts"][0]
            self.assertIn("RuntimeError", a.error)
            self.assertEqual(a.parent_round, 2)
            self.assertEqual(a.lane_idx, 3)

    def test_zero_files_with_rejections_surfaces_error(self):
        # When the model only ever attempted writes that hit the cap,
        # the lane returns an error artifact so the synthesizer
        # surfaces the gap rather than silently composing past it.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            item = CoderTaskItem(task="x", lane_idx=0)
            state = {"coder_task_item": item, "lane_idx": 0}

            def _stub_only_rejected(payload, cwd, *, config, tool_specs,
                                    chat_fn, tool_executor,
                                    on_iter=None, on_tool=None,
                                    preseed_builder=None):
                # Try to write a file too big for the cap. The tool
                # executor returns "error: ..." rather than raising.
                import json
                args = json.dumps({"path": "big.txt", "content": "X" * 100})
                _ = tool_executor("write_file", args)
                if on_iter is not None:
                    on_iter(0, payload, {"choices": [
                        {"message": {"content": "could not write"}},
                    ]}, 50)
                return {"final": {"choices": [
                    {"message": {"content": "could not write"}},
                ]}}
            out = coder_node(
                state, chat_client=_FakeChat(),
                grounding_msgs=[], model="m",
                cwd=tmp, sid="sid-1",
                loop_runner=_stub_only_rejected,
                max_file_bytes=4,
            )
            a = out["coder_artifacts"][0]
            self.assertEqual(a.files, [])
            self.assertIsNotNone(a.error)
            self.assertIn("per-file cap", a.error)


# ============================================================== #
# parse_coder_preamble
# ============================================================== #

class TestParseCoderPreamble(unittest.TestCase):

    def test_fenced_true(self):
        text = '''Plan:
1. step one
2. step two

```json
{"requires_code_generation": true, "coder_tasks": []}
```
'''
        self.assertIs(parse_coder_preamble(text), True)

    def test_fenced_false(self):
        text = '```json\n{"requires_code_generation": false}\n```'
        self.assertIs(parse_coder_preamble(text), False)

    def test_bare_json(self):
        text = '{"requires_code_generation": true}'
        self.assertIs(parse_coder_preamble(text), True)

    def test_missing_key_returns_none(self):
        text = '```json\n{"unrelated": true}\n```'
        self.assertIsNone(parse_coder_preamble(text))

    def test_empty_returns_none(self):
        self.assertIsNone(parse_coder_preamble(""))
        self.assertIsNone(parse_coder_preamble(None))  # type: ignore

    def test_malformed_returns_none(self):
        self.assertIsNone(parse_coder_preamble("```json\n{not valid"))

    def test_string_true_tolerated(self):
        text = '{"requires_code_generation": "true"}'
        self.assertIs(parse_coder_preamble(text), True)

    def test_plain_text_returns_none(self):
        text = "1. do this\n2. then that"
        self.assertIsNone(parse_coder_preamble(text))


# ============================================================== #
# parse_coder_tasks
# ============================================================== #

class TestParseCoderTasks(unittest.TestCase):

    def test_basic_extraction(self):
        text = '''```json
{"requires_code_generation": true, "coder_tasks": [
  {"task": "Write parse()", "path": "iso8601.py", "why": "main"},
  {"task": "Write tests", "path": "test_iso8601.py"}
]}
```'''
        tasks = parse_coder_tasks(text)
        self.assertEqual(len(tasks), 2)
        self.assertEqual(tasks[0].task, "Write parse()")
        self.assertEqual(tasks[0].path, "iso8601.py")
        self.assertEqual(tasks[0].why, "main")
        self.assertEqual(tasks[0].lane_idx, 0)
        self.assertEqual(tasks[1].lane_idx, 1)
        self.assertEqual(tasks[1].why, "")  # missing key tolerated

    def test_skips_items_without_task(self):
        text = ('```json\n{"coder_tasks": [{"path": "x.py"}, '
                '{"task": "valid"}]}\n```')
        tasks = parse_coder_tasks(text)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].task, "valid")

    def test_bare_list_shape(self):
        text = '[{"task": "x"}, {"task": "y"}]'
        tasks = parse_coder_tasks(text)
        self.assertEqual(len(tasks), 2)

    def test_empty_returns_empty(self):
        self.assertEqual(parse_coder_tasks(""), [])
        self.assertEqual(parse_coder_tasks("plain text"), [])

    def test_malformed_returns_empty(self):
        self.assertEqual(parse_coder_tasks("```json\n{bad"), [])

    def test_parent_round_propagated(self):
        text = '```json\n{"coder_tasks": [{"task": "x"}]}\n```'
        tasks = parse_coder_tasks(text, parent_round=2)
        self.assertEqual(tasks[0].parent_round, 2)


# ============================================================== #
# build_coder_artifacts_block
# ============================================================== #

class TestBuildCoderArtifactsBlock(unittest.TestCase):

    def test_empty_returns_empty(self):
        self.assertEqual(build_coder_artifacts_block([]), "")
        self.assertEqual(build_coder_artifacts_block(None), "")  # type: ignore

    def test_renders_artifact_with_files(self):
        a = CoderArtifact(
            task="Write foo()",
            summary="wrote foo.py implementing X",
            files=[{"path": "foo.py", "bytes": 42,
                     "sha256": "abc"}],
        )
        block = build_coder_artifacts_block([a])
        self.assertIn("CODE GENERATED", block)
        self.assertIn("Write foo()", block)
        self.assertIn("foo.py", block)
        self.assertIn("42 B", block)
        self.assertIn("wrote foo.py", block)

    def test_renders_tombstone_distinctly(self):
        a = CoderArtifact(task="Write foo()", error="boom")
        block = build_coder_artifacts_block([a])
        self.assertIn("FAILED", block)
        self.assertIn("boom", block)

    def test_long_summary_truncated(self):
        a = CoderArtifact(
            task="x", summary="Y" * 800,
            files=[{"path": "a.py", "bytes": 1, "sha256": "..."}],
        )
        block = build_coder_artifacts_block([a])
        self.assertIn("[truncated]", block)


# ============================================================== #
# Channel reducer sanity
# ============================================================== #

class TestCoderArtifactsReducer(unittest.TestCase):

    def test_additive_merge(self):
        l1 = [CoderArtifact(task="a", lane_idx=0)]
        l2 = [CoderArtifact(task="b", lane_idx=1)]
        merged = operator.add(l1, l2)
        self.assertEqual([a.task for a in merged], ["a", "b"])


# ============================================================== #
# Planner gate — planner_node(coder_enabled=True)
# ============================================================== #

class _SimpleChat:
    """Records the last system message so we can assert the gate
    block was appended."""
    def __init__(self, reply: str):
        self.reply = reply
        self.last_payload: dict = {}

    def chat(self, payload, *, think=False):
        self.last_payload = payload
        return {
            "choices": [{
                "message": {"role": "assistant", "content": self.reply},
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }


class TestPlannerCoderGate(unittest.TestCase):

    def _run(self, *, coder_enabled, planner_reply):
        from consultants.engine import council
        chat = _SimpleChat(planner_reply)
        state = {"question": "Add a CLI flag"}
        return council.planner_node(
            state, chat_client=chat, model="kimi-k2.6:cloud",
            coder_enabled=coder_enabled,
        ), chat

    def test_no_coder_no_block_appended(self):
        out, chat = self._run(
            coder_enabled=False,
            planner_reply="1. step\n2. step",
        )
        sys_msg = chat.last_payload["messages"][0]["content"]
        self.assertNotIn("CODER GATE", sys_msg)
        # No coder delta in output.
        self.assertNotIn("requires_code_generation", out)
        self.assertNotIn("coder_tasks", out)

    def test_coder_enabled_appends_gate_block(self):
        out, chat = self._run(
            coder_enabled=True,
            planner_reply="1. step\n2. step",
        )
        sys_msg = chat.last_payload["messages"][0]["content"]
        self.assertIn("CODER GATE", sys_msg)
        # Planner didn't declare anything -> no requires_code_generation.
        self.assertNotIn("requires_code_generation", out)

    def test_coder_enabled_with_planner_opt_in_returns_tasks(self):
        reply = (
            "1. step one\n2. step two\n\n"
            "```json\n"
            '{"requires_code_generation": true, "coder_tasks": ['
            '{"task": "Write foo()", "path": "foo.py"}]}\n'
            "```\n"
        )
        out, _chat = self._run(
            coder_enabled=True, planner_reply=reply,
        )
        self.assertIs(out["requires_code_generation"], True)
        tasks = out["coder_tasks"]
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].task, "Write foo()")
        self.assertEqual(tasks[0].path, "foo.py")

    def test_coder_enabled_with_planner_opt_out(self):
        reply = (
            "1. step\n```json\n"
            '{"requires_code_generation": false}\n```'
        )
        out, _chat = self._run(
            coder_enabled=True, planner_reply=reply,
        )
        self.assertIs(out["requires_code_generation"], False)
        self.assertNotIn("coder_tasks", out)

    def test_coder_enabled_with_true_but_no_tasks_treated_as_false(self):
        # Planner asserted code-gen but emitted zero tasks. Defensive
        # fallback: route around the coder.
        reply = (
            "1. step\n```json\n"
            '{"requires_code_generation": true, "coder_tasks": []}\n```'
        )
        out, _chat = self._run(
            coder_enabled=True, planner_reply=reply,
        )
        self.assertIs(out["requires_code_generation"], False)


# ============================================================== #
# Synthesizer block integration
# ============================================================== #

class TestSynthesizerCoderIntegration(unittest.TestCase):

    def test_block_appended_when_artifacts_present(self):
        from consultants.engine.council import build_synthesizer_messages
        artifacts = [
            CoderArtifact(
                task="Write foo()",
                summary="wrote foo.py",
                files=[{"path": "foo.py", "bytes": 42, "sha256": "x"}],
            ),
        ]
        msgs = build_synthesizer_messages(
            "Q", "1. step", [], None,
            coder_artifacts=artifacts,
        )
        user = msgs[1]["content"]
        self.assertIn("CODE GENERATED", user)
        self.assertIn("foo.py", user)

    def test_block_omitted_when_artifacts_absent(self):
        from consultants.engine.council import build_synthesizer_messages
        msgs = build_synthesizer_messages(
            "Q", "1. step", [], None, coder_artifacts=[],
        )
        user = msgs[1]["content"]
        self.assertNotIn("CODE GENERATED", user)


# ============================================================== #
# Task #111: per-language model dispatch via model_chain_resolver
# ============================================================== #

class TestCoderNodeResolverDispatch(unittest.TestCase):
    """When the runner wires a ``model_chain_resolver``, the lane
    walks the chain instead of using the legacy ``chat_client`` +
    ``model`` kwargs. The resolver is called with the language id
    derived from ``CoderTaskItem.path``.
    """

    def test_resolver_called_with_python_for_py_path(self):
        import tempfile
        seen: list = []

        def resolver(lang):
            seen.append(lang)
            return [(_FakeChat(), "glm-5.1:cloud")]

        with tempfile.TemporaryDirectory() as tmp:
            item = CoderTaskItem(task="x", path="solution.py")
            state = {"coder_task_item": item, "lane_idx": 0}
            out = coder_node(
                state, grounding_msgs=[], cwd=tmp, sid="sid-1",
                model_chain_resolver=resolver,
                loop_runner=_stub_loop_runner_writes(),
            )
        self.assertEqual(seen, ["python"])
        self.assertIsNone(out["coder_artifacts"][0].error)

    def test_resolver_called_with_csharp_for_cs_path(self):
        import tempfile
        seen: list = []

        def resolver(lang):
            seen.append(lang)
            return [(_FakeChat(), "deepseek-v4-pro:cloud")]

        with tempfile.TemporaryDirectory() as tmp:
            item = CoderTaskItem(task="x", path="Service.cs")
            state = {"coder_task_item": item, "lane_idx": 0}
            coder_node(
                state, grounding_msgs=[], cwd=tmp, sid="sid-1",
                model_chain_resolver=resolver,
                loop_runner=_stub_loop_runner_writes(
                    "Service.cs", "class S {}\n",
                ),
            )
        self.assertEqual(seen, ["csharp"])

    def test_resolver_called_with_none_for_empty_path(self):
        # When the planner emits no path, the language id is None
        # → the resolver routes to its global default route.
        import tempfile
        seen: list = []

        def resolver(lang):
            seen.append(lang)
            return [(_FakeChat(), "glm-5.1:cloud")]

        with tempfile.TemporaryDirectory() as tmp:
            item = CoderTaskItem(task="x", path="")
            state = {"coder_task_item": item, "lane_idx": 0}
            coder_node(
                state, grounding_msgs=[], cwd=tmp, sid="sid-1",
                model_chain_resolver=resolver,
                loop_runner=_stub_loop_runner_writes(),
            )
        self.assertEqual(seen, [None])

    def test_chain_failover_on_exception(self):
        """Primary raises → fallback wins. Recorder + tools see
        BOTH attempts; the artifact reflects the fallback's output."""
        import tempfile
        attempt_models: list = []

        # Primary stub raises; fallback stub writes successfully.
        def variant_runner(payload, cwd, *, config, tool_specs, chat_fn,
                            tool_executor, on_iter=None, on_tool=None,
                            preseed_builder=None):
            attempt_models.append(payload["model"])
            if payload["model"] == "primary:cloud":
                raise RuntimeError("simulated primary failure")
            # Fallback: write + emit non-empty final
            import json as _json
            args = _json.dumps({"path": "out.py", "content": "ok\n"})
            out = tool_executor("write_file", args)
            if on_tool: on_tool("write_file", args, out, 5, None)
            return {"final": {"choices": [{"message": {
                "role": "assistant", "content": "fallback wrote out.py",
            }}]}}

        def resolver(lang):
            return [
                (_FakeChat(), "primary:cloud"),
                (_FakeChat(), "fallback:cloud"),
            ]

        with tempfile.TemporaryDirectory() as tmp:
            item = CoderTaskItem(task="x", path="solution.py")
            state = {"coder_task_item": item, "lane_idx": 0}
            out = coder_node(
                state, grounding_msgs=[], cwd=tmp, sid="sid-1",
                model_chain_resolver=resolver,
                loop_runner=variant_runner,
            )
        self.assertEqual(attempt_models, ["primary:cloud", "fallback:cloud"])
        art = out["coder_artifacts"][0]
        self.assertIsNone(art.error)
        self.assertIn("fallback wrote out.py", art.summary)

    def test_chain_exhausted_tombstones_with_both_models_named(self):
        import tempfile

        def no_artifacts_runner(payload, cwd, *, config, tool_specs,
                                 chat_fn, tool_executor,
                                 on_iter=None, on_tool=None,
                                 preseed_builder=None):
            # Don't write anything → no_artifacts failover trigger
            return {"final": {"choices": [{"message": {
                "role": "assistant", "content": "I refuse",
            }}]}}

        def resolver(lang):
            return [
                (_FakeChat(), "p:cloud"), (_FakeChat(), "f:cloud"),
            ]

        with tempfile.TemporaryDirectory() as tmp:
            item = CoderTaskItem(task="x", path="solution.py")
            state = {"coder_task_item": item, "lane_idx": 0}
            out = coder_node(
                state, grounding_msgs=[], cwd=tmp, sid="sid-1",
                model_chain_resolver=resolver,
                loop_runner=no_artifacts_runner,
            )
        art = out["coder_artifacts"][0]
        self.assertIsNotNone(art.error)
        self.assertIn("primary p:cloud and fallback f:cloud both failed",
                      art.error)

    def test_empty_message_triggers_failover(self):
        """Stricter than no_artifacts — if the model wrote a file
        but returned an empty final message, the lane treats it as
        a failure (catches the kimi empty-content failure mode)."""
        import tempfile
        attempts: list = []

        def runner_variant(payload, cwd, *, config, tool_specs, chat_fn,
                            tool_executor, on_iter=None, on_tool=None,
                            preseed_builder=None):
            attempts.append(payload["model"])
            import json as _json
            args = _json.dumps({"path": "out.py", "content": "x\n"})
            out = tool_executor("write_file", args)
            if on_tool: on_tool("write_file", args, out, 5, None)
            # Primary writes a file but returns empty final text →
            # empty_message trigger. Fallback writes + responds.
            if payload["model"] == "p:cloud":
                content = "   "
            else:
                content = "fallback summary"
            return {"final": {"choices": [{"message": {
                "role": "assistant", "content": content,
            }}]}}

        def resolver(lang):
            return [(_FakeChat(), "p:cloud"), (_FakeChat(), "f:cloud")]

        with tempfile.TemporaryDirectory() as tmp:
            item = CoderTaskItem(task="x", path="solution.py")
            state = {"coder_task_item": item, "lane_idx": 0}
            out = coder_node(
                state, grounding_msgs=[], cwd=tmp, sid="sid-1",
                model_chain_resolver=resolver,
                loop_runner=runner_variant,
            )
        self.assertEqual(attempts, ["p:cloud", "f:cloud"])
        art = out["coder_artifacts"][0]
        self.assertIsNone(art.error)
        self.assertIn("fallback summary", art.summary)

    def test_resolver_returning_empty_chain_tombstones_cleanly(self):
        """A graph-wiring bug shouldn't crash the lane — return a
        tombstone with a descriptive error instead."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            item = CoderTaskItem(task="x", path="solution.py")
            state = {"coder_task_item": item, "lane_idx": 0}
            out = coder_node(
                state, grounding_msgs=[], cwd=tmp, sid="sid-1",
                model_chain_resolver=lambda lang: [],
                loop_runner=_stub_loop_runner_writes(),
            )
        art = out["coder_artifacts"][0]
        self.assertIsNotNone(art.error)
        self.assertIn("no resolvable model", art.error)

    def test_backcompat_no_resolver_uses_chat_client_kwarg(self):
        """When ``model_chain_resolver`` is None (v1 callers), the
        lane behaves exactly as it did before #111."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            item = CoderTaskItem(task="x", path="solution.py")
            state = {"coder_task_item": item, "lane_idx": 0}
            out = coder_node(
                state, chat_client=_FakeChat(),
                grounding_msgs=[], model="legacy:cloud",
                cwd=tmp, sid="sid-1",
                loop_runner=_stub_loop_runner_writes(),
                # NO resolver
            )
        art = out["coder_artifacts"][0]
        self.assertIsNone(art.error)
        self.assertEqual(len(art.files), 1)


# ============================================================== #
# CODER_WRITE_FILE_TOOL_SPEC sanity
# ============================================================== #

class TestToolSpecShape(unittest.TestCase):
    """The OpenAI-shape tool spec is consumed by ``run_loop`` and
    serialized to the model — pin its shape so a future refactor
    doesn't silently drop a required field."""

    def test_shape(self):
        spec = CODER_WRITE_FILE_TOOL_SPEC
        self.assertEqual(spec["type"], "function")
        fn = spec["function"]
        self.assertEqual(fn["name"], "write_file")
        params = fn["parameters"]
        self.assertEqual(params["type"], "object")
        self.assertEqual(
            sorted(params["properties"].keys()),
            ["content", "path"],
        )
        self.assertEqual(
            sorted(params["required"]),
            ["content", "path"],
        )


if __name__ == "__main__":
    unittest.main()
