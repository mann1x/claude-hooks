---
session_id: csl-2026-05-07-0813-faa2
created: "2026-05-07T08:13:24"
question: "In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:\n\n1. CouncilState's additive reducers (consultants/engine/graph.py)\n2. consultants/server/runner.py's compiled.stream loop\n3. The synthesizer's input messages — what does it actually see?\n4. The storage.py artifact writer\n\nAnswer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line."
models:
  critic: "kimi-k2.6:cloud"
  planner: "kimi-k2.6:cloud"
  researcher: "kimi-k2.6:cloud"
  synthesizer: "kimi-k2.6:cloud"
topology: council
effort: high
duration_seconds: 1023.35
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/claude-hooks
---

The council does not crash; it streams to completion, synthesizes from partial evidence, and persists an artifact whose body is a coherent degraded answer while its YAML front-matter marks `status: failed`.

1. **CouncilState additive reducers.** The schema merges parallel lanes with `operator.add` on `research` (`consultants/engine/graph.py:64`) and `turns` (`consultants/engine/graph.py:68`). `error` and `_role_failed` are bare `Optional[str]` with no reducer (`consultants/engine/graph.py:74-75`). A crashed lane returns `{"error": "researcher failed: ...", "_role_failed": "researcher"}` (`consultants/engine/council.py:540-541`) and omits `research`. The additive reducer therefore appends nothing for that lane, while `error` is overwritten last-write-wins by the default dict merge.

2. **`compiled.stream` loop.** The runner drives the graph with `for mode, payload in compiled.stream(...)` (`consultants/server/runner.py:137-138`). The surrounding `except Exception` (`consultants/server/runner.py:155`) only catches stream machinery failures, not node-internal exceptions. Because `researcher_node` swallows exceptions internally (`consultants/engine/council.py:530-541`) and returns a partial-state dict, the stream never aborts and continues to the synthesizer.

3. **Synthesizer input messages.** `synthesizer_node` reads `state.get("research") or []` (`consultants/engine/council.py:675`) and never inspects `state.get("error")` or `state.get("_role_failed")`. It passes that list directly to `build_synthesizer_messages` (`consultants/engine/council.py:677`). The synthesizer therefore sees only successful lanes' outputs—no tombstone, no exception object, no indication that a lane is missing.

4. **Artifact writer.** After the stream drains, the runner checks `node_error = final_state.get("error")` (`consultants/server/runner.py:172`) and sets `terminal_status = "failed" if node_error else "completed"` (`consultants/server/runner.py:174`). `write_consultation` (`consultants/engine/storage.py:200-208`) writes `summary.md` via `render_summary` (`consultants/engine/storage.py:117-120`), emitting YAML front-matter `status: failed` and `error: ...` but still writing the synthesizer's `final_answer` into the body. `render_transcript` (`consultants/engine/storage.py:123-158`) iterates `result.turns`, so the crashed lane is silently omitted.

**Outcome.** The council produces a **degraded answer**, not a blank failure. The user receives a `summary.md` whose Markdown body reads like a real result synthesized from incomplete evidence, paired with YAML front-matter declaring `status: failed` and `error: researcher failed: ...`. The transcript silently omits the crashed lane. If all lanes fail, the synthesizer may emit a vacuous or hallucinated answer under the same `failed` header.

**Recommended hardening.** In `consultants/engine/council.py:538-541`, change the exception return in `researcher_node` to inject a tombstone `research` entry and `RoleTurn` so the additive reducers record the failure and the synthesizer sees an explicit gap:

```python
except Exception as e:
    log.exception("researcher_node failed: %s", e)
    err_text = f"(researcher lane failed: {e})"
    return {
        "error": f"researcher failed: {e}",
        "_role_failed": "researcher",
        "research": [err_text],
        "turns": [RoleTurn(
            role="researcher", round=this_round, content=err_text,
            prompt_tokens=0, completion_tokens=0, duration_seconds=0,
        )],
    }
```

This ensures the transcript records the missing lane and the synthesizer prompt contains a visible failure stub instead of a silently under-informed answer.
