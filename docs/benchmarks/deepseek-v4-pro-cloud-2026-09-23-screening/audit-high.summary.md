---
session_id: csl-2026-09-23-1817-4d32
created: "2026-09-23T18:17:52"
question: "In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:\n\n1. CouncilState's additive reducers (consultants/engine/graph.py)\n2. consultants/server/runner.py's compiled.stream loop\n3. The synthesizer's input messages — what does it actually see?\n4. The storage.py artifact writer\n\nAnswer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line."
models:
  critic: "deepseek-v4-pro:cloud"
  planner: "deepseek-v4-pro:cloud"
  researcher: "deepseek-v4-pro:cloud"
  synthesizer: "deepseek-v4-pro:cloud"
topology: council
effort: high
duration_seconds: 297.34
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-09-23/tmp/claude-hooks-bench-bench-baseline-2026-05-07-0ZSm
---

**Bottom line: the consultation produces a real, coherent `final_answer`, but it is surfaced to the user as `status=failed` — a false-negative caused by the lane tombstone's `error` field, not by an actual pipeline crash.**

1. **Additive reducers** — `research` and `turns` use `operator.add` (list concat) and the counters use int-sum, so parallel `Send` lanes merge deltas cleanly (`consultants/engine/graph.py:64-72 [no Send at this line; line is in CouncilState]`). But `error` and `_role_failed` have **no reducer** (`consultants/engine/graph.py:74-75`), so once a failed lane writes `error`, LangGraph's default semantics keep it — surviving lanes return no `error` key and cannot clear it.

2. **The exception never reaches `compiled.stream`'s handler** — the `try/except` around `compiled.stream` (`consultants/server/runner.py:137-161`) is *not* triggered; it only fires on graph-level crashes. The lane exception is caught earlier inside `researcher_node`, which wraps the entire `loop_runner(...)` call in `try/except Exception` (`consultants/engine/council.py:547-555`). The concrete sources — HTTP retry exhaustion raising from `chat_client.chat` and propagating through the unguarded `final = chat_fn(payload)` (`claude_hooks/agent_loop/runner.py:230`) — land in that catch. (A custom non-string tool result doesn't raise in `run_loop`; it blows up later at the next `json.dumps` in the chat client, still landing in the same tombstone.)

3. **The catch returns a tombstone** with `error`, `_role_failed: "researcher"`, `research: ["(researcher lane failed: ...)"]`, `research_rounds_used: 1`, and a `RoleTurn` (`consultants/engine/council.py:562-573`). The graph does **not** stall: topology is unconditional through `plan_topology` and `route_after_critic` keys only on `critic_decision`, so the synthesizer always runs.

4. **What the synthesizer sees** — `synthesizer_node` builds messages from `question`, `plan`, `research`, and `critique` only (`consultants/engine/council.py:718-723`). It does **not** read `error` or `_role_failed`. The failed lane's tombstone string is just one entry in the `research` list, rendered as `RESEARCHER REPORT (round N)` alongside the surviving lanes' real reports (`consultants/engine/council.py:253-254`). The prompt still instructs it to write a direct final answer (`consultants/engine/council.py:257-260`) — nothing tells it to abort or flag the gap, so the missing sub-question may be silently dropped if the synthesizer doesn't echo the marker.

5. **Storage and status** — after the stream drains, `node_error = final_state.get("error")` is truthy, so `terminal_status = "failed" if node_error else "completed"` (`consultants/server/runner.py:171-174`). `ConsultationResult` is built with the **real** `final_answer` but `status="failed"` and the lane `error` (`consultants/server/runner.py:177-198`). `write_consultation` writes three files (`consultants/engine/storage.py:200-208`); `render_summary` puts `status: failed` and the error in YAML front-matter with the real answer as the body (`consultants/engine/storage.py:117-120`). The poll endpoint then reports `failed` because `state.status`/`state.error` are set from `terminal_status`/`node_error` (`consultants/server/runner.py:201-202`).

**Perceived failure mode:** the user gets a failed consultation whose `summary.md` still contains a plausible answer. The failed lane's sub-question may be uncovered, and the only signals are the tombstone string in the research list, the tombstone `RoleTurn` in `transcript.md`, and the `_role_failed` warning logged at `consultants/server/runner.py:204-206 [no _role_failed at this line; line is in run_council]`.

**One hardening change** — fix the false-negative at `consultants/server/runner.py:171-174`: reserve `failed` for synthesizer failure or no answer, not a lane tombstone:

```python
node_error = final_state.get("error")
node_failed = final_state.get("_role_failed")
if node_failed == "synthesizer" or not final_state.get("final_answer"):
    terminal_status = "failed"
else:
    terminal_status = "completed"
```

This keeps a synthesizer tombstone (`consultants/engine/council.py:736-747`, which sets `_role_failed="synthesizer"`) or an answerless graph as genuinely `failed`, while a degraded-but-answered lane failure becomes `completed` with the failure still visible via `research`, `turns`, and the `_role_failed` warning.
