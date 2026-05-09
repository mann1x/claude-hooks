# Consultation transcript — csl-2026-05-09-1036-b147

_Question_: In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:

1. CouncilState's additive reducers (consultants/engine/graph.py)
2. consultants/server/runner.py's compiled.stream loop
3. The synthesizer's input messages — what does it actually see?
4. The storage.py artifact writer

Answer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line.


## Planner

1. Locate the `agent_loop` node definition in `consultants/engine/graph.py` and identify how `agent_loop.runner.run_loop` is invoked within a `try/except` block or if it is left naked.
2. Examine the `CouncilState` class in `consultants/engine/graph.py` to see the reducer logic for `researcher_outputs` and `messages`, specifically checking for error-type handling or filtering.
3. Trace the `Send` fan-out call in `consultants/engine/graph.py` to determine if LangGraph's default error propagation behavior is overridden or caught at the branch level.
4. Inspect the `compiled.astream_events` or `compiled.stream` loop in `consultants/server/runner.py` for exception handling that might catch sub-node failures and update the `status` in the database.
5. Identify the synthesizer node's input processing logic in `consultants/engine/graph.py` to see if it iterates over a list of results and how it handles missing or malformed data from a failed lane.
6. Verify the `update_consultation_status` calls in `consultants/server/runner.py` and `storage.py` to see if a partial graph termination triggers a `status="failed"` or allows a `status="completed"` with partial data.

---

## Researcher (round 1)

The failure path for an unhandled exception in a researcher lane (e.g., HTTP timeout or tool-executor type error) is as follows:

### 1. Failure Path Trace
*   **Researcher Node Capture**: The exception is caught in `consultants/engine/council.py:555` within `researcher_node`. Instead of crashing the graph, it returns a "tombstone" state containing `error`, `_role_failed`, and an error string in the `research` list: `(researcher lane failed: {e})` (`consultants/engine/council.py:562-566`).
*   **CouncilState Reducer**: The `research` additive reducer (`consultants/engine/graph.py:64`) concatenates this error string into the global `research` list. The `error` and `_role_failed` fields use default replacement logic (`consultants/engine/graph.py:74-75`), so the final state preserves the failure marker.
*   **Runner Loop**: The `compiled.stream` loop in `consultants/server/runner.py:137-154` continues to execute because the node returned a valid partial state rather than throwing. The graph machine progresses to the `synthesizer` node.
*   **Synthesizer Input**: The synthesizer receives the `research` list via `build_synthesizer_messages` (`consultants/engine/council.py:245`). It sees the failure marker as a legitimate report: `RESEARCHER REPORT (round 2): (researcher lane failed: ...)` (`consultants/engine/council.py:254`).
*   **Artifact Writing**: `consultants/server/runner.py:172-174` detects the `error` key in the final state and sets `terminal_status = "failed"`. The `storage.py` writer persists the synthesizer's answer but marks the metadata and summary front-matter as `status: failed` (`consultants/engine/storage.py:108`).

### 2. Concrete Conclusion
The council **still produces a coherent but degraded answer**. The user perceives a "confidently degraded" answer where the synthesizer attempts to fulfill the request using only the surviving lanes, while the overall consultation is flagged as `status=failed`.

### 3. Hardening Recommendation
The `research_rounds_used` additive reducer incorrectly multiplies the round count by the number of parallel lanes, which can prematurely exhaust the research budget for multi-round consultations after a fan-out.

**Hardening Change**: Remove the additive reducer for `research_rounds_used` to ensure fan-out lanes do not multiply the round count.
*   **File**: `consultants/engine/graph.py:69`
*   **Change**: Change `research_rounds_used: Annotated[int, operator.add]` to `research_rounds_used: int`.
*   **Note**: This also requires updating `consultants/engine/council.py:660` to return `this_round` instead of the delta `1`.

---

## Researcher (round 1)

The council produces a **coherent but degraded answer** while surfacing a global **`status=failed`**.

### 1. Trace of the Send-API failure path

*   **CouncilState reducers (`consultants/engine/graph.py`)**: Parallel researcher lanes merge results into `CouncilState` using `operator.add` for the `research` and `turns` fields (`consultants/engine/graph.py:64, 68`). However, the `error` field (`graph.py:74`) has no reducer, meaning it follows last-write-wins semantics. When a lane fails, it returns a tombstone dict containing both a specific error and an entry in the additive `research` list (`consultants/engine/council.py:562-573`).
*   **`compiled.stream` loop (`consultants/server/runner.py`)**: The runner's `compiled.stream` loop (`consultants/server/runner.py:137-154`) does **not** crash because the `researcher_node` catches the exception internally (`consultants/engine/council.py:555`). The graph continues to the synthesizer node. The outer `try/except` in the runner (`runner.py:155`) is only triggered by graph-level internal failures.
*   **Synthesizer's input messages**: The synthesizer's input is built from the `research` list (`consultants/engine/council.py:720`). It sees the tombstone string `(researcher lane failed: {e})` as one of the research reports. Because it is a robust LLM, it typically writes a final answer that incorporates the successful lanes while noting the gap in the failed lane.
*   **Storage artifact writer (`consultants/engine/storage.py`)**: The runner detects that `final_state["error"]` is set and assigns `terminal_status = "failed"` (`consultants/server/runner.py:172-174`). This status is persisted in `metadata.json` and the YAML front-matter of `summary.md` (`consultants/engine/storage.py:108`). The body of `summary.md` contains the synthesizer's coherent degraded answer.

### 2. User Perception
The user perceives the consultation as **failed** (status=failed) via the API or metadata, but finds a **usable, coherent answer** in the final `summary.md` artifact. The transcript will explicitly show the tombstone error for the crashed lane.

### 3. Hardening Recommendation
Wrap the `tool_executor` call in a `str()` cast to ensure that tool implementations returning non-string types (e.g. a `dict` from a mock or a `None` from a failed file read) do not cause a serialization crash in the `ChatClient` or a `TypeError` in the tracer.

**Change:** `output = str(tool_executor(name, args_str, cwd))`  
**Path:** `claude_hooks/agent_loop/runner.py:122`

---

## Researcher (round 1)

### 1. CouncilState Additive Reducers
*   The `research` channel uses `Annotated[list, operator.add]`, which causes parallel `Send` lanes to concatenate their results into a single list in the parent state: `consultants/engine/graph.py:64`.
*   The `turns` channel similarly uses `operator.add` to aggregate `RoleTurn` objects from all lanes for the final transcript: `consultants/engine/graph.py:68`.
*   Counters like `total_prompt_tokens` and `research_rounds_used` also use `operator.add` to sum usage across parallel lanes: `consultants/engine/graph.py:69-72`.

### 2. Runner.py Compiled Stream Loop
*   Individual researcher lane exceptions are **not** caught by the `compiled.stream` loop in `runner.py`, which only catches terminal graph-level crashes: `consultants/server/runner.py:155`.
*   Exceptions inside `run_loop` propagate to `researcher_node`, which catches them and returns a "tombstone" dictionary containing `error` and a failure string in the `research` list: `consultants/engine/council.py:555-573`.
*   The runner detects these mid-pipeline failures by checking the `error` key in the `final_state` after the stream ends: `consultants/server/runner.py:172-174`.

### 3. Synthesizer Input Messages
*   The synthesizer iterates over the `research` list (which now contains tombstone strings like `"(researcher lane failed: ...)"`) and includes them as literal "RESEARCHER REPORT" blocks: `consultants/engine/council.py:253-254`.
*   The synthesizer sees the planner's original plan and the reports from any surviving lanes alongside these failure markers: `consultants/engine/council.py:249-260`.

### 4. Storage.py Artifact Writer
*   The writer records `status: "failed"` in the `summary.md` YAML front-matter because `runner.py` passes `terminal_status="failed"` when an `error` key exists: `consultants/engine/storage.py:108-110`, `consultants/server/runner.py:189`.
*   The `final_answer` (even if degraded/hallucinated) is still written to the body of `summary.md`: `consultants/engine/storage.py:119-120`.
*   The transcript includes a turn for the failed lane showing the error message: `consultants/engine/storage.py:129-136`.

### Outcome and Perceived Failure Mode
The council **still produces a coherent-looking but degraded answer** to the user. The consultation surfaces as `status=failed` in metadata/logs, but the user perceives a "hallucination of omission": the synthesizer tries to bridge the gap left by the failed lane, often resulting in an answer that ignores the missing data or mentions the "researcher lane failed" text as if it were a finding.

### Hardening Recommendation
In `consultants/engine/council.py`, modify `synthesizer_node` to check for existing errors and abort if the research is incomplete:
```python
# consultants/engine/council.py:716
if state.get("error"):
    return {"final_answer": f"(Incomplete answer: {state.get('error')})"}
```

---

## Critic (round 3)

DECISION: ready

The researcher has successfully traced the failure path from the researcher node's internal `try/except` block to the final artifact generation. The evidence correctly identifies that LangGraph's additive reducers (`consultants/engine/graph.py:64`) allow the "tombstone" error message to merge into the global state instead of crashing the graph. It accurately captures the behavior of the synthesizer receiving these failure strings as valid input (`consultants/engine/council.py:254`) and the runner's post-stream check (`consultants/server/runner.py:172`) that marks the consultation as `status=failed` while still writing the degraded output to `summary.md` (`consultants/engine/storage.py:108`).

One critical detail confirmed: the `error` key in `CouncilState` uses last-write-wins semantics (`consultants/engine/graph.py:74`), meaning a failed lane sets the error, and the runner detects it regardless of subsequent successful nodes, provided those nodes don't explicitly clear the `error` key.

The recommended hardening at `consultants/engine/council.py:716` is specific and addresses the "hallucination of omission" failure mode where the synthesizer tries to "make do" with a crashed lane's data.

---

## Synthesizer

The council produces a coherent but degraded answer while surfacing a global `status=failed`.

1. **CouncilState Reducers**: The `research` channel uses `operator.add` (`consultants/engine/graph.py:64`), causing the "tombstone" error string generated by a failed researcher node (`consultants/engine/council.py:562-573`) to be concatenated into the global research list. The `error` field uses last-write-wins semantics (`consultants/engine/graph.py:74`), ensuring the failure marker persists in the state.
2. **Runner Loop**: The `compiled.stream` loop (`consultants/server/runner.py:137-154`) does not crash because `researcher_node` catches the exception internally (`consultants/engine/council.py:555`) and returns a valid state dictionary.
3. **Synthesizer Input**: The synthesizer receives the tombstone string (e.g., `"(researcher lane failed: ...)"`) as a literal research report via `build_synthesizer_messages` (`consultants/engine/council.py:254`).
4. **Artifact Writer**: The runner detects the `error` key in the final state and sets `terminal_status = "failed"` (`consultants/server/runner.py:172-174`). This is persisted as `status: failed` in the `summary.md` YAML front-matter (`consultants/engine/storage.py:108`), though the synthesizer's answer is still written to the file body (`consultants/engine/storage.py:119-120`).

**Perceived Failure Mode**: The user sees a "hallucination of omission." The synthesizer attempts to bridge the data gap left by the failed lane, either ignoring the missing information or treating the "researcher lane failed" error message as an actual finding.

**Hardening Recommendation**: Modify `synthesizer_node` to detect the `error` key and abort synthesis if research was incomplete to avoid misleading answers.
*   **Path**: `consultants/engine/council.py:716`
*   **Change**: Add `if state.get("error"): return {"final_answer": f"(Incomplete answer: {state.get('error')})"}` before calling the LLM.

---
