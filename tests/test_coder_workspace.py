"""The coder under ``--shell write``: its own worktree view (plan E).

csl-2026-10-18-1033-5a67: the coder wrote a 19.8 kB test file into
``coder-out/``; nothing ran it, the critic said ready anyway, and it was
missing from the held changes. With a worktree view the coder writes
there, runs what it wrote through ``run_command``, and its files freeze
into the lane's held patch like any lane's.
"""
from __future__ import annotations

import json
from pathlib import Path

from consultants.engine.coder import (CODER_WORKSPACE_NOTE,
                                      build_coder_artifacts_block, coder_node)
from consultants.engine.state_v2 import CoderArtifact, CoderTaskItem

RUN_SPEC = {"type": "function", "function": {"name": "run_command",
                                             "parameters": {"type": "object"}}}


class _Chat:
    def chat(self, payload):  # pragma: no cover — the loop is stubbed
        return {}


def _loop(seen):
    def run(payload, cwd, *, config, tool_specs, chat_fn, tool_executor, **kw):
        seen["specs"] = [t["function"]["name"] for t in tool_specs]
        seen["system"] = payload["messages"][0]["content"]
        seen["iters"] = config.max_iterations
        seen["force"] = config.force_answer_after
        seen["turn_check"] = config.turn_check
        seen["write"] = tool_executor("write_file", json.dumps(
            {"path": "tests/test_x.py", "content": "def test_x():\n    assert 1\n"}), cwd)
        seen["run"] = tool_executor("run_command", json.dumps(
            {"command": "pytest -q tests/test_x.py"}), cwd)
        return {"choices": [{"message": {"role": "assistant",
                                         "content": "wrote tests/test_x.py; 1 passed"},
                             "finish_reason": "stop"}]}
    return run


def _state():
    return {"coder_task_item": CoderTaskItem(task="pin the defect"), "lane_idx": 1,
            "question": "q"}


def test_the_coder_writes_and_runs_in_its_view(tmp_path):
    view = tmp_path / "wt"
    view.mkdir()
    shell_calls, seen = [], {}

    def shell(name, args, cwd):
        shell_calls.append((name, json.loads(args)))
        return "exit=0 wall=0.4s killed_by=none\n--- stdout ---\n1 passed\n"

    out = coder_node(_state(), chat_client=_Chat(), grounding_msgs=[
        {"role": "system", "content": "SYS"}], model="m", cwd=str(tmp_path),
        sid="s", loop_runner=_loop(seen), workspace=lambda: (str(view), None),
        shell_executor=shell, shell_spec=RUN_SPEC)
    a = out["coder_artifacts"][0]
    assert a.error is None and a.workspace == "view"
    assert (view / "tests/test_x.py").read_text().startswith("def test_x")
    assert not (tmp_path / ".claude-hooks").exists()       # not coder-out/
    assert seen["specs"] == ["write_file", "run_command"]
    assert seen["iters"] > 8
    assert shell_calls == [("run_command", {"command": "pytest -q tests/test_x.py"})]
    assert a.commands == [{"command": "pytest -q tests/test_x.py", "exit": "0"}]
    block = build_coder_artifacts_block([a])
    assert "Sandbox changes" in block and "-> exit 0" in block


def test_the_workspace_note_reaches_the_prompt(tmp_path):
    from consultants.engine import coder as C
    msgs = C.build_coder_messages(CoderTaskItem(task="t"), [])
    seen = {}
    coder_node(_state(), chat_client=_Chat(), grounding_msgs=[], model="m",
               cwd=str(tmp_path), sid="s", loop_runner=_loop(seen),
               workspace=lambda: (str(tmp_path), None),
               shell_executor=lambda *a: "exit=0", shell_spec=RUN_SPEC)
    assert msgs[0]["role"] == "system"
    assert seen["system"].endswith(CODER_WORKSPACE_NOTE)


def test_without_a_view_nothing_changes(tmp_path):
    seen = {}
    out = coder_node(_state(), chat_client=_Chat(), grounding_msgs=[], model="m",
                     cwd=str(tmp_path), sid="s", loop_runner=_loop(seen))
    a = out["coder_artifacts"][0]
    assert seen["specs"] == ["write_file"] and a.workspace == ""
    assert "not available to the coder" in seen["run"]
    assert (tmp_path / ".claude-hooks/consultants/s/coder-out/tests/test_x.py").exists()
    assert "NOT verified" in build_coder_artifacts_block([a])


def test_a_view_that_cannot_be_made_falls_back_to_coder_out(tmp_path):
    def broken():
        raise RuntimeError("untracked files too large")
    seen = {}
    out = coder_node(_state(), chat_client=_Chat(), grounding_msgs=[], model="m",
                     cwd=str(tmp_path), sid="s", loop_runner=_loop(seen),
                     workspace=broken, shell_executor=lambda *a: "", shell_spec=RUN_SPEC)
    assert out["coder_artifacts"][0].workspace == ""
    assert seen["specs"] == ["write_file"]


def test_written_files_go_to_the_sandbox_uid(tmp_path, monkeypatch):
    from consultants.engine import coder as C
    owned = []
    monkeypatch.setattr(C.os, "chown", lambda p, u, g: owned.append((Path(p).name, u)))
    sb = C.CoderSandbox(root=tmp_path, max_file_bytes=1000, max_total_bytes=1000,
                        max_files=4, owner=4242)
    sb.write("pkg/sub/f.py", "x")
    assert owned == [("pkg", 4242), ("sub", 4242), ("f.py", 4242)]


def test_the_graph_gives_the_coder_a_shell_only_in_write_mode(monkeypatch):
    from consultants.engine import graph as G
    from consultants.engine import coder as C
    got = {}
    monkeypatch.setattr(C, "coder_node", lambda state, **kw: got.update(kw) or {})
    deps = G.GraphDeps(chat_clients={}, models={}, enabled_roles=("coder",),
                       cwd="/r", tool_executor=lambda *a, **k: "",
                       tool_specs=[RUN_SPEC], sid="s",
                       lane_workspace=lambda lane: ("/v/" + lane, None))
    node = G._wrap_coder(deps, max_file_bytes=1, max_total_bytes=1, max_files=1)
    node({"lane_idx": 2})
    assert got["workspace"]() == ("/v/coder:2", None)
    assert got["shell_spec"] is RUN_SPEC
    got.clear()
    deps.lane_workspace = None
    G._wrap_coder(deps, max_file_bytes=1, max_total_bytes=1, max_files=1)({})
    assert "workspace" not in got and "shell_executor" not in got
