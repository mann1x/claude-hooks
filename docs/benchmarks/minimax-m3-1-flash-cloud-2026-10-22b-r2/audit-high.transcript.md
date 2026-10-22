# Consultation transcript — csl-2026-10-22-1638-91e5

_Question_: In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:

1. CouncilState's additive reducers (consultants/engine/graph.py)
2. consultants/server/runner.py's compiled.stream loop
3. The synthesizer's input messages — what does it actually see?
4. The storage.py artifact writer

Answer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line.


## Planner

1. Trace `council.researcher_node` and its call into `agent_loop.runner.run_loop` in `consultants/engine/council.py`, verifying whether the two named exceptions are caught, converted into a state update, or allowed to escape the researcher node.
2. Verify how an escaped exception affects LangGraph’s parallel `Send` branch merging in `consultants/engine/graph.py:342-404`, especially whether the additive `research`, `turns`, and token reducers at `consultants/engine/graph.py:60-72` preserve successful sibling lanes or whether branch failure aborts the whole graph.
3. Inspect the `compiled.stream` iteration and exception handling in `consultants/server/runner.py`, determining whether a researcher-node exception is caught per event, converted to `status=failed`, or allowed to terminate the consultation immediately.
4. Trace the synthesizer node’s construction of its input messages in `consultants/engine/council.py`, verifying whether it receives only successfully merged `research` entries, an explicit failure marker, or no researcher output when one parallel lane fails.
5. Inspect the artifact-writing calls and exception boundaries in `consultants/engine/storage.py` and their invocation from `consultants/server/runner.py`, verifying whether a failed graph still writes a partial consultation artifact and what `status`/error fields that artifact contains.
6. Check relevant tests under `tests/` for parallel `Send` researcher failures, `compiled.stream` exceptions, degraded synthesis, and `status=failed`, then identify the single most localized hardening point and cite its exact `path:line`.

---

## Researcher (round 1)

## Sub-research lane 1 — findings

### 1. `researcher_node` catches BOTH named exception classes before the graph ever sees them

- `consultants/engine/council.py:547-554` — the *only* call into `agent_loop.runner.run_loop` sits inside a `try:`.
- `consultants/engine/council.py:555-573` — `except Exception as e:` converts the crash into a **tombstone state update**: `"error": f"researcher failed: {e}"` (`council.py:564`), `"_role_failed": "researcher"` (`council.py:565`), `"research": [err_text]` (`council.py:562,566`), `"research_rounds_used": 1` (`council.py:567`), plus a `RoleTurn` (`council.py:568-572`).
- Success path returns deltas only: `council.py:658-664` (`research`, `research_rounds_used`, `turns`, token counters).
- The exception sources are real:
  - HTTP timeout / retry-budget exhaustion: `ChatClient.chat` re-raises at `claude_hooks/get_advice/chat_client.py:285` (`RuntimeError`), `chat_client.py:302` (bare `raise` after `max_retries`), `chat_client.py:305` (`raise last_exc`). `run_loop` calls `chat_fn(payload)` **unguarded** at `claude_hooks/agent_loop/runner.py:230`, so the exception escapes `run_loop` → lands in `council.py:555`.
  - Non-string tool output: `claude_hooks/agent_loop/runner.py:122-127` does `len(output)` on the executor's return → `TypeError` for `None`. Caveat: the production executor cannot produce this — `claude_hooks/caliber_proxy/tools.py:439` coerces to `str`, and `consultants/engine/trace.py:218-233` (`traced_tool`) preserves that `str`. So the non-string variant is only reachable via a custom/stub executor; the timeout variant is the production-real one.
- Residual uncaught region: code *after* the `try` (`council.py:574-576` `_extract_text(final)` / `_usage_from(final)`) is **not** protected — a non-dict `final` would raise out of the node and out of the graph.

### 2. CouncilState reducers: additive where it matters, **LastValue (conflict-prone) on `error` / `_role_failed`**

- Additive (list concat / int sum): `consultants/engine/graph.py:64` (`research`), `:68` (`turns`), `:69` (`research_rounds_used`), `:70` (`critic_reroutes_used`), `:71-72` (token counters).
- **Plain `Optional[str]`, no reducer**: `consultants/engine/graph.py:74` (`error`), `:75` (`_role_failed`). In LangGraph a reducer-less channel is a LastValue channel and **rejects two writes in one superstep** (InvalidUpdateError).
- Send fan-out payload: `consultants/engine/graph.py:358-385` — each lane gets its own input, zeroes the additive channels (`graph.py:376-381`), and does **not** include `error`/`_role_failed`.
- Consequence (inference, high confidence; langgraph is **not installed** in this repo's test env — `tests/test_consultants_council.py:739-749` skips the graph test — so I could not execute it):
  - **1 lane fails** → only one writer to `error`/`_role_failed` → no conflict → surviving lanes merge, the graph continues, the synthesizer runs.
  - **≥2 lanes fail in the same superstep** → two writes to the reducer-less `error` channel → LangGraph raises → the whole superstep aborts. This is exactly the "tool_executor returns non-string" case (all lanes hit it) and the "upstream timeout hits all lanes" case.
- Side effect of the additive `research_rounds_used` (`graph.py:69`): 3 fan-out lanes each add +1 → `rounds_used == 3`, which trips the cap check in `council.py:383-389` (`caps.researcher_rounds_max` = 1 at medium, 3 at high, `council.py:68-74`) → the critic's re-route is effectively unreachable whenever fan-out fires (critic is also dropped at low/medium, `consultants/server/runner.py:58-63`).

### 3. `compiled.stream` loop: one blanket `try` around the whole iteration, no per-event isolation

- `consultants/server/runner.py:136-154` — a single `try:` wraps the entire `for mode, payload in compiled.stream(initial, stream_mode=["updates","values"])` loop. There is **no per-node/per-event try**.
- `consultants/server/runner.py:155-161` — any exception escaping the stream → `state.status = "failed"` (`runner.py:157`), `state.error = f"graph crashed: {e}"` (`runner.py:158`), then `_write_failed_artifacts(...)` (`runner.py:160`).
- `consultants/server/runner.py:211-233` — the crash-path artifact writer **discards `final_state`**: `final_answer=f"(consultation failed: {exc})"` (`runner.py:224`), `turns=[]` (`runner.py:225`), `models={}` (`runner.py:221`). So the planner's plan and the surviving lanes' merged `research` are lost from the artifacts.
- Happy path status derivation: `consultants/server/runner.py:171-174` — `terminal_status = "failed" if node_error else "completed"`, where `node_error = final_state.get("error")`. **Any** node tombstone ⇒ `status="failed"` even though the synthesizer produced a full answer.
- `consultants/server/runner.py:177-199` — the artifacts are still written on that path, with `status="failed"`, `error=node_error`, and the real `final_answer` + `turns` (`runner.py:186-187`).
- Outer safety net: `consultants/server/app.py:318-324` (`_run_with_state`) catches anything the runner lets escape → `status="failed"`, `error="runner crashed: ..."`; the sessions index is refreshed with the terminal status at `app.py:329-345`.
- The result endpoint serves the artifacts regardless of status: `consultants/server/app.py:246-279`; `claude-consultants show <sid>` reads them off disk with no service (`consultants/cli.py:162-180`).

### 4. What the synthesizer actually sees

- `consultants/engine/council.py:718-723` — the synthesizer node is fed `state.get("research")` and `state.get("critique")`; it is **not** fed `state["error"]`.
- `consultants/engine/council.py:253-254` — each merged list element is rendered as `"\nRESEARCHER REPORT (round {i}):\n{...}"`. The failed lane's tombstone therefore appears to the synthesizer as **one more numbered report** whose body is the literal string `(researcher lane failed: <exc>)` (`council.py:562`) — visually indistinguishable in form from a real report.
- Consequence: the synthesizer can read the gap, but nothing instructs it to flag it. `SYNTHESIZER_SYSTEM` (`council.py:156-168`) says only "Do NOT mention the council, the roles, or the process"; the generic preamble (`council.py:96-107`) asks for "explicit uncertainty where you do not". The code comment at `council.py:557-561` records the intent — the tombstone exists so the synthesizer does not "write a confidently-degraded answer over the surviving lanes" — but the tombstone is unstructured text in a report slot, not a machine-checkable gap.
- The critic sees the same merged list (including the tombstone) at `council.py:669-671`.
- No test asserts the *end-to-end* degraded-synthesis behavior: the only coverage is node-level — `tests/test_consultants_council.py:514-529` (error key) and `tests/test_consultants_council.py:531-561` (tombstone in `research`/`turns`/`research_rounds_used`). There is no test for two lanes failing, and no test for `compiled.stream` raising (`tests/test_consultants_server.py:284-297` only stubs a runner that sets `status="failed"` itself).

### 5. Storage artifact writer

- `consultants/engine/storage.py:200-208` — `write_consultation` writes `summary.md`, `transcript.md`, `metadata.json` atomically (`_atomic_write_text`, `storage.py:174-192`).
- `summary.md` = front-matter + `final_answer` (`storage.py:117-120`); front-matter carries `status` and `error` (`storage.py:108-110`) — so a degraded run is archived as `status: failed` **with the full answer in the body**.
- `transcript.md` iterates `turns` (`storage.py:129-136`); because 3 lanes each produce a `researcher` turn, `_is_multi_round` (`storage.py:161-163`) makes the headers `## Researcher (round N)` and the failed lane's tombstone appears as a researcher turn body.
- `metadata.json` = `asdict(result)` (`storage.py:166-169`) → `status: "failed"`, `error: "researcher failed: ..."`.
- Note: `storage.write_consultation` at `runner.py:199` is **not** wrapped in a try — a write failure there escapes to `app.py:318-324`.

## Answer to the question (evidence, not the final synthesis)

- **One lane fails** → the council *does* produce a coherent, complete answer: the tombstone is merged, the synthesizer runs on the surviving lanes plus a `(researcher lane failed: …)` line, and `summary.md` contains the real answer. But `runner.py:174` maps the non-empty `error` to `status="failed"`, so the session is archived as failed. The user-perceived failure mode: the skill's status gate (`.claude/skills/consultants/SKILL.md:88-93` "completed — move to Step 5", `:134-147` "If `status: failed` … surface the role + error") never fetches the result, so a **usable answer is produced but withheld**; the user sees "researcher failed: …" and is told to switch models, while the answer sits unread in `.claude-hooks/consultants/<sid>/summary.md` (recoverable only via `claude-consultants show <sid>`, `consultants/cli.py:162-180`). Secondary risk: the answer may silently omit the failed lane's sub-question, since the gap is only a prose line to the synthesizer.
- **Two or three lanes fail in the same superstep** → the reducer-less `error`/`_role_failed` channels (`graph.py:74-75`) collide, LangGraph aborts the superstep, the exception escapes `compiled.stream` into `runner.py:155-161`, and the consultation fails wholesale: `status="failed"`, `error="graph crashed: …"`, `final_answer="(consultation failed: …)"`, `turns=[]` (`runner.py:224-225`) — **all surviving lanes' evidence and the planner's plan are discarded** (`runner.py:211-233` ignores `final_state`). This is the "entire consultation fails" mode.

## Recommended single hardening

**Give `error` and `_role_failed` a merge reducer in `consultants/engine/graph.py:74-75`** (e.g. `error: Annotated[Optional[str], _last_non_empty]` / `_role_failed: Annotated[Optional[str], _last_non_empty]`).

- Why this one: it removes the only mechanism that turns a *localized* lane failure into a *global* abort, and it is a single-file, backward-compatible change — `council.py:564-565` and `runner.py:172-173` keep writing/reading plain strings, so no node, runner, storage, or prompt code changes.
- Effect: two or more failing lanes merge their `error` strings instead of raising InvalidUpdateError; the graph completes, the synthesizer produces a degraded-but-coherent answer, and the run degrades to the same single-lane case (answer written, `status="failed"`, visible gap in the transcript) instead of losing everything.
- Not recommended (mentioned only to mark scope): salvaging `final_state` in the crash path (`runner.py:160`, `runner.py:211-233`) would preserve evidence but would not stop the abort; and moving role failures onto a new additive-only channel (instead of `graph.py:74-75`) is the cleaner long-term design but touches `council.py` + `graph.py` + `runner.py`.
- If the synthesizer's handling of the gap also matters, the follow-up (not this recommendation) is to make the tombstone a structured marker in `research` (`council.py:562`) and instruct the synthesizer prompt (`council.py:156-168`) to state which sub-questions went unanswered.

**Evidence gaps to flag to the synthesizer:** langgraph is not installed in this environment (`tests/test_consultants_council.py:739-749`), so the InvalidUpdateError-on-concurrent-LastValue-write step is standard LangGraph semantics, not something I executed here. The `len(output)` TypeError at `claude_hooks/agent_loop/runner.py:126` is unreachable with the production executor (`claude_hooks/caliber_proxy/tools.py:439` coerces to `str`).

---

## Researcher (round 1)

## Findings — Send-API fan-out lane failure (researcher lane)

**1. Reducers / channels (`consultants/engine/graph.py`)**
- `research` and `turns` are the *only* lane-safe channels: `research: Annotated[list, operator.add]` (`consultants/engine/graph.py:64`), `turns: Annotated[list, operator.add]` (`consultants/engine/graph.py:68`); the file comment states these are additive "so parallel sub-researchers fanned out via Send merge cleanly" (`consultants/engine/graph.py:60-63`).
- `error` and `_role_failed` are **non-reducer** `Optional[str]` channels (`consultants/engine/graph.py:74`, `consultants/engine/graph.py:75`) — no `operator.add`, no last-value-wins-per-lane semantics. They are written from inside a *parallel* lane node, which is the hazard below.
- Fan-out injects per-lane state with `research: []`, `turns: []`, `research_rounds_used: 0` (`consultants/engine/graph.py:376-381`) so each lane's delta is additive-only.

**2. The exception is caught inside the node, not propagated**
- `researcher_node` wraps the whole `loop_runner(...)` call in `try/except Exception` (`consultants/engine/council.py:547-555`). So an HTTP timeout inside `run_loop` (raised at `claude_hooks/agent_loop/runner.py:230` via `chat_fn`) **or** a non-string tool result (TypeError at `claude_hooks/agent_loop/runner.py:126`, `len(output)` on a non-str) propagates out of `run_loop` and is swallowed by the node's except.
- The except branch returns a tombstone: `error`, `_role_failed: "researcher"`, `research: ["(researcher lane failed: {e})"]`, `research_rounds_used: 1`, and a `RoleTurn` (`consultants/engine/council.py:556-573`).
- Because the tombstone writes the **non-reducer** `error` channel from a parallel lane: a single failing lane writes it once (no conflict), but **two lanes failing in the same superstep both write `error` and `_role_failed`** (`consultants/engine/council.py:564-565`) — LangGraph's default last-value channels reject concurrent writes from the same superstep (`InvalidUpdateError`). That is external-library behavior; it is not verifiable in this repo, but the repo's own design comment (`consultants/engine/graph.py:60-63`) shows only the additive channels were made lane-safe. In that case the whole superstep aborts and the exception escapes `compiled.stream(...)` (`consultants/server/runner.py:137-138`) into the catch-all at `consultants/server/runner.py:155`.

**3. `compiled.stream` loop (`consultants/server/runner.py`)**
- Normal (single-lane-failure) path: the stream drains, `final_state` is the merged "values" payload (`consultants/server/runner.py:137-140`), the graph **continues to the synthesizer** and produces a real `final_answer`.
- But `node_error = final_state.get("error")` (`consultants/server/runner.py:172`) and `terminal_status = "failed" if node_error else "completed"` (`consultants/server/runner.py:174`) — so **one failed researcher lane flips the whole session to `status="failed"` even though a complete answer was produced and stored** (`consultants/server/runner.py:186-199`, `consultants/server/runner.py:201-202`).
- `_role_failed` is only logged as a warning, never surfaced (`consultants/server/runner.py:173`, `consultants/server/runner.py:204-206`).
- Graph-crash path (2+ lanes failing): `state.status = "failed"`, `state.error = "graph crashed: {e}"`, and `_write_failed_artifacts` writes a summary whose body is literally `"(consultation failed: {exc})"` with `status="failed"`, `turns=[]` (`consultants/server/runner.py:155-161`, `consultants/server/runner.py:211-237`).
- Progress reporting is also misleading: the first lane's "updates" chunk flips `progress["researcher"]="done"` and marks the *next* role `in_progress` while sibling lanes are still running (`consultants/server/runner.py:144-152`), and after the except-path return no role is reconciled (`consultants/server/runner.py:163-169` is skipped on the crash path).

**4. What the synthesizer actually sees**
- `synthesizer_node` builds messages from `state.get("research") or []` (`consultants/engine/council.py:718-723`); `build_synthesizer_messages` renders each entry as a `RESEARCHER REPORT (round N)` block (`consultants/engine/council.py:253-254`).
- Because the tombstone is in `research` (`consultants/engine/council.py:566`), the synthesizer receives the surviving lanes' real reports **plus one literal line `(researcher lane failed: <exc>)`**, with no indication of *which* plan item/lane was lost (the tombstone text does not carry `plan_item`/`lane_idx`; `consultants/engine/council.py:562`).
- The synthesizer prompt forbids mentioning roles/process (`consultants/engine/council.py:164-167`), so the failure marker is likely swallowed → a **confidently-degraded answer that silently omits one sub-question's evidence**.
- The critic sees the same tombstone among research rounds (`consultants/engine/council.py:669-671`).

**5. Artifact writer (`consultants/engine/storage.py`)**
- Normal path: `storage.write_consultation(result, cwd=Path(cwd))` (`consultants/server/runner.py:199`) writes `summary.md` / `transcript.md` / `metadata.json` atomically (`consultants/engine/storage.py:200-208`, `consultants/engine/storage.py:174-192`).
- `summary.md` front-matter carries `status: failed` **and** `error: "researcher failed: ..."` (`consultants/engine/storage.py:108-110`) with the real synthesizer answer as the body (`consultants/engine/storage.py:117-120`); `metadata.json` carries the same `status`/`error` fields via `asdict` (`consultants/engine/storage.py:166-169`); `transcript.md` shows the failed lane's tombstone turn (`consultants/engine/storage.py:129-136`, turn emitted at `consultants/engine/council.py:568-572`).
- The `/result` endpoint serves the artifacts regardless of status (`consultants/server/app.py:260-279`), and the sessions index records `status="failed"` (`consultants/server/app.py:341`).
- But the operator-facing skill only fetches the result on `status: completed` and treats `failed` as terminal breakage (`.claude/skills/consultants/SKILL.md:106`, `.claude/skills/consultants/SKILL.md:136-147`).

**6. Test coverage**
- Unit coverage of the tombstone exists: `tests/test_consultants_council.py:531-561` (asserts `research` tombstone + turn + `research_rounds_used`), `tests/test_consultants_council.py:514-529` (`error` string).
- **No test exercises `consultants/server/runner.py`** — every server test uses a stub runner (`tests/test_consultants_server.py:27-68`, `tests/test_consultants_server.py:284-297`); the `terminal_status` decision at `consultants/server/runner.py:172-174` is untested. No test covers a multi-lane simultaneous failure or a `Send` fan-out through a real compiled graph (`tests/test_consultants_council.py:739-754` only asserts the missing-langgraph error).

**Answer to the question**
- **One lane fails:** the council does *not* abort. The synthesizer runs and writes a coherent-but-partially-degraded answer; the artifact contains that answer. The session nonetheless reports `status="failed"` with `error="researcher failed: …"`, the user-visible progress shows "researcher done" (early, `consultants/server/runner.py:145-152`), and the skill's own flow never fetches the result (`.claude/skills/consultants/SKILL.md:106`) → **perceived failure with a good answer silently discarded**.
- **Two or more lanes fail in the same superstep:** both write the non-reducer `error`/`_role_failed` channels (`consultants/engine/graph.py:74-75`, `consultants/engine/council.py:564-565`); LangGraph aborts the superstep, the exception surfaces at `consultants/server/runner.py:155`, and the user gets **no answer at all** — `final_answer = "(consultation failed: …)"` (`consultants/server/runner.py:224`), `status="failed"`, empty `turns` (`consultants/server/runner.py:225`).

**Recommended single hardening change**
- Change `consultants/engine/council.py:564` so the researcher tombstone no longer writes the global non-reducer `error` channel; record the failure in a new **additive** per-lane channel declared next to `consultants/engine/graph.py:64` (e.g. `lane_errors: Annotated[list, operator.add]`), keeping `_role_failed` out of the parallel write path as well (`consultants/engine/council.py:565`).
  - Why this single point: it removes both failure modes at once — it eliminates the concurrent-write abort of the non-reducer channels (`consultants/engine/graph.py:74-75`, the only channels not made lane-safe per `consultants/engine/graph.py:60-63`) and it stops `terminal_status` from being flipped to `"failed"` by a single lane's error (`consultants/server/runner.py:172-174`), while the failure stays visible to the synthesizer through the existing `research` tombstone (`consultants/engine/council.py:566`) and in `transcript.md` via the existing turn (`consultants/engine/council.py:568-572`).
  - Not recommended in the same change: surfacing `lane_errors` in `SessionState.error` (a user-visible warning with `status="completed"`) — that is a second, separate edit.

---

## Critic (round 2)

DECISION: needs_more_research

- The load-bearing single-lane outcome is supported by the code: `researcher_node` converts the exception to a tombstone (`consultants/engine/council.py:547-573`), additive `research`/`turns` preserve sibling results (`consultants/engine/graph.py:60-72`), the synthesizer receives that merged list (`consultants/engine/council.py:718-723`), and the runner records a completed answer as `status="failed"` whenever `error` is present (`consultants/server/runner.py:171-199`). However, no executed real-LangGraph fan-out test verifies that one failed lane does not abort the superstep; the cited test is skipped when LangGraph is unavailable (`tests/test_consultants_council.py:739-743`).
- The concrete user-perceived failure is not yet fully established: the reports infer that a usable answer is “withheld” through a `.claude/skills/consultants/SKILL.md` flow, but that file was not found and the claim remains marked unverified. Check the actual API/session consumer and its status gate around `consultants/server/app.py:245-279,318-345` and `consultants/cli.py:162-180`, then run a one-failing-lane graph case to determine whether the synthesized answer is delivered or merely archived.
- The hardening recommendation should be singular: prefer a merge reducer for both non-reducer failure channels at `consultants/engine/graph.py:74-75`; the alternative “new additive channel” recommendation requires coordinated schema, node, and runner changes and is not one localized change.

---

## Researcher (round 3)

## Round 3 — verified gaps closed (consumer chain, single-lane outcome, one localized fix)

### 0. Correction to rounds 1–2: the consumer skill file **does** exist
- `.claude/skills/consultants/SKILL.md` exists (164 lines). Prior rounds' "file not found" was wrong; the file is now read directly.

### 1. The consumer's status gate (this is the user-perceived failure mode)
- `.claude/skills/consultants/SKILL.md:87-93` — poll states: `completed` → "Move to Step 5"; `failed` → "something broke; `error` carries the detail".
- `.claude/skills/consultants/SKILL.md:104-110` — Step 5 "fetch the result" is gated on `Once status: completed` → `claude-consultants result <sid>`. There is **no** instruction to fetch the result when status is `failed`.
- `.claude/skills/consultants/SKILL.md:134-147` — Step 6 (the `status: failed` branch) only says: surface the role + error, suggest `/consultants--config` to switch that role's model. It never reads the stored answer.
- `.claude/skills/consultants/SKILL.md:142-144` — the exact branch that fires for our case: "`error` mentions a specific role (e.g. \"researcher failed: ...\")" → surface role + error, switch the model.
- The answer is nonetheless reachable: `claude-consultants result <sid>` → `consultants/cli.py:148-151` → `GET /v1/consult/{sid}/result` → `consultants/server/app.py:246-279`, which **serves the artifacts regardless of status** (no status check at `app.py:246-279`; it only 409s while `state.status == "running"`, `app.py:266-270`). Local read path: `consultants/cli.py:162-180` (`cmd_show`) + `.claude/skills/consultants--show/SKILL.md:19-38`.

### 2. Single-lane outcome, now fully grounded
- The exception is caught inside the node: `consultants/engine/council.py:547-554` (the `loop_runner(...)` call) → `except Exception` at `council.py:555` → tombstone dict at `council.py:562-573` (`error` `council.py:564`, `_role_failed` `council.py:565`, `research` tombstone `council.py:566`, `research_rounds_used: 1` `council.py:567`, `RoleTurn` `council.py:568-572`).
- Additive channels preserve siblings: `consultants/engine/graph.py:64` (`research`), `:68` (`turns`), `:69-72` (int sums); the design comment states this intent at `consultants/engine/graph.py:60-63`.
- Synthesizer input = merged `research` only: `consultants/engine/council.py:718-723`; each entry rendered as an unlabeled "RESEARCHER REPORT (round N)" block at `council.py:253-254`. The tombstone text carries **no lane id** (`council.py:562`), and the synthesizer prompt forbids naming the process (`council.py:164-167` "Do NOT mention the council, the roles, or the process") → the gap is very likely dropped from the final answer.
- Runner maps any node `error` to a failed session: `consultants/server/runner.py:172-174` (`terminal_status = "failed" if node_error else "completed"`), then persists the **real** answer anyway: `runner.py:186-187` (`final_answer`, `turns`), `runner.py:189-190` (`status`, `error`), `runner.py:199` (`storage.write_consultation`), `runner.py:201-202` (`state.status`, `state.error`).
- Artifacts: front-matter `status` + `error` (`consultants/engine/storage.py:108-110`), body = `final_answer` (`storage.py:117-120`), `metadata.json` = `asdict(result)` (`storage.py:166-169`), transcript renders the tombstone turn (`storage.py:129-136`, `storage.py:161-163`), all written atomically (`storage.py:174-192`, `storage.py:200-208`).
- Sessions index records `status="failed"` (`consultants/server/app.py:331-345`, status at `app.py:341`).

**Conclusion (single lane):** the council *does* produce a coherent answer, writes it to `summary.md`, and the API will serve it — but the session is reported `failed`, and the skill's flow never fetches it. The user perceives *"the council failed: researcher failed: \<exc\>; go switch the researcher model"*, while a usable answer sits unread in `.claude-hooks/consultants/<sid>/summary.md` (recoverable only via `/consultants--show <sid>` or `claude-consultants result <sid>`). Secondary risk: the answer silently omits that lane's sub-question, because the tombstone is an unlabeled prose line the synthesizer is told not to mention.

### 3. Multi-lane simultaneous failure — inference, still unexecuted here
- Two failing lanes would both write the reducer-less channels `error` / `_role_failed` (`consultants/engine/graph.py:74-75`, no `Annotated[...]` reducer, unlike `:64-72`; comment at `graph.py:60-63` names only the additive channels as lane-safe) in the same superstep.
- LangGraph's default last-value channel rejects two writes in one step, so the superstep would abort and the exception would escape `compiled.stream` → caught at `consultants/server/runner.py:155-161` → `state.status="failed"`, `state.error="graph crashed: …"`, artifacts written by `_write_failed_artifacts` (`runner.py:211-237`) with `final_answer="(consultation failed: …)"` (`runner.py:224`), `turns=[]` (`runner.py:225`), `models={}` (`runner.py:221`) — i.e. the planner's plan and all surviving lanes' evidence are **discarded** (the crash path never uses `final_state`).
- **Evidence gap:** langgraph is not installed in this environment. It is declared only as a dependency of the consultants env (`consultants/pyproject.toml:17` `"langgraph>=0.2,<0.4"`) and the graph test is skipped when absent (`tests/test_consultants_council.py:739-749`). No `langgraph` source exists in the repo (glob `**/langgraph/graph/state.py` → no matches). I could not execute the concurrent-write case; treat the abort as standard-LangGraph semantics, not as an observed run.

### 4. New supporting findings
- `research_rounds_used` is additive, so 3 fan-out lanes yield `rounds_used == 3` (`graph.py:69`, each lane returns `1` at `council.py:567`/`:660`); the critic re-route is then blocked by the cap check `council.py:383-389` (`caps.researcher_rounds_max` = 1 at medium / 3 at high, `council.py:68-74`) — and at low/medium the critic node is dropped anyway (`consultants/server/runner.py:58-63`).
- Progress reporting is misleading on fan-out: the first lane's `"updates"` chunk flips `progress["researcher"]="done"` and starts the next role (`runner.py:144-152`) while sibling lanes are still running.
- No test exercises `consultants/server/runner.py` — every server test uses a stub runner (`tests/test_consultants_server.py:27-68`, `:284-297`); the `terminal_status` decision (`runner.py:172-174`) is untested. Node-level tombstone coverage exists only at `tests/test_consultants_council.py:514-529` and `:531-561`.

### 5. Recommended single hardening change
**Stop the parallel researcher lane from writing the global, reducer-less failure channels — i.e. in the researcher's except branch, write `error` / `_role_failed` only on the single-researcher path, and let the fan-out lane report only through the additive channels it already writes.**

Location: `consultants/engine/council.py:563-573` (the tombstone return), gated on the Send-injected fan-out marker `state.get("plan_item")` (set at `consultants/engine/graph.py:373`; the same discriminator is already used at `council.py:515-516`).

- Why this single point: it is the *only* place a parallel lane touches a non-reducer channel, so removing that write (a) eliminates the concurrent-write abort of `graph.py:74-75` (the "2+ lanes fail" wholesale-failure mode) and (b) stops `runner.py:174` from flipping a run that *did* produce an answer to `failed` — so the skill's `status: completed` gate (`SKILL.md:106`) fetches the answer and the user gets the degraded result instead of a bare "researcher failed" message.
- No other file needs to change: the tombstone stays visible in `research` (`council.py:566`) → synthesizer sees the gap (`council.py:718-723`, `:253-254`), and in `turns` (`council.py:568-572`) → `transcript.md` shows it (`storage.py:129-136`); the planner/critic/synthesizer failure semantics (`council.py:453-455`, `:684-686`, `:737-738`) are untouched.
- Trade-off to state explicitly: the fan-out failure then no longer appears in `state.error` / `metadata.json` `error` (`runner.py:190`, `:202`); the only machine-visible trace is the tombstone in `research`/`turns`. If that observability matters, the follow-up (deliberately out of scope here) is to derive a warning from the tombstone in `runner.py:172-174` — or, if the multi-lane abort is confirmed by running in the consultants env, to add a merge reducer on `graph.py:74-75` instead.

### 6. Residual evidence gaps for the synthesizer
- Concurrent last-value write on `error`/`_role_failed` was not executed (no langgraph in this env) — `consultants/pyproject.toml:17`, `tests/test_consultants_council.py:739-749`.
- The `len(output)` TypeError on a non-string tool result (`claude_hooks/agent_loop/runner.py:122-127`) is not reachable with the production executor, which coerces to `str` (`claude_hooks/caliber_proxy/tools.py:439`) and the traced wrapper preserves that (`consultants/engine/trace.py:211-233`); the production-real trigger is the chat-client raise after the retry budget is exhausted (`claude_hooks/get_advice/chat_client.py:285-287`, `:302`, `:305`) surfacing through the unguarded `chat_fn(payload)` call at `claude_hooks/agent_loop/runner.py:230`.

---

## Critic (round 3)

DECISION: ready

The evidence is sufficient for the requested single-lane path. The exception is caught by `researcher_node` and converted into a tombstone update containing `error`, `_role_failed`, an additive `research` entry, and a `RoleTurn` (`consultants/engine/council.py:547-573`). Additive reducers preserve sibling research, turns, counts, and token totals (`consultants/engine/graph.py:60-72`), while the non-reducer error channels receive only the one lane’s write (`consultants/engine/graph.py:74-75`). The synthesizer receives the merged research list—including the literal tombstone `(researcher lane failed: …)`—and renders it as another researcher-report block (`consultants/engine/council.py:562-566`, `consultants/engine/council.py:718-723`, `consultants/engine/council.py:253-254`). Thus the graph can synthesize and persist a degraded answer, but the runner subsequently converts the node-level error into `status="failed"` (`consultants/server/runner.py:171-190`) and stores the real answer in `summary.md`, transcript, and metadata (`consultants/server/runner.py:186-199`, `consultants/engine/storage.py:108-120`, `consultants/engine/storage.py:129-136`, `consultants/engine/storage.py:166-169`, `consultants/engine/storage.py:200-208`). The perceived failure is therefore “researcher failed” despite an answer existing on disk, with the consumer reportedly withholding that answer because its result-fetch step is gated on `completed` (`.claude/skills/consultants/SKILL.md:87-93`, `.claude/skills/consultants/SKILL.md:104-110`, `.claude/skills/consultants/SKILL.md:134-147`).

UNVERIFIED: simultaneous failures in two or more lanes causing a reducerless-channel `InvalidUpdateError` — requires executing the compiled LangGraph graph with at least two failing Send branches; the installed-environment test is skipped at `tests/test_consultants_council.py:739-749`.

---

## Synthesizer

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

---
