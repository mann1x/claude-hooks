"""Plan D4: a tool call the server could not read is re-asked with its cause,
never repaired; arguments are read the way models send them, where exactly
one reading exists."""
from __future__ import annotations

import io
import json
import urllib.error
from unittest.mock import patch

import pytest

from claude_hooks import silent_turns as st
from claude_hooks import tool_args
from claude_hooks.agent_loop import runner
from claude_hooks.caliber_proxy import tools as caliber_tools
from claude_hooks.get_advice import chat_client as cc
from claude_hooks.get_advice.chat_client import ChatClient, ChatFault

XML_ERROR = ("XML syntax error on line 12: element <parameter> closed by "
             "</function>")
REJECTED = json.dumps({"error": {
    "message": "Invalid diff: now finding less tool calls",
    "type": "tool_call_rejected", "reason": "swallowed_key",
    "key": "new_text", "tool": "edit_file"}})


# --- classification ------------------------------------------------------ #

def test_classification():
    assert cc.classify_http_fault(500, json.dumps({"error": XML_ERROR})) \
        == cc.TOOL_CALL_UNPARSABLE
    assert cc.classify_http_fault(500, REJECTED) == cc.TOOL_CALL_UNPARSABLE
    assert cc.classify_http_fault(
        500, "Invalid diff: now finding less tool calls") == cc.TOOL_CALL_UNPARSABLE
    assert cc.classify_http_fault(400, "failed to parse the tool call "
                                       "arguments") == cc.TOOL_CALL_UNPARSABLE
    assert cc.classify_http_fault(500, "runner crashed") == cc.TRANSPORT
    # Prose about XML is not a parser error.
    assert cc.classify_http_fault(400, "XML syntax error on line 2") \
        == cc.BAD_REQUEST


def test_rejection_is_found_nested_and_encoded():
    want = {"reason": "swallowed_key", "key": "new_text", "tool": "edit_file"}
    assert cc.tool_call_rejection(REJECTED) == want
    assert cc.tool_call_rejection({"error": json.dumps(
        json.loads(REJECTED)["error"])}) == want
    assert cc.tool_call_rejection("plain text") is None
    assert cc.tool_call_rejection({"error": {"type": "tool_call_rejected"}}) is None


# --- the client never resends it ----------------------------------------- #

class _Lines:
    def __init__(self, lines):
        self._lines = [json.dumps(x).encode() + b"\n" for x in lines]
        self.headers, self.status = {}, 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        return iter(self._lines)

    def read(self):
        return b"".join(self._lines)


class _Seq:
    def __init__(self, items):
        self.items, self.calls = list(items), 0

    def __call__(self, req, timeout=None):
        self.calls += 1
        item = self.items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setattr(cc.time, "sleep", lambda s: None)
    monkeypatch.setenv("CLAUDE_HOOKS_OLLAMA_SLOTS_DISABLE", "1")


def _http(code, body):
    return urllib.error.HTTPError("http://stub/api/chat", code, "x", {},
                                  io.BytesIO(body.encode()))


def test_http_rejection_is_not_retried():
    seq = _Seq([_http(500, REJECTED)])
    with patch("urllib.request.urlopen", side_effect=seq), \
            pytest.raises(ChatFault) as caught:
        ChatClient("http://stub", max_retries=5).chat(
            {"model": "m", "messages": []})
    assert seq.calls == 1
    assert caught.value.fault_class == cc.TOOL_CALL_UNPARSABLE
    assert caught.value.rejection["key"] == "new_text"


def test_stream_error_line_is_a_fault_with_its_partial():
    seq = _Seq([_Lines([
        {"message": {"role": "assistant", "content": "Fixing it. "}},
        {"error": XML_ERROR}])])
    with patch("urllib.request.urlopen", side_effect=seq), \
            pytest.raises(ChatFault) as caught:
        ChatClient("http://stub", max_retries=5).chat_streamed(
            {"model": "m", "messages": []})
    assert seq.calls == 1
    assert caught.value.fault_class == cc.TOOL_CALL_UNPARSABLE
    assert caught.value.partial == "Fixing it. "


def test_other_stream_error_is_retried_not_read_as_a_reply():
    # It used to be read past: the partial came back as a finished answer.
    seq = _Seq([
        _Lines([{"message": {"role": "assistant", "content": "half"}},
                {"error": "llama runner process has terminated"}]),
        _Lines([{"message": {"role": "assistant", "content": "whole"},
                 "done": True, "done_reason": "stop"}])])
    with patch("urllib.request.urlopen", side_effect=seq):
        out = ChatClient("http://stub", max_retries=5).chat_streamed(
            {"model": "m", "messages": []})
    assert seq.calls == 2
    assert out["choices"][0]["message"]["content"] == "whole"


# --- the loop re-asks it -------------------------------------------------- #

SPEC = [{"type": "function", "function": {"name": "read_file",
                                          "parameters": {"type": "object"}}}]


def _fault(rejection=None, partial=""):
    return ChatFault("rejected", fault_class=cc.TOOL_CALL_UNPARSABLE,
                     rejection=rejection, partial=partial)


def _say(content="", tool_args_text=None, done="stop"):
    msg = {"role": "assistant", "content": content}
    if tool_args_text is not None:
        msg["tool_calls"] = [{"id": "c", "type": "function", "function": {
            "name": "read_file", "arguments": tool_args_text}}]
    # As ``ChatClient`` shapes it: a call present means ``tool_calls``, and
    # ``done_reason`` says verbatim whether the cap ended the turn.
    finish = "tool_calls" if tool_args_text is not None else done
    return {"choices": [{"message": msg, "finish_reason": finish,
                         "done_reason": done}]}


def _run(script, executor=None):
    sent, events, ran = [], [], []

    def chat_fn(payload):
        sent.append(dict(payload, messages=list(payload["messages"])))
        item = script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def run_tool(n, a, c):
        ran.append(a)
        return "body"

    out = runner.run_loop(
        {"model": "m", "messages": [{"role": "user", "content": "q"}]}, "/tmp",
        config=runner.LoopConfig(force_first_tool_call=False,
                                 force_answer_after=0, on_guard=events.append),
        tool_specs=SPEC, chat_fn=chat_fn,
        tool_executor=executor or run_tool)
    return out, sent, events, ran


def test_rejected_call_is_reasked_naming_the_swallowed_key():
    out, sent, events, _ = _run([
        _fault({"reason": "swallowed_key", "key": "new_text",
                "tool": "edit_file"}, partial="I found it."),
        _say("Answer.")])
    assert out["choices"][0]["message"]["content"] == "Answer."
    tail = sent[1]["messages"][-2:]
    assert tail[0] == {"role": "assistant", "content": "I found it."}
    assert tail[1]["_harness"] and "`new_text`" in tail[1]["content"]
    assert "`edit_file` call" in tail[1]["content"]
    assert events[0]["kind"] == "tool_call_unparsable"
    assert events[0]["rejection"]["key"] == "new_text"


def test_generic_reason_gets_the_generic_reminder():
    _, sent, _, _ = _run([_fault(), _say("Answer.")])
    assert sent[1]["messages"][-1]["content"] == st.TOOL_CALL_UNPARSABLE_REMINDER


def test_third_rejection_in_a_row_stands():
    with pytest.raises(ChatFault):
        _run([_fault(), _fault(), _fault()])


def test_a_tool_turn_resets_the_rejection_budget():
    out, _, events, _ = _run([_fault(), _fault(), _say(tool_args_text='{}'),
                              _fault(), _say("Answer.")])
    assert out["choices"][0]["message"]["content"] == "Answer."
    assert len([e for e in events
                if e.get("kind") == "tool_call_unparsable"]) == 3


def test_other_faults_are_not_caught():
    with pytest.raises(ChatFault):
        _run([ChatFault("x", fault_class=cc.BAD_REQUEST)])


def test_arguments_cut_at_the_cap_are_answered_as_cut_not_run():
    out, sent, _, ran = _run([_say(tool_args_text='{"path": "src/a', done="length"),
                              _say("Answer.")])
    assert ran == []
    answer = [m for m in sent[1]["messages"] if m.get("role") == "tool"][-1]
    assert answer["content"].startswith("error: this call was cut off")


def test_bad_json_on_a_normal_turn_still_reaches_the_tool():
    _, _, _, ran = _run([_say(tool_args_text='{"path": "a"', done="stop"),
                         _say("Answer.")])
    assert ran == ['{"path": "a"']


# --- one reading only ----------------------------------------------------- #

GREP = {"type": "object", "properties": {
    "pattern": {"type": "string"}, "case_insensitive": {"type": "boolean"}}}
READ = {"type": "object", "properties": {
    "path": {"type": "string"}, "start_line": {"type": "integer"},
    "ratio": {"type": "number"}}}
LIST = {"type": "object", "properties": {
    "paths": {"type": "array", "items": {"type": "string"}}}}


def test_one_reading_coercions():
    assert tool_args.coerce({"case_insensitive": " FALSE "}, GREP) \
        == {"case_insensitive": False}
    assert tool_args.coerce({"start_line": "108", "ratio": "0.5"}, READ) \
        == {"start_line": 108, "ratio": 0.5}
    assert tool_args.coerce({"start_line": 3.0}, READ) == {"start_line": 3}
    assert tool_args.coerce({"paths": "src/a.py"}, LIST) == {"paths": ["src/a.py"]}
    assert tool_args.coerce({"paths": '["a", "b"]'}, LIST) == {"paths": ["a", "b"]}


def test_no_guessing():
    assert tool_args.coerce({"case_insensitive": "yes"}, GREP) \
        == {"case_insensitive": "yes"}
    assert tool_args.coerce({"start_line": "1.5"}, READ) == {"start_line": "1.5"}
    assert tool_args.coerce({"paths": "a,b"}, LIST) == {"paths": ["a,b"]}
    assert tool_args.coerce({"pattern": "true"}, GREP) == {"pattern": "true"}
    # A regex class opens with a bracket and is not a list.
    assert tool_args.coerce({"paths": "[a-z]+"}, LIST) == {"paths": ["[a-z]+"]}


def test_truncated_array_text_is_refused_by_name():
    refused = tool_args.coerce({"paths": '["a", "b'}, LIST)
    assert isinstance(refused, str) and refused.startswith("error: `paths`")


def test_object_encoded_twice():
    assert tool_args.parse_arguments(json.dumps('{"path": "a"}')) == {"path": "a"}
    assert tool_args.parse_arguments('"just text"').startswith("error:")
    assert tool_args.parse_arguments('{"path": ').startswith("error:")
    assert tool_args.parse_arguments("") == {}


def test_grep_told_false_stays_case_sensitive(tmp_path):
    (tmp_path / "a.txt").write_text("Needle\n")
    out = caliber_tools.execute(
        "grep", json.dumps({"pattern": "needle", "case_insensitive": "false"}),
        str(tmp_path))
    assert "a.txt" not in out
    out = caliber_tools.execute(
        "grep", json.dumps({"pattern": "needle", "case_insensitive": "true"}),
        str(tmp_path))
    assert "a.txt" in out


# --- a name the provider mangled (D4, measured in our transcripts) ------- #

TOOLS = {s["function"]["name"]: s["function"]["parameters"]
         for s in caliber_tools.openai_tool_specs()}


def test_measured_mangled_names_are_recovered():
    seen = {  # verbatim from researcher tool_call rows in transcript.db
        "read_file(path": "read_file", "read_file(end_line": "read_file",
        "read_fileend_line": "read_file", "read_file:end_line=2230": "read_file",
        "read_file:end_line==3875": "read_file", "read_filepath": "read_file",
        "read_file<tool_call>path": "read_file", "read_file_path": "read_file",
        "read_file<tool_call>end_line": "read_file", "read_file'": "read_file",
        "list_files(path": "list_files", "grep_path": "grep",
        "grep_case_insensitive": "grep", "grepcase_insensitive": "grep",
        "glob_pattern_check = glob  # placeholder</think><tool_call>read_file":
            "read_file",
    }
    for sent, want in seen.items():
        assert tool_args.recover_tool_name(sent, TOOLS) == want, sent


def test_names_with_no_single_reading_are_left_alone():
    for sent in ("bash", "exec", "read_files", "grepper", "read_file",
                 "prose<tool_call>read_text", "please_read_file_now"):
        assert tool_args.recover_tool_name(sent, TOOLS) is None, sent


def test_the_loop_renames_a_mangled_call_only_when_its_arguments_parse():
    def turn(name, args):
        return {"choices": [{"message": {"role": "assistant", "content": "",
                "tool_calls": [{"id": "c", "type": "function", "function": {
                    "name": name, "arguments": args}}]},
                "finish_reason": "tool_calls"}]}
    script = [turn("read_file(path", '{"path": "a.py"}'),
              turn("grep_path", '{"pattern": "x"'), _say("Answer.")]
    ran, events = [], []
    runner.run_loop(
        {"model": "m", "messages": [{"role": "user", "content": "q"}]}, "/tmp",
        config=runner.LoopConfig(force_first_tool_call=False, force_answer_after=0,
                                 on_guard=events.append),
        tool_specs=caliber_tools.openai_tool_specs(),
        chat_fn=lambda p: script.pop(0),
        tool_executor=lambda n, a, c: ran.append(n) or "ok")
    # The first is renamed; the second's arguments do not parse, so it is
    # not (the executor answers it as unknown).
    assert ran == ["read_file", "grep_path"]
    assert [e["tool"] for e in events if e.get("guard") == "tool_name"] == ["read_file"]
