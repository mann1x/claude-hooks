# Consultation transcript — csl-2026-05-09-0624-73a7

_Question_: In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:

1. CouncilState's additive reducers (consultants/engine/graph.py)
2. consultants/server/runner.py's compiled.stream loop
3. The synthesizer's input messages — what does it actually see?
4. The storage.py artifact writer

Answer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line.


## Planner

1. Consultants/engine/graph.py additive reducers (e.g., `add_consultant_state` around lines 58‑73) to see if exceptions from `agent_loop.runner.run_loop` are caught, logged, or stored as error state.  
2. Consultants/server/runner.py compiled.stream loop (around line 112) to verify whether the loop propagates lane exceptions upward, breaks the stream, or yields an error event.  
3. Consultants/engine/synthesizer.py input assembly (around line 89) to determine what messages are fed to the synthesizer when a lane raises an exception (missing output vs error placeholder).  
4. Storage.py artifact writer (around line 45) to check if partial artifacts are written on lane failure or if the write is aborted entirely.  
5. Consultants/server/api.py response handler (around line 200) to ascertain whether the final user‑facing status is set to `failed` or a degraded answer is returned.

---

## Researcher (round 1)

- The researcher node wrapper in `consultants/engine/graph.py:_wrap_researcher` (lines 145‑157) calls `council.researcher_node` without a try/catch, so any exception from `agent_loop.runner.run_loop` propagates upward uncaught.
- LangGraph’s `Send` fan‑out treats an exception in any parallel researcher lane as a graph‑level failure; the stream in `consultants/server/runner.py:run_council` (lines 135‑161) catches the exception, logs it, sets `state.status = "failed"` and `state.error = f"graph crashed: {e}"`, writes failed artifacts, and returns immediately.
- Because the graph aborts before reaching the synthesizer node, the synthesizer never runs; the `final_state` consulted by the storage layer contains only whatever state updates were merged before the failure (typically empty `final_answer`), so the user observes a completely failed consultation (`status=failed`) with an empty answer and the error message in the metadata.
- The `ConsultationResult` written by `consultants/engine/storage.py:write_consultation` (lines 200‑208) records `status="failed"` and the captured `error`, producing a `summary.md` with YAML front‑matter showing `status: failed` and an empty body.
- **Hardening change:** wrap the researcher node call in a try/catch and return a state update that populates `CouncilState.error` and `CouncilState._role_failed` (already declared in lines 74‑75). This lets the graph continue, allowing other lanes to complete and the synthesizer to generate a degraded answer.  
  Edit `consultants/engine/graph.py:_wrap_researcher` around lines 145‑157:  
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
  This ensures lane‑level errors are stored in state rather than crashing the whole graph.

---

## Researcher (round 1)

- CouncilState uses additive reducers for `research` and `turns` (list concat) so parallel researcher lanes merge their outputs, but `error` is a plain string (last write wins)【consultants/engine/graph.py:60-64】【consultants/engine/graph.py:74】.  
- In `researcher_node`, an unhandled exception (e.g., HTTP timeout) is caught and returns a state update with `"error": f"researcher failed: {e}"` (line 564) and `"research": [err_text]` (line 566)【consultants/engine/council.py:555-573】.  
- The runner’s `compiled.stream` loop catches only graph‑level exceptions; node‑level exceptions are converted to state updates, so the graph continues to completion【consultants/server/runner.py:135-161】.  
- The synthesizer receives all research strings via `build_synthesizer_messages(state.get("research") or [])`, thus seeing any error placeholders inserted by failed lanes【consultants/engine/council.py:245-266】.  
- Artifacts are written unconditionally via `storage.write_consultation`, which persists `final_answer` (synthesizer output) and `turns` (including error turns) to `summary.md`, `transcript.md`, and `metadata.json`【consultants/engine/storage.py:200-208】.  
- Because `error` is non‑empty after any researcher‑lane failure, `terminal_status` is set to `"failed"` (runner line 174)【consultants/server/runner.py:172-174】; the user sees a failed consultation, but the synthesizer may still produce an answer degraded by the error‑text placeholders in the research input.  
- **Hardening:** remove the `"error"` field from the failure return in `researcher_node` so a single lane failure does not mark the whole consultation as failed; change `consultants/engine/council.py:564` to omit the `"error"` key (or set it to `""`). This lets the consultation finish with `status="completed"` while preserving the failure trace in `_role_failed` and `turns`.

---

## Researcher (round 1)

- CouncilState declares additive reducers for parallel lanes: `research`, `turns`, `total_prompt_tokens`, `total_completion_tokens`, `research_rounds_used`, `critic_reroutes_used` use `Annotated[list, operator.add]` or `Annotated[int, operator.add]` (consultants/engine/graph.py:60-74)  
- Researcher node catches exceptions from `agent_loop.runner.run_loop` (e.g. HTTP timeout, non-string tool return) and returns a tombstone update with `"error": f"researcher failed: {e}"`, `"_role_failed": "researcher"`, and `"research": [f"(researcher lane failed: {e})"]` (consultants/engine/council.py:555-573)  
- Runner’s `compiled.stream` loop only catches graph‑level exceptions; node‑level errors are retained in state via the `error` and `_role_failed` fields (consultants/server/runner.py:135-161)  
- After streaming, the runner sets `terminal_status = "failed"` if `state.error` is truthy, otherwise `"completed"` (consultants/server/runner.py:171-174)  
- Synthesizer receives the merged `research` list (including any error strings from failed lanes) and attempts to produce a `final_answer`; if it succeeds, `final_state["final_answer"]` is non‑empty (consultants/engine/council.py:715-758)  
- Storage writes a `ConsultationResult` containing `status=terminal_status`, `error=node_error`, and `final_answer=final_state.get("final_answer", "")` (consultants/server/runner.py:176-199)  
- Outcome: If at least one researcher lane fails but the synthesizer still yields a final answer, the consultation is marked `status="failed"` yet the user receives an answer (possibly degraded); the user perceives a failed status with an error message but may still see the synthesized output  

- Hardening change: In `consultants/server/runner.py`, treat a run as completed when a final answer is produced despite node errors; replace lines 171‑174 with:  
  ```python
  terminal_status = "completed" if final_state.get("final_answer") else ("failed" if node_error else "completed")
  ```  
  (consultants/server/runner.py:171-174)

---

## Critic (round 3)

DECISION: needs_more_research
consultants/engine/graph.py:145-157
consultants/engine/council.py:555-573

---

## Synthesizer

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

---
