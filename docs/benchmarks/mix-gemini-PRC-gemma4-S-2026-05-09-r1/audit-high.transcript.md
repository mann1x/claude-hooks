# Consultation transcript — csl-2026-05-09-1010-8a10

_Question_: In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:

1. CouncilState's additive reducers (consultants/engine/graph.py)
2. consultants/server/runner.py's compiled.stream loop
3. The synthesizer's input messages — what does it actually see?
4. The storage.py artifact writer

Answer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line.


## Planner

1. Search `consultants/engine/agent_loop/runner.py` for `run_loop` to identify internal exception handling and whether failures are raised to the parent LangGraph node.
2. Examine `consultants/engine/graph.py` to see if the `CouncilState` reducer for `research_results` handles `None` or `Exception` objects during the fan-in merge.
3. Analyze `consultants/server/runner.py` around the `compiled.stream` call to determine if it catches exceptions from individual nodes or terminates the entire generator.
4. Inspect the synthesizer node in `consultants/engine/graph.py` to see how it filters the `research_results` list before constructing the prompt.
5. Trace `consultants/engine/storage.py` for the `ArtifactWriter` implementation to see if it persists partial outputs or requires a successful graph completion.
6. Verify the `fan_out` logic in `consultants/engine/graph.py` to see if it uses `langgraph.types.Send` and if the graph configuration includes `retry_policy` or error-handling edges.

---

## Researcher (round 1)

The failure path for an unhandled exception in a parallel researcher lane (fan-out path) is as follows:

*   **Exception Catching**: The exception (e.g., HTTP timeout or non-string tool output causing a downstream crash) is caught at the node level by the `researcher_node` wrapper's `try...except` block in `consultants/engine/council.py:555`.
*   **Tombstone Generation**: Instead of crashing the graph, the node returns a "tombstone" state update (`consultants/engine/council.py:563-573`) containing an error message in the `research` list (e.g., `"(researcher lane failed: ...)"`), increments `research_rounds_used`, and sets the top-level `error` and `_role_failed` keys.
*   **CouncilState Reducers**: LangGraph's `Send` mechanism merges this update into the global state. The `research` and `turns` lists use `operator.add` (`consultants/engine/graph.py:64,68`), so the error string is appended to the list of successful reports from other lanes. The `error` and `_role_failed` keys have no reducer and are overwritten by the latest failing lane's values (`consultants/engine/graph.py:74,75`).
*   **Stream Loop**: In `consultants/server/runner.py:137-141`, the `compiled.stream` loop continues execution because the node returned a valid update rather than raising. `final_state` accumulates the merged results.
*   **Synthesizer Input**: The `synthesizer_node` is invoked with the merged `research` list. Its input builder (`consultants/engine/council.py:253-254`) iterates over the list, including the error string `"(researcher lane failed: ...)"` as if it were a valid researcher report.
*   **Answer Production**: The synthesizer produces a `final_answer` (`consultants/engine/council.py:754`) based on the available successful reports and the visible error markers.
*   **Status & Artifacts**: After the graph finishes, `consultants/server/runner.py:172-174` detects the `error` key in `final_state` and sets `terminal_status = "failed"`. The `storage.write_consultation` call (`consultants/server/runner.py:199`) persists the artifacts with `status: failed` in the metadata, even though a coherent `final_answer` is present in `summary.md`.

**Perceived Failure Mode**: The user receives a **coherent but degraded answer**. The summary body will be missing information from the failed research lane (and might contain a note about the failure if the synthesizer includes it), while the consultation status is marked as `failed` in the session history and metadata.

**Hardening Recommendation**:
Change the `error` field in `CouncilState` to be additive so that multiple lane failures are all captured and reported, rather than having concurrent failures overwrite each other.
*   **Change**: In `consultants/engine/graph.py:74`, change `error: Optional[str]` to `error: Annotated[list[str], operator.add]`, and update `researcher_node` in `consultants/engine/council.py:564` to return `[f"researcher failed: {e}"]`.

---

## Researcher (round 1)

### Researcher Findings

*   **CouncilState Reducers**: The `research` and `turns` keys use `operator.add` as an additive reducer, allowing parallel researcher lanes to concatenate their results and transcripts into shared lists. The `error` key has no reducer, meaning the last lane to fail (or any single failure) overwrites the global error state.
    *   `consultants/engine/graph.py:64,68,74`
*   **Exception Handling in Lanes**: The `researcher_node` wraps the `agent_loop.runner.run_loop` call in a broad `try...except Exception`. If an HTTP timeout or tool error occurs, the lane does not crash the `compiled.stream` generator; instead, it returns a "tombstone" state containing an `error` message and a placeholder research report.
    *   `consultants/engine/council.py:555-573`
*   **Synthesizer Input**: The synthesizer's input messages are built by iterating over the `research` list. It does not filter out error messages; it presents them as `RESEARCHER REPORT (round N): (researcher lane failed: ...)` to the synthesizer LLM.
    *   `consultants/engine/council.py:253-254`
*   **Status and Storage**: The `runner.py` loop detects the presence of the `error` key in the final graph state and sets the `terminal_status` to `"failed"`. `storage.py` writes this status into `metadata.json` and the YAML front-matter of `summary.md`, but it still persists the `final_answer` produced by the synthesizer.
    *   `consultants/server/runner.py:172-174`
    *   `consultants/engine/storage.py:108-110,205`

### Conclusion on Failure Mode
The council **does produce an answer**, but it surfaces as **`status=failed`** in the metadata and UI. The answer is **degraded**: the synthesizer sees the technical error message in its context and either incorporates it into the response or produces an incomplete answer based only on the surviving lanes. The user perceives a "failed" consultation that nonetheless has a "summary.md" artifact containing a partial or failure-acknowledging response.

### Hardening Recommendation
Cast the `tool_executor` output to `str` to prevent a `TypeError` in logging or a downstream API failure if a tool returns a non-string (e.g., `None` or a `dict`).
*   **File**: `claude_hooks/agent_loop/runner.py:122`
*   **Change**: `output = str(tool_executor(name, args_str, cwd))`

---

## Researcher (round 1)

Unhandled exception trace in the fan-out path:

*   **CouncilState's additive reducers (`consultants/engine/graph.py:64, 68`)**: The `research` and `turns` channels use `operator.add`. When a lane fails, the `researcher_node` returns a "tombstone" state containing `{"research": [error_string], "turns": [ErrorRoleTurn]}`. These are concatenated into the global state, ensuring the failure is recorded without blocking other lanes.
*   **`consultants/server/runner.py`'s `compiled.stream` loop**: The loop does **not** crash. Because `researcher_node` catches the exception (`council.py:555`), the graph continues. The `stream` loop receives the node's `updates` payload, flips the lane's progress to `done` (`runner.py:146`), and proceeds to the synthesizer. The `try/except` block at `runner.py:155` is only hit if a node fails to catch its own exception.
*   **Synthesizer's input messages**: The synthesizer's prompt-builder (`council.py:254`) iterates over the merged `research` list. It sees the successful reports from other lanes and the tombstone message `(researcher lane failed: ...)` for the crashed lane.
*   **The `storage.py` artifact writer**: `runner.py:174` detects the `error` key in the final state (set by `researcher_node:564`) and sets `terminal_status = "failed"`. `storage.write_consultation` is called with this status, writing a `metadata.json` with `status: failed`, but a `summary.md` that contains the synthesizer's **degraded answer**.

**Conclusion**: The council produces a **coherent but degraded answer** (incorporating the failure note). The consultation surfaces as `status=failed` in metadata and logs. The user perceives an answer that acknowledges a lane failure in its reasoning or transcript.

**Recommended Hardening**:
Coerce tool executor output to string in the agent loop to prevent JSON serialization crashes in the chat client if a tool returns a non-string object.
`claude_hooks/agent_loop/runner.py:133`: change to `"content": str(output),`

---

## Critic (round 3)

DECISION: ready

The researcher has successfully traced the failure path through all four requested components, providing specific file paths and line numbers. The evidence confirms that while an exception in `run_loop` is technically "unhandled" by the runner itself, it is caught by the `researcher_node` wrapper (`consultants/engine/council.py:555`), which prevents a graph-level crash.

The trace correctly identifies:
1.  **Reducers**: `research` and `turns` use `operator.add` in `consultants/engine/graph.py:64, 68` to merge "tombstone" error strings.
2.  **Stream Loop**: `consultants/server/runner.py:137-141` continues execution because the node returns a state update instead of propagating the error.
3.  **Synthesizer Input**: The prompt builder in `consultants/engine/council.py:254` includes the error string in the context.
4.  **Storage/Status**: The `terminal_status` is set to `"failed"` in `consultants/server/runner.py:174` based on the `error` key, which is then persisted by `storage.py`.

The hardening recommendation (`claude_hooks/agent_loop/runner.py:133`) directly addresses one of the specific triggers mentioned in the prompt (non-string tool outputs). Sufficient information exists to construct the final answer.

---

## Synthesizer

The council produces a coherent but degraded answer, though the consultation surfaces as `status=failed` in metadata.

*   **CouncilState Reducers**: The `research` and `turns` keys use `operator.add` (`consultants/engine/graph.py:64, 68`). When a lane fails, `researcher_node` catches the exception (`consultants/engine/council.py:555`) and returns a "tombstone" update containing an error string. This string is appended to the global research list via the additive reducer.
*   **Compiled Stream Loop**: The `compiled.stream` loop in `consultants/server/runner.py:137-141` does not terminate because the `researcher_node` returns a valid state update instead of raising a graph-level exception. The loop continues to the synthesizer node.
*   **Synthesizer Input**: The synthesizer's prompt-builder iterates over the merged `research` list (`consultants/engine/council.py:253-254`), passing the tombstone error message (e.g., `"(researcher lane failed: ...)"`) into the LLM context as if it were a research report.
*   **Storage Artifact Writer**: The `runner.py` loop detects the `error` key in the final state (`consultants/server/runner.py:174`) and sets `terminal_status = "failed"`. `storage.py` persists this status in `metadata.json` (`consultants/engine/storage.py:108-110`), while still writing the synthesizer's output to `summary.md`.

**Perceived Failure Mode**: The user sees a "failed" status in the session history/UI, but the actual output (`summary.md`) contains a response that is missing data from the failed lane and may explicitly reference the technical error in its reasoning.

**Hardening Recommendation**:
Coerce tool executor output to a string in the agent loop to prevent `TypeError` or serialization crashes when a tool returns a non-string object.
`claude_hooks/agent_loop/runner.py:133`: change `"content": output` to `"content": str(output)`

---
