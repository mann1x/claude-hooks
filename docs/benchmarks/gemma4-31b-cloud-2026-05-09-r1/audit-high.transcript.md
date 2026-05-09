# Consultation transcript — csl-2026-05-09-0900-48d6

_Question_: In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:

1. CouncilState's additive reducers (consultants/engine/graph.py)
2. consultants/server/runner.py's compiled.stream loop
3. The synthesizer's input messages — what does it actually see?
4. The storage.py artifact writer

Answer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line.


## Planner

1. Examine `consultants/engine/graph.py` to identify the specific additive reducer functions for `CouncilState` and determine if they filter out exceptions or `None` values from parallel node outputs.
2. Analyze the `compiled.stream` loop in `consultants/server/runner.py` to see if the graph executor catches exceptions within individual nodes or if an unhandled exception in `run_loop` propagates to terminate the entire stream.
3. Trace the message flow to the Synthesizer node in `consultants/engine/graph.py` to verify if it receives a partial list of researcher results when one lane fails and how it handles missing expected inputs.
4. Inspect the artifact writing logic in `consultants/engine/storage.py` to see if it is called per-node or at the end of the graph, and whether it records failure states for specific lanes.
5. Verify the final response assembly in `consultants/server/runner.py` to determine if a partial graph completion results in a `status=failed` response or a degraded successful response.

---

## Researcher (round 1)

The failure path for an unhandled exception in a parallel researcher lane is as follows:

### 1. CouncilState Additive Reducers (`consultants/engine/graph.py`)
The `CouncilState` uses `operator.add` for `research` and `turns` channels (`consultants/engine/graph.py:64`, `68`). These are **not** custom reducer functions; they are standard Python list concatenations. They do not filter for exceptions, `None` values, or specific types. They assume the node returns a `dict` containing the expected list. If a node raises an unhandled exception, it never returns a value to the reducer.

### 2. `compiled.stream` Loop (`consultants/server/runner.py`)
The `compiled.stream` call is wrapped in a generic `try...except Exception` block (`consultants/server/runner.py:136-161`). 
- Because the researcher lanes are fanned out via LangGraph's `Send` (parallel execution), an unhandled exception in any single lane's `run_loop` propagates up through the graph executor.
- This triggers the `except Exception as e` block at `consultants/server/runner.py:155`.
- The runner immediately sets `state.status = "failed"`, `state.error = f"graph crashed: {e}"`, and calls `_write_failed_artifacts`.
- **The stream terminates immediately.** No further nodes (including the synthesizer) are executed.

### 3. Synthesizer's Input Messages
The synthesizer **never sees any messages**. Because the exception in the researcher lane crashes the entire graph execution, the control flow never reaches the synthesizer node. 

### 4. `storage.py` Artifact Writer
The failure path diverts to `_write_failed_artifacts` (`consultants/server/runner.py:160`), which uses `storage.ConsultationResult` with:
- `final_answer = f"(consultation failed: {exc})"` (`consultants/server/runner.py:224`)
- `status = "failed"` (`consultants/server/runner.py:227`)
- `turns = []` (`consultants/server/runner.py:225`)
This is written to disk via `storage.write_consultation` (`consultants/server/runner.py:235`).

### Conclusion
The council **does not produce a coherent answer**; the entire consultation fails and surfaces as `status=failed`. The user perceives a complete crash where the `summary.md` simply contains the exception string.

### Recommended Hardening
Wrap the researcher node's internal logic (specifically the `run_loop` call) in a `try...except` that captures the exception and returns it as a state update (e.g., updating `state['error']` and `state['_role_failed']`) rather than letting it propagate. This allows the graph to continue to the synthesizer.

**Change:** In `consultants/engine/council.py` (the logic called by `_wrap_researcher` in `graph.py:145`), wrap the `agent_loop.run_loop` call in a `try...except` block and return `{"error": str(e), "_role_failed": "researcher"}` on failure.

---

## Researcher (round 1)

The failure path for an unhandled exception in a researcher lane (e.g., HTTP timeout or non-string tool return in `run_loop`) is as follows:

### 1. CouncilState's Additive Reducers
The `CouncilState` defines `research` and `turns` as additive channels using `operator.add` (`consultants/engine/graph.py:64, 68`). When a researcher lane fails, it returns a "tombstone" dict (`consultants/engine/council.py:563-573`) containing:
- `research`: `["(researcher lane failed: <exception>)"]`
- `turns`: A `RoleTurn` object containing the failure text.
- `_role_failed`: `"researcher"`
- `error`: `"researcher failed: <exception>"`

These values are concatenated/merged into the global state. Because it is a list concatenation, the successful lanes' results are preserved, and the failure is appended as a single entry.

### 2. Runner's Compiled Stream Loop
The `compiled.stream` loop in `consultants/server/runner.py:137-141` captures the full merged state in `final_state` whenever `mode == "values"`. 
- Node-level exceptions (like the one in `researcher_node`) are caught **inside the node wrapper** (`consultants/engine/council.py:547-556`), so they do **not** trigger the `except Exception` block at `consultants/server/runner.py:155-161`.
- The graph continues to the next node (Critic or Synthesizer) because the researcher node successfully returned a value (the tombstone).

### 3. Synthesizer's Input Messages
The synthesizer receives the `final_state` which contains the `research` list. It sees the results from all successful lanes **plus** the explicit error string: `(researcher lane failed: <exception>)`. 
- The synthesizer does not "know" a lane crashed in a technical sense; it simply sees a research finding that says the lane failed.
- This allows the synthesizer to produce a coherent (though degraded) answer by synthesizing the surviving research and noting the gaps.

### 4. Storage Artifact Writer
The `runner.py` logic detects the failure via `node_error = final_state.get("error")` (`consultants/server/runner.py:172`).
- Even if a coherent answer is produced, `terminal_status` is set to `"failed"` (`consultants/server/runner.py:174`).
- `storage.write_consultation` (`consultants/server/runner.py:199`) persists this as `status="failed"` and includes the `error` string in `metadata.json` and the YAML front-matter of `summary.md` (`consultants/engine/storage.py:108-110`).

**Outcome:** The council produces a **coherent but degraded answer** (synthesizing surviving lanes), but the overall consultation surfaces as **`status=failed`**. The user perceives a working answer but sees a "failed" status in the metadata/logs.

**Hardening Recommendation:**
To prevent the overall consultation from being marked "failed" when only a sub-lane crashes (allowing the synthesizer to salvage the run), move the `_role_failed` flag to a separate `warnings` list in `CouncilState` and only set `terminal_status = "failed"` if the *Synthesizer* or *Planner* fails.

**Change:** In `consultants/server/runner.py:174`, change:
`terminal_status = "failed" if node_error else "completed"`
to:
`terminal_status = "failed" if (node_error and "synthesizer" in node_error) else "completed"` (or similar logic to distinguish critical vs. lane failures).

---

## Researcher (round 1)

The failure path for an unhandled exception in a parallel researcher lane is handled as a **non-terminal "Tombstone" return**, meaning the council produces a **degraded but successful answer** rather than a global failure.

### 1. CouncilState's Additive Reducers
In `consultants/engine/graph.py:64-72`, the state uses `operator.add` for `research` and `turns`. When a researcher lane fails, it returns a partial state update containing an error message in these lists (`consultants/engine/council.py:563-573`). Because the reducer is additive, the failing lane's "tombstone" (e.g., `"(researcher lane failed: ...)"`) is appended to the `research` list alongside successful findings from other lanes.

### 2. `consultants/server/runner.py` Stream Loop
The `compiled.stream` loop in `consultants/server/runner.py:137-141` captures the final merged state into `final_state` via the `"values"` mode.
- If an exception is raised **inside** the LangGraph orchestration (graph crash), the `try...except` block at `consultants/server/runner.py:155-161` catches it, sets `state.status = "failed"`, and calls `_write_failed_artifacts`.
- However, exceptions **inside** the researcher node are caught locally within `consultants/engine/council.py:547-555`. The node returns a valid dict instead of raising, so the graph continues. The runner then proceeds to `consultants/server/runner.py:172-174`, where it checks `final_state.get("error")`. Since `researcher_node` sets `"error": f"researcher failed: {e}"` (`consultants/engine/council.py:564`), the runner sets `terminal_status = "failed"` (`consultants/server/runner.py:174`).

### 3. Synthesizer's Input Messages
The synthesizer sees the failure as a piece of evidence. In `consultants/engine/council.py:245-254`, `build_synthesizer_messages` iterates over `research_rounds` (which contains the merged `research` list). The synthesizer receives a prompt containing the successful reports from other lanes AND the tombstone string `"(researcher lane failed: ...)"` from the failed lane. It treats this as a gap in evidence.

### 4. `storage.py` Artifact Writer
The `ConsultationResult` is constructed with `status="failed"` (because `node_error` is present) but includes the `final_answer` produced by the synthesizer (`consultants/server/runner.py:177-199`).
- `consultants/engine/storage.py:117-120` writes this `final_answer` into `summary.md`.
- `consultants/engine/storage.py:123-158` writes the `RoleTurn` records into `transcript.md`, explicitly showing the failure in the researcher's turn.

**User Perception:** The user receives a coherent answer based on the remaining lanes. The "failure" is surfaced as `status: failed` in the `metadata.json` and the `summary.md` YAML front-matter, but the actual content is a degraded answer.

### Recommendation
**Hardening Change:** The `synthesizer_node` should be explicitly alerted to the *number* of failed lanes to avoid "confident hallucination" when critical research is missing.

**Change:** In `consultants/engine/council.py:718-723`, modify `build_synthesizer_messages` to count entries in `research_rounds` starting with `(researcher lane failed:` and inject a warning: *"Warning: X researcher lanes failed; your evidence is incomplete."*

---

## Critic (round 3)

DECISION: ready

The researcher has provided a complete trace across all four requested components with precise line citations. They correctly distinguished between a global graph crash (`runner.py:155`) and the localized "tombstone" error handling in the researcher node (`council.py:547-573`), which allows the synthesizer to execute. The evidence clearly demonstrates that the system produces a degraded answer but marks the overall status as `failed` (`runner.py:174`). The hardening recommendation is specific and targets the logic in `council.py:718-723`.

---

## Synthesizer

The council produces a coherent but degraded answer, though the overall consultation is marked as `status=failed`.

*   **CouncilState's additive reducers:** `consultants/engine/graph.py:64, 68` use `operator.add` for the `research` and `turns` channels. When a researcher lane fails, `consultants/engine/council.py:563-573` returns a "tombstone" dictionary containing error strings. These are concatenated into the global state lists alongside successful results.
*   **Runner's compiled.stream loop:** Local exceptions are caught within the researcher node wrapper at `consultants/engine/council.py:547-555`, which prevents the global exception handler at `consultants/server/runner.py:155` from triggering. However, because the node returns an error string, `consultants/server/runner.py:174` detects `final_state.get("error")` and sets `terminal_status = "failed"`.
*   **Synthesizer's input messages:** The synthesizer receives the merged research list via `consultants/engine/council.py:718-723`. It sees the valid reports from successful lanes and the tombstone strings (e.g., `"(researcher lane failed: ...)"`) as standard input evidence.
*   **Storage.py artifact writer:** `consultants/server/runner.py:177-199` invokes `storage.write_consultation`. The resulting `summary.md` and `metadata.json` are marked `status="failed"` (`consultants/engine/storage.py:108-110`), but the actual `final_answer` produced by the synthesizer is still written to the file (`consultants/engine/storage.py:117-120`).

**Perceived Failure Mode:** The user receives a functional response synthesized from the surviving lanes, but the system's metadata and the YAML front-matter of `summary.md` report the consultation as failed.

**Hardening Recommendation:**
Modify `consultants/engine/council.py:718-723` (`build_synthesizer_messages`) to count entries starting with `(researcher lane failed:` and inject an explicit warning into the synthesizer's prompt (e.g., *"Warning: X researcher lanes failed; your evidence is incomplete"*) to prevent confident hallucinations regarding the missing data.

---
