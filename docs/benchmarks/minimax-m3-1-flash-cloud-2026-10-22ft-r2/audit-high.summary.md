---
session_id: csl-2026-10-22-2052-970f
created: "2026-10-22T20:52:48"
question: "In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:\n\n1. CouncilState's additive reducers (consultants/engine/graph.py)\n2. consultants/server/runner.py's compiled.stream loop\n3. The synthesizer's input messages — what does it actually see?\n4. The storage.py artifact writer\n\nAnswer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line."
models:
  critic: "minimax-m3.1-flash:cloud"
  planner: "minimax-m3.1-flash:cloud"
  researcher: "minimax-m3.1-flash:cloud"
  synthesizer: "minimax-m3.1-flash:cloud"
topology: council
effort: high
duration_seconds: 592.75
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-10-22/tmp/claude-hooks-bench-bench-baseline-2026-05-07-i6US
---

For an exception raised inside `run_loop`, the graph does **not** abort: it continues to synthesis and persists a real but potentially incomplete answer, yet the server labels the session `status="failed"`. The user therefore gets a failed-consultation result and error string, while the degraded answer remains recoverable from the artifacts/result API (`consultants/engine/council.py:547-573`; `consultants/server/runner.py:171-202`; `consultants/engine/storage.py:108-120`).

1. **CouncilState and the Send fan-out**

   Each researcher lane receives empty additive `research` and `turns` lists and zeroed counters, so lane results are not pre-populated with another lane’s data (`consultants/engine/graph.py:358-385`). `research` and `turns` concatenate, while `research_rounds_used` and the token counters sum (`consultants/engine/graph.py:60-72`).

   When `loop_runner(...)` raises, `researcher_node` catches the exception and returns:

   - `error="researcher failed: …"`
   - `_role_failed="researcher"`
   - `research=["(researcher lane failed: …)"]`
   - `research_rounds_used=1`
   - a researcher tombstone turn

   (`consultants/engine/council.py:547-573`).

   Because the research list is additive, successful reports from the other lanes remain alongside the tombstone rather than being overwritten (`consultants/engine/graph.py:64-72`). The `error` and `_role_failed` fields themselves have no additive reducer and are plain optional state fields (`consultants/engine/graph.py:74-75`).

2. **`compiled.stream` and terminal status**

   The `run_loop` exception is contained inside the node, so it does not escape into the server’s `compiled.stream` loop. The server consumes both `"updates"` and `"values"` payloads and keeps the last merged `"values"` state as `final_state` (`consultants/server/runner.py:124-141`).

   After the stream drains, the runner sees the recorded node error and currently applies:

   ```python
   terminal_status = "failed" if node_error else "completed"
   ```

   (`consultants/server/runner.py:171-174`). Thus a single failed researcher lane causes the whole session to be marked failed even if the synthesizer subsequently produced a non-empty answer (`consultants/server/runner.py:174-190`). The session receives both `status="failed"` and the researcher error (`consultants/server/runner.py:201-206`).

   This differs from a true graph-level crash: an exception escaping the node boundary reaches the server’s exception handler and writes the placeholder `"(consultation failed: …)"` with no role turns (`consultants/server/runner.py:155-161`; `consultants/server/runner.py:211-237`). The stated exception inside the `loop_runner(...)` call does not take that path (`consultants/engine/council.py:547-555`).

3. **What the synthesizer sees**

   The synthesizer receives the question, planner plan, merged `state["research"]`, and optional critique (`consultants/engine/council.py:718-723`). Its user message renders every research entry as an ordinary numbered report:

   ```text
   RESEARCHER REPORT (round N):
   <entry>
   ```

   (`consultants/engine/council.py:245-265`). Consequently, it sees the failed lane approximately as:

   ```text
   RESEARCHER REPORT (round N):
   (researcher lane failed: <exception>)
   ```

   The separate `error` and `_role_failed` fields are not passed to the synthesizer-message builder (`consultants/engine/council.py:245-248 [no _role_failed at this line; line is in build_synthesizer_messages]`; `consultants/engine/council.py:718-723`). The tombstone also contains only the exception text, not the lane number or failed plan item (`consultants/engine/council.py:562-566`).

   Therefore, synthesis still occurs (`consultants/engine/council.py:725-758`), but the answer may silently omit the failed lane’s subject. The marker is present in the input, yet it is merely raw report text rather than a structured failure that the synthesizer is required to surface. Consequently, the result can be a confident but incomplete answer rather than an explicitly caveated degraded answer (`consultants/engine/council.py:253-265`).

4. **Artifact writer and user-visible failure**

   The normal result object contains:

   - the synthesizer’s real `final_answer`,
   - the merged turns, including the failed-lane tombstone,
   - `status="failed"`,
   - the researcher error.

   (`consultants/server/runner.py:186-199`; `consultants/engine/storage.py:54-72`). The writer consequently produces:

   - `summary.md` with `status: "failed"` and the error in front matter, followed by the real answer body (`consultants/engine/storage.py:108-120`);
   - `metadata.json` containing the failed status, error, real answer, and turns (`consultants/engine/storage.py:166-169`);
   - `transcript.md` containing the researcher tombstone turn (`consultants/engine/storage.py:123-158`).

   Thus the user-facing API reports a failed consultation and exposes the researcher error (`consultants/server/app.py:61-75`), but the degraded answer is still available through the result artifact/API rather than being replaced by the graph-crash placeholder (`consultants/server/app.py:246-279`; `consultants/server/runner.py:211-237`). Whether a particular external client automatically retrieves and displays answers for failed sessions could not be verified because the referenced consultants skill is absent from this tree.

**Exactly one hardening change:** change `consultants/server/runner.py:174` so `failed` is reserved for a failed synthesizer or an empty final answer; when a researcher/critic tombstone coexists with a non-empty synthesizer answer, set `terminal_status="completed"` while retaining the diagnostic error in the result and session state. This delivers the usable degraded answer without hiding the lane failure (`consultants/server/runner.py:171-202`; `consultants/engine/council.py:735-741`).
