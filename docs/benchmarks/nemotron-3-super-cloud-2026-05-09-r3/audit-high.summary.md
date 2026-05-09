---
session_id: csl-2026-05-09-0812-227b
created: "2026-05-09T08:12:15"
question: "In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:\n\n1. CouncilState's additive reducers (consultants/engine/graph.py)\n2. consultants/server/runner.py's compiled.stream loop\n3. The synthesizer's input messages — what does it actually see?\n4. The storage.py artifact writer\n\nAnswer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line."
models:
  critic: "nemotron-3-super:cloud"
  planner: "nemotron-3-super:cloud"
  researcher: "nemotron-3-super:cloud"
  synthesizer: "nemotron-3-super:cloud"
topology: council
effort: high
duration_seconds: 387.44
status: completed
cwd: /tmp/claude-hooks-bench-bench-baseline-2026-05-07-PyyM
---

The consultation ends with `status=failed` and no answer to the user when a researcher‑lane exception propagates out of the graph; the runner’s `try/except` in `runner.py` (lines 136‑161) catches it, sets the status, returns early, and the synthesizer never runs. If the researcher node catches the exception and records it in state, the runner finishes the stream, sees the error (lines 171‑174), marks the consultation failed, but the synthesizer has already executed with whatever successful research it received, so the user may see a degraded answer (missing insights from the failed lane) while the overall status remains failed. The artifact writer still runs afterward (runner.py line 177) but does not change the failed status.

**Hardening change:** Wrap the researcher node call in a try/except inside `_wrap_researcher` so any exception is caught, logged, and returned as an error state, allowing other lanes and the synthesizer to continue while preserving failure visibility.  
Add to `consultants/engine/graph.py` around line 149:

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
        except Exception as e:
            log.exception("researcher node failed: %s", e)
            return {"error": str(e), "_role_failed": "researcher"}
    return _node
``` 

(See `graph.py:145‑157` for the surrounding context.)
