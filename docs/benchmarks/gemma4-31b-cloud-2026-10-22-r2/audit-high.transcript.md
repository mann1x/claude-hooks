# Consultation transcript — csl-2026-10-22-1248-277e

_Question_: In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:

1. CouncilState's additive reducers (consultants/engine/graph.py)
2. consultants/server/runner.py's compiled.stream loop
3. The synthesizer's input messages — what does it actually see?
4. The storage.py artifact writer

Answer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line.


## Planner

1. Search `claude_hooks/agent_loop/runner.py` for `run_loop` to identify how unhandled exceptions are propagated or caught.
2. Analyze `consultants/engine/graph.py` to locate `CouncilState` and the reducer logic that aggregates parallel researcher results, specifically looking for error handling of failed lanes.
3. Trace `consultants/server/runner.py`'s `compiled.stream` loop to determine if an exception in a parallel branch triggers a global failure or is suppressed.
4. Inspect the synthesizer's message assembly logic (likely in `consultants/engine/graph.py` or `consultants/engine/council.py`) to see if failed researcher outputs are filtered or passed as error markers.
5. Examine `consultants/engine/storage.py` to verify if partial results are committed to storage when a lane fails.
6. Evaluate the final return status of the `compiled.stream` loop to decide if the user sees `status=failed` or a degraded response.

---

## Researcher (round 1)

The failure path for an unhandled exception in a parallel researcher lane is as follows:

### 1. CouncilState's Additive Reducers
When `claude_hooks/agent_loop/runner.py:run_loop` raises an exception (e.g., HTTP timeout), it is caught by the `researcher_node` wrapper in `consultants/engine/council.py:555`. Instead of propagating the crash, the node returns a **tombstone update** (`consultants/engine/council.py:563`):
- `research`: A list containing the error string `(researcher lane failed: {e})` (`consultants/engine/council.py:566`).
- `_role_failed`: Set to `"researcher"` (`consultants/engine/council.py:565`).
- `turns`: A `RoleTurn` containing the error text (`consultants/engine/council.py:568`).

Because `CouncilState` defines `research` and `turns` with `operator.add` (`consultants/engine/graph.py:64, 68`), this error string is concatenated with the successful reports from other parallel lanes.

### 2. `compiled.stream` Loop
The exception does **not** reach the `compiled.stream` loop in `consultants/server/runner.py:137` because the node-level `try...except` block in `council.py:547` swallows it. The stream continues normally, and `final_state` is updated with the merged tombstone data (`consultants/server/runner.py:140`).

### 3. Synthesizer's Input Messages
The synthesizer receives all researcher reports via `build_synthesizer_messages` (`consultants/engine/council.py:245`). It sees the failing lane's output as a literal string in the `research_rounds` list:
- **What it sees**: `RESEARCHER REPORT (round 1): (researcher lane failed: <exception_details>)` (`consultants/engine/council.py:254`).
- The synthesizer does not know this is a system error; it treats it as a report of "failure to find information" and attempts to synthesize the final answer using only the surviving lanes.

### 4. Storage Artifact Writer
The `storage.ConsultationResult` is constructed using `final_state.get("final_answer")` (`consultants/server/runner.py:186`).
- **Status**: The result is marked as `status="failed"` because `final_state` contains the `error` key set by the failing researcher node (`consultants/server/runner.py:174`).
- **Artifacts**: `storage.write_consultation` (`consultants/engine/storage.py:200`) writes the degraded `final_answer` to `summary.md` and the error string to the `transcript.md` under the researcher's turn (`consultants/engine/storage.py:136`).

**Conclusion**: The council produces a **degraded answer**. The user perceives a "completed" look in the UI (if only looking at the answer) but the metadata and transcript reveal a `status=failed` state. The failure mode is a **confidently-degraded answer** where the synthesizer ignores the gap left by the crashed lane.

**Recommended Hardening**:
Modify `consultants/engine/council.py:720` in `synthesizer_node` to check for `_role_failed` in the state and inject a system warning into the synthesizer's prompt if any researcher lane crashed, forcing the model to acknowledge the missing data.

**Change**: 
In `consultants/engine/council.py:718`, add:
```python
if state.get("_role_failed") == "researcher":
    msgs.append({"role": "system", "content": "WARNING: One or more research lanes failed. Your answer may be incomplete."})
```

---

## Researcher (round 1)

### Failure Path Analysis: Parallel Researcher Exception

If a researcher lane raises an unhandled exception inside `agent_loop.runner.run_loop` (or any node function called by the graph), the failure propagates as follows:

**1. CouncilState's Additive Reducers (`consultants/engine/graph.py`)**
*   The researchers are fanned out via LangGraph's `Send` mechanism (`consultants/engine/graph.py:359`). 
*   The state uses additive reducers for `research` (`operator.add` at `consultants/engine/graph.py:64`) and `turns` (`operator.add` at `consultants/engine/graph.py:68`).
*   **Effect:** If a lane crashes, its partial updates (its specific `research` results and `turns`) are **never emitted**. Because these are additive, the rest of the council simply proceeds with the union of results from the surviving lanes. The failing lane contributes nothing to the state.

**2. `consultants/server/runner.py`'s `compiled.stream` Loop**
*   The graph is executed via `compiled.stream(initial, stream_mode=["updates", "values"])` (`consultants/server/runner.py:137`).
*   LangGraph's `stream` implementation propagates unhandled exceptions from nodes directly to the caller.
*   **Effect:** An unhandled exception in any researcher lane triggers the `except Exception as e` block at `consultants/server/runner.py:155`. This immediately halts the entire graph execution. The stream is broken, and subsequent nodes (Critic, Synthesizer) are **never executed**.

**3. Synthesizer's Input Messages**
*   Since the exception breaks the `compiled.stream` loop (`consultants/server/runner.py:137`), the graph never reaches the `synthesizer` node.
*   **Effect:** The synthesizer sees **nothing**. It is never invoked.

**4. The `storage.py` Artifact Writer**
*   The failure is caught by `_write_failed_artifacts(state, cwd, question, e)` (`consultants/server/runner.py:160`).
*   This helper creates a `ConsultationResult` with `status="failed"`, `error=str(exc)`, and `final_answer=f"(consultation failed: {exc})"` (`consultants/server/runner.py:224-228`).
*   **Effect:** `storage.write_consultation` is called (`consultants/server/runner.py:235`), writing a `summary.md` that explicitly states the consultation failed, and a `metadata.json` with `status: "failed"`.

### Conclusion
The council **does not produce a coherent answer**. The entire consultation **fails and surfaces as `status=failed`**. 

**User Perception:** The user receives a failure message instead of a synthesized answer. They see the specific Python exception (e.g., `HTTP timeout` or `TypeError`) as the final result.

### Recommended Hardening Change
Wrap the researcher node logic in a try-except block to capture the error into the state rather than crashing the graph. This allows the synthesizer to provide a degraded answer based on surviving lanes.

**Change:** In `consultants/engine/council.py` (the implementation of `researcher_node`), wrap the main loop in a try-except that returns `{"error": str(e), "_role_failed": "researcher"}`.

**Concrete Recommendation:**
Modify `consultants/engine/council.py` (impl of `researcher_node`) to catch exceptions and return a state update containing the error, instead of letting it propagate to `consultants/server/runner.py:155`.

---

## Critic (round 2)

DECISION: ready

The evidence provided by Researcher 1 is sufficient and verified against the project files. Researcher 2 incorrectly claimed the graph crashes; however, `consultants/engine/council.py:547-573` explicitly wraps the researcher's `loop_runner` in a `try...except` block that swallows exceptions and returns a "tombstone" state update. This allows the LangGraph `compiled.stream` loop in `consultants/server/runner.py:137` to continue to the synthesizer. The synthesizer receives the error as a literal string in the research reports via `consultants/engine/council.py:254`, leading to a "confidently-degraded" answer. The final status is marked as `failed` in `consultants/server/runner.py:174` because the `error` key is present in the state.

CORRECTIONS:
- Researcher 2: claimed the exception breaks the `compiled.stream` loop and halts the graph (`consultants/server/runner.py:155`) — actual: exception is caught at `consultants/engine/council.py:555` and converted to a state update, allowing the graph to complete.

---

## Synthesizer

The council produces a degraded answer, but the overall consultation surfaces as `status=failed`.

**Failure Path Trace**
1. **CouncilState Reducers**: An exception in `agent_loop.runner.run_loop` is caught by the `researcher_node` wrapper at `consultants/engine/council.py:555 [no stream at this line; line is in researcher_node]`. It returns a "tombstone" state update containing an error string (`consultants/engine/council.py:563-566`) and sets `_role_failed` to `"researcher"` (`consultants/engine/council.py:565`). Because `research` and `turns` use `operator.add` reducers (`consultants/engine/graph.py:64, 68`), this error string is simply appended to the lists of successful reports.
2. **`compiled.stream` Loop**: The exception is swallowed at the node level (`consultants/engine/council.py:555 [no stream at this line; line is in researcher_node]`), so it never reaches the `compiled.stream` loop in `consultants/server/runner.py:137`. The graph execution continues uninterrupted to the synthesizer.
3. **Synthesizer Input**: In `build_synthesizer_messages` (`consultants/engine/council.py:245`), the failing lane's output is treated as a standard report. The synthesizer sees the literal string: `RESEARCHER REPORT (round 1): (researcher lane failed: <exception_details>)` (`consultants/engine/council.py:254`).
4. **Storage Artifact Writer**: The final result is marked `status="failed"` at `consultants/server/runner.py:174` because the `error` key is present in the state. The `storage.write_consultation` call (`consultants/engine/storage.py:200`) writes the degraded `final_answer` to `summary.md` and the error string to `transcript.md` via `render_transcript` (`consultants/engine/storage.py:136`).

**User Perception**
The failure mode is a **confidently-degraded answer**. The user receives a response that appears complete because the synthesizer ignores the missing data gap, but the metadata and transcript reveal the `status=failed` state and the specific Python exception.

**Recommended Hardening**
Modify `consultants/engine/council.py:718` in the `synthesizer_node` to detect failed lanes and inject a system warning, forcing the model to acknowledge the missing information:
```python
if state.get("_role_failed") == "researcher":
    msgs.append({"role": "system", "content": "WARNING: One or more research lanes failed. Your answer may be incomplete."})
```

---
