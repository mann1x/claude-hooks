# Consultation transcript — csl-2026-05-09-0918-0578

_Question_: In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:

1. CouncilState's additive reducers (consultants/engine/graph.py)
2. consultants/server/runner.py's compiled.stream loop
3. The synthesizer's input messages — what does it actually see?
4. The storage.py artifact writer

Answer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line.


## Planner

1. Locate the call site of `agent_loop.runner.run_loop` within the fan-out logic to determine if it is wrapped in a try/except block and how errors are propagated.
2. Analyze the additive reducers in `consultants/engine/graph.py` to see how they handle missing values or exception objects when aggregating parallel lane results.
3. Trace the exception propagation from the parallel worker to the `compiled.stream` loop in `consultants/server/runner.py` to see if a single lane failure triggers a global stream termination.
4. Inspect the message construction logic leading into the synthesizer to verify if it receives a partial set of successful responses or an error indicator when a lane fails.
5. Examine the artifact writer in `storage.py` to determine if it commits the current `CouncilState` regardless of lane failures or requires a fully successful graph execution.
6. Identify the specific logic that determines the final `status` (e.g., `success` vs `failed`) based on the outcome of the parallel lanes.

---

## Researcher (round 1)

The failure path for an unhandled exception in a parallel researcher lane is as follows:

### 1. Exception Handling in `researcher_node`
When `agent_loop.runner.run_loop` (invoked as `loop_runner` in `consultants/engine/council.py:548`) raises an unhandled exception, it is caught by a `try/except` block in `consultants/engine/council.py:555-573`. 
- The node does **not** propagate the exception to the graph.
- Instead, it returns a "tombstone" state update:
    - `error`: `f"researcher failed: {e}"` (`consultants/engine/council.py:564`)
    - `research`: `[f"(researcher lane failed: {e})"]` (`consultants/engine/council.py:566`)
    - `turns`: A `RoleTurn` containing the error text (`consultants/engine/council.py:568-572`).

### 2. Aggregation via Additive Reducers
The "tombstone" results are merged into the `CouncilState` using LangGraph's additive reducers defined in `consultants/engine/graph.py:64, 68-72`.
- The `research` list (Annotated with `operator.add`) concatenates the error string with findings from other successful lanes (`consultants/engine/graph.py:64`).
- The `turns` list concatenates the failure `RoleTurn`, ensuring the error is preserved in the session history (`consultants/engine/graph.py:68`).
- Total tokens are summed as 0 for the failed lane (`consultants/engine/council.py:570-571`).

### 3. Synthesizer's Input
The synthesizer receives the merged `research` list in `build_synthesizer_messages` (`consultants/engine/council.py:245-266`). 
- It sees the results of successful lanes **and** the explicit error string: `"(researcher lane failed: ...)"` as part of the researcher's report.
- Because the synthesizer is tasked to "write the final answer... consuming the researcher's report(s)" (`consultants/engine/council.py:158`), it perceives the failure as a missing piece of evidence.

### 4. Artifact Writing and Final Status
- **`consultants/server/runner.py`**: The `compiled.stream` loop completes normally because the exception was caught inside the node (`consultants/server/runner.py:137-141`).
- **Status Determination**: The runner checks `final_state.get("error")` (`consultants/server/runner.py:172`). Since the `researcher_node` set `error` in the state (`consultants/engine/council.py:564`), `terminal_status` is set to `"failed"` (`consultants/server/runner.py:174`).
- **`storage.py`**: The `ConsultationResult` is written with `status="failed"` and the `error` string (`consultants/server/runner.py:189-190`). The `summary.md` still contains the synthesizer's best effort based on the remaining lanes (`consultants/engine/storage.py:119`).

### Summary of User Perception
The consultation **produces a degraded answer** rather than a total crash.
- **User Experience**: The user receives a response that may be missing data from the failed lane.
- **Status**: The system reports `status=failed` in the metadata/logs, but the `final_answer` is still delivered.
- **Failure Mode**: "Confidently degraded" — the synthesizer attempts to answer with partial evidence, though it is explicitly told about the lane failure in its prompt.

### Recommended Hardening
The current "tombstone" approach prevents crashes but relies on the LLM to handle the error string gracefully. To ensure the synthesizer doesn't hallucinate missing data, the error should be promoted to a high-priority warning.

**Change**: In `consultants/engine/council.py:562`, change the error text to a more explicit directive that the synthesizer cannot ignore.

**Path:line**: `consultants/engine/council.py:562`
**Change**: `err_text = f"CRITICAL FAILURE: Researcher lane {state.get('lane_idx')} crashed with error: {e}. This evidence is missing from the report."`

---

## Researcher (round 1)

The trace of an unhandled exception inside `agent_loop.runner.run_loop` (e.g., HTTP timeout or type error) during a parallel researcher fan-out is as follows:

### 1. CouncilState's Additive Reducers
The `researcher_node` in `consultants/engine/council.py:547-573` wraps the `loop_runner` (which is `run_loop`) in a `try...except` block. If `run_loop` raises an unhandled exception, the `researcher_node` catches it and returns a "tombstone" state update:
- `research`: `[f"(researcher lane failed: {e})"]` (`consultants/engine/council.py:566`)
- `_role_failed`: `"researcher"` (`consultants/engine/council.py:565`)
- `turns`: A `RoleTurn` containing the error text (`consultants/engine/council.py:568-572`).

Because `CouncilState.research` is defined with an additive reducer (`Annotated[list, operator.add]` in `consultants/engine/graph.py:64`), this error string is concatenated with the successful reports from other parallel lanes.

### 2. `consultants/server/runner.py`'s compiled.stream loop
The exception is caught *inside* the node (`researcher_node`), so it does **not** propagate to the `compiled.stream` loop in `consultants/server/runner.py:137`.
- The `try...except` at `consultants/server/runner.py:155` is not triggered because the graph continues to run.
- The `final_state` is successfully updated via the `values` stream mode (`consultants/server/runner.py:139`), merging the tombstone update into the global state.

### 3. Synthesizer's Input Messages
The synthesizer receives the combined research reports in `build_synthesizer_messages` (`consultants/engine/council.py:245-266`).
- Specifically, it iterates over `state.get("research") or []` (`consultants/engine/council.py:253`).
- The synthesizer sees the successful reports and the literal string `(researcher lane failed: ...)` as part of its context.
- It does **not** receive an error indicator; it simply sees one of the "reports" as an error message and attempts to synthesize a final answer based on the remaining successful lanes.

### 4. storage.py Artifact Writer
The `run_council` function detects that a role failed by checking `final_state.get("_role_failed")` (`consultants/server/runner.py:173`).
- The `terminal_status` is set to `"completed"` if `node_error` (a global graph error) is absent, even if `node_failed` (a specific role failure) is present (`consultants/server/runner.py:174`).
- `storage.write_consultation` is called with `status="completed"` (`consultants/server/runner.py:189`).
- The `transcript.md` will contain the failure message for that specific lane (`consultants/engine/storage.py:136`), but the `summary.md` will contain the synthesizer's best effort answer.

**Conclusion**: The council **still produces a coherent answer**. It does **not** surface as `status=failed` unless the synthesizer itself fails. The user perceives a **degraded answer** where some expected information is missing, but the session completes normally.

**Recommended Hardening**:
Currently, the synthesizer is not explicitly told that a lane failed; it just sees a weird string in the research list. To improve grounding, the `researcher_node` should use a structured error format that the synthesizer can explicitly recognize as a missing data gap.

**Change**: Update `consultants/engine/council.py:562` from:
`err_text = f"(researcher lane failed: {e})"`
to:
`err_text = f"ERROR: Research lane failure. Evidence missing for this step. Details: {e}"`

---

## Researcher (round 1)

The failure path for an unhandled exception in a parallel researcher lane (via `Send` API) is as follows:

### 1. CouncilState Additive Reducers (`consultants/engine/graph.py`)
The `CouncilState` defines additive reducers for `research`, `turns`, `research_rounds_used`, and `total_*_tokens` using `operator.add` (`consultants/engine/graph.py:64, 68, 69, 71, 72`). 
When a researcher lane fails, `researcher_node` catches the exception and returns a "tombstone" update (`consultants/engine/council.py:556-573`). Because the reducer is additive, this failure payload (containing `err_text` and a `RoleTurn` marker) is appended to the global state. The `research` list will contain a string like `"(researcher lane failed: <error>)"` instead of a report, and the `turns` list will record the failure.

### 2. Runner's Compiled Stream Loop (`consultants/server/runner.py`)
The `compiled.stream` loop in `run_council` (`consultants/server/runner.py:137-141`) continuously updates `final_state` with the merged values from the graph. 
- **Crucially**, an exception *inside* a node (like `researcher_node`) is caught by the node's own `try-except` block (`consultants/engine/council.py:547-555`), meaning the exception does **not** bubble up to the runner's `compiled.stream` catch-all (`consultants/server/runner.py:155`).
- The graph continues to execute other lanes and subsequently moves to the `critic` and `synthesizer` nodes.

### 3. Synthesizer's Input Messages
The synthesizer's prompt is built using `build_synthesizer_messages`, which iterates over `state.get("research") or []` (`consultants/engine/council.py:253-254`). 
The synthesizer sees the reports from surviving lanes AND the failure string from the crashed lane (e.g., `"(researcher lane failed: ...)"`). Since the model is instructed to be "decisive" and "ground claims in actual project files" (`consultants/engine/council.py:105`), it will synthesize an answer based on the remaining evidence, effectively ignoring the failed lane's intended contribution.

### 4. Storage Artifact Writer (`consultants/engine/storage.py`)
The runner determines the `terminal_status` based on `final_state.get("error")` (`consultants/server/runner.py:172-174`). 
Because `researcher_node` sets the `error` key in its tombstone return (`consultants/engine/council.py:564`), the `terminal_status` becomes `"failed"`. This is then passed to `storage.ConsultationResult` (`consultants/server/runner.py:189`) and written to `metadata.json` and `summary.md` (`consultants/engine/storage.py:205-207`).

### Conclusion: User Perception
The council **still produces a coherent (but degraded) answer**. The user sees a final response from the synthesizer, but the `summary.md` YAML front-matter and `metadata.json` will mark `status: failed`. The failure mode is a **"confidently-degraded" answer**: the user gets a response that is missing the evidence the failed lane was supposed to find, without an explicit warning in the final answer text (unless the synthesizer chooses to mention the failure string it saw in its context).

### Recommended Hardening
The synthesizer should be explicitly told to notify the user if any researcher lanes failed, rather than silently omitting the missing data.

**Change:** Update `SYNTHESIZER_SYSTEM` in `consultants/engine/council.py:156` to include:
`"If any researcher reports contain failure markers (e.g., 'lane failed'), explicitly state which part of the plan could not be verified in your final answer."`

---

## Critic (round 3)

DECISION: ready

The researcher has comprehensively traced the failure path across all four requested architectural points. They correctly identified that the exception is caught within the `researcher_node` (`consultants/engine/council.py:556-573`), preventing a global crash in `compiled.stream` (`consultants/server/runner.py:137`), and detailed how the additive reducers (`consultants/engine/graph.py:64`) propagate a "tombstone" error string to the synthesizer's context (`consultants/engine/council.py:253`). The logic for the final `status="failed"` assignment (`consultants/server/runner.py:172-174`) and the subsequent artifact write is explicitly cited. The evidence is sufficient for the synthesizer to provide a concrete answer and the recommended hardening fix.

---

## Synthesizer

The council produces a coherent but degraded answer; the session is marked as `status=failed` in metadata, but a final response is still delivered to the user.

*   **CouncilState Reducers**: The `researcher_node` catches the exception internally (`consultants/engine/council.py:556-573`) and returns a "tombstone" update. Because `CouncilState.research` and `CouncilState.turns` use `operator.add` reducers (`consultants/engine/graph.py:64, 68`), the error string `"(researcher lane failed: ...)"` is concatenated into the global state alongside successful reports.
*   **Runner Stream Loop**: The exception does not propagate to `consultants/server/runner.py:137` because it is handled inside the node. The `compiled.stream` loop continues normally, allowing the graph to proceed to the synthesizer.
*   **Synthesizer Input**: The synthesizer's prompt is constructed by iterating over the `research` list (`consultants/engine/council.py:253`). It receives the successful findings and the literal error string, treating the latter as a piece of context.
*   **Artifact Writer**: The runner detects the `error` key set by the failed researcher node (`consultants/engine/council.py:564`) and assigns `terminal_status = "failed"` (`consultants/server/runner.py:174`). This status is persisted via `storage.write_consultation` (`consultants/server/runner.py:189`), while the `summary.md` is still written with the synthesizer's best-effort response (`consultants/engine/storage.py:119`).

**Failure Mode**: The user perceives a "confidently degraded" answer. They receive a final response that may be missing critical information, and while the metadata indicates a failure, the answer text itself typically ignores the lane failure unless the LLM happens to mention the error string.

**Recommended Hardening**:
Update `SYNTHESIZER_SYSTEM` in `consultants/engine/council.py:156` to include: `"If any researcher reports contain failure markers (e.g., 'lane failed'), explicitly state in your final answer which specific part of the plan could not be verified."`

---
