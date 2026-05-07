---
session_id: csl-2026-05-07-0910-c184
created: "2026-05-07T09:10:41"
question: "In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:\n\n1. CouncilState's additive reducers (consultants/engine/graph.py)\n2. consultants/server/runner.py's compiled.stream loop\n3. The synthesizer's input messages — what does it actually see?\n4. The storage.py artifact writer\n\nAnswer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line."
models:
  critic: "kimi-k2.6:cloud"
  planner: "kimi-k2.6:cloud"
  researcher: "kimi-k2.6:cloud"
  synthesizer: "kimi-k2.6:cloud"
topology: council
effort: high
duration_seconds: 779.43
status: completed
cwd: /tmp/claude-hooks-bench-bench-baseline-2026-05-07-ce1r
---

The council streams to completion and produces a degraded `final_answer`, but the consultation still surfaces as `status=failed`.

- **CouncilState additive reducers.** `research: Annotated[list, operator.add]` at `consultants/engine/graph.py:64` concatenates outputs from parallel lanes. When a researcher lane crashes inside `agent_loop.runner.run_loop`, the `researcher_node` wrapper catches it and returns a tombstone dict containing `research: [err_text]` at `consultants/engine/council.py:562-566`; the additive reducer merges that tombstone string into the shared list alongside surviving lanes.

- **`compiled.stream` loop.** The runner invokes the graph via `compiled.stream(...)` at `consultants/server/runner.py:137-138` and iterates updates normally because the node-level catch already converted the exception into a state update. The outer `except` at `runner.py:155` is never reached, and after the loop drains the merged `final_state` contains the tombstone's `error` key.

- **Synthesizer input messages.** The synthesizer node calls `build_synthesizer_messages(state.get("research") or [], ...)` at `consultants/engine/council.py:718-723`. Because `research` is additive, the tombstone text appears as one of the rounds, which `build_synthesizer_messages` formats verbatim as `RESEARCHER REPORT (round {i}): {r.strip()}` at `council.py:253-254`. The synthesizer therefore reasons over a partially degraded corpus.

- **Artifact writer.** After streaming, `runner.py` reads `node_error = final_state.get("error")` at `runner.py:172` and sets `terminal_status = "failed"` at `runner.py:174`. It persists a `ConsultationResult` via `storage.write_consultation(...)` at `runner.py:199`, which writes `metadata.json` and `summary.md` with YAML front-matter `status: failed` at `consultants/engine/storage.py:108-110`, while still embedding the synthesizer's `final_answer` in the summary body at `storage.py:119`.

**User-perceived failure mode:** The summary body contains a coherent but potentially thinner answer, yet every downstream consumer sees `status: failed` and `error: researcher failed: ...`, causing the response to be treated as a total failure rather than a partial degradation.

**Hardening change:** At `consultants/server/runner.py:174`, replace the binary `terminal_status = "failed" if node_error else "completed"` with a three-way check that emits `"degraded"` when `node_error` is truthy but `final_state.get("final_answer")` exists, preserving `"failed"` only when no answer was produced.
