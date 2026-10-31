---
session_id: csl-2026-10-22-0801-ea70
created: "2026-10-22T08:01:18"
question: "In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:\n\n1. CouncilState's additive reducers (consultants/engine/graph.py)\n2. consultants/server/runner.py's compiled.stream loop\n3. The synthesizer's input messages — what does it actually see?\n4. The storage.py artifact writer\n\nAnswer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line."
models:
  critic: "minimax-m3.1-flash:cloud"
  planner: "minimax-m3.1-flash:cloud"
  researcher: "minimax-m3.1-flash:cloud"
  synthesizer: "minimax-m3.1-flash:cloud"
topology: council
effort: high
duration_seconds: 705.93
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-10-22/tmp/claude-hooks-bench-bench-baseline-2026-05-07-YDhi
---

Bottom line: for a **single researcher-lane exception**, the consultation does **not** entirely fail internally—the graph continues to synthesis and stores a real answer—but the session is nevertheless exposed as `status="failed"`. The answer is therefore **degraded and potentially misleading**, not a total consultation loss.

1. **Failure path and state merging**

   `researcher_node` calls the agent loop inside a broad `try/except Exception` (`consultants/engine/council.py:547-555`). An exhausted HTTP retry or a malformed tool result therefore does not escape that node; it is converted into a tombstone containing:

   - `error: "researcher failed: ..."`
   - `_role_failed: "researcher"`
   - `research: ["(researcher lane failed: ...)"]`
   - a researcher turn and a research-round count  
     (`consultants/engine/council.py:562-573`).

   The parallel-lane collections are additive: `research` and `turns` use `operator.add`, as do the round/token counters (`consultants/engine/graph.py:60-72`). Consequently, the failed lane’s tombstone is concatenated with surviving lanes’ reports rather than replacing them (`consultants/engine/graph.py:64`, `consultants/engine/graph.py:68-72`).

   The `error` and `_role_failed` channels themselves are ordinary `Optional[str]` channels without an additive reducer (`consultants/engine/graph.py:74-75`). For the requested one-lane failure, that does not stop the graph.

2. **What `compiled.stream` does**

   Because the exception was caught inside `researcher_node`, it does not reach the `except Exception` around `compiled.stream`. The graph-crash branch would set `state.status = "failed"` and invoke `_write_failed_artifacts` (`consultants/server/runner.py:155-161 [no stream at this line; line is in run_council]`), but the single-lane tombstone does not enter that branch.

   After the stream drains, the runner separately derives terminal status from the merged state:

   - `node_error = final_state.get("error")`
   - `terminal_status = "failed" if node_error else "completed"`  
     (`consultants/server/runner.py:171-174`).

   Since the tombstone populated `error`, the completed graph is still labeled `failed` (`consultants/server/runner.py:201-203 [no failed at this line; line is in run_council]`). This is the key inconsistency: the graph synthesized an answer, but the runner presents that completed result as failed.

3. **What the synthesizer actually sees**

   The synthesizer receives the merged `research` list, including both surviving reports and the failed lane’s literal tombstone. Each entry is rendered as an ordinary researcher report:

   ```text
   RESEARCHER REPORT (round N):
   (researcher lane failed: <exception>)
   ```

   This formatting occurs in `build_synthesizer_messages` (`consultants/engine/council.py:245-266`, specifically `consultants/engine/council.py:253-254`).

   The prompt does **not** require the synthesizer to disclose an evidence gap. Instead, it says not to mention the council, roles, or process (`consultants/engine/council.py:156-168`, specifically `consultants/engine/council.py:164-165`). The closing instruction only asks for a direct answer with citations (`consultants/engine/council.py:257-260`). Thus, the synthesizer can see that a lane failed, but it may silently omit that lane’s scope and produce a confidently incomplete answer.

4. **What storage writes**

   On the single-lane path, the runner constructs a result containing:

   - `final_state["final_answer"]`
   - `status=terminal_status`
   - `error=node_error`
   - all merged turns  
     (`consultants/server/runner.py:177-199`).

   The writer then persists the consultation (`consultants/server/runner.py:199`). `summary.md` contains YAML front matter with `status` and, when present, `error`, followed by the synthesized body (`consultants/engine/storage.py:108-120`). The transcript also includes the tombstone turn, making the failed lane visible in the on-disk transcript (`consultants/engine/storage.py:123-136`).

   Therefore, `summary.md` contains a **real synthesized answer**, but its front matter says `status: failed` and includes the researcher error. The result endpoint returns that summary and metadata (`consultants/server/app.py:260-279`).

5. **User-visible failure mode**

   The user encounters a contradictory result:

   - polling reports `status: "failed"` with `error: "researcher failed: ..."` (`consultants/server/app.py:61-75`);
   - the stored answer exists and can be retrieved, but is not automatically treated as a successful consultation (`consultants/server/app.py:260-279`);
   - if the answer is shown, it may be confidently incomplete because disclosure of the failed lane is not required (`consultants/engine/council.py:253-260`).

   This is distinct from a graph-level crash. That path writes only the placeholder `"(consultation failed: ...)"`, no turns, and `status="failed"` (`consultants/server/runner.py:211-237`).

**One recommended hardening change**

Change the terminal-status calculation at `consultants/server/runner.py:171-174`: when the graph completed with a non-empty `final_answer` and `_role_failed == "researcher"`, expose the result as a degraded/completed consultation rather than terminal `failed`, while retaining `error=node_error` in the result and artifact metadata. Reserve `status="failed"` and `_write_failed_artifacts` for exceptions that actually escape `compiled.stream` (`consultants/server/runner.py:155-161 [no stream at this line; line is in run_council]`). This fixes the central problem: a usable degraded answer is currently mislabeled as total failure.

**Verification status**

I verified the reducer declarations, tombstone construction, synthesizer formatting, stream exception branch, status derivation, result construction, and storage rendering by reading the cited source regions directly. The separate behavior of two or more lanes concurrently writing the non-additive `error` and `_role_failed` channels was not verified; that LangGraph-specific `InvalidUpdateError` risk remains unverified (`consultants/engine/graph.py:74-75`).
