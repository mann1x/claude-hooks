---
session_id: csl-2026-05-09-0710-3ecb
created: "2026-05-09T07:10:18"
question: "In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:\n\n1. CouncilState's additive reducers (consultants/engine/graph.py)\n2. consultants/server/runner.py's compiled.stream loop\n3. The synthesizer's input messages — what does it actually see?\n4. The storage.py artifact writer\n\nAnswer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line."
models:
  critic: "deepseek-v4-pro:cloud"
  planner: "deepseek-v4-pro:cloud"
  researcher: "deepseek-v4-pro:cloud"
  synthesizer: "deepseek-v4-pro:cloud"
topology: council
effort: high
duration_seconds: 728.71
status: completed
cwd: /tmp/claude-hooks-bench-bench-baseline-2026-05-07-MszE
---

The council produces a degraded but coherent answer, while the consultation is incorrectly marked `status=failed` because a failed lane’s tombstone sets the `error` field to a truthy value at `runner.py:174` — even though the synthesizer ran successfully on the surviving lanes’ evidence.

Each researcher lane that raises inside `loop_runner` is caught by the `except Exception` at `consultants/engine/council.py:555`. It returns a tombstone containing `error: "researcher failed: …"` and `_role_failed: "researcher"` (lines 562–573). Because the `research` channel uses `operator.add` (list concatenation) at `consultants/engine/graph.py:64`, the final merged `research` list contains both the actual findings and error stubs like `"(researcher lane failed: HTTP timeout)"`. The synthesizer then consumes this merged list at `council.py:718–723` (rendered as plain text at lines 245–266) and produces a real `final_answer` — the answer is coherent but may be missing facts the failed lane was tasked to discover.

The stream loop at `consultants/server/runner.py:137–141` collects the merged state normally because no exception escapes the graph. After the stream drains, lines 172–174 check `final_state.get("error")` and unconditionally set `terminal_status = "failed"` when it is truthy. This `terminal_status` is passed to the storage writer at `runner.py:177–199` and written to `summary.md` and `metadata.json` (`storage.py:200–208`). The user therefore receives a real final answer but sees `status: "failed"` and an error description, even though the consultation completed and delivered a usable result. The answer may be missing findings from the crashed lane, but there is no obvious indication that the missing information is due to a lane failure — only the contradiction between a valid answer and a "failed" status.

**Hardening recommendation (single change):** Modify the status decision in `consultants/server/runner.py:172–179` to treat the consultation as `"completed"` when a non‑empty `final_answer` was actually produced, ignoring the tombstone `error` key:

```python
# BEFORE (lines 172-174)
node_error = final_state.get("error")
node_failed = final_state.get("_role_failed")
terminal_status = "failed" if node_error else "completed"

# AFTER (replace lines 172-174 with this block)
node_error = final_state.get("error")
node_failed = final_state.get("_role_failed")
has_answer = bool(
    final_state.get("final_answer", "").strip()
    and not final_state.get("final_answer", "").startswith("(consultation incomplete")
)
terminal_status = "completed" if has_answer else "failed"
```

This preserves the `error` in metadata for debugging but accurately marks the consultation as `"completed"` when a real answer exists, eliminating the confusing `status=failed` label after a successful synthesis.
