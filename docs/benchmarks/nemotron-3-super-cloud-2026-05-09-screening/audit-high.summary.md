---
session_id: csl-2026-05-09-0624-73a7
created: "2026-05-09T06:24:42"
question: "In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:\n\n1. CouncilState's additive reducers (consultants/engine/graph.py)\n2. consultants/server/runner.py's compiled.stream loop\n3. The synthesizer's input messages — what does it actually see?\n4. The storage.py artifact writer\n\nAnswer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line."
models:
  critic: "nemotron-3-super:cloud"
  planner: "nemotron-3-super:cloud"
  researcher: "nemotron-3-super:cloud"
  synthesizer: "nemotron-3-super:cloud"
topology: council
effort: high
duration_seconds: 470.57
status: completed
cwd: /tmp/claude-hooks-bench-bench-baseline-2026-05-07-AxlY
---

The exception from a researcher lane is caught by `council.researcher_node`, which returns a state update containing an `"error"` string, a `"_role_failed"` flag, and a placeholder research entry【consultants/engine/council.py:555-573】.  
Because `CouncilState.error` is a plain string (last write wins) and `research`/`turns` use additive list reducers【consultants/engine/graph.py:60-64】【consultants/engine/graph.py:74】, this update overwrites any prior error and appends the placeholder to the research list.  

The runner’s `compiled.stream` loop treats node‑level exceptions as state updates, so the graph continues to completion unless a graph‑level exception occurs【consultants/server/runner.py:135-161】. After streaming, the runner sets `terminal_status = "failed"` if `state.error` is truthy, otherwise `"completed"`【consultants/server/runner.py:171-174】; thus any non‑empty error forces a failed status.  

The synthesizer runs only after the graph finishes and receives `build_synthesizer_messages(state.get("research") or [])`, which includes any error‑placeholder strings from failed lanes【consultants/engine/council.py:245-266】. If it can still produce a `final_answer`, that value is stored in state.  

`storage.write_consultation` unconditionally writes a `ConsultationResult` containing `status=terminal_status`, `error=state.error`, and `final_answer=final_state.get("final_answer", "")` to `summary.md`, `transcript.md`, and `metadata.json`【consultants/engine/storage.py:200-208】.  

**Outcome:** The consultation is marked `status=failed`; the user sees a failed status with an error message in the metadata. If the synthesizer succeeded despite the error placeholders, the user also receives a synthesized answer that may be degraded by those placeholders.  

**Hardening change:** Wrap the researcher node in `_wrap_researcher` to catch exceptions and return a state update without letting the error abort the graph, allowing other lanes to complete and the synthesizer to run. Edit `consultants/engine/graph.py:_wrap_researcher` around lines 145‑157:

```python
def _wrap_researcher(deps: GraphDeps):
    def _node(state: dict) -> dict:
        try:
            return council.researcher_node(
                state,
                chat_client=deps.chat_clients["researcher"],
                tool_executor=deps.tool_executor,
                tool_specs=deps.tool_specs,
                grounding_msgs=deps.grounding_msgs,
                model=deps.models["researcher"],
                cwd=deps.cwd,
                think=_think_for(deps, "researcher"),
            )
        except Exception as exc:  # pragma: no cover – defensive
            log.exception("researcher lane failed: %s", exc)
            return {"error": str(exc), "_role_failed": "researcher"}
    return _node
```
