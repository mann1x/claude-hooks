"""Plan G4, evidence by ref: a judge reads the bytes a lane read, from the
run's read cache, never the disk (``consultants/engine/read_evidence.py``)."""
from __future__ import annotations

import json

from consultants.engine import council, read_evidence as rev
from consultants.engine import typed_results as tr
from consultants.engine.lane_executor import LaneBoundExecutor
from consultants.server.tool_cache import SharedReadCache

FILE = {"a.py": ["import os", "", "def foo(x):", "    return x + 1"]}


def _reader(name, raw_args, cwd, **kw):
    if name != "read_file":
        return "ok"
    a = json.loads(raw_args)
    lines = FILE[a["path"]]
    s = int(a.get("start_line") or 1)
    e = int(a.get("end_line") or len(lines))
    body = "\n".join(f"{n:>6}: {lines[n - 1]}" for n in range(s, e + 1))
    return f"{a['path']}:{s}-{e}  (file has {len(lines)} lines)\n{body}"


def _cache(tmp_path):
    (tmp_path / "a.py").write_text("\n".join(FILE["a.py"]))
    return SharedReadCache(_reader)


def _read(cache, tmp_path, lane="researcher:0", **args):
    return LaneBoundExecutor(cache, lane)(
        "read_file", json.dumps({"path": "a.py", **args}), str(tmp_path))


def _ask(cache, tmp_path, **args):
    return rev.answer(cache, json.dumps({"path": "a.py", **args}),
                      str(tmp_path))


def test_lines_a_lane_read_come_back_as_it_saw_them(tmp_path):
    cache = _cache(tmp_path)
    _read(cache, tmp_path, start_line=1, end_line=3)
    out = _ask(cache, tmp_path, start_line=3)
    assert out == "a.py:3-3 as read by the lanes\n     3: def foo(x):"


def test_lines_no_lane_read_are_named(tmp_path):
    cache = _cache(tmp_path)
    _read(cache, tmp_path, start_line=1, end_line=2)
    out = _ask(cache, tmp_path, start_line=1, end_line=4)
    assert "[unread by any lane: 3-4]" in out
    assert "     1: import os" in out and "def foo" not in out


def test_a_file_nobody_read_is_an_error_not_a_disk_read(tmp_path):
    out = _ask(_cache(tmp_path), tmp_path, start_line=1)
    assert out.startswith("error: no lane read a.py")


def test_a_diverged_lanes_private_reads_are_not_evidence(tmp_path):
    cache = _cache(tmp_path)
    lane = LaneBoundExecutor(cache, "researcher:1")
    lane("run_command", '{"command": "true"}', str(tmp_path))   # its view moved
    _read(cache, tmp_path, lane="researcher:1", start_line=1, end_line=4)
    assert _ask(cache, tmp_path, start_line=1).startswith("error:")


def test_bad_arguments_are_refused(tmp_path):
    cache = _cache(tmp_path)
    assert rev.answer(cache, '{"path": "a.py"}', "").startswith("error:")
    assert _ask(cache, tmp_path, start_line=3, end_line=1).startswith("error:")


def test_offered_only_on_the_typed_protocol_with_a_cache(tmp_path):
    cache = _cache(tmp_path)
    ex = LaneBoundExecutor(cache, "critic:0")
    specs = [{"type": "function", "function": {"name": "read_file"}}]
    assert rev.with_evidence({}, specs, ex) == (specs, ex)          # parity
    typed = {"runtime_control": {"typed_protocol": True}}
    assert rev.with_evidence(typed, None, ex) == (None, ex)         # no tools
    assert rev.with_evidence(typed, specs, _reader) == (specs, _reader)
    s2, ex2 = rev.with_evidence(typed, specs, ex)
    assert [t["function"]["name"] for t in s2] == ["read_file", rev.EVIDENCE_TOOL]
    _read(cache, tmp_path, start_line=1, end_line=4)
    assert "def foo" in ex2(rev.EVIDENCE_TOOL,
                            '{"path": "a.py", "start_line": 3}', str(tmp_path))
    assert ex2("read_file", '{"path": "a.py"}', str(tmp_path)).startswith("a.py:1-4")


def test_the_critic_is_offered_evidence_on_the_typed_protocol(tmp_path):
    from tests.test_typed_results import FakeChat, VERDICT, _call, _resp
    chat = FakeChat([_resp(calls=[_call(tr.VERDICT, VERDICT)])])
    state = council.initial_state(question="q", cwd=str(tmp_path),
                                  models={"critic": "m"}, topology="council",
                                  effort="high")
    state.update(plan="1. look", research=["- r (`a.py:1`)"],
                 research_rounds_used=1, critic_reroutes_used=0)
    state["runtime_control"] = {"typed_protocol": True}
    council.critic_node(
        state, chat_client=chat, model="m", cwd=str(tmp_path),
        tool_specs=[{"type": "function", "function": {
            "name": "read_file", "parameters": {"type": "object"}}}],
        tool_executor=LaneBoundExecutor(_cache(tmp_path), "critic:0"))
    names = [t["function"]["name"] for t in chat.calls[0]["tools"]]
    assert rev.EVIDENCE_TOOL in names
