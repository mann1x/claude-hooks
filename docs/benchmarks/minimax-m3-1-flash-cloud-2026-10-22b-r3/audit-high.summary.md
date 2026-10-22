---
session_id: csl-2026-10-22-1703-1dde
created: "2026-10-22T17:03:41"
question: "In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:\n\n1. CouncilState's additive reducers (consultants/engine/graph.py)\n2. consultants/server/runner.py's compiled.stream loop\n3. The synthesizer's input messages — what does it actually see?\n4. The storage.py artifact writer\n\nAnswer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line."
models:
  critic: "minimax-m3.1-flash:cloud"
  planner: "minimax-m3.1-flash:cloud"
  researcher: "minimax-m3.1-flash:cloud"
  synthesizer: "minimax-m3.1-flash:cloud"
topology: council
effort: high
duration_seconds: 609.77
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-10-22/tmp/claude-hooks-bench-bench-baseline-2026-05-07-YKSM
---

**Bottom line:** For a single researcher lane, the consultation does **not** wholly abort: `researcher_node` catches the exception, records a tombstone, and the synthesizer still produces an answer. However, the server labels the entire consultation `status="failed"` even though that answer and the surviving lanes’ work are persisted. The user therefore sees a usable but degraded answer presented as a failed consultation.

1. **Failure and reducer path**

   The `run_loop` call is inside `researcher_node`’s `try` block (`consultants/engine/council.py:547-554`). Therefore, a retry-exhausted HTTP exception or an exception raised by an injected tool executor is caught at `consultants/engine/council.py:555-573`; it does not escape that node.

   The catch returns:

   - `error="researcher failed: …"`
   - `_role_failed="researcher"`
   - one `research` tombstone, `(researcher lane failed: …)`
   - `research_rounds_used=1`
   - one researcher `RoleTurn` containing the tombstone  
   (`consultants/engine/council.py:562-573`).

   `research`, `turns`, and `research_rounds_used` are additive channels, so this tombstone and the surviving lanes’ results merge (`consultants/engine/graph.py:60-72`). The lane-specific Send payload includes `plan_item` and `lane_idx`, but the failure record itself does not retain either identifier (`consultants/engine/graph.py:76-80`; `consultants/engine/council.py:562-573`).

   `error` and `_role_failed` have no reducer (`consultants/engine/graph.py:74-75`). Consequently, two lanes failing in the same parallel superstep may conflict under LangGraph’s last-value semantics. That specific multi-lane failure is **unverified** because the available test environment lacks LangGraph; it must not be conflated with the ordinary single-lane path.

2. **`compiled.stream` behavior**

   For the named single-lane case, no exception reaches `compiled.stream`, so the graph continues through the remaining pipeline. The stream loop accumulates merged state from LangGraph’s `"values"` stream mode specifically to preserve additive fan-out results (`consultants/server/runner.py:124-140`).

   If an exception really escapes the graph—such as an exception raised after the `researcher_node` catch boundary—the server catches it around `compiled.stream` (`consultants/server/runner.py:136-161`). That graph-crash path sets `status="failed"`, records `error="graph crashed: …"`, writes the failure artifacts, and returns before synthesis (`consultants/server/runner.py:155-161`).

3. **What the synthesizer sees**

   The synthesizer receives the merged `state["research"]` list (`consultants/engine/council.py:718-723`). Each element is rendered as a positional `RESEARCHER REPORT (round N)` section (`consultants/engine/council.py:245-266`).

   Thus, the failed lane appears to the synthesizer as an ordinary report whose body is:

   ```text
   (researcher lane failed: <exception>)
   ```

   It is not identified by lane number or plan item, and the “round N” label is merely the element’s list position (`consultants/engine/council.py:253-254`; `consultants/engine/council.py:562-566`). The synthesizer can therefore produce a syntactically complete answer from the surviving evidence, but it has no reliable way to associate the missing evidence with the specific sub-question that failed.

4. **Storage and user-visible failure mode**

   The normal completion path stores the synthesizer’s `final_answer`, all merged turns, the lane error, and the derived status (`consultants/server/runner.py:171-199`). Because the failed lane populated `error`, the runner unconditionally derives `terminal_status="failed"` (`consultants/server/runner.py:171-174`).

   The artifacts therefore contain:

   - `summary.md`: front matter with `status: failed` and the researcher error, followed by the synthesizer’s complete answer (`consultants/engine/storage.py:97-120`);
   - `transcript.md`: the surviving turns plus the failed researcher’s tombstone turn (`consultants/engine/storage.py:123-158`);
   - `metadata.json`: the serialized result, including `status`, `error`, `final_answer`, and turns (`consultants/engine/storage.py:166-169 [no final_answer at this line; line is in render_metadata]`).

   All three files are written atomically through `write_consultation` (`consultants/engine/storage.py:200-208`).

   The perceived failure mode is therefore: **the answer exists, but the consultation is reported as wholly failed because one supporting lane failed**. A result-retrieval client that exposes the stored answer can return the degraded answer, while a status-gated client may withhold it because the terminal status is `failed` (`consultants/server/app.py:246-279 [no failed at this line; line is in create_app]`).

   By contrast, a graph-level crash does not produce a coherent answer: the failure artifact substitutes `(consultation failed: …)` and discards all turns (`consultants/server/runner.py:211-237`).

**Exactly one recommended hardening change**

At `consultants/server/runner.py:171-174`, derive the terminal status from whether a usable `final_answer` was actually produced, while retaining the researcher lane’s error separately. A consultation with a synthesizer answer and only a researcher-lane failure should be completed-but-degraded, not labeled as having produced no consultation result. This is the narrowest change that corrects the contradictory user-visible outcome without changing fan-out, synthesis, or storage semantics.
