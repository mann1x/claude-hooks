"""``CONSULTANTS_ROLE_TOOL_TURNS``: the tool turns of a planner, critic,
synthesizer or adversary, for one engine process (the measurement arm
behind any change to the 3-turn default)."""
from __future__ import annotations

from consultants.engine import council

SPEC = [{"type": "function", "function": {"name": "read_file",
                                          "parameters": {"type": "object"}}}]
SHELL = SPEC + [{"type": "function", "function": {"name": "run_command",
                                                  "parameters": {"type": "object"}}}]


def test_unset_keeps_the_defaults(monkeypatch):
    monkeypatch.delenv("CONSULTANTS_ROLE_TOOL_TURNS", raising=False)
    assert council._role_tool_iters() == (council.ROLE_TOOL_MAX_ITERATIONS,
                                          council.ROLE_TOOL_FORCE_ANSWER_AFTER)


def test_n_tool_turns_and_one_to_answer(monkeypatch):
    monkeypatch.setenv("CONSULTANTS_ROLE_TOOL_TURNS", "12")
    assert council._role_tool_iters() == (13, 12)
    monkeypatch.setenv("CONSULTANTS_ROLE_TOOL_TURNS", "0")
    assert council._role_tool_iters() == (2, 1)


def test_garbage_keeps_the_defaults(monkeypatch):
    monkeypatch.setenv("CONSULTANTS_ROLE_TOOL_TURNS", "lots")
    assert council._role_tool_iters() == (council.ROLE_TOOL_MAX_ITERATIONS,
                                          council.ROLE_TOOL_FORCE_ANSWER_AFTER)


def test_role_turn_and_judges_follow_it(monkeypatch):
    monkeypatch.setattr(council, "ROLE_TOOL_ITERS", (13, 12))
    seen = []

    def loop_runner(payload, cwd, *, config, **kw):
        seen.append((config.max_iterations, config.force_answer_after))
        return {"choices": [{"message": {"role": "assistant", "content": "x"}}]}

    class Client:
        def chat(self, payload):  # pragma: no cover — loop_runner is faked
            return {}

    council._role_turn(Client(), "m", [{"role": "user", "content": "q"}],
                       role="planner", tool_specs=SPEC,
                       tool_executor=lambda *a: "", cwd="/tmp",
                       loop_runner=loop_runner)
    assert seen == [(13, 12)]
    assert council.judge_iters(SPEC) == (13, 12)
    assert council.judge_iters(SHELL) == (13 + 10, 12 + 10)
