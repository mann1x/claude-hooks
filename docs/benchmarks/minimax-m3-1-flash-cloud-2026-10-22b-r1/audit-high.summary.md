---
session_id: csl-2026-10-22-1618-1e3a
created: "2026-10-22T16:18:22"
question: "In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:\n\n1. CouncilState's additive reducers (consultants/engine/graph.py)\n2. consultants/server/runner.py's compiled.stream loop\n3. The synthesizer's input messages — what does it actually see?\n4. The storage.py artifact writer\n\nAnswer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line."
models:
  critic: "minimax-m3.1-flash:cloud"
  planner: "minimax-m3.1-flash:cloud"
  researcher: "minimax-m3.1-flash:cloud"
  synthesizer: "minimax-m3.1-flash:cloud"
topology: council
effort: high
duration_seconds: 422.58
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-10-22/tmp/claude-hooks-bench-bench-baseline-2026-05-07-PPRS
---

**The council does not abort, but the consultation is nevertheless surfaced as `status="failed"` even though a degraded synthesized answer exists.** The user therefore perceives a total failure, while `summary.md` contains the answer.

- **Failure capture:** An exception raised inside `run_loop` is caught by `researcher_node`; it does not propagate out of the node. The catch returns a tombstone containing `error`, `_role_failed="researcher"`, one research entry, `research_rounds_used=1`, and a failed researcher turn (`consultants/engine/council.py:547-573`).

- **Reducer merge:** `research` and `turns` use additive list reducers, and `research_rounds_used` uses integer addition, so the tombstone merges with the surviving lanes’ results (`consultants/engine/graph.py:60-69`). `error` and `_role_failed` are single-value channels (`consultants/engine/graph.py:74-75`). Thus the final state contains the surviving research, the literal failure report, the researcher error, and a synthesized `final_answer`.

- **Streaming path:** Because the failing researcher returned a state update rather than raising, `compiled.stream` continues through the graph and does not enter its graph-crash handler. The runner takes the last merged `"values"` payload as `final_state` (`consultants/server/runner.py:124-161`).

- **What the synthesizer sees:** Every element of `state["research"]` becomes a `RESEARCHER REPORT (round N)` block (`consultants/engine/council.py:245-266`). Therefore, the synthesizer sees the tombstone literally as:

  ```text
  RESEARCHER REPORT (round N):
  (researcher lane failed: <exception>)
  ```

  The tombstone does not identify the failed lane or its sub-question, and `state["error"]` is not passed into the message builder (`consultants/engine/council.py:245-248`, `consultants/engine/council.py:715-723`). Consequently, the answer may be usable, but it is not guaranteed to be complete or coherent for the missing subtopic: it may silently omit that topic or expose the raw exception in the answer.

- **User-visible result:** The runner classifies the consultation as failed whenever any `error` string exists, regardless of whether `final_answer` was produced (`consultants/server/runner.py:171-174 [no final_answer at this line; line is in run_council]`, `consultants/server/runner.py:186-202`). It persists that synthesized answer, but with `status="failed"` and the researcher error (`consultants/server/runner.py:176-199`).

- **Artifacts:** `summary.md` contains both failed front matter and the actual synthesized body (`consultants/engine/storage.py:108-120`). `transcript.md` includes the failed researcher tombstone alongside the surviving turns (`consultants/engine/storage.py:129-136`), while `metadata.json` serializes the result, including its failed status, error, turns, and final answer (`consultants/engine/storage.py:166-169`).

- **True graph-crash distinction:** If the graph itself raises—such as an exception outside the guarded `run_loop` call—the runner instead writes a generic failed answer, no turns, and discards surviving work (`consultants/server/runner.py:155-161`, `consultants/server/runner.py:211-237`). That is distinct from the specified exception inside `run_loop`.

**Exactly one hardening change:** change the status calculation at `consultants/server/runner.py:174` so a researcher-lane error does not automatically make a usable synthesized answer terminal. Treat the consultation as failed only when there is no usable `final_answer` or the synthesizer itself failed; otherwise return the degraded answer while retaining the researcher error in `state.error` and the artifacts. The existing error persistence remains at `consultants/server/runner.py:189-202` and `consultants/engine/storage.py:108-110`.
