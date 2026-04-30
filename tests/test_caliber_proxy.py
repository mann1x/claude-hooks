"""Tests for the caliber grounding proxy: tools, prompt builder, and
the agent loop (with a mocked Ollama)."""

from __future__ import annotations

import json
import os
import socket
import threading
import time
import urllib.request
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_hooks.caliber_proxy import ollama, prompt, recall, server, tools


@pytest.fixture(autouse=True)
def _disable_recall(monkeypatch):
    """Recall has its own test file; here we only care about the
    grounding-prompt + agent-loop behaviour. Disabling at env level keeps
    the message ordering deterministic and avoids hitting live providers
    if the test runner happens to have config/claude-hooks.json populated."""
    monkeypatch.setenv("CALIBER_GROUNDING_RECALL_ENABLED", "0")
    recall.reset_state_for_tests()
    yield
    recall.reset_state_for_tests()


# --------------------------------------------------------------- #
# Tools — path escape guarantees
# --------------------------------------------------------------- #
class TestPathEscape:
    def test_relative_ok(self, tmp_path: Path):
        (tmp_path / "a.txt").write_text("hi")
        assert tools.resolve_in_cwd("a.txt", str(tmp_path)) == \
            str((tmp_path / "a.txt").resolve())

    def test_dotdot_rejected(self, tmp_path: Path):
        with pytest.raises(ValueError):
            tools.resolve_in_cwd("../../etc/passwd", str(tmp_path))

    def test_absolute_outside_rejected(self, tmp_path: Path):
        with pytest.raises(ValueError):
            tools.resolve_in_cwd("/etc/passwd", str(tmp_path))

    def test_absolute_inside_ok(self, tmp_path: Path):
        inside = str(tmp_path / "a.txt")
        (tmp_path / "a.txt").write_text("hi")
        # Absolute path pointing inside cwd should resolve.
        assert tools.resolve_in_cwd(inside, str(tmp_path)) == \
            str((tmp_path / "a.txt").resolve())


# --------------------------------------------------------------- #
# Tools — list_files / read_file / glob / grep
# --------------------------------------------------------------- #
class TestTools:
    def test_list_files_basic(self, tmp_path: Path):
        (tmp_path / "a.txt").write_text("x")
        (tmp_path / "sub").mkdir()
        out = tools.list_files({"path": "."}, str(tmp_path))
        assert "a.txt" in out
        assert "sub/" in out

    def test_list_files_missing(self, tmp_path: Path):
        out = tools.list_files({"path": "nope"}, str(tmp_path))
        assert out.startswith("error:")

    def test_list_files_escape_blocked(self, tmp_path: Path):
        out = tools.list_files({"path": "../.."}, str(tmp_path))
        assert out.startswith("error:")

    def test_read_file_numbers_lines(self, tmp_path: Path):
        (tmp_path / "f.py").write_text("a\nb\nc\nd\n")
        out = tools.read_file({"path": "f.py"}, str(tmp_path))
        assert "     1: a" in out
        assert "     4: d" in out
        assert "file has 4 lines" in out

    def test_read_file_line_range(self, tmp_path: Path):
        (tmp_path / "f.py").write_text("\n".join(f"line{i}" for i in range(1, 11)))
        out = tools.read_file(
            {"path": "f.py", "start_line": 3, "end_line": 5},
            str(tmp_path),
        )
        assert "     3: line3" in out
        assert "     5: line5" in out
        assert "line1" not in out
        assert "line9" not in out

    def test_read_file_truncation(self, tmp_path: Path):
        big = "x" * (200 * 1024)  # 200 KB
        (tmp_path / "big.txt").write_text(big)
        out = tools.read_file({"path": "big.txt"}, str(tmp_path))
        assert "truncated" in out

    def test_glob_match(self, tmp_path: Path):
        (tmp_path / "a.py").write_text("")
        (tmp_path / "b.md").write_text("")
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "c.py").write_text("")
        out = tools.glob_files({"pattern": "*.py"}, str(tmp_path))
        # fnmatch on relative paths, so both top-level and sub c.py match
        assert "a.py" in out
        assert "sub/c.py" in out or "c.py" in out

    def test_grep_finds_match(self, tmp_path: Path):
        (tmp_path / "f.py").write_text("alpha\nbeta\nALPHA\n")
        out = tools.grep(
            {"pattern": "alpha", "path": ".", "case_insensitive": True},
            str(tmp_path),
        )
        assert "f.py:1" in out
        assert "f.py:3" in out

    def test_grep_no_match(self, tmp_path: Path):
        (tmp_path / "f.py").write_text("alpha\n")
        out = tools.grep({"pattern": "zzz", "path": "."}, str(tmp_path))
        assert "(no matches" in out

    def test_execute_unknown_tool(self, tmp_path: Path):
        out = tools.execute("bogus", "{}", str(tmp_path))
        assert out.startswith("error: unknown tool")

    def test_execute_bad_json(self, tmp_path: Path):
        out = tools.execute("list_files", "{not json", str(tmp_path))
        assert out.startswith("error:")

    def test_execute_non_dict_args(self, tmp_path: Path):
        out = tools.execute("list_files", '[1,2]', str(tmp_path))
        assert out.startswith("error:")

    def test_openai_specs_well_formed(self):
        specs = tools.openai_tool_specs()
        assert len(specs) == 6
        names = {s["function"]["name"] for s in specs}
        assert names == {"survey_project", "list_files", "read_file",
                         "glob", "grep", "recall_memory"}
        for s in specs:
            assert s["type"] == "function"
            assert "parameters" in s["function"]
        # survey_project must be first so the model picks it up first.
        assert specs[0]["function"]["name"] == "survey_project"


# --------------------------------------------------------------- #
# survey_project tool
# --------------------------------------------------------------- #
class TestSurveyProject:
    def setup_method(self):
        # Each test gets a clean cache so prior cwds don't leak.
        tools.clear_survey_cache()

    def test_basic_layout_includes_top_dirs_and_extensions(self, tmp_path: Path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "a.py").write_text("x")
        (tmp_path / "src" / "b.py").write_text("x")
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_a.py").write_text("x")
        (tmp_path / "README.md").write_text("hi")
        (tmp_path / "pyproject.toml").write_text("[]")

        out = tools.survey_project({}, str(tmp_path))
        assert "Top-level directories" in out
        assert "`src/`" in out
        assert "`tests/`" in out
        assert ".py: 3" in out
        assert "README.md" in out
        assert "pyproject.toml" in out

    def test_skipped_dirs_excluded(self, tmp_path: Path):
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / "x.js").write_text("//")
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "config").write_text("[]")
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "a.py").write_text("x")

        out = tools.survey_project({}, str(tmp_path))
        assert "node_modules" not in out
        assert ".git" not in out
        assert "`src/`" in out

    def test_skipped_subdirs_dont_inflate_counts(self, tmp_path: Path):
        # Even if .git/.venv sit deep inside a tracked dir, their
        # contents must not be counted in that dir's file total.
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "real.py").write_text("x")
        (tmp_path / "src" / "__pycache__").mkdir()
        for i in range(50):
            (tmp_path / "src" / "__pycache__" / f"{i}.pyc").write_text("x")
        out = tools.survey_project({}, str(tmp_path))
        # Should report 1 file, not 51.
        assert "`src/` — 1 files" in out

    def test_caches_per_cwd(self, tmp_path: Path):
        (tmp_path / "x.py").write_text("a")
        first = tools.survey_project({}, str(tmp_path))
        # Mutate the tree — should NOT show up in the cached result.
        (tmp_path / "y.py").write_text("b")
        second = tools.survey_project({}, str(tmp_path))
        assert first == second
        # Clearing the cache should cause a re-walk and reflect the change.
        tools.clear_survey_cache()
        third = tools.survey_project({}, str(tmp_path))
        assert "y.py" in third
        assert third != first

    def test_budget_cap_truncates(self, tmp_path: Path):
        # Create enough top-level dirs to blow past the 8 KB cap.
        for i in range(400):
            d = tmp_path / f"dir_{i:04d}"
            d.mkdir()
            (d / "f.py").write_text("x")
        out = tools.survey_project({}, str(tmp_path))
        assert len(out) <= tools._SURVEY_MAX_BYTES
        assert "truncated" in out

    def test_executable_via_dispatch(self, tmp_path: Path):
        # Exposed through the same dispatcher the model hits.
        (tmp_path / "a").mkdir()
        (tmp_path / "a" / "x.py").write_text("x")
        out = tools.execute("survey_project", "{}", str(tmp_path))
        assert "Project survey" in out
        assert "`a/`" in out

    def test_empty_project(self, tmp_path: Path):
        out = tools.survey_project({}, str(tmp_path))
        assert "no subdirectories" in out
        assert "(none)" in out  # root files


# --------------------------------------------------------------- #
# Prompt builder
# --------------------------------------------------------------- #
class TestPromptBuilder:
    def test_no_anchors_still_emits_addendum(self, tmp_path: Path):
        msgs = prompt.build_grounding_messages(str(tmp_path))
        assert len(msgs) == 1
        assert "GROUNDING PROTOCOL" in msgs[0]["content"]

    def test_reads_known_anchors(self, tmp_path: Path):
        (tmp_path / "CLAUDE.md").write_text("# project rules")
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'")
        msgs = prompt.build_grounding_messages(str(tmp_path))
        # addendum + structure map + anchor block (map always emitted
        # when cwd has anything non-trivial — drives grounding check).
        assert len(msgs) == 3
        body = msgs[2]["content"]
        assert "### CLAUDE.md" in body
        assert "# project rules" in body
        assert "### pyproject.toml" in body

    def test_anchor_byte_cap(self, tmp_path: Path):
        big = "x" * 80_000
        (tmp_path / "CLAUDE.md").write_text(big)
        msgs = prompt.build_grounding_messages(str(tmp_path), max_anchor_bytes=10_000)
        block = msgs[1]["content"]
        # Payload should not exceed the cap + a few hundred chars of framing
        assert len(block) < 11_000

    def test_missing_anchor_skipped(self, tmp_path: Path):
        # Only one anchor; ensure others don't cause errors
        (tmp_path / "README.md").write_text("readme")
        msgs = prompt.build_grounding_messages(str(tmp_path))
        # With the structure-map injection, anchor block moved to msgs[-1].
        assert "### README.md" in msgs[-1]["content"]

    def test_no_tools_addendum_used_when_disabled(self, tmp_path: Path):
        msgs = prompt.build_grounding_messages(
            str(tmp_path), tools_available=False,
        )
        addendum = msgs[0]["content"]
        assert "No filesystem tools are available" in addendum
        # The with-tools variant must NOT show up.
        assert "list_files, read_file, glob, grep" not in addendum

    def test_with_tools_addendum_default(self, tmp_path: Path):
        msgs = prompt.build_grounding_messages(str(tmp_path))
        addendum = msgs[0]["content"]
        assert "list_files, read_file, glob, grep" in addendum

    def test_rubric_present_with_tools(self, tmp_path: Path):
        msgs = prompt.build_grounding_messages(str(tmp_path))
        addendum = msgs[0]["content"]
        # Slim rubric: four numbered rules targeting top-point sinks in
        # caliber's scoreAndRefine (grounding 12, density 8, code 8, tree 3).
        # The addendum references the structure map by name — dropping
        # that reference decouples the rubric from the auto-injected map
        # and regresses the grounding lift this design relies on.
        assert "CONFIG QUALITY RUBRIC" in addendum
        # Rubric cross-refs the map — word may wrap, so normalize whitespace.
        assert "STRUCTURE MAP" in " ".join(addendum.split())
        assert "backtick ref" in addendum
        assert "fenced code blocks" in addendum
        assert "box-drawing" in addendum

    def test_rubric_present_without_tools(self, tmp_path: Path):
        msgs = prompt.build_grounding_messages(
            str(tmp_path), tools_available=False,
        )
        addendum = msgs[0]["content"]
        assert "CONFIG QUALITY RUBRIC" in addendum
        assert "STRUCTURE MAP" in " ".join(addendum.split())
        # No-tools variant should NOT tell the model to call list_files
        # to discover structure — the material is already inlined.
        assert "Call `list_files" not in addendum

    def test_structure_map_emitted(self, tmp_path: Path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "api").mkdir()
        (tmp_path / "src" / "api" / "h.py").write_text("x=1")
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "t.py").write_text("x=1")
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'")
        msgs = prompt.build_grounding_messages(str(tmp_path))
        # Map lands between the addendum and the anchor block.
        joined = "\n".join(m["content"] for m in msgs)
        assert "PROJECT STRUCTURE MAP" in joined
        assert "src/" in joined
        assert "tests/" in joined
        assert "src/api/" in joined
        assert "pyproject.toml" in joined

    def test_structure_map_skips_noise_dirs(self, tmp_path: Path):
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "__pycache__").mkdir()
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "m.py").write_text("x=1")
        m = prompt.read_project_structure_map(str(tmp_path))
        assert "src/" in m
        assert "node_modules" not in m
        assert "__pycache__" not in m

    def test_extended_sources_included(self, tmp_path: Path):
        (tmp_path / "pkg").mkdir()
        (tmp_path / "pkg" / "m.py").write_text("def hello():\n    pass\n")
        (tmp_path / "pkg" / "t.py").write_text("def t():\n    pass\n")
        msgs = prompt.build_grounding_messages(
            str(tmp_path), extended_sources=True, max_extended_bytes=10_000,
        )
        # addendum + (no anchors here) + extended
        ext_block = msgs[-1]["content"]
        assert "EXTENDED SOURCE FILES" in ext_block
        assert "pkg/m.py" in ext_block
        assert "def hello()" in ext_block

    def test_extended_respects_byte_cap(self, tmp_path: Path):
        # Five files each ~900 bytes. With max=4000 and the "skip
        # files > max/4" gate (=1000), each file clears the gate but
        # cumulative content exceeds the budget — the last file picked
        # gets truncated mid-way.
        for letter in "abcde":
            (tmp_path / f"{letter}.py").write_text(f"{letter} = 1\n" * 150)
        msgs = prompt.build_grounding_messages(
            str(tmp_path), extended_sources=True, max_extended_bytes=4_000,
        )
        ext_block = msgs[-1]["content"]
        assert "EXTENDED SOURCE FILES" in ext_block
        assert "truncated to fit grounding budget" in ext_block
        # The block carries framing overhead; keep a generous ceiling.
        assert len(ext_block) < 5_500


# --------------------------------------------------------------- #
# Agent loop — with a mocked ollama.chat_completions
# --------------------------------------------------------------- #
class TestAgentLoop:
    def test_no_tool_calls_returns_directly(
        self, tmp_path: Path, monkeypatch,
    ):
        # Default force_first would trigger one retry; the dedicated
        # retry tests cover that path. Here we just check that an
        # immediate stop is honoured when force_first is disabled.
        monkeypatch.setenv("CALIBER_GROUNDING_FORCE_FIRST_TOOL_CALL", "0")
        responses = [{
            "choices": [{
                "message": {"role": "assistant", "content": "done"},
                "finish_reason": "stop",
            }],
        }]

        def fake_chat(payload, upstream=None):
            return responses.pop(0)

        with patch.object(ollama, "chat_completions", side_effect=fake_chat):
            result = server.run_agent_loop(
                {"model": "m", "messages": [{"role": "user", "content": "hi"}]},
                str(tmp_path),
                max_iterations=5,
            )
        assert result["choices"][0]["message"]["content"] == "done"

    def test_tool_call_then_answer(self, tmp_path: Path):
        (tmp_path / "README.md").write_text("hello")

        responses = [
            {
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "tool_calls": [{
                            "id": "tc_1",
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": '{"path": "README.md"}',
                            },
                        }],
                    },
                    "finish_reason": "tool_calls",
                }],
            },
            {
                "choices": [{
                    "message": {"role": "assistant", "content": "README.md:1"},
                    "finish_reason": "stop",
                }],
            },
        ]

        def fake_chat(payload, upstream=None):
            return responses.pop(0)

        with patch.object(ollama, "chat_completions", side_effect=fake_chat):
            result = server.run_agent_loop(
                {"model": "m", "messages": [{"role": "user", "content": "read it"}]},
                str(tmp_path),
                max_iterations=5,
            )
        # The final response should come from the second mocked call.
        assert result["choices"][0]["message"]["content"] == "README.md:1"
        assert responses == []  # both calls consumed

    def test_iteration_cap_exits_cleanly(self, tmp_path: Path):
        # Always request another tool call; loop should bail after cap.
        def always_tool_call(payload, upstream=None):
            return {
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "tool_calls": [{
                            "id": "tc", "type": "function",
                            "function": {
                                "name": "list_files",
                                "arguments": '{"path": "."}',
                            },
                        }],
                    },
                    "finish_reason": "tool_calls",
                }],
            }
        with patch.object(ollama, "chat_completions",
                          side_effect=always_tool_call):
            result = server.run_agent_loop(
                {"model": "m", "messages": [{"role": "user", "content": "x"}]},
                str(tmp_path),
                max_iterations=3,
            )
        # We still get a response (the last iteration's reply).
        assert result["choices"][0]["finish_reason"] == "tool_calls"

    def test_duplicate_tool_call_gets_dedup_stub(self, tmp_path: Path):
        (tmp_path / "README.md").write_text("hello")
        call = 0

        def fake_chat(payload, upstream=None):
            nonlocal call
            call += 1
            if call <= 2:
                return {"choices": [{
                    "message": {
                        "role": "assistant",
                        "tool_calls": [{
                            "id": f"tc_{call}",
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": '{"path": "README.md"}',
                            },
                        }],
                    },
                    "finish_reason": "tool_calls",
                }]}
            return {"choices": [{
                "message": {"role": "assistant", "content": "done"},
                "finish_reason": "stop",
            }]}

        with patch.object(ollama, "chat_completions", side_effect=fake_chat):
            server.run_agent_loop(
                {"model": "m", "messages": [{"role": "user", "content": "x"}]},
                str(tmp_path),
                max_iterations=5,
            )
        # iter0: real, iter1: duplicate (stub), iter2: final.
        # Confirm that at least one message marked as dedup made it into
        # the transcript.
        # (Exercised indirectly — just verifies no crash.)
        assert call >= 2

    def test_force_answer_strips_tools_after_n_rounds(
        self, tmp_path: Path, monkeypatch,
    ):
        # After 2 tool rounds, the proxy should drop ``tools`` so the
        # model commits to the final prose+JSON answer caliber expects.
        monkeypatch.setenv("CALIBER_GROUNDING_FORCE_ANSWER_AFTER", "2")
        seen_tools: list[bool] = []

        def fake_chat(payload, upstream=None):
            seen_tools.append("tools" in payload)
            return {
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "tool_calls": [{
                            "id": "tc", "type": "function",
                            "function": {
                                "name": "list_files",
                                "arguments": '{"path": "."}',
                            },
                        }],
                    },
                    "finish_reason": "tool_calls",
                }],
            }

        with patch.object(ollama, "chat_completions", side_effect=fake_chat):
            server.run_agent_loop(
                {"model": "m", "messages": [{"role": "user", "content": "x"}]},
                str(tmp_path),
                max_iterations=4,
            )
        # iters 0,1 have tools; 2,3 must NOT.
        assert seen_tools == [True, True, False, False]

    def test_force_first_tool_call_pins_required(
        self, tmp_path: Path, monkeypatch,
    ):
        # First iter has tool_choice=required. After the model calls a
        # tool, subsequent iters drop to "auto" so the model can answer.
        monkeypatch.setenv("CALIBER_GROUNDING_FORCE_FIRST_TOOL_CALL", "1")
        # Disable preseed: it would set has_called_tool=True before iter 0
        # and short out the force_first path under test.
        monkeypatch.setenv("CALIBER_GROUNDING_PRESEED_SURVEY", "0")
        (tmp_path / "README.md").write_text("hello")
        seen_choice: list[Any] = []

        responses = [
            {"choices": [{
                "message": {
                    "role": "assistant",
                    "tool_calls": [{
                        "id": "tc_1", "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": '{"path": "README.md"}',
                        },
                    }],
                },
                "finish_reason": "tool_calls",
            }]},
            {"choices": [{
                "message": {"role": "assistant", "content": "done"},
                "finish_reason": "stop",
            }]},
        ]

        def fake_chat(payload, upstream=None):
            seen_choice.append(payload.get("tool_choice"))
            return responses.pop(0)

        with patch.object(ollama, "chat_completions", side_effect=fake_chat):
            server.run_agent_loop(
                {"model": "m", "messages": [{"role": "user", "content": "x"}]},
                str(tmp_path),
                max_iterations=5,
            )
        # iter0 → required (no tool yet); iter1 → auto (tool was called)
        assert seen_choice == ["required", "auto"]

    def test_force_first_tool_call_disabled_via_env(
        self, tmp_path: Path, monkeypatch,
    ):
        monkeypatch.setenv("CALIBER_GROUNDING_FORCE_FIRST_TOOL_CALL", "0")
        seen_choice: list[Any] = []

        def fake_chat(payload, upstream=None):
            seen_choice.append(payload.get("tool_choice"))
            return {"choices": [{
                "message": {"role": "assistant", "content": "done"},
                "finish_reason": "stop",
            }]}

        with patch.object(ollama, "chat_completions", side_effect=fake_chat):
            server.run_agent_loop(
                {"model": "m", "messages": [{"role": "user", "content": "x"}]},
                str(tmp_path),
                max_iterations=1,
            )
        # No "required" — first iter just gets "auto" because env override.
        assert seen_choice == ["auto"]

    def test_force_first_skipped_when_tools_disabled(
        self, tmp_path: Path, monkeypatch,
    ):
        # CALIBER_GROUNDING_TOOLS=0 strips tools entirely; force_first
        # must be a no-op (no tool_choice key in payload).
        monkeypatch.setenv("CALIBER_GROUNDING_TOOLS", "0")
        monkeypatch.setenv("CALIBER_GROUNDING_FORCE_FIRST_TOOL_CALL", "1")
        seen_payload: dict = {}

        def fake_chat(payload, upstream=None):
            seen_payload.update(payload)
            return {"choices": [{
                "message": {"role": "assistant", "content": "done"},
                "finish_reason": "stop",
            }]}

        (tmp_path / "m.py").write_text("x = 1\n")
        with patch.object(ollama, "chat_completions", side_effect=fake_chat):
            server.run_agent_loop(
                {"model": "m", "messages": [{"role": "user", "content": "x"}]},
                str(tmp_path),
                max_iterations=1,
            )
        assert "tool_choice" not in seen_payload
        assert "tools" not in seen_payload

    def test_force_first_retry_when_model_skips_tools(
        self, tmp_path: Path, monkeypatch,
    ):
        # Ollama's native /api/chat drops tool_choice="required". When
        # the model returns no tool_calls on iter 0 and force_first is
        # on, the proxy should inject a corrective user message and
        # retry — exactly once.
        monkeypatch.setenv("CALIBER_GROUNDING_FORCE_FIRST_TOOL_CALL", "1")
        monkeypatch.setenv("CALIBER_GROUNDING_PRESEED_SURVEY", "0")
        seen_messages: list[list[dict]] = []
        responses = [
            # iter 0: model SKIPS tools, returns content directly
            {"choices": [{
                "message": {"role": "assistant", "content": "{}"},
                "finish_reason": "stop",
            }]},
            # iter 1 (retry): model uses survey_project
            {"choices": [{
                "message": {
                    "role": "assistant",
                    "tool_calls": [{
                        "id": "tc_1", "type": "function",
                        "function": {
                            "name": "survey_project",
                            "arguments": '{}',
                        },
                    }],
                },
                "finish_reason": "tool_calls",
            }]},
            # iter 2: model emits final answer
            {"choices": [{
                "message": {"role": "assistant", "content": "done"},
                "finish_reason": "stop",
            }]},
        ]

        def fake_chat(payload, upstream=None):
            seen_messages.append(list(payload["messages"]))
            return responses.pop(0)

        with patch.object(ollama, "chat_completions", side_effect=fake_chat):
            result = server.run_agent_loop(
                {"model": "m", "messages": [{"role": "user", "content": "x"}]},
                str(tmp_path),
                max_iterations=5,
            )
        assert result["choices"][0]["message"]["content"] == "done"
        # Iter 1's payload must include the corrective retry user message.
        retry_msgs = seen_messages[1]
        retry_user_text = retry_msgs[-1]["content"]
        assert "skipped tool use" in retry_user_text
        assert "survey_project" in retry_user_text
        # The corrective message must also tell the model to RESUME the
        # original task (not answer conversationally), otherwise gemma
        # asks "what would you like next?" and caliber's JSON parser
        # rejects the response.
        assert "original" in retry_user_text.lower()
        assert "json" in retry_user_text.lower() or "format" in retry_user_text.lower()
        # And the prior assistant content is in the conversation history
        # so the model has full context of what it did wrong.
        assert any(
            m["role"] == "assistant" and m.get("content") == "{}"
            for m in retry_msgs
        )

    def test_force_first_retry_capped_at_once(
        self, tmp_path: Path, monkeypatch,
    ):
        # If the model still doesn't use tools after the retry, the
        # loop must accept that and not retry again — better to ship
        # ungrounded output than to spin forever.
        monkeypatch.setenv("CALIBER_GROUNDING_FORCE_FIRST_TOOL_CALL", "1")
        monkeypatch.setenv("CALIBER_GROUNDING_PRESEED_SURVEY", "0")
        call_count = 0

        def fake_chat(payload, upstream=None):
            nonlocal call_count
            call_count += 1
            return {"choices": [{
                "message": {"role": "assistant", "content": "stubborn"},
                "finish_reason": "stop",
            }]}

        with patch.object(ollama, "chat_completions", side_effect=fake_chat):
            result = server.run_agent_loop(
                {"model": "m", "messages": [{"role": "user", "content": "x"}]},
                str(tmp_path),
                max_iterations=10,
            )
        assert result["choices"][0]["message"]["content"] == "stubborn"
        # Exactly 2 calls — initial + 1 retry — then accept.
        assert call_count == 2

    def test_force_first_drops_required_after_tools_stripped(
        self, tmp_path: Path, monkeypatch,
    ):
        # Once force_after kicks in and tools are stripped, we should
        # also stop pinning tool_choice — the field is just dropped.
        monkeypatch.setenv("CALIBER_GROUNDING_FORCE_FIRST_TOOL_CALL", "1")
        monkeypatch.setenv("CALIBER_GROUNDING_FORCE_ANSWER_AFTER", "1")
        monkeypatch.setenv("CALIBER_GROUNDING_PRESEED_SURVEY", "0")
        seen_choice: list[Any] = []

        responses = [
            # iter 0: tools required; model emits a tool call (loop continues)
            {"choices": [{
                "message": {
                    "role": "assistant",
                    "tool_calls": [{
                        "id": "tc", "type": "function",
                        "function": {
                            "name": "list_files",
                            "arguments": '{"path": "."}',
                        },
                    }],
                },
                "finish_reason": "tool_calls",
            }]},
            # iter 1: tools have been stripped (force_after=1 → strip on
            # this iter); model returns final content.
            {"choices": [{
                "message": {"role": "assistant", "content": "done"},
                "finish_reason": "stop",
            }]},
        ]

        def fake_chat(payload, upstream=None):
            seen_choice.append(payload.get("tool_choice"))
            return responses.pop(0)

        with patch.object(ollama, "chat_completions", side_effect=fake_chat):
            server.run_agent_loop(
                {"model": "m", "messages": [{"role": "user", "content": "x"}]},
                str(tmp_path),
                max_iterations=4,
            )
        # iter 0 → required (tools live, no tool yet)
        # iter 1 → tools stripped, tool_choice popped → None
        assert seen_choice == ["required", None]

    def test_grounding_prepended(self, tmp_path: Path):
        captured = {}

        def fake_chat(payload, upstream=None):
            captured["payload"] = payload
            return {"choices": [{
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }]}

        (tmp_path / "CLAUDE.md").write_text("hello")
        with patch.object(ollama, "chat_completions", side_effect=fake_chat):
            server.run_agent_loop(
                {"model": "m", "messages": [{"role": "user", "content": "hi"}]},
                str(tmp_path),
                max_iterations=1,
            )
        msgs = captured["payload"]["messages"]
        # addendum + structure-map + anchor block + user (+ preseed pair)
        assert msgs[0]["role"] == "system"
        assert "GROUNDING PROTOCOL" in msgs[0]["content"]
        # The anchor block is the last system message (map is inserted
        # between addendum and anchors).
        anchor_idx = next(
            i for i, m in enumerate(msgs)
            if m["role"] == "system" and "### CLAUDE.md" in m["content"]
        )
        assert anchor_idx >= 1
        # The user turn must be present somewhere in the chain. Preseed
        # appends a synthetic survey pair after it, so it is no longer
        # always the final message.
        assert any(m["role"] == "user" for m in msgs)

    def test_tools_injected(self, tmp_path: Path):
        captured = {}

        def fake_chat(payload, upstream=None):
            captured["payload"] = payload
            return {"choices": [{
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }]}

        with patch.object(ollama, "chat_completions", side_effect=fake_chat):
            server.run_agent_loop(
                {"model": "m", "messages": [{"role": "user", "content": "hi"}]},
                str(tmp_path),
                max_iterations=1,
            )
        names = {t["function"]["name"] for t in captured["payload"]["tools"]}
        assert {"list_files", "read_file", "glob", "grep"} <= names

    def test_tools_stripped_when_disabled(self, tmp_path: Path, monkeypatch):
        """CALIBER_GROUNDING_TOOLS=0 must drop the tools field entirely
        and switch the addendum to the no-tools variant."""
        captured = {}
        monkeypatch.setenv("CALIBER_GROUNDING_TOOLS", "0")

        def fake_chat(payload, upstream=None):
            captured["payload"] = payload
            return {"choices": [{
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }]}

        (tmp_path / "m.py").write_text("def f():\n    pass\n")
        with patch.object(ollama, "chat_completions", side_effect=fake_chat):
            server.run_agent_loop(
                {
                    "model": "m",
                    "messages": [{"role": "user", "content": "hi"}],
                    "tools": [{"type": "function",
                               "function": {"name": "caller_supplied"}}],
                    "tool_choice": "auto",
                },
                str(tmp_path),
                max_iterations=1,
            )
        payload = captured["payload"]
        assert "tools" not in payload
        assert "tool_choice" not in payload
        # Addendum swapped AND extended sources forced on.
        assert "No filesystem tools are available" in payload["messages"][0]["content"]
        joined = "\n".join(m["content"] for m in payload["messages"] if m["role"] == "system")
        assert "EXTENDED SOURCE FILES" in joined

    def test_upstream_error_propagates(self, tmp_path: Path):
        """Non-2xx from Ollama must surface as an UpstreamError so the
        server can mirror a real status code to the client rather than
        quietly returning empty choices."""
        def fake_chat(payload, upstream=None):
            raise ollama.UpstreamError(500, {
                "error": {"message": "model failed to load"},
            })

        with patch.object(ollama, "chat_completions", side_effect=fake_chat):
            with pytest.raises(ollama.UpstreamError) as excinfo:
                server.run_agent_loop(
                    {"model": "m",
                     "messages": [{"role": "user", "content": "hi"}]},
                    str(tmp_path),
                    max_iterations=1,
                )
            assert excinfo.value.status == 500


# --------------------------------------------------------------- #
# Preseed survey — synthetic survey_project tool call before iter 0
# --------------------------------------------------------------- #
class TestPreseedSurvey:
    def test_preseed_injects_assistant_and_tool_messages(
        self, tmp_path: Path, monkeypatch,
    ):
        # Preseed is off by default (it suppresses the audit response on
        # gemma4-98e), so opt in here. The very first payload sent to
        # the model should then contain the synthetic survey_project
        # assistant turn and its tool result, with no force_first
        # ``required`` (because has_called_tool is True).
        monkeypatch.setenv("CALIBER_GROUNDING_PRESEED_SURVEY", "1")
        (tmp_path / "README.md").write_text("hi")
        captured: dict = {}

        def fake_chat(payload, upstream=None):
            captured["payload"] = payload
            return {"choices": [{
                "message": {"role": "assistant", "content": "done"},
                "finish_reason": "stop",
            }]}

        with patch.object(ollama, "chat_completions", side_effect=fake_chat):
            server.run_agent_loop(
                {"model": "m", "messages": [{"role": "user", "content": "x"}]},
                str(tmp_path),
                max_iterations=1,
            )
        msgs = captured["payload"]["messages"]
        # Last two messages are the synthetic pair, in order.
        assert msgs[-2]["role"] == "assistant"
        assert msgs[-2]["tool_calls"][0]["function"]["name"] == "survey_project"
        assert msgs[-2]["tool_calls"][0]["function"]["arguments"] == "{}"
        assert msgs[-2]["tool_calls"][0]["id"] == server.PRESEED_SURVEY_TOOL_CALL_ID
        assert msgs[-1]["role"] == "tool"
        assert msgs[-1]["tool_call_id"] == server.PRESEED_SURVEY_TOOL_CALL_ID
        assert msgs[-1]["name"] == "survey_project"
        # The survey content really came from tools.survey_project — it
        # should mention the file we created.
        assert "README.md" in msgs[-1]["content"]
        # tool_choice on iter 0 is "auto", not "required" — preseed
        # marks has_called_tool=True so force_first is dormant.
        assert captured["payload"]["tool_choice"] == "auto"

    def test_preseed_disabled_via_env(
        self, tmp_path: Path, monkeypatch,
    ):
        monkeypatch.setenv("CALIBER_GROUNDING_PRESEED_SURVEY", "0")
        captured: dict = {}

        def fake_chat(payload, upstream=None):
            captured["payload"] = payload
            return {"choices": [{
                "message": {"role": "assistant", "content": "done"},
                "finish_reason": "stop",
            }]}

        with patch.object(ollama, "chat_completions", side_effect=fake_chat):
            server.run_agent_loop(
                {"model": "m", "messages": [{"role": "user", "content": "x"}]},
                str(tmp_path),
                max_iterations=1,
            )
        msgs = captured["payload"]["messages"]
        assert all(
            not (m["role"] == "tool" and m.get("name") == "survey_project")
            for m in msgs
        )

    def test_preseed_skipped_when_tools_disabled(
        self, tmp_path: Path, monkeypatch,
    ):
        # No tools, no preseed — caliber is in pre-stuffing-only mode.
        # Explicitly turn preseed on so the test exercises the AND-gate
        # (preseed enabled but tools disabled), not just the default-off
        # behaviour.
        monkeypatch.setenv("CALIBER_GROUNDING_PRESEED_SURVEY", "1")
        monkeypatch.setenv("CALIBER_GROUNDING_TOOLS", "0")
        captured: dict = {}

        def fake_chat(payload, upstream=None):
            captured["payload"] = payload
            return {"choices": [{
                "message": {"role": "assistant", "content": "done"},
                "finish_reason": "stop",
            }]}

        with patch.object(ollama, "chat_completions", side_effect=fake_chat):
            server.run_agent_loop(
                {"model": "m", "messages": [{"role": "user", "content": "x"}]},
                str(tmp_path),
                max_iterations=1,
            )
        msgs = captured["payload"]["messages"]
        assert all(m["role"] != "tool" for m in msgs)

    def test_preseed_dedups_model_retry(
        self, tmp_path: Path, monkeypatch,
    ):
        # If the model decides to call survey_project again with the
        # same args, the dedup cache (pre-populated by preseed) must
        # short-circuit it to a stub instead of re-running the survey.
        monkeypatch.setenv("CALIBER_GROUNDING_PRESEED_SURVEY", "1")
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "x.py").write_text("# stub\n")

        responses = [
            {"choices": [{
                "message": {
                    "role": "assistant",
                    "tool_calls": [{
                        "id": "tc_dup", "type": "function",
                        "function": {
                            "name": "survey_project",
                            "arguments": "{}",
                        },
                    }],
                },
                "finish_reason": "tool_calls",
            }]},
            {"choices": [{
                "message": {"role": "assistant", "content": "done"},
                "finish_reason": "stop",
            }]},
        ]
        captured: list[list[dict]] = []

        def fake_chat(payload, upstream=None):
            captured.append(list(payload["messages"]))
            return responses.pop(0)

        with patch.object(ollama, "chat_completions", side_effect=fake_chat):
            server.run_agent_loop(
                {"model": "m", "messages": [{"role": "user", "content": "x"}]},
                str(tmp_path),
                max_iterations=5,
            )
        # On iter 1 the model's duplicate survey_project call should
        # have produced a dedup-stub tool message, NOT a fresh survey.
        iter1_msgs = captured[1]
        # Find the tool result for tc_dup.
        dup_result = next(
            m for m in iter1_msgs
            if m["role"] == "tool" and m.get("tool_call_id") == "tc_dup"
        )
        assert "duplicate" in dup_result["content"].lower()

    def test_preseed_survives_empty_project(
        self, tmp_path: Path, monkeypatch,
    ):
        # An empty cwd: tools.survey_project still produces a small
        # legible report, and the preseed pair is injected.
        monkeypatch.setenv("CALIBER_GROUNDING_PRESEED_SURVEY", "1")
        captured: dict = {}

        def fake_chat(payload, upstream=None):
            captured["payload"] = payload
            return {"choices": [{
                "message": {"role": "assistant", "content": "done"},
                "finish_reason": "stop",
            }]}

        with patch.object(ollama, "chat_completions", side_effect=fake_chat):
            server.run_agent_loop(
                {"model": "m", "messages": [{"role": "user", "content": "x"}]},
                str(tmp_path),
                max_iterations=1,
            )
        msgs = captured["payload"]["messages"]
        tool_msg = next(m for m in msgs if m["role"] == "tool")
        assert tool_msg["name"] == "survey_project"
        assert len(tool_msg["content"]) > 0


# --------------------------------------------------------------- #
# JSON sanitiser — strip trailing junk after balanced braces
# --------------------------------------------------------------- #
class TestSanitizeAssistantJson:
    def test_strips_one_trailing_brace(self):
        # Real shape from a gemma4 audit response: STATUS preamble +
        # EXPLAIN + valid JSON object + one stray closing brace.
        content = (
            'STATUS: working\n\n'
            'EXPLAIN:\n[Changes]\n- ...\n\n'
            '{\n  "targetAgent": ["claude"],\n  "claude": {"claudeMd": "x"}\n}\n}'
        )
        result = {"choices": [{"message": {"content": content}}]}
        server.sanitize_assistant_json(result)
        cleaned = result["choices"][0]["message"]["content"]
        # Cleaned content is shorter than original by exactly the stray "\n}".
        assert len(cleaned) == len(content) - 2
        # The cleaned JSON parses cleanly via the same regex caliber uses.
        import json as _json
        idx = cleaned.find('{\n  "targetAgent"')
        assert idx >= 0
        parsed = _json.loads(cleaned[idx:])
        assert parsed["targetAgent"] == ["claude"]

    def test_strips_multiple_trailing_braces_and_fences(self):
        content = (
            '{\n  "k": 1\n}\n}\n}\n```'
        )
        result = {"choices": [{"message": {"content": content}}]}
        server.sanitize_assistant_json(result)
        cleaned = result["choices"][0]["message"]["content"]
        assert cleaned.rstrip().endswith('}')
        import json as _json
        _json.loads(cleaned[cleaned.find('{'):])

    def test_preserves_status_preamble(self):
        content = 'STATUS: a\nSTATUS: b\n\n{\n  "x": 1\n}\n}'
        result = {"choices": [{"message": {"content": content}}]}
        server.sanitize_assistant_json(result)
        cleaned = result["choices"][0]["message"]["content"]
        assert cleaned.startswith('STATUS: a\nSTATUS: b\n')

    def test_truncated_json_left_untouched(self):
        # If the JSON itself is incomplete, don't pretend to fix it —
        # caliber should see the raw error so it can retry.
        content = '{\n  "x": 1, "y":'
        result = {"choices": [{"message": {"content": content}}]}
        server.sanitize_assistant_json(result)
        assert result["choices"][0]["message"]["content"] == content

    def test_no_json_in_content_left_untouched(self):
        content = 'STATUS: I refuse to emit JSON. The end.'
        result = {"choices": [{"message": {"content": content}}]}
        server.sanitize_assistant_json(result)
        assert result["choices"][0]["message"]["content"] == content

    def test_clean_json_is_idempotent(self):
        content = '{\n  "ok": true\n}'
        result = {"choices": [{"message": {"content": content}}]}
        server.sanitize_assistant_json(result)
        assert result["choices"][0]["message"]["content"] == content

    def test_braces_inside_strings_are_ignored(self):
        # A `}` inside a string literal must not close the outer object.
        content = '{\n  "code": "f() { return {\\"a\\":1}; }",\n  "ok": true\n}\n}'
        result = {"choices": [{"message": {"content": content}}]}
        server.sanitize_assistant_json(result)
        cleaned = result["choices"][0]["message"]["content"]
        assert cleaned.count('}\n}') == 0
        import json as _json
        _json.loads(cleaned)

    def test_empty_choices_no_crash(self):
        server.sanitize_assistant_json({"choices": []})
        server.sanitize_assistant_json({})

    def test_non_string_content_no_crash(self):
        # OpenAI tool-call shape: content is None.
        result = {"choices": [{"message": {"content": None, "tool_calls": []}}]}
        server.sanitize_assistant_json(result)
        assert result["choices"][0]["message"]["content"] is None

    def test_array_top_level_not_truncated_to_first_element(self):
        # Regression: file-scoring responses are JSON arrays. An earlier
        # sanitiser implementation matched the `{` of the FIRST inner
        # element and stripped the rest of the array, dropping 74 of 75
        # candidate scores. The sanitiser must recognise ``[`` as a
        # valid top-level start.
        content = (
            '[\n'
            '  {"index": 0, "score": 85, "reason": "good"},\n'
            '  {"index": 1, "score": 60, "reason": "ok"},\n'
            '  {"index": 2, "score": 30, "reason": "weak"}\n'
            ']'
        )
        result = {"choices": [{"message": {"content": content}}]}
        server.sanitize_assistant_json(result)
        cleaned = result["choices"][0]["message"]["content"]
        import json as _json
        parsed = _json.loads(cleaned)
        assert isinstance(parsed, list) and len(parsed) == 3
        assert parsed[2]["index"] == 2

    def test_array_with_trailing_garbage_stripped(self):
        content = (
            '[\n  {"index": 0, "score": 85}\n]\n}\n'
            'extraneous explanation the model added'
        )
        result = {"choices": [{"message": {"content": content}}]}
        server.sanitize_assistant_json(result)
        cleaned = result["choices"][0]["message"]["content"]
        import json as _json
        parsed = _json.loads(cleaned)
        assert parsed == [{"index": 0, "score": 85}]
        assert "extraneous" not in cleaned


# --------------------------------------------------------------- #
# HTTP server smoke test — spin up, probe /health + POST a no-op
# --------------------------------------------------------------- #
class TestHttpServer:
    def _free_port(self) -> int:
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        p = s.getsockname()[1]
        s.close()
        return p

    def test_health_endpoint(self, tmp_path: Path, monkeypatch):
        port = self._free_port()
        monkeypatch.setenv("CALIBER_GROUNDING_PORT", str(port))
        monkeypatch.setenv("CALIBER_GROUNDING_CWD", str(tmp_path))
        srv = server.build_server()
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            resp = urllib.request.urlopen(
                f"http://127.0.0.1:{port}/health", timeout=3,
            )
            body = json.loads(resp.read())
            assert body["ok"] is True
        finally:
            srv.shutdown()
            srv.server_close()

    def test_chat_completions_happy_path(self, tmp_path: Path, monkeypatch):
        port = self._free_port()
        monkeypatch.setenv("CALIBER_GROUNDING_PORT", str(port))
        monkeypatch.setenv("CALIBER_GROUNDING_CWD", str(tmp_path))
        (tmp_path / "CLAUDE.md").write_text("x")

        def fake_chat(payload, upstream=None):
            return {"choices": [{
                "message": {"role": "assistant", "content": "hello"},
                "finish_reason": "stop",
            }]}

        srv = server.build_server()
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            with patch.object(ollama, "chat_completions", side_effect=fake_chat):
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/v1/chat/completions",
                    data=json.dumps({
                        "model": "m",
                        "messages": [{"role": "user", "content": "hi"}],
                    }).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                resp = urllib.request.urlopen(req, timeout=5)
                body = json.loads(resp.read())
                assert body["choices"][0]["message"]["content"] == "hello"
        finally:
            srv.shutdown()
            srv.server_close()

    def test_404_on_unknown_path(self, tmp_path: Path, monkeypatch):
        port = self._free_port()
        monkeypatch.setenv("CALIBER_GROUNDING_PORT", str(port))
        monkeypatch.setenv("CALIBER_GROUNDING_CWD", str(tmp_path))
        srv = server.build_server()
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/bogus", method="GET",
            )
            try:
                urllib.request.urlopen(req, timeout=3)
                pytest.fail("expected 404")
            except urllib.error.HTTPError as e:
                assert e.code == 404
        finally:
            srv.shutdown()
            srv.server_close()

    def _read_sse_until_done(self, sock: socket.socket, deadline: float) -> str:
        """Drain bytes from a raw socket until ``data: [DONE]`` is seen
        or the deadline expires. Avoids urllib.request.urlopen.read()
        blocking on EOF when the server keeps the connection open.
        """
        buf = bytearray()
        while time.time() < deadline:
            sock.settimeout(max(0.05, deadline - time.time()))
            try:
                chunk = sock.recv(4096)
            except (socket.timeout, TimeoutError):
                break
            if not chunk:
                break
            buf.extend(chunk)
            if b"data: [DONE]" in buf:
                break
        return buf.decode("utf-8", errors="replace")

    def _post_raw(self, port: int, body: dict) -> socket.socket:
        """Open a raw socket POST and return it for incremental reading.
        urllib.request.urlopen.read() waits for EOF, which our SSE
        responses don't provide promptly under HTTP/1.0 keep-alive.
        """
        s = socket.create_connection(("127.0.0.1", port), timeout=5)
        body_bytes = json.dumps(body).encode("utf-8")
        req = (
            b"POST /v1/chat/completions HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: " + str(len(body_bytes)).encode() + b"\r\n"
            b"Connection: close\r\n"
            b"\r\n" + body_bytes
        )
        s.sendall(req)
        return s

    def test_stream_sends_heartbeat_during_long_loop(
        self, tmp_path: Path, monkeypatch,
    ):
        """When stream=true and the agent loop blocks for several seconds,
        the proxy must emit ``: heartbeat`` SSE comments so the client's
        bodyTimeout (undici 5min default) doesn't fire before the loop
        returns.
        """
        port = self._free_port()
        monkeypatch.setenv("CALIBER_GROUNDING_PORT", str(port))
        monkeypatch.setenv("CALIBER_GROUNDING_CWD", str(tmp_path))
        monkeypatch.setenv(
            "CALIBER_GROUNDING_SSE_HEARTBEAT_SECONDS", "0.2",
        )
        (tmp_path / "CLAUDE.md").write_text("x")

        def slow_chat(payload, upstream=None):
            time.sleep(0.7)  # spans ~3 heartbeat intervals
            return {"choices": [{
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }]}

        srv = server.build_server()
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            with patch.object(ollama, "chat_completions", side_effect=slow_chat):
                sock = self._post_raw(port, {
                    "model": "m", "stream": True,
                    "messages": [{"role": "user", "content": "hi"}],
                })
                try:
                    raw = self._read_sse_until_done(sock, time.time() + 5)
                finally:
                    sock.close()
        finally:
            srv.shutdown()
            srv.server_close()
        assert "Content-Type: text/event-stream" in raw
        assert ": heartbeat" in raw
        assert "data: [DONE]" in raw
        assert '"content": "ok"' in raw

    def test_sse_response_uses_connection_close(self, tmp_path: Path, monkeypatch):
        """SSE responses must declare ``Connection: close`` so undici (used
        by caliber's OpenAI Node SDK) doesn't wait for keep-alive bytes
        on a closed HTTP/1.0 socket and then raise "terminated" /
        "other side closed" 5min later.
        """
        port = self._free_port()
        monkeypatch.setenv("CALIBER_GROUNDING_PORT", str(port))
        monkeypatch.setenv("CALIBER_GROUNDING_CWD", str(tmp_path))
        monkeypatch.setenv("CALIBER_GROUNDING_SSE_HEARTBEAT_SECONDS", "60")
        (tmp_path / "CLAUDE.md").write_text("x")

        def fast_chat(payload, upstream=None):
            return {"choices": [{
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }]}

        srv = server.build_server()
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            with patch.object(ollama, "chat_completions", side_effect=fast_chat):
                sock = self._post_raw(port, {
                    "model": "m", "stream": True,
                    "messages": [{"role": "user", "content": "hi"}],
                })
                try:
                    raw = self._read_sse_until_done(sock, time.time() + 5)
                finally:
                    sock.close()
        finally:
            srv.shutdown()
            srv.server_close()
        # Header line is case-insensitive but the value should be `close`.
        assert "Connection: close" in raw

    def test_stream_emits_done_marker(self, tmp_path: Path, monkeypatch):
        """A streaming response must end with ``data: [DONE]``."""
        port = self._free_port()
        monkeypatch.setenv("CALIBER_GROUNDING_PORT", str(port))
        monkeypatch.setenv("CALIBER_GROUNDING_CWD", str(tmp_path))
        monkeypatch.setenv(
            "CALIBER_GROUNDING_SSE_HEARTBEAT_SECONDS", "60",
        )
        (tmp_path / "CLAUDE.md").write_text("x")

        def fast_chat(payload, upstream=None):
            return {"choices": [{
                "message": {"role": "assistant", "content": "abc"},
                "finish_reason": "stop",
            }]}

        srv = server.build_server()
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            with patch.object(ollama, "chat_completions", side_effect=fast_chat):
                sock = self._post_raw(port, {
                    "model": "m", "stream": True,
                    "messages": [{"role": "user", "content": "hi"}],
                })
                try:
                    raw = self._read_sse_until_done(sock, time.time() + 5)
                finally:
                    sock.close()
        finally:
            srv.shutdown()
            srv.server_close()
        assert "data: [DONE]" in raw
        assert '"content": "abc"' in raw


# --------------------------------------------------------------- #
# Ollama /api/chat translators — request and response reshaping
# --------------------------------------------------------------- #
class TestOllamaRequestTranslator:
    def test_base_url_strips_legacy_v1_suffix(self):
        assert ollama._base_url("http://h:11433/v1") == "http://h:11433"
        assert ollama._base_url("http://h:11433/v1/") == "http://h:11433"
        assert ollama._base_url("http://h:11433") == "http://h:11433"
        assert ollama._base_url("http://h:11433/") == "http://h:11433"

    def test_max_completion_tokens_maps_to_num_predict(self):
        out = ollama._to_ollama_request({
            "model": "m",
            "messages": [{"role": "user", "content": "hi"}],
            "max_completion_tokens": 256,
        })
        assert out["options"]["num_predict"] == 256

    def test_max_tokens_fallback(self):
        out = ollama._to_ollama_request({
            "model": "m", "messages": [], "max_tokens": 128,
        })
        assert out["options"]["num_predict"] == 128

    def test_sampling_fields_into_options(self):
        out = ollama._to_ollama_request({
            "model": "m", "messages": [],
            "temperature": 0.7, "top_p": 0.9, "seed": 42,
            "stop": ["</s>"], "presence_penalty": 0.1,
        })
        opts = out["options"]
        assert opts == {
            "temperature": 0.7, "top_p": 0.9, "seed": 42,
            "stop": ["</s>"], "presence_penalty": 0.1,
        }

    def test_existing_options_preserved(self):
        """Caller-set ``options.num_ctx`` (the proxy's env-var injection)
        must survive; mapped fields use setdefault so explicit values win."""
        out = ollama._to_ollama_request({
            "model": "m", "messages": [],
            "options": {"num_ctx": 131072, "temperature": 0.3},
            "temperature": 0.9,  # should NOT override the explicit option
        })
        assert out["options"]["num_ctx"] == 131072
        assert out["options"]["temperature"] == 0.3

    def test_think_and_keep_alive_pass_through(self):
        out = ollama._to_ollama_request({
            "model": "m", "messages": [],
            "think": "medium", "keep_alive": -1,
        })
        assert out["think"] == "medium"
        assert out["keep_alive"] == -1

    def test_response_format_json_to_format(self):
        out = ollama._to_ollama_request({
            "model": "m", "messages": [],
            "response_format": {"type": "json_object"},
        })
        assert out["format"] == "json"

    def test_tool_choice_dropped(self):
        out = ollama._to_ollama_request({
            "model": "m", "messages": [],
            "tools": [{"type": "function", "function": {"name": "f"}}],
            "tool_choice": "auto",
        })
        assert "tool_choice" not in out
        assert out["tools"]

    def test_assistant_tool_calls_string_args_parsed(self):
        out = ollama._to_ollama_request({
            "model": "m",
            "messages": [{
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_1", "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": '{"path": "README.md"}',
                    },
                }],
            }],
        })
        tc = out["messages"][0]["tool_calls"][0]
        # OpenAI-only fields removed; arguments parsed to dict.
        assert tc == {"function": {"name": "read_file",
                                    "arguments": {"path": "README.md"}}}

    def test_assistant_tool_calls_invalid_json_falls_back_empty(self):
        out = ollama._to_ollama_request({
            "model": "m",
            "messages": [{
                "role": "assistant",
                "tool_calls": [{
                    "id": "x", "type": "function",
                    "function": {"name": "f", "arguments": "not-json"},
                }],
            }],
        })
        assert out["messages"][0]["tool_calls"][0]["function"]["arguments"] == {}

    def test_tool_message_drops_id_keeps_name(self):
        out = ollama._to_ollama_request({
            "model": "m",
            "messages": [{
                "role": "tool",
                "tool_call_id": "call_1",
                "name": "read_file",
                "content": "hi",
            }],
        })
        assert out["messages"][0] == {
            "role": "tool", "content": "hi", "tool_name": "read_file",
        }

    def test_stream_forced_false(self):
        out = ollama._to_ollama_request({
            "model": "m", "messages": [], "stream": True,
        })
        assert out["stream"] is False


class TestOllamaResponseTranslator:
    def test_simple_content_response(self):
        result = ollama._to_openai_response({
            "model": "gemma4-98e",
            "message": {"role": "assistant", "content": "hello"},
            "done": True, "done_reason": "stop",
            "prompt_eval_count": 12, "eval_count": 4,
        })
        assert result["object"] == "chat.completion"
        assert result["model"] == "gemma4-98e"
        assert result["choices"][0]["message"]["content"] == "hello"
        assert result["choices"][0]["finish_reason"] == "stop"
        assert result["usage"] == {
            "prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16,
        }

    def test_tool_calls_get_id_type_and_string_args(self):
        result = ollama._to_openai_response({
            "model": "m",
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "function": {
                        "name": "list_files",
                        "arguments": {"path": "."},
                    },
                }],
            },
            "done": True, "done_reason": "stop",
        })
        # done_reason="stop" but tool_calls present -> finish_reason coerced
        assert result["choices"][0]["finish_reason"] == "tool_calls"
        tc = result["choices"][0]["message"]["tool_calls"][0]
        assert tc["type"] == "function"
        assert tc["id"].startswith("call_")
        assert tc["function"]["name"] == "list_files"
        assert json.loads(tc["function"]["arguments"]) == {"path": "."}

    def test_done_reason_length_passes_through(self):
        result = ollama._to_openai_response({
            "model": "m",
            "message": {"role": "assistant", "content": "x"},
            "done": True, "done_reason": "length",
        })
        assert result["choices"][0]["finish_reason"] == "length"

    def test_thinking_field_preserved(self):
        result = ollama._to_openai_response({
            "model": "m",
            "message": {
                "role": "assistant", "content": "answer",
                "thinking": "let me think",
            },
            "done_reason": "stop",
        })
        assert result["choices"][0]["message"]["thinking"] == "let me think"

    def test_missing_token_counts_default_to_zero(self):
        result = ollama._to_openai_response({
            "model": "m",
            "message": {"role": "assistant", "content": "x"},
            "done_reason": "stop",
        })
        assert result["usage"] == {
            "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
        }


class TestRoundTripMessages:
    """Translate an assistant tool-call response back into a request — what
    the agent loop does on every iteration. The runtime must not lose
    information across the boundary."""

    def test_response_then_request_preserves_tool_call_payload(self):
        ollama_resp = {
            "model": "m",
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "function": {
                        "name": "grep",
                        "arguments": {"pattern": "foo", "path": "src"},
                    },
                }],
            },
            "done_reason": "stop",
        }
        openai_msg = ollama._to_openai_response(ollama_resp)["choices"][0]["message"]
        # Now feed it back as an assistant message in a follow-up request.
        translated_back = ollama._translate_request_message(openai_msg)
        tc = translated_back["tool_calls"][0]
        assert tc == {"function": {"name": "grep",
                                    "arguments": {"pattern": "foo", "path": "src"}}}
