# Consultation transcript — csl-2026-10-22-1724-d606

_Question_: In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:

1. CouncilState's additive reducers (consultants/engine/graph.py)
2. consultants/server/runner.py's compiled.stream loop
3. The synthesizer's input messages — what does it actually see?
4. The storage.py artifact writer

Answer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line.


## Planner

1. Map `CouncilState` channel behavior in `consultants/engine/graph.py:50-80`, distinguishing additive `research`/`turns`/counters from ordinary `error`/`_role_failed` writes, then inspect the per-lane `Send` payloads and zero-initialized deltas at `consultants/engine/graph.py:342-385`.

2. Trace the stated exceptions from `chat_fn` and `tool_executor` inside `claude_hooks/agent_loop/runner.py:95-135` and `claude_hooks/agent_loop/runner.py:215-310` into `researcher_node`’s exception handler and tombstone update at `consultants/engine/council.py:546-573`.

3. Determine the exact parallel-merge consequence when one lane returns the failure tombstone while other lanes return successful research, including whether any two lanes write the same ordinary `error` or `_role_failed` channel and trigger a LangGraph concurrent-update exception (`consultants/engine/graph.py:64-75`, `consultants/engine/council.py:555-573`, `consultants/engine/council.py:650-664`).

4. Reconstruct the synthesizer’s literal input from the merged state using `build_synthesizer_messages` at `consultants/engine/council.py:245-266` and `synthesizer_node` at `consultants/engine/council.py:715-723`, verifying whether the lane-failure tombstone, `error`, and `_role_failed` are present and distinguishable.

5. Follow `compiled.stream` through the runner’s success path and graph-crash handler at `consultants/server/runner.py:124-174`, then verify how a node-recorded `error` determines `terminal_status` and whether the synthesizer’s `final_answer` is still persisted at `consultants/server/runner.py:176-203`.

6. Verify the user-visible artifact split in `consultants/engine/storage.py:97-120`, `consultants/engine/storage.py:123-169`, and `consultants/engine/storage.py:200-208`: whether `summary.md` contains both the synthesizer answer and failed-status front matter, while `transcript.md` and `metadata.json` expose the lane tombstone and error.

7. Compare the single highest-value hardening at the reducer boundary (`consultants/engine/graph.py:64-75`) versus the status boundary (`consultants/server/runner.py:171-203`), selecting exactly one change that preserves partial results while preventing either concurrent failure-marker loss or a misleading failed/completed terminal state.

---

## Researcher (round 1)

## Findings (researcher)

### 1. Reducer/channel map — `CouncilState` (`consultants/engine/graph.py:50-80`)
- Additive (concatenate/sum) channels: `research: Annotated[list, operator.add]` (graph.py:64), `turns` (graph.py:68), `research_rounds_used` / `critic_reroutes_used` / `total_prompt_tokens` / `total_completion_tokens` (graph.py:69-72).
- Plain (LastValue, non-annotated) channels: `error: Optional[str]` (graph.py:74) and `_role_failed: Optional[str]` (graph.py:75). Both are written by role-failure tombstones: planner (council.py:453-454), researcher (council.py:564-565), critic (council.py:684-685), synthesizer (council.py:737-738). Because they are LastValue channels, **two lanes writing `error` in the same superstep is a concurrent write to the same non-additive channel** — LangGraph's default LastValue reducer raises `InvalidUpdateError` in that case. *(This is LangGraph semantics, not repo-verifiable: `langgraph` is deliberately absent from the main env, see graph.py:1-9; version pin `langgraph>=0.2,<0.4` at `consultants/pyproject.toml:17`.)* → **two failing lanes = whole graph aborts.**
- Fan-out `Send` payloads (graph.py:358-385): each lane gets a **fresh, zero-initialized** input state — `"research": []`, `"turns": []`, `"research_rounds_used": 0`, token counters `0` (graph.py:377-381) — plus `plan_item` (graph.py:373) and `lane_idx` (graph.py:374). Lanes never see each other's research.
- Fan-out triggers when `len(plan_items) >= FANOUT_MIN_ITEMS = 2` (council.py:297) and is capped at `FANOUT_MAX_LANES = 3` (council.py:307) → up to 3 concurrent lanes in one superstep.

### 2. Exception path inside `run_loop` → researcher tombstone
- `chat_fn(payload)` is called with **no try/except** (`claude_hooks/agent_loop/runner.py:230`); an exhausted-retry `RuntimeError` from the chat client (`claude_hooks/get_advice/chat_client.py:285`, re-raise at :302) propagates straight out of `run_loop`.
- Tool execution is also unguarded: `tool_executor(name, args_str, cwd)` at `claude_hooks/agent_loop/runner.py:122`; a non-string return then blows up on `len(output)` in the `log.info` at `claude_hooks/agent_loop/runner.py:124-127` → `TypeError` propagates out of `run_loop`. (Production `tools.execute` always returns `str`, `claude_hooks/caliber_proxy/tools.py:439`, so this is stub-only in prod.)
- Both land in `researcher_node`'s `except Exception` at `consultants/engine/council.py:555`, which returns a **tombstone**: `error` (council.py:564), `_role_failed="researcher"` (council.py:565), `research=["(researcher lane failed: {e})"]` (council.py:562, 566), `research_rounds_used=1` (council.py:567), and a `RoleTurn` tombstone (council.py:568-572). The additive channels therefore record the failure; the two LastValue channels are the only conflict source.
- **Not covered by that handler:** the `run_loop` import at `council.py:497-499` and message assembly at `council.py:521-527` sit *outside* the `try` (which starts at council.py:547). An `ImportError` there (e.g. `claude_hooks` not importable in the consultants env) escapes the node → LangGraph aborts the graph → runner-level `except` at `consultants/server/runner.py:155` → `status="failed"`, artifacts contain `"(consultation failed: {exc})"` (runner.py:224).

### 3. Merge consequence
- **One lane fails:** merged `research` = surviving lane reports + one tombstone string; `turns` = all lane turns; `error` = `"researcher failed: …"`; `_role_failed` = `"researcher"`. No concurrent write to a LastValue channel (only one lane writes them) → **no `InvalidUpdateError`; the graph continues to critic → synthesizer.**
- **Two+ lanes fail:** both write `error` and `_role_failed` in the same superstep → `InvalidUpdateError` → the whole `compiled.stream` raises → runner.py:155-161 → `status="failed"`, `state.error="graph crashed: …"`, and `_write_failed_artifacts` writes `final_answer="(consultation failed: …)"` (runner.py:224). **No answer at all.** The same upstream flap that kills two lanes kills the whole consultation.
- **No recovery path for the dead lane:** merged `research_rounds_used` = number of lanes (3), because each lane contributes `+1` (council.py:567). `route_after_critic` then hits the round cap and forces the synthesizer (council.py:383-389). With `EFFORT_CAPS["high"] = (3, 2, 10, 8)` (council.py:72) → `3 >= 3` → the critic's re-route is dead at high; at low/medium the critic node is dropped outright (`consultants/server/runner.py:58-63`, caps council.py:68-70). Only `effort=max` (cap 8, council.py:74) could re-run the researcher, and the re-route uses the single-researcher path, not the lane path (graph.py:406-411).
- **Lane identity is lost in the merge:** every lane sees `research_rounds_used=0` (graph.py:379) so every researcher turn gets `round=1` (council.py:507-508, 569). The failing lane's tombstone is indistinguishable by index; only its literal text differs.

### 4. What the synthesizer actually sees
- `synthesizer_node` (council.py:715-723) → `build_synthesizer_messages(question, plan, state["research"], state.get("critique"))` (council.py:718-721).
- `build_synthesizer_messages` (council.py:245-266) renders each merged research entry as a **numbered "RESEARCHER REPORT (round i)"** block (council.py:253-254). The failure tombstone is therefore presented as a report whose body is `"(researcher lane failed: HTTP timeout exhausted retries)"` — no dedicated field, no lane number, no "this lane produced nothing" framing beyond that literal sentence.
- The synthesizer system prompt explicitly forbids process talk: "Do NOT mention the council, the roles, or the process — the user only wants the answer" (council.py:164). So the gap is *visible in the prompt* but the model is instructed not to narrate it. The critic (enabled only at high/max) sees the same list (council.py:237-238, 669-671) and can name the gap, but the re-route is cap-blocked (above).
- The synthesizer therefore writes a **coherent answer over the surviving evidence** (council.py:753-758). The final answer text itself never mentions the failed lane unless the model chooses to.

### 5. Runner status + artifact writer — the perceived failure mode
- The `error` channel value survives into the merged final state, so `node_error = final_state.get("error")` is truthy → `terminal_status = "failed"` (`consultants/server/runner.py:172-174`).
- Artifacts are still written with the **real** synthesizer answer: `final_answer=final_state.get("final_answer", "")` (runner.py:186), `status="failed"`, `error=node_error` (runner.py:189-190), `storage.write_consultation(...)` (runner.py:199). `_write_failed_artifacts` is *not* used on this path (only runner.py:160).
- `summary.md` front-matter therefore says `status: failed` + `error: researcher failed: …` while the body is a confident, complete-looking answer (`consultants/engine/storage.py:97-120`, status/error at :108-110).
- `transcript.md` shows the tombstone turn, but all researcher turns share `round=1`, so three identical "## Researcher (round 1)" headers appear with no lane label (storage.py:129-136; `_is_multi_round` at storage.py:161-163).
- Perceived: `claude-consultants status <sid>` returns `status: failed` with `error: researcher failed: …` (app.py:61-75, `public_dict`); the skill only fetches the result on `completed` (`.claude/skills/consultants/SKILL.md:92-93`, Step 5 at :104-118) and on `failed` tells the agent to surface the role error and suggest switching models (SKILL.md:134-147) — **so the coherent-but-degraded answer is never shown to the user by the normal flow**, even though it exists on disk. The user sees a hard failure for a consultation that actually produced an answer.

### 6. Net answer
- **One lane dies:** the council *does* produce a coherent (degraded) answer; the synthesizer sees the tombstone as a numbered report; but the session is stamped `status=failed` and the answer is withheld by the client flow. Failure mode = **"failed" status attached to a usable-but-incomplete answer, plus a silent gap** (the final answer omits the un-researched sub-plan, and the prompt discourages mentioning it).
- **Two lanes die:** `InvalidUpdateError` on the `error`/`_role_failed` LastValue channels → graph abort → `status=failed` with `"(consultation failed: …)"` and zero research in the artifacts. Failure mode = **total loss, no partial answer**.

## Recommended single hardening
**Give `error` and `_role_failed` merging reducers in `CouncilState` — `consultants/engine/graph.py:74-75`** (e.g. `Annotated[Optional[str], _merge_error]` that concatenates distinct strings instead of LastValue).

Why this one:
- It removes the only path where the *entire* consultation dies from a lane failure (the concurrent LastValue write at graph.py:74-75 fed by council.py:564-565) — turning the "two lanes die ⇒ no answer" case into "two lanes die ⇒ two tombstones in `research` + a joined error string", i.e. the same degraded-but-usable outcome as a single lane death.
- It keeps the channel type `str`, so `consultants/server/runner.py:172-190` and `storage.py:109-110` need no change (a list-typed `error` would break `_yaml_str`).
- It also preserves *how many* lanes died in the error text, which is the prerequisite for any later distinction between "one lane degraded" and "all lanes dead".

Caveat (not part of the change): the status boundary `consultants/server/runner.py:172-174` still maps any role-level `error` to `status="failed"`, so a single-lane failure will still be reported as failed even after this fix; that is a separate behavior decision, not a crash.

---

## Researcher (round 1)

Research report — sub-lane 2 (failure path + artifacts + one hardening).

## Trace of the unhandled `run_loop` exception

- **Origin**: `run_loop` has no try/except around the upstream call or the tool call — `chat_fn(payload)` at `claude_hooks/agent_loop/runner.py:230` and `tool_executor(name, args_str, cwd)` at `claude_hooks/agent_loop/runner.py:122` (invoked from `:300`) both propagate. `ChatClient` re-raises after the retry budget is spent: `RuntimeError` at `claude_hooks/get_advice/chat_client.py:285`, raw `URLError`/`OSError` re-raise at `claude_hooks/get_advice/chat_client.py:302`. `TracedChat` re-raises (`consultants/engine/trace.py:181`) and `traced_tool` re-raises (`consultants/engine/trace.py:222`). Note: the *real* tool executor never raises — `execute()` catches everything and returns `"error: ..."` strings (`claude_hooks/caliber_proxy/tools.py:436`), and coerces a non-string return to `str` at `claude_hooks/caliber_proxy/tools.py:439` — so the realistic trigger is the chat timeout, not the tool.
- **Caught where**: `researcher_node` wraps the whole `loop_runner(...)` call in try/except at `consultants/engine/council.py:547-555` and returns a **tombstone**, not an exception: `error`, `_role_failed`, `research: ["(researcher lane failed: …)"]`, `research_rounds_used: 1`, one `RoleTurn` (`consultants/engine/council.py:562-573`). So the node does **not** raise → the graph does **not** crash on a single-lane failure.
- **Reducer boundary** (`consultants/engine/graph.py:64-75`): `research`, `turns`, `research_rounds_used`, `total_*_tokens` are additive (`operator.add`); `error` and `_role_failed` are **plain `Optional[str]`** — i.e. single-writer (LastValue) channels, while the fan-out writes them from N concurrent `Send` tasks (`consultants/engine/graph.py:358-385`). A *single* failing lane writes `error` once → no conflict; **two** lanes failing in the same superstep both write `error`/`_role_failed` → LangGraph raises an invalid-update error on a non-annotated key (library-side behavior, not verifiable in-repo), which escapes `compiled.stream` and lands in the graph-crash handler.
- **Graph-crash handler** (`consultants/server/runner.py:155-161`): sets `state.status = "failed"`, `state.error = "graph crashed: …"`, and calls `_write_failed_artifacts` (`consultants/server/runner.py:211-237`), which writes `status="failed"`, `final_answer="(consultation failed: …)"`, **`turns=[]`** and **`models={}`** — i.e. *all* surviving lanes' research and any synthesizer answer are discarded. This is the only path where the consultation truly fails end-to-end, and it needs ≥2 simultaneous lane failures.
- **Success path** (`consultants/server/runner.py:136-174`): the stream loop swallows node errors; `final_state` comes from the `"values"` stream mode (`consultants/server/runner.py:139-141`), so the tombstoned `error` *is* present in the merged state. `terminal_status = "failed" if node_error else "completed"` at `consultants/server/runner.py:174` — **any** node-recorded error flips the whole session to `failed`, even though the synthesizer already ran and produced an answer.
- **Synthesizer input**: `build_synthesizer_messages` (`consultants/engine/council.py:245-266`) renders every entry of the merged `research` list as a `"RESEARCHER REPORT (round N)"` block (`consultants/engine/council.py:253-254`). The tombstone string `(researcher lane failed: …)` is therefore presented to the synthesizer **as if it were a report**, indistinguishable from a real lane report; there is no separate "a lane failed" field in the prompt. `SYNTHESIZER_SYSTEM` explicitly discourages process disclosure (`consultants/engine/council.py:164-165`: "Do NOT mention the council, the roles, or the process"). Net effect: the synthesizer can write a confident answer that silently omits the failed lane's sub-question.
- **Storage split**: `write_consultation` (`consultants/engine/storage.py:200-208`) writes all three files. `summary.md` = YAML front matter with `status: failed` (`consultants/engine/storage.py:108`) + `error: "researcher failed: …"` (`consultants/engine/storage.py:109-110`) **followed by the synthesizer's real answer** as the body (`consultants/engine/storage.py:117-120`). `transcript.md` iterates `turns` (`consultants/engine/storage.py:129-136`), so the tombstone appears as a Researcher turn — but every fan-out lane reports `round=1` (`consultants/engine/council.py:507-508` + `:568-569`, lane state has `research_rounds_used: 0` at `consultants/engine/graph.py:379`), so the transcript shows N identical `## Researcher (round 1)` headers with no lane id (`consultants/engine/storage.py:161-163`). `metadata.json` carries `status`, `error`, and the same turn list (`consultants/engine/storage.py:166-169`).
- **User-visible outcome**: `state.status = "failed"` (`consultants/server/runner.py:201-203`) → the status poll reports failed (`consultants/server/app.py:61-75`) and the sessions index records `status: failed` (`consultants/server/app.py:329-345`). The artifacts are still served by `/v1/consult/{sid}/result` (only "running" yields 409 — `consultants/server/app.py:263-274`) and `claude-consultants result|show` (`consultants/cli.py:148-151`, `consultants/cli.py:162-180`). But the skill script gates result-fetching on `status: completed` (`.claude/skills/consultants/SKILL.md:104-121`) and, on `status: failed`, instructs the agent to surface only the role+error (`.claude/skills/consultants/SKILL.md:136-144`). **So the good answer is on disk but is discarded by the default orchestration path.**
- **Secondary reducer bug (same fan-out)**: each lane contributes `+1` to the additive `research_rounds_used` (`consultants/engine/council.py:567`, `:660`; reducer at `consultants/engine/graph.py:69`), so after a 3-lane fan-out the counter is 3, not 1. `route_after_critic` compares that against `caps.researcher_rounds_max` (=1 at medium, `consultants/engine/council.py:70`) and therefore always routes straight to the synthesizer (`consultants/engine/council.py:373-383`) — a critic's `needs_more_research` can never trigger a recovery round after fan-out, so a degraded lane is never re-run.

## Verdict on the question asked

Not "entire consultation fails" in the common case: with **one** failing lane the graph completes, the synthesizer writes a real (but silently incomplete) answer, and the session is nonetheless labeled `status=failed` with `error: researcher failed: …` (`consultants/server/runner.py:174`, `:189-202`). The perceived failure mode is a **mislabeled-but-usable result**: the answer exists on disk, yet the skill path reports "consultation failed (researcher failed: …)" and never fetches it. The genuinely catastrophic variant (everything lost) requires ≥2 concurrent lane failures hitting the non-additive `error`/`_role_failed` channels (`consultants/engine/graph.py:74-75`) and lands in `_write_failed_artifacts` with `turns=[]` (`consultants/server/runner.py:215-233`).

## Recommended single hardening change

**At the status boundary — `consultants/server/runner.py:174`:** derive the terminal status from whether the synthesizer actually produced an answer, instead of from the mere presence of a node error. Replace `consultants/server/runner.py:174` with a three-way derivation: `completed` when no `error`; `failed` when `error` is set **and** (`_role_failed == "synthesizer"` or `final_answer` is empty — the synthesizer-crash case is `consultants/engine/council.py:736-747`); otherwise **`partial`** (answer present, one or more lanes degraded). Keep `state.error` / `ConsultationResult.error` populated (`consultants/server/runner.py:190`, `:202`) so the failure stays visible, and reuse the same `final_answer` string at `consultants/server/runner.py:186`.

Why this one and not the reducer boundary: it fires on **every** single-lane failure (the common case in the question), it preserves the partial results that are already being written (`consultants/server/runner.py:199`), and it removes the misleading `failed` label without touching four node return contracts. The reducer-boundary alternative (making `error`/`_role_failed` additive at `consultants/engine/graph.py:74-75`) fixes the rarer double-failure crash but requires changing the `error`/`_role_failed` returns in all four nodes (`consultants/engine/council.py:453-454`, `:564-565`, `:684-685`, `:737-738`) and their readers (`consultants/server/runner.py:172-173`, `:190`).

Two one-line consequences of the recommended change (not separate hardenings): `status: "partial"` must be added to the documented vocabulary at `consultants/engine/storage.py:65`, and the skill script's result gate at `.claude/skills/consultants/SKILL.md:106` must treat `partial` like `completed` (the HTTP path already serves the artifacts — `consultants/server/app.py:263-274`).

Optional follow-up (separate, not the recommendation): the synthesizer should be told to name the uncovered sub-question instead of silently omitting it — the tombstone is currently camouflaged as a "RESEARCHER REPORT" (`consultants/engine/council.py:253-254`) while the prompt forbids mentioning the process (`consultants/engine/council.py:164-165`).

---

## Critic (round 2)

DECISION: ready

The evidence is sufficient for the stated single-lane case. The `run_loop` exception is caught by `researcher_node` and converted into a tombstone containing `error`, `_role_failed`, additive research/turn deltas, and a round counter (`consultants/engine/council.py:547-573`). That tombstone merges with successful lanes through the additive `research`/`turns` channels (`consultants/engine/graph.py:60-72`), while the tombstone appears to the synthesizer as an ordinary numbered researcher report (`consultants/engine/council.py:245-266`, `consultants/engine/council.py:715-723`). The graph can therefore reach the synthesizer and produce a coherent but incomplete answer. Nevertheless, the merged `error` forces terminal status `failed` (`consultants/server/runner.py:171-190`), and the successful-path writer persists that real synthesizer answer with failed-status front matter (`consultants/server/runner.py:186-199`; `consultants/engine/storage.py:97-120`). The normal skill path reports a role failure and fetches results only after `completed`, so the user perceives a hard failed consultation even though a usable degraded answer exists on disk (`.claude/skills/consultants/SKILL.md:92-93`, `.claude/skills/consultants/SKILL.md:104-109`, `.claude/skills/consultants/SKILL.md:134-144`). For a graph-level exception, the runner instead writes only a failure placeholder with no turns (`consultants/server/runner.py:155-161`, `consultants/server/runner.py:211-235`). Recommended single hardening: change the status derivation at `consultants/server/runner.py:174` so a nonempty synthesizer answer plus a non-synthesizer role failure is represented as a degraded/partial terminal status rather than `failed`; this directly fixes the user-visible failure mode for the question’s single-lane scenario, whereas reducer hardening at `consultants/engine/graph.py:74-75` only protects the rarer simultaneous multi-lane case.

UNVERIFIED: simultaneous failures on two parallel lanes cause LangGraph to reject concurrent updates to the unannotated `error`/`_role_failed` channels — an integration test in the dedicated `langgraph>=0.2,<0.4` environment would confirm this library-side behavior; it is not needed to establish the requested one-lane outcome.

---

## Synthesizer

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

---
