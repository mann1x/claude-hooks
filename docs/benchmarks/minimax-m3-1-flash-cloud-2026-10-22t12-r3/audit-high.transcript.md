# Consultation transcript — csl-2026-10-22-1818-a218

_Question_: In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:

1. CouncilState's additive reducers (consultants/engine/graph.py)
2. consultants/server/runner.py's compiled.stream loop
3. The synthesizer's input messages — what does it actually see?
4. The storage.py artifact writer

Answer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line.


## Planner

1. Verify the exception containment boundary in `consultants/engine/council.py:547-573` — confirm that `loop_runner(...)` (i.e. `claude_hooks/agent_loop/runner.py:138` `run_loop`) is called inside the `try` at `council.py:547`, and enumerate what is *outside* that try: the lazy import at `council.py:497-499`, message assembly at `council.py:521-527`, and the post-loop `_extract_text(final)` at `council.py:575` — i.e. which failures inside the run_loop call are caught and which escape as raw exceptions.

2. Confirm the reducer semantics in `consultants/engine/graph.py:64-80`: `research`, `turns`, `total_*_tokens` are `operator.add` (additive) while `error` and `_role_failed` at `graph.py:74-75` are plain `Optional[str]` (last-writer-wins) — determine (a) whether the tombstone string returned at `council.py:562-573` lands in the merged `research` list the synthesizer reads, and (b) whether two concurrently failing lanes can clobber each other's `error` text.

3. Check the Send fan-out payload at `consultants/engine/graph.py:349-386` and the node wrapper at `consultants/engine/graph.py:145-157` to establish whether an exception escaping `council.researcher_node` aborts the whole LangGraph superstep (killing sibling lanes and the downstream critic/synthesizer) or is contained to that one lane; note that the traced wrapper at `consultants/engine/trace.py:238-243` adds no `try/except`.

4. Trace the runner's two exit paths in `consultants/server/runner.py:136-203`: the `except Exception` at `runner.py:155-161` (graph-level crash → `state.status="failed"`, `_write_failed_artifacts` at `runner.py:211-237` writing `final_answer="(consultation failed: ...)"`) versus the normal drain path at `runner.py:167-203` where `terminal_status = "failed" if node_error else "completed"` at `runner.py:174` is derived purely from the presence of `final_state["error"]` — i.e. whether a *successful* synthesizer answer can coexist with `status="failed"`.

5. Establish exactly what the synthesizer receives by reading `council.build_synthesizer_messages` at `consultants/engine/council.py:245-266` against the merged state: the surviving lanes' reports, the literal tombstone text `"(researcher lane failed: ...)"`, the plan, and the critique — and check whether `SYNTHESIZER_SYSTEM` (`council.py:156-168`) instructs the model to surface the missing lane or to treat the tombstone as a normal report.

6. Verify the artifact writer's behavior in `consultants/engine/storage.py`: `render_summary` at `storage.py:117-120` writes front-matter `status:`/`error:` (from `_yaml_front_matter`, `storage.py:97-114`) but the body is `result.final_answer`, so a coherent synthesizer answer can exist on disk while the front-matter says `status: failed`; confirm `write_consultation` (`storage.py:200-208`) is reached in both the normal and the crashed path.

7. Confirm the user-perceived contract end-to-end: `consultants/server/app.py:231-243` (`GET /v1/consult/{sid}` returns `state.public_dict()` with `status`/`error`), `consultants/server/app.py:246-279` (`/result` returns `summary_markdown` + `metadata` regardless of status), and the skill's branch logic in `.claude/skills/consultants/SKILL.md:106` and `SKILL.md:134-147` (result is only fetched when `status: completed`; on `failed` the agent is told to surface the role error) — this determines whether a degraded-but-written answer ever reaches the user.

8. Cover the "non-string tool_executor result" sub-case separately: read `claude_hooks/agent_loop/runner.py:95-135` `execute_tool_calls` (the `len(output)` log at `runner.py:125`, `seen[key]=output` at `runner.py:128`, `content: output` at `runner.py:132`) and `consultants/engine/trace.py:211-233` `traced_tool` (re-raises at `trace.py:222`), then check `council.py:585-639` — whether a non-string tool output raises (→ caught at `council.py:555` → tombstone) or passes through silently and instead causes an empty final text → the forced-summary path and its stub text at `council.py:629-632` / `council.py:637-639`, which produces a `status: completed` run with a silently degraded answer.

9. Locate the exact single-line candidate for the hardening recommendation, and confirm it is the only place needed: `consultants/server/runner.py:172-174` (where `node_error` from `final_state["error"]` — set by the tombstone at `council.py:564` — forces `terminal_status="failed"`) together with `consultants/engine/graph.py:74-75` (the non-additive `error`/`_role_failed` channels), versus `consultants/engine/council.py:555-573` (the existing tombstone that already preserves the gap in `research`). Verify whether any test pins the current `status="failed"`-on-node-error behavior (check `tests/test_consultants_council.py:525-558` and any runner-level test) so the recommendation accounts for the expected contract.

---

## Researcher (round 1)

## Findings — failure path of a crashing Send-fan-out research lane

**1. Containment boundary in `researcher_node` — the exception is caught, and it is caught at the right place**
- `consultants/engine/council.py:547` opens the `try`; the `loop_runner(...)` call is `council.py:548-554`; `except Exception as e` is `council.py:555`. So an exception raised *inside* `run_loop` (HTTP timeout exhausting retries, or a non-string tool result) is caught here and converted to a tombstone update.
- The HTTP-timeout sub-case is real: `ChatClient.chat` raises `RuntimeError` after the retry budget (`claude_hooks/get_advice/chat_client.py:285-287`, re-raises `URLError/OSError` at `chat_client.py:302`), `TracedChat` re-raises it unchanged (`consultants/engine/trace.py:179-181`), `run_loop` has no internal try around `chat_fn(payload)` (`claude_hooks/agent_loop/runner.py:230`) → it propagates out of `run_loop` into `council.py:555`.
- The non-string-tool-result sub-case: `execute_tool_calls` calls `tool_executor(name, args_str, cwd)` (`claude_hooks/agent_loop/runner.py:122`) then does `len(output)` (`runner.py:126`) — a `None`/int return raises `TypeError` inside `run_loop` → same containment at `council.py:555`. (The production executor never raises and always returns str: `claude_hooks/caliber_proxy/tools.py:420-426`; `traced_tool` re-raises, `consultants/engine/trace.py:220-222`.)
- What is **outside** the try (escapes as a raw exception): the lazy import `council.py:497-499`; message assembly `council.py:521-527` (uses `state["question"]` / `state["plan"]`); `caps_for`/`LoopConfig` build `council.py:528-544`; and post-loop `_extract_text(final)` + `_usage_from(final)` at `council.py:575-576`. Also `BaseException` (e.g. `CancelledError`) is not caught by `except Exception`.

**2. Reducer semantics — the tombstone *does* reach the synthesizer, but the shared `error` channel is a single-writer channel**
- `research` is additive (`consultants/engine/graph.py:64`), as are `turns` (`:68`), `research_rounds_used` (`:69`), `total_*_tokens` (`:71-72`). The failure branch returns `"research": [err_text]` (`council.py:566`) with `err_text = f"(researcher lane failed: {e})"` (`council.py:562`) → the tombstone is concatenated into the merged `research` list the synthesizer reads. Covered by test `tests/test_consultants_council.py:531-561`.
- `error` and `_role_failed` are plain `Optional[str]` (`graph.py:74-75`) → default last-value channel, **not** additive. The failing lane writes `error` (`council.py:564`) and `_role_failed` (`council.py:565`).
- Consequence: two lanes failing in the same superstep both write `error`/`_role_failed`. In LangGraph a non-annotated channel is a `LastValue` channel, which is documented to reject two writes to the same key in one step — I could **not verify this in-repo** (langgraph is an external dep, `consultants/pyproject.toml:17`; no vendored source), so treat "two concurrent lane failures raise `InvalidUpdateError` and abort the whole superstep" as an unverified-but-material risk. Either way, the *text* of the second lane's error clobbers the first's; both tombstones still land in `research` (`council.py:566`).
- Side effect: a failed lane still consumes a research-round budget slot (`council.py:567` → additive `research_rounds_used`, counted at `council.py:383`).

**3. Send fan-out payload and node wrapper**
- Fan-out is gated on `len(plan_items) >= FANOUT_MIN_ITEMS` (`council.py:297`, used at `graph.py:351`) with `FANOUT_MAX_LANES = 3` (`council.py:307`); the `Send` payload is `graph.py:359-383` and deliberately zeroes the additive counters per lane (`graph.py:377-381`).
- The node wrapper `_wrap_researcher` (`graph.py:145-157`) adds **no** try/except; the tracer wrapper `traced_node` (`consultants/engine/trace.py:238-243`) also adds none (it only opens a span). The researcher node is registered at `graph.py:331-333`.
- Therefore any exception that escapes `council.researcher_node` (i.e. anything outside `council.py:547-573`, see finding 1) propagates out of the compiled graph into the runner's `except Exception` at `consultants/server/runner.py:155` → `state.status="failed"`, `state.error="graph crashed: ..."` (`runner.py:157-158`), `_write_failed_artifacts` (`runner.py:160`) writes `final_answer=f"(consultation failed: {exc})"` with **empty turns** (`runner.py:224-225`) — i.e. total loss of the surviving lanes' work. (Whether a single lane's exception also kills sibling lanes in the same superstep is LangGraph-internal and **not verifiable in this repo**.)

**4. Runner exit paths — the decisive line**
- Normal drain path: `consultants/server/runner.py:136-154` consumes `compiled.stream(..., stream_mode=["updates","values"])`; `final_state` is the merged state from the `"values"` payload (`runner.py:139-140`).
- `node_error = final_state.get("error")` (`runner.py:172`) and `terminal_status = "failed" if node_error else "completed"` (`runner.py:174`). **`terminal_status` is derived purely from the presence of `error`, not from whether an answer was produced.**
- So: a *successful* synthesizer answer **does** coexist with `status="failed"`. The artifacts are written with `final_answer=final_state.get("final_answer","")` (`runner.py:186`) — the real, good answer — plus `status=terminal_status` (`runner.py:189`) and `error=node_error` (`runner.py:190`), then `state.status = terminal_status` (`runner.py:201`).
- The graph-crash path (`runner.py:155-161`) is *not* taken for a lane failure; it is taken only for an exception escaping the node.
- Net answer to the question: **the council still produces a coherent answer, but the session surfaces as `status=failed`.**

**5. What the synthesizer actually sees**
- `build_synthesizer_messages` (`council.py:245-266`) receives `state.get("research")` (`council.py:720`) and appends each entry verbatim as `"\nRESEARCHER REPORT (round N):\n{...}"` (`council.py:253-254`). The failed lane's literal text `(researcher lane failed: <exc>)` is therefore presented **as if it were a research report**, with no lane label and no marker beyond the parentheses.
- `SYNTHESIZER_SYSTEM` (`council.py:156-168`) tells the model to consume "the planner's plan, researcher's report(s), and critic's verdict", cite `path:line`, and explicitly **"Do NOT mention the council, the roles, or the process"** (`council.py:164-165`). There is **no instruction** to treat a parenthesized failure marker as a gap, to flag missing coverage, or to downgrade confidence. The self-critic variant has the same blind spot (`council.py:177-193`).
- The critic sees the same tombstone (`council.py:669-671` → `council.py:237-238`) and `parse_critic_decision` defaults to `"ready"` on anything unparseable (`council.py:351-357`), so a failed lane will not trigger a re-route to repair the gap.

**6. Artifact writer — `consultants/engine/storage.py`**
- `write_consultation` writes summary/transcript/metadata atomically (`storage.py:200-208`, `storage.py:174-192`).
- `render_summary` (`storage.py:117-120`) = front-matter + body. Front-matter carries `status:` (`storage.py:108`) and `error:` when set (`storage.py:109-110`) — so `summary.md` contains a *good answer body* under `status: failed` / `error: researcher failed: ...`. Body is `result.final_answer` (`storage.py:119`).
- `render_transcript` (`storage.py:123-158`) iterates `turns`; the failed lane's tombstone turn is present (`council.py:568-572`) alongside the synthesizer's turn → the transcript shows the gap (`storage.py:129-136`).
- `render_metadata` (`storage.py:166-169`) → `metadata.json` with `status: "failed"` and `error` = the lane's error text.

**7. What the user actually perceives**
- `SessionState.public_dict()` exposes `status` and `error` (`consultants/server/app.py:61-75`); the poll route returns it (`app.py:231-243`); the result route returns `summary_markdown` + `metadata` (`app.py:246-279`).
- The skill only fetches the result **"Once `status: completed`"** (`.claude/skills/consultants/SKILL.md:106`) and on `status: failed` instructs the agent to surface the role + error and suggest switching that role's model (`.claude/skills/consultants/SKILL.md:136-147`).
- Perceived failure mode: a 3-lane fan-out loses one lane to an upstream timeout; the council still researched the other two and the synthesizer produced a real answer; but the session is reported as `failed` with `error: "researcher failed: <exc>"`, so the skill **never fetches or shows the answer** — the user gets a failure notice instead of a (degraded) answer, and the good answer sits unread in `.claude-hooks/consultants/<sid>/summary.md` (`storage.py:196-197`).

**8. Summary of the two distinct outcomes**
- Lane failure *inside* `run_loop` → caught at `council.py:555` → tombstone in `research` (`council.py:566`) → synthesizer still runs and answers → but `runner.py:174` marks the whole session `failed` → user sees failure, answer withheld.
- Exception *outside* `council.py:547-573` (lazy import `council.py:497-499`, message assembly `council.py:521-527`, `_extract_text` `council.py:575`) → escapes the node → caught at `runner.py:155` → `status="failed"`, artifacts contain only `"(consultation failed: ...)"` and no turns (`runner.py:224-225`) → total loss.

## Recommended single hardening change

**`consultants/server/runner.py:174`** — stop deriving the terminal status from the mere presence of a role-level `error`; a lane failure that still produced a final answer is a degraded *completion*, not a failed consultation:

```python
# before (runner.py:174)
terminal_status = "failed" if node_error else "completed"
# after
terminal_status = ("failed" if node_error and not final_state.get("final_answer")
                   else "completed")
```

Why this one line is sufficient and the right lever:
- It makes the "degraded but produced" branch reachable at all — today it is unreachable because `runner.py:174` short-circuits on `error` (`council.py:564`) regardless of the answer.
- It does not erase the failure signal: `error=node_error` is still written into `metadata.json` and into `summary.md`'s front-matter (`storage.py:109-110`), and `state.error` is still set (`runner.py:202`) — so `GET /v1/consult/{sid}` reports `status: completed` + `error: "researcher failed: ..."` and the skill's Step 5 prints the summary front-matter line, telling the user one lane degraded while still delivering the answer.
- The transcript keeps the tombstone turn (`council.py:568-572` → `storage.py:129-136`), so the on-disk audit trail is unchanged.

Known limitation of this one change (flagged, not a second recommendation): the synthesizer still receives the tombstone as a plain "RESEARCHER REPORT" (`council.py:253-254`) with no instruction to surface the gap (`council.py:156-168`), so the delivered answer can still be silently over-confident; and the shared `error`/`_role_failed` channels (`graph.py:74-75`) remain single-writer, so two lanes failing in the same superstep is a hazard whose behavior depends on LangGraph internals I could not verify in this repo (`consultants/pyproject.toml:17`).

---

## Researcher (round 1)

## Findings — consultants Send fan-out failure path

### 1. Where the lane exception is actually caught (it does NOT escape the node)

- `agent_loop.runner.run_loop` calls the upstream chat via `chat_fn(payload)` at `claude_hooks/agent_loop/runner.py:230` with no try/except; a retry-budget exhaustion surfaces as an exception from `ChatClient.chat` — HTTP path raises `RuntimeError` at `claude_hooks/get_advice/chat_client.py:285-287`, socket path re-raises at `claude_hooks/get_advice/chat_client.py:302`; `TracedChat.chat` re-raises at `consultants/engine/trace.py:181`.
- That exception propagates out of `run_loop` into `council.researcher_node`, whose `try` covers the `loop_runner(...)` call at `consultants/engine/council.py:547-554`, and whose `except Exception` at `consultants/engine/council.py:555` returns a **tombstone**: `error` (`council.py:564`), `_role_failed="researcher"` (`council.py:565`), `research=[err_text]` (`council.py:566`), `research_rounds_used=1` (`council.py:567`), plus a `turns` record (`council.py:568-572`).
- Consequence: the graph does **not** crash from a single lane's exception. The node wrapper `graph.py:145-157` and `traced_node` (`consultants/engine/trace.py:238-244`) add no error handling, but none is needed — `council.py:555` already swallows the exception.

### 2. Reducer behaviour in CouncilState

- Additive (list-concat) channels: `research` `consultants/engine/graph.py:64`, `turns` `graph.py:68`, `research_rounds_used` `graph.py:69`, `critic_reroutes_used` `graph.py:70`, token counters `graph.py:71-72`.
- Non-additive channels: `error: Optional[str]` `consultants/engine/graph.py:74` and `_role_failed: Optional[str]` `consultants/engine/graph.py:75` — plain last-writer-wins, no reducer.
- Fan-out payload per lane: `consultants/engine/graph.py:358-385` (each `Send` carries `plan_item`, `lane_idx`, and zeroed `research`/`turns`/counters so the lane's delta is additive-only).
- The surviving lanes' reports and the crashed lane's tombstone string both land in the single merged `research` list, so the gap **is** visible in state — but only as free text inside the report list.

### 3. What the synthesizer actually sees

- `build_synthesizer_messages` (`consultants/engine/council.py:245-266`) renders each merged `research` entry as a section: `RESEARCHER REPORT (round {i})` at `consultants/engine/council.py:253-254`.
- So the synthesizer input contains a section literally reading `(researcher lane failed: <exc>)` (string built at `consultants/engine/council.py:562`) plus the surviving lanes' real reports, the plan, and the critic verdict.
- The critic sees the same tombstone string (`consultants/engine/council.py:231-242`, loop at `:237-238`).
- Risk of silent degradation: `SYNTHESIZER_SYSTEM` instructs "Do NOT mention the council, the roles, or the process" (`consultants/engine/council.py:164-165`), so the synthesizer may absorb the failed-lane marker and emit a confident answer that silently omits that lane's evidence.

### 4. The runner's stream loop and the status decision

- Stream loop with `stream_mode=["updates","values"]` at `consultants/server/runner.py:137-138`; `final_state` is refreshed from `"values"` payloads at `consultants/server/runner.py:139-140` (this is why the additive reducers merge correctly).
- Graph-level crash handler at `consultants/server/runner.py:155-161` → `state.status="failed"` (`:157`), `state.error="graph crashed: {e}"` (`:158`), `_write_failed_artifacts` (`:160`).
- After a *successful* stream: `node_error = final_state.get("error")` `consultants/server/runner.py:172`, `node_failed = final_state.get("_role_failed")` `consultants/server/runner.py:173`, and **`terminal_status = "failed" if node_error else "completed"`** `consultants/server/runner.py:174`.
- The synthesizer's real answer is still written: `final_answer=final_state.get("final_answer","")` `consultants/server/runner.py:186`, then `status=terminal_status` `consultants/server/runner.py:189`, `error=node_error` `consultants/server/runner.py:190`, `storage.write_consultation(result, cwd=Path(cwd))` `consultants/server/runner.py:199`, `state.status = terminal_status` `consultants/server/runner.py:201`, `state.error = node_error` `consultants/server/runner.py:202`.
- `node_failed` is only logged as a warning (`consultants/server/runner.py:204-206`); it does not change the status.

### 5. Storage artifact writer

- `render_summary` writes front-matter + body where **body = `result.final_answer`** (`consultants/engine/storage.py:117-120`).
- Front-matter `status:` is written at `consultants/engine/storage.py:108`, `error:` at `consultants/engine/storage.py:109-110` — so `summary.md` for a lane-failure run contains a **complete, coherent answer body** under a header saying `status: failed` / `error: researcher failed: ...`.
- `write_consultation` writes all three files atomically (`consultants/engine/storage.py:200-208`); it is reached on the normal path (`consultants/server/runner.py:199`) and on the hard-crash path (`consultants/server/runner.py:160` → `consultants/server/runner.py:235`), where the body is the placeholder `(consultation failed: {exc})` (`consultants/server/runner.py:224`).
- `metadata.json` carries the same `status`/`error` via `asdict` (`consultants/engine/storage.py:166-169`).

### 6. What the user actually perceives

- Poll route returns `state.public_dict()` with `status` and `error` (`consultants/server/app.py:231-243`; fields at `consultants/server/app.py:61-75`, `status` at `:64`, `error` at `:70`).
- Result route returns `summary_markdown` + `metadata` **regardless of status** (`consultants/server/app.py:246-279`, return at `:275-279`; the only gate is artifact existence at `:263-274`).
- But the skill gates on status: fetch only when `status: completed` (`.claude/skills/consultants/SKILL.md:104-110`), and on `status: failed` the instruction is to surface the role + error to the user and suggest `/consultants--config` (`.claude/skills/consultants/SKILL.md:134-147`; role-error branch at `:142-144`).
- Result: the synthesized answer exists on disk and is served by the API, but the documented flow never asks for it. The user perceives a **total loss of a completed consultation** ("researcher failed: …") even though a usable answer was produced. Recovery only via `/consultants--show` (disk read, `consultants/cli.py:162-180`; skill prints it verbatim `.claude/skills/consultants--show/SKILL.md:31`).
- Answer to "coherent or total failure": **both** — the council produces a coherent-but-degraded answer (synthesizer sees the failed-lane marker), *and* the run surfaces as `status=failed` with that answer withheld.

### 7. The "non-string tool_executor result" sub-case

- `execute_tool_calls` assigns `output = tool_executor(...)` at `claude_hooks/agent_loop/runner.py:122`, then evaluates `len(output)` in the log call at `claude_hooks/agent_loop/runner.py:124-127` → a non-sized return (`None`, `int`) raises `TypeError` **inside run_loop** → caught by `council.py:555` → same tombstone → same `status=failed` outcome. `seen[key]=output` (`claude_hooks/agent_loop/runner.py:128`) and `"content": output` (`claude_hooks/agent_loop/runner.py:132`) are reached only for sized returns (e.g. `dict`/`list`).
- The traced wrapper is a second trap: `traced_tool` calls the executor at `consultants/engine/trace.py:218`, re-raises at `consultants/engine/trace.py:220-222`, and its `finally` computes `len(out or "")` at `consultants/engine/trace.py:230` — for a truthy non-sized object this raises inside `finally`, masking the original exception.
- In production the case is unreachable: the real executor is `claude_hooks/caliber_proxy/tools.py:420-439`, which catches every exception (`:436-438`) and coerces non-strings via `str(result)` (`:439`).
- A sized non-string (dict) that survives `len()` and is accepted upstream yields a lane with **no** `error` key → the empty-text path fires: forced summary call `consultants/engine/council.py:585-632`, and if that also comes back empty the stub `(researcher lane produced no findings within the iteration budget; gaps remain — see plan)` `consultants/engine/council.py:629-632` (exception variant at `consultants/engine/council.py:637-639`) becomes that lane's `research` entry (`council.py:659`). No `error` is set → `node_error` is `None` → `terminal_status="completed"` (`consultants/server/runner.py:174`) → the user gets a **silently degraded but "completed"** answer. This is the only sub-case that does not surface as `failed`.

### 8. Risk not verifiable in-repo (flagged, not asserted)

- `error`/`_role_failed` have no reducer (`consultants/engine/graph.py:74-75`) while all fan-out lanes write in the same superstep (`consultants/engine/graph.py:358-385`). If **two** lanes fail concurrently, LangGraph's last-value channel may reject the double write (an `InvalidUpdateError`-style abort), which would instead hit the graph-crash path at `consultants/server/runner.py:155-161` → `status=failed` with a placeholder body. No langgraph source is vendored in this repo (glob for `**/langgraph/**` → no matches; version pinned `langgraph>=0.2,<0.4` at `consultants/pyproject.toml:17`), so I could not verify the exact semantics — treat as a hypothesis, and note it makes the single-failure case the common one.

### 9. Test coverage of the status decision

- `tests/test_consultants_council.py:514-529` pins that a crashing loop sets `update["error"]`; `tests/test_consultants_council.py:531-561` pins the tombstone contents (`research` entry, `turns`, `research_rounds_used == 1`).
- **No test exercises `consultants/server/runner.py`** — grep for `make_runner|server.runner` across `tests/` returns no matches; the only `status == "failed"` test uses a stub runner that fabricates the failure (`tests/test_consultants_server.py:27-38`, assertion at `tests/test_consultants_server.py:285-297`). Therefore the `status=failed`-on-node-error mapping at `consultants/server/runner.py:174` is **not** pinned by any test and can be changed safely.

### 10. Recommended single hardening change

- **One line, `consultants/server/runner.py:174`**: stop promoting a *recoverable* role failure into a terminal `failed` when the synthesizer already produced an answer. Concretely, derive `terminal_status` as `"failed"` only when the graph itself failed, the synthesizer failed (`node_failed == "synthesizer"`), or no `final_answer` exists; otherwise `"completed"` while keeping `node_error`/`_role_failed` in `state.error` (`consultants/server/runner.py:202`), in the artifact front-matter (`consultants/engine/storage.py:108-110`) and in `metadata.json` (`consultants/engine/storage.py:166-169`).
- Why this single site: it is the only place the degraded-but-real answer is converted into a hard failure; the gap is already recorded losslessly upstream by the tombstone at `consultants/engine/council.py:562-572` (visible to critic at `consultants/engine/council.py:237-238` and synthesizer at `consultants/engine/council.py:253-254`), and the API already serves the answer on `status=failed` (`consultants/server/app.py:275-279`).
- The user-visible effect of the change: the polling agent sees `status=completed` plus a non-null `error` (`consultants/server/app.py:64`, `:70`), so the documented branch at `.claude/skills/consultants/SKILL.md:104-110` fetches and prints the answer while the role error remains visible in the status payload and the artifact header.
- Add a regression test alongside (none exists today): a stub that returns `{"error": "researcher failed: X", "final_answer": "answer", "_role_failed": "researcher"}` and asserts `status == "completed"` with `error` preserved.

---

## Critic (round 2)

DECISION: ready

The evidence is sufficient for the stated single-lane failure. An exception raised inside `run_loop` is caught by `researcher_node` at `consultants/engine/council.py:547-555`, which returns a tombstone containing `error`, `_role_failed`, an additive `research` entry, and a transcript turn at `consultants/engine/council.py:562-572`. `research` and `turns` are additive reducers at `consultants/engine/graph.py:64,68`, so the tombstone and surviving reports coexist in merged state. The synthesizer receives every merged entry verbatim as a “RESEARCHER REPORT” at `consultants/engine/council.py:245-266`; therefore it sees surviving reports plus the literal `(researcher lane failed: …)` marker, although its prompt does not require it to disclose the gap (`consultants/engine/council.py:156-168`). The graph can therefore finish and produce a degraded answer, but the runner independently maps any recorded node error to `status="failed"` at `consultants/server/runner.py:171-174`, while still storing the synthesizer’s answer at `consultants/server/runner.py:176-199`. The summary consequently contains that answer under `status: failed` because its body is `final_answer` (`consultants/engine/storage.py:97-120`). The user-facing skill fetches results only for `status: completed` and otherwise reports the role error (`.claude/skills/consultants/SKILL.md:92-93,104-110,134-147`), so the perceived failure is “consultation failed due to researcher,” despite a potentially useful answer existing in the artifacts. A non-string tool result is not reachable through the production executor because it coerces results to strings (`claude_hooks/caliber_proxy/tools.py:420-439`); an injected non-sized result would instead raise at `len(output)` in `claude_hooks/agent_loop/runner.py:122-127` and enter the same tombstone path. The single recommended hardening change is at `consultants/server/runner.py:174`: retain `status="failed"` when there is no substantive final answer or when the synthesizer itself failed, but classify a researcher failure with a substantive synthesizer answer as completed while preserving the warning in `error`/artifacts.

CORRECTIONS:
- report 1: claimed plain `error`/`_role_failed` channels have deterministic “last-writer-wins” clobber semantics — actual in-repo code only establishes that they have no reducer (`consultants/engine/graph.py:74-75`); concurrent-write semantics are external to the repository and were not verified. This does not affect the single-failing-lane conclusion.
- report 1: claimed the compiled stream’s “values” payload is necessarily the merged final state — actual code refreshes `final_state` from each such payload (`consultants/server/runner.py:136-140`), while the additive reducer definitions establish the intended merge semantics (`consultants/engine/graph.py:60-72`).
- report 1: called the synthesized result “coherent” — actual evidence establishes only that a final answer is produced; coherence and whether the failed lane is disclosed are not guaranteed because the tombstone is merely passed through as free text (`consultants/engine/council.py:253-265`).

---

## Synthesizer

A single exception inside `run_loop` does **not** abort the Send superstep: it is converted into a researcher tombstone, the graph can still synthesize a potentially useful degraded answer, but the runner subsequently marks the consultation `status="failed"`—so the normal user flow reports a hard failure and withholds the answer that was actually written.

### 1. Exact failure path

1. `researcher_node` calls `loop_runner(...)` inside a `try` and catches `Exception`; the `run_loop` invocation itself is therefore contained (`consultants/engine/council.py:547 [no run_loop at this line; line is in researcher_node]`, `consultants/engine/council.py:555`). The catch returns:
   - `error="researcher failed: ..."`,
   - `_role_failed="researcher"`,
   - one `research` entry containing `(researcher lane failed: ...)`,
   - and a matching transcript turn (`consultants/engine/council.py:562-572`).

2. `research` and `turns` use additive reducers, so the failed lane’s tombstone and turn coexist with surviving lanes’ reports and turns (`consultants/engine/graph.py:64`, `consultants/engine/graph.py:68`). `error` and `_role_failed` have no reducer (`consultants/engine/graph.py:74-75`); the repository therefore does not establish deterministic concurrent-write behavior if multiple lanes fail simultaneously. That external LangGraph detail is unverified, but it does not affect the single-failure path.

3. The runner drains `compiled.stream(..., stream_mode=["updates", "values"])` and refreshes `final_state` from each `values` payload (`consultants/server/runner.py:136-140`). Because the researcher returned a normal state update instead of raising, execution does not enter the graph-crash handler at `consultants/server/runner.py:155-161`; the critic and synthesizer can still run.

4. The synthesizer receives the merged `research` entries and constructs one `RESEARCHER REPORT (round N)` section for each, inserting the entry text verbatim (`consultants/engine/council.py:245-266`, `consultants/engine/council.py:253-254`). Thus it sees:
   - the surviving lanes’ reports,
   - the literal `(researcher lane failed: ...)` tombstone,
   - the planner’s plan,
   - and the critic’s verdict (`consultants/engine/council.py:249-256`).

   The prompt does not require disclosure of missing coverage and instead says not to mention roles or the process (`consultants/engine/council.py:156-168`). Consequently, evidence establishes that a final answer is produced, but **not** that it will be coherent or disclose the failed lane.

5. After the stream drains, the runner finds the researcher’s `error` and unconditionally chooses `terminal_status="failed"` whenever it is present (`consultants/server/runner.py:171-174`). It nevertheless stores the synthesizer’s `final_answer` in the result, records `status="failed"` and the researcher error, and writes the artifacts (`consultants/server/runner.py:176-199`).

6. The normal artifact writer writes `summary.md`, `transcript.md`, and `metadata.json` (`consultants/engine/storage.py:200-208`). The summary front matter contains `status` and `error`, while its body is `result.final_answer` (`consultants/engine/storage.py:97-120`). The resulting `summary.md` therefore contains the synthesized answer body under `status: failed` and the researcher error.

### 2. What the user perceives

The API status response exposes both `status` and `error` (`consultants/server/app.py:61-75`), and the result endpoint will serve the written summary even for a failed session (`consultants/server/app.py:246-279`). However, the documented client flow fetches the result only after `status: completed` (`.claude/skills/consultants/SKILL.md:104-120 [unverified — file not found]`); for `status: failed`, it tells the agent to surface the role error rather than print the synthesized answer (`.claude/skills/consultants/SKILL.md:134-147 [unverified — file not found]`).

So the user-facing failure mode is:

> “The consultation failed because the researcher failed.”

The surviving research and synthesized answer remain in the session artifacts, but the normal workflow does not retrieve or display them.

This differs from a genuine graph crash: if an exception escapes outside the protected `loop_runner` call—for example, during import, message assembly, or post-loop extraction—it reaches the runner’s graph-crash handler, which writes a placeholder `(consultation failed: ...)` with no turns (`consultants/engine/council.py:497-499`, `consultants/engine/council.py:521-527`, `consultants/engine/council.py:575-576`, `consultants/server/runner.py:155-160`, `consultants/server/runner.py:224-235`).

### 3. Non-string tool-executor sub-case

- A `None` or integer result raises `TypeError` at `len(output)` inside `run_loop` (`claude_hooks/agent_loop/runner.py:122-127 [no run_loop at this line; line is in execute_tool_calls]`). It is caught by the same researcher boundary and therefore follows the tombstone → synthesized answer → `status="failed"` path (`consultants/engine/council.py:547 [no run_loop at this line; line is in researcher_node]-566`).
- A sized non-string such as a dictionary or list passes `len()` and is placed directly into the tool-message content without a string validation or coercion at this boundary (`claude_hooks/agent_loop/runner.py:122-133`). Whether a later client rejects it is not established by the reviewed code.
- The production executor catches tool failures and coerces results with `str(result)`, so this non-string case is unreachable through that implementation (`claude_hooks/caliber_proxy/tools.py:420-439`).
- Separately, if the loop returns an empty final text rather than raising, the forced-summary path can emit the “no findings” or “failed to produce a written report” stub (`consultants/engine/council.py:585-639`). That path does not set `error`, so the runner classifies it as `completed` (`consultants/server/runner.py:171-174`).

### Exactly one hardening change

Change the terminal-status decision at `consultants/server/runner.py:174` so a recoverable researcher failure with a substantive synthesized answer is a completed, degraded consultation; retain failure when the synthesizer failed or no substantive answer exists:

```python
terminal_status = (
    "failed"
    if node_error and (
        node_failed == "synthesizer"
        or not final_state.get("final_answer", "").strip()
    )
    else "completed"
)
```

This preserves `node_error` in the session and artifacts (`consultants/server/runner.py:190`, `consultants/server/runner.py:202`) while allowing the normal result-fetch path to deliver the degraded answer (`.claude/skills/consultants/SKILL.md:104-120 [unverified — file not found]`).

---
