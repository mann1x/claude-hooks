---
session_id: csl-2026-10-22-0826-f6c3
created: "2026-10-22T08:26:03"
question: "In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:\n\n1. CouncilState's additive reducers (consultants/engine/graph.py)\n2. consultants/server/runner.py's compiled.stream loop\n3. The synthesizer's input messages — what does it actually see?\n4. The storage.py artifact writer\n\nAnswer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line."
models:
  critic: "minimax-m3.1-flash:cloud"
  planner: "minimax-m3.1-flash:cloud"
  researcher: "minimax-m3.1-flash:cloud"
  synthesizer: "minimax-m3.1-flash:cloud"
topology: council
effort: high
duration_seconds: 310.05
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-10-22/tmp/claude-hooks-bench-bench-baseline-2026-05-07-TN4A
---

**Bottom line: for a single failed researcher lane, the council continues to synthesis and can produce a coherent, degraded answer, but the server still labels the entire consultation `status="failed"`. The user therefore perceives a failed consultation—not a successful answer with one research gap—even though a synthesized answer and failure tombstone are persisted.**

- **The premise that the exception reaches `compiled.stream` is incorrect for the stated single-lane case.** `researcher_node` wraps `run_loop` in `try/except Exception` (`consultants/engine/council.py:547-555 [no run_loop at this line; line is in researcher_node]`). An HTTP exception from `chat_fn` or a tool-result exception from `execute_tool_calls` is caught there (`claude_hooks/agent_loop/runner.py:122-127`, `claude_hooks/agent_loop/runner.py:230`, `claude_hooks/agent_loop/runner.py:299-301`). The node returns a tombstone rather than propagating the exception: `error`, `_role_failed`, one `research` entry, one round, and one `RoleTurn` (`consultants/engine/council.py:562-572`).

- **Reducers preserve both surviving and failed-lane contributions.** `research` and `turns` use additive list reducers, while research-round and token counters use additive integer reducers (`consultants/engine/graph.py:60-72`). Thus the failed lane contributes `research: ["(researcher lane failed: …)"]` and a corresponding researcher turn alongside successful reports (`consultants/engine/council.py:562-572`).

- **`compiled.stream` completes normally for this single-lane failure.** It consumes merged `values` as the final graph state (`consultants/server/runner.py:136-141`). The graph-level exception handler is only entered if an exception actually escapes the graph; that path sets `status="failed"` and writes placeholder failure artifacts (`consultants/server/runner.py:155-161`, `consultants/server/runner.py:211-237`).

- **The synthesizer actually sees the tombstone as an ordinary research report.** It receives the merged `state["research"]` list (`consultants/engine/council.py:718-723`). Every entry—including the failure tombstone—is rendered as `RESEARCHER REPORT (round N)` without lane metadata or a special failed-lane section (`consultants/engine/council.py:245-266`). The synthesizer prompt does not explicitly instruct it to disclose the missing research or reduce confidence; it also says not to mention the council, roles, or process (`consultants/engine/council.py:156-168`). Consequently, the synthesized answer may be coherent but silently omit the failed sub-question, or mention the raw failure marker.

- **The runner nevertheless converts the lane-level error into terminal failure.** It reads `final_state["error"]` and uses any such error to select `terminal_status = "failed"`, even if `final_state["final_answer"]` is nonempty (`consultants/server/runner.py:171-186`). It then stores that answer and turns list, but with `status="failed"` and the lane error (`consultants/server/runner.py:186-202`).

- **Storage contains a contradictory result.** The normal path writes the synthesized `final_answer` and the researcher tombstone turn while also recording failed status/error metadata (`consultants/server/runner.py:176-199`). The artifact writer persists `summary.md`, `transcript.md`, and `metadata.json` atomically (`consultants/engine/storage.py:200-208`); `summary.md` includes status/error front matter and the final-answer body (`consultants/engine/storage.py:108-120`), while `transcript.md` renders the turns, including the failure tombstone (`consultants/engine/storage.py:129-136 [no md at this line; line is in render_transcript]`). This is not the full graph-crash artifact, which instead contains `"(consultation failed: …)"` and no turns (`consultants/server/runner.py:224-225`).

**Exactly one hardening change:** at `consultants/server/runner.py:174`, reserve terminal `failed` for the absence of a usable synthesized answer. If `final_state["final_answer"]` is nonempty, mark the consultation `completed` while retaining the node error in `result.error` and `state.error` (`consultants/server/runner.py:189-202`). This preserves the degraded answer for normal clients and leaves the lane failure visible in the artifact metadata and transcript.

**Verification and limits:** I verified the reducer definitions, researcher exception boundary and tombstone, synthesizer message construction, `compiled.stream` handling, terminal-status derivation, and normal storage call by reading the cited source ranges. The exact client-side presentation of `status="failed"` could not be established because the referenced consultants skill file was absent. Concurrent failures from two or more lanes remain unverified: `error` and `_role_failed` are last-value channels (`consultants/engine/graph.py:74-75`), but the claimed conflict was not executed and is outside the single-lane scenario.
