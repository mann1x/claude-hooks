---
session_id: csl-2026-10-22-1638-91e5
created: "2026-10-22T16:38:15"
question: "In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:\n\n1. CouncilState's additive reducers (consultants/engine/graph.py)\n2. consultants/server/runner.py's compiled.stream loop\n3. The synthesizer's input messages — what does it actually see?\n4. The storage.py artifact writer\n\nAnswer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line."
models:
  critic: "minimax-m3.1-flash:cloud"
  planner: "minimax-m3.1-flash:cloud"
  researcher: "minimax-m3.1-flash:cloud"
  synthesizer: "minimax-m3.1-flash:cloud"
topology: council
effort: high
duration_seconds: 744.85
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-10-22/tmp/claude-hooks-bench-bench-baseline-2026-05-07-NShH
---

**Bottom line:** if exactly one Send-fan-out researcher lane fails, the exception is caught inside `researcher_node`; the graph continues, the synthesizer receives the surviving reports plus a failure tombstone, and a degraded answer is written. However, the runner marks the entire consultation `status="failed"`, so the normal consumer surfaces “researcher failed” instead of returning the answer that was already stored.

1. **Failure path and reducers**

   - `researcher_node` invokes `loop_runner`—the `agent_loop.runner.run_loop` path—inside a `try` block, and catches `Exception` broadly (`consultants/engine/council.py:547-555`).
   - The timeout example is production-reachable: retry exhaustion re-raises from the chat client (`claude_hooks/get_advice/chat_client.py:285`, `claude_hooks/get_advice/chat_client.py:302`, `claude_hooks/get_advice/chat_client.py:305`) after the unguarded `chat_fn(payload)` call in `run_loop` (`claude_hooks/agent_loop/runner.py:230`).
   - A non-string executor result causes `len(output)` to raise `TypeError` (`claude_hooks/agent_loop/runner.py:122-127`), but the normal executor coerces tool output to `str` (`claude_hooks/caliber_proxy/tools.py:439`), so that variant is primarily reachable with a custom or stub executor.
   - The except branch returns a tombstone containing:
     - global `error = "researcher failed: …"` and `_role_failed = "researcher"` (`consultants/engine/council.py:562 [no research at this line; line is in researcher_node]-565`);
     - an additive `research` entry containing `(researcher lane failed: …)` (`consultants/engine/council.py:562 [no research at this line; line is in researcher_node]`, `consultants/engine/council.py:566`);
     - one research round and a researcher `RoleTurn` (`consultants/engine/council.py:567-572`).
   - `research`, `turns`, round counts, and token totals are additive, so successful sibling-lane results are retained (`consultants/engine/graph.py:60-72`). Each Send lane starts with empty additive lists, ensuring it returns only its own delta (`consultants/engine/graph.py:376-381`).
   - `error` and `_role_failed`, however, have no reducers (`consultants/engine/graph.py:74-75`). Thus one failed lane does not conflict with another lane’s write, and the graph can continue.

2. **`compiled.stream` and status**

   - The runner wraps the entire `compiled.stream(..., stream_mode=["updates", "values"])` iteration in one catch-all rather than isolating individual events (`consultants/server/runner.py:136-154`).
   - For one failed lane, the stream completes normally and supplies the merged final state (`consultants/server/runner.py:137-140`). There is no graph exception to enter the crash handler.
   - Nevertheless, the runner treats any final-state `error` as terminal:
     - it reads `node_error = final_state.get("error")`;
     - it sets `terminal_status = "failed" if node_error else "completed"` (`consultants/server/runner.py:171-174`).
   - Consequently, the council has an answer, but the session is reported as failed (`consultants/server/runner.py:186-199`).

3. **What the synthesizer sees**

   - The synthesizer receives the merged `research` list and the critic critique; it is not given a separate failure event or the `error` field (`consultants/engine/council.py:718-723 [no error at this line; line is in synthesizer_node]`).
   - Every research-list element is rendered as a numbered `RESEARCHER REPORT (round N)` block (`consultants/engine/council.py:253-254`). The failed lane therefore appears to the synthesizer as another report whose body is the literal text `(researcher lane failed: …)`.
   - That tombstone does not identify the missing `plan_item` or lane index (`consultants/engine/council.py:562 [no research at this line; line is in researcher_node]`), and the synthesizer prompt says not to mention the council, roles, or process (`consultants/engine/council.py:164-167`). The resulting answer may therefore be coherent but silently omit the failed lane’s sub-question rather than clearly telling the user that evidence is missing.

4. **Artifacts and perceived result**

   - The normal runner path persists the real synthesizer answer, turns, `status`, and error (`consultants/server/runner.py:186-199`).
   - `write_consultation` writes `summary.md`, `transcript.md`, and `metadata.json` atomically (`consultants/engine/storage.py:174-192 [no json at this line; line is in _atomic_write_text]`, `consultants/engine/storage.py:200-208`).
   - `summary.md` has `status` and `error` in front matter, followed by the real `final_answer` (`consultants/engine/storage.py:108-120`). `metadata.json` also contains the failed status and error (`consultants/engine/storage.py:166-169`), while the transcript includes the failed researcher turn (`consultants/engine/storage.py:129-136`).
   - The result API can serve those artifacts regardless of terminal status (`consultants/server/app.py:246-279`), but the normal skill fetches the result only after `status: completed` (`.claude/skills/consultants/SKILL.md:104-110 [unverified — file not found]`). On `status: failed`, it surfaces the role and error and suggests changing that role’s model (`.claude/skills/consultants/SKILL.md:134-147 [unverified — file not found]`).
   - The user therefore perceives **“the researcher failed; switch models”**, even though a usable-but-possibly-incomplete answer exists in the stored artifacts. The answer is recoverable through the result/show paths, but it is not returned by the normal failed-status flow.

5. **One hardening change — recommended**

   Change the researcher exception return at `consultants/engine/council.py:563-573` so that **Send-fan-out lanes do not write the global reducer-less `error` or `_role_failed` channels**. Keep the existing failure marker in the additive `research` and `turns` channels, but write `error`/`_role_failed` only on the non-fan-out researcher path.

   This single change prevents a lane-local failure from changing the whole session’s terminal status, while retaining the gap in the synthesizer input and transcript (`consultants/engine/council.py:566-572`). It also removes concurrent fan-out writes to the reducer-less channels (`consultants/engine/graph.py:74-75`), which otherwise create a possible multi-lane abort path.

   **Unverified:** simultaneous failures in two or more lanes were not executed because the installed environment skips the LangGraph test (`tests/test_consultants_council.py:739-749`). The single-lane behavior and artifact path above are directly supported by the code and unit-level tombstone tests (`tests/test_consultants_council.py:514-529`, `tests/test_consultants_council.py:531-561`).
