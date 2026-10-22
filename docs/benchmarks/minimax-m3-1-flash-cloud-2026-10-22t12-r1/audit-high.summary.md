---
session_id: csl-2026-10-22-1724-d606
created: "2026-10-22T17:24:01"
question: "In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:\n\n1. CouncilState's additive reducers (consultants/engine/graph.py)\n2. consultants/server/runner.py's compiled.stream loop\n3. The synthesizer's input messages — what does it actually see?\n4. The storage.py artifact writer\n\nAnswer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line."
models:
  critic: "minimax-m3.1-flash:cloud"
  planner: "minimax-m3.1-flash:cloud"
  researcher: "minimax-m3.1-flash:cloud"
  synthesizer: "minimax-m3.1-flash:cloud"
topology: council
effort: high
duration_seconds: 865.20
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-10-22/tmp/claude-hooks-bench-bench-baseline-2026-05-07-Rwwx
---

**For one failed parallel researcher lane, the council still reaches the synthesizer and writes a real—but degraded—answer; it does not take the graph-crash path. The consultation is nevertheless reported as `status="failed"`. The user therefore sees a hard failure even though a usable answer exists in the artifacts. If two lanes fail simultaneously, the consultation may instead abort entirely; that concurrent-update behavior is not integration-tested here.**

1. **Exception origin and reducer merge**
   - `run_loop` does not catch exceptions from `chat_fn(payload)` (`claude_hooks/agent_loop/runner.py:230`) or from `tool_executor(...)` (`claude_hooks/agent_loop/runner.py:122`). A non-string tool result fails when the code takes `len(output)` for logging (`claude_hooks/agent_loop/runner.py:124`).
   - An exhausted HTTP retry raises a `RuntimeError` from the chat client (`claude_hooks/get_advice/chat_client.py:285`).
   - `researcher_node` catches that exception around the `loop_runner(...)` call (`consultants/engine/council.py:547`) and returns a tombstone rather than re-raising it (`consultants/engine/council.py:555`).
   - The tombstone contains:
     - `error="researcher failed: …"` and `_role_failed="researcher"` (`consultants/engine/council.py:564`)
     - one research item, `"(researcher lane failed: …)"` (`consultants/engine/council.py:562`)
     - one additive research-round increment and one researcher transcript turn (`consultants/engine/council.py:567`).
   - `research` and `turns` concatenate, and round/token counters sum (`consultants/engine/graph.py:64 [no turns at this line; line is in CouncilState]`, `consultants/engine/graph.py:68`). Thus the failed lane’s tombstone and turn merge with the successful lanes’ results.
   - `error` and `_role_failed` are ordinary single-value channels rather than additive channels (`consultants/engine/graph.py:74`). A single failed lane therefore leaves the other lanes’ research intact.
   - **Unverified:** if two parallel lanes fail in the same superstep, LangGraph may reject their concurrent writes to those single-value channels. This is library-side behavior requiring an integration test in the pinned LangGraph environment; it is not established by repository code alone.

2. **The `compiled.stream` path**
   - The runner consumes both `"updates"` and `"values"` stream modes and keeps the latest merged state (`consultants/server/runner.py:137`). Consequently, the tombstone, the surviving research, and the synthesizer’s eventual answer are present in `final_state`.
   - Because the researcher exception is caught inside the node, a single-lane failure does **not** enter the runner’s graph-crash handler (`consultants/server/runner.py:155 [no stream at this line; line is in run_council]`). That handler is reserved for an exception escaping `compiled.stream`; it marks the session failed and writes a failure-only artifact set (`consultants/server/runner.py:157`).
   - On the ordinary path, however, the tombstone’s `error` survives in merged state (`consultants/server/runner.py:172`), and the runner unconditionally maps any node error to `terminal_status="failed"` (`consultants/server/runner.py:174`).

3. **What the synthesizer actually sees**
   - `synthesizer_node` passes the merged `research` list directly into `build_synthesizer_messages` (`consultants/engine/council.py:718`).
   - Every merged entry—including the tombstone—is rendered as an ordinary numbered `"RESEARCHER REPORT (round N)"` block (`consultants/engine/council.py:253`). The synthesizer does **not** receive a distinct failure field or a separately labeled missing lane.
   - The literal text it sees in that report is `"(researcher lane failed: …)"` (`consultants/engine/council.py:562`). It also sees the successful lanes’ reports in the same list.
   - The synthesizer prompt tells it not to mention the council, roles, or process (`consultants/engine/council.py:164`). Therefore, the likely degradation is a coherent answer based only on the surviving evidence, with the failed lane’s subject matter silently omitted rather than a dedicated “lane failed” section.
   - The synthesizer can still return that answer and set `final_answer` and its turn (`consultants/engine/council.py:753`).

4. **Artifacts and user-visible result**
   - The successful stream path stores the real `final_state["final_answer"]` and merged turns, but sets the result status to `failed` and retains the node error (`consultants/server/runner.py:186 [no failed at this line; line is in run_council]`, `consultants/server/runner.py:189 [no failed at this line; line is in run_council]`).
   - `summary.md` therefore contains front matter with `status: failed` and the researcher error, followed by the real synthesizer answer as its body (`consultants/engine/storage.py:108`, `consultants/engine/storage.py:117`).
   - `transcript.md` includes the tombstone as a researcher turn (`consultants/engine/storage.py:129 [no md at this line; line is in render_transcript]`).
   - `metadata.json` serializes the entire `ConsultationResult`, including its status, error, final answer, and turns (`consultants/engine/storage.py:166`).
   - All three files are written by the normal artifact writer (`consultants/engine/storage.py:200`).
   - The session status endpoint exposes the resulting `status="failed"` and error (`consultants/server/app.py:61`). The result endpoint can nevertheless return the artifacts once they exist (`consultants/server/app.py:263`).
   - Thus the concrete perceived failure mode is: **“consultation failed: researcher failed …” plus a usable-but-incomplete answer hidden in `summary.md`/metadata rather than surfaced as a successful consultation.** The repository’s referenced consultant skill file was not present for verification, so its exact client-side presentation could not be confirmed.

5. **If the graph itself aborts**
   - An exception escaping `compiled.stream` takes the graph-crash handler (`consultants/server/runner.py:155 [no stream at this line; line is in run_council]`), which writes a placeholder answer, no turns, and `status="failed"` (`consultants/server/runner.py:224`). In that case the entire consultation truly fails rather than producing a degraded answer.

**Recommended single hardening change:** change the status derivation at `consultants/server/runner.py:174` so that a non-empty synthesizer answer accompanied by a non-synthesizer role failure is represented as a degraded/partial status rather than `failed`; retain `error` so the degraded condition remains visible (`consultants/server/runner.py:190`). This directly fixes the common one-lane failure mode, whereas reducer hardening for `error`/`_role_failed` (`consultants/engine/graph.py:74`) would only address the rarer, currently unverified simultaneous-failure case.
