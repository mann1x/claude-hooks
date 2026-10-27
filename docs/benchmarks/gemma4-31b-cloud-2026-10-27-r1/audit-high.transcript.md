# Consultation transcript — csl-2026-10-27-1115-0a04

_Question_: In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:

1. CouncilState's additive reducers (consultants/engine/graph.py)
2. consultants/server/runner.py's compiled.stream loop
3. The synthesizer's input messages — what does it actually see?
4. The storage.py artifact writer

Answer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line.


## Planner

1. Analyze `claude_hooks/agent_loop/runner.py:run_loop` to determine if it catches all exceptions or allows them to propagate to the caller.
2. Trace how `consultants/engine/council.py` handles exceptions raised by `run_loop` during the parallel researcher fan-out (likely around the call identified at `consultants/engine/council.py:490-599`).
3. Examine `consultants/engine/graph.py` to see how the `CouncilState` additive reducers process results (or lack thereof) from failed researcher lanes.
4. Trace the error propagation from the council engine into `consultants/server/runner.py`'s `compiled.stream` loop to see if a partial failure triggers a global `status=failed`.
5. Verify the synthesizer's input construction in `consultants/engine/council.py` to see if it filters out failed researcher outputs or passes empty/error messages.
6. Inspect `consultants/engine/storage.py` to determine if the artifact writer is called upon partial failure and what it records for the failed lane.

---

## Researcher (round 1)

The failure path for an unhandled exception in a parallel researcher lane is as follows:

1.  **`agent_loop.runner.run_loop`**: This function does **not** catch top-level exceptions (e.g., HTTP timeouts or `tool_executor` type errors). An unhandled exception here propagates directly to the caller (`consultants/engine/council.py:548`).
2.  **`consultants/engine/council.py` (Researcher Node)**: The `researcher_node` explicitly catches all exceptions from `loop_runner` at `consultants/engine/council.py:555`. Instead of failing the entire graph, it returns a **"tombstone" update** (`consultants/engine/council.py:563-573`) containing:
    *   `error`: A failure string.
    *   `research`: A list containing `(researcher lane failed: {e})`.
    *   `turns`: A `RoleTurn` with the error text.
3.  **`consultants/engine/graph.py` (Additive Reducers)**: The `CouncilState` uses `operator.add` for the `research` and `turns` channels (`consultants/engine/graph.py:64, 68`). The tombstone's error message is concatenated with the successful results from other parallel lanes.
4.  **Synthesizer's Input**: The `synthesizer_node` consumes `state.get("research")` (`consultants/engine/council.py:720`). It sees the successful reports from healthy lanes and the literal string `"(researcher lane failed: ...)"` for the failed lane.
5.  **`consultants/server/runner.py`**: The `compiled.stream` loop (`consultants/server/runner.py:137`) completes normally because the exception was handled inside the node. The `final_state` is captured, and `terminal_status` is set to `"failed"` because `final_state.get("error")` is now populated (`consultants/server/runner.py:172-174`).
6.  **`consultants/engine/storage.py`**: The `ConsultationResult` is written with `status="failed"` and the specific `node_error` (`consultants/server/runner.py:189-190`), but the `final_answer` (produced by the synthesizer) is still persisted.

**Conclusion**: The council **still produces an answer**, but it is **degraded**. The user perceives a result that may have gaps (missing the failed lane's evidence), accompanied by a status of `failed` in the metadata/logs. The synthesizer is aware of the failure because the error message is injected into its context.

**Recommended Hardening**:
The synthesizer currently treats the error string as a standard research report. It should be explicitly prompted to acknowledge missing data when a lane fails.

**Change**: Update `build_synthesizer_messages` in `consultants/engine/council.py:245` to detect failure markers in `research_rounds` and add a system warning.

```python
# consultants/engine/council.py:253
# Current:
for i, r in enumerate(research_rounds, start=1):
    parts.append(f"\nRESEARCHER REPORT (round {i}):\n{r.strip()}")

# Recommended:
for i, r in enumerate(research_rounds, start=1):
    content = r.strip()
    if "lane failed" in content:
        content = f"⚠️ WARNING: This research lane failed to complete. Evidence is missing: {content}"
    parts.append(f"\nRESEARCHER REPORT (round {i}):\n{content}")
```

---

## Researcher (round 1)

The failure path for an unhandled exception in a parallel researcher lane is as follows:

### 1. CouncilState's Additive Reducers (`consultants/engine/graph.py`)
When a researcher lane is spawned via `Send` (`consultants/engine/graph.py:359`), its output is merged into the global state using `operator.add` for the `research` and `turns` channels (`consultants/engine/graph.py:64, 68`).
- The `researcher_node` catches all exceptions from `loop_runner` (`consultants/engine/council.py:547-555`).
- Instead of propagating the exception to the graph engine, it returns a "tombstone" update:
  - `research`: `["(researcher lane failed: <exception>)"]` (`consultants/engine/council.py:566`)
  - `turns`: A `RoleTurn` containing the error text (`consultants/engine/council.py:568-573`)
  - `error` and `_role_failed`: Set to signal the failure (`consultants/engine/council.py:564-565`)
- Because of the `operator.add` reducer, this error string is simply appended to the `research` list alongside any successful reports from other lanes.

### 2. Server's `compiled.stream` Loop (`consultants/server/runner.py`)
The `compiled.stream` loop (`consultants/server/runner.py:137`) does **not** see the researcher exception because the `researcher_node` caught it.
- The stream continues normally.
- `final_state` captures the merged state including the tombstone (`consultants/server/runner.py:139-141`).
- The runner detects the `node_error` (from `final_state.get("error")`) and sets `terminal_status = "failed"` (`consultants/server/runner.py:172-174`).

### 3. Synthesizer's Input Messages (`consultants/engine/council.py`)
The synthesizer receives the `research` list via `build_synthesizer_messages` (`consultants/engine/council.py:245`).
- It sees the literal string `"(researcher lane failed: ...)"` as one of the research reports (`consultants/engine/council.py:253-254`).
- The synthesizer is an LLM; it will likely incorporate this failure into its final answer (e.g., "I was unable to verify X because the researcher lane failed").

### 4. Artifact Writer (`consultants/engine/storage.py`)
The `storage.write_consultation` call (`consultants/server/runner.py:199`) uses the `ConsultationResult` object.
- `status` is set to `"failed"` because `node_error` was present (`consultants/server/runner.py:189`).
- `final_answer` contains whatever the synthesizer produced from the degraded input (`consultants/server/runner.py:186`).
- `transcript.md` records the exact error text in the researcher's turn (`consultants/engine/storage.py:136`).

### Conclusion
The council **produces a degraded answer** rather than failing entirely. The user perceives a coherent but incomplete response where the synthesizer admits (or ignores) that a portion of the research failed. The overall consultation status is marked as `failed`, but a `summary.md` is still written.

**Recommended Hardening:**
The synthesizer currently receives raw error strings. To prevent the synthesizer from hallucinating a "reason" for the failure or omitting it, the `researcher_node` should use a structured error marker that the synthesizer is explicitly prompted to handle.

**Change:** Update `consultants/engine/council.py:562` to use a distinct structural marker:
```python
# consultants/engine/council.py:562
err_text = f"### LANE_FAILURE: {e}" 
```
(And update `SYNTHESIZER_SYSTEM` to explicitly handle `LANE_FAILURE` markers).

---

## Critic (round 2)

DECISION: ready

The evidence is sufficient. The failure path is fully traced:
1.  **`agent_loop.runner.run_loop`** exceptions are caught by `researcher_node` in `consultants/engine/council.py:555`.
2.  **`CouncilState`** additive reducers (`consultants/engine/graph.py:64, 68`) merge the returned "tombstone" error string `"(researcher lane failed: ...)"` into the `research` and `turns` lists.
3.  **`consultants/server/runner.py`**'s `compiled.stream` loop completes normally, but `final_state.get("error")` (populated at `consultants/engine/council.py:564`) causes `terminal_status` to be set to `"failed"` (`consultants/server/runner.py:174`).
4.  **The synthesizer** receives the raw error string as a researcher report in `build_synthesizer_messages` (`consultants/engine/council.py:254`), producing a degraded but coherent answer.
5.  **`storage.py`** (via `runner.py:189, 199`) writes the consultation with `status="failed"` but includes the `final_answer`.

The user perceives a coherent answer that may be missing information, and the session metadata marks it as `failed`.

Recommended Hardening:
Update `consultants/engine/council.py:562` to use a structured marker like `err_text = f"### LANE_FAILURE: {e}"` to prevent the synthesizer from treating the error as valid research content.

---

## Synthesizer

The council produces a degraded answer rather than failing entirely; the user perceives a coherent but incomplete response, while the session metadata is marked as `status=failed`.

**Failure Path Trace:**
*   **Exception Origin**: An unhandled exception in `agent_loop.runner.run_loop` propagates to the `researcher_node` in `consultants/engine/council.py:555`, where it is caught by a generic `except Exception` block.
*   **Tombstone Generation**: The `researcher_node` returns a "tombstone" update containing an error string `"(researcher lane failed: {e})"` in the `research` list (`consultants/engine/council.py:566`), a `RoleTurn` with the error text (`consultants/engine/council.py:568-573`), and a populated `error` field (`consultants/engine/council.py:564`).
*   **State Reduction**: `CouncilState` uses `operator.add` to merge `research` and `turns` (`consultants/engine/graph.py:64 [no turns at this line; line is in CouncilState], 68`), appending the failure tombstone to the results of healthy lanes.
*   **Execution Loop**: The `compiled.stream` loop in `consultants/server/runner.py:137` continues normally because the exception was handled within the node. However, since `final_state.get("error")` is now populated, the runner sets `terminal_status = "failed"` (`consultants/server/runner.py:172-174`).
*   **Synthesizer Input**: The `build_synthesizer_messages` function passes the merged `research` list to the synthesizer (`consultants/engine/council.py:245 [no research at this line; line is in build_synthesizer_messages]`), meaning the LLM receives the literal string `"(researcher lane failed: ...)"` as one of its research reports (`consultants/engine/council.py:254`).
*   **Artifact Writing**: `consultants/server/runner.py:189, 199` triggers `storage.write_consultation` with `status="failed"`, but it still persists the `final_answer` produced by the synthesizer.

**Recommended Hardening:**
Update `consultants/engine/council.py:562 [no final_answer at this line; line is in researcher_node]` to use a structured marker to prevent the synthesizer from treating the error as valid research content:
`err_text = f"### LANE_FAILURE: {e}"`

---
