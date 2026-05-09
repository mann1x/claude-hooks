# Consultation transcript — csl-2026-05-09-0933-c549

_Question_: In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:

1. CouncilState's additive reducers (consultants/engine/graph.py)
2. consultants/server/runner.py's compiled.stream loop
3. The synthesizer's input messages — what does it actually see?
4. The storage.py artifact writer

Answer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line.


## Planner

1. Analyze `consultants/engine/graph.py` to identify how `CouncilState` reducers handle exceptions during additive updates from parallel researcher lanes.
2. Examine the `compiled.stream` loop in `consultants/server/runner.py` to determine if a lane-level exception triggers a generator crash or is captured as a state update.
3. Trace the data flow to the synthesizer to see if it filters out failed researcher responses or receives an explicit error message.
4. Verify if `storage.py` artifact writers are executed inside a try-finally block or if a lane crash prevents artifact persistence for that specific researcher.
5. Determine the final status return value in `consultants/server/runner.py` when a partial fan-out failure occurs.

---

## Researcher (round 1)

Here is the failure path trace for an unhandled exception in a parallel researcher lane.

### 1. CouncilState's Additive Reducers (`consultants/engine/graph.py`)
The `CouncilState` uses `operator.add` for `research`, `turns`, `research_rounds_used`, `total_prompt_tokens`, and `total_completion_tokens` (`consultants/engine/graph.py:64, 68-72`). However, these reducers only act on the *return value* of a node. Because `researcher_node` wraps the `run_loop` call in a `try...except` block (`consultants/engine/council.py:547`), the exception is caught **before** it reaches LangGraph's reducer logic. The node returns a "tombstone" update:
- `research`: `["(researcher lane failed: <exception>)"]` (`consultants/engine/council.py:566`)
- `_role_failed`: `"researcher"` (`consultants/engine/council.py:565`)
- `turns`: A `RoleTurn` containing the error text (`consultants/engine/council.py:568`).

### 2. `compiled.stream` loop (`consultants/server/runner.py`)
The `runner.py` loop (`consultants/server/runner.py:137`) handles two stream modes:
- `updates`: Flips role progress to `"done"` (`consultants/server/runner.py:146`). Since the `researcher_node` catches its own exception and returns a valid dict, the `updates` stream continues normally.
- `values`: Updates `final_state` with the merged state (`consultants/server/runner.py:139`). The tombstone from the failed lane is merged into the `research` list via the additive reducer.
The `try...except Exception as e` block in `runner.py:155` is **not triggered** because the exception was handled inside the node.

### 3. Synthesizer's Input Messages
The synthesizer receives the merged `research` list (`consultants/engine/council.py:720`).
- It sees a list of reports from the surviving lanes AND the string `"(researcher lane failed: <exception>)"` from the failed lane.
- Because it consumes the raw evidence, the synthesizer sees exactly which lane failed and why. It will typically produce a **degraded but coherent** answer, noting that part of the research was unavailable or citing only the successful lanes.

### 4. Storage Artifact Writer (`consultants/engine/storage.py`)
The `runner.py` determines the final status:
- `node_error = final_state.get("error")` (`consultants/server/runner.py:172`). The tombstone set `"error": "researcher failed: ..."` in `consultants/engine/council.py:564`.
- `terminal_status` becomes `"failed"` (`consultants/server/runner.py:174`).
- `storage.write_consultation` is called (`consultants/server/runner.py:199`).
- The `metadata.json` will show `status: "failed"` and the `error` string, but `summary.md` will still contain the synthesizer's best-effort answer.

### Final Conclusion
**The council produces a degraded answer.** The entire consultation does **not** crash (it does not surface as a graph-level failure in `runner.py:155`), but it is marked as `status="failed"` in the artifacts because at least one role reported an error. The user perceives a successful interaction (they get an answer), but the `metadata.json` and `transcript.md` record the failure.

**Hardening Recommendation:**
The current failure mode is a "silent-to-user" failure. To make this explicit to the synthesizer (preventing it from hallucinating over the gap), change the tombstone text in `consultants/engine/council.py:562` from `f"(researcher lane failed: {e})"` to a structured marker that the synthesizer's prompt explicitly instructs it to report as a gap.

**Recommended Change:**
`consultants/engine/council.py:562`: Change `err_text = f"(researcher lane failed: {e})"` to `err_text = f"CRITICAL_FAILURE: Lane {state.get('lane_idx')} crashed. Error: {e}. This research gap must be noted in the final answer."`

---

## Researcher (round 1)

The failure path for an unhandled exception in a parallel researcher lane is as follows:

### 1. CouncilState's Additive Reducers
When a parallel researcher lane crashes inside `agent_loop.runner.run_loop`, the `researcher_node` catches the exception at `consultants/engine/council.py:555`. It returns a "tombstone" update containing:
- `"research": [err_text]` (`consultants/engine/council.py:566`)
- `"turns": [RoleTurn(...)]` (`consultants/engine/council.py:568`)
- `"_role_failed": "researcher"` (`consultants/engine/council.py:565`)

Because `CouncilState` defines `research` as `Annotated[list, operator.add]` (`consultants/engine/graph.py:64`), LangGraph's reducer concatenates this error string into the global research list. Other successful lanes continue to append their findings normally.

### 2. Compiled Stream Loop
The `consultants/server/runner.py` stream loop (`consultants/server/runner.py:137`) captures the final merged state in `final_state` via the `"values"` mode (`consultants/server/runner.py:139`). Since the exception was caught inside the `researcher_node` and not propagated to the graph's top-level `compiled.stream`, the `try...except` block at `consultants/server/runner.py:155` is **not** triggered. The graph completes its traversal normally.

### 3. Synthesizer's Input Messages
The synthesizer receives the results of all lanes via `build_synthesizer_messages` (`consultants/engine/council.py:245`). Specifically, it iterates over `state.get("research")` (`consultants/engine/council.py:253`).
- **What it sees:** A list of reports where one (or more) entries are literal error strings like `"(researcher lane failed: <exception>)"`.
- **Effect:** The synthesizer sees the "gap" explicitly. It will likely mention that some research failed or simply synthesize the answer based on the surviving lanes' data.

### 4. Storage Artifact Writer
The `storage.write_consultation` call (`consultants/server/runner.py:199`) is executed regardless of individual lane failures because the graph technically "completed." 
- The `ConsultationResult` uses `terminal_status = "failed" if node_error else "completed"` (`consultants/server/runner.py:174`).
- Since the researcher node set `"error": f"researcher failed: {e}"` (`consultants/engine/council.py:564`), the final artifact `status` is marked as `"failed"` (`consultants/server/runner.py:189`), even if a `final_answer` was produced.

### Conclusion
The council **produces a degraded but coherent answer**. The entire consultation does **not** fail (it doesn't crash the server), but it surfaces as `status=failed` in the metadata. The user perceives a response that may be missing specific details from the crashed lane, but the failure is explicitly logged in the transcript and metadata.

**Recommended Hardening:**
Modify `consultants/engine/council.py:564` to set a specific `_lane_failed` marker instead of the global `error` key, and update `consultants/server/runner.py:174` to only mark the status as `"failed"` if the `synthesizer` (the mandatory final node) failed, allowing consultations with partial researcher failures to be marked as `"completed"` (or a new `"degraded"` status).

---

## Researcher (round 1)

The failure path for an unhandled exception in a parallel researcher lane (e.g., `agent_loop.runner.run_loop`) is as follows:

*   **Node Exception Handling**: The exception is caught by the `try...except` block in `researcher_node` at `consultants/engine/council.py:547-555`. Instead of crashing the graph, the node returns a "tombstone" state update (`consultants/engine/council.py:563-573`).
*   **Additive Reducers**: The tombstone includes `research: [err_text]`. Because `research` is defined as an additive reducer (`Annotated[list, operator.add]`) in `consultants/engine/graph.py:64`, this error string (`"(researcher lane failed: ...)"`) is concatenated into the global research list along with results from successful lanes.
*   **Runner Execution**: The `compiled.stream` loop in `consultants/server/runner.py:137-141` continues normally because the exception was handled within the node. `final_state` is updated with the merged results of all lanes.
*   **Synthesizer Input**: The synthesizer receives the complete `research` list at `consultants/engine/council.py:720`. It sees the a mix of successful reports and failure strings. It synthesizes the final answer based on the available data, treating failure strings as evidence of a gap.
*   **Artifact Storage**: 
    *   The runner detects the failure via `node_error = final_state.get("error")` at `consultants/server/runner.py:172`.
    *   `terminal_status` is set to `"failed"` at `consultants/server/runner.py:174`.
    *   `storage.write_consultation` (`consultants/server/runner.py:199`) writes this status to `summary.md` (YAML front-matter `consultants/engine/storage.py:108`) and `metadata.json` (`consultants/engine/storage.py:168`).

**Final Result**: The council **still produces a coherent answer**, though it is degraded (missing the data from the failed lane). The user perceives a successful interaction in the UI, but the consultation surfaces as `status=failed` in the on-disk artifacts.

**Recommended Hardening**:
Currently, `error` is a non-reducer `Optional[str]` (`consultants/engine/graph.py:74`), meaning if multiple lanes fail, only the error from the *last* lane to finish is preserved. Change `error` to an additive list to capture all lane failures.

**Change**: `consultants/engine/graph.py:74`
```python
-    error: Optional[str]
+    error: Annotated[list[str], operator.add]
```
*(Note: This would also require updating `consultants/server/runner.py:172-174` to check if the list is non-empty rather than checking for a truthy string).*

---

## Critic (round 3)

DECISION: ready

The researcher has provided a comprehensive trace across all four requested touchpoints with precise line references. They correctly identified that `researcher_node` (`consultants/engine/council.py:547`) intercepts exceptions, preventing a `compiled.stream` crash in `runner.py` and instead injecting a "tombstone" error string into the `research` list via the additive reducer (`consultants/engine/graph.py:64`). The data flow to the synthesizer and the subsequent `status=failed` assignment in `runner.py:174` based on the `error` key are clearly mapped. The conclusion regarding the degraded user experience and the specific hardening fix for the `error` reducer is well-supported.

---

## Synthesizer

The council produces a degraded but coherent answer; it does not crash, but the consultation is marked as `status=failed` in the final artifacts.

*   **CouncilState Reducers**: The exception is caught within `researcher_node` (`consultants/engine/council.py:547`), preventing a graph crash. The node returns a "tombstone" update where the error string is appended to the `research` list via the `operator.add` reducer (`consultants/engine/graph.py:64`) and the `error` key is set (`consultants/engine/council.py:564`).
*   **Runner Loop**: The `compiled.stream` loop in `consultants/server/runner.py:137` continues normally because the exception was handled inside the node. The `final_state` is updated with the merged results of all lanes, including the tombstone (`consultants/server/runner.py:139`).
*   **Synthesizer Input**: The synthesizer receives the merged `research` list (`consultants/engine/council.py:720`). It sees a mix of successful research reports and literal error strings (e.g., `"(researcher lane failed: ...)"`) at `consultants/engine/council.py:253`, allowing it to synthesize a response based on available data.
*   **Artifact Writer**: The runner identifies a failure because `final_state.get("error")` is populated (`consultants/server/runner.py:172`), setting `terminal_status` to `"failed"` (`consultants/server/runner.py:174`). This status is persisted to `metadata.json` and `summary.md` via `storage.write_consultation` (`consultants/server/runner.py:199` and `consultants/engine/storage.py:168`).

**Failure Mode**: The user perceives a successful interaction in the UI (they receive an answer), but the data is missing the failed lane's contribution, and the system logs the entire consultation as a failure in the on-disk metadata.

**Hardening Recommendation**: Change the `error` field in `CouncilState` from an `Optional[str]` to an additive list to prevent subsequent lane completions from overwriting previous lane errors.
`consultants/engine/graph.py:74`: Change `error: Optional[str]` to `error: Annotated[list[str], operator.add]`.

---
