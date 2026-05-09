---
session_id: csl-2026-05-09-0827-d547
created: "2026-05-09T08:27:54"
question: "In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:\n\n1. CouncilState's additive reducers (consultants/engine/graph.py)\n2. consultants/server/runner.py's compiled.stream loop\n3. The synthesizer's input messages — what does it actually see?\n4. The storage.py artifact writer\n\nAnswer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line."
models:
  critic: "deepseek-v4-flash:cloud"
  planner: "deepseek-v4-flash:cloud"
  researcher: "deepseek-v4-flash:cloud"
  synthesizer: "deepseek-v4-flash:cloud"
topology: council
effort: high
duration_seconds: 682.23
status: completed
cwd: /tmp/claude-hooks-bench-bench-baseline-2026-05-07-5MHl
---

The council **does produce an answer**, but it is **degraded** and **status=failed**. The user sees a consultation marked as failed in the dashboard/UI, yet the summary contains a plausible-looking, silently incomplete answer from the surviving lanes.

## Concrete trace (path:line for every claim)

1. **Exception origin** – `agent_loop/runner.py:230` (`chat_fn(payload)`) and `runner.py:300` (`execute_tool_calls(...)`) have no `try/except`. The exception propagates uncaught out of `run_loop` (confirmed: zero `except` hits in that file).

2. **researcher_node catches it** – `council.py:555-573` catches the exception and returns a tombstone dict with `error` and `_role_failed` fields set, plus `research: [err_text]`, `turns: [RoleTurn(...)]`, `research_rounds_used: 1`.

3. **Additive reducers merge across Send lanes** – `graph.py:64-72`: `research`, `turns`, `research_rounds_used`, `total_*_tokens` use `Annotated[..., operator.add]`. The tombstone's `research` text is appended to the successful lanes' findings. Fields `error` (`graph.py:73`) and `_role_failed` (`graph.py:75`) are **not additive** – they are plain `Optional[str]` with LangGraph's default last-writer-wins. Only the failing lane sets them, so they persist in the merged state.

4. **Stream loop completes normally** – `runner.py:135-161` iterates the LangGraph stream. Because the exception was caught _inside_ the node, no exception reaches the loop; the `except Exception` at `runner.py:155` does **not** fire. `final_state` at `runner.py:140` contains the merged dict with `error` set.

5. **Synthesizer sees the error text as a research report** – `council.py:717-723` calls `build_synthesizer_messages(state.get("research") or [])`. `build_synthesizer_messages` (`council.py:245-266`) renders all entries as `RESEARCHER REPORT (round N): <text>`. The failed lane's `"(researcher lane failed: HTTP timeout after 3 retries)"` appears as a normal research report. The synthesizer's system prompt (`council.py:156-167`) says *"Do NOT mention the council, the roles, or the process"* – so it produces a **confidently incomplete answer** that silently ignores the missing lane's scope.

6. **Storage writes status=failed with partial answer** – `runner.py:172-174`:
   ```python
   node_error = final_state.get("error")       # truthy
   node_failed = final_state.get("_role_failed")  # "researcher"
   terminal_status = "failed" if node_error else "completed"
   ```
   Then `runner.py:177-199` builds `ConsultationResult` with `status="failed"`, `error="researcher failed: ..."`, `final_answer=<synthesizer's partial output>`. `storage.write_consultation()` (`storage.py:174-192`) writes all three atomic files successfully – this code is after the stream loop, not inside a try block for the graph crash.

## User-perceived failure mode

- **Dashboard/UI**: Shows `status=failed` (red banner, error indicator).
- **summary.md**: Contains the synthesizer's answer based only on surviving lanes – reads as a complete, definitive answer.
- **metadata.json**: `status: failed`, `error: researcher failed: timeout after 3 retries`.
- **Silent degradation**: The user has no signal that one researcher lane was lost; the answer is missing the scope assigned to that lane, but the model never mentions the gap.

## Recommended hardening change (exactly one)

**File**: `consultants/server/runner.py:174`

**Problem**: The binary `terminal_status` (`"failed"`/`"completed"`) conflates "total failure" with "partial answer from surviving lanes." A single failed researcher lane among N causes the whole consultation to report as failed, hiding a useful partial answer behind a red banner.

**Fix**: Introduce a `"degraded"` status when a researcher lane failed but the synthesizer still produced a non-empty answer:

```python
# Replace line 174:   terminal_status = "failed" if node_error else "completed"
if node_error and node_failed == "researcher" and final_state.get("final_answer", "").strip():
    terminal_status = "degraded"
elif node_error:
    terminal_status = "failed"
else:
    terminal_status = "completed"
```

**Companion changes** (minimal, required for correctness):
- Add `"degraded"` to the `ConsultationResult.status` type hint at `consultants/engine/storage.py:65` (e.g., `Literal["completed", "failed", "degraded"]`).
- Update any UI/API consumer that checks `status` to handle `"degraded"` (e.g., show a yellow/yield banner instead of red).

This change allows the UI to display a nuanced state: "Partially complete — some research lanes failed" while still writing the partial answer, rather than hiding it behind a binary "failed."
