# Consultation transcript — csl-2026-05-07-0952-b0fc

_Question_: In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:

1. CouncilState's additive reducers (consultants/engine/graph.py)
2. consultants/server/runner.py's compiled.stream loop
3. The synthesizer's input messages — what does it actually see?
4. The storage.py artifact writer

Answer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line.


## Planner

1. Trace the call stack of `agent_loop.runner.run_loop` to identify the immediate try-except blocks and determine if exceptions are caught or propagated to the graph executor.
2. Inspect `consultants/engine/graph.py` to see how additive reducers in `CouncilState` process results from failed nodes (e.g., if they filter `None` or propagate error objects).
3. Analyze the `compiled.stream` loop in `consultants/server/runner.py` to verify if a node failure terminates the entire stream or allows other nodes to complete.
4. Examine the synthesizer's input assembly logic to determine if it receives a partial list of messages when one parallel researcher lane fails.
5. Review `storage.py` to check if the artifact writer persists partial results or requires a complete successful graph run.
6. Verify the final response status logic in `consultants/server/runner.py` to see if a partial failure results in `status=failed` or a successful response with degraded content.

---

## Researcher (round 1)

### Failure Path Analysis: Researcher Lane Exception

**1. Exception Propagation from `agent_loop.runner.run_loop`**
*   `claude_hooks/agent_loop/runner.py` has **no** top-level `try-except` block around its main loop or tool execution (`execute_tool_calls` at `line 95` and the chat call at `line 230`). Any unhandled exception (HTTP timeout, non-string tool output) propagates directly to the caller.
*   In `consultants/engine/council.py`, the `researcher_node` wraps the `loop_runner` call in a `try-except` block (`lines 547-555`).
*   **Immediate Result**: The exception is caught at the node level. The researcher returns a "tombstone" state update (`lines 563-573`) containing:
    *   `"error": f"researcher failed: {e}"`
    *   `"_role_failed": "researcher"`
    *   `"research": [f"(researcher lane failed: {e})"]`
    *   A `RoleTurn` recording the failure text.

**2. CouncilState's Additive Reducers (`consultants/engine/graph.py`)**
*   The `CouncilState` uses `operator.add` for the `research` channel (`graph.py:64`).
*   When parallel lanes are fanned out via `Send` (`graph.py:359`), LangGraph merges the partial results.
*   **Result**: The failure string `"(researcher lane failed: ...)"` is appended to the `research` list. Other successful lanes' reports are also preserved in the list.

**3. Synthesizer's Input Messages (`consultants/engine/council.py`)**
*   The synthesizer's prompt is built using `build_synthesizer_messages` (`lines 245-266`), which iterates over the `research_rounds` list (`lines 253-254`).
*   **Synthesizer's View**: The synthesizer sees the full list of research reports, including the specific string `"(researcher lane failed: ...)"` for the crashed lane. It does **not** see the `error` or `_role_failed` keys (as they are not passed into the prompt builder).
*   **Behavior**: The synthesizer treats the failure string as "evidence" that a specific part of the research failed and attempts to write the final answer based on the surviving lanes' data.

**4. Storage and User Perception (`consultants/server/runner.py` & `consultants/engine/storage.py`)**
*   The `runner.py` stream loop (`lines 137-141`) captures the `final_state`.
*   The `terminal_status` is determined by `node_error = final_state.get("error")` (`runner.py:172`).
*   Since the `researcher_node` set the `error` key (`council.py:564`), `terminal_status` becomes `"failed"` (`runner.py:174`).
*   **Artifact Writer**: `storage.write_consultation` is called. `summary.md` will contain the synthesizer's answer (which may be degraded but coherent), but `metadata.json` and the YAML front-matter of `summary.md` will show `status: failed` and the exception message (`storage.py:108`).
*   **User Perception**: The council produces a **degraded but coherent answer** (based on surviving lanes). However, the system reports the overall consultation as `status=failed`.

### Summary
The council **does not** fail entirely; it produces a degraded answer. The failure mode is a "silent" loss of one research dimension, flagged as a system failure in metadata but appearing as a partial research report to the synthesizer.

### Recommended Hardening
The `researcher_node` currently marks the entire consultation as `failed` if a single lane crashes. To allow the council to gracefully degrade and report `completed` when the synthesizer can still provide a useful answer from other lanes:

**Change**: In `consultants/engine/council.py`, modify the `researcher_node` exception handler to avoid setting the global `error` key if other lanes are likely to provide coverage, or move the failure marker to a non-terminal field.

**Specific Recommendation**:
In `consultants/engine/council.py:564`, change:
`"error": f"researcher failed: {e}",` 
to 
`"lane_error": f"researcher failed: {e}",`
and update `consultants/server/runner.py:172` to only trigger `terminal_status = "failed"` for critical roles (planner/synthesizer) rather than any node error.

---

## Researcher (round 1)

The failure path for an unhandled exception in a parallel researcher lane (e.g., HTTP timeout in `run_loop`) is as follows:

1.  **Exception Catch**: The exception is raised inside the `loop_runner` call within `researcher_node`. It is caught by the `try...except Exception as e` block at `consultants/engine/council.py:555`.
2.  **Tombstone Return**: The node returns a "tombstone" state update. Crucially, it sets both the global `"error"` key and the lane-specific `"_role_failed"` key at `consultants/engine/council.py:564-565`. It also appends the error text to the `research` list at `consultants/engine/council.py:566`.
3.  **Additive Reduction**: Because `CouncilState` defines `research` and `turns` as additive reducers (`operator.add` at `consultants/engine/graph.py:64, 68`), the failed lane's error message is concatenated with the successful reports from other lanes.
4.  **Synthesizer Input**: The `synthesizer_node` consumes the merged `research` list at `consultants/engine/council.py:720`. It sees the reports from successful lanes plus the failure string (e.g., `"(researcher lane failed: ...)"`). Since the other lanes provide data, the synthesizer typically produces a **coherent, though potentially degraded, final answer**.
5.  **Status Determination**: In `consultants/server/runner.py`, the runner retrieves `node_error = final_state.get("error")` at `consultants/server/runner.py:172`. Because the failed researcher node set this key, `terminal_status` is set to `"failed"` at `consultants/server/runner.py:174`.
6.  **Artifact Writing**: The `storage.ConsultationResult` is constructed with `status="failed"` at `consultants/server/runner.py:189`. The `storage.py` writer then persists this status into `metadata.json` (`consultants/engine/storage.py:168`) and the `summary.md` YAML front-matter (`consultants/engine/storage.py:108`).

**Conclusion**: The council **still produces a coherent answer** (provided at least one lane succeeded), but the entire consultation surfaces as **`status=failed`**. The user perceives a successful answer in the UI/file but sees a "failed" status in the metadata.

**Recommended Hardening**:
Remove the global `"error"` key from the researcher's tombstone return to allow degraded-but-successful completions. Rely on `_role_failed` for logging and the `research` list for the synthesizer to detect the gap.
**Change**: Remove line `consultants/engine/council.py:564` (`"error": f"researcher failed: {e}",`).

---

## Researcher (round 1)

### Failure Path Trace: Unhandled Exception in Researcher Lane

If an unhandled exception occurs inside `agent_loop.runner.run_loop` (e.g., HTTP timeout, tool return type error), the failure propagates as follows:

**1. CouncilState Reducers (`consultants/engine/graph.py`)**
*   The exception is caught by the `researcher_node` wrapper's `try...except` block at `consultants/engine/council.py:555`.
*   Instead of crashing the graph, the node returns a "tombstone" delta:
    *   `"error": f"researcher failed: {e}"` (`consultants/engine/council.py:564`)
    *   `"research": [err_text]` (`consultants/engine/council.py:566`), where `err_text` is `(researcher lane failed: {e})`.
*   Because `CouncilState.research` is defined with `operator.add` (`consultants/engine/graph.py:64`), this error string is **appended** to the list of research findings. Other parallel lanes that succeeded continue to contribute their reports to the same list.

**2. Runner Stream Loop (`consultants/server/runner.py`)**
*   The `compiled.stream` loop (`consultants/server/runner.py:137`) does **not** crash because the exception was handled inside the `researcher_node`.
*   The stream completes normally. The `final_state` is captured from the `"values"` mode (`consultants/server/runner.py:139`).
*   The runner detects the mid-pipeline error via `node_error = final_state.get("error")` (`consultants/server/runner.py:172`).
*   The `terminal_status` is set to `"failed"` (`consultants/server/runner.py:174`) because `node_error` is present.

**3. Synthesizer's Input Messages**
*   The synthesizer receives the full `research` list via `build_synthesizer_messages` (`consultants/engine/council.py:245`).
*   The synthesizer sees the actual reports from successful lanes **plus** the error string `(researcher lane failed: ...)` for the failed lane.
*   It produces a `final_answer` based on the available (degraded) evidence.

**4. Artifact Writer (`consultants/engine/storage.py`)**
*   The runner creates a `ConsultationResult` with `status="failed"` and `error=node_error` (`consultants/server/runner.py:189, 190`).
*   `storage.write_consultation` (`consultants/engine/storage.py:200`) writes the `summary.md`, `transcript.md`, and `metadata.json`.
*   The `summary.md` contains the synthesizer's degraded answer but is tagged with `status: failed` in the YAML front-matter (`consultants/engine/storage.py:108`).

### Conclusion
The council **produces a degraded answer** rather than a total failure. The user perceives a successful-looking response (the answer is present), but the metadata/status is `failed`. The failure mode is **silent degradation**: the synthesizer tries to make sense of a partial dataset, potentially hallucinating or omitting facts that would have been found by the crashed lane.

### Recommended Hardening
Currently, the synthesizer is not explicitly told that some lanes failed; it just sees a weird string in the research list. To prevent "confidently degraded" answers, the synthesizer should be explicitly alerted to the failure.

**Change:** In `consultants/engine/council.py`, modify `build_synthesizer_messages` to check for the `error` key in state and append a warning to the prompt.

**Path:** `consultants/engine/council.py:245` (within `build_synthesizer_messages`)
**Logic:**
```python
if state.get("error"):
    parts.append(f"\nWARNING: Some research lanes failed: {state['error']}. "
                 "Your answer must reflect this missing evidence.")
```

---

## Critic (round 3)

DECISION: ready

The researcher has provided a complete trace across all four requested components with specific line citations (`graph.py:64`, `runner.py:172-174`, `council.py:245`, `storage.py:108`). The conclusion regarding the "silent degradation" failure mode (coherent answer but `status=failed`) is logically derived from the code path. The recommended hardening change is specific, cited, and addresses the identified failure mode.

---

## Synthesizer

The council produces a degraded but coherent answer, but the overall consultation is marked as `status=failed`.

*   **CouncilState Reducers**: The exception is caught at `consultants/engine/council.py:555`, and the node returns a "tombstone" update containing an error string in the `research` list (`consultants/engine/council.py:566`). Because `CouncilState.research` uses `operator.add` (`consultants/engine/graph.py:64`), this error string is appended to the successful reports from other parallel lanes.
*   **Runner Stream Loop**: The `compiled.stream` loop (`consultants/server/runner.py:137`) does not crash. However, the runner checks for the presence of the `error` key in the final state (`consultants/server/runner.py:172`), which triggers `terminal_status = "failed"` (`consultants/server/runner.py:174`).
*   **Synthesizer Input**: The synthesizer receives the merged `research` list via `build_synthesizer_messages` (`consultants/engine/council.py:245`). It sees the valid reports from successful lanes and the failure string (e.g., `"(researcher lane failed: ...)"`) for the crashed lane, attempting to synthesize an answer from the remaining evidence.
*   **Artifact Writer**: The `storage.py` writer persists the `status="failed"` marker into `metadata.json` (`consultants/engine/storage.py:168`) and the `summary.md` YAML front-matter (`consultants/engine/storage.py:108`), even though a `final_answer` was generated.

**Failure Mode**: Silent degradation. The user receives a response that appears successful in the UI/body but is flagged as a system failure in the metadata. The synthesizer may hallucinate or omit facts due to the missing research dimension without being explicitly told the lane failed.

**Recommended Hardening**:
Modify `build_synthesizer_messages` in `consultants/engine/council.py:245` to check for the `error` key in the state and explicitly append a warning to the synthesizer's prompt, ensuring it acknowledges the missing evidence rather than ignoring the failure string.

---
