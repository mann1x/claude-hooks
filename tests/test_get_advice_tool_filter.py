"""Verify the /get-advice CLI's tool filtering — only the configured
subset is exposed to the advisor model, and ``none`` suppresses both
the tool list and (downstream) the tool-availability addendum."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_hooks.get_advice import cli


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_ADVISOR_CACHE_DIR",
                       str(tmp_path / "cache"))
    monkeypatch.setenv("CALIBER_GROUNDING_UPSTREAM", "http://test.invalid")
    yield


def _run(argv) -> dict:
    buf = io.StringIO()
    with patch("sys.stdout", buf):
        cli.main(argv)
    return json.loads(buf.getvalue().strip())


class TestToolFilter:
    def test_default_passes_all_six(self):
        specs = cli._filter_tool_specs(list(cli.advisor_config.KNOWN_TOOLS))
        names = {s["function"]["name"] for s in specs}
        assert names == set(cli.advisor_config.KNOWN_TOOLS)

    def test_subset_passes_only_configured(self):
        specs = cli._filter_tool_specs(["read_file", "grep"])
        names = {s["function"]["name"] for s in specs}
        assert names == {"read_file", "grep"}

    def test_empty_list_returns_empty(self):
        assert cli._filter_tool_specs([]) == []

    def test_unknown_name_dropped(self):
        # Defensive — the CLI's set-tools rejects unknowns at write
        # time, but if config gets written by hand the filter must
        # not crash on a bogus entry.
        specs = cli._filter_tool_specs(["read_file", "fake_tool"])
        names = {s["function"]["name"] for s in specs}
        assert names == {"read_file"}


class TestTurnHonorsConfig:
    def _setup(self):
        Path(os.environ["CLAUDE_ADVISOR_CACHE_DIR"]).mkdir(parents=True,
                                                           exist_ok=True)

    def test_none_suppresses_tools(self, tmp_path: Path):
        self._setup()
        _run(["set-tools", "none"])
        sentinel = {"choices": [{
            "message": {"role": "assistant", "content": "ok"},
            "finish_reason": "stop",
        }], "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                      "total_tokens": 2}}

        seen_payload = []

        def fake_chat(self_inst, payload):
            self_inst.last_usage = {"prompt_eval_count": 1, "eval_count": 1}
            seen_payload.append(payload)
            return sentinel

        with patch.object(cli.ChatClient, "chat", autospec=True,
                          side_effect=fake_chat):
            out = _run(["turn", "ntest", "--first",
                        "--message", "hi", "--cwd", str(tmp_path)])

        assert out["tools_enabled"] == []
        # Payload must not carry ``tools`` or ``tool_choice`` when
        # tools are disabled.
        assert "tools" not in seen_payload[0]
        assert "tool_choice" not in seen_payload[0]

    def test_subset_passed_to_payload(self, tmp_path: Path):
        self._setup()
        _run(["set-tools", "read_file,grep"])
        sentinel = {"choices": [{
            "message": {"role": "assistant", "content": "ok"},
            "finish_reason": "stop",
        }], "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                      "total_tokens": 2}}

        seen_payload = []

        def fake_chat(self_inst, payload):
            self_inst.last_usage = {"prompt_eval_count": 1, "eval_count": 1}
            seen_payload.append(payload)
            return sentinel

        with patch.object(cli.ChatClient, "chat", autospec=True,
                          side_effect=fake_chat):
            out = _run(["turn", "stest", "--first",
                        "--message", "hi", "--cwd", str(tmp_path)])

        assert set(out["tools_enabled"]) == {"read_file", "grep"}
        # Payload must carry exactly those two tools.
        names = {t["function"]["name"]
                 for t in (seen_payload[0].get("tools") or [])}
        assert names == {"read_file", "grep"}
