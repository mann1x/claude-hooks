"""Plan D acceptance: no guard path pauses or asks. Every terminal action a
guard takes is one forced report (tools off, think off), never a wait on a
person — the council runs unattended."""
from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GUARDS = ("reasoning_loop_guard", "reasoning_repetition", "loop_detection",
          "struggle_supervisor", "silent_turns")
#: What waiting on a person looks like anywhere in this codebase.
WAITS = {"input", "interrupt", "request_approval", "ask_human", "wait_for_approval"}


def _calls(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            yield node, (f.id if isinstance(f, ast.Name)
                         else f.attr if isinstance(f, ast.Attribute) else "")


def test_no_guard_module_can_wait_on_a_person():
    for mod in GUARDS:
        tree = ast.parse((REPO / "claude_hooks" / f"{mod}.py").read_text())
        names = {name for _, name in _calls(tree)}
        assert not names & WAITS, f"{mod} calls {names & WAITS}"
        imports = {a.name for n in ast.walk(tree)
                   if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
        assert "interrupt" not in imports and "langgraph" not in str(imports), mod


def test_every_guard_stop_is_one_forced_report():
    tree = ast.parse((REPO / "claude_hooks/agent_loop/runner.py").read_text())
    reasons = set()
    for node, name in _calls(tree):
        if name == "_forced_report" and len(node.args) > 3:
            arg = node.args[3]
            if isinstance(arg, ast.Constant):
                reasons.add(arg.value)
            else:   # ToolGuards' stop: the reason it recorded
                reasons.add(ast.unparse(arg))
    assert {"reasoning_loop", "struggle", "iteration_cap", "wrap_up",
            "stop['stop_reason']"} <= reasons
    # ToolGuards records only these two, and only as a forced-report reason.
    src = (REPO / "claude_hooks/agent_loop/runner.py").read_text()
    assert '{"stop_reason": "loop_guard"' in src
    assert '{"stop_reason": "mistake_limit"' in src
    [fn] = [n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "_forced_report"]
    assert not {name for _, name in _calls(fn)} & WAITS
