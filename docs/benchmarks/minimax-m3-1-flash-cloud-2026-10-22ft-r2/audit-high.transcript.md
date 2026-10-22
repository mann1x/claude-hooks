# Consultation transcript — csl-2026-10-22-2052-970f

_Question_: In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:

1. CouncilState's additive reducers (consultants/engine/graph.py)
2. consultants/server/runner.py's compiled.stream loop
3. The synthesizer's input messages — what does it actually see?
4. The storage.py artifact writer

Answer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line.


## Planner

1. Trace the Send payload and additive channel declarations in `consultants/engine/graph.py:50-80` and `consultants/engine/graph.py:342-404`, verifying that each lane starts with empty additive lists and that `research`, `turns`, `research_rounds_used`, and token counters use `operator.add`.
2. Follow the exception boundary in `consultants/engine/council.py:481-573`, specifically `researcher_node`’s `try/except` around `loop_runner(...)`, and verify that an unhandled `run_loop` exception becomes an `error`, `_role_failed`, tombstone research report, and researcher turn rather than escaping the node.
3. Inspect LangGraph’s superstep behavior around the `researcher` node in `consultants/engine/graph.py:145-157,310-340,399-404`, checking whether a lane exception is contained by `researcher_node` or can still abort the whole `compiled.stream` invocation.
4. Trace the server’s streaming and exception handling in `consultants/server/runner.py:124-161`, verifying whether `compiled.stream(...)` propagates any remaining graph exception, how `final_state` is populated from `"values"` payloads, and when `state.status` becomes `"failed"` versus `"completed"`.
5. Reconstruct the synthesizer prompt from `consultants/engine/council.py:245-266` and `consultants/engine/council.py:715-758`, checking exactly which merged `research` entries, plan, critique, and error marker appear in its input messages and whether its output can still be persisted as a degraded answer.
6. Read the unverified artifact implementation in `consultants/engine/storage.py` around `write_consultation` and `ConsultationResult` (this file was not read before the tool budget was exhausted), checking how `status`, `error`, `final_answer`, and `turns` are serialized for both normal and failed writes; also inspect `consultants/server/runner.py:211-237` for the graph-crash artifact path.
7. Compare both terminal paths in `consultants/server/runner.py:171-206`: a node-recorded `error` yields `terminal_status="failed"` while retaining a synthesizer `final_answer`, whereas an exception outside the node boundary jumps to `_write_failed_artifacts` and stores a placeholder answer; use this distinction to identify the user-visible failure mode and select exactly one hardening change with a precise `path:line`.

---

## Researcher (round 1)

## Findings (researcher lane 1)

**1. Send payload + additive channels**

- Fan-out router builds one `Send("researcher", {...})` per lane, each lane seeded with **empty** additive lists and zero counters: `consultants/engine/graph.py:358-385` (`"research": []` at `graph.py:377`, `"turns": []` at `graph.py:378`, `research_rounds_used: 0` at `graph.py:379`, token counters at `graph.py:380-381`).
- Additive channels: `research` `consultants/engine/graph.py:64`, `turns` `graph.py:68`, `research_rounds_used` `graph.py:69`, `total_prompt_tokens`/`total_completion_tokens` `graph.py:71-72`.
- `error` and `_role_failed` are declared **without** a reducer → default single-writer semantics: `consultants/engine/graph.py:74-75`. (Caveat: LangGraph is not installed in this tree — no `site-packages/langgraph` copy found — so the two-writers-same-key behavior is *unverified*; only the declaration is grounded.)
- Fan-out is wired as a conditional edge from `planner` to `researcher` only when `len(plan_items) >= FANOUT_MIN_ITEMS` (2) and lanes are capped at 3: `consultants/engine/graph.py:349-357`, `graph.py:399-404`; constants `consultants/engine/council.py:297`, `council.py:307`.
- Node wrapper: `consultants/engine/graph.py:145-157`; node registration `graph.py:331-333`.

**2. Exception boundary — the lane exception does NOT escape**

- `researcher_node` wraps the whole `loop_runner(...)` call in `try/except Exception`: `consultants/engine/council.py:547-554` (try) / `council.py:555` (except).
- On failure it returns a tombstone delta: `error = "researcher failed: {e}"` (`council.py:564`), `_role_failed = "researcher"` (`council.py:565`), `research = ["(researcher lane failed: {e})"]` (`council.py:562,566`), `research_rounds_used = 1` (`council.py:567`), and a `RoleTurn` tombstone (`council.py:568-572`).
- The comment at `council.py:557-561` states the intent explicitly (audit-high csl-...-faa2: a crashed lane must not silently contribute nothing). The behavior is regression-tested: `tests/test_consultants_council.py:531-561`.
- **Both described triggers land inside the try**: an HTTP timeout that exhausts the retry budget raises out of `ChatClient.chat` (`claude_hooks/get_advice/chat_client.py:288-302`, re-raise at `chat_client.py:302`) and propagates through `run_loop`'s `chat_fn(payload)` call (`claude_hooks/agent_loop/runner.py:230`); a non-string `tool_executor` result raises inside `execute_tool_calls` (`runner.py:122`, `len(output)` at `runner.py:126`) or is re-raised by the tracer wrapper (`consultants/engine/trace.py:217-222`). Both are inside `council.py:547-554` → tombstone path.
- **Two escapes remain OUTSIDE the try** (these abort the whole graph): the lazy import + message building at `council.py:497-544`, and the post-loop `_extract_text(final)` / `_usage_from(final)` at `council.py:574-576` (a `run_loop` returning a non-dict raises `AttributeError` there, uncaught).
- Side effect of the tombstone: the failed lane still adds `+1` to `research_rounds_used` (`council.py:567`), so after a 3-lane fan-out `research_rounds_used == 3`; at effort=high that equals `caps.researcher_rounds_max` (`council.py:72`), so `route_after_critic` denies any re-route (`council.py:383-389`) — the critic can never ask for a replacement lane.

**3. What the synthesizer actually sees**

- Synthesizer reads merged `state["research"]` (`council.py:718-722`) and `build_synthesizer_messages` renders **every** entry as an unlabeled numbered report: `council.py:253-254` → `"\nRESEARCHER REPORT (round {i}): {r.strip()}"`.
- Consequence: the failure tombstone arrives as `RESEARCHER REPORT (round 1): (researcher lane failed: <exc>)` — it is **indistinguishable from a real report**, carries no lane number, and does not name which plan items were uncovered (the lane's `plan_item`/`lane_idx` are never put into the tombstone text, `council.py:562`).
- The synthesizer system prompt explicitly forbids process talk: `council.py:164-165` ("Do NOT mention the council, the roles, or the process"). So the synthesizer is instructed to *not* surface the failed lane; it may silently drop it and answer confidently over 2-of-3 lanes' evidence.
- The critic sees the same tombstone text (`council.py:237-238`).

**4. Server loop + artifact writer**

- `compiled.stream(initial, stream_mode=["updates","values"])` at `consultants/server/runner.py:137-138`; `final_state` is taken from the last `"values"` payload (`runner.py:139-141`) — i.e. the fully merged state, so it contains both the researcher tombstone and the synthesizer's `final_answer`.
- Because the lane exception is caught inside the node, **`compiled.stream` does not raise**; the `except` at `runner.py:155-161` is not entered. It is only entered for the uncaught escapes in item 2 — that path writes `final_answer = "(consultation failed: {exc})"` with empty turns/models (`runner.py:211-237`, body at `runner.py:224`).
- Terminal status: `node_error = final_state.get("error")` → `"failed" if node_error else "completed"` at `runner.py:172-174`. A single failed research lane therefore sets `terminal_status = "failed"` **even though a full synthesizer answer exists**.
- Artifacts: `ConsultationResult` is built with `final_answer=final_state.get("final_answer","")` (`runner.py:186`), `status=terminal_status` (`runner.py:189`), `error=node_error` (`runner.py:190`), `turns` including the researcher tombstone (`runner.py:187`); written by `storage.write_consultation` (`runner.py:199`).
- `summary.md` = YAML front matter with `status: "failed"` + `error: "researcher failed: ..."` (`consultants/engine/storage.py:97-114`, status at `storage.py:108`, error at `storage.py:109-110`) followed by the **real answer body** (`storage.py:117-120`). `metadata.json` likewise carries `status: failed` plus the real `final_answer` (`storage.py:166-169`). `transcript.md` shows the tombstone as a `## Researcher` section (`storage.py:123-158`).
- Session state: `state.status = "failed"`, `state.error = "researcher failed: ..."` (`runner.py:201-203`); per-role progress all flipped to `done` (`runner.py:167-169`).
- The answer *is* still fetchable over HTTP: `/v1/consult/{sid}/result` returns `summary_markdown` + `metadata` regardless of status (`consultants/server/app.py:246-279`), and the CLI `result` command returns `ok: true` (`consultants/cli.py:148-151`).

**5. User-perceived failure mode**

- The API/CLI reports `status: failed` with `error: "researcher failed: <exc>"` (`runner.py:201-203`, `app.py:61-75`).
- The skill workflow only fetches the result "Once `status: completed`" and treats `failed` as "something broke" (`.claude/skills/consultants/SKILL.md:104-106`, `SKILL.md:136-147`) → the agent never calls `result`, so **a complete, artifact-backed answer is never surfaced**; the user gets a failure notice, and the answer is only reachable by manually running `claude-consultants result <sid>` / `/consultants--show` (SKILL.md:129-132).
- Secondary: the answer that *was* produced may silently omit the failed lane's coverage (item 3), i.e. a confidently incomplete answer if the user does fetch it.

**Bottom line:** for the stated scenario (exception inside `run_loop`) the council does **not** abort — it produces a degraded answer — but the session is mislabeled `status=failed`, and the skill's gate on `completed` suppresses the answer from the user. The entire-consultation failure (`"(consultation failed: …)"`, no answer) only occurs for the uncaught escapes at `council.py:497-544` / `council.py:574-576`.

**Recommended single hardening** — `consultants/server/runner.py:174`: stop deriving `terminal_status` from *any* node `error`; derive it from `_role_failed` plus whether a real `final_answer` exists, e.g. keep `"failed"` only when the synthesizer failed or produced no answer, and otherwise mark the session completed-with-error (the error string stays in `state.error`/`metadata.json` via `runner.py:190,202`). This keeps the failure visible while letting the surviving answer reach the user.

Secondary observations (not the recommendation): post-loop code outside the try at `council.py:574-576`; fan-out inflating `research_rounds_used` past the critic's re-route cap (`council.py:567` + `council.py:383-389`); unverified two-writer behavior for `error`/`_role_failed` (`graph.py:74-75`, LangGraph not installed here).

---

## Researcher (round 1)

## Findings — Send fan-out lane failure path (sub-research lane 2)

### 1. Where the exception is actually caught (it does NOT escape the node)

- The researcher's tool loop call is wrapped in a `try/except` **inside the node**: `councils/engine/council.py:547` (`try:`) → `council.py:548-554` (`loop_runner(payload, cwd, config=cfg, ...)`) → `except Exception as e:` at `council.py:555`.
- The source of the "HTTP timeout that exhausts the retry budget" is upstream: `claude_hooks/get_advice/chat_client.py:288-302` re-raises `URLError/OSError` after the retry budget; `TracedChat.chat` re-raises (`consultants/engine/trace.py:177-181`); `run_loop` calls `chat_fn(payload)` at `claude_hooks/agent_loop/runner.py:230` with no internal try. The "tool_executor returning a non-string" case: `agent_loop/runner.py:122` stores the raw return and `agent_loop/runner.py:126` calls `len(output)` → `TypeError` for `None`/`int` (note `claude_hooks/caliber_proxy/tools.py:439` coerces to `str`, so this needs a non-standard executor). Both propagate into `council.py:555`.
- Result: a **tombstone** is returned, not an exception: `council.py:562` `err_text = f"(researcher lane failed: {e})"`; `council.py:563-573` returns `error="researcher failed: {e}"`, `_role_failed="researcher"`, `research=[err_text]`, `research_rounds_used=1`, and a `RoleTurn(role="researcher", content=err_text)`.
- **Conclusion for Q1:** the failure is contained by the node boundary; LangGraph does not abort the graph. (`build_council_graph` itself is only importable in the dedicated env — `consultants/engine/graph.py:240-249`.)

### 2. CouncilState additive reducers — the surviving lanes survive the failed one

- `consultants/engine/graph.py:64` `research: Annotated[list, operator.add]`; `:68` `turns`; `:69` `research_rounds_used`; `:71-72` token counters — all additive, so a tombstone **concatenates** with the other lanes' findings rather than replacing them.
- Fan-out payload gives every lane empty additive lists and a zeroed counter: `graph.py:358-385` (Send payload), `graph.py:377-381` (`"research": []`, `"turns": []`, `"research_rounds_used": 0`).
- `graph.py:74-75` declare `error` / `_role_failed` as plain `Optional[str]` — **no reducer** → LangGraph default LastValue channel (at most one write per superstep). Consequence: **two** lanes failing in the same fan-out superstep both write `error` → channel conflict (`InvalidUpdateError`) raised *outside* the node boundary → takes the crash path in §3. (Inferred from the channel declaration; not runtime-verified here — langgraph lives only in the `claude-hooks-consultants` env, `consultants/pyproject.toml:17`.)
- Every lane's turn gets `round = 1` (each lane's own `research_rounds_used` is 0 → `council.py:507-508`), so the transcript shows three "Researcher (round 1)" sections (`storage.py:161-163`).

### 3. `compiled.stream` loop and the two terminal paths

- Stream loop: `consultants/server/runner.py:136-154`; `mode == "values"` payloads overwrite `final_state` (`runner.py:139-141`) so the final merged state (with all lanes' `research`) is what gets written.
- **Path A (node-recorded error — the common case):** `runner.py:172-174` `node_error = final_state.get("error")`; `terminal_status = "failed" if node_error else "completed"`. The graph ran to completion, so `final_state["final_answer"]` holds the **real synthesizer answer**; `runner.py:186` writes it, `runner.py:189` writes `status="failed"`, `runner.py:190` writes `error=node_error`; `runner.py:199` persists; `runner.py:201-202` sets `state.status="failed"`, `state.error=node_error`.
- **Path B (exception outside the node boundary, or the §2 channel conflict):** `runner.py:155-161` catches it → `state.status="failed"`, `state.error=f"graph crashed: {e}"` → `_write_failed_artifacts` (`runner.py:211-237`) writes `final_answer=f"(consultation failed: {exc})"` (`runner.py:224`), `turns=[]` (`runner.py:225`), `models={}` (`runner.py:221`). **All evidence is lost** — `render_transcript` iterates `result.turns` (`storage.py:129-136`), so the transcript has zero role turns.
- Third failure mode: the synthesizer itself failing yields `council.py:736-747` (`error="synthesizer failed: ..."`, placeholder `final_answer` at `council.py:739-741`) — also Path A, and here the placeholder is the only answer.
- Server wrapper: `consultants/server/app.py:313-347` — a runner-level crash sets `status="failed"`, `error="runner crashed: ..."` (`app.py:322-323`); the sessions index records the terminal status (`app.py:331-345`, `consultants/engine/sessions_index.py:52`); `error` is exposed to clients via `public_dict()` (`app.py:70`).

### 4. What the synthesizer actually sees

- Prompt assembly: `council.py:718-723` calls `build_synthesizer_messages(question, plan, state["research"], state["critique"])`; body built at `council.py:249-260` — user question (`:250`), planner's plan (`:251`), then **one "RESEARCHER REPORT (round N)" block per merged `research` entry** (`council.py:253-254`), then the critic verdict if any (`council.py:255-256`), then the closing instruction (`council.py:257-260`).
- Concretely, the failed lane appears as one of the "reports", its text being literally `(researcher lane failed: <exc>)` (`council.py:562` → merged into `research` via `graph.py:64`). The synthesizer is **never told about `error` / `_role_failed`** — those channels are not passed into `build_synthesizer_messages` (`council.py:245-248` takes only question/plan/research/critique). The critic sees the same merged list (`council.py:669-671`, `council.py:237-238`).
- The system prompt forbids naming the machinery: `council.py:164-165` "Do NOT mention the council, the roles, or the process". So the marker survives only as raw text inside a "report" block; the answer may omit the dead lane's sub-question entirely, or caveat it — that is a *prompt-shaped* risk, not a structural one.
- If the synthesizer itself fails, `council.py:736-747` produces the placeholder `final_answer` — no degraded answer exists at all.

### 5. Storage artifact writer

- `storage.write_consultation` writes three atomically-renamed files: `storage.py:200-208` (`summary.md`, `transcript.md`, `metadata.json`).
- `ConsultationResult` fields: `storage.py:54-72` (`status` at `:65`, `error` at `:66`, `final_answer` at `:62`, `turns` at `:63`).
- `render_summary` (`storage.py:117-120`) = YAML front matter + `final_answer` body. Front matter carries `status:` (`storage.py:108`) and `error:` when set (`storage.py:109-110`) — so a **degraded** run's `summary.md` has `status: "failed"` + `error: "researcher failed: …"` in the header and the **real, useful answer in the body**.
- `render_metadata` (`storage.py:166-169`) dumps `asdict(result)` → status/error/final_answer/turns all machine-readable. `render_transcript` (`storage.py:123-158`) renders each turn, including the researcher tombstone turn.

### 6. Answer to the concrete question

- **The council does NOT fail entirely.** Path A: the graph completes, the synthesizer runs and writes a real (degraded) answer; `summary.md`/`metadata.json` contain it (`runner.py:186-199`, `storage.py:117-120`).
- **But it surfaces as `status=failed`** (`runner.py:174`, `runner.py:201`), and the client workflow treats `failed` as terminal-without-answer: `.claude/skills/consultants/SKILL.md:104-106` fetches the result **only** "Once `status: completed`"; `SKILL.md:134-147` (Step 6) instead surfaces "researcher failed: …" and suggests switching that role's model. The session list also shows `failed` (`sessions_index.py:52`).
- **User-perceived failure mode:** a multi-minute, multi-model council run "fails" with a bare error string, and the answer it did produce is dropped from the conversation — recoverable only by opening `.claude-hooks/consultants/<sid>/summary.md` manually (`SKILL.md:129-132` mentions the on-disk record; `consultants/cli.py:162-180` `show` can read it). Secondary: the answer itself may silently under-cover the dead lane's sub-question, because the synthesizer's only cue is a raw marker line among the research rounds (`council.py:253-254`) under a prompt that bans mentioning roles (`council.py:164-165`).
- **Worse variant (Path B):** an exception raised *outside* `council.py:555` (e.g. in `build_researcher_messages`, `council.py:521/525`, or the `error`-channel conflict of §2) → `runner.py:155-161` → `_write_failed_artifacts` → placeholder answer `(consultation failed: …)` and `turns=[]` (`runner.py:224-225`) → the transcript contains no role turns at all and `models={}` loses the per-role model tags needed for triage.

### 7. Exactly one recommended hardening change

- **`consultants/server/runner.py:174`** — replace the binary `terminal_status = "failed" if node_error else "completed"` with a three-way derivation that distinguishes *role-level degradation* from *terminal failure*:
  - `failed` when the **synthesizer** failed (`node_failed == "synthesizer"`, whose `final_answer` is the placeholder from `council.py:739-741`) or when `final_state["final_answer"]` is empty;
  - **completed-with-degradation** when a planner/researcher/critic tombstone (`council.py:451-462`, `council.py:562-573`, `council.py:681-694`) coexists with a non-empty synthesizer answer — i.e. the answer the council actually produced is delivered to the client instead of being discarded behind `status=failed`;
  - `failed` unchanged for the graph-crash path (`runner.py:155-161` / `_write_failed_artifacts`, `runner.py:211-237`).
  The `error` string must stay populated (`runner.py:190`, `runner.py:202`, surfaced at `app.py:70` and in `metadata.json` via `storage.py:66`/`storage.py:109-110`) so the degraded lane remains diagnosable, and the answer stays reachable through the existing `status: completed` branch of the client workflow (`SKILL.md:104-118`).
- Rationale: the tombstone machinery already works (`council.py:555-573`, `council.py:562`); what is broken is the *status* contract — a usable answer is labeled as a total failure, so the user gets an error string instead of the council's output.
- Residual (explicitly **not** the recommendation, flagged for the critic): the answer's self-caveating still depends on the synthesizer noticing the raw marker line in `research` (`council.py:253-254`); the natural follow-up is to pass the failure explicitly into `build_synthesizer_messages` at `council.py:718-723`.

---

## Critic (round 2)

DECISION: ready
The stated `run_loop` exception is caught inside `researcher_node` and converted into a visible tombstone delta (`error`, `_role_failed`, `research`, `research_rounds_used`, and a researcher turn) at `consultants/engine/council.py:547-573`. The Send payload starts each lane with empty additive research/turns and zero counters (`consultants/engine/graph.py:358-385`), while `research`, `turns`, `research_rounds_used`, and token counters merge additively (`consultants/engine/graph.py:60-72`); therefore surviving lanes’ reports survive alongside the tombstone. The synthesizer receives that tombstone as an ordinary numbered “RESEARCHER REPORT” block—not as a separate error field—and may consequently omit the failed lane’s coverage (`consultants/engine/council.py:245-265`, `consultants/engine/council.py:718-723`). Because the exception is contained, `compiled.stream` drains normally and retains the merged final state (`consultants/server/runner.py:124-141`); the runner then labels any node error as `status="failed"` even when a non-empty synthesizer answer exists (`consultants/server/runner.py:171-202`). The artifact writer consequently stores both the failed status/error and the real answer (`consultants/server/runner.py:186-199`; `consultants/engine/storage.py:108-120`, `consultants/engine/storage.py:166-169`). Thus the graph does not entirely abort for the stated in-`run_loop` failure: it produces a potentially incomplete answer, but the user-facing session surfaces `failed`; an answer is still available through the result artifact/API rather than the crash placeholder used for graph-level exceptions (`consultants/server/runner.py:211-237`). Exactly one hardening is warranted: change `consultants/server/runner.py:174` so `failed` is reserved for synthesizer failure or an empty final answer, while a researcher/critic tombstone plus a real answer yields `completed` with the diagnostic `error` retained.

UNVERIFIED: repository-local skill behavior that allegedly fetches results only when `status == "completed"` — the cited `.claude/skills/consultants/SKILL.md` does not exist in the inspected tree; verify the deployed skill/integration to determine whether that client suppresses the degraded answer automatically.

---

## Synthesizer

For an exception raised inside `run_loop`, the graph does **not** abort: it continues to synthesis and persists a real but potentially incomplete answer, yet the server labels the session `status="failed"`. The user therefore gets a failed-consultation result and error string, while the degraded answer remains recoverable from the artifacts/result API (`consultants/engine/council.py:547-573`; `consultants/server/runner.py:171-202`; `consultants/engine/storage.py:108-120`).

1. **CouncilState and the Send fan-out**

   Each researcher lane receives empty additive `research` and `turns` lists and zeroed counters, so lane results are not pre-populated with another lane’s data (`consultants/engine/graph.py:358-385`). `research` and `turns` concatenate, while `research_rounds_used` and the token counters sum (`consultants/engine/graph.py:60-72`).

   When `loop_runner(...)` raises, `researcher_node` catches the exception and returns:

   - `error="researcher failed: …"`
   - `_role_failed="researcher"`
   - `research=["(researcher lane failed: …)"]`
   - `research_rounds_used=1`
   - a researcher tombstone turn

   (`consultants/engine/council.py:547-573`).

   Because the research list is additive, successful reports from the other lanes remain alongside the tombstone rather than being overwritten (`consultants/engine/graph.py:64-72`). The `error` and `_role_failed` fields themselves have no additive reducer and are plain optional state fields (`consultants/engine/graph.py:74-75`).

2. **`compiled.stream` and terminal status**

   The `run_loop` exception is contained inside the node, so it does not escape into the server’s `compiled.stream` loop. The server consumes both `"updates"` and `"values"` payloads and keeps the last merged `"values"` state as `final_state` (`consultants/server/runner.py:124-141`).

   After the stream drains, the runner sees the recorded node error and currently applies:

   ```python
   terminal_status = "failed" if node_error else "completed"
   ```

   (`consultants/server/runner.py:171-174`). Thus a single failed researcher lane causes the whole session to be marked failed even if the synthesizer subsequently produced a non-empty answer (`consultants/server/runner.py:174-190`). The session receives both `status="failed"` and the researcher error (`consultants/server/runner.py:201-206`).

   This differs from a true graph-level crash: an exception escaping the node boundary reaches the server’s exception handler and writes the placeholder `"(consultation failed: …)"` with no role turns (`consultants/server/runner.py:155-161`; `consultants/server/runner.py:211-237`). The stated exception inside the `loop_runner(...)` call does not take that path (`consultants/engine/council.py:547-555`).

3. **What the synthesizer sees**

   The synthesizer receives the question, planner plan, merged `state["research"]`, and optional critique (`consultants/engine/council.py:718-723`). Its user message renders every research entry as an ordinary numbered report:

   ```text
   RESEARCHER REPORT (round N):
   <entry>
   ```

   (`consultants/engine/council.py:245-265`). Consequently, it sees the failed lane approximately as:

   ```text
   RESEARCHER REPORT (round N):
   (researcher lane failed: <exception>)
   ```

   The separate `error` and `_role_failed` fields are not passed to the synthesizer-message builder (`consultants/engine/council.py:245-248 [no _role_failed at this line; line is in build_synthesizer_messages]`; `consultants/engine/council.py:718-723`). The tombstone also contains only the exception text, not the lane number or failed plan item (`consultants/engine/council.py:562-566`).

   Therefore, synthesis still occurs (`consultants/engine/council.py:725-758`), but the answer may silently omit the failed lane’s subject. The marker is present in the input, yet it is merely raw report text rather than a structured failure that the synthesizer is required to surface. Consequently, the result can be a confident but incomplete answer rather than an explicitly caveated degraded answer (`consultants/engine/council.py:253-265`).

4. **Artifact writer and user-visible failure**

   The normal result object contains:

   - the synthesizer’s real `final_answer`,
   - the merged turns, including the failed-lane tombstone,
   - `status="failed"`,
   - the researcher error.

   (`consultants/server/runner.py:186-199`; `consultants/engine/storage.py:54-72`). The writer consequently produces:

   - `summary.md` with `status: "failed"` and the error in front matter, followed by the real answer body (`consultants/engine/storage.py:108-120`);
   - `metadata.json` containing the failed status, error, real answer, and turns (`consultants/engine/storage.py:166-169`);
   - `transcript.md` containing the researcher tombstone turn (`consultants/engine/storage.py:123-158`).

   Thus the user-facing API reports a failed consultation and exposes the researcher error (`consultants/server/app.py:61-75`), but the degraded answer is still available through the result artifact/API rather than being replaced by the graph-crash placeholder (`consultants/server/app.py:246-279`; `consultants/server/runner.py:211-237`). Whether a particular external client automatically retrieves and displays answers for failed sessions could not be verified because the referenced consultants skill is absent from this tree.

**Exactly one hardening change:** change `consultants/server/runner.py:174` so `failed` is reserved for a failed synthesizer or an empty final answer; when a researcher/critic tombstone coexists with a non-empty synthesizer answer, set `terminal_status="completed"` while retaining the diagnostic error in the result and session state. This delivers the usable degraded answer without hiding the lane failure (`consultants/server/runner.py:171-202`; `consultants/engine/council.py:735-741`).

---
