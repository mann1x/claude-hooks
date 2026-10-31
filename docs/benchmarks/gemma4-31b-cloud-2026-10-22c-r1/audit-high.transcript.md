# Consultation transcript — csl-2026-10-22-1916-60d8

_Question_: In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:

1. CouncilState's additive reducers (consultants/engine/graph.py)
2. consultants/server/runner.py's compiled.stream loop
3. The synthesizer's input messages — what does it actually see?
4. The storage.py artifact writer

Answer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line.


## Planner

1. Locate the parallel fan-out mechanism and `agent_loop.runner.run_loop` (likely `claude_hooks/agent_loop/runner.py`) to determine if exceptions are caught or propagated to the caller.
2. Examine `consultants/engine/graph.py` to identify the additive reducers in `CouncilState` and verify how they handle missing or errored researcher outputs during state merging.
3. Analyze `consultants/server/runner.py` (specifically the `compiled.stream` loop) to see if a failure in one parallel lane triggers a global exception or if it continues streaming partial results.
4. Trace the data flow into the synthesizer to determine if it receives a filtered list of successful messages or if an exception object/empty string is passed as a researcher's contribution.
5. Inspect `consultants/engine/storage.py` to verify if the artifact writer saves partial state/logs when a lane fails or if the write is skipped/fails entirely.
6. Determine the final response status (success vs failed) by tracing the return value of the server's request handler in `consultants/server/runner.py` under the identified failure conditions.

---

## Researcher (round 1)

The failure path for an unhandled exception in a parallel researcher lane is as follows:

### 1. Exception Propagation & Catching
*   **The Crash**: An unhandled exception in `agent_loop.runner.run_loop` (e.g., HTTP timeout or tool return type error) propagates up to the caller.
*   **The Catch**: The exception is caught by the `researcher_node` wrapper in `consultants/engine/council.py:555`.
*   **Tombstone Return**: Instead of crashing the graph, the node returns a "tombstone" state update (`consultants/engine/council.py:563-573`):
    *   `error`: `"researcher failed: <exception>"`
    *   `_role_failed`: `"researcher"`
    *   `research`: `["(researcher lane failed: <exception>)"]` (A list containing the error text).
    *   `turns`: A `RoleTurn` recording the failure content.

### 2. State Merging (Additive Reducers)
*   **Additive Merge**: Because `CouncilState` uses `operator.add` for the `research` and `turns` channels (`consultants/engine/graph.py:64`, `consultants/engine/graph.py:68`), the error string is concatenated with the successful reports from other parallel lanes.
*   **Result**: The global `state["research"]` becomes a list of findings where one or more entries are error strings (e.g., `["Finding A...", "(researcher lane failed: ...)", "Finding C..."]`).

### 3. Synthesizer Input
*   **Message Construction**: The `build_synthesizer_messages` function (`consultants/engine/council.py:245-267`) iterates over `research_rounds`.
*   **What it Sees**: The synthesizer receives the combined list of researcher reports. It sees the explicit failure text `(researcher lane failed: ...)` as part of the evidence.
*   **Behavior**: The synthesizer typically ignores the error or notes the gap, but because the other lanes provided data, it produces a **degraded but coherent answer** based on the surviving research.

### 4. Server Loop & Artifact Storage
*   **Stream Loop**: `consultants/server/runner.py`'s `compiled.stream` loop (`consultants/server/runner.py:137-141`) continues to run because the exception was handled inside the node.
*   **Terminal Status**: The runner detects the `error` key in `final_state` (`consultants/server/runner.py:172`) and sets `terminal_status = "failed"` (`consultants/server/runner.py:174`).
*   **Storage**: `storage.write_consultation` (`consultants/server/runner.py:199`) writes the `ConsultationResult`. The `summary.md` contains the degraded answer, but `metadata.json` records `status: "failed"` and the error string (`consultants/engine/storage.py:108-110`).

**User Perception**: The user receives a response that looks correct but may be missing specific details from the failed lane. If they check the metadata/logs, the consultation is marked as `failed`.

### Recommendation
**Hardening**: The `researcher_node` tombstone currently uses a simple string. To prevent the synthesizer from hallucinating a "failed" finding as a real codebase fact, wrap the error in a structured tag.

**Change**: In `consultants/engine/council.py:562`, change:
`err_text = f"(researcher lane failed: {e})"` 
to:
`err_text = f"[[LANE_ERROR]]: Researcher lane failed to provide findings due to: {e}"`

---

## Researcher (round 1)

The failure path for an unhandled exception in a parallel researcher lane is as follows:

### 1. CouncilState's additive reducers (`consultants/engine/graph.py`)
When a researcher lane crashes, the `researcher_node` catches the exception (via `try...except` at `consultants/engine/council.py:547`) and returns a "tombstone" update.
- **Additive Merge**: The `CouncilState` uses `operator.add` for the `research` and `turns` channels (`consultants/engine/graph.py:64, 68`).
- **Payload**: The failed lane contributes `{"research": [err_text], "turns": [turn], ...}` where `err_text` is `(researcher lane failed: <exception>)` (`consultants/engine/council.py:562-573`).
- **Result**: The `research` list in the global state becomes a mix of successful reports and failure strings (e.g., `["Finding A...", "(researcher lane failed: ...)", "Finding B..."]`).

### 2. `consultants/server/runner.py`'s compiled.stream loop
The runner iterates over the graph stream (`consultants/server/runner.py:137`).
- **State Update**: The `final_state` is updated via the `values` mode (`consultants/server/runner.py:139-141`), capturing the merged additive state.
- **Status Detection**: After the stream drains, the runner checks `final_state.get("error")` (`consultants/server/runner.py:172`). Since `researcher_node` sets `"error": f"researcher failed: {e}"` on failure (`consultants/engine/council.py:564`), `terminal_status` is set to `"failed"` (`consultants/server/runner.py:174`).

### 3. The Synthesizer's input messages
The synthesizer is invoked via `synthesizer_node` (`consultants/engine/council.py:715`).
- **Input Construction**: It calls `build_synthesizer_messages`, which iterates over `state.get("research")` (`consultants/engine/council.py:253-254`).
- **What it sees**: The synthesizer receives the full list of researcher reports, including the explicit failure string `(researcher lane failed: <exception>)`. It does **not** see an exception object or an empty string; it sees the failure as a textual piece of evidence.

### 4. The `storage.py` artifact writer
The runner creates a `ConsultationResult` using `final_state.get("final_answer", "")` and `terminal_status = "failed"` (`consultants/server/runner.py:186, 189`).
- **Persistence**: `storage.write_consultation` is called (`consultants/server/runner.py:199`), which atomically writes `summary.md`, `transcript.md`, and `metadata.json` (`consultants/engine/storage.py:205-207`).
- **Artifacts**: `metadata.json` records `status: "failed"` and the specific error string from the failed lane (`consultants/engine/storage.py:168`).

### Conclusion: Coherence vs. Failure
The council **produces a degraded answer** rather than a total crash. The consultation is marked as `status=failed`, but the synthesizer still attempts to write a `final_answer` based on the surviving lanes and the failure note. The user perceives a coherent but incomplete answer, potentially accompanied by the synthesizer mentioning that some research lanes failed.

### Recommended Hardening
Currently, the synthesizer is not explicitly told that a failure string in the `research` list represents a lost data lane; it just sees it as text.

**Change**: Modify `build_synthesizer_messages` in `consultants/engine/council.py` to explicitly flag failed lanes to the synthesizer.
- **Path**: `consultants/engine/council.py:253`
- **Change**: Replace the simple loop over `research_rounds` with a check: if a report starts with `(researcher lane failed:`, prepend a warning: `"WARNING: This research lane failed and provided no data: <report>"`. This prevents the synthesizer from attempting to "interpret" the error message as a factual finding.

---

## Critic (round 2)

DECISION: ready

The evidence is sufficient to trace the failure path. The researcher reports accurately identified the "tombstone" mechanism in `consultants/engine/council.py` that prevents a total graph crash, the additive reducers in `consultants/engine/graph.py` that merge failure strings into the research list, and the server's status detection in `consultants/server/runner.py` that marks the result as `failed` despite producing a partial answer. I have verified the critical lines: `consultants/engine/council.py:562` (tombstone text), `consultants/engine/graph.py:64` (additive reducer), `consultants/server/runner.py:174` (terminal status), and `consultants/engine/storage.py:207` (metadata persistence).

The council produces a **degraded but coherent answer**. The user perceives a successful response (via the `final_answer` in `summary.md`), but the consultation is internally flagged as `status: "failed"` in `metadata.json`. The synthesizer sees the error as a literal piece of evidence: `(researcher lane failed: <exception>)` (`consultants/engine/council.py:254`).

CORRECTIONS:
- report 1: claimed `consultants/engine/storage.py:108-110` for metadata json — actual `consultants/engine/storage.py:207` calls `render_metadata` which uses `asdict(result)` at `consultants/engine/storage.py:168`.
- report 2: claimed `consultants/engine/storage.py:205-207` for json — actual `consultants/engine/storage.py:207` for the write call.

Recommended Hardening:
In `consultants/engine/council.py:562`, change `err_text = f"(researcher lane failed: {e})"` to `err_text = f"[[LANE_ERROR]]: Researcher lane failed to provide findings due to: {e}"` to provide a clear structural signal to the synthesizer that this is a system failure, not a factual finding.

---

## Synthesizer

The council produces a degraded but coherent answer; the overall consultation is marked as `status=failed`, but the synthesizer still generates a response based on surviving researcher lanes.

**Failure Path Trace:**
*   **Exception Catching:** An unhandled exception in `agent_loop.runner.run_loop` is caught by the `researcher_node` wrapper at `consultants/engine/council.py:547`. This prevents the entire graph from crashing.
*   **Tombstone Generation:** The node returns a "tombstone" state update containing an error string `(researcher lane failed: <exception>)` at `consultants/engine/council.py:562`, a global error flag at `consultants/engine/council.py:564`, and the failure text added to the research list at `consultants/engine/council.py:568`.
*   **Additive Reducers:** In `consultants/engine/graph.py:64` and `consultants/engine/graph.py:68`, the `CouncilState` uses `operator.add` for `research` and `turns`. This merges the failure tombstone into the global state, resulting in a `research` list that contains a mix of successful findings and error strings.
*   **Server Stream Loop:** The `compiled.stream` loop in `consultants/server/runner.py:137` continues to run because the exception was handled within the node. After the stream completes, the runner checks `final_state.get("error")` at `consultants/server/runner.py:172` and sets `terminal_status = "failed"` at `consultants/server/runner.py:174`.
*   **Synthesizer Input:** The `build_synthesizer_messages` function iterates over the merged research list at `consultants/engine/council.py:254`. The synthesizer receives the literal string `(researcher lane failed: <exception>)` as if it were a piece of research evidence.
*   **Artifact Writer:** `consultants/server/runner.py:199` calls `storage.write_consultation`, which executes the write at `consultants/engine/storage.py:207`. The `metadata.json` file records the `status: "failed"` and the error string via `asdict(result)` at `consultants/engine/storage.py:168`, while `summary.md` contains the degraded answer.

**User Perception:**
The user receives a coherent response that appears successful but may be missing information. The failure is only visible if the user inspects the `metadata.json` or if the synthesizer explicitly mentions the failed lane in its response.

**Recommended Hardening:**
In `consultants/engine/council.py:562`, change the tombstone text to use a structured tag to prevent the synthesizer from interpreting the error as a factual finding:
`err_text = f"[[LANE_ERROR]]: Researcher lane failed to provide findings due to: {e}"`

---
