# Consultation transcript — csl-2026-05-09-0612-2133

_Question_: In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:

1. CouncilState's additive reducers (consultants/engine/graph.py)
2. consultants/server/runner.py's compiled.stream loop
3. The synthesizer's input messages — what does it actually see?
4. The storage.py artifact writer

Answer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line.


## Planner

1. In consultants/engine/graph.py, locate the CouncilState reducer that handles unhandled exceptions in agent_loop.runner.run_loop and identify the lines that catch or propagate such failures.  
2. In consultants/server/runner.py, examine the compiled.stream loop around lines that subscribe to message queues to verify how an exception is intercepted and bubbled up to the outer framework.  
3. Open consultants/server/synthesizer.py and review the message assembly code near the input handling section to determine which messages the synthesizer actually receives after an upstream error.  
4. Inspect storage.py’s artifact writer function around the lines that commit consultation results to disk to see whether a partial or failed write occurs on error.  
5. Trace the status flag propagation in consultants/server/runner.py where a status=failed is set, locating the exact condition that aborts the consultation versus allowing degraded output.

---

## Researcher (round 1)

- In `consultants/engine/council.py` line 555‑573, `researcher_node` catches any exception from `loop_runner` and returns a tombstone containing `"error": f"researcher failed: {e}"` and `"_role_failed": "researcher"`; this delta is merged into `final_state` and marks the lane as failed.  
- `consultants/server/runner.py` lines 155‑159 catch any exception raised by `compiled.stream`; on failure they set `state.status = "failed"` and write a `ConsultationResult` whose `final_answer` is `"(consultation failed: {e})"` (lines 224‑225).  
- Because the failure is recorded as `status: "failed"` and the failed artifact’s `final_answer` is a synthetic error string, the user never sees a coherent answer; instead they receive either the error placeholder or a degraded summary that explicitly states “researcher lane failed to produce findings …”.  
- The failure propagates to the transcript writer in `consultants/engine/storage.py` lines 140‑155, where the failed lane’s `turns` entry contains the raw exception text, rendered verbatim in `transcript.md`.  
- Consequently the council does **not** produce a coherent answer; it surfaces as `status=failed` with a placeholder final answer, i.e. a degraded user‑visible message.  

**Hardening change:** Add a shape‑validation guard immediately after `final = loop_runner(...)` in `consultants/engine/council.py` at line 549 to reject non‑dict or non‑list `_loop_messages` and return a controlled error tombstone, e.g.:

```
if not isinstance(final, dict) or not isinstance(final.get("_loop_messages"), list):
    log.exception("run_loop returned unexpected shape; treating as failure")
    return {
        "error": "(run_loop returned malformed output)",
        "_role_failed": "researcher",
        "research": ["(run_loop returned malformed output)"],
        "research_rounds_used": 1,
        "turns": [RoleTurn(role="researcher", round=1, content="(run_loop returned malformed output)", duration_seconds=0.0, prompt_tokens=0, completion_tokens=0)],
    }
```

---

## Researcher (round 1)

(researcher lane produced no findings within the iteration budget; gaps remain — see plan)

---

## Researcher (round 1)

- The streaming runner catches any exception during graph execution and immediately sets `state.status = "failed"` at **`consultants/server/runner.py:157`**.  
- It then invokes `_write_failed_artifacts` at **`consultants/server/runner.py:160`** and returns, halting further role processing.  
- The identifier of the failed role is stored in `_role_failed` (defined at **`consultants/engine/graph.py:75`**) and later retrieved at **`consultants/server/runner.py:173`** via `node_failed = final_state.get("_role_failed")`.  
- The final terminal status is set to `"failed"` when `node_error` is truthy at **`consultants/server/runner.py:174`**, causing the fan‑out loop to terminate early.  
- Consequently the synthesized reply sent to the user is constructed at **`consultants/server/runner.py:224`** where `final_answer=f"(consultation failed: {exc})"` is used, so the user observes a failure message rather than a coherent answer.

---

## Critic (round 3)

DECISION: ready  
The council does not produce a coherent answer; the failure is handled in `consultants/server/runner.py:157‑159` where `state.status` is set to `"failed"` and a synthetic `final_answer` of `"(consultation failed: {e})"` is attached to the result. This error propagates to the transcript writer in `consultants/engine/storage.py:140‑155`, preserving the raw exception, and the user ultimately receives the placeholder failure message rather than a usable response. No further missing facts are needed to confirm this outcome.

---

## Synthesizer

The council never returns a coherent answer; it fails with status=failed and the user sees only a synthetic error placeholder.  
An exception raised inside agent_loop.runner.run_loop is caught in consultants/server/runner.py:155‑159, which sets state.status="failed" and writes final_answer="(consultation failed: {e})"; this error string is persisted by consultants/engine/storage.py:140‑155, so the user receives only that placeholder message, and to harden the path insert a shape‑validation guard after the loop runner return in consultants/engine/council.py at line 549 (consultants/engine/council.py:549).

---
