# Consultation transcript — csl-2026-09-23-2024-9dca

_Question_: In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:

1. CouncilState's additive reducers (consultants/engine/graph.py)
2. consultants/server/runner.py's compiled.stream loop
3. The synthesizer's input messages — what does it actually see?
4. The storage.py artifact writer

Answer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line.


## Planner

1. Examine `claude_hooks/agent_loop/runner.py` to identify the exception handling boundary in `run_loop` and determine if it catches or propagates unhandled exceptions.
2. Analyze `consultants/engine/graph.py` to see how `CouncilState` reducers handle missing or errored results from parallel lanes during additive aggregation.
3. Trace the flow in `consultants/server/runner.py`'s `compiled.stream` (or equivalent loop) to check if a crashed lane triggers a global `status=failed` or allows the stream to continue.
4. Identify the exact point in `consultants/engine/graph.py` or `consultants/server/runner.py` where the synthesizer's input list is constructed to see if failed lane outputs are filtered or passed as errors.
5. Verify in `consultants/engine/storage.py` how artifacts are written when one or more parallel lanes have failed.
6. Synthesize the failure path to determine the user-perceived outcome (complete failure vs. degraded answer) and pinpoint the line for a hardening fix.

---

## Researcher (round 1)

In the consultants engine's Send-API fan-out path, an unhandled exception in a parallel researcher lane (e.g., within `agent_loop.runner.run_loop`) triggers a **global consultation failure**. It does not produce a degraded answer; it surfaces as `status=failed`.

### Exact Failure Path

1.  **`claude_hooks/agent_loop/runner.py` (The Crash)**:
    The `run_loop` function contains no top-level `try...except` block wrapping its main iteration logic (lines 215–303). Any unhandled exception (HTTP timeout from `chat_fn` or non-string output from `tool_executor` at `claude_hooks/agent_loop/runner.py:122`) propagates immediately out of the runner.

2.  **`consultants/engine/graph.py` (The Reducers)**:
    Because the exception occurs *inside* a node execution during the LangGraph superstep, the `CouncilState` additive reducers (e.g., `research: Annotated[list, operator.add]` at `consultants/engine/graph.py:64`) are **never invoked** for the crashing lane. The state is not partially updated; the node execution fails.

3.  **`consultants/server/runner.py` (The Stream)**:
    The `compiled.stream` call is wrapped in a broad `try...except Exception` block (`consultants/server/runner.py:136–161`). When a researcher lane crashes, the exception bubbles up to this block.
    *   The catch block executes `state.status = "failed"` and `state.error = f"graph crashed: {e}"` (`consultants/server/runner.py:157–158`).
    *   The loop terminates immediately, skipping the synthesizer entirely.

4.  **The Synthesizer's Input**:
    The synthesizer **never runs**. It sees nothing because the graph execution is aborted before it can be reached.

5.  **`consultants/engine/storage.py` (The Artifacts)**:
    The failure path diverts to `_write_failed_artifacts` (`consultants/server/runner.py:160`). This function creates a `ConsultationResult` where:
    *   `final_answer` is set to `(consultation failed: {exc})` (`consultants/server/runner.py:224`).
    *   `status` is `"failed"` (`consultants/server/runner.py:227`).
    *   `turns` is an empty list `[]` (`consultants/server/runner.py:225`).
    The `storage.write_consultation` call then persists this failed state to disk (`consultants/engine/storage.py:200`).

### Conclusion
The council **fails completely**. The user perceives a "graph crashed" error instead of a response.

### Recommended Hardening
Wrap the `run_loop` call in the researcher node (or the `run_loop` function itself) in a `try...except` block that returns a structured error message instead of raising.

**Change**: In `claude_hooks/agent_loop/runner.py`, wrap the `for i in range(config.max_iterations):` block (lines 215–303) in a `try...except Exception` that returns a response dict containing the error in the `content` field, allowing the LangGraph reducer to capture the failure as a "result" and allowing the synthesizer to potentially explain the failure to the user.

---

## Researcher (round 1)

The failure path for an unhandled exception in a parallel researcher lane is as follows:

### 1. CouncilState's Additive Reducers (`consultants/engine/graph.py`)
When a researcher lane fails, the exception is caught within `consultants/engine/council.py:researcher_node` (lines 547–573). Instead of allowing the exception to propagate and crash the LangGraph state machine, the node returns a **"tombstone" update**:
- `research`: `[f"(researcher lane failed: {e})"]`
- `_role_failed`: `"researcher"`
- `error`: `f"researcher failed: {e}"`
- `turns`: A `RoleTurn` containing the error text (`consultants/engine/council.py:568`).

Because `CouncilState.research` and `CouncilState.turns` use `operator.add` as reducers (`consultants/engine/graph.py:64, 68`), this error string is concatenated with the successful reports from other parallel lanes.

### 2. `consultants/server/runner.py`'s `compiled.stream` Loop
The `compiled.stream` loop (`consultants/server/runner.py:137`) continues to run. Since the exception was handled inside the node, the stream does not crash. 
- The `final_state` is updated via the `"values"` stream mode (`consultants/server/runner.py:139`), preserving the combined list of research reports (some successful, some tombstoned).
- The runner then checks for errors in the `final_state` (`consultants/server/runner.py:172`). Because `researcher_node` set the `error` key, the `terminal_status` is marked as `"failed"` (`consultants/server/runner.py:174`).

### 3. The Synthesizer's Input Messages
The synthesizer receives the state via `build_synthesizer_messages` (`consultants/engine/council.py:245`). 
- It sees the `research` list, which now contains a mix of real findings and error strings like `"(researcher lane failed: ...)"` (`consultants/engine/council.py:253`).
- The synthesizer processes this as literal text. It does not "know" a lane failed in a technical sense; it simply sees a report that says the lane failed.

### 4. The `storage.py` Artifact Writer
The runner creates a `ConsultationResult` with `status="failed"` and `error=node_error` (`consultants/server/runner.py:189-190`).
- `storage.write_consultation` (`consultants/engine/storage.py:200`) writes these to disk.
- The `summary.md` will contain the synthesizer's answer (which may be degraded) and a YAML front-matter entry `status: "failed"` and `error: "researcher failed: ..."` (`consultants/engine/storage.py:108-110`).

### Conclusion
The council **produces a degraded answer** rather than a complete failure. 
- **User Perception**: The user receives a final answer, but it may be missing critical information from the failed lane. The `summary.md` and `metadata.json` explicitly mark the session as `status: failed`, alerting the user (or an automated auditor) that the result is incomplete.

### Recommended Hardening
Currently, `researcher_node` records the error in the state, but the synthesizer is not explicitly told that this is a system failure rather than a "no findings" result. 

**Change**: Update `build_synthesizer_messages` to explicitly flag failed lanes to the LLM so it can mention the gap in its final answer.

**Location**: `consultants/engine/council.py:253`
**Recommendation**: Modify the loop that appends research rounds to check if the report starts with `(researcher lane failed:` and prepend a warning like `[SYSTEM ERROR: This research lane failed to execute]` to the report text.

---

## Critic (round 2)

DECISION: ready

The researcher's evidence is sufficient. I have verified the exact code paths across the engine and server. The researcher correctly identified that the `researcher_node` captures exceptions to prevent graph crashes, emitting "tombstone" updates that allow the consultation to proceed to synthesis.

1.  **Reducers**: Verified `consultants/engine/graph.py:64, 68` use `operator.add`, ensuring the tombstone error strings from `consultants/engine/council.py:563-573` are merged into the global research state.
2.  **Runner Loop**: Verified `consultants/server/runner.py:137-141` continues streaming because the exception is handled at the node level. The `terminal_status` is set to `"failed"` at `consultants/server/runner.py:174` because `final_state.get("error")` is populated by the node.
3.  **Synthesizer**: Verified the synthesizer receives the research list (containing the error strings) via `build_synthesizer_messages` (called at `consultants/engine/council.py:718`).
4.  **Storage**: Verified `consultants/server/runner.py:177-199` creates a `ConsultationResult` with `status="failed"` and `final_answer` populated by the synthesizer, which is then persisted via `consultants/engine/storage.py:200`.

The council produces a **degraded answer** (the synthesizer tries to work with partial/errored data), but the metadata clearly marks the session as `status: failed`.

---

## Synthesizer

The council produces a **degraded answer** rather than a complete failure; the consultation proceeds to synthesis, but the session is metadata-marked as failed.

### Failure Path Trace
1.  **CouncilState Reducers**: Exceptions in a researcher lane are caught within the `researcher_node` (`consultants/engine/council.py:547–573`). The node returns a "tombstone" update containing an error string (e.g., `(researcher lane failed: {e})`) and sets the state's `error` key. Because `CouncilState.research` and `CouncilState.turns` use `operator.add` (`consultants/engine/graph.py:64, 68`), these error strings are concatenated into the global state alongside successful reports from other lanes.
2.  **`compiled.stream` Loop**: The stream loop in `consultants/server/runner.py:137` continues to run because the exception was handled inside the node. However, after the graph completes, the runner detects the `error` key in the final state and sets the `terminal_status` to `"failed"` (`consultants/server/runner.py:174`).
3.  **Synthesizer Input**: The synthesizer receives the research list via `build_synthesizer_messages` (`consultants/engine/council.py:245`). It sees the literal error strings (e.g., `(researcher lane failed: ...)`) as part of the research findings (`consultants/engine/council.py:253`) and attempts to synthesize an answer based on this partial/corrupted data.
4.  **Artifact Writer**: `consultants/server/runner.py` creates a `ConsultationResult` where `final_answer` is the synthesizer's (potentially degraded) output, but `status` is set to `"failed"` (`consultants/server/runner.py:189-190`). This is persisted to disk via `storage.write_consultation` (`consultants/engine/storage.py:200`), resulting in a `summary.md` with a `status: "failed"` YAML header (`consultants/engine/storage.py:108-110 [no md at this line; line is in _yaml_front_matter]`).

**User Perception**: The user receives a final response, but it may be missing critical information. The user (or an automated system) is alerted to the failure via the `"failed"` status in the metadata and the presence of error strings in the research turns.

### Recommended Hardening
Update `build_synthesizer_messages` to explicitly flag system failures to the LLM so it can acknowledge the missing data in its response.

**Change**: In `consultants/engine/council.py:253`, modify the loop appending research reports to detect strings starting with `(researcher lane failed:` and prepend a clear system warning such as `[SYSTEM ERROR: This research lane failed to execute]`.

---
