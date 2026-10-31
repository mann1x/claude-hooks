# Consultation transcript — csl-2026-10-22-1300-1466

_Question_: In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:

1. CouncilState's additive reducers (consultants/engine/graph.py)
2. consultants/server/runner.py's compiled.stream loop
3. The synthesizer's input messages — what does it actually see?
4. The storage.py artifact writer

Answer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line.


## Planner

I have identified the core components of the requested trace but have not yet performed the deep-dive analysis.

**Found/Verified:**
- **`consultants/engine/graph.py`**: Exists; contains the `CouncilState` and additive reducers logic.
- **`consultants/engine/storage.py`**: Exists; contains the artifact writer.
- **`consultants/server/`**: Directory exists; contains the server-side runner logic (though I have not yet read `runner.py` to locate `compiled.stream`).
- **`claude_hooks/agent_loop/runner.py`**: Exists; contains `run_loop` where the unhandled exception is hypothesized to occur.

**How Verified:**
- Used `survey_project` to map the directory structure.
- Used `list_files` on `consultants/engine` and `consultants/server` to confirm file presence.

**Unfinished/Open:**
1. **Exception Propagation**: I have not yet traced how an exception in `agent_loop.runner.run_loop` propagates through the `CouncilState` reducers in `consultants/engine/graph.py`.
2. **Server Loop**: I have not analyzed `consultants/server/runner.py` to see if the `compiled.stream` loop catches worker exceptions or if they crash the stream.
3. **Synthesizer Visibility**: I have not determined if the synthesizer receives a partial result, an error message, or nothing at all when a lane fails.
4. **Storage Impact**: I have not verified if `storage.py` records a partial success or a global failure for the session.
5. **Hardening Recommendation**: No specific line-level recommendation can be made without the trace.

**Current State**: I am at the "Investigation Plan" phase, having mapped the files but not yet executed the `read_file` or `grep` calls required to trace the failure path.

---

## Researcher (round 1)

In the consultants engine's Send-API fan-out path, an unhandled exception inside `agent_loop.runner.run_loop` (triggered by a researcher lane) is caught and neutralized by a "tombstone" pattern, preventing a total consultation failure.

### Failure Path Trace

1.  **`consultants/engine/graph.py` (Additive Reducers)**:
    The `CouncilState` defines `research`, `turns`, and `total_*_tokens` as `Annotated[list/int, operator.add]` (`consultants/engine/graph.py:64, 68, 69, 71, 72`). When a parallel researcher lane crashes, its return value is merged into the global state using these reducers.

2.  **`consultants/engine/council.py` (Tombstone Return)**:
    The `researcher_node` explicitly wraps the `loop_runner` (which is `run_loop`) in a try-except block (`consultants/engine/council.py:547-555`). If an exception occurs, it returns a **tombstone dictionary** (`consultants/engine/council.py:563-573`):
    - `research`: `[f"(researcher lane failed: {e})"]` $\rightarrow$ additive reducer appends this error string to the research list.
    - `_role_failed`: `"researcher"` $\rightarrow$ marks the failure.
    - `turns`: A `RoleTurn` containing the error text $\rightarrow$ additive reducer appends this to the transcript.
    - `error`: `f"researcher failed: {e}"` $\rightarrow$ this value is merged into the state.

3.  **`consultants/server/runner.py` (Compiled Stream)**:
    The `compiled.stream` loop in `consultants/server/runner.py:137-142` collects the `values` (full merged state) into `final_state`. Because the `researcher_node` caught the exception and returned a valid dictionary, the graph continues to the next node (Critic or Synthesizer) rather than crashing the `compiled.stream` iterator.

4.  **Synthesizer's Input**:
    The synthesizer receives the merged `research` list (`consultants/engine/council.py:720`). It sees the reports from surviving lanes interleaved with the error string `"(researcher lane failed: ...)"`. The synthesizer then produces a **degraded but coherent answer** based on the surviving evidence.

5.  **`consultants/engine/storage.py` (Artifact Writer)**:
    The `ConsultationResult` is created using `final_state.get("final_answer")` (`consultants/server/runner.py:186`).
    - `summary.md`: Contains the degraded answer.
    - `transcript.md`: Contains the specific error message for the failed lane (`consultants/engine/storage.py:136`).
    - `metadata.json`: Sets `status="failed"` because `node_error` (from `final_state.get("error")`) is present (`consultants/server/runner.py:174, 189`).

### Conclusion
The consultation **does not fail** (status=failed refers to the metadata/internal state, but the process completes). The user perceives a **degraded answer** where some planned investigation steps are missing or explicitly noted as failed.

### Hardening Recommendation
The `researcher_node` currently captures the exception but does not attempt a "safe" fallback to a non-tool-using summary if the loop crashes halfway through (it only does this if the loop *finishes* with empty text).

**Change**: Wrap the `loop_runner` call in a more robust recovery block that attempts a single `_single_shot` summary call using whatever partial messages exist in the `payload` before emitting the tombstone.

**Exact Location**: `consultants/engine/council.py:555` (inside the `except Exception as e` block).

---

## Researcher (round 1)

The trace of an unhandled exception in a parallel researcher lane (e.g., `agent_loop.runner.run_loop` raising an HTTP timeout) is as follows:

### 1. CouncilState's Additive Reducers (`consultants/engine/graph.py`)
The researcher node is wrapped in a `try...except Exception` block in `consultants/engine/council.py:547-574`. When `run_loop` raises an exception, the `researcher_node` catches it and returns a **tombstone update**:
*   `error`: `f"researcher failed: {e}"` (`consultants/engine/council.py:564`)
*   `_role_failed`: `"researcher"` (`consultants/engine/council.py:565`)
*   `research`: `[f"(researcher lane failed: {e})"]` (`consultants/engine/council.py:566`)
*   `turns`: A `RoleTurn` object containing the error text (`consultants/engine/council.py:568-572`).

Because `CouncilState.research` and `CouncilState.turns` use `operator.add` as reducers (`consultants/engine/graph.py:64`, `consultants/engine/graph.py:68`), this tombstone is appended to the global state. Other parallel lanes continue unaffected, and their successful reports are also concatenated into the `research` list.

### 2. Server Loop (`consultants/server/runner.py`)
The `compiled.stream` loop in `consultants/server/runner.py:137-161` does **not** crash. The exception was already caught and neutralized by the `researcher_node` into a state update. The loop continues to drain, and `final_state` (`consultants/server/runner.py:140`) captures the merged result including the tombstone.

### 3. Synthesizer's Input Messages
The synthesizer consumes the merged `research` list via `build_synthesizer_messages` (`consultants/engine/council.py:245-266`). It sees the successful reports from other lanes **and** the literal string `"(researcher lane failed: ...)"` from the failed lane. 
*   **Visibility**: The synthesizer perceives the failure as a "finding" (or lack thereof) in one of the research reports. It does not see a system-level exception; it sees a textual report of a failure.

### 4. Storage Artifact Writer (`consultants/engine/storage.py`)
The runner determines the `terminal_status` by checking `final_state.get("error")` (`consultants/server/runner.py:172-174`). 
*   Since the researcher's tombstone set the `error` key (`consultants/engine/council.py:564`), `terminal_status` becomes `"failed"` (`consultants/server/runner.py:174`).
*   `storage.write_consultation` is called with `status="failed"` and the error message (`consultants/server/runner.py:189-190`).
*   The `summary.md` will contain the synthesizer's answer (which may be degraded) and the YAML front-matter will explicitly mark `status: failed` and `error: researcher failed: ...` (`consultants/engine/storage.py:108-110`).

### Final Conclusion
**Does it produce a coherent answer?** Yes, but it is **degraded**. The council does **not** fail the entire consultation (the graph does not crash), but it surfaces as `status=failed` in the metadata.
**User Perception**: The user receives an answer based on the surviving lanes. If the failed lane held critical info, the answer will have a gap. The user will see `status: failed` in the `summary.md` front-matter and the error in the `transcript.md`.

### Recommended Hardening
The current failure mode marks the *entire* consultation as `failed` if *any* lane fails, even if the synthesizer produced a perfectly valid answer from other lanes.

**Recommendation**: Change the terminal status logic to only mark `failed` if the **synthesizer** fails, or if **all** researcher lanes fail.

**Path**: `consultants/server/runner.py:172`
**Change**:
```python
# From:
node_error = final_state.get("error")
# To:
node_error = final_state.get("error") if final_state.get("_role_failed") == "synthesizer" else None
```
(Or more robustly, check if `final_answer` is present before marking the whole session failed).

---

## Critic (round 2)

DECISION: ready

The research is sufficient. The failure path is fully traced through the source code. The researcher's claims about the "tombstone" pattern, the additive reducers, the server's stream handling, the synthesizer's input, and the storage status are all verified against the provided file contents.

1.  **Reducers**: `consultants/engine/graph.py:64` confirms `research` uses `operator.add`.
2.  **Tombstone**: `consultants/engine/council.py:547-573` explicitly catches `Exception` and returns a dictionary containing `error`, `_role_failed`, `research` (as a list), and `turns`.
3.  **Server Loop**: `consultants/server/runner.py:136-161` shows the `compiled.stream` is wrapped in a try-except that catches graph-level crashes, but since the node catches its own exception, the loop continues to completion.
4.  **Synthesizer**: `consultants/engine/council.py:253-254` shows `research_rounds` (which contains the tombstone error string) are joined into the user prompt.
5.  **Storage**: `consultants/server/runner.py:172-174` shows `terminal_status = "failed" if node_error else "completed"`, where `node_error` is pulled from `final_state.get("error")`. This matches the tombstone's `error` key.

The conclusion that the user sees a degraded answer but the metadata marks the session as `failed` is correct.

---

## Synthesizer

The council produces a degraded but coherent answer; the consultation does not crash, but it is marked as `status=failed` in the metadata.

### Failure Path Trace

1.  **CouncilState's Additive Reducers**: The `researcher_node` wraps the loop execution in a `try...except` block (`consultants/engine/council.py:547-573`). Upon an exception, it returns a "tombstone" dictionary. Because `CouncilState.research` and `CouncilState.turns` use `operator.add` as reducers (`consultants/engine/graph.py:64, 68`), the error string `"(researcher lane failed: {e})"` and the failure `RoleTurn` are appended to the global state without interrupting other parallel lanes.
2.  **Server Loop**: The `compiled.stream` loop (`consultants/server/runner.py:136-161`) does not crash because the exception was neutralized within the node. The loop continues to completion, and `final_state` (`consultants/server/runner.py:140`) captures the merged state including the tombstone.
3.  **Synthesizer's Input**: The synthesizer receives the merged `research` list via `build_synthesizer_messages` (`consultants/engine/council.py:245-266`). It sees the successful reports from surviving lanes interleaved with the literal error string `"(researcher lane failed: ...)"` (`consultants/engine/council.py:253-254`), treating the failure as a textual finding.
4.  **Storage Artifact Writer**: The runner checks `final_state.get("error")` to determine the session status (`consultants/server/runner.py:172 [no failed at this line; line is in run_council]-174`). Since the tombstone set the `error` key (`consultants/engine/council.py:564`), `terminal_status` is set to `"failed"`. This status and the error message are written to the `summary.md` YAML front-matter (`consultants/engine/storage.py:108-110 [no md at this line; line is in _yaml_front_matter]`) and the `transcript.md`.

**User Perception**: The user receives a functional answer based on the remaining researchers. The failure is only visible if the user inspects the `status: failed` metadata in the summary or the specific error in the transcript.

### Recommended Hardening
Prevent a single researcher lane failure from marking the entire consultation as failed if a final answer was still produced.

**Change**: Modify the terminal status logic to only mark the session as `failed` if the synthesizer fails or no final answer is present.
**Path**: `consultants/server/runner.py:172 [no failed at this line; line is in run_council]`
**Implementation**:
```python
node_error = final_state.get("error") if final_state.get("_role_failed") == "synthesizer" else None
```

---
