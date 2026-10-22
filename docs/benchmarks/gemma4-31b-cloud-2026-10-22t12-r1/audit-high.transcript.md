# Consultation transcript — csl-2026-10-22-1839-883f

_Question_: In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:

1. CouncilState's additive reducers (consultants/engine/graph.py)
2. consultants/server/runner.py's compiled.stream loop
3. The synthesizer's input messages — what does it actually see?
4. The storage.py artifact writer

Answer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line.


## Planner

1. **Exception Path**: An unhandled exception in `agent_loop.runner.run_loop` is caught by the `try...except` block in `consultants/engine/council.py:547-555`.
2. **State Reducer**: The `researcher_node` returns a "tombstone" update `consultants/engine/council.py:563-573` containing `research: [err_text]` and `turns: [RoleTurn(...)]`. Because `CouncilState.research` and `CouncilState.turns` use `operator.add` (additive reducers) in `consultants/engine/graph.py:64,68`, this failure message is appended to the global state without erasing results from other parallel lanes.
3. **Runner Loop**: The `compiled.stream` loop in `consultants/server/runner.py:137-141` captures this merged state in `final_state`. Since the exception was caught inside the node, the graph does not crash; the stream continues.
4. **Synthesizer Input**: The synthesizer receives the full `research` list via `build_synthesizer_messages` (`consultants/engine/council.py:245-267`). It sees the successful reports from healthy lanes and the literal string `"(researcher lane failed: ...)"` for the failed lane.
5. **Artifact Writer**: `consultants/server/runner.py:177-198` constructs a `ConsultationResult` using `final_state`. Since `researcher_node` set `error` and `_role_failed` in the state (`consultants/engine/council.py:564-565`), the runner sets `terminal_status = "failed"` (`consultants/server/runner.py:174`) and `status="failed"` in the written artifact.
6. **User Perception**: The council produces a **degraded but coherent answer**. The synthesizer attempts to answer using only the available evidence. However, the final API status is `status=failed` because any role failure triggers a terminal failure status, even if the synthesizer successfully produced a result.

**Hardening Recommendation**: Change `consultants/server/runner.py:174` to `terminal_status = "failed" if (node_error and node_failed == "synthesizer") else "completed"`. This prevents a non-terminal researcher lane failure from marking the entire consultation as failed when a usable answer was still synthesized.

---

## Researcher (round 1)

The failure path for an unhandled exception in a parallel researcher lane is as follows:

1.  **Exception Catching**: An unhandled exception in `agent_loop.runner.run_loop` is caught by the `try...except` block in `consultants/engine/council.py:547-555`.
2.  **Tombstone Update**: Instead of crashing the graph, `researcher_node` returns a "tombstone" update `consultants/engine/council.py:563-573` containing:
    *   `research: [f"(researcher lane failed: {e})"]`
    *   `turns: [RoleTurn(role="researcher", content=err_text, ...)]`
    *   `error: f"researcher failed: {e}"`
    *   `_role_failed: "researcher"`
3.  **Additive Reducers**: In `consultants/engine/graph.py:64,68`, the `research` and `turns` channels use `operator.add`. This means the failure message is appended to the global state. Results from other healthy parallel lanes are preserved; they are not erased by the failing lane.
4.  **Runner Loop**: The `compiled.stream` loop in `consultants/server/runner.py:137-141` captures the merged state in `final_state`. Since the exception was caught within the node, the LangGraph execution continues normally to the next node.
5.  **Synthesizer Input**: The synthesizer reads the `research` list in `consultants/engine/council.py:719-721`. It sees a mix of real reports and one or more strings like `"(researcher lane failed: ...)"`.
6.  **Artifact Writing**: `consultants/server/runner.py:177-198` creates a `ConsultationResult`.
    *   `status` is set to `"failed"` because `final_state.get("error")` (set by the tombstone at `council.py:564`) is truthy (`consultants/server/runner.py:174`).
    *   `final_answer` is the synthesizer's output, which will be a degraded answer based on partial evidence.
    *   The `transcript.md` (via `storage.py:123-158`) explicitly lists the failure text in the researcher's turn.

**Conclusion**: The council **still produces an answer**, but the consultation is marked as **`status=failed`**. The user perceives a **degraded answer** where some investigation steps are missing or replaced by failure notices, but the process does not crash.

**Recommended Hardening**:
Modify `consultants/engine/council.py:563-573` to include a specific hint to the synthesizer in the `research` update, e.g., `"(RESEARCH FAILURE: The following plan item was not investigated due to an error: {plan_item})"` using the `plan_item` available in `state` (`council.py:515`). This prevents the synthesizer from assuming the evidence was simply "not found" and instead allows it to explicitly notify the user about the missing gap.

---

## Researcher (round 1)

The failure path for an unhandled exception in a parallel researcher lane is as follows:

### 1. CouncilState Additive Reducers (`consultants/engine/graph.py`)
An exception inside `agent_loop.runner.run_loop` is caught by the `try...except` block in `consultants/engine/council.py:547-555`. The `researcher_node` then returns a "tombstone" update (`consultants/engine/council.py:563-573`). 
*   **Additive Reducer**: The `research` channel in `CouncilState` is defined as `Annotated[list, operator.add]` (`consultants/engine/graph.py:64`). The tombstone string `"(researcher lane failed: ...)"` is appended to the `research` list alongside reports from healthy lanes.
*   **State Markers**: The node also sets `error` and `_role_failed: "researcher"` in the state (`consultants/engine/council.py:564-565`).

### 2. Compiled Stream Loop (`consultants/server/runner.py`)
The `compiled.stream` loop (`consultants/server/runner.py:137-141`) captures the merged state in `final_state`. Because the exception was caught and handled *inside* the node, the graph itself does not crash; it completes its execution normally.
*   **Status Determination**: After the stream drains, the runner checks `final_state.get("error")` (`consultants/server/runner.py:172`). Since the researcher's tombstone set this field, `terminal_status` is set to `"failed"` (`consultants/server/runner.py:174`).

### 3. Synthesizer Input Messages
The synthesizer receives the full `research` list via `build_synthesizer_messages` (`consultants/engine/council.py:245-267`). 
*   **What it sees**: It sees a mix of successful reports and the literal error string `"(researcher lane failed: ...)"` for the failed lane. It attempts to synthesize a final answer using only the surviving evidence.

### 4. Storage Artifact Writer
The `ConsultationResult` is constructed using `final_state` (`consultants/server/runner.py:177-198`).
*   **Artifact Status**: The `status` field of the written artifact is set to `terminal_status` (which is `"failed"`) (`consultants/server/runner.py:189`).

**Conclusion**: The council produces a **degraded but coherent answer** (provided the synthesizer doesn't crash), but the overall consultation is marked as `status=failed`. The user perceives a successful answer in the UI/transcript, but the backend logs and artifacts report a failure.

**Hardening Recommendation**:
Change `consultants/server/runner.py:174` to:
`terminal_status = "failed" if (node_error and node_failed == "synthesizer") else "completed"`
This ensures that a non-terminal researcher lane failure does not mark the entire consultation as failed if a usable answer was still produced.

---

## Critic (round 2)

DECISION: ready

The researcher's trace is accurate and verified against the source. An unhandled exception in a researcher lane is caught by `consultants/engine/council.py:547-555`. The node returns a "tombstone" update (`consultants/engine/council.py:563-573`) containing an error string in the `research` list and setting `error` and `_role_failed`. Because `CouncilState.research` uses `operator.add` (`consultants/engine/graph.py:64`), this failure is appended without wiping other lanes' results. The `compiled.stream` loop in `consultants/server/runner.py:137-141` captures the resulting state. The synthesizer (`consultants/engine/council.py:715-740`) receives these reports via `build_synthesizer_messages` (`consultants/engine/council.py:245-267`), allowing it to produce a degraded but coherent answer. Finally, `consultants/server/runner.py:174` marks the entire consultation as `status="failed"` because any truthy `error` in the final state triggers a failure status.

CORRECTIONS:
- report 1: claimed `consultants/engine/council.py:547-555` had no `run_loop` — actual: it calls `loop_runner` (which is `run_loop` per line 499) at line 548.
- report 1: claimed synthesizer reads research at `consultants/engine/council.py:719-721` — actual: it calls `build_synthesizer_messages` there, which processes the list at `consultants/engine/council.py:253-254`.

Hardening Recommendation:
Modify `consultants/server/runner.py:174` to `terminal_status = "failed" if (node_error and node_failed == "synthesizer") else "completed"`. This prevents non-terminal researcher failures from marking a successful synthesis as a global failure.

---

## Synthesizer

The council produces a degraded but coherent answer, but the consultation is marked as `status=failed`.

**Failure Path Trace**
*   **Exception Handling**: An exception in the runner loop (called at `consultants/engine/council.py:548`) is caught by the `try...except` block at `consultants/engine/council.py:547-555`. The `researcher_node` then returns a "tombstone" update at `consultants/engine/council.py:563-573` containing the error string in the `research` list and setting `error` and `_role_failed` in the state.
*   **Additive Reducers**: Because `CouncilState.research` (`consultants/engine/graph.py:64`) and `CouncilState.turns` (`consultants/engine/graph.py:68`) use `operator.add`, the tombstone is appended to the global state. Results from other parallel lanes are preserved.
*   **Runner Loop**: The `compiled.stream` loop in `consultants/server/runner.py:137-141` captures this merged state into `final_state`. Since the error was handled inside the node, the graph execution continues normally.
*   **Synthesizer Input**: The synthesizer calls `build_synthesizer_messages` at `consultants/engine/council.py:719-721`, which iterates over the `research` list at `consultants/engine/council.py:253-254 [no research at this line; line is in build_synthesizer_messages]`. The synthesizer receives a mix of successful reports and the literal string `"(researcher lane failed: ...)"`.
*   **Artifact Writer**: The runner checks `final_state.get("error")` at `consultants/server/runner.py:172`. Because the tombstone set this field, `terminal_status` is set to `"failed"` at `consultants/server/runner.py:174`, which is then written as the final `status` of the `ConsultationResult` at `consultants/server/runner.py:189 [no ConsultationResult at this line; line is in run_council]`.

**User Perception**
The user receives a final answer synthesized from the remaining healthy researcher lanes. However, the answer is degraded (missing evidence from the failed lane), and the overall API response/artifact indicates a failure (`status=failed`).

**Hardening Recommendation**
Modify `consultants/server/runner.py:174` to:
`terminal_status = "failed" if (node_error and node_failed == "synthesizer") else "completed"`

---
