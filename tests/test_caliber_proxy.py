"""Tests for the caliber grounding proxy: tools, prompt builder, and
the agent loop (with a mocked Ollama)."""

from __future__ import annotations

import json
import os
import socket
import threading
import urllib.request
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_hooks.caliber_proxy import ollama, prompt, server, tools


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
        assert len(specs) == 4
        names = {s["function"]["name"] for s in specs}
        assert names == {"list_files", "read_file", "glob", "grep"}
        for s in specs:
            assert s["type"] == "function"
            assert "parameters" in s["function"]


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
        assert len(msgs) == 2  # addendum + anchor block
        body = msgs[1]["content"]
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
        assert "### README.md" in msgs[1]["content"]

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
    def test_no_tool_calls_returns_directly(self, tmp_path: Path):
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
        # addendum system + anchor system + user
        assert msgs[0]["role"] == "system"
        assert "GROUNDING PROTOCOL" in msgs[0]["content"]
        assert msgs[1]["role"] == "system"
        assert "### CLAUDE.md" in msgs[1]["content"]
        assert msgs[-1]["role"] == "user"

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
