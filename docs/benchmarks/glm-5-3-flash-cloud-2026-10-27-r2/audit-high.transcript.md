# Consultation transcript — csl-2026-10-27-1155-5d08

_Question_: In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:

1. CouncilState's additive reducers (consultants/engine/graph.py)
2. consultants/server/runner.py's compiled.stream loop
3. The synthesizer's input messages — what does it actually see?
4. The storage.py artifact writer

Answer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line.


## Planner

I need to write the plan now. I've verified: consultants/engine/graph.py exists (429 lines, read fully), consultants/engine/council.py exists, consultants/engine/storage.py exists, consultants/server/runner.py exists, consultants/server/app.py exists, claude_hooks/agent_loop/runner.py exists.

I confirmed in graph.py:
- CouncilState reducers at graph.py:64 (research: Annotated[list, operator.add]), :68 (turns), :69-72 int-sum reducers
- Fan-out Send at graph.py:349-386
- No error key handling visible in graph — `error` and `_role_failed` fields declared at :74-75 but I didn't see any node setting them in graph.py (they're set presumably in council.py nodes).
- Synthesizer node wrapper at :171-180.

Unchecked: council.py node implementations (researcher_node error handling, synthesizer input construction), server/runner.py stream loop, storage.py artifact writer, claude_hooks/agent_loop/runner.py retry budget and non-string tool result handling.

Plan steps, concrete, 3-7:

1. Read consultants/engine/council.py: researcher_node and synthesizer_node — verify whether researcher_node catches exceptions from chat/tool execution or sets `error`/`_role_failed` (declared CouncilState keys graph.py:74-75), and what synthesizer puts into its input messages from state["research"] (empty-list vs error-string handling).
2. Read consultants/server/runner.py: the compiled.stream/invoke loop — how node exceptions propagate, whether there's try/except around graph execution, what sets status=failed, and what partial state gets persisted.
3. Read claude_hooks/agent_loop/runner.py run_loop: confirm retry budget exhaustion raises (which exception type) and what happens when tool_executor returns non-string (where the type check/assert lives).
4. Read consultants/engine/storage.py artifact writer: what fields are written on failure vs success (final_answer, error, status), and whether a partial consultation is persisted at all.
5. Check consultants/server/app.py for the API endpoint that surfaces status to the caller (failed vs completed payload shape).
6. Verify LangGraph Send semantics assumption: whether an exception in one Send lane aborts the whole superstep (check installed langgraph version behavior in the consultants env or tests tests/test_agent_loop_runner.py / any consultants graph tests under tests/ for fan-out failure coverage).
7. Trace synthesizer input construction concretely: confirm what a lane failure leaves in state["research"] (missing entry vs placeholder) by reading council.join_lane_items / group_items_into_lanes and the researcher return dict keys.

That's 7 steps. Keep concise, cite verified lines where I have them.</think>1. Read `consultants/engine/council.py` — `researcher_node` and `synthesizer_node`: verify whether `researcher_node` wraps its chat/tool calls in try/except or lets exceptions escape, whether it ever sets the declared-but-unverified state keys `error`/`_role_failed` (`consultants/engine/graph.py:74-75`), and exactly how `synthesizer_node` builds its input messages from `state["research"]` (empty-list case, per-lane dict shape).
2. Read `consultants/server/runner.py` — the `compiled.stream`/`invoke` loop: locate the try/except (if any) around graph execution, what sets `status=failed`, whether partial state is captured on exception, and how `retries_by_role`/token totals are read out.
3. Read `claude_hooks/agent_loop/runner.py` `run_loop`: confirm what exception type escapes when the HTTP retry budget is exhausted, and where (if anywhere) a non-string `tool_executor` return is rejected — note this file was listed in the survey (`claude_hooks/agent_loop/runner.py`) but not yet read; also check `tests/test_agent_loop_runner.py` for existing failure-path coverage.
4. Read `consultants/engine/storage.py` — the artifact writer: which fields are persisted on success vs failure (`final_answer`, `error`, `status`), and whether an in-flight consultation that dies mid-stream is written at all or left absent from `consultancy.json`.
5. Read `consultants/server/app.py` — the HTTP endpoint returning consultation status to the dispatcher: confirm the failed-vs-degraded payload shape the user actually surfaces.
6. Verify the LangGraph Send failure semantics assumption — that one lane's unhandled exception aborts the whole fan-out superstep rather than being isolated: check the installed langgraph version in the `claude-hooks-consultants` env and grep `tests/` for any fan-out/lane-failure test (none seen in the survey; if absent, state that as unverified rather than asserted).
7. Read `council.group_items_into_lanes` / `join_lane_items` / `FANOUT_MIN_ITEMS` / `FANOUT_MAX_LANES` in `consultants/engine/council.py` alongside the Send payload at `consultants/engine/graph.py:349-386` to determine what a dead lane leaves in the `research` channel (`Annotated[list, operator.add]`, `graph.py:64`) — i.e. whether the synthesizer sees fewer lane entries, a partial list, or nothing.

---

## Researcher (round 1)

## Findings

**1. CouncilState reducers (graph.py)**
- `research: Annotated[list, operator.add]` and `turns` additive at `consultants/engine/graph.py:64,68`; int-sum reducers for rounds/tokens at `graph.py:69-72`. `error` / `_role_failed` are plain non-additive keys at `graph.py:74-75` (last-writer-wins if two lanes fail).
- Send fan-out: `consultants/engine/graph.py:349-386` — each lane gets `"research": []`, `"turns": []`, zeroed counters (`graph.py:377-381`) so lane deltas merge additively with no double-count.

**2. researcher_node catches the exception — the graph never crashes (the load-bearing fact)**
- `consultants/engine/council.py:547-555`: `run_loop` is wrapped in `try/except Exception`. On failure it returns a **tombstone**: `error: "researcher failed: {e}"`, `_role_failed: "researcher"`, and — critically — `"research": [f"(researcher lane failed: {e})"]` (`council.py:563-573`). The comment at `council.py:557-561` states this exists precisely so a crashed lane doesn't silently vanish.
- So a lane exception does **not** propagate to LangGraph; the lane "completes" with an error string as its research contribution.

**3. What raises out of run_loop**
- Retry budget exhaustion: `claude_hooks/get_advice/chat_client.py:270-287` — non-retryable or exhausted budget raises `RuntimeError(f"ollama chat HTTP {e.code}: ...")` (`chat_client.py:285-287`); URLError/OSError (timeout) after budget re-raises at `chat_client.py:302`. Both are `Exception` subclasses → caught by `council.py:555`.
- Non-string tool result: `ToolExecutor = Callable[..., str]` (`claude_hooks/agent_loop/runner.py:70`); `execute_tool_calls` assigns the raw return and calls `len(output)` at `claude_hooks/agent_loop/runner.py:122-126` — an int return raises `TypeError` there, propagates out of `run_loop`, caught identically at `council.py:555`. (No type-check/assert exists in the runner; there is no defensive coercion.)

**4. Synthesizer input — what it actually sees**
- `synthesizer_node` (`council.py:715-723`) calls `build_synthesizer_messages(question, plan, state.get("research") or [], state.get("critique"))` (`council.py:245-266`). The failed lane's tombstone string `(researcher lane failed: ...)` lands in the prompt as `RESEARCHER REPORT (round N)` (`council.py:253-254`). **The synthesizer never reads `state["error"]` or `_role_failed`** — it just sees a degraded report list and writes an answer over the surviving lanes' real findings.

**5. runner.py stream loop**
- `compiled.stream(initial, stream_mode=["updates","values"])` at `consultants/server/runner.py:137-138`; `values` payloads become `final_state` (`runner.py:139-140`). A graph-level crash is caught at `runner.py:155-161` → `status="failed"`, `_write_failed_artifacts` with `final_answer="(consultation failed: {e})"`, `turns=[]` (`runner.py:211-237`).
- Post-stream, node-recorded errors are detected at `runner.py:172-174`: `terminal_status = "failed" if node_error else "completed"`. **Any single lane's tombstone flips the whole consultation to `status="failed"`** even though the synthesizer ran and produced a real `final_answer` (`runner.py:186`), which is persisted normally (`runner.py:177-199`).

**6. storage.py artifact writer**
- Success and lane-failure paths use the same `write_consultation` (`consultants/engine/storage.py:200-208`): atomic writes of `summary.md` / `transcript.md` / `metadata.json`. Front-matter carries `status` + `error` (`storage.py:108-110`); the body is `final_answer` verbatim (`storage.py:119`). A degraded consultation is fully persisted — coherent answer body, but `status: "failed"` in front-matter and metadata.

**7. API surface**
- Poll returns `status`/`error` in `public_dict` (`consultants/server/app.py:61-75`); result returns `summary_markdown` + `metadata` (`app.py:275-279`). The CLI consumer sees a full summary body *and* `status: "failed"` in metadata — contradictory signals.

## Verdict

The council **does not fail**. One crashed lane → tombstone in `research` → synthesizer still runs over surviving lanes → coherent (degraded) answer written to disk. **But** `runner.py:174` marks the whole session `status="failed"` because any node-set `error` string triggers it — so the user perceives a *failed* consultation whose summary.md nonetheless contains a real answer. Perceived failure mode: poll shows `status=failed` + `error="researcher failed: ..."`, while `/result` returns a perfectly readable summary; downstream consumers keying on `status` (sessions index at `app.py:331-345`) record it as failed.

## One hardening change

`consultants/server/runner.py:174` — classify lane-level failures as degraded-completed, not failed:

```python
terminal_status = (
    "failed" if (node_error and not (final_state.get("final_answer") or "").strip())
    else "completed"
)
```

A researcher/critic/planner tombstone still leaves a synthesizer-produced `final_answer` → `completed` with `error` retained for the transcript; only a synthesizer failure (which writes the placeholder `(consultation incomplete: ...)` at `council.py:739-741`, no real answer) or a graph crash (empty answer) stays `failed`.

---

## Researcher (round 1)

## Findings

**Exception containment — the lane never raises into LangGraph**

- `researcher_node` wraps the entire `run_loop` call in try/except: `consultants/engine/council.py:547-556`. Any exception escaping `run_loop` — including `RuntimeError("ollama chat HTTP ...")` raised when the retry budget exhausts (`claude_hooks/get_advice/chat_client.py:285-287`) or the re-raised `URLError`/`OSError` (`chat_client.py:301-305`) — is caught and converted to a **tombstone return** (`council.py:558-573`): `{"error": "researcher failed: ...", "_role_failed": "researcher", "research": ["(researcher lane failed: ...)"], "research_rounds_used": 1, "turns": [RoleTurn(...)]}`.
- Non-string `tool_executor` return: `run_loop`'s `execute_tool_calls` calls the executor (`claude_hooks/agent_loop/runner.py:122`) then does `len(output)` at `runner.py:126` — a dict return raises `TypeError` there, which propagates out of `run_loop` and lands in the same `except` at `council.py:555`. Same tombstone. (In production this can't happen anyway: `tools.execute` never raises and coerces to `str` — `claude_hooks/caliber_proxy/tools.py:420-439`, esp. `:439 return result if isinstance(result, str) else str(result)`.)

**1. CouncilState reducers**

- `research` and `turns` are `Annotated[list, operator.add]`; token/round counters are `Annotated[int, operator.add]` — `consultants/engine/graph.py:64-72`. The tombstone's `research: [err_text]` therefore **concatenates** into the merged state; surviving lanes' findings are preserved alongside it.
- ⚠️ But `error` and `_role_failed` (`graph.py:74-75`) have **no reducer**. If **two lanes fail in the same fan-out superstep**, both Send branches return `error` concurrently — LangGraph raises `InvalidUpdateError` for concurrent writes to a reducer-less channel, which aborts the whole superstep. Unverified against the installed langgraph version (no test covers concurrent lane failure; grep over `tests/` for `InvalidUpdateError|concurrent.*fail` returned nothing) — flagging as likely, not proven.

**2. runner.py stream loop**

- `compiled.stream(..., stream_mode=["updates","values"])` at `consultants/server/runner.py:136-138`; `final_state` tracks the last `values` payload (`:139-140`).
- Graph-level crash path: `except Exception` at `runner.py:155-161` → `state.status="failed"`, `state.error=f"graph crashed: {e}"`, `_write_failed_artifacts` writes placeholder `final_answer=f"(consultation failed: {exc})"` (`runner.py:224`).
- Normal path: `node_error = final_state.get("error")`; **`terminal_status = "failed" if node_error else "completed"`** (`runner.py:172-174`). So a *single* lane failure still sets `status="failed"` even though the synthesizer ran and produced an answer.

**3. Synthesizer input**

- `synthesizer_node` reads `state.get("research") or []` (`council.py:720`) and `build_synthesizer_messages` renders each entry verbatim as `RESEARCHER REPORT (round i)` (`council.py:253-254`). The synthesizer literally sees the string `"(researcher lane failed: HTTP timeout ...)"` as one of its reports, interleaved with surviving lanes' real findings. It is not told which entries are tombstones — it must infer it from the text. Empty-lane case is separately handled (forced summary call, `council.py:585-639`).

**4. storage.py artifact writer**

- `ConsultationResult` is written with `final_answer=final_state.get("final_answer","")`, `status=terminal_status`, `error=node_error` (`runner.py:177-199`) → `storage.write_consultation` atomically writes `summary.md`/`transcript.md`/`metadata.json` (`consultants/engine/storage.py:200-208`). The degraded answer **is persisted**, with `status: failed` + `error:` in the YAML front-matter (`storage.py:108-110`). The tombstone turn also appears in `transcript.md` (`storage.py:129-136`). Graph-crash case gets its own placeholder artifacts (`runner.py:211-237`).

**5. User-visible surface**

- Status poll returns `state.status` + `error` + per-role progress (`consultants/server/app.py:63-71`); result endpoint 409s while running, 404s if artifacts missing (`app.py:266-273`). So the caller sees **status=failed** and must read `summary.md` to discover a usable answer exists.

**Bottom line**

- **One lane fails:** council completes; synthesizer produces a degraded-but-coherent answer over surviving lanes (tombstone text visible in its input). But `runner.py:174` flips the whole session to `status="failed"` — the user perceives a hard failure despite a written, mostly-correct `summary.md`. This is the dominant failure mode, and it's test-anchored (`tests/test_consultants_council.py:531-561` asserts the tombstone reaches `research`).
- **Two+ lanes fail simultaneously:** likely `InvalidUpdateError` on the reducer-less `error` channel → whole graph aborts → `status=failed`, answer replaced by `"(consultation failed: ...)"` (`runner.py:224`). Unverified (no langgraph-version test evidence).
- **Exception outside the try** (e.g. missing `state["question"]` at `council.py:521` before the try block): escapes the node → graph crash path → full failure.

**ONE hardening change**

`consultants/server/runner.py:174` — replace `terminal_status = "failed" if node_error else "completed"` with lane-aware degradation: when `final_state.get("_role_failed") == "researcher"` and `final_state.get("final_answer","").strip()` is non-empty, set `terminal_status = "completed"` (keeping `state.error`/`node_error` populated and logged at `runner.py:202-206`) so a degraded-but-real answer isn't reported as a total failure; reserve `status="failed"` for synthesizer/planner failures and graph crashes. This directly fixes the single-lane case; the two-lane `InvalidUpdateError` case would be a second (additive-reducer on `error`) change, out of scope for one.

**Evidence gaps:** installed langgraph version's concurrent-write behavior not verified (no test, didn't inspect the env's package); `bin/claude-consultants` CLI's rendering of `status=failed` not read.

---

## Critic (round 2)

I need to write my verdict now. Tools are off. Let me assess what I verified and what remains unchecked.

Verified by me directly:
1. `consultants/engine/council.py:547-573` — researcher_node wraps `loop_runner(...)` in `try/except Exception` (line 547 `try:`, 555 `except Exception as e:`), and returns a tombstone dict with `error`, `_role_failed`, `research: [err_text]`, `turns: [RoleTurn]`, `research_rounds_used: 1`. The comment at 557-561 confirms intent: tombstone so additive reducers record failure visibly. VERIFIED — matches both reports.
2. `consultants/engine/council.py:245-266` — `build_synthesizer_messages` renders each research entry verbatim as `RESEARCHER REPORT (round {i})` at 253-254. VERIFIED.
3. `consultants/engine/council.py:715-723` — synthesizer_node builds msgs from `state.get("research") or []`. VERIFIED. And 725-747: synthesizer failure → placeholder `final_answer` `(consultation incomplete: synthesizer error: ...)` — matches report 2's `council.py:739-741` claim (actual lines 739-741 contain the placeholder text). Verified.
4. `consultants/server/runner.py:135-161` — stream loop, `except Exception` at 155 → status failed, `_write_failed_artifacts` at 160. VERIFIED. `runner.py:172-174` — `node_error = final_state.get("error")`, `terminal_status = "failed" if node_error else "completed"`. VERIFIED at exactly line 174. `runner.py:186` final_answer from final_state; `runner.py:199` `storage.write_consultation`. VERIFIED.
5. `consultants/engine/graph.py:64-75` — reducers exactly as claimed: research/turns Annotated[list, operator.add] at 64/68, int sums at 69-72, `error`/`_role_failed` plain at 74-75. VERIFIED.
6. `consultants/engine/graph.py:349-386` — Send fan-out with empty research/turns/zeroed counters at 377-381. VERIFIED.
7. `claude_hooks/get_advice/chat_client.py:264-302` — retryable status → retry with backoff; exhausted → `raise RuntimeError(f"ollama chat HTTP {e.code}: ...")` at 285-287; URLError/OSError exhausted → bare `raise` at 302. VERIFIED — both are Exception subclasses, caught by council.py:555.
8. `claude_hooks/agent_loop/runner.py:122-126` — `output = tool_executor(...)`, then `len(output)` in the log call at 126. An int/dict return raises TypeError at `len(output)` (line 126). VERIFIED — no type check/coercion in execute_tool_calls. `ToolExecutor = Callable[..., str]` at runner.py:70 verified only via the provided comment block (lines 69-70 shown in the evidence header) — that matches.
9. `consultants/engine/storage.py:108-110` — status + error in YAML front-matter; `render_summary` at 117-120 body = final_answer. VERIFIED. `write_consultation` at 200-208 — verified via docstring header provided; the report claims atomic writes of three files at 200-208; I saw the docstring at 200-203 confirming summary.md/transcript.md/metadata.json. Good enough.
10. Tests: grep showed `tests/test_consultants_council.py:531` `test_loop_failure_emits_tombstone_research` with assertions at 552-556 including "researcher lane failed" and "HTTP timeout" in update["research"][0]. VERIFIED that the tombstone path is test-anchored.

Not verified (unchecked because tools went off):
- `consultants/server/app.py` status/result endpoints (reports cite app.py:61-75, 266-279, 331-345) — I did not read app.py. Both reports agree on the shape (poll returns status+error; result returns summary_markdown+metadata), but I cannot confirm line numbers. This is a load-bearing-ish claim for "what the user perceives" but both lanes independently reported consistent shapes; still, per strictness I should mark it UNVERIFIED rather than assert.
- LangGraph concurrent-write behavior for reducer-less `error` channel when two lanes fail in the same superstep — report 2 flags this as unverified/likely (`InvalidUpdateError`). I could not check (glob for langgraph/errors.py failed — tools off). This remains UNVERIFIED. Note: this matters for the "two lanes fail simultaneously" sub-case. However, the question asks about ONE lane raising — the dominant, verified path. I should list it as UNVERIFIED with what would check it (inspect installed langgraph's channel update logic / write a two-failing-lanes test).
- Report 2's claim that `tools.execute` coerces to str at `claude_hooks/caliber_proxy/tools.py:439` — the evidence header shows `tools.py:420-424` docstring "Never raises — errors are returned as `error: ...` strings" which supports the "never raises" part; the isinstance-str coercion at 439 I did not see. Partially supported. But the question's premise (tool_executor returning non-string) is handled at runner.py:126 regardless — verified.
- Report 2's claim that `state["question"]` access at council.py:521 is before the try block — I read 495-625: line 521-522 `build_researcher_messages(state["question"], ...)` is indeed OUTSIDE the try (try starts at 547). Wait — 521 is before 547, so yes, outside the try. Verified from my own read: the try begins at line 547, and `state["question"]` is accessed at 521-526, before it. So a missing "question" key would raise before the try → escapes node → graph crash path. That's consistent with report 2's claim (they cited council.py:521). Verified by my own read.

Discrepancies/corrections:
- Report 1 cited `council.py:563-573` for the tombstone return — actual return dict spans 563-573. Correct.
- Report 1 cited `council.py:555` for except — correct (line 555).
- Report 1 cited `council.py:739-741` for synthesizer placeholder — actual placeholder at 739-741. Correct.
- Report 2 cited `council.py:547-556` for try/except — actual try at 547, except at 555. Correct.
- Report 2 cited `chat_client.py:301-305` for URLError re-raise — actual bare `raise` at 302. Within range, fine.
- Report 1 cited `runner.py:155-161` — actual except at 155, block through 161. Correct.
- Report 1 cited `runner.py:172-174` — correct.
- Report 1 cited storage.py:200-208 for write_consultation — docstring at 200-203 confirms; the atomic-write detail at 204-208 I didn't read but the docstring says "Idempotent: re-writing the same result overwrites in place". Close enough; not a correction.
- Report 1 cited `council.py:253-254` for RESEARCHER REPORT rendering — verified exactly.
- Report 2's `graph.py:74-75` for error/_role_failed — verified.
- Report 1 cited `runner.py:186` for final_answer — verified (line 186 `final_answer=final_state.get("final_answer", "")`).
- Report 2 cited `runner.py:224` for placeholder `(consultation failed: ...)` — verified at line 224.

No corrections needed. Both reports agree on all load-bearing facts, and I independently verified the core chain.

The verdict: DECISION: ready. The evidence chain is verified end-to-end for the single-lane failure case:
- lane exception → caught at council.py:555 → tombstone into additive `research` channel (graph.py:64) → synthesizer sees tombstone string as a RESEARCHER REPORT (council.py:253-254, 720) → synthesizer produces degraded answer → runner.py:174 flips status to "failed" despite real final_answer → storage writes summary.md with status: failed front-matter (storage.py:108-110) + real body (storage.py:119).
- Verdict: council produces a coherent degraded answer but surfaces as status=failed — both reports converge on this, and I verified every link.

UNVERIFIED items:
1. Two-lanes-fail-simultaneously → InvalidUpdateError on reducer-less `error` channel (report 2) — unverified against installed langgraph; would check by inspecting the env's langgraph channel implementation or writing a two-failing-lane test. This is an edge case beyond the question's "one of the parallel researcher lanes" premise, so not blocking.
2. app.py endpoint shapes (both reports cite app.py:61-75, 266-279) — I did not read app.py; both lanes independently reported the same shape, but mark UNVERIFIED with pointer.
3. tools.py:439 isinstance-coercion (report 2) — partially supported by the 420-424 docstring; not blocking since runner.py:126 handles the non-string case regardless.

Hardening change: both reports independently recommend the same one-line change at runner.py:174 (degrade-to-completed when a real final_answer exists). Convergent recommendation — good for the synthesizer.

Confidence: high. Core chain fully verified by my own reads; test anchor verified (test_consultants_council.py:531-556). Remaining unverified items are secondary (concurrent-lane edge case, API surface line numbers). I'd say 0.85.

Format:
- Line 1: DECISION: ready
- Paragraph explaining why
- UNVERIFIED items listed (required since I answer ready while noting unverified claims)
- CORRECTIONS: omit (none)
- Final line: CONFIDENCE: 0.85DECISION: ready

The load-bearing chain is verified end-to-end by my own reads, and both reports agree on every link. Lane exception containment: `researcher_node` wraps `loop_runner(...)` in `try/except Exception` (`consultants/engine/council.py:547-555`) and returns a tombstone — `error`, `_role_failed`, `research: ["(researcher lane failed: ...)"]`, a `RoleTurn` — with the comment at `council.py:557-561` stating this is deliberate (the audit-high csl-...-faa2 fix), so it is intended behaviour, not a defect. What raises: retry-budget exhaustion → `RuntimeError` at `claude_hooks/get_advice/chat_client.py:285-287` or bare re-raise of `URLError`/`OSError` at `:302`; non-string tool result → `TypeError` at `len(output)` in `claude_hooks/agent_loop/runner.py:126` (no coercion/check exists in `execute_tool_calls`; `ToolExecutor = Callable[..., str]` at `:70` is unenforced) — all `Exception` subclasses, all caught at `council.py:555`. Reducer merge: tombstone `research` list concatenates via `Annotated[list, operator.add]` (`consultants/engine/graph.py:64`); `error`/`_role_failed` are reducer-less last-writer-wins keys (`graph.py:74-75`); Send lanes start with empty/zeroed additive fields (`graph.py:377-381`). Synthesizer input: `state.get("research") or []` (`council.py:720`) rendered verbatim as `RESEARCHER REPORT (round i)` (`council.py:253-254`) — the synthesizer sees the tombstone string interleaved with surviving lanes' findings and never reads `error`/`_role_failed`. Surface: `terminal_status = "failed" if node_error else "completed"` at `consultants/server/runner.py:174` — one lane's tombstone flips the whole session to `failed` even though a real `final_answer` is persisted (`runner.py:186,199`); storage writes `status:`/`error:` front-matter (`consultants/engine/storage.py:108-110`) with the real answer as body (`storage.py:119`). Verdict for the synthesizer: the council produces a coherent degraded answer, but the user perceives `status=failed` with a readable `summary.md` — a contradictory signal, exactly as both reports concluded. The tombstone path is test-anchored: `tests/test_consultants_council.py:531-556` (`test_loop_failure_emits_tombstone_research`) asserts `"researcher lane failed"` and `"HTTP timeout"` land in `update["research"][0]`. Both reports independently converge on the same one hardening change: `consultants/server/runner.py:174` — classify a lane failure with a non-empty synthesizer-produced `final_answer` as `completed` (retaining `error`), reserving `failed` for synthesizer failure (`council.py:735-741` placeholder) or graph crash (`runner.py:155-161`, placeholder at `:224`). No corrections to either report.

UNVERIFIED: two lanes failing in the same superstep → claimed `InvalidUpdateError` on the reducer-less `error` channel (report 2) — not checked against the installed langgraph; would check by inspecting the env's langgraph channel code or a two-failing-lanes test. Outside the question's single-lane premise; the recommended fix at `runner.py:174` is unaffected.
UNVERIFIED: app.py endpoint shapes (`status`/`error` in poll, `summary_markdown`+`metadata` in result; reports cite `consultants/server/app.py:61-75, 266-279, 331-345`) — I did not read app.py before tools went off; both lanes independently reported the same shape, but the synthesizer should treat those line numbers as report-sourced.
UNVERIFIED: report 2's `tools.execute` str-coercion at `claude_hooks/caliber_proxy/tools.py:439` — only the `:420-424` docstring ("Never raises — errors are returned as `error: ...` strings") was confirmed; immaterial to the verdict since `runner.py:126` handles non-string returns regardless.

---

## Synthesizer

**Bottom line: the consultation does NOT crash — it completes with a degraded-but-coherent answer — but it is surfaced as `status=failed` anyway.** The user polling the API sees a hard failure with `error="researcher failed: ..."`, while `/result` returns a fully readable `summary.md` containing a real answer synthesized from the surviving lanes. The failure is contained inside the researcher node before it ever reaches LangGraph.

## 1. CouncilState's additive reducers

- The lane exception never escapes the node. `researcher_node` wraps the entire `run_loop` call in `try/except Exception` (`consultants/engine/council.py:547-555 [no run_loop at this line; line is in researcher_node]`) and returns a **tombstone** instead of raising: `{"error": "researcher failed: {e}", "_role_failed": "researcher", "research": ["(researcher lane failed: {e})"], "research_rounds_used": 1, "turns": [RoleTurn(...)]}` (`council.py:563-573`). The comment at `council.py:557-561` states this is deliberate — a crashed lane must not silently vanish.
- The tombstone's `research` list **concatenates** into the merged state via the additive reducer `research: Annotated[list, operator.add]` (`consultants/engine/graph.py:64`); `turns` likewise (`graph.py:68`), token/round counters sum (`graph.py:69-72`). Surviving lanes' findings are preserved alongside the tombstone.
- `error` and `_role_failed` (`graph.py:74-75`) are plain last-writer-wins keys with no reducer — fine for one failing lane; two simultaneous lane failures would contend on these (see Unverified).
- What actually raises out of `run_loop`: retry-budget exhaustion → `RuntimeError(f"ollama chat HTTP ...")` (`claude_hooks/get_advice/chat_client.py:285-287 [no run_loop at this line; line is in chat]`) or a bare re-raise of `URLError`/`OSError` after the budget (`chat_client.py:302`); a non-string tool result → `TypeError` at `len(output)` in `execute_tool_calls` (`claude_hooks/agent_loop/runner.py:122-126` — no type check or coercion exists; `ToolExecutor = Callable[..., str]` at `runner.py:70` is unenforced). All are `Exception` subclasses → all caught at `council.py:555`.

## 2. runner.py's compiled.stream loop

- `compiled.stream(initial, stream_mode=["updates","values"])` at `consultants/server/runner.py:137-138`; the last `values` payload becomes `final_state` (`runner.py:139-140`).
- A graph-level crash (exception escaping a node) is caught at `runner.py:155-161` → `status="failed"` and `_write_failed_artifacts` with placeholder `final_answer="(consultation failed: {e})"` (`runner.py:224`). **This path is not taken in the single-lane case** because the tombstone contains the exception.
- Normal path: `node_error = final_state.get("error")` and `terminal_status = "failed" if node_error else "completed"` (`runner.py:172-174`). The tombstone's `error` string is present → **the whole session is marked `failed` even though the synthesizer ran and produced a real answer** (`final_answer` read from `final_state` at `runner.py:186`, persisted at `runner.py:199`).

## 3. What the synthesizer actually sees

- `synthesizer_node` builds its messages from `state.get("research") or []` (`council.py:720`) via `build_synthesizer_messages` (`council.py:245-266`), which renders each entry verbatim as `RESEARCHER REPORT (round N)` (`council.py:253-254`).
- So the synthesizer literally sees the string `"(researcher lane failed: HTTP timeout ...)"` as one of its reports, interleaved with the surviving lanes' real findings. It is **never told** which entries are tombstones — it must infer degradation from the text — and it never reads `state["error"]` or `_role_failed`. It still writes an answer over the surviving lanes' evidence.

## 4. storage.py artifact writer

- The degraded consultation is **fully persisted** via the same success path (`runner.py:177-199` → `storage.write_consultation`, `consultants/engine/storage.py:200-208`): atomic `summary.md` / `transcript.md` / `metadata.json`. YAML front-matter carries `status: failed` + `error:` (`storage.py:108-110`); the body is the real `final_answer` verbatim (`storage.py:119`). The tombstone turn also appears in `transcript.md` (`storage.py:129-136`).

## Perceived failure mode

The caller polling status sees `status=failed` with `error="researcher failed: ..."`; the result endpoint returns a coherent, mostly-correct `summary_markdown` plus metadata that says `failed` (endpoint shapes: `consultants/server/app.py:61-75 [no failed at this line; line is in public_dict], 266-279` — report-sourced, see Unverified). Downstream consumers keying on `status` record a usable consultation as a total failure — a contradictory signal, not a crash.

## One hardening change

`consultants/server/runner.py:174` — replace the unconditional `terminal_status = "failed" if node_error else "completed"` with lane-aware degradation: when a lane-level tombstone exists (`final_state.get("_role_failed")`) and the synthesizer produced a non-empty real `final_answer`, set `terminal_status = "completed"` while retaining `error` (logged at `runner.py:202-206`); reserve `status="failed"` for a synthesizer failure (placeholder `"(consultation incomplete: ...)"`, `council.py:739-741`) or a graph crash (`runner.py:155-161, 224`), where no real answer exists. Both research lanes independently converged on this exact change.

## Unverified

- **Two lanes failing in the same superstep**: likely `InvalidUpdateError` from concurrent writes to the reducer-less `error` channel (`graph.py:74-75`) → graph crash path → true `failed`. Not checked against the installed langgraph version; no test covers it. Outside the question's single-lane premise.
- **app.py endpoint shapes/line numbers** — flagged by the critic as report-sourced; both lanes agreed on the shape but the exact lines were not independently read.
- **`tools.execute` str-coercion** (`claude_hooks/caliber_proxy/tools.py:439`) — only the "never raises" docstring (`tools.py:420-424`) was confirmed; immaterial, since `runner.py:126` rejects non-string returns regardless.

The single-lane path is test-anchored: `tests/test_consultants_council.py:531-556` (`test_loop_failure_emits_tombstone_research`) asserts `"researcher lane failed"` and `"HTTP timeout"` land in `update["research"][0]`.

---
