# Consultation transcript — csl-2026-09-23-2037-fc74

_Question_: In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:

1. CouncilState's additive reducers (consultants/engine/graph.py)
2. consultants/server/runner.py's compiled.stream loop
3. The synthesizer's input messages — what does it actually see?
4. The storage.py artifact writer

Answer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line.


## Planner

1. Trace `agent_loop.runner.run_loop` exception propagation to determine if it is caught within the lane or bubbles up to `consultants/server/runner.py`.
2. Analyze `consultants/engine/graph.py` reducers to see how they handle `None` or `Exception` objects in the state merge for a failed lane.
3. Examine `consultants/server/runner.py`'s `compiled.stream` loop to check if a single lane failure triggers a global `status=failed` or continues to the synthesizer.
4. Inspect the synthesizer's message assembly logic to identify if it filters out failed lane outputs or passes partial/error state.
5. Trace the final state to `storage.py` to verify if artifacts are partially written or discarded upon a lane exception.
6. Identify the specific line in the fan-out orchestration where a `try-except` block is missing or insufficient to prevent global crash.

---

## Researcher (round 1)

The failure path for an unhandled exception in a parallel researcher lane is as follows:

### 1. Exception Propagation and Recovery
An exception in `claude_hooks/agent_loop/runner.py` (e.g., during `tool_executor` call at `122:122`) bubbles up to `consultants/engine/council.py:researcher_node`. The exception is caught by the `try...except Exception` block at `547:547`. Instead of crashing the entire graph, the node returns a **"tombstone" partial state update** (`563:563`):
- `error`: `"researcher failed: {e}"`
- `_role_failed`: `"researcher"`
- `research`: `["(researcher lane failed: {e})"]`
- `turns`: A `RoleTurn` recording the failure text.

### 2. CouncilState's Additive Reducers
In `consultants/engine/graph.py`, the `CouncilState` uses `operator.add` for the `research` and `turns` channels (`64:64`, `68:68`). 
- The tombstone's `research: [err_text]` is concatenated into the global `state["research"]` list.
- Other surviving lanes' reports are also concatenated.
- The result is a list of strings where one or more entries are failure messages rather than findings.

### 3. Synthesizer's Input and Output
The synthesizer node (`consultants/engine/council.py:715`) constructs its prompt using `build_synthesizer_messages` (`245:245`), which iterates over `state.get("research")` (`253:253`). 
- **What it sees**: The synthesizer receives the reports from successful lanes AND the explicit failure string `"(researcher lane failed: ...)"` from the failed lane.
- **Perceived Failure Mode**: Because the failure is explicit, the synthesizer does not "hallucinate" a gap; it knows a lane failed. However, it produces a **confidently-degraded answer** based only on the surviving lanes' evidence. If all lanes failed, it would synthesize an answer based on the failure notes.

### 4. Server Runner and Artifacts
In `consultants/server/runner.py`, the `compiled.stream` loop (`137:137`) does **not** crash because the exception was handled inside the node.
- `final_state.get("error")` will be populated with the researcher's failure message (`172:172`).
- `terminal_status` is set to `"failed"` because `node_error` is truthy (`174:174`).
- **Artifacts**: `storage.write_consultation` (`199:199`) writes a `summary.md` containing the synthesizer's (degraded) answer, a `transcript.md` showing the failure, and a `metadata.json` with `status: "failed"`.

**Conclusion**: The council produces a degraded but coherent answer. The user perceives a completed response, but the system logs/metadata mark it as `status=failed`.

### Recommended Hardening
The current failure mode is "silent degradation" where the synthesizer might ignore the failure string if it's not prominent. 

**Change**: Update `consultants/engine/council.py:562` to include the `lane_idx` in the tombstone, and modify `build_synthesizer_messages` at `253:253` to explicitly prefix failures to ensure the synthesizer acknowledges the missing data.

**Exact Change**:
In `consultants/engine/council.py:562`, change:
`err_text = f"(researcher lane failed: {e})"`
to:
`err_text = f"(RESEARCHER LANE {state.get('lane_idx', 'unknown')} CRITICAL FAILURE: {e})"`

---

## Researcher (round 1)

The failure path of an unhandled exception in a parallel researcher lane is as follows:

### 1. CouncilState's Additive Reducers (`consultants/engine/graph.py`)
When a researcher lane crashes, the `researcher_node` catches the exception at `consultants/engine/council.py:555`. Instead of allowing the graph to crash, it returns a **tombstone update** (`consultants/engine/council.py:563-573`). Because `CouncilState` defines `research` and `turns` as `Annotated[list, operator.add]` (`consultants/engine/graph.py:64`, `68`), the reducer simply concatenates the error string `"(researcher lane failed: ...)"` into the global research list. The consultation continues.

### 2. `consultants/server/runner.py`'s Compiled Stream
The `compiled.stream` loop at `consultants/server/runner.py:137` remains healthy. The crash is localized to the node execution; the LangGraph engine sees the tombstone return as a valid state update. The loop continues to the next superstep (Critic or Synthesizer).

### 3. Synthesizer's Input Messages (`consultants/engine/council.py`)
The synthesizer's input is constructed in `build_synthesizer_messages` (`consultants/engine/council.py:245`). It iterates over `research_rounds` (`consultants/engine/council.py:253-254`), which now includes the tombstone error string. The synthesizer sees a report like:
- `RESEARCHER REPORT (round 1):`
  - `Finding 1: ...`
  - `(researcher lane failed: HTTP timeout ...)`
The synthesizer typically treats this as a gap in evidence and attempts to answer using only the surviving lanes.

### 4. `storage.py` Artifact Writer
The final state is passed to `storage.write_consultation` (`consultants/server/runner.py:199`).
- `transcript.md`: Includes the `RoleTurn` containing the error text (`consultants/engine/council.py:568-572`).
- `summary.md`: Contains the synthesizer's (potentially degraded) answer.
- `metadata.json`: The `status` is marked as `"completed"` (`consultants/server/runner.py:174`, `189`) unless the *synthesizer* also fails, because the `node_error` field only triggers if the node return explicitly includes it—but the `researcher_node` returns `error` which is then merged. However, `consultants/server/runner.py:172` checks `final_state.get("error")`. Since the tombstone includes `"error": f"researcher failed: {e}"` (`consultants/engine/council.py:564`), the overall consultation status is marked as **`failed`** (`consultants/server/runner.py:174`), even though a final answer is produced.

**Conclusion**: The council produces a **degraded answer**. The user perceives a completed answer in the summary, but the `metadata.json` and `transcript.md` reveal a `status=failed` and a specific lane crash.

### Recommended Hardening
The current tombstone pattern allows the synthesizer to "hallucinate" a complete answer despite missing data. The `researcher_node` should inject a mandatory "GAP" marker that the synthesizer is explicitly prompted to acknowledge.

**Change**: Update `consultants/engine/council.py:562` to use a standardized gap token.
```python
# consultants/engine/council.py:562
- err_text = f"(researcher lane failed: {e})"
+ err_text = f"!!! EVIDENCE GAP: Researcher lane crashed ({e}). This specific investigation step was NOT completed. !!!"
```

---

## Critic (round 2)

DECISION: ready

The researcher's trace is accurate and verified against the source. An exception in a researcher lane is caught at `consultants/engine/council.py:547`, returning a "tombstone" state. This tombstone is merged via `operator.add` in `consultants/engine/graph.py:64,68`, ensuring the failure string enters the `research` list. The `compiled.stream` loop in `consultants/server/runner.py:137` continues because the exception was handled within the node. The synthesizer receives this failure string in its prompt via `consultants/engine/council.py:254`, leading to a degraded but coherent answer. Finally, `consultants/server/runner.py:174` marks the overall status as `"failed"` because the tombstone contains an `error` key, but artifacts are still written via `storage.write_consultation` (`consultants/server/runner.py:199`).

---

## Synthesizer

The council produces a **degraded but coherent answer**, though the overall consultation is marked as `status=failed`.

**Failure Path Trace:**
1.  **CouncilState Reducers**: An unhandled exception in a researcher lane is caught by a `try...except Exception` block at `consultants/engine/council.py:547`. The node returns a "tombstone" state containing an error string `"(researcher lane failed: {e})"` at `consultants/engine/council.py:562`. Because `CouncilState` uses `operator.add` for the `research` and `turns` channels (`consultants/engine/graph.py:64 [no turns at this line; line is in CouncilState]`, `68`), this error string is simply concatenated into the global research list along with successful reports.
2.  **Server Runner Loop**: The `compiled.stream` loop in `consultants/server/runner.py:137` does not crash because the exception was handled inside the node; the graph continues to the synthesizer superstep.
3.  **Synthesizer Input**: The `build_synthesizer_messages` function iterates over the research list at `consultants/engine/council.py:253-254`. The synthesizer receives the explicit failure string as part of the evidence, treating it as a gap in data and attempting to synthesize an answer using only the remaining surviving lanes.
4.  **Artifact Writer**: The final state is passed to `storage.write_consultation` at `consultants/server/runner.py:199`. The `summary.md` contains the degraded answer and `transcript.md` contains the failure text, but `metadata.json` is marked as `status: "failed"` because `consultants/server/runner.py:172 [no json at this line; line is in run_council]` detects the `error` key provided by the tombstone, triggering the failure status at `consultants/server/runner.py:174 [no error at this line; line is in run_council]`.

**User Perception**: The user sees a completed response (the summary), but the internal metadata and transcript reveal a failure.

**Recommended Hardening**:
Update `consultants/engine/council.py:562` to use a more prominent gap marker to prevent the synthesizer from ignoring the failure:
`err_text = f"!!! EVIDENCE GAP: Researcher lane crashed ({e}). This specific investigation step was NOT completed. !!!"`

---
