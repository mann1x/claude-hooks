# Consultation transcript — csl-2026-10-22-2016-3c35

_Question_: In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:

1. CouncilState's additive reducers (consultants/engine/graph.py)
2. consultants/server/runner.py's compiled.stream loop
3. The synthesizer's input messages — what does it actually see?
4. The storage.py artifact writer

Answer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line.


## Planner

1. Search `consultants/` for calls to `run_loop` to identify the exact integration point between the server and the agent loop (e.g., `consultants/server/runner.py` or `consultants/engine/graph.py`).
2. Examine `consultants/engine/graph.py` to find the additive reducers and verify how they handle exceptions or `None`/missing values returned from researcher lanes.
3. Analyze the `compiled.stream` loop in `consultants/server/runner.py` to determine if it wraps researcher execution in a try-except block and how it signals failure to the client.
4. Trace the data flow from the reducers in `consultants/engine/graph.py` to the synthesizer's input to see if failed researcher output is omitted, replaced by an error message, or causes a crash.
5. Inspect `consultants/engine/storage.py` to check if the artifact writer captures execution exceptions or only writes successful results.
6. Verify if the server returns `status=failed` or a degraded response by checking the final return path in `consultants/server/runner.py`.

---

## Researcher (round 1)

The failure path for an unhandled exception in a parallel researcher lane is as follows:

### 1. CouncilState's Additive Reducers
When a researcher lane raises an unhandled exception inside `run_loop`, it is caught by the `try...except` block in `council.researcher_node` (`consultants/engine/council.py:547-555`). 

Instead of propagating the exception to the graph engine, the node returns a **"Tombstone"** update (`consultants/engine/council.py:563-573`). Because `CouncilState` uses additive reducers (`operator.add`) for the `research` and `turns` channels (`consultants/engine/graph.py:64, 68`), the failed lane's error message is concatenated with the results of successful lanes:
- **`research`**: The string `"(researcher lane failed: <exception>)"` is added to the list of reports.
- **`turns`**: A `RoleTurn` object containing the error text is added to the global transcript.

### 2. Server's `compiled.stream` Loop
The `compiled.stream` loop in `consultants/server/runner.py:137-146` processes these updates. Since the exception was handled inside the node and converted to a state update, the `stream` loop **does not raise an exception**. It continues to the next superstep. The graph completes normally, and `final_state` (`consultants/server/runner.py:140`) contains the merged results, including the tombstone error.

### 3. Synthesizer's Input
The synthesizer receives the merged state via `build_synthesizer_messages` (`consultants/engine/council.py:245`). It sees all reports in `state.get("research")` (`consultants/engine/council.py:253-254`). Consequently, the synthesizer sees:
- Reports from surviving lanes.
- The explicit failure marker: `"(researcher lane failed: <exception>)"`.

### 4. Artifact Writer (`storage.py`)
The `ConsultationResult` is constructed using `final_state` (`consultants/server/runner.py:177-198`). 
- **`status`**: Set to `"failed"` because `final_state.get("error")` is populated by the tombstone return (`consultants/engine/council.py:564`, `consultants/server/runner.py:174`).
- **`final_answer`**: The synthesizer's output (which may be degraded but still coherent) is written to `summary.md` via `storage.write_consultation` (`consultants/engine/storage.py:200`).
- **`transcript.md`**: The `RoleTurn` recording the failure is rendered into the transcript (`consultants/engine/storage.py:129-158`).

**Conclusion**: The council **does not fail entirely**; it produces a **degraded but coherent answer**. The user perceives a "failed" status in the metadata, but receives a final answer based on the subset of surviving research lanes.

**Recommended Hardening**:
To prevent the synthesizer from hallucinating a complete answer when critical lanes fail, the `synthesizer_node` should explicitly check for the `error` key in the state and prepend a warning to the prompt.

**Change**: In `consultants/engine/council.py`, modify `build_synthesizer_messages` (around line 245) to inject a warning if `state.get("error")` is present, ensuring the LLM acknowledges the missing data.

---

## Researcher (round 1)

The failure path for an unhandled exception in a parallel researcher lane (e.g., in `agent_loop.runner.run_loop`) is as follows:

### 1. CouncilState's Additive Reducers
When a researcher lane raises an exception, it is caught by the `try...except` block in `consultants/engine/council.py:547-555`. The `researcher_node` does **not** propagate the exception to the graph; instead, it returns a "tombstone" state update:
*   **`research`**: The additive reducer (`Annotated[list, operator.add]` in `consultants/engine/graph.py:64`) appends a failure string: `"(researcher lane failed: <exception>)"` (`consultants/engine/council.py:562-566`).
*   **`turns`**: The additive reducer (`consultants/engine/graph.py:68`) appends a `RoleTurn` containing the error text (`consultants/engine/council.py:568-573`).
*   **`error` & `_role_failed`**: These fields are set to the exception message and `"researcher"`, respectively (`consultants/engine/council.py:564-565`).

### 2. Server Runner's Stream Loop
The `consultants/server/runner.py` stream loop (`consultants/server/runner.py:137-141`) continues normally because the exception was handled inside the node. The `compiled.stream` does not crash.
*   The `final_state` is updated with the merged result of all lanes (successful reports + tombstone error strings).
*   The server detects the `node_error` from `final_state.get("error")` (`consultants/server/runner.py:172`).
*   `terminal_status` is set to `"failed"` (`consultants/server/runner.py:174`).

### 3. Synthesizer's Input
The synthesizer receives the merged `research` list via `build_synthesizer_messages` (`consultants/engine/council.py:245-266`).
*   **What it sees**: It sees a list of reports where one or more entries are exactly `"(researcher lane failed: <exception>)"`.
*   **Behavior**: The synthesizer attempts to produce a final answer using the surviving lanes' data. It does not "crash" but is forced to work with degraded evidence.

### 4. Storage Artifact Writer
The `ConsultationResult` is constructed with `status="failed"` and the `error` string (`consultants/server/runner.py:189-190`).
*   `storage.write_consultation` (`consultants/engine/storage.py:200`) writes the `summary.md`, `transcript.md`, and `metadata.json`.
*   The `summary.md` contains the synthesizer's answer (which may be degraded) and the YAML front-matter `status: failed` and `error: researcher failed: ...` (`consultants/engine/storage.py:108-110`).

### Conclusion
**The council produces a degraded answer.** The consultation does **not** fail the entire process (it doesn't return a `status=failed` to the user immediately via a crash), but it marks the final artifact as `status=failed`. The user perceives a response that is missing information from the failed lane, potentially accompanied by the synthesizer mentioning the failure if the model chooses to report it.

### Recommended Hardening
The current tombstone is a simple string. To prevent the synthesizer from hallucinating "missing" data as "non-existent" data, the tombstone should explicitly flag the gap as a system failure.

**Recommendation**: Change the researcher failure text to a more prominent system-level warning.
**Path**: `consultants/engine/council.py:562`
**Change**: Update `err_text = f"(researcher lane failed: {e})"` to `err_text = f"!!! SYSTEM ERROR: Researcher Lane Failed !!!\nReason: {e}\n(This evidence is missing from the final report)"`.

---

## Critic (round 2)

DECISION: ready

The evidence is sufficient. Both researchers accurately traced the failure path, and my spot-checks confirm the specific lines and logic.

1.  **Additive Reducers**: `consultants/engine/council.py:547-573` catches exceptions and returns a tombstone dictionary. `consultants/engine/graph.py:64` and `68` use `operator.add` to merge these into the `research` (list of strings) and `turns` (list of `RoleTurn`) channels.
2.  **Server Loop**: `consultants/server/runner.py:137-146` continues because the exception was handled at the node level. The loop finishes, and `final_state` contains the tombstone updates.
3.  **Synthesizer Input**: `consultants/engine/council.py:253-254` iterates over the `research` list. If a lane failed, the synthesizer sees the string `"(researcher lane failed: <exception>)"` as one of the reports.
4.  **Artifact Writer**: `consultants/server/runner.py:172-174` detects `final_state.get("error")` and sets `terminal_status = "failed"`. This is then passed to `storage.write_consultation` (`consultants/server/runner.py:199`).

**Conclusion**: The council produces a degraded but coherent answer. The final artifact is marked `status: failed` in metadata, but a `final_answer` is still generated by the synthesizer using whatever research succeeded.

**Recommended Hardening**:
Update `consultants/engine/council.py:562` to use a more prominent error marker (e.g., `!!! SYSTEM ERROR: Researcher Lane Failed !!!`) to prevent the synthesizer from interpreting the failure as a "lack of evidence" rather than a "system failure."

---

## Synthesizer

The council produces a degraded but coherent answer. The entire consultation does not crash; instead, it completes the graph execution and marks the final artifact as `status=failed` in the metadata.

**Failure Path Trace:**

1.  **CouncilState's Additive Reducers**: Exceptions in `run_loop` are caught by a `try...except` block in `researcher_node` (`consultants/engine/council.py:547-555`). The node returns a "tombstone" dictionary containing an error message (`consultants/engine/council.py:562-573`). Because `CouncilState` uses `operator.add` for the `research` and `turns` channels (`consultants/engine/graph.py:64 [no turns at this line; line is in CouncilState], 68`), the failure string `"(researcher lane failed: <exception>)"` is appended to the research list, and a corresponding `RoleTurn` is added to the transcript.
2.  **`compiled.stream` Loop**: Since the exception is handled within the node, the stream loop in `consultants/server/runner.py:137-146` does not encounter an unhandled exception and continues to the next superstep. The process completes normally, and `final_state` (`consultants/server/runner.py:140`) captures the merged state including the tombstone.
3.  **Synthesizer's Input**: The synthesizer receives the merged research list via `build_synthesizer_messages` (`consultants/engine/council.py:245-266`). It sees all successful reports plus the explicit failure string `"(researcher lane failed: <exception>)"` as part of the evidence (`consultants/engine/council.py:253-254`).
4.  **Storage Artifact Writer**: The server detects the error in `final_state.get("error")` (`consultants/server/runner.py:172`) and sets `terminal_status = "failed"` (`consultants/server/runner.py:174`). `storage.write_consultation` (`consultants/engine/storage.py:200`) then writes the synthesizer's (potentially degraded) answer to `summary.md`, but includes `status: failed` and the error message in the YAML front-matter (`consultants/engine/storage.py:108-110`).

**User Perception:**
The user receives a final answer that is missing information from the failed researcher lane. The metadata/header of the result indicates `status: failed`, but the content remains a coherent response based on the surviving lanes.

**Recommended Hardening:**
Update `consultants/engine/council.py:562` to use a more prominent error marker (e.g., `!!! SYSTEM ERROR: Researcher Lane Failed !!!\nReason: {e}`) to ensure the synthesizer recognizes the gap as a system failure rather than a lack of available evidence.

---
