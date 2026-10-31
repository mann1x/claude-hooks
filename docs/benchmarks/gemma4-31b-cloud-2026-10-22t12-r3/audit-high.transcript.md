# Consultation transcript — csl-2026-10-22-1904-3e4c

_Question_: In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:

1. CouncilState's additive reducers (consultants/engine/graph.py)
2. consultants/server/runner.py's compiled.stream loop
3. The synthesizer's input messages — what does it actually see?
4. The storage.py artifact writer

Answer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line.


## Planner

The failure path for an unhandled exception (e.g., an HTTP timeout or non-string tool output) in a parallel researcher lane is as follows:

1.  **Exception Trigger**: The exception occurs inside `claude_hooks/agent_loop/runner.py:230` during the `chat_fn(payload)` call (if the `tool_executor` returned a non-string, the LLM client will fail to serialize the tool message content) or during `tool_executor` execution at `claude_hooks/agent_loop/runner.py:122`.
2.  **Researcher Node Catch**: The exception propagates to `consultants/engine/council.py:555`, where the `researcher_node`'s `try...except` block catches it. It returns a "tombstone" dictionary containing `{"error": f"researcher failed: {e}", "_role_failed": "researcher", "research": [err_text], ...}` (consultants/engine/council.py:563-573).
3.  **Additive Reducers**: LangGraph merges this result into the `CouncilState`. The `research` key uses `operator.add` (consultants/engine/graph.py:64), so the error string `(researcher lane failed: ...)` is concatenated into the list of research reports.
4.  **Synthesizer Input**: The `synthesizer_node` (consultants/engine/council.py:718) calls `build_synthesizer_messages`, which includes `state.get("research") or []` (consultants/engine/council.py:246) in the prompt. The synthesizer sees the failure message as one of the findings and attempts to synthesize the final answer using only the surviving lanes.
5.  **Runner Logic**: In `consultants/server/runner.py:172`, `node_error` is assigned `final_state.get("error")`. Since the failed researcher lane populated this key, `node_error` is truthy.
6.  **Status & Storage**: The runner sets `terminal_status = "failed"` (consultants/server/runner.py:174). The `storage.ConsultationResult` is created with `status="failed"` and `error=node_error` (consultants/server/runner.py:189-190), and then persisted via `storage.write_consultation` (consultants/server/runner.py:199).

**Conclusion**: The council **produces a degraded answer** (the synthesizer still runs and answers based on partial evidence), but the entire consultation **surfaces as `status=failed`**. The user perceives a system error, although a partially correct `final_answer` may still be present in the artifacts.

**Recommended Hardening**:
Remove the `"error"` key from the `researcher_node` failure return to prevent a single lane failure from marking the entire consultation as failed. The `_role_failed` key and the tombstone in the `research` list are sufficient for logging and synthesizer awareness.

**Path:line**: `consultants/engine/council.py:564` (Remove `"error": f"researcher failed: {e}",`)

---

## Researcher (round 1)

The failure path for an unhandled exception in a parallel researcher lane is as follows:

### 1. Exception Trigger & Catch
An exception (e.g., HTTP timeout or serialization error) occurs inside `claude_hooks/agent_loop/runner.py:230` (the `chat_fn(payload)` call) or `claude_hooks/agent_loop/runner.py:122` (`tool_executor` execution). This exception propagates up to `consultants/engine/council.py:547`, where it is caught by the `try...except` block in `researcher_node`.

### 2. Tombstone Return & Reducers
Instead of crashing the graph, `researcher_node` returns a "tombstone" state update (`consultants/engine/council.py:563-573`):
- `error`: `"researcher failed: <exception>"`
- `_role_failed`: `"researcher"`
- `research`: `["(researcher lane failed: <exception>)"]`
- `turns`: A `RoleTurn` containing the error text.

In `consultants/engine/graph.py:64`, the `research` key is defined with `Annotated[list, operator.add]`. LangGraph's reducer concatenates the tombstone's list into the global `CouncilState`. The resulting `state["research"]` contains a mix of successful reports from surviving lanes and error strings from failed lanes.

### 3. Synthesizer Input & perceived failure
The synthesizer's input is constructed in `consultants/engine/council.py:245-266`. It iterates over `state.get("research")` and appends every entry to the prompt (`consultants/engine/council.py:253-254`). 

The synthesizer sees:
- `RESEARCHER REPORT (round 1): <successful findings from lane A>`
- `RESEARCHER REPORT (round 1): (researcher lane failed: <exception>)`

Because the synthesizer is instructed to "write the final answer" based on the research provided, it typically produces a **degraded but coherent answer**. It will synthesize what it can from the surviving lanes and may explicitly mention that some research failed or simply ignore the error string if the other lanes provided sufficient info.

### 4. Artifact Writing & Status
The `consultants/server/runner.py` loop (`consultants/server/runner.py:137-141`) collects the final merged state into `final_state`.
- `terminal_status` is set to `"failed"` if `final_state.get("error")` is present (`consultants/server/runner.py:174`). Since the tombstone set `error`, the consultation is marked as **`status="failed"`** in `metadata.json` (`consultants/engine/storage.py:108`).
- However, a `final_answer` is still produced by the synthesizer and written to `summary.md` (`consultants/engine/storage.py:119`).

**Perceived Failure Mode:** The user receives a valid answer in `summary.md` (though potentially missing data), but the system logs and `metadata.json` report a failure.

### Hardening Recommendation
Currently, the synthesizer is not explicitly told that a "lane failure" string is an error; it just sees it as a report. 

**Change:** Update `consultants/engine/council.py:254` to explicitly label failed lanes in the synthesizer's prompt so the model can provide a warning to the user about the missing data.

**Recommended edit in `consultants/engine/council.py`:**
```python
# Replace line 254:
# parts.append(f"\nRESEARCHER REPORT (round {i}):\n{r.strip()}")

# With:
is_fail = r.strip().startswith("(researcher lane failed")
label = "RESEARCHER REPORT" if not is_fail else "RESEARCHER LANE FAILURE"
parts.append(f"\n{label} (round {i}):\n{r.strip()}")
```

---

## Researcher (round 1)

The failure path for an unhandled exception in a parallel researcher lane is as follows:

1.  **Additive Reducer (CouncilState)**: The `researcher_node` catches the exception from `loop_runner` (which wraps `run_loop`) at `consultants/engine/council.py:555`. It returns a "tombstone" dictionary: `{"error": f"researcher failed: {e}", "_role_failed": "researcher", "research": [err_text], ...}` (`consultants/engine/council.py:563-573`). Because `research` is an `Annotated[list, operator.add]` reducer (`consultants/engine/graph.py:64`), the failure string `"(researcher lane failed: ...)"` is appended to the global research list, preserving the failure for downstream nodes.

2.  **Runner Loop**: The `compiled.stream` loop in `consultants/server/runner.py:137-141` captures the final merged state into `final_state`. The exception inside the node was already caught and converted to a state update, so the `stream` loop completes normally without hitting the `except Exception` block at `consultants/server/runner.py:155`.

3.  **Synthesizer Input**: The `synthesizer_node` calls `build_synthesizer_messages` (`consultants/engine/council.py:718`), which iterates over `state.get("research") or []` (`consultants/engine/council.py:253-254`). The synthesizer sees the failure string `"(researcher lane failed: ...)"` as one of the researcher reports and attempts to synthesize the final answer using only the findings from the surviving lanes.

4.  **Storage Artifact Writer**: After the graph finishes, the runner checks `node_error = final_state.get("error")` (`consultants/server/runner.py:172`). Since the failed researcher lane populated the `"error"` key, `node_error` is truthy. The runner sets `terminal_status = "failed"` (`consultants/server/runner.py:174`) and creates a `storage.ConsultationResult` with `status="failed"` and `error=node_error` (`consultants/server/runner.py:189-190`).

**Conclusion**: The council **produces a degraded answer** (the synthesizer still runs and provides a response based on partial evidence), but the entire consultation **surfaces as `status=failed`**. The user perceives a system error (via the `status` and `error` fields in the artifacts), even though a `final_answer` was generated.

**Recommended Hardening**:
Remove the `"error"` key from the `researcher_node` failure return. This prevents a single lane failure from marking the entire consultation as `failed` while still allowing the synthesizer and logs to see the failure via the `research` list and `_role_failed` key.

**Path:line**: `consultants/engine/council.py:564` (Remove `"error": f"researcher failed: {e}",`)

---

## Critic (round 2)

DECISION: ready

The evidence is sufficient to trace the failure path. The researcher's claims are verified against the provided code. The logic flow is: an exception in a researcher lane is caught in `consultants/engine/council.py:555`, returning a tombstone dictionary containing an `"error"` key (`consultants/engine/council.py:564`). This error is merged into the global state. While the synthesizer still receives the error string via the `research` list reducer (`consultants/engine/graph.py:64`) and produces a degraded answer (`consultants/engine/council.py:254`), the server runner sees the `"error"` key in the final state (`consultants/server/runner.py:172`) and marks the entire consultation as `status="failed"` (`consultants/server/runner.py:174`), which is then persisted (`consultants/server/runner.py:189-199`).

CORRECTIONS:
- report 1: claimed `consultants/engine/council.py:547` as where the exception is caught — actual `consultants/engine/council.py:555` (the `except` block).
- report 1: claimed `consultants/engine/council.py:253-254` contains `.get` — actual `consultants/engine/council.py:253-254` is a loop over `research_rounds`.
- report 2: claimed `consultants/engine/council.py:555` is where `loop_runner` is caught — actual `consultants/engine/council.py:555` is the `except` block; the call is at `consultants/engine/council.py:548`.

---

## Synthesizer

The council produces a degraded answer, but the entire consultation surfaces as `status=failed`.

*   **CouncilState's additive reducers**: The `research` key is defined with `Annotated[list, operator.add]` in `consultants/engine/graph.py:64`. When a researcher lane fails, the `researcher_node` catches the exception at `consultants/engine/council.py:555` and returns a tombstone list containing the error string `(researcher lane failed: ...)`. This string is appended to the global `research` list via the reducer.
*   **`consultants/server/runner.py`'s compiled.stream loop**: The loop (`consultants/server/runner.py:137-141`) finishes normally without raising an exception because the error was handled inside the node and returned as a state update.
*   **Synthesizer's input**: The `build_synthesizer_messages` function iterates over research rounds at `consultants/engine/council.py:253-254`, inserting the failure string directly into the prompt as a research report. The synthesizer synthesizes the final answer using only the surviving reports.
*   **The storage.py artifact writer**: The runner checks `final_state.get("error")` at `consultants/server/runner.py:172`. Because the tombstone populated this key at `consultants/engine/council.py:564`, the runner sets `terminal_status = "failed"` at `consultants/server/runner.py:174` and persists this failed status via `storage.write_consultation` at `consultants/server/runner.py:189-199`.

**Failure Mode**: The user perceives a system failure (via the `status` and `error` fields in the consultation metadata), even though a partially correct `final_answer` is still generated and written to `summary.md`.

**Recommended Hardening**:
Remove the `"error"` key from the `researcher_node` failure return to prevent a single lane failure from marking the entire consultation as failed.
`consultants/engine/council.py:564` (Remove `"error": f"researcher failed: {e}",`)

---
