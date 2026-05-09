# Consultation transcript — csl-2026-05-09-0731-3531

_Question_: In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:

1. CouncilState's additive reducers (consultants/engine/graph.py)
2. consultants/server/runner.py's compiled.stream loop
3. The synthesizer's input messages — what does it actually see?
4. The storage.py artifact writer

Answer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line.


## Planner

1. Inspect `consultants/engine/graph.py` and `consultants/agent_loop/runner.py` to identify the node wrapper around `run_loop` and locate any `try/except` blocks handling researcher lane failures.
2. Examine `CouncilState` in `consultants/engine/graph.py` to determine how additive reducers (e.g., for `messages` or `research_results`) handle `None` or partial data from a crashing node.
3. Trace the `compiled.stream` loop in `consultants/server/runner.py` to see if a `StopIteration` or `Exception` from a single lane terminates the generator or is caught.
4. Analyze the synthesizer node function in `consultants/engine/graph.py` to verify how it processes the state if one expected lane key is missing or contains an error object.
5. Review `consultants/server/storage.py` to check if the `save_artifact` or status update logic depends on a "complete" state object or if it writes partial results upon graph termination.
6. Check `consultants/agent_loop/tool_executor.py` (or equivalent) to see the expected return type and where it is validated before being passed back to the `run_loop`.

---

## Researcher (round 1)

Trace of the failure path for an unhandled exception in `agent_loop.runner.run_loop` within a researcher lane:

*   **Node Exception Capture:** The `researcher_node` in `consultants/engine/council.py:547-554` wraps the `loop_runner` (which is `run_loop`) in a `try/except Exception` block. An unhandled HTTP timeout or a `TypeError` from a non-string tool return is caught at `consultants/engine/council.py:555`.
*   **1. CouncilState Additive Reducers:** The node returns a "tombstone" dict (`consultants/engine/council.py:563-573`) containing `error`, `_role_failed`, `research`, and `turns`. In `consultants/engine/graph.py`, the `research` (line 64) and `turns` (line 68) channels use `Annotated[list, operator.add]`. The tombstone's failure text `"(researcher lane failed: ...)"` is concatenated into the shared state's `research` list alongside any successful lanes.
*   **2. `compiled.stream` Loop:** In `consultants/server/runner.py:137-141`, the `compiled.stream` loop receives the merged state in `mode == "values"`. Because the exception was caught by the node wrapper, the graph does NOT crash; the stream continues to the next superstep (the synthesizer).
*   **3. Synthesizer Input:** The `synthesizer_node` calls `build_synthesizer_messages` (`consultants/engine/council.py:245-266`), which iterates over the `research` list. The synthesizer actually sees the failure message as one of the research reports: `RESEARCHER REPORT (round X): (researcher lane failed: ...)`.
*   **4. Storage Artifact Writer:** After the stream drains, `runner.py:172-174` detects the `error` key in `final_state` and sets `terminal_status = "failed"`. The `ConsultationResult` is created with `status="failed"` (line 189) and written to disk via `storage.write_consultation` (line 199).

**Outcome:** The consultation **surfaces as `status=failed`** (in both the metadata and the UI). It still produces a **degraded answer** because the synthesizer successfully runs but bases its response on a context containing the explicit failure notice for that lane.

**Failure Mode:** The user perceives a consultation that finished but is labeled as "failed" in the status indicator. The final answer in `summary.md` will likely include an apology or uncertainty regarding the missing data from the crashed lane.

**Hardening Change:**
Cast the `tool_executor` output to a string immediately to prevent downstream `TypeError` or LLM validation failures if a tool returns a non-string.
`path:line` — `claude_hooks/agent_loop/runner.py:122`
```python
-            output = tool_executor(name, args_str, cwd)
+            output = str(tool_executor(name, args_str, cwd))
```

---

## Researcher (round 1)

The failure path for an unhandled exception in `agent_loop.runner.run_loop` (caught within the node) is as follows:

*   **CouncilState Reducers**: The `researcher_node` catches the exception at `consultants/engine/council.py:555` and returns a "tombstone" dict (`consultants/engine/council.py:563-573`). Because `CouncilState` defines `research` and `turns` with `Annotated[list, operator.add]` (`consultants/engine/graph.py:64, 68`), the error string `"(researcher lane failed: ...)"` and the failure `RoleTurn` are **appended** to the lists, preserving them alongside results from successful parallel lanes.
*   **runner.py Stream Loop**: The `compiled.stream` loop in `consultants/server/runner.py:137` continues to drain. It does not terminate or raise an exception because the `researcher_node` handled the error and returned a valid partial state. The `final_state` is updated with the `error` key returned by the failed lane (`consultants/server/runner.py:140`).
*   **Synthesizer Input**: The synthesizer node receives the merged state. Its input messages, built by `build_synthesizer_messages` (`consultants/engine/council.py:245`), include the contents of the `research` list. It explicitly sees the string `"(researcher lane failed: ...)"` as one of the `RESEARCHER REPORT` blocks (`consultants/engine/council.py:254`).
*   **storage.py Artifact Writer**: In `runner.py`, `node_error` is extracted from the `final_state` (`consultants/server/runner.py:172`). Since the researcher lane set this key, `terminal_status` becomes `"failed"` (`consultants/server/runner.py:174`). `storage.write_consultation` persists this to disk, where `render_summary` includes `status: failed` and the `error` string in the YAML front-matter (`consultants/engine/storage.py:108-110`).

**Conclusion**: The council produces a **degraded answer** (the synthesizer still runs and incorporates the tombstone failure message into its reasoning), but the entire consultation surfaces as **`status=failed`**. The user perceives a coherent (if partial) final answer, but the system-level status and metadata indicate a failure.

**Recommended Hardening Change**:
Change the `error` channel in `CouncilState` to an additive list to prevent parallel lanes from overwriting each other's error messages (as the default reducer for `Optional[str]` is overwrite).
*   **File**: `consultants/engine/graph.py:74`
*   **Change**: `error: Annotated[list[str], operator.add]` (and update `researcher_node` at `council.py:564` to return a list).

---

## Researcher (round 1)

- **CouncilState's additive reducers**: In `consultants/engine/graph.py:64-72`, the channels `research`, `turns`, `research_rounds_used`, `total_prompt_tokens`, and `total_completion_tokens` use `operator.add`. When a parallel researcher lane fanned out via `Send` returns its partial state, these values are merged into the global state. Specifically, `research` and `turns` concatenate the new findings and the `RoleTurn` objects respectively.
- **runner.py's stream loop**: In `consultants/server/runner.py:137-154`, the loop consumes the `compiled.stream`. If a researcher lane raises an exception, it is caught by the internal `try...except` block in `researcher_node` (`consultants/engine/council.py:555`). The node returns a "tombstone" dict (lines 563-573) containing an `error` key and a failure message. Consequently, the graph does NOT crash; the stream continues to the `synthesizer_node`.
- **Synthesizer's input**: The synthesizer receives the merged state. In `consultants/engine/council.py:254`, it iterates over `state["research"]`, which now includes the tombstone string `"(researcher lane failed: ...)"`. The synthesizer produces a "degraded" answer based on the surviving lanes and the failure notice.
- **Storage artifact writer**: After the stream ends, `runner.py:172-174` detects the `error` key in the final state and sets `terminal_status = "failed"`. `storage.write_consultation` (called at line 199) writes `summary.md` with the synthesizer's degraded answer but with `status: failed` in the YAML front-matter.
- **Outcome**: The council produces a **degraded answer** rather than failing entirely, but the user perceives the consultation as having **status=failed**. If the exception were to occur in a way that bypasses the node's `try` block (e.g., a bug in a reducer), `runner.py:160` would call `_write_failed_artifacts`, which discards partial results and writes an empty transcript with a generic error message.
- **Tool Executor Type Hazard**: In `consultants/engine/trace.py:230`, the tracer calls `len(out or "")` on the result of the tool executor. If an executor returns a non-string (e.g., an `int`), this raises a `TypeError` before the researcher node can return its successful result.

**Recommended Hardening Change:**
In `consultants/engine/trace.py:230`, ensure the tool output is coerced to a string before calling `len()` to prevent tracer-induced crashes when a tool returns a non-string value.
```python
# consultants/engine/trace.py:230
                output_chars=len(str(out) if out is not None else ""),
```

---

## Critic (round 3)

DECISION: ready

The researcher has mapped the full failure path from the researcher node's `try/except` block to the final artifact writing. The trace confirms that while a researcher lane failure is caught and "tombstoned" into the additive `research` list, the presence of the `error` key in the final state triggers a global `status="failed"` in the server runner. This results in a "degraded" answer (the synthesizer summarizes the remaining data plus the error message) that is nonetheless marked as a failure in the metadata.

Gaps filled:
- `consultants/engine/council.py:555-573`: Exception handling and tombstone generation.
- `consultants/engine/graph.py:64-72`: Additive reducers for state merging.
- `consultants/server/runner.py:172-174`: Logic for setting `terminal_status = "failed"` based on the state's `error` key.
- `consultants/engine/trace.py:230`: Specific vulnerability point for non-string tool outputs causing a tracer crash.

---

## Synthesizer

The council produces a **degraded answer** while the consultation metadata is marked as **`status=failed`**.

*   **CouncilState Reducers**: The `researcher_node` catches the exception (`consultants/engine/council.py:555`) and returns a "tombstone" dictionary (`consultants/engine/council.py:563-573`). Because `CouncilState` defines the `research` and `turns` channels using `Annotated[list, operator.add]` (`consultants/engine/graph.py:64, 68`), the failure notification string `"(researcher lane failed: ...)"` is concatenated into the shared state alongside results from successful parallel lanes.
*   **Stream Loop**: In `consultants/server/runner.py:137-141`, the `compiled.stream` loop continues to drain. The graph does not terminate early because the node handled the exception internally and returned a valid (though error-laden) state update. 
*   **Synthesizer Input**: The `synthesizer_node` calls `build_synthesizer_messages` (`consultants/engine/council.py:245-266`), which iterates through the `research` list. The synthesizer receives the tombstone failure message as if it were a valid research report (line 254) and attempts to incorporate this "missing data" notice into its final summary.
*   **Storage Writer**: After the graph completes, the server runner extracts the `error` key from the final state (`consultants/server/runner.py:172`). Finding the tombstone error, it sets `terminal_status = "failed"` (line 174). `storage.write_consultation` (`consultants/server/runner.py:199`) persists the artifact with this failed status.

**User Perception**: The user sees a coherent summary in the UI (the "degraded" answer), but the consultation is flagged with a "failed" status indicator. The final report will likely contain explicit mentions of the researcher lane failure within the text.

**Recommended Hardening Change**:
Coerce tool executor outputs to strings in the tracer to prevent `TypeError` when `len()` is called on non-string returns (e.g., integers).
`consultants/engine/trace.py:230`
```python
- output_chars=len(out or ""),
+ output_chars=len(str(out) if out is not None else ""),
```

---
