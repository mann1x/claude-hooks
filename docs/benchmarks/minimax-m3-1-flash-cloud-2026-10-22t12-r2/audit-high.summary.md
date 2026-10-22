---
session_id: csl-2026-10-22-1752-35a9
created: "2026-10-22T17:52:29"
question: "In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:\n\n1. CouncilState's additive reducers (consultants/engine/graph.py)\n2. consultants/server/runner.py's compiled.stream loop\n3. The synthesizer's input messages — what does it actually see?\n4. The storage.py artifact writer\n\nAnswer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line."
models:
  critic: "minimax-m3.1-flash:cloud"
  planner: "minimax-m3.1-flash:cloud"
  researcher: "minimax-m3.1-flash:cloud"
  synthesizer: "minimax-m3.1-flash:cloud"
topology: council
effort: high
duration_seconds: 738.29
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-10-22/tmp/claude-hooks-bench-bench-baseline-2026-05-07-vcrw
---

**Bottom line:** a single failed research lane does *not* abort the council — the graph runs to the synthesizer and a real (incomplete) answer is written to disk — but the session is still reported `status="failed"`, and the skill only prints the answer on `completed`, so the user sees a hard failure with no answer while the degraded answer sits unread in `summary.md`.

**1. Where the exception is born and caught**

- `run_loop` calls `chat_fn(payload)` with no guard: `claude_hooks/agent_loop/runner.py:230`; a retry-exhausted HTTP error surfaces as `RuntimeError("ollama chat HTTP …")` (`claude_hooks/get_advice/chat_client.py:285-287`) or a re-raised `URLError`/`OSError` (`claude_hooks/get_advice/chat_client.py:288-302`).
- A non-string tool result blows up at `len(output)`: `claude_hooks/agent_loop/runner.py:122` (executor call) and `claude_hooks/agent_loop/runner.py:126` (`len(output)`).
- Both are caught by the single `except Exception` around the loop call: `consultants/engine/council.py:547` (try) → `consultants/engine/council.py:555` (except) → tombstone delta at `consultants/engine/council.py:562-573`: `error="researcher failed: {e}"`, `_role_failed="researcher"`, `research=[err_text]`, `research_rounds_used=1`, and one researcher `RoleTurn` (`consultants/engine/council.py:568-572`). The intent is documented at `consultants/engine/council.py:557-561` (audit-high `csl-…-faa2`).
- Pinned by tests: `tests/test_consultants_council.py:514-529` (`error` set) and `tests/test_consultants_council.py:531-561 [no error at this line; line is in test_loop_failure_emits_tombstone_research]` (tombstone in `research`, turn emitted, counter +1).
- The lane's already-gathered evidence is lost: `run_loop` never returns, so no partial report reaches the tombstone (the except branch writes only the exception text, `consultants/engine/council.py:562`).

**2. Reducers (`CouncilState`)**

- Additive: `research` (`consultants/engine/graph.py:64`), `turns` (`consultants/engine/graph.py:68`), `research_rounds_used` / `critic_reroutes_used` / token counters (`consultants/engine/graph.py:69-72`).
- **Not additive**: `error` and `_role_failed` are bare `Optional[str]` with no reducer (`consultants/engine/graph.py:74`, `consultants/engine/graph.py:75`).
- Each `Send` lane seeds empty additive deltas so only the lane's delta merges (`consultants/engine/graph.py:377-381 [no Send at this line; line is in _fanout_after_planner]`); the lane's own `plan_item`/`lane_idx` are Send-injected (`consultants/engine/graph.py:373-374`, `consultants/engine/graph.py:79-80`).
- Net for one failed lane: the tombstone string concatenates cleanly into `research` and the surviving lanes' reports; `error` gets a single write — no merge conflict.

**3. `compiled.stream` loop**

- `compiled.stream(initial, stream_mode=["updates","values"])`: `consultants/server/runner.py:137-138`. Only `"values"` payloads update `final_state` (`consultants/server/runner.py:139-140`); `"updates"` payloads are used *solely* to flip `state.progress[role]` (`consultants/server/runner.py:144-154`).
- Because each lane is a separate `researcher` node invocation, the first lane to finish flips `progress["researcher"] = "done"` while sibling lanes are still running — progress is cosmetic/premature (`consultants/server/runner.py:145-153`).
- Any exception escaping the stream (graph-level, not node-level) is caught at `consultants/server/runner.py:155-161` → `state.status="failed"`, `state.error="graph crashed: …"`, and `_write_failed_artifacts` (`consultants/server/runner.py:211-237`) writes `final_answer=f"(consultation failed: {exc})"`, `turns=[]`, `models={}` (`consultants/server/runner.py:221`, `consultants/server/runner.py:224-225`) — a genuine total loss of the consultation, with an empty transcript.

**4. What the synthesizer actually sees**

- `synthesizer_node` passes `state.get("research")` verbatim into the prompt (`consultants/engine/council.py:718-723`).
- `build_synthesizer_messages` renders every element of `research` as `"\nRESEARCHER REPORT (round {i}): {r}"` (`consultants/engine/council.py:253-254 [no research at this line; line is in build_synthesizer_messages]`). So the dead lane arrives as a *report* whose body is the literal string `"(researcher lane failed: <exc>)"` (`consultants/engine/council.py:562`) — formatted identically to real evidence.
- The tombstone carries only the exception text, not the lane's `plan_item`/`lane_idx` (`consultants/engine/council.py:515-520` shows those are known in-node, but `consultants/engine/council.py:562` does not include them) — so the synthesizer cannot tell *which* sub-question is missing.
- The synthesizer prompt explicitly forbids mentioning roles or the process (`consultants/engine/council.py:164-165`), and nothing instructs it to hedge on a failed report. The synthesizer still runs and returns `final_answer` (`consultants/engine/council.py:726 [no final_answer at this line; line is in synthesizer_node]`, `consultants/engine/council.py:753-758`); only a synthesizer-side exception produces a placeholder (`consultants/engine/council.py:727-747`).
- Graph wiring does not short-circuit on `error`: the researcher→synthesizer edge is unconditional (`consultants/engine/graph.py:213-219 [no error at this line; line is in plan_topology]`, `consultants/engine/graph.py:391-397`), and the fan-out only replaces the planner→researcher edge (`consultants/engine/graph.py:349-385`, `consultants/engine/graph.py:399-404`).

**5. Terminal status and artifacts (the user-perceived failure)**

- `node_error = final_state.get("error")`; `terminal_status = "failed" if node_error else "completed"` (`consultants/server/runner.py:172-174`). The `error` value persists to the final state, so one caught lane failure marks the whole consultation failed even though a full answer was produced.
- The result carries the real `final_answer` (`consultants/server/runner.py:186`), the real `turns` (`consultants/server/runner.py:187`), `status="failed"` (`consultants/server/runner.py:189 [no turns at this line; line is in run_council]`) and `error="researcher failed: …"` (`consultants/server/runner.py:190`).
- Artifacts: `summary.md` front-matter carries `status: "failed"` and `error: …` (`consultants/engine/storage.py:108-110 [no md at this line; line is in _yaml_front_matter]`, `consultants/engine/storage.py:97-114`) with the degraded answer as the body (`consultants/engine/storage.py:117-120`); `transcript.md` shows the failed lane as a `## Researcher` section carrying the tombstone text (`consultants/engine/storage.py:129-136`) plus the synthesizer turn; `metadata.json` is `asdict(result)` (`consultants/engine/storage.py:166-169`); all three written atomically (`consultants/engine/storage.py:200-208`).
- The API still serves those artifacts regardless of status (`consultants/server/app.py:246-279`), but the skill only fetches `/result` "Once `status: completed`" (`.claude/skills/consultants/SKILL.md:104-106 [unverified — file not found]`, `.claude/skills/consultants/SKILL.md:120 [unverified — file not found]`) and on `failed` merely surfaces the role + error and suggests switching that role's model (`.claude/skills/consultants/SKILL.md:136-144 [unverified — file not found]`, `.claude/skills/consultants/SKILL.md:142-144 [unverified — file not found]`).
- **Perceived failure mode:** "consultation failed — researcher failed: HTTP timeout…" and *no answer*, even though a plausible answer was synthesized and persisted. If the user reads `summary.md` directly, they get a confidently-written answer that silently omits the dead lane's sub-question (the tombstone is unlabeled, `consultants/engine/council.py:562`, and the prompt forbids process talk, `consultants/engine/council.py:164-165`).

**Unverified / not tested**

- Whether *two* lanes failing in the same superstep raises a LangGraph `InvalidUpdateError` on the reducer-less `error`/`_role_failed` channels (`consultants/engine/graph.py:74`, `consultants/engine/graph.py:75`). LangGraph is not installed in this environment, so no live invocation was possible. If it does raise, the path is `consultants/server/runner.py:155-161` → `consultants/server/runner.py:224-225` → placeholder answer, empty transcript — total loss. Not confirmed.
- Semantic coherence of the degraded answer: the code guarantees a synthesizer invocation and assignment of `final_answer` (`consultants/engine/council.py:753-758`), not that the prose acknowledges the gap. No test asserts this.

**Recommended single hardening**

`consultants/server/runner.py:174` — derive the terminal status from answer availability, not from the mere presence of a node error:

```python
node_error = final_state.get("error")
node_failed = final_state.get("_role_failed")
terminal_status = ("failed"
                   if (node_error and not final_state.get("final_answer"))
                   else "completed")
```

This is the one place where a *caught* per-lane failure is escalated to a consultation-level hard failure. It feeds both the poll API (`consultants/server/runner.py:201`) and the persisted artifacts (`consultants/server/runner.py:189 [no turns at this line; line is in run_council]-190`, `consultants/engine/storage.py:108-110 [no md at this line; line is in _yaml_front_matter]`), so the lane error stays auditable while the status becomes `completed` — which makes the existing skill fetch and print the degraded answer (`.claude/skills/consultants/SKILL.md:104-120 [unverified — file not found]`) with no new status vocabulary to teach. A true total loss (no `final_answer`) still reports `failed`, and the graph-crash path is unaffected (`consultants/server/runner.py:155-161`).
