"""The evidence bar in code (consultants/engine/evidence.py), from council
run csl-2026-10-18-1120-95ce (mails #1625 / #1627)."""
from __future__ import annotations

import json
import sqlite3

import pytest

from consultants.engine import evidence as E


# The critic texts of run 2, verbatim where it matters.
RUN2_CRITIC = (
    "DECISION: ready\n\nThe researcher's evidence is sufficient. While the "
    "researcher was unable to execute tests due to a read-only environment "
    "(`EROFS` on `node_modules`), the static analysis is deep.\n"
    "CONFIDENCE: 0.9")


class TestCriticHold:

    @pytest.mark.parametrize("said", [
        "the researcher was unable to execute tests due to EROFS",
        "the lanes could not run the vitest tests",
        "No tests were run.",
        "the tests were never executed",
        "the findings stand without running any tests",
        "we failed to run the unit tests",
    ])
    def test_ready_while_tests_were_not_run_is_held(self, said):
        d, text, held = E.hold_to_evidence_bar("ready", f"DECISION: ready\n{said}")
        assert held and d == "needs_more_research"
        assert "[harness] Held at needs_more_research" in text

    @pytest.mark.parametrize("said", [
        "lane 4 ran the tests: 330 pass",
        "The remaining unverified items are listed as UNVERIFIED lines.",
        "tests were run and passed",
        "add a test that pins the newline behaviour",
    ])
    def test_a_ready_that_does_not_say_so_stands(self, said):
        assert E.hold_to_evidence_bar("ready", said) == ("ready", said, False)

    def test_only_ready_is_held(self):
        t = "needs more: could not run tests"
        assert E.hold_to_evidence_bar("needs_more_research", t)[2] is False

    def test_run2_critic_is_held(self):
        assert E.hold_to_evidence_bar("ready", RUN2_CRITIC)[2]


def _db(tmp_path, rows):
    p = tmp_path / "transcript.db"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE events (event_id INTEGER PRIMARY KEY, ts REAL, "
              "kind TEXT, role TEXT, round INT, lane_idx INT, tool TEXT, "
              "args TEXT, output TEXT)")
    for role, lane, args, out in rows:
        c.execute("INSERT INTO events (kind, role, lane_idx, tool, args, "
                  "output) VALUES ('tool_call', ?, ?, 'run_command', ?, ?)",
                  (role, lane, json.dumps(args), out))
    c.execute("INSERT INTO events (kind, role, tool, args, output) VALUES "
              "('tool_call', 'researcher', 'read_file', '{}', 'x')")
    c.commit()
    c.close()
    return p


class TestCommandsBlock:

    def test_test_runs_carry_their_output_and_reads_do_not(self, tmp_path):
        db = _db(tmp_path, [
            ("researcher", 0, {"command": "git show abc"},
             "exit=0 wall=0.1s killed_by=none\n--- stdout (9 chars) ---\nDIFF BODY"),
            ("researcher", 1, {"command": "bun x vitest run"},
             "exit=1 wall=2.0s killed_by=None\n--- stderr (40 chars) ---\n"
             "SyntaxError: Unexpected token '?.'"),
            ("coder", 0, {"argv": ["bun", "test", "a.test.ts"]},
             "exit=0 wall=1.0s killed_by=none\n--- stdout (9 chars) ---\n10 pass"),
        ])
        b = E.commands_block(db)
        assert "only these were executed" in b
        assert "`bun x vitest run` → exit 1" in b
        assert "Unexpected token '?.'" in b
        assert "`bun test a.test.ts` → exit 0\n      10 pass" in b
        assert "`git show abc` → exit 0" in b and "DIFF BODY" not in b
        assert "killed" not in b
        assert "read_file" not in b

    def test_no_command_says_so(self, tmp_path):
        b = E.commands_block(_db(tmp_path, []))
        assert "no lane ran a command" in b

    def test_no_transcript_is_no_block(self, tmp_path):
        assert E.commands_block(tmp_path / "none.db") == ""
        assert E.commands_block(None) == ""

    def test_a_timeout_is_named(self, tmp_path):
        b = E.commands_block(_db(tmp_path, [
            ("researcher", 2, {"command": "pytest"},
             "exit=-9 wall=120.0s killed_by=timeout (the 120s wall clock ran "
             "out; it was killed)\n(no output)")]))
        assert "exit -9, killed by timeout" in b


class TestUnverifiedSection:

    @pytest.mark.parametrize("ans", [
        "## Defects\n1. x\n\n## Unverified\n(none)",
        "**Unverified**\n- a: run the test",
        "### unverified claims\n- b",
    ])
    def test_a_section_is_recognised(self, ans):
        assert not E.needs_unverified_revision(ans)
        assert E.with_unverified_stub(ans) == ans

    def test_none_gets_the_stub(self):
        out = E.with_unverified_stub("## Defects\n1. newline drift")
        assert out.rstrip().endswith("cited read as unverified._")
        assert "## Unverified" in out
        assert not E.needs_unverified_revision("")


def _resp(text):
    return {"choices": [{"message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1}}


class _Seq:
    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    def chat(self, payload, *, think=True, **_kw):
        self.calls.append(payload)
        return _resp(self.replies.pop(0) if self.replies else "")


def _state(**kw):
    return {"question": "q", "plan": "p", "research": ["REPORT"],
            "critique": "ok", "cwd": "", **kw}


class TestSynthesizerNode:

    def test_off_is_one_call_and_unchanged(self):
        from consultants.engine.council import synthesizer_node
        c = _Seq("ANSWER without the section")
        out = synthesizer_node(_state(), chat_client=c, model="m")
        assert len(c.calls) == 1
        assert out["final_answer"] == "ANSWER without the section"
        assert "COMMANDS THE LANES RAN" not in json.dumps(c.calls[0])

    def test_with_the_shell_it_revises_once_and_sees_the_commands(self, tmp_path):
        from consultants.engine.council import synthesizer_node

        class Rec:
            db_path = _db(tmp_path, [("researcher", 0, {"command": "bun test"},
                                      "exit=0 wall=1s killed_by=none\n--- stdout (6 chars) ---\n3 pass")])

            def record_node(self, **kw):
                pass

            def record_llm_call(self, **kw):
                pass

        c = _Seq("## Defects\n1. drift", "## Defects\n1. drift\n\n## Unverified\n"
                 "- drift: run insertAtColumn on 'abc\\ndef'")
        out = synthesizer_node(_state(), chat_client=c, model="m",
                               recorder=Rec(), shell=True)
        assert len(c.calls) == 2
        assert "COMMANDS THE LANES RAN" in json.dumps(c.calls[0])
        assert "`bun test` \\u2192 exit 0" in json.dumps(c.calls[0])
        assert "no `Unverified` section" in json.dumps(c.calls[1])
        assert "## Unverified\n- drift" in out["final_answer"]

    def test_a_revision_that_still_has_none_gets_the_stub(self):
        from consultants.engine.council import synthesizer_node
        c = _Seq("ANSWER", "ANSWER again")
        out = synthesizer_node(_state(), chat_client=c, model="m", shell=True)
        assert out["final_answer"].startswith("ANSWER again")
        assert "## Unverified" in out["final_answer"]

    def test_a_checkpoint_brief_alone_turns_it_on(self):
        from consultants.engine.council import synthesizer_node
        from consultants.engine.state_v2 import Doc
        c = _Seq("ANSWER", "ANSWER\n\n## Unverified\n(none)")
        synthesizer_node(_state(additional_context=[
            Doc(role="synthesizer", text="brief", ts=0.0, source="adversary")]),
            chat_client=c, model="m")
        assert len(c.calls) == 2
        assert "COMMANDS THE LANES RAN" not in json.dumps(c.calls[0])


class TestCriticNode:

    def test_the_critic_is_held_on_a_shell_run_only(self):
        from consultants.engine.council import critic_node
        for shell, want in ((False, "ready"), (True, "needs_more_research")):
            out = critic_node(_state(), chat_client=_Seq(RUN2_CRITIC),
                              model="m", shell=shell)
            assert out["critic_decision"] == want
            assert out["critic_reroutes_used"] == (1 if shell else 0)


def test_the_synthesizer_copy_of_a_brief_asks_for_attribution():
    from consultants.server.control_routes import _with_synthesizer_copy
    from consultants.engine.state_v2 import Doc

    class Inj:
        pass
    d = _with_synthesizer_copy({"additional_context": [
        Doc(role="researcher", text="X", ts=0.0, source="adversary")]},
        Inj(), "researcher")
    assert "(from the checkpoint brief, not re-verified)" in \
        d["additional_context"][1].text


def test_the_graph_gives_the_roles_the_bar_when_lanes_have_the_shell():
    pytest.importorskip("langgraph.graph")
    from langgraph.checkpoint.memory import MemorySaver
    from consultants.engine.graph import GraphDeps, build_council_graph

    def run(specs):
        crit, syn = _Seq(RUN2_CRITIC, "DECISION: ready"), _Seq("A", "A2")
        deps = GraphDeps(
            chat_clients={"planner": _Seq("1. one thing"),
                          "researcher": _Seq("R1", "R2", "R3"),
                          "critic": crit, "synthesizer": syn},
            models={r: "m" for r in ("planner", "researcher", "critic",
                                     "synthesizer")},
            enabled_roles=("planner", "researcher", "critic", "synthesizer"),
            cwd="/tmp", tool_executor=lambda *a, **kw: "", tool_specs=specs,
            grounding_msgs=[], disable_cache=True)
        g = build_council_graph(deps, checkpointer=MemorySaver())
        g.invoke({"question": "q", "cwd": "/tmp", "models": deps.models,
                  "topology": "council", "effort": "high",
                  "runtime_control": {"max_rounds": 3}},
                 config={"configurable": {"thread_id": "eb"}})
        return len(crit.calls), len(syn.calls)

    shell = [{"type": "function", "function": {"name": "run_command",
                                               "parameters": {}}}]
    assert run([]) == (1, 1)          # off: ready stands, one answer
    assert run(shell) == (2, 2)       # held -> another round; revised
