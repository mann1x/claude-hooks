---
session_id: csl-2026-09-23-2024-9dca
created: "2026-09-23T20:24:44"
question: "In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:\n\n1. CouncilState's additive reducers (consultants/engine/graph.py)\n2. consultants/server/runner.py's compiled.stream loop\n3. The synthesizer's input messages — what does it actually see?\n4. The storage.py artifact writer\n\nAnswer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line."
models:
  critic: "gemma4:31b-cloud"
  planner: "gemma4:31b-cloud"
  researcher: "gemma4:31b-cloud"
  synthesizer: "gemma4:31b-cloud"
topology: council
effort: high
duration_seconds: 22.92
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-09-23/tmp/claude-hooks-bench-bench-baseline-2026-05-07-Zddh
---

The council produces a **degraded answer** rather than a complete failure; the consultation proceeds to synthesis, but the session is metadata-marked as failed.

### Failure Path Trace
1.  **CouncilState Reducers**: Exceptions in a researcher lane are caught within the `researcher_node` (`consultants/engine/council.py:547–573`). The node returns a "tombstone" update containing an error string (e.g., `(researcher lane failed: {e})`) and sets the state's `error` key. Because `CouncilState.research` and `CouncilState.turns` use `operator.add` (`consultants/engine/graph.py:64, 68`), these error strings are concatenated into the global state alongside successful reports from other lanes.
2.  **`compiled.stream` Loop**: The stream loop in `consultants/server/runner.py:137` continues to run because the exception was handled inside the node. However, after the graph completes, the runner detects the `error` key in the final state and sets the `terminal_status` to `"failed"` (`consultants/server/runner.py:174`).
3.  **Synthesizer Input**: The synthesizer receives the research list via `build_synthesizer_messages` (`consultants/engine/council.py:245`). It sees the literal error strings (e.g., `(researcher lane failed: ...)`) as part of the research findings (`consultants/engine/council.py:253`) and attempts to synthesize an answer based on this partial/corrupted data.
4.  **Artifact Writer**: `consultants/server/runner.py` creates a `ConsultationResult` where `final_answer` is the synthesizer's (potentially degraded) output, but `status` is set to `"failed"` (`consultants/server/runner.py:189-190`). This is persisted to disk via `storage.write_consultation` (`consultants/engine/storage.py:200`), resulting in a `summary.md` with a `status: "failed"` YAML header (`consultants/engine/storage.py:108-110 [no md at this line; line is in _yaml_front_matter]`).

**User Perception**: The user receives a final response, but it may be missing critical information. The user (or an automated system) is alerted to the failure via the `"failed"` status in the metadata and the presence of error strings in the research turns.

### Recommended Hardening
Update `build_synthesizer_messages` to explicitly flag system failures to the LLM so it can acknowledge the missing data in its response.

**Change**: In `consultants/engine/council.py:253`, modify the loop appending research reports to detect strings starting with `(researcher lane failed:` and prepend a clear system warning such as `[SYSTEM ERROR: This research lane failed to execute]`.
