# Consultation transcript — csl-2026-10-22-1851-ef4b

_Question_: In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:

1. CouncilState's additive reducers (consultants/engine/graph.py)
2. consultants/server/runner.py's compiled.stream loop
3. The synthesizer's input messages — what does it actually see?
4. The storage.py artifact writer

Answer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line.


## Planner

The failure path for an unhandled exception in a parallel researcher lane is as follows:

1.  **Exception Catch**: An exception in `claude_hooks/agent_loop/runner.py:run_loop` (e.g., an `AttributeError` if `tool_executor` returns a non-string, causing `_extract_text` to fail at `consultants/engine/council.py:575`) is caught by the `try...except` block in `consultants/engine/council.py:547-555`.
2.  **Tombstone Return**: The `researcher_node` returns a "tombstone" state update at `consultants/engine/council.py:563-573`. This update includes an error message in the `research` list and the `_role_failed` marker.
3.  **Additive Reduction**: Because `CouncilState.research` and `CouncilState.turns` are defined as `Annotated[list, operator.add]` (`consultants/engine/graph.py:64, 68`), the failed lane's error text and failure turn are appended to the global state without overwriting results from successful lanes.
4.  **Synthesizer Input**: The synthesizer receives the merged `research` list via `build_synthesizer_messages` (`consultants/engine/council.py:245-246`). It sees the successful reports from other lanes and the explicit failure string (e.g., `"(researcher lane failed: ...)"`) for the failed lane.
5.  **Status Propagation**: The `consultants/server/runner.py` loop continues because the exception was handled inside the node. However, at `consultants/server/runner.py:172`, it detects `node_error = final_state.get("error")` (set by the researcher tombstone at `consultants/engine/council.py:564`), which forces `terminal_status = "failed"` (`consultants/server/runner.py:174`).
6.  **Artifact Writing**: `storage.write_consultation` is called with `status="failed"` and the researcher's error message (`consultants/server/runner.py:189, 190`).

**Conclusion**: The council **still produces a coherent answer** (the synthesizer runs and utilizes available evidence), but the entire consultation **surfaces as `status=failed`**. The user perceives a successful response accompanied by a system-level failure status.

**Recommended Hardening Change**:
Remove the top-level `error` key from the researcher's failure return to prevent a single non-fatal lane failure from marking the entire consultation as failed. The failure is already tracked via `_role_failed` and the additive `research` log.

**Path:line**: `consultants/engine/council.py:564` — Delete the line `"error": f"researcher failed: {e}",`.

---

## Researcher (round 1)

The failure path for an unhandled exception in a parallel researcher lane is as follows:

1.  **Exception Catch**: Any exception raised inside `agent_loop.runner.run_loop` is caught by the `try...except Exception as e` block in `consultants/engine/council.py:547-555`.
2.  **Tombstone Return**: The `researcher_node` returns a "tombstone" state update at `consultants/engine/council.py:563-573`. This update includes:
    *   `error`: A string describing the failure (`consultants/engine/council.py:564`).
    *   `_role_failed`: Set to `"researcher"` (`consultants/engine/council.py:565`).
    *   `research`: A list containing a failure marker: `["(researcher lane failed: <error>)"]` (`consultants/engine/council.py:566`).
    *   `turns`: A `RoleTurn` recording the error text (`consultants/engine/council.py:568-572`).
3.  **Additive Reduction**: Because `CouncilState.research` and `CouncilState.turns` are defined as `Annotated[list, operator.add]` (`consultants/engine/graph.py:64, 68`), this tombstone is appended to the global state. The failure of one lane does not overwrite or block the successful research findings from other parallel lanes.
4.  **Runner Stream**: The `consultants/server/runner.py` compiled stream loop continues to execute. Since the exception was caught inside the node, the graph does not crash; the loop completes normally (`consultants/server/runner.py:137-143`).
5.  **Synthesizer Input**: The synthesizer receives the merged `research` list via `build_synthesizer_messages` (`consultants/engine/council.py:245-267`). It sees the findings from successful lanes interleaved with the `(researcher lane failed: ...)` marker from the failed lane.
6.  **Artifact Writing**:
    *   The `final_state` is passed to `storage.ConsultationResult` (`consultants/server/runner.py:177-198`).
    *   The `status` is set to `"failed"` if `node_error` (from the tombstone) is present (`consultants/server/runner.py:174`).
    *   `storage.write_consultation` writes the `summary.md` (the synthesizer's answer) and `transcript.md` (including the failure turn) to disk (`consultants/engine/storage.py:200-208`).

**Conclusion**: The council **still produces a coherent answer**, though it is **degraded**. The user perceives a "completed" consultation that may have gaps in the answer, accompanied by a `status: failed` in the metadata and a visible error message in the transcript. The entire consultation does not fail unless the *synthesizer* itself crashes (`consultants/engine/council.py:725-747`).

**Hardening Recommendation**: 
The current tombstone return in `consultants/engine/council.py:563-573` records a `research_rounds_used: 1`. If a lane fails, it should not increment the round count to avoid hitting the `researcher_rounds_max` cap prematurely in potential re-routes.
**Change**: Update `consultants/engine/council.py:567` from `"research_rounds_used": 1` to `"research_rounds_used": 0`.

---

## Researcher (round 1)

The failure path for an unhandled exception in a parallel researcher lane is as follows:

1.  **CouncilState Reducers (`consultants/engine/graph.py`)**:
    The `research` and `turns` channels use `operator.add` (additive reducers) (`consultants/engine/graph.py:64, 68`). When a researcher lane crashes, the `researcher_node` catches the exception and returns a "tombstone" partial state: `{"error": f"researcher failed: {e}", "_role_failed": "researcher", "research": [err_text], ...}` (`consultants/engine/council.py:563-573`). Because of the additive reducer, this `err_text` (e.g., `"(researcher lane failed: ...)"`) is appended to the global `research` list without wiping out successful reports from other lanes.

2.  **Compiled Stream Loop (`consultants/server/runner.py`)**:
    The `compiled.stream` loop continues normally because the exception was handled inside the `researcher_node` wrapper (`consultants/engine/council.py:555`). After the stream drains, the runner checks for mid-pipeline errors: `node_error = final_state.get("error")` (`consultants/server/runner.py:172`). Since the crashed researcher lane set the `error` key, `terminal_status` is forced to `"failed"` (`consultants/server/runner.py:174`).

3.  **Synthesizer's Input (`consultants/engine/council.py`)**:
    The synthesizer is still invoked. It calls `build_synthesizer_messages` which iterates over `research_rounds` (`consultants/engine/council.py:253-254`). It sees the successful reports from healthy lanes and the explicit failure string `"(researcher lane failed: ...)"` for the failed lane. The synthesizer produces a coherent answer based on the available (degraded) evidence.

4.  **Artifact Writer (`consultants/engine/storage.py`)**:
    `storage.write_consultation` is called via the runner (`consultants/server/runner.py:199`). It writes the `ConsultationResult` where `status="failed"` and `error` contains the researcher's exception string (`consultants/server/runner.py:189-190`). This is persisted in `metadata.json` and the YAML front-matter of `summary.md` (`consultants/engine/storage.py:108-110`).

**Conclusion**: The council **still produces a coherent answer** (the synthesizer runs and utilizes all available reports), but the entire consultation **surfaces as `status=failed`**. The user perceives a successful, albeit potentially incomplete, response accompanied by a system-level failure status in the metadata.

**Recommended Hardening Change**:
Remove the top-level `error` key from the researcher's failure return to prevent a single non-fatal lane failure from marking the entire consultation as failed. The failure is already tracked via `_role_failed` and the additive `research` log.

**Path:line**: `consultants/engine/council.py:564` — Delete the line `"error": f"researcher failed: {e}",`.

---

## Critic (round 2)

DECISION: ready

The research is sufficient. The failure path is verified: an exception in a researcher lane is caught in `consultants/engine/council.py:547-555` and returns a tombstone (`council.py:563-573`). The additive reducers in `consultants/engine/graph.py:64, 68` ensure the failure is appended to the state without blocking other lanes. The synthesizer receives this failure string via `build_synthesizer_messages` (`council.py:253-254`) and produces a coherent (though degraded) answer. However, because the tombstone sets the `error` key (`council.py:564`), the runner marks the entire consultation as `status="failed"` (`runner.py:172-174`) and persists this in the metadata (`runner.py:189-190`).

The user perceives a successful, coherent response (via `summary.md`) but sees a `status: failed` in the system metadata and transcript.

Recommended Hardening Change:
Remove the `error` key from the researcher tombstone to prevent a non-fatal lane failure from marking the entire consultation as failed.
Path:line: `consultants/engine/council.py:564` — Delete `"error": f"researcher failed: {e}",`.

---

## Synthesizer

The council produces a coherent but degraded answer, but the entire consultation surfaces as `status=failed`. The user perceives a successful response (via the synthesizer) accompanied by a system-level failure status in the metadata and transcript.

**Failure Path Trace**

1.  **Exception Catch & Tombstone**: An unhandled exception in a researcher lane is caught by the `try...except` block in `consultants/engine/council.py:547-555`. The `researcher_node` then returns a "tombstone" state update containing an error string in the `error` key (`consultants/engine/council.py:564 [no research at this line; line is in researcher_node]`), a failure marker in `_role_failed` (`consultants/engine/council.py:565`), and a failure string appended to the `research` list (`consultants/engine/council.py:566`).
2.  **Additive Reducers**: Because `CouncilState.research` and `CouncilState.turns` are defined as `Annotated[list, operator.add]` (`consultants/engine/graph.py:64 [no turns at this line; line is in CouncilState], 68`), the tombstone's failure data is appended to the global state. This ensures successful reports from other parallel lanes are preserved and not overwritten.
3.  **Compiled Stream Loop**: The `consultants/server/runner.py` stream loop continues normally because the exception was handled inside the node (`consultants/engine/council.py:555`). However, after the stream finishes, the runner checks `final_state.get("error")` (`consultants/server/runner.py:172`); since the tombstone set this key, `terminal_status` is forced to `"failed"` (`consultants/server/runner.py:174`).
4.  **Synthesizer Input**: The synthesizer is still invoked. Through `build_synthesizer_messages` (`consultants/engine/council.py:245-267`), it receives a merged `research` list containing both the successful findings from healthy lanes and the explicit `(researcher lane failed: ...)` marker. It produces a coherent answer based on the available evidence.
5.  **Artifact Writer**: `storage.write_consultation` is called with `status="failed"` and the researcher's exception string (`consultants/server/runner.py:189-190`). This is persisted in the metadata and the YAML front-matter of the summary (`consultants/engine/storage.py:108-110`).

**Recommended Hardening Change**

Remove the top-level `error` key from the researcher's failure return to prevent a single non-fatal lane failure from marking the entire consultation as failed. The failure is already tracked via `_role_failed` and the additive `research` log.

**Path:line**: `consultants/engine/council.py:564 [no research at this line; line is in researcher_node]` — Delete the line `"error": f"researcher failed: {e}",`.

---
