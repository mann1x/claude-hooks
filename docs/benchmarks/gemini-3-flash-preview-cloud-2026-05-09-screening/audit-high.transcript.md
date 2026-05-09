# Consultation transcript — csl-2026-05-09-0615-63b9

_Question_: In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:

1. CouncilState's additive reducers (consultants/engine/graph.py)
2. consultants/server/runner.py's compiled.stream loop
3. The synthesizer's input messages — what does it actually see?
4. The storage.py artifact writer

Answer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line.


## Planner

1. Locate the `agent_loop` node definition and `CouncilState` reducers in `consultants/engine/graph.py` to determine how parallel results are merged and if the reducer handles `Exception` objects or missing keys.
2. Examine `agent_loop/runner.py` for try/except blocks surrounding the core `run_loop` and tool execution logic to see if exceptions are caught or propagated to the graph.
3. Trace the `compiled.stream` iteration in `consultants/server/runner.py` to identify if an exception in the stream terminates the HTTP response or allows partial completion.
4. Inspect the synthesizer node's implementation in `consultants/engine/graph.py` to see if it filters the message history for error states or proceeds with a subset of researcher data.
5. Search `consultants/storage.py` for artifact writing logic to see if it commits state per-node-completion or requires a successful end-of-graph transition.
6. Check `consultants/engine/graph.py` for the `Send` fan-out configuration to see if `on_error` behaviors or specific error-handling nodes are defined for the researcher lanes.

---

## Researcher (round 1)

Following the trace of an unhandled exception in a researcher lane:

### 1. CouncilState Reducers
*   **Additive merging**: `CouncilState` defines `research`, `turns`, `research_rounds_used`, and token counters with `Annotated[..., operator.add]` (`consultants/engine/graph.py:64-72`).
*   **Last-writer-wins**: The `error` and `_role_failed` fields (`consultants/engine/graph.py:74-75`) do **not** have additive reducers. In a parallel fan-out, if multiple lanes fail, the `error` field reflects only the last lane to report a failure.
*   **State preservation**: Parallel lanes fanned out via `Send` inherit the parent state but return only deltas. The additive reducers ensure that even if one lane fails, results from successful lanes are preserved and merged (`consultants/engine/graph.py:375-381`).

### 2. Stream Loop in `runner.py`
*   **Exception handling**: `consultants/engine/council.py:547-555` wraps the `run_loop` call in a `try...except Exception`. If `run_loop` raises an exception (e.g., a `TypeError` from a non-string tool result or a 400/500 from the LLM), the node does **not** crash the graph.
*   **Tombstone injection**: The node returns a "tombstone" state containing `error`, `_role_failed`, and a placeholder in the `research` list: `(researcher lane failed: {e})` (`consultants/engine/council.py:562-566`).
*   **Execution flow**: Because the exception is caught, `compiled.stream` in `consultants/server/runner.py:137` continues to completion. The graph proceeds to the `synthesizer` node.

### 3. Synthesizer Input
*   **Degraded context**: The synthesizer's input messages are built by `build_synthesizer_messages` (`consultants/engine/council.py:245-266`).
*   **Visibility**: It iterates over `state["research"]`, which now contains the successful reports from other lanes AND the failure tombstone string (`consultants/engine/council.py:254`).
*   **Output**: The synthesizer sees the failure as a textual report and typically produces a "degraded" answer that incorporates surviving evidence while potentially ignoring or noting the failed lane.

### 4. Artifact Writer
*   **Status marking**: In `consultants/server/runner.py:172-174`, the runner checks `final_state.get("error")`. If present, `terminal_status` is set to `"failed"`.
*   **Persistence**: `storage.write_consultation` is called with this status (`consultants/server/runner.py:199`).
*   **User perception**: The `summary.md` contains the synthesizer's answer (coherent but potentially incomplete), while `metadata.json` and the YAML front-matter explicitly show `status: failed` and the specific error message (`consultants/engine/storage.py:108-110`).

### Summary Conclusion
The council **still produces a coherent (but degraded) answer** rather than failing the entire consultation. The user perceives a successful text response, but the system metadata flags the run as `status=failed`.

### Recommendation
To prevent `run_loop` from crashing on common tool integration errors (like a `tool_executor` returning `None` or a `dict` which causes `len()` or downstream JSON schema failures), explicitly cast the tool output to a string.

**Hardening Change:**
In `claude_hooks/agent_loop/runner.py:122`, wrap the executor call in `str()`.
```python
output = str(tool_executor(name, args_str, cwd))
```

---

## Researcher (round 1)

### 1. CouncilState Additive Reducers
* `CouncilState` defines `research` and `turns` as additive lists using `Annotated[list, operator.add]`, which ensures that error notices from a failed lane are concatenated with findings from successful parallel lanes rather than overwriting them: `consultants/engine/graph.py:64, 68`.
* The `error` and `_role_failed` fields lack reducers; in a parallel fan-out, multiple failing lanes will clobber these keys, meaning `final_state` only preserves the error from the last lane to report a failure: `consultants/engine/graph.py:74-75`.

### 2. consultants/server/runner.py's compiled.stream loop
* The `compiled.stream` loop (`consultants/server/runner.py:137`) does **not** terminate when a researcher lane raises an exception because the `researcher_node` catches all `run_loop` exceptions and returns a "tombstone" state update: `consultants/engine/council.py:555-562`.
* Once the stream concludes, the runner checks `final_state.get("error")` (`consultants/server/runner.py:172`); because the researcher tombstone explicitly sets this key (`consultants/engine/council.py:564`), the run is marked with `terminal_status = "failed"`: `consultants/server/runner.py:174`.

### 3. Synthesizer's Input Messages
* The synthesizer's message builder iterates over every entry in the `research` list (`consultants/engine/council.py:253-254`). It sees the error tombstone (e.g., `"(researcher lane failed: ...)"`) as a literal `RESEARCHER REPORT` round: `consultants/engine/council.py:254`.
* Consequently, the synthesizer remains functional and incorporates the failure notice into its reasoning context, allowing it to produce a final answer based on the surviving lanes.

### 4. storage.py Artifact Writer
* `storage.write_consultation` persists the consultation metadata with `status: "failed"` and the collected error string: `consultants/engine/storage.py:200`.
* `summary.md` is rendered using the synthesizer's `final_answer` (`consultants/engine/storage.py:117-121`), while `transcript.md` includes the researcher's failure tombstone in the turn history: `consultants/engine/storage.py:136`.

### Conclusion
The council **still produces a coherent (though degraded) answer** to the user in the final summary, but the consultation **surfaces as status=failed** in all status polls and metadata. The user perceives a failure state but can still read the partial answer, which may explicitly mention that some research lanes failed.

### Recommended Hardening
**Harden the tool execution block to prevent lane-terminating crashes.**
In `claude_hooks/agent_loop/runner.py:122`, wrap the `tool_executor` call in a `try/except` block and cast the result to `str` to ensure the agent loop continues with an error message instead of triggering the researcher node's tombstone logic.

---

## Researcher (round 1)

The failure path for an unhandled exception (e.g., HTTP timeout or tool-executor `TypeError`) in a parallel researcher lane is as follows:

1.  **Node-Level Exception Handling**: The exception is caught by the `researcher_node` wrapper in `consultants/engine/council.py:555`. It does NOT crash the entire graph process at this stage. Instead, it logs the exception and returns a "tombstone" dictionary containing `error: "researcher failed: {e}"`, `_role_failed: "researcher"`, and a `research` list entry with the text `(researcher lane failed: {e})` (`consultants/engine/council.py:562-573`).
2.  **CouncilState Reducers**: LangGraph merges this tombstone into the global state. The `research` and `turns` fields use `operator.add` (`consultants/engine/graph.py:64, 68`), so the failure marker is concatenated with reports from successful lanes. However, `error` and `_role_failed` have no reducers (`consultants/engine/graph.py:74-75`) and use default overwrite; if multiple lanes fail, only the last-merged error persists.
3.  **Synthesizer Input**: The `synthesizer_node` is called with the merged state. Its input messages (`build_synthesizer_messages`) iterate through the `research` list, so the synthesizer explicitly sees the `(researcher lane failed: ...)` string as if it were a valid report (`consultants/engine/council.py:253-254`).
4.  **Runner Execution Loop**: The `compiled.stream` loop in `consultants/server/runner.py:137` continues to termination because nodes catch their own exceptions. After the graph ends, the runner checks `final_state.get("error")` (`consultants/server/runner.py:172`). Since the researcher lane set this, `terminal_status` becomes `"failed"` (`consultants/server/runner.py:174`).
5.  **Artifact Persistence**: The `storage.py` writer is called with `status="failed"` but includes the `final_answer` produced by the synthesizer (`consultants/server/runner.py:177-199`).

**Conclusion**: The council **does produce a coherent (though degraded) answer** because the synthesizer successfully consumes the remaining lanes' data and the "failure tombstone." However, the entire consultation **surfaces as `status=failed`** in the session state and the metadata artifact. The user perceives an answer in `summary.md` that likely mentions "researcher lane failed" in its reasoning or contains gaps, with the YAML front-matter explicitly marking `status: failed`.

**One Logic Bug/Hardening**: A structural bug exists in the fan-out path where `research_rounds_used` uses an additive reducer, causing parallel lanes to multiply the round count (e.g., 3 lanes = 3 rounds) and prematurely exhaust the budget for critic re-routes.

**Hardening Recommendation**:
Change the `research_rounds_used` reducer to `max` (or similar) so parallel lanes within a single round do not over-consume the consultation budget.
`consultants/engine/graph.py:69`:
```python
    research_rounds_used: Annotated[int, max]
```

---

## Critic (round 3)

DECISION: ready

The researcher has correctly traced the error propagation from the fanned-out node (`consultants/engine/council.py:555`) to the final status marking in the server runner (`consultants/server/runner.py:174`). The evidence confirms that while the consultation's terminal status is `failed`, the synthesizer successfully generates a "degraded" answer because it consumes the merged state containing both successful researcher reports and the failure tombstone via the additive `research` reducer (`consultants/engine/graph.py:64`).

The identified hardening change regarding `research_rounds_used` is a critical structural fix for the fan-out path described. If left as `operator.add`, a single parallel round with 3 researchers would decrement the remaining budget by 3, potentially causing the entire consultation to hit its turn limit prematurely due to a single fan-out.

**Hardening Change:**
`consultants/engine/graph.py:69`:
```python
research_rounds_used: Annotated[int, max]
```

---

## Synthesizer

The council produces a **coherent but degraded answer** while the consultation's terminal status is marked as **failed**.

*   **CouncilState Reducers**: The `research` list uses `Annotated[list, operator.add]` (`consultants/engine/graph.py:64`), meaning the failure notice from the crashed lane is appended alongside successful reports from other lanes. However, the `error` and `_role_failed` keys have no reducers (`consultants/engine/graph.py:74-75`) and use default overwrite; the final state preserves only the error from the last lane to fail.
*   **Stream Loop**: The `compiled.stream` loop in `consultants/server/runner.py:137` does not crash because `researcher_node` wraps the agent execution in a `try/except` block (`consultants/engine/council.py:555`). It catches the exception and returns a "tombstone" state containing the error message and a failure string in the `research` list (`consultants/engine/council.py:562-573`).
*   **Synthesizer Input**: The synthesizer's message builder iterates over the entire `research` list (`consultants/engine/council.py:253-254`). It treats the failure tombstone (e.g., `"(researcher lane failed: ...)"`) as a literal research report. The synthesizer then generates a response based on the surviving lanes' data and the explicit notification of the failure.
*   **Storage**: After the graph completes, the runner detects the `error` key in the final state and sets `terminal_status = "failed"` (`consultants/server/runner.py:172-174`). `storage.write_consultation` persists this status in `metadata.json` and the YAML front-matter of `summary.md` (`consultants/engine/storage.py:108-110, 200`), even though the `final_answer` text is successfully written to the file (`consultants/engine/storage.py:117-121`).

**Failure Mode**: The user sees a completed `summary.md` containing a functional answer that may explicitly mention a lane failure or omit expected data. However, any UI or API polling the consultation status will report it as `failed`.

**Hardening Change**: Change the `research_rounds_used` reducer from `operator.add` to `max` to prevent parallel fan-out lanes from incorrectly multiplying the round count and prematurely exhausting the consultation's turn budget.

`consultants/engine/graph.py:69`:
```python
research_rounds_used: Annotated[int, max]
```

---
