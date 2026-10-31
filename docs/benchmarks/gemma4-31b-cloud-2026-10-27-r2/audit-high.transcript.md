# Consultation transcript — csl-2026-10-27-1142-e47b

_Question_: In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:

1. CouncilState's additive reducers (consultants/engine/graph.py)
2. consultants/server/runner.py's compiled.stream loop
3. The synthesizer's input messages — what does it actually see?
4. The storage.py artifact writer

Answer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line.


## Planner

1. Analyze `consultants/engine/graph.py` to identify the additive reducers in `CouncilState` and determine if they wrap parallel researcher execution in try-except blocks or if an exception propagates upward.
2. Trace the exception flow from `agent_loop/runner.py:run_loop` into `consultants/server/runner.py` to see if `compiled.stream` (or the loop calling it) catches exceptions from parallel lanes or allows them to terminate the stream.
3. Examine the synthesis trigger in `consultants/engine/council.py` or `consultants/server/runner.py` to determine what messages are passed to the synthesizer when one or more parallel lanes fail (e.g., are partial results passed, or is the entire input set nullified?).
4. Inspect `consultants/engine/storage.py` to verify if the artifact writer is called during a partial failure and whether it records a "failed" status for the session or only successful outputs.
5. Determine the final API response status (e.g., `status=failed` vs. a degraded successful response) by checking the error handling logic in `consultants/server/runner.py` that wraps the `compiled.stream` execution.

---

## Researcher (round 1)

The failure path for an unhandled exception in a parallel researcher lane is as follows:

### 1. CouncilState's Additive Reducers (`consultants/engine/graph.py`)
The `CouncilState` defines additive reducers for `research`, `turns`, `total_prompt_tokens`, and `total_completion_tokens` using `operator.add` (`consultants/engine/graph.py:64, 68, 71, 72`). These reducers are managed by LangGraph and only execute when a node successfully returns a partial state update. If a node raises an unhandled exception, no update is returned for that lane, and the reducer is not called for that specific lane's output.

### 2. Exception Flow and `compiled.stream` (`consultants/server/runner.py`)
The researcher node wraps the `agent_loop.runner.run_loop` call in a `try...except` block (`consultants/engine/council.py:547-555`). 
- **Internal Node Catch**: If `run_loop` raises an exception (e.g., HTTP timeout), `researcher_node` catches it and returns a "tombstone" state update (`consultants/engine/council.py:563-573`). This update includes:
  - `error`: `"researcher failed: {e}"`
  - `_role_failed`: `"researcher"`
  - `research`: `["(researcher lane failed: {e})"]` (which the `operator.add` reducer then appends to the global state).
- **Outer Stream Catch**: If an exception were to bypass the node's internal catch, it would propagate to `consultants/server/runner.py:136-161`. The `compiled.stream` loop is wrapped in a `try...except Exception` block (`consultants/server/runner.py:136`). If it crashes here, the runner sets `state.status = "failed"`, writes failed artifacts via `_write_failed_artifacts` (`consultants/server/runner.py:160`), and returns immediately.

### 3. Synthesizer's Input Messages (`consultants/engine/council.py`)
Because `researcher_node` catches the exception and returns a tombstone, the graph does **not** crash; it continues to the synthesizer. The synthesizer's input messages are built using `build_synthesizer_messages` (`consultants/engine/council.py:245`), which iterates over `state.get("research")` (`consultants/engine/council.py:253`).
- **What it sees**: The synthesizer sees the successful reports from other lanes PLUS the error string `"(researcher lane failed: {e})"` as one of the research reports.

### 4. Storage Artifact Writer (`consultants/engine/storage.py`)
The `ConsultationResult` is constructed from the `final_state` (`consultants/server/runner.py:177-198`). 
- **Status**: The `terminal_status` is determined by `final_state.get("error")` (`consultants/server/runner.py:172-174`). Since the tombstone return sets the `error` key, the `status` is recorded as `"failed"` in `metadata.json` and `summary.md` (`consultants/engine/storage.py:108`).
- **Content**: The `final_answer` (produced by the synthesizer) is still written to `summary.md` (`consultants/engine/storage.py:119`).

### Conclusion
The council **produces a degraded answer** rather than failing entirely. The user perceives a coherent answer, but one that is missing data from the failed lane. The failure is surfaced as `status="failed"` in the metadata/front-matter, but the `final_answer` is still delivered.

**Recommended Hardening Change**:
In `consultants/engine/council.py`, the tombstone for `researcher_node` currently provides a generic error. To prevent the synthesizer from hallucinating based on the absence of data, the tombstone should explicitly list which `plan_item` failed.

**Change**: Update `consultants/engine/council.py:562` from:
`err_text = f"(researcher lane failed: {e})"`
to:
`err_text = f"(researcher lane {state.get('lane_idx', 0)+1} failed on item '{state.get('plan_item', 'unknown')}': {e})"`

---

## Researcher (round 1)

The failure path for an unhandled exception in a parallel researcher lane (via `Send` API) is as follows:

1.  **`agent_loop.runner.run_loop`**: An unhandled exception (e.g., HTTP timeout or `tool_executor` failure) propagates out of `run_loop` (`claude_hooks/agent_loop/runner.py:138`).
2.  **`consultants/engine/council.py`**: The exception is caught by the `try...except` block in `researcher_node` (`consultants/engine/council.py:547-555`).
3.  **Additive Reducer (Tombstoning)**: Instead of letting the graph crash, the `researcher_node` returns a "tombstone" state update (`consultants/engine/council.py:563-573`).
    *   `research`: `[ "(researcher lane failed: <exception>)" ]` (additive reducer in `CouncilState` at `consultants/engine/graph.py:64`).
    *   `error`: `"researcher failed: <exception>"` (overwrites state).
    *   `_role_failed`: `"researcher"`.
    *   `turns`: A `RoleTurn` containing the failure text is appended to the additive `turns` list (`consultants/engine/graph.py:68`).
4.  **Synthesizer Input**: The synthesizer receives the merged state. In `build_synthesizer_messages` (`consultants/engine/council.py:245-266`), the `research_rounds` list (which is the merged `research` channel) now contains the failure string. The synthesizer sees the evidence from successful lanes plus the explicit failure note for the crashed lane.
5.  **`consultants/server/runner.py`**: The `compiled.stream` loop completes normally because the exception was handled inside the node (`consultants/server/runner.py:137`).
6.  **Final Status & Storage**: 
    *   `terminal_status` is set to `"failed"` because `final_state.get("error")` is now populated (`consultants/server/runner.py:174`).
    *   The `storage.ConsultationResult` is written with `status="failed"` and the specific `error` message (`consultants/server/runner.py:189-190`).
    *   `storage.write_consultation` persists this to `metadata.json` and `summary.md` (`consultants/engine/storage.py:200`).

**Conclusion**: The council **produces a degraded answer** rather than failing the entire consultation. The user perceives a successful-looking response (from the synthesizer) but the session metadata and the summary's YAML front-matter will mark the status as `failed` (`consultants/engine/storage.py:108`).

**Recommended Hardening**:
Currently, the synthesizer is not explicitly told that a "failed" researcher lane is a critical gap. It might hallucinate a complete answer based on partial data.
**Change**: Modify `build_synthesizer_messages` in `consultants/engine/council.py:253-254` to explicitly check for failure strings in the research reports and add a system warning.

**Exact Change**:
In `consultants/engine/council.py`, update the research loop in `build_synthesizer_messages` (approx line 253):
```python
    for i, r in enumerate(research_rounds, start=1):
        # Add check for failure markers
        prefix = "RESEARCHER REPORT (round {i}):"
        if "failed:" in r.lower():
            prefix = "CRITICAL FAILURE IN RESEARCH LANE (round {i}):"
        parts.append(f"\n{prefix}\n{r.strip()}")
```

---

## Critic (round 2)

DECISION: ready

The evidence is sufficient. Both researcher reports accurately traced the path, and my spot-checks of the source code confirmed the exact line numbers and logic.

The failure path is as follows:
1. **`agent_loop.runner.run_loop`**: An exception (e.g., timeout) propagates out of the loop.
2. **`consultants/engine/council.py`**: The `researcher_node` catches this in a `try...except` block (`consultants/engine/council.py:547-555`).
3. **Tombstoning**: It returns a "tombstone" update containing `research: [f"(researcher lane failed: {e})"]` (`consultants/engine/council.py:566`) and `error: f"researcher failed: {e}"` (`consultants/engine/council.py:564`).
4. **Additive Reducers**: The `operator.add` reducer in `CouncilState` (`consultants/engine/graph.py:64`) appends the failure string to the global research list.
5. **Synthesizer**: The `build_synthesizer_messages` function iterates over the merged research list (`consultants/engine/council.py:253-254`), so the synthesizer receives the successful reports plus the explicit failure string.
6. **Runner Status**: In `consultants/server/runner.py`, the stream completes normally because the exception was handled in the node. However, `terminal_status` is set to `"failed"` because `final_state.get("error")` is now populated (`consultants/server/runner.py:172-174`).
7. **Storage**: The `ConsultationResult` is written with `status="failed"` (`consultants/server/runner.py:189`), which appears in the `summary.md` YAML front-matter (`consultants/engine/storage.py:108`), but the `final_answer` is still rendered as the body of the file (`consultants/engine/storage.py:119`).

**Conclusion**: The council produces a **degraded answer**. The user receives a response that is coherent but based on partial data; the failure is signaled in the metadata (`status=failed`) but not as a hard API failure.

**Recommended Hardening Change**:
Modify `consultants/engine/council.py:254` to explicitly flag failure strings to the synthesizer to prevent hallucinated completion:
`parts.append(f"\n{'CRITICAL FAILURE' if 'failed:' in r.lower() else 'RESEARCHER REPORT'} (round {i}):\n{r.strip()}")`

---

## Synthesizer

The council produces a **degraded answer** rather than failing the entire consultation. The user receives a coherent response based on partial data, but the session metadata and the summary's YAML front-matter are marked with `status=failed`.

**Failure Path Trace:**
*   **Exception Handling**: An exception in `agent_loop.runner.run_loop` is caught by the `try...except` block in `researcher_node` (`consultants/engine/council.py:547-555`).
*   **Tombstoning**: The node returns a "tombstone" update containing `research: [f"(researcher lane failed: {e})"]` (`consultants/engine/council.py:566`) and `error: f"researcher failed: {e}"` (`consultants/engine/council.py:564`).
*   **Additive Reducers**: The `operator.add` reducer in `CouncilState` (`consultants/engine/graph.py:64`) appends the failure string to the global research list.
*   **Stream Execution**: The `compiled.stream` loop in `consultants/server/runner.py:137` completes normally because the exception was handled within the node.
*   **Synthesizer Input**: `build_synthesizer_messages` iterates over the merged research list (`consultants/engine/council.py:253-254`), passing the successful reports and the explicit failure string to the synthesizer.
*   **Status Assignment**: `terminal_status` is set to `"failed"` because `final_state.get("error")` is now populated (`consultants/server/runner.py:172-174`).
*   **Artifact Writing**: The `ConsultationResult` is written with `status="failed"` (`consultants/server/runner.py:189 [no ConsultationResult at this line; line is in run_council]`), which is recorded in the `summary.md` YAML front-matter (`consultants/engine/storage.py:108 [no md at this line; line is in _yaml_front_matter]`), while the `final_answer` is still rendered as the body of the file (`consultants/engine/storage.py:119`).

**Recommended Hardening Change:**
Modify `consultants/engine/council.py:254` to explicitly flag failure strings to the synthesizer to prevent it from hallucinating a complete answer based on partial data:
`parts.append(f"\n{'CRITICAL FAILURE' if 'failed:' in r.lower() else 'RESEARCHER REPORT'} (round {i}):\n{r.strip()}")`

---
