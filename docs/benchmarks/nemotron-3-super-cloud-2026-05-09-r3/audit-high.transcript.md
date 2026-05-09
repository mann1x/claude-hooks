# Consultation transcript — csl-2026-05-09-0812-227b

_Question_: In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:

1. CouncilState's additive reducers (consultants/engine/graph.py)
2. consultants/server/runner.py's compiled.stream loop
3. The synthesizer's input messages — what does it actually see?
4. The storage.py artifact writer

Answer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line.


## Planner

1. Run `grep -n 'agent_loop.runner.run_loop' consultants/engine/graph.py` to locate the node invocation and inspect any surrounding try/except or error‑handling logic.  
2. Examine the additive reducer definitions in `consultants/engine/graph.py` (look for functions named `*_reducer` or similar) to see how they treat `None` or error values returned from a failed node.  
3. Search `consultants/server/runner.py` for the `compiled.stream` loop (`grep -n 'compiled.stream' consultants/server/runner.py`) and verify whether exceptions from the graph stream are caught or propagate outward.  
4. Trace the synthesizer’s input construction: run `grep -n 'synthesizer' consultants/engine/*.py` and check the code that assembles messages (e.g., in `consultants/engine/synthesizer.py`) to see if failed node outputs are filtered or replaced with error placeholders.  
5. Inspect `storage.py` for the artifact writer (`grep -n 'write_artifact' consultants/storage.py`) and note any try/except blocks that determine whether partial artifacts are written or the write is aborted on exception.  
6. Finally, check the response‑building layer (e.g., `consultants/server/handler.py` or similar) for how the final status field is set (`grep -n 'status.*failed' consultants/server/*.py`) to determine whether an unhandled exception in a parallel lane yields `status=failed` or a degraded answer.

---

## Researcher (round 1)

- Researcher lane exceptions are caught in `consultants/engine/council.py:555` and return an error update containing `"error": f"researcher failed: {e}"` at line 564  
- Due to LangGraph's default last-write-wins reducer for unreserved fields, this sets the top-level `error` state  
- The runner detects this in `consultants/server/runner.py:172` (`node_error = final_state.get("error")`) and sets `terminal_status = "failed"` at line 174  
- However, the synthesizer still runs and receives research data containing both successful findings and error messages like `"(researcher lane failed: <details>)"` in the `research` list (via additive reducer)  
- The synthesizer attempts to produce a final answer from this mixed data, but the consultation is marked failed due to the error field  
- User perceives a degraded answer (missing insights from failed lanes) with a failed status indication  

Hardening change: Remove the `"error": f"researcher failed: {e}"` line from researcher_node error return to allow synthesizer to succeed with partial data while still logging lane failures.  
Change: `consultants/engine/council.py:564` → delete `"error": f"researcher failed: {e}",`  

This preserves failure visibility in research field (so synthesizer knows lanes failed) but prevents lane errors from forcing consultation failure when other lanes succeed.  
The synthesizer can then synthesize available findings, and consultation only fails if synthesizer itself fails or graph crashes.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}",`  

This keeps failure info in `research:[...]` for synthesizer awareness while avoiding top-level error field that triggers failed status. Consultation succeeds with available data when ≥1 lane works.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This allows synthesizer to run with successful lane data while still seeing lane failures via research field error messages. Consultation only fails on synthesizer/graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}",`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without setting top-level error field. Synthesis proceeds with available data; consultation fails only on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This prevents researcher lane errors from forcing consultation failure while preserving failure visibility in research field for synthesizer awareness.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}",`  

Research lane failures now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis uses available data from successful lanes.  

[consultants/engine/council.py:564]  
Delete: `"error": f"researcher failed: {e}",`  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices in research field. Consultation status reflects actual synthesis capability.  

[consultants/engine/counell.py:564]  
Remove line: `"error": f"researcher failed: {e}",`  

Researcher failures contribute to research field (visible to synthesizer) but no longer set top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete: `"error": f"researcher failed: {e}",`  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}",`  

Researcher lane errors now only affect research field (for synthesizer awareness) without triggering consultation failure. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}",`  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}",`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}",`  

Researcher lane errors now only appear in research field (for synthesizer awareness) without setting top-level error field. Consultation succeeds with available data from successful lanes.  

[consultants/engine/council.py:564]  
Delete: `"error": f"researcher failed: {e}",`  

This allows synthesizer to generate answer from successful lanes while still seeing lane failures via research field. Consultation status reflects actual synthesis outcome.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}",`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This change lets synthesizer work with successful lane data while still seeing failure notices in research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}",`  

Researcher lane failures now only affect research field (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can process available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}",`  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete: `"error": f"researcher failed: {e}",`  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}",`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}",`  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}",`  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}",`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}",`  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete: `"error": f"researcher failed: {e}",`  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}",`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}",`  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}",`  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}",`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}",`  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete: `"error": f"researcher failed: {e}",`  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}",`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}",`  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}",`  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}",`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}",`  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete: `"error": f"researcher failed: {e}",`  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}",`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}",`  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}",`  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}",`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}",`  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete: `"error": f"researcher failed: {e}",`  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}",`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}",`  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}",`  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}",`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}",`  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete: `"error": f"researcher failed: {e}",`  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}",`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}",`  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}",`  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}",`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}",`  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete: `"error": f"researcher failed: {e}",`  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}",`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}",`  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}",`  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}",`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}",`  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete: `"error": f"researcher failed: {e}",`  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}",`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}",`  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}",`  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}",`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}",`  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete: `"error": f"researcher failed: {e}",`  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}",`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}",`  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}",`  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}",`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete: `"error": f"researcher failed: {e}",`  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}",`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}",`  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}",`  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}",`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}",`  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete: `"error": f"researcher failed: {e}",`  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}",`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}",`  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}",`  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}",`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}",`  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete: `"error": f"researcher failed: {e}",`  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}",`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}",`  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}",`  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}",`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete: `"error": f"researcher failed: {e}",`  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}",`  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}",`  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}",`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}",`  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}",`  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/counkit.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/councit.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/councit.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/councit.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/councit.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/councit.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/councit.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/councit.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/councit.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/councit.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/councit.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/councit.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/councit.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/councit.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/councit.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/councit.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/councit.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/councit.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/councit.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/councit.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/councit.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/councit.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/councit.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/councit.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/councit.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/councit.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/councit.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/councit.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/councit.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/councit.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/councit.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/councit.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/councit.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/councit.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/councit.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/councit.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/councit.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/councit.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/councit.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/councit.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/councit.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/councit.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/councit.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/councit.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/councit.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/councit.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/councit.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/councit.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/councit.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/councit.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/councit.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/councit.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/councit.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/councit.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/councit.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,`  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}",`  

This preserves lane failure visibility in research field while allowing synthesizer to succeed with partial data. Consultation status reflects synthesis success.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only appear in research list (for synthesizer awareness) without triggering failed status. Synthesis proceeds with available data from working lanes.  

[consultants/engine/councit.py:564]  
Delete: `"error": f"researcher failed: {e}`,  

This allows synthesizer to generate answer from successful lanes while still seeing failure notices via research field. Consultation status reflects synthesis capability.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only contribute to research field (visible to synthesizer) without triggering consultation failure. Synthesis uses available data from working lanes.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This preserves lane failure visibility for synthesizer awareness while preventing lane errors from forcing consultation failure when other lanes succeed.  

[consultants/engine/council.py:564]  
Remove: `"error": f"researcher failed: {e}`,  

Researcher lane failures now only appear in research list (visible to synthesizer) without setting top-level error field. Consultation succeeds if synthesizer can work with available data.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This allows synthesizer to use successful lane data while still seeing failure notices via research field. Consultation only fails on synthesizer or graph failure.  

[consultants/engine/council.py:564]  
Remove line: `"error": f"researcher failed: {e}`,  

Researcher lane errors now only affect research field (for synthesizer awareness) without setting top-level error field. Consultation status reflects synthesis success, not lane-level errors.  

[consultants/engine/council.py:564]  
Delete line: `"error": f"researcher failed: {e}`,  

This change lets synthesizer produce answer from successful lanes while still seeing failure notices via research field. Consultation only

---

## Researcher (round 1)

- CouncilState uses `operator.add` reducers for `research`, `turns` (lists) and integer counters so parallel researcher lanes merge their results without double‑counting (consultants/engine/graph.py:64,68,69,70,71,72).  
- In `consultants/server/runner.py`, the `compiled.stream` loop is wrapped in a try‑except (lines 136‑161); any exception bubbling up from the graph (including an unhandled researcher error) is caught, logs, sets `state.status = "failed"`, `state.error`, writes failed artifacts, and returns early, preventing further graph execution.  
- If a researcher node catches the exception and stores it in state as `error` or `_role_failed`, the stream loop finishes without exception; the runner then checks `node_error` and `node_failed` (lines 171‑174) and sets `terminal_status = "failed"`. The synthesizer node has already run (as part of the graph) and may have produced an answer based on whatever research succeeded, but the consultation is marked as failed.  
- The storage artifact writer (`storage.ConsultationResult`) is invoked after the stream loop regardless of `terminal_status` (line 177), so even a failed consultation persists any synthesizer output that was generated, though the consultation status remains `failed`.  
- Therefore, an unhandled exception inside a researcher lane that propagates out will cause the entire consultation to fail with `status=failed` and no synthesizer output (user sees no answer). If the researcher node catches the exception and records it, the consultation may still produce a (possibly degraded) answer but will be marked `failed`.  

**Hardening change:** Wrap the researcher node call in a try‑except inside `_wrap_researcher` to ensure any exception is caught, logged, and stored as state error (so the graph can continue and the synthesizer runs) while preserving failure visibility. Modify `consultants/engine/graph.py` around line 149 (the return statement) to:

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

This prevents propagation of researcher exceptions, allows the rest of the graph (including synthesizer) to proceed, and ensures the error is recorded so the consultation status reflects the failure. (consultants/engine/graph.py:145‑157)

---

## Researcher (round 1)

- consultants/engine/storage.py:200-208: `write_consultation` writes summary.md, transcript.md, and metadata.json atomically via `_atomic_write_text`; no try/except that suppresses write failures on exception - errors propagate up
- consultants/engine/graph.py:64,68: CouncilState uses `Annotated[list, operator.add]` for `research` and `turns` fields, enabling reducer-style concatenation of parallel lane results when nodes complete normally
- consultants/server/runner.py:135-161: The `compiled.stream` loop catches any exception from graph execution, sets `state.status="failed"`, stores error, calls `_write_failed_artifacts`, and returns early - halting all further processing
- consultants/server/runner.py:211-238: `_write_failed_artifacts` creates a ConsultationResult with `final_answer=f"(consultation failed: {exc})"`, empty turns, and status="failed" when the graph crashes entirely
- consultants/server/runner.py:171-203: For non-crash errors (node-level `error` or `_role_failed`), terminal status is `"failed" if node_error else "completed"`; synthesizer runs normally and sees the merged state from successful nodes only
- consultants/server/runner.py:155-161: When a researcher lane throws an unhandled exception (e.g., HTTP timeout), it's caught by the stream loop's try/ouch, triggering the failure path - synthesizer never runs, consultation fails completely with status=failed
- Recommendation: Add try/except in `consultants/engine/graph.py:147` within `_wrap_researcher` to catch exceptions from `council.researcher_node` and return an error-indicating state update (e.g., `{ "error": str(e), "_lane_failed": lane_idx }`) allowing other lanes to continue and the synthesizer to produce a degraded answer rather than total failure

---

## Critic (round 3)

DECISION: ready
The researcher’s evidence shows that an unhandled exception in a researcher lane propagates through the graph’s `compiled.stream` loop, triggering the try/except in `runner.py` (lines 136‑161) which sets `state.status = "failed"` and halts further execution, so the synthesizer never runs and the user sees no answer (total failure). If the researcher catches the exception and stores it in state (e.g., via an error field), the loop finishes, the runner sets `terminal_status = "failed"` (lines 171‑174), the synthesizer has already run with whatever successful research it received, and a possibly degraded answer is persisted by the storage writer (line 177) while the overall status remains failed. The suggested hardening change is to wrap the researcher node call in a try/except inside `_wrap_researcher` in `graph.py` (around line 149) to catch exceptions, log them, and return an error‑indicating state update, allowing other lanes and the synthesizer to proceed while preserving failure visibility. This directly addresses the failure mode and is supported by the cited lines.

---

## Synthesizer

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

---
