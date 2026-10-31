"""Plan D12: a role is offered the tools it uses (measured)."""
from __future__ import annotations

from types import SimpleNamespace

from claude_hooks.caliber_proxy import tools as caliber_tools
from consultants.engine.state_v2 import ToolPlanItem
from consultants.engine.tool_executor import tool_executor_node


def test_tool_executor_is_not_offered_tools_it_never_calls():
    seen = {}

    def loop(payload, cwd, *, tool_specs, **kw):
        seen["names"] = [t["function"]["name"] for t in tool_specs]
        return {"choices": [{"message": {"role": "assistant",
                                         "content": "EVIDENCE: a.py:1"},
                             "finish_reason": "stop"}]}

    item = ToolPlanItem(intent="find X", why="audit", lane_idx=0,
                        parent_round=1, suggested_tools=["grep"])
    tool_executor_node(
        {"tool_plan_item": item, "lane_idx": 0, "question": "q"},
        chat_client=SimpleNamespace(), tool_executor=lambda *a: "",
        tool_specs=caliber_tools.openai_tool_specs(), grounding_msgs=[],
        model="m", cwd="/tmp", loop_runner=loop)
    assert seen["names"] == ["list_files", "read_file", "glob", "grep"]
    from consultants.engine.tool_executor import UNUSED_TOOLS
    assert UNUSED_TOOLS == {"survey_project", "recall_memory"}
