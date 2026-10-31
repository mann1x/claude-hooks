# Consultation transcript — csl-2026-10-27-1209-846b

_Question_: In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:

1. CouncilState's additive reducers (consultants/engine/graph.py)
2. consultants/server/runner.py's compiled.stream loop
3. The synthesizer's input messages — what does it actually see?
4. The storage.py artifact writer

Answer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line.


## Planner

1. Locate `agent_loop.runner.run_loop` (likely in `claude_hooks/agent_loop/runner.py`) to determine how exceptions are caught or propagated to the caller.
2. Analyze `consultants/engine/graph.py` for additive reducers in `CouncilState` to see if they handle `None`, `Exception`, or missing keys when aggregating parallel researcher results.
3. Trace the execution flow in `consultants/server/runner.py` within the `compiled.stream` loop to identify if a single lane's failure triggers a global `status=failed` or is swallowed.
4. Examine the synthesizer's input construction in `consultants/engine/council.py` (or equivalent) to see if failed researcher outputs are filtered out or passed as error strings.
5. Check `consultants/server/storage.py` (or the artifact writer logic) to verify if partial results are persisted or if an exception prevents the final artifact from being written.
6. Determine if the end-user perceives a "degraded" response (missing researcher input) or a hard failure based on the exception handling found in steps 2 and 3.

---

## Researcher (round 1)

The failure path for an unhandled exception in a parallel researcher lane is as follows:

### 1. Exception Propagation & Reducers
An exception inside `agent_loop.runner.run_loop` is caught by the `researcher_node` wrapper in `consultants/engine/council.py:547-555`. Instead of propagating the exception to the graph runner, the node returns a "tombstone" state update (`consultants/engine/council.py:563-573`):
- `error`: Set to `"researcher failed: <exception>"`.
- `_role_failed`: Set to `"researcher"`.
- `research`: An additive reducer (`consultants/engine/graph.py:64`) that appends the string `"(researcher lane failed: <exception>)"` to the list of research reports.
- `turns`: An additive reducer (`consultants/engine/graph.py:68`) that appends a `RoleTurn` containing the error text.

### 2. Server Stream Loop
In `consultants/server/runner.py`, the `compiled.stream` loop (`consultants/server/runner.py:137`) does **not** crash because the exception was swallowed by the `researcher_node`. The `final_state` is updated via the `"values"` stream mode (`consultants/server/runner.py:139-141`). 
The loop finishes normally, but `final_state.get("error")` now contains the researcher's failure string (`consultants/server/runner.py:172`).

### 3. Synthesizer Input
The synthesizer receives the aggregated `research` list via `build_synthesizer_messages` (`consultants/engine/council.py:245-267`). It sees a list where some entries are valid reports and one (or more) is the tombstone string `"(researcher lane failed: ...)"`. 
The synthesizer will attempt to produce a `final_answer` based on the remaining successful lanes, effectively treating the failed lane as a gap in evidence.

### 4. Artifact Writing
The `ConsultationResult` is constructed using `final_state.get("error")` (`consultants/server/runner.py:174, 190`).
- **Status**: `terminal_status` is set to `"failed"` because `node_error` (the researcher's tombstone) is present (`consultants/server/runner.py:174`).
- **Artifacts**: `storage.write_consultation` (`consultants/engine/storage.py:200`) writes the `summary.md` (containing the degraded answer), `transcript.md` (showing the error in the researcher's turn), and `metadata.json` (marking `status: "failed"` and recording the error string).

**Conclusion**: The council produces a **degraded but coherent answer**. The user perceives a successful response in the UI (if only the answer is shown), but the underlying artifacts and internal state are marked as `status=failed`.

### Recommended Hardening
The current "tombstone" approach prevents graph crashes but marks the entire consultation as failed even if 90% of the research succeeded. The `researcher_node` should distinguish between terminal failures and recoverable lane failures.

**Change**: Modify `consultants/server/runner.py:174` to only set `terminal_status = "failed"` if the error comes from the `synthesizer` (which is truly terminal), while treating `researcher` errors as warnings that allow `status = "completed"` if a `final_answer` exists.

**Specific fix**: 
In `consultants/server/runner.py:174`, change:
`terminal_status = "failed" if node_error else "completed"`
to:
`terminal_status = "failed" if (node_error and final_state.get("_role_failed") == "synthesizer") else "completed"`

---

## Researcher (round 1)

The failure path for an unhandled exception in a parallel researcher lane is as follows:

### 1. CouncilState's Additive Reducers (`consultants/engine/graph.py`)
The `CouncilState` uses `operator.add` as a reducer for `research`, `turns`, `total_prompt_tokens`, and `total_completion_tokens` (`consultants/engine/graph.py:64, 68, 71, 72`). In a `Send` fan-out, LangGraph collects the partial updates from each parallel lane and applies these additive reducers to merge the results into the global state.

### 2. Exception Handling in `researcher_node` (`consultants/engine/council.py`)
If an unhandled exception occurs inside `agent_loop.runner.run_loop` (e.g., an HTTP timeout or `tool_executor` crash), it is caught by the `try...except` block in `researcher_node` (`consultants/engine/council.py:547-555`).
- Instead of propagating the exception to the graph runner, `researcher_node` returns a **tombstone update** (`consultants/engine/council.py:563-573`).
- This update contains a failure string `(researcher lane failed: {e})` injected into the `research` list and a `RoleTurn` recording the failure in the `turns` list.
- It also marks the state with `error` and `_role_failed: "researcher"` (`consultants/engine/council.py:564, 565`).

### 3. Synthesizer's Input Messages (`consultants/engine/council.py`)
The `synthesizer_node` builds its prompt using `build_synthesizer_messages`, which iterates over all entries in `state.get("research")` (`consultants/engine/council.py:253-254`).
- The synthesizer **actually sees the error string** as part of the research report: `RESEARCHER REPORT (round 1):\n(researcher lane failed: ...)` (`consultants/engine/council.py:238`).
- Because the failed lane contributes this string via the additive reducer, the synthesizer is aware of the failure but is not blocked by it.

### 4. The `storage.py` Artifact Writer (`consultants/server/runner.py` & `consultants/engine/storage.py`)
The `consultants/server/runner.py` loop continues to `final_state` (`consultants/server/runner.py:140`).
- It detects the node failure via `final_state.get("error")` (`consultants/server/runner.py:172`).
- The `terminal_status` is set to `"failed"` (`consultants/server/runner.py:174`).
- `storage.write_consultation` is called with this `"failed"` status (`consultants/server/runner.py:199`), and the `final_answer` (which the synthesizer produced despite the partial research) is persisted to `summary.md` (`consultants/engine/storage.py:119`).

### Conclusion: Outcome and User Perception
The council **still produces an answer**, but the consultation is marked as `status=failed`.
- **Failure Mode**: The user perceives a **degraded answer**. The synthesizer attempts to answer the question using only the surviving researcher lanes. Since it sees the failure string for the crashed lane, it may explicitly mention that some research failed or simply produce an incomplete answer based on partial evidence.
- **Status**: The overall consultation surfaces as `status=failed` in the metadata and logs because `_role_failed` was set, but the `final_answer` is still delivered.

### Recommended Hardening Change
The current failure mode allows the synthesizer to hallucinate or provide a confidently degraded answer because it treats the error string as a research finding. The synthesizer should be explicitly warned when lanes fail to prevent "confident degradation."

**Change**: Modify `build_synthesizer_messages` in `consultants/engine/council.py` to explicitly flag failed lanes.

**Path**: `consultants/engine/council.py:253`
**Recommendation**: Replace the simple `research_rounds` loop with one that detects and labels failure strings (starting with `(researcher lane failed:`) as `[LANE FAILED]` to trigger the LLM's uncertainty mechanisms.

---

## Critic (round 2)

DECISION: ready

The evidence is sufficient to trace the failure path. An unhandled exception in a researcher lane is caught by the `researcher_node` wrapper (`consultants/engine/council.py:547-555`), which prevents a graph crash by returning a "tombstone" state update. This update uses additive reducers in `CouncilState` (`consultants/engine/graph.py:64, 68`) to inject an error string into the research reports and turns. The synthesizer receives this error string in its prompt (`consultants/engine/council.py:253-254`), allowing it to produce a degraded but coherent answer based on the remaining successful lanes. However, because the `error` key is now present in the state, the server marks the entire consultation as `status=failed` (`consultants/server/runner.py:174`), despite delivering a final answer. The artifacts (summary, transcript, metadata) are still written, with the metadata recording the failure status (`consultants/server/runner.py:199`).

Recommended Hardening:
Modify `consultants/server/runner.py:174` to prevent researcher-lane failures from marking the entire consultation as failed when a final answer is produced.
Change:
`terminal_status = "failed" if node_error else "completed"`
To:
`terminal_status = "failed" if (node_error and final_state.get("_role_failed") == "synthesizer") else "completed"`

---

## Synthesizer

The council produces a degraded but coherent answer, although the consultation is marked as `status=failed` in the metadata.

*   **CouncilState Reducers**: An exception in `agent_loop.runner.run_loop` is caught by the `researcher_node` wrapper (`consultants/engine/council.py:547-555`). The node returns a "tombstone" update which uses additive reducers in `CouncilState` (`consultants/engine/graph.py:64, 68`) to append the error string `(researcher lane failed: <exception>)` to the `research` list and a failure `RoleTurn` to the `turns` list.
*   **Server Stream Loop**: The `compiled.stream` loop (`consultants/server/runner.py:137`) does not crash because the exception was swallowed. It continues to update `final_state` via the `"values"` stream mode (`consultants/server/runner.py:139-141`).
*   **Synthesizer Input**: The synthesizer receives the aggregated research list via `build_synthesizer_messages` (`consultants/engine/council.py:245-267`). It sees the tombstone error string as a research report entry (`consultants/engine/council.py:238`) and generates a `final_answer` using the surviving lanes.
*   **Storage Artifact Writer**: The server detects the error in `final_state` (`consultants/server/runner.py:172`) and sets `terminal_status = "failed"` (`consultants/server/runner.py:174`). `storage.write_consultation` (`consultants/server/runner.py:199`) then persists the degraded `final_answer` to `summary.md` (`consultants/engine/storage.py:119`) while marking the `metadata.json` as `status: "failed"`.

**User Perception**: The user receives a response that may be incomplete or explicitly mention research failures, but the internal system logs and artifacts record the session as a failure.

**Hardening Recommendation**: Modify `consultants/server/runner.py:174` to prevent researcher-lane failures from marking the entire consultation as failed when a final answer is still produced.
**Change**: Replace `terminal_status = "failed" if node_error else "completed"` with `terminal_status = "failed" if (node_error and final_state.get("_role_failed") == "synthesizer") else "completed"`.

---
