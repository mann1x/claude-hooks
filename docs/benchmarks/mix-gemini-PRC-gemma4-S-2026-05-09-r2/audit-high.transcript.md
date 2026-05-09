# Consultation transcript — csl-2026-05-09-1021-0736

_Question_: In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:

1. CouncilState's additive reducers (consultants/engine/graph.py)
2. consultants/server/runner.py's compiled.stream loop
3. The synthesizer's input messages — what does it actually see?
4. The storage.py artifact writer

Answer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line.


## Planner

1. Identify the `researcher` node definition and `CouncilState` field reducers in `consultants/engine/graph.py` to see how parallel outputs are merged.
2. Search `consultants/engine/agent_loop/runner.py` for `run_loop` to determine if exceptions are caught locally or allowed to propagate to the LangGraph orchestrator.
3. Locate the `compiled.stream` call in `consultants/server/runner.py` and check for exception handling that determines if a node failure terminates the generator.
4. Inspect the `synthesizer` node function in `consultants/engine/graph.py` to see how it accesses state and whether it validates the presence/type of researcher messages.
5. Examine `consultants/engine/storage.py` for the logic that triggers artifact persistence and whether it executes on graph `FAIL` states or only on `END`.

---

## Researcher (round 1)

Based on the code trace of the **Send-API fan-out path**, here is the failure path for an unhandled exception in a researcher lane:

### Findings
*   **Exception Handling**: The `researcher_node` wraps the `loop_runner` (which executes `run_loop`) in a broad `try...except Exception` block. It catches any unhandled exception, including HTTP timeouts or tool executor errors. `consultants/engine/council.py:555`
*   **Tombstone Generation**: Upon failure, the node returns a state update containing an `error` key and a "tombstone" message (e.g., `"(researcher lane failed: ...)"`) inside the `research` list. `consultants/engine/council.py:562-566`
*   **State Merging**: `CouncilState` defines the `research` field with an additive reducer (`Annotated[list, operator.add]`). When parallel lanes merge, the tombstone message is concatenated with the valid reports from successful lanes. `consultants/engine/graph.py:64`
*   **Synthesizer Input**: The `synthesizer_node` calls `build_synthesizer_messages`, which iterates over the merged `research` list. The synthesizer LLM explicitly sees the error tombstone strings as part of its "RESEARCHER REPORT" context. `consultants/engine/council.py:718-720`
*   **Runner Detection**: After the graph finishes, `consultants/server/runner.py` checks for the `error` key in the merged `final_state`. If present, it sets the session `status` to `"failed"`. `consultants/server/runner.py:172-174`
*   **Artifact Writing**: The `ConsultationResult` is initialized with the `final_answer` produced by the synthesizer and the `failed` status. `storage.py` writes this to `summary.md` (front-matter status) and `metadata.json`. `consultants/engine/storage.py:108-110`, `consultants/server/runner.py:186-190`

### Perceived Failure Mode
The council produces a **degraded answer**. The user perceives a coherent final response that may be incomplete or contain an apology/note about the failed investigation (because the synthesizer saw the tombstone), while the system status and metadata explicitly report `status=failed`. This behavior is intentional to avoid "silently contributing nothing" and to let the synthesizer acknowledge the gap. `consultants/engine/council.py:561-562`

### Recommended Hardening
To prevent wasting tokens and providing potentially misleading degraded answers when a core role fails, the synthesizer should short-circuit if an error is already present in the state.

**Hardening Change**:  
In `consultants/engine/council.py:718`, insert an early return check:  
`if state.get("error"): return {"final_answer": f"Consultation failed: {state['error']}", "turns": []}`

---

## Researcher (round 1)

Trace through the Send-API fan-out failure path:

*   **CouncilState additive reducers:** Parallel researcher lanes merge results into the `research` channel, which uses `operator.add` to concatenate lists of findings (`consultants/engine/graph.py:64`). This ensures that failures in one lane do not prevent the successful collection of findings from others.
*   **`compiled.stream` loop:** The loop in `consultants/server/runner.py:137-138` continues to completion even if a researcher lane fails. This is because `researcher_node` in `consultants/engine/council.py:547-573` wraps the `run_loop` call in a `try/except Exception` block, catching the failure (e.g., HTTP timeout or non-string tool return) and returning a "tombstone" update instead of propagating the exception.
*   **Synthesizer input messages:** The `synthesizer_node` calls `build_synthesizer_messages`, which iterates through the concatenated `research` list (`consultants/engine/council.py:253-254`). It explicitly sees the failure marker string `(researcher lane failed: ...)` for the crashed lane alongside successful findings from other lanes.
*   **Storage artifact writer:** After the stream drains, `runner.py:172-174` detects the `error` key in the final state and sets `terminal_status = "failed"`. The `storage.write_consultation` call (`consultants/server/runner.py:199`) then persists `summary.md` with a YAML front-matter showing `status: failed` (`consultants/engine/storage.py:108`), while the file body contains the synthesizer's degraded but coherent answer.

**Conclusion:**
The council **produces a coherent but degraded answer** and the consultation **surfaces as `status=failed`**. The user perceives an answer that may contain "tombstone" error text or omit information from the failed lane, accompanied by a failure status in the metadata/front-matter.

**Hardening recommendation:**
Wrap the tool execution in a string cast to prevent `TypeError` when a tool executor returns a non-string value.
*   **File:** `claude_hooks/agent_loop/runner.py:122`
*   **Change:** `output = str(tool_executor(name, args_str, cwd))`

---

## Researcher (round 1)

The parallel researcher lane failure path traces as follows:

*   **CouncilState Reducers:** The `research` field in `CouncilState` is defined with an additive reducer (`Annotated[list, operator.add]`) at `consultants/engine/graph.py:64`. When a parallel lane fails, its return value is merged via list concatenation into the global state.
*   **Researcher Error Handling:** The `researcher_node` catches all exceptions (including HTTP timeouts or tool execution errors) at `consultants/engine/council.py:555`. Instead of crashing the graph, it returns a "tombstone" state update at `consultants/engine/council.py:563-573`.
*   **Additive Failure Marker:** This tombstone includes an error string in the `research` list: `"(researcher lane failed: {e})"` (`consultants/engine/council.py:566`). Because of the additive reducer, this string is appended alongside successful reports from other lanes.
*   **Runner Stream Loop:** The `compiled.stream` loop in `consultants/server/runner.py:137` continues execution because the node handled the exception. However, the researcher node also sets the `error` key in the state (`consultants/engine/council.py:564`), which is detected after the loop at `consultants/server/runner.py:172`.
*   **Synthesizer Input:** The `synthesizer_node` receives the full `research` list, including the failure tombstone(s), via `build_synthesizer_messages` (`consultants/engine/council.py:253`). It treats the error string as just another researcher report.
*   **Degraded Answer Production:** The synthesizer produces a **coherent but degraded** answer based on the partial evidence available. It may ignore the failure marker or, if the model is capable, acknowledge the missing data.
*   **Status and Artifacts:** The runner sets `terminal_status = "failed"` because a node error was recorded (`consultants/server/runner.py:174`). It then calls `storage.write_consultation` (`consultants/server/runner.py:199`), which persists the degraded `final_answer` to `summary.md` and records `status: failed` in the YAML front-matter and `metadata.json` (`consultants/engine/storage.py:108-110`).

**Perceived Failure Mode:** The user receives a formatted answer that is likely incomplete (missing findings from the failed lane), but the session status is surfaced as `failed`. If the UI/client strictly checks the `status` field, it may display a generic error message despite the presence of a partial answer.

**Recommended Hardening Change:**
In `consultants/engine/council.py:562`, update the `err_text` to include the lane index and the specific plan item that failed, ensuring the synthesizer can explicitly communicate which part of the investigation was lost:
`err_text = f"(researcher lane {state.get('lane_idx', 0) + 1} failed: {e} while investigating: {plan_item or 'full plan'})"`

---

## Critic (round 3)

DECISION: ready

The researcher has successfully traced the failure path from the `researcher_node` exception handling through the state merge and into the synthesizer's context. The trace confirms that the engine follows a "graceful degradation" pattern: it catches lane-specific exceptions (`consultants/engine/council.py:555`), injects a tombstone string into the additive `research` list (`consultants/engine/graph.py:64`), and allows the synthesizer to generate a final response based on remaining valid data. The runner then marks the final session status as `failed` based on the presence of the `error` key in the terminal state (`consultants/server/runner.py:172`).

The hardening recommendation in Report 2 (`claude_hooks/agent_loop/runner.py:122`) to cast tool outputs to strings is technically more robust than the Report 3 recommendation, as it prevents a specific class of unhandled `TypeError` that would bypass the researcher's local retry logic. However, the path provided in Report 3 for the tombstone enhancement (`consultants/engine/council.py:562`) is also valid for improving the synthesizer's ability to communicate the failure. Given the prompt asks for a concrete trace and one hardening change, the current evidence is sufficient for a synthesis.

**Trace Summary:**
- **State Merging:** `research` list uses `operator.add` at `consultants/engine/graph.py:64`.
- **Stream Loop:** Continues via `compiled.stream` at `consultants/server/runner.py:137` because `researcher_node` catches the error at `consultants/engine/council.py:555`.
- **Synthesizer Input:** Receives the error string via `build_synthesizer_messages` at `consultants/engine/council.py:253`.
- **Persistence:** Status set to `failed` at `consultants/server/runner.py:174` and written to front-matter by `storage.py:108`.

---

## Synthesizer

The council produces a coherent but degraded answer and surfaces as `status=failed`.

*   **CouncilState Reducers**: The `research` field uses an additive reducer (`Annotated[list, operator.add]`) at `consultants/engine/graph.py:64`. Parallel lane outputs are concatenated; if one lane fails, its "tombstone" error message is simply appended to the list of successful findings.
*   **Runner Stream Loop**: The `compiled.stream` loop at `consultants/server/runner.py:137` does not terminate because the `researcher_node` catches all exceptions within a `try...except` block at `consultants/engine/council.py:555`, preventing the exception from propagating to the orchestrator.
*   **Synthesizer Input**: The `synthesizer_node` processes the merged `research` list via `build_synthesizer_messages` (`consultants/engine/council.py:253`). The synthesizer sees the literal string `(researcher lane failed: ...)` (`consultants/engine/council.py:566`) as a research finding and incorporates it into the final response.
*   **Storage Artifact Writer**: After the stream completes, `consultants/server/runner.py:172-174` detects the `error` key in the final state and sets the session status to `failed`. `consultants/engine/storage.py:108-110` then writes `status: failed` into the `summary.md` YAML front-matter and `metadata.json`, while the file body contains the degraded answer.

**Perceived Failure Mode**: The user receives a formatted answer that is likely incomplete (missing data from the failed lane), but the session metadata/UI explicitly indicates a failure.

**Hardening Recommendation**: Wrap tool execution results in a string cast to prevent `TypeError` exceptions when a tool returns a non-string value, avoiding the failure path entirely.
*   **Change**: `output = str(tool_executor(name, args_str, cwd))` at `claude_hooks/agent_loop/runner.py:122`.

---
