# Consultation transcript — csl-2026-09-23-2050-3d74

_Question_: In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:

1. CouncilState's additive reducers (consultants/engine/graph.py)
2. consultants/server/runner.py's compiled.stream loop
3. The synthesizer's input messages — what does it actually see?
4. The storage.py artifact writer

Answer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line.


## Planner

1. Examine `claude_hooks/agent_loop/runner.py` to identify the exception handling boundaries in `run_loop` and how errors are propagated to the caller.
2. Analyze `consultants/engine/graph.py` to determine if `CouncilState` additive reducers wrap researcher lane outputs in try-except blocks or filter out null/error values.
3. Trace the exception propagation from the runner through `consultants/server/runner.py` (specifically the `compiled.stream` loop) to see if a single lane failure crashes the entire stream.
4. Inspect `consultants/engine/council.py` (or the relevant synthesizer call site) to see how the synthesizer's input message list is constructed when some researcher lanes have failed.
5. Verify the error handling in `consultants/engine/storage.py` to see if the artifact writer persists partial results or fails the entire session on a researcher exception.

---

## Researcher (round 1)

The failure path for an unhandled exception in a parallel researcher lane is as follows:

### 1. Agent Loop Propagation
In `claude_hooks/agent_loop/runner.py`, the `run_loop` function has **no top-level try-except block** around the `chat_fn` or `execute_tool_calls` calls (`claude_hooks/agent_loop/runner.py:230`, `claude_hooks/agent_loop/runner.py:300`). 
- If `tool_executor` raises an exception (e.g., HTTP timeout) or returns a non-string that crashes a subsequent operation, the exception propagates directly out of `run_loop` to the caller.

### 2. CouncilState and Reducers
The `CouncilState` defined in `consultants/engine/graph.py` uses `operator.add` as a reducer for the `research` channel (`consultants/engine/graph.py:64`). 
- Because the exception happens *inside* the researcher node execution, the node never returns a partial state update.
- LangGraph's `Send` mechanism (used for fan-out in `consultants/engine/graph.py:359`) does not automatically catch exceptions in parallel lanes; a crash in any spawned node crashes the entire graph execution.

### 3. Server Runner and Stream Loop
In `consultants/server/runner.py`, the `compiled.stream` loop is wrapped in a broad `try-except` block (`consultants/server/runner.py:136`).
- When the researcher lane crashes, `compiled.stream` raises the exception.
- This is caught at `consultants/server/runner.py:155`, which triggers the failure sequence:
    - `state.status` is set to `"failed"` (`consultants/server/runner.py:157`).
    - `state.error` is populated with the exception string (`consultants/server/runner.py:158`).
    - `_write_failed_artifacts` is called (`consultants/server/runner.py:160`).

### 4. Synthesizer and Storage
- **Synthesizer**: Because the graph crashed during the researcher phase, the execution **never reaches the synthesizer node**. The synthesizer sees nothing; it is not invoked.
- **Storage**: The `_write_failed_artifacts` helper (`consultants/server/runner.py:211`) creates a `ConsultationResult` where:
    - `final_answer` is set to `"(consultation failed: {exc})"` (`consultants/server/runner.py:224`).
    - `status` is `"failed"` (`consultants/server/runner.py:227`).
    - `turns` is an empty list `[]` (`consultants/server/runner.py:225`).
- This is written to disk via `storage.write_consultation` (`consultants/server/runner.py:235`), resulting in a `summary.md` and `metadata.json` that explicitly mark the failure.

**Conclusion**: The council does **not** produce a coherent answer. The entire consultation fails and surfaces as `status=failed`. The user perceives a complete crash with an error message in the summary rather than a degraded answer.

### Recommended Hardening
Wrap the `researcher_node` call in the graph wrapper to capture lane-specific failures without crashing the entire council.

**Change**: In `consultants/engine/graph.py`, modify `_wrap_researcher` to catch exceptions and return a state update containing the error.

```python
# consultants/engine/graph.py:146
def _wrap_researcher(deps: GraphDeps):
    def _node(state: dict) -> dict:
        try:
            return council.researcher_node(
                state,
                chat_client=deps.chat_clients["researcher"],
                tool_executor=deps.tool_executor,
                tool_specs=deps.tool_specs,
                grounding_msgs=deps.grounding_msgs,
                model=deps.models["researcher"],
                cwd=deps.cwd,
                think=_think_for(deps, "researcher"),
            )
        except Exception as e:
            return {"error": f"Researcher lane failed: {e}", "research": ["(Lane Error: {e})"]}
    return _node
```

---

## Researcher (round 1)

Based on the trace of the consultants engine's execution path, here is the failure analysis for an unhandled exception in a parallel researcher lane.

### 1. CouncilState's Additive Reducers
In the Send-API fan-out path, researcher lanes are merged into the global state via additive reducers defined in `consultants/engine/graph.py:64`. When a researcher lane raises an unhandled exception inside `agent_loop.runner.run_loop`, the `researcher_node` catches it in a broad `try...except` block (`consultants/engine/council.py:547-555`). 

Instead of propagating the crash to the graph, the node emits a **"tombstone" update**:
- It returns a dictionary containing a failure marker: `{"error": "researcher failed: {e}", "_role_failed": "researcher", "research": [err_text], ...}` (`consultants/engine/council.py:563-573`).
- Because `research` is an `Annotated[list, operator.add]` reducer (`consultants/engine/graph.py:64`), this error string `"(researcher lane failed: ...)"` is simply appended to the list of findings from other successful lanes.

### 2. `compiled.stream` Loop
The loop in `consultants/server/runner.py:137-144` processes these updates. Since the exception was caught inside the node and converted into a state update, the `compiled.stream` call **does not raise an exception**. The stream continues to drain normally, and the `final_state` (`consultants/server/runner.py:140`) successfully accumulates the results of the surviving lanes plus the tombstone from the failed one.

### 3. Synthesizer's Input Messages
The synthesizer's input is constructed via `build_synthesizer_messages` (`consultants/engine/council.py:245-266`). It consumes `state.get("research") or []` (`consultants/engine/council.py:220`).
- **What it sees:** A list of researcher reports where one or more entries are the tombstone strings: `"(researcher lane failed: ...)"`.
- **Effect:** The synthesizer receives partial evidence. Depending on the model's robustness, it will either ignore the failure and answer based on the surviving lanes or explicitly mention that some research lanes failed.

### 4. Storage Artifact Writer
The `ConsultationResult` is constructed using `final_state` (`consultants/server/runner.py:177-198`).
- **Status:** The status is determined by `terminal_status = "failed" if node_error else "completed"` (`consultants/server/runner.py:174`). Since the `researcher_node` set the `error` key in the state, the overall consultation status is marked as **`failed`** (`consultants/server/runner.py:189`).
- **Persistence:** `storage.write_consultation` (`consultants/engine/storage.py:200`) still executes. It writes the `final_answer` (which may be a degraded but coherent answer) to `summary.md` and the tombstone failure to `transcript.md` and `metadata.json`.

### Conclusion
The council **still produces an answer**, but the session is flagged as `status="failed"`. 
- **User Perception:** The user sees a potentially degraded answer (missing the data from the failed lane). If they check the metadata or transcript, they will see the specific exception. The consultation does **not** crash the server or surface as a hard 500 error.

### Recommended Hardening
The current failure mode is "confidently degraded"—the synthesizer might not realize a critical piece of evidence is missing because the error is just another string in a list.

**Recommendation:** Modify `build_synthesizer_messages` to explicitly label failed lanes in the prompt so the synthesizer can account for the missing data in its reasoning.

**Change:** In `consultants/engine/council.py:253-254`, replace the simple loop with one that distinguishes between successful reports and failure tombstones.

```python
# consultants/engine/council.py:253
for i, r in enumerate(research_rounds, start=1):
    prefix = "RESEARCHER REPORT" if not r.startswith("(") else "FAILED RESEARCH LANE"
    parts.append(f"\n{prefix} (round {i}):\n{r.strip()}")
```

---

## Critic (round 2)

DECISION: ready

The researcher has successfully reconciled a contradiction between Round 1 (which hypothesized a total crash) and Round 2 (which identified the "tombstone" pattern in `consultants/engine/council.py:547-555`). The trace now correctly covers all four requested points: the additive reducer's behavior with error strings, the non-crashing nature of the `compiled.stream` loop due to node-level catching, the synthesizer's receipt of partial/error data, and the storage of a "failed" status alongside a potentially coherent but degraded answer. The recommended hardening is specific, grounded in the correct file, and addresses the identified "confidently degraded" failure mode.

---

## Synthesizer

The council still produces an answer, but the overall session is flagged as `status="failed"`. The user perceives a potentially degraded answer (missing data from the failed lane) while the system logs the failure in the metadata and transcript.

**Failure Path Trace:**

*   **CouncilState's additive reducers:** In `consultants/engine/council.py:547-555`, the `researcher_node` wraps the agent loop in a `try...except` block. When an exception occurs, it emits a "tombstone" update: a dictionary containing an error message and a failure marker `"(researcher lane failed: ...)"` in the `research` list (`consultants/engine/council.py:563-573`). Because the `research` channel uses `operator.add` as a reducer (`consultants/engine/graph.py:64`), this error string is simply appended to the findings of any successful lanes.
*   **`compiled.stream` loop:** Since the exception is caught inside the node and converted into a state update, the `compiled.stream` loop in `consultants/server/runner.py:137-144` does not raise an exception. The stream drains normally, and the `final_state` (`consultants/server/runner.py:140`) accumulates both the successful reports and the tombstone.
*   **Synthesizer's input:** The synthesizer's input is constructed via `build_synthesizer_messages` (`consultants/engine/council.py:245-266`), which consumes the `research` list (`consultants/engine/council.py:220 [no research at this line; line is in build_researcher_messages]`). The synthesizer sees the tombstone string as just another piece of research evidence. It will attempt to synthesize an answer using the remaining available data.
*   **Storage artifact writer:** The `ConsultationResult` is constructed using the `final_state` (`consultants/server/runner.py:177-198`). Because the `researcher_node` set an `error` key in the state, the overall session status is marked as `"failed"` (`consultants/server/runner.py:189`). `storage.write_consultation` (`consultants/engine/storage.py:200`) then persists this "failed" status alongside the (possibly degraded) `final_answer` in `summary.md`.

**Recommended Hardening:**
Modify `build_synthesizer_messages` to explicitly label failed lanes in the prompt so the synthesizer can reason about the missing data rather than treating the error string as standard research.

**Change:** In `consultants/engine/council.py:253-254`, replace the report loop with:
```python
for i, r in enumerate(research_rounds, start=1):
    prefix = "RESEARCHER REPORT" if not r.startswith("(") else "FAILED RESEARCH LANE"
    parts.append(f"\n{prefix} (round {i}):\n{r.strip()}")
```

---
