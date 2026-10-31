---
session_id: csl-2026-10-22-0816-0e41
created: "2026-10-22T08:16:13"
question: "In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:\n\n1. CouncilState's additive reducers (consultants/engine/graph.py)\n2. consultants/server/runner.py's compiled.stream loop\n3. The synthesizer's input messages — what does it actually see?\n4. The storage.py artifact writer\n\nAnswer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line."
models:
  critic: "minimax-m3.1-flash:cloud"
  planner: "minimax-m3.1-flash:cloud"
  researcher: "minimax-m3.1-flash:cloud"
  synthesizer: "minimax-m3.1-flash:cloud"
topology: council
effort: high
duration_seconds: 413.97
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-10-22/tmp/claude-hooks-bench-bench-baseline-2026-05-07-7fei
---

The graph completes and can produce a partially grounded answer, but the API/artifact status is nevertheless `failed`; for a single researcher-lane exception, the consultation is not aborted at the graph level.

### Exact failure path

1. **The lane exception is converted into a tombstone.**
   - `researcher_node` calls `loop_runner` inside `try`; any `Exception` raised by `agent_loop.runner.run_loop` is caught at `consultants/engine/council.py:547-555 [no run_loop at this line; line is in researcher_node]`.
   - The catch returns:
     - `error="researcher failed: ..."`,
     - `_role_failed="researcher"`,
     - one additive `research` entry containing `(researcher lane failed: ...)`,
     - `research_rounds_used=1`, and
     - one failed `RoleTurn` (`consultants/engine/council.py:562-573`).
   - Thus, the exception does **not** escape the Send-lane node or abort that graph superstep (`consultants/engine/council.py:555-573`).

   The two example origins can reach that catch:
   - An exhausted HTTP retry raises from the chat call, which is invoked at `claude_hooks/agent_loop/runner.py:230`; the broad node catch handles it (`consultants/engine/council.py:547-555 [no run_loop at this line; line is in researcher_node]`).
   - A non-string tool result is returned raw at `claude_hooks/agent_loop/runner.py:122`, after which `len(output)` at `claude_hooks/agent_loop/runner.py:124-127` can raise a `TypeError`; it likewise reaches the node catch (`consultants/engine/council.py:547-555 [no run_loop at this line; line is in researcher_node]`).

2. **Additive state survives the failed lane.**
   - `research`, `turns`, `research_rounds_used`, and token totals use additive reducers (`consultants/engine/graph.py:60-72`).
   - Each Send lane starts with empty research/turn lists and zeroed counters, so the tombstone is concatenated with successful lanes rather than replacing them (`consultants/engine/graph.py:358-385`).
   - `error` and `_role_failed` are ordinary last-writer fields rather than additive fields (`consultants/engine/graph.py:74-75`). For the specified **single** failed lane, successful lanes do not overwrite them.

3. **`compiled.stream` completes normally, but the server interprets the tombstone error as terminal.**
   - The server drains `compiled.stream(initial, stream_mode=["updates", "values"])` inside a graph-level `try` (`consultants/server/runner.py:136-154`).
   - The graph-crash handler, which sets `status="failed"` and writes failure artifacts, is reached only if an exception escapes the stream loop (`consultants/server/runner.py:155-161`). A caught researcher exception does not reach that handler.
   - After the stream completes, the server reads `error` and `_role_failed` and computes `terminal_status = "failed" if node_error else "completed"` (`consultants/server/runner.py:171-174`).
   - Because the researcher tombstone populated `error`, the externally reported status becomes `failed` even if synthesis returned a final answer (`consultants/server/runner.py:172-174`, `consultants/server/runner.py:186-202`).

4. **What the synthesizer actually receives.**
   - The synthesizer is given `state.get("research")`, but not the separate `error` field (`consultants/engine/council.py:718-723 [no error at this line; line is in synthesizer_node]`).
   - Every research-list item is rendered as a numbered `RESEARCHER REPORT (round N)` block (`consultants/engine/council.py:245-266`).
   - Consequently, the synthesizer sees text equivalent to:
     ```text
     RESEARCHER REPORT (round N):
     (researcher lane failed: <exception>)
     ```
     but it does not receive `error="researcher failed: ..."` as a separate field.
   - The synthesis prompt says not to mention the council, roles, or process (`consultants/engine/council.py:164-165`). Therefore, the synthesizer may either acknowledge the missing evidence or silently omit it. The result is potentially degraded and not reliably labeled as incomplete; it is not guaranteed to be a coherent complete answer.

5. **What the artifact writer stores.**
   - The result passed to storage contains the synthesizer’s `final_answer`, all merged turns, `status="failed"`, and the researcher error (`consultants/server/runner.py:186-199 [no md at this line; line is in run_council]`).
   - `summary.md` front matter records `status: failed` and the error (`consultants/engine/storage.py:97-110 [no md at this line; line is in _yaml_front_matter]`), while its body is still the synthesizer’s actual answer (`consultants/engine/storage.py:117-120`).
   - `transcript.md` renders each `RoleTurn`, including the failed researcher tombstone (`consultants/engine/storage.py:123-135 [no RoleTurn at this line; line is in render_transcript]`).
   - `metadata.json` is built from the result object and therefore preserves its status, error, final answer, and turns (`consultants/engine/storage.py:166-169`).

### User-visible outcome

This is a **degraded answer stored behind a failed consultation**, not a total loss of computation:

- The graph reaches synthesis, and a potentially usable answer is written into `summary.md` (`consultants/server/runner.py:186-199 [no md at this line; line is in run_council]`; `consultants/engine/storage.py:117-120`).
- The session nevertheless reports `status=failed` with `error="researcher failed: ..."` (`consultants/server/runner.py:172-202`).
- The normal completed-result path is therefore not entered, so the user experiences “the consultation failed on a researcher lane” rather than receiving the synthesized answer. The exact presentation by the unavailable skill file could not be verified.
- The answer may also be quietly incomplete because the synthesizer receives the failure as one report among others and is instructed not to discuss the internal process (`consultants/engine/council.py:164-165`, `consultants/engine/council.py:245-266`).

### Exactly one hardening change

Change the terminal-status decision at `consultants/server/runner.py:171-174` so a non-empty final answer from a non-synthesizer role failure is considered delivered:

```python
node_error = final_state.get("error")
node_failed = final_state.get("_role_failed")
final_answer = (final_state.get("final_answer") or "").strip()
delivered = bool(final_answer) and node_failed != "synthesizer"
terminal_status = "completed" if delivered else "failed"
```

This preserves `failed` for synthesizer failures and graph exceptions, while allowing a researcher-lane failure to expose the partially grounded answer through the completed-result path. The researcher error remains available in the result and artifact metadata (`consultants/server/runner.py:186-199 [no md at this line; line is in run_council]`; `consultants/engine/storage.py:108-110`).

### Verification limits

- The single-lane path, reducer behavior, synthesizer input, server status calculation, and artifact contents were checked directly from the cited source ranges.
- Concurrent failures from two researcher lanes writing the non-additive `error`/`_role_failed` channels were **not independently verified**; the LangGraph runtime behavior was unavailable. That case is outside the specified single-lane failure.
- The skill’s exact rendering of a failed consultation could not be verified because `.claude/skills/consultants/SKILL.md` was unavailable. The server’s `status=failed` behavior itself is directly established by `consultants/server/runner.py:171-202`.
