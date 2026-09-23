# Consultation transcript — csl-2026-09-23-1824-ef5e

_Question_: In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:

1. CouncilState's additive reducers (consultants/engine/graph.py)
2. consultants/server/runner.py's compiled.stream loop
3. The synthesizer's input messages — what does it actually see?
4. The storage.py artifact writer

Answer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line.


## Planner

1. Read the `CouncilState` definition in consultants/engine/graph.py — enumerate every field and its reducer (e.g. `Annotated[list, operator.add]` or custom functions), noting which fields researcher lanes append to vs. overwrite, and cite exact line numbers.

2. Find the `Send(...)` fan-out site in consultants/engine/graph.py (conditional-edge or router node) — identify the target lane node name, and check whether that node wraps its `agent_loop.runner.run_loop` call in any try/except or returns partial results on failure.

3. Read consultants/server/runner.py — map the `compiled.stream(...)` consumption loop: where node exceptions would propagate (inside the `for update in stream` iteration), whether there's a surrounding try/except that sets `status="failed"`, and whether any `astream`/checkpointing config affects exception surfacing.

4. Read the lane node body in consultants/engine/graph.py plus the `run_loop` call path (agent_loop/runner.py) — identify the exact raise sites for retry-budget exhaustion (HTTP timeout) and non-string `tool_executor` returns, and confirm whether the exception escapes before the lane returns a dict.

5. Read the synthesizer node in consultants/engine/graph.py — determine which state fields it reads to build input messages (e.g. `research_messages` or `messages`), and reason concretely about what it sees when one parallel Send branch raised: does LangGraph commit partial updates from successful sibling branches before the failing branch aborts the graph, or is nothing committed? Check langgraph's version in pyproject/requirements to pin `Send`/parallel-node error semantics.

6. Read consultants/engine/storage.py — locate the artifact-writer function, find all call sites (graph node vs. runner.py post-stream), and verify whether it executes on the failure path, and how it handles missing/None state fields.

7. Consolidate: decide between "whole consultation fails → status=failed" vs. "degraded synthesizer answer with missing lane content"; identify the single highest-leverage hardening point (candidate: per-lane try/except in the lane node in graph.py converting exceptions into a sentinel message that flows through the additive reducer to the synthesizer), and prepare path:line citations for the researcher's final answer.

---

## Researcher (round 1)

Findings, all verified this turn:

**1. CouncilState reducers (`consultants/engine/graph.py`)**
- Additive: `research: Annotated[list, operator.add]` (graph.py:64), `turns` (graph.py:68), `research_rounds_used`/`critic_reroutes_used`/`total_prompt_tokens`/`total_completion_tokens` (graph.py:69-72). Send lanes are seeded with empty `research: []`/`turns: []`/zero token counters so lane deltas merge additively without double-count (graph.py:376-381).
- Last-write (no reducer): `error: Optional[str]` (graph.py:74) and `_role_failed: Optional[str]` (graph.py:75). Success paths never write `error`, so one failed lane's error persists to terminal state.
- Fan-out site: `_fanout_after_planner` builds `Send("researcher", {...plan_item, lane_idx...})` per lane (graph.py:358-385); conditional edge registered at graph.py:399-404 with target node `"researcher"` added at graph.py:331-333.

**2. Lane exception does NOT escape the node.** `researcher_node` wraps the `run_loop` call in try/except (council.py:547-555). Raise sites inside `claude_hooks/agent_loop/runner.py`:
- Retry-exhausted HTTP failure: `chat_fn` is invoked at runner.py:230 (`final = chat_fn(payload)`) — no try/except in `run_loop`; ChatClient's retry budget exhausting propagates here.
- Non-string `tool_executor` return: `output = tool_executor(name, args_str, cwd)` (runner.py:122) then `len(output)` in the log line (runner.py:125) — TypeError propagates out of `execute_tool_calls` (called at runner.py:300) and out of `run_loop`.
- Catch site: council.py:555-573 returns a **tombstone delta**: `error`, `_role_failed: "researcher"`, `research: ["(researcher lane failed: <e>)"]`, `research_rounds_used: 1`, zero-token `turns` entry (council.py:562-573). Comment explicitly cites the prior silent-gap audit finding (council.py:557-561). So the exception never reaches `compiled.stream`.

**3. `compiled.stream` loop (`consultants/server/runner.py`)**
- Stream loop at runner.py:137-154; a graph-level exception would be caught at runner.py:155 and set `state.status = "failed"` + `_write_failed_artifacts` (runner.py:157-161, 211-237) — but lane exceptions don't reach this path thanks to the tombstone.
- After drain: `node_error = final_state.get("error")` (runner.py:172) → `terminal_status = "failed" if node_error else "completed"` (runner.py:174). Because `error` is last-write and no later node clears it, a single failed lane flips the **whole consultation to status=failed** (runner.py:189, 201-202).

**4. Synthesizer input.** `build_synthesizer_messages` (council.py:245-266) iterates `state["research"]` emitting `RESEARCHER REPORT (round N):` blocks (council.py:253-254). The failed lane's tombstone string appears inline as a numbered researcher report alongside the surviving lanes' findings; critique omitted at low/medium effort (critic dropped, runner.py:58-59). Synthesizer then produces a **real answer built over the surviving lanes** (council.py:753-758).

**5. Storage.** `storage.write_consultation` (runner.py:199 → storage.py:200-208) writes `summary.md` with `status: failed` + `error:` in YAML front-matter (storage.py:108-110) over a body that is the synthesizer's actual answer (storage.py:117-120). `transcript.md` includes the tombstone turn `(researcher lane failed: …)` (storage.py:123-158). `metadata.json` carries `status`/`error` fields (storage.py:166-169).

**Verdict:** The council still produces a coherent, partially-grounded answer (surviving lanes + plan), but the session is marked `status=failed`. User-perceived failure mode: they receive a real answer in the summary body while the session/CLI poll and front-matter scream `failed` with a raw error string — a false "whole consultation died" signal for a single-lane degrade. Also nondeterministic: with ≥2 failed lanes, `error` holds whichever lane wrote last (parallel last-write).

**One hardening change:** demote the lane failure from a global-state error to a lane-scoped marker — in the tombstone at council.py:564-565, drop `"error"`/`"_role_failed"` (keep the `research` tombstone string so the synthesizer/transcript still see the gap) and instead let runner.py:172-174 compute `terminal_status` from an aggregate flag (e.g. `status="failed"` only when every researcher tombstone or a mandatory role failed, else `"completed"` with the per-lane error surfaced via `retries_by_role` or a new `lane_errors` additive list in `CouncilState` at graph.py:73-75). The single edit point: council.py:562-573.

---

## Researcher (round 1)

## Findings

**Key correction to the question's premise:** the lane exception is NOT unhandled at the graph level — `researcher_node` wraps `loop_runner` in try/except.

### 1. Where the exception is caught
- `run_loop` has **no internal catch** around `chat_fn(payload)` at `claude_hooks/agent_loop/runner.py:230` or around `tool_executor` at `runner.py:122` — an HTTP-timeimeout-exhausts-retries error or a non-string `tool_executor` return (breaks `len(output)` at `runner.py:125`) propagates out of `run_loop`.
- `researcher_node` catches it: `consultants/engine/council.py:547-555` (`try: final = loop_runner(...)` / `except Exception as e:`), returning a **tombstone delta** at `council.py:562-573`: `error`, `_role_failed="researcher"`, `research=["(researcher lane failed: {e})"]`, `research_rounds_used=1`, plus a zero-token `RoleTurn` in `turns`. The comment at `council.py:557-561` explicitly cites the audit finding (csl-...-faa2) that without this a crashed lane silently contributes nothing.

### 2. Reducer merge (graph.py)
- Additive channels: `research`, `turns`, `research_rounds_used`, `total_*_tokens` via `Annotated[..., operator.add]` — `consultants/engine/graph.py:64-72`. Lane payloads pre-zero these at `graph.py:377-381` so deltas are additive-only.
- **Non-additive channels**: `error` and `_role_failed` are plain `LastValue` channels (`graph.py:74-75`). LangGraph pinned at `consultants/pyproject.toml:17` (`langgraph>=0.2,<0.4`). Langgraph semantics (not readable from this repo — langgraph not vendored): concurrent writes to a `LastValue` channel from parallel `Send` branches raise `InvalidUpdateError`.
  - **One lane fails, siblings succeed** → only the failing lane writes `error`/`_role_failed`; successful lanes write only additive keys (`council.py:658-664`). No conflict; merge succeeds.
  - **Two lanes fail concurrently** → both write `error` and `_role_failed` → `InvalidUpdateError` → the exception surfaces from `compiled.stream(...)` in the runner.

### 3. runner.py stream loop
- `consultants/server/runner.py:136-161`: `compiled.stream(initial, stream_mode=["updates","values"])`; `values` payloads become `final_state` (`runner.py:139-140`).
- Graph-level crash (incl. the double-lane `InvalidUpdateError`) → `except Exception` at `runner.py:155-160`: `state.status="failed"`, `state.error="graph crashed: {e}"`, then `_write_failed_artifacts` (`runner.py:211-237`): writes `summary.md`/`transcript.md`/`metadata.json` with `final_answer=f"(consultation failed: {exc})"`, `turns=[]`, `status="failed"` — **all successful-lane work discarded**.
- Single-lane-failure path: stream completes; `node_error = final_state.get("error")` at `runner.py:172`; `terminal_status = "failed" if node_error else "completed"` at `runner.py:174`.

### 4. Synthesizer input
- `synthesizer_node` builds messages via `build_synthesizer_messages(question, plan, state.get("research") or [], critique)` — `council.py:718-723`, builder at `council.py:245-266`.
- The failed lane appears as one enumerated `"\nRESEARCHER REPORT (round {i}):\n(researcher lane failed: {e})"` entry (`council.py:253-254`) interleaved with surviving lanes' real reports. The synthesizer prompt (`council.py:156-168`) never mentions failure semantics — the LLM sees a text tombstone and typically synthesizes a confident answer over the surviving lanes, possibly citing the "(researcher lane failed: …)" string verbatim.

### 5. storage.py
- Success/degraded path: `storage.write_consultation(result, cwd)` at `runner.py:199` → `consultants/engine/storage.py:200-207` writes all three artifacts atomically (`_atomic_write_text`, `storage.py:174-192`). Status is `"failed"` (from `runner.py:189`) but `final_answer` is the real synthesizer text (`runner.py:186`) and `turns` contains the failure tombstone.
- Crash path: same writer invoked from `_write_failed_artifacts` (`runner.py:235`).

### Bottom line
- **One lane fails** → degraded but coherent answer; however `status=failed` (runner.py:172-174 flips it because `error` is set), so the user polling `/v1/consult/{sid}` (`consultants/server/app.py:231-243`) sees `failed` + `error: "researcher failed: …"` even though `summary.md` holds a usable answer. Perceived failure mode: status/answer mismatch.
- **Two+ lanes fail concurrently** → whole consultation dies: `InvalidUpdateError` on the non-additive `error`/`_role_failed` channels → `status=failed`, answer is `"(consultation failed: …)"`, transcript empty (`runner.py:224-225`).

### Recommended hardening (exactly one)
In `council.py:562-573`, when the failure occurs on a Send lane (state has `plan_item`/`lane_idx`, see `council.py:515-516`), **omit the non-additive `error` and `_role_failed` keys from the tombstone return** (keep the additive `research`/`turns` markers so the synthesizer and transcript still see the gap). This removes the concurrent-write channel conflict — the only mode that destroys successful-lane work — while preserving the existing visible-degradation path; the `error`/`_role_failed` keys remain for the single-researcher re-route path where no concurrency exists.

---

## Critic (round 2)

DECISION: ready

The evidence chain is complete and internally consistent across both rounds: premise correction (exception caught at council.py:547-555, tombstone delta at council.py:562-573), reducer semantics (graph.py:64-75, additive vs. LastValue), runner status logic (runner.py:136-161, 172-174, 211-237), synthesizer input construction (council.py:245-266, 718-723, 753-758), and storage writes (storage.py:108-120, 123-158, 166-169, 200-207). Every load-bearing claim has a path:line, and the two rounds converge on the same verdict with the hardening edit point (council.py:562-573). Two residual caveats the synthesizer should hedge on rather than re-research: (1) the multi-lane `InvalidUpdateError` claim rests on langgraph's documented LastValue concurrent-write semantics for the pinned `>=0.2,<0.4` range, not repo-vendored code — high-confidence inference, but present it as such; (2) "success paths never write `error`" (so the tombstone's error survives to terminal state) was asserted in round 1 without explicitly enumerating synthesizer_node's return keys — round 2's reading of council.py:753-758 makes this very likely verified, but the synthesizer should state it as the researcher reported it. Note also the round-2 self-flagged bad citation for app.py:231-243 — cite the polling-endpoint behavior without that line number. The hardening recommendation is genuinely one change at one edit point and eliminates the only catastrophic mode (concurrent LastValue writes discarding all successful-lane work); rounds 1 and 2 give compatible narrow and elaborated variants for the synthesizer to present.

---

## Synthesizer

**Bottom line: the consultation does not die — one failed lane produces a degraded but coherent answer, yet the whole session is mislabeled `status=failed` with a raw error string. Two or more lanes failing concurrently is a genuinely different, catastrophic case: the graph itself crashes and all successful-lane work is discarded.**

Trace, step by step:

1. **Reducers (graph.py).** `research`, `turns`, `research_rounds_used`, `critic_reroutes_used`, and the token counters are additive via `Annotated[..., operator.add]` (consultants/engine/graph.py:64-72), so parallel `Send` lanes merge cleanly — the fan-out payload pre-zeroes them (graph.py:376-381 per the researchers; Send fan-out built at graph.py:358-385). But `error` and `_role_failed` are plain last-write fields with **no reducer** (graph.py:74-75), and successful lanes never write `error` — so a single failed lane's error survives to terminal state, and two lanes writing them concurrently is a channel conflict.

2. **The exception never reaches the stream loop.** The premise of an "unhandled" exception is wrong at the graph level: `researcher_node` wraps `loop_runner(...)` in try/except (consultants/engine/council.py:547-555). Inside `run_loop` the raise sites are real — `chat_fn` at claude_hooks/agent_loop/runner.py:230 (where an exhausted retry budget propagates) and `tool_executor` output hitting `len(output)` at runner.py:125 (non-string return → TypeError) — but the except at council.py:555 catches whatever escapes and returns a **tombstone delta** (council.py:562-573): `error: "researcher failed: …"`, `_role_failed: "researcher"`, `research: ["(researcher lane failed: …)"]`, plus a zero-token `RoleTurn`. This is the deliberate fix for a prior audit finding (council.py:557-561).

3. **Stream loop (runner.py).** `compiled.stream` at consultants/server/runner.py:137-138 drains normally on the single-lane-failure path; the `except Exception` at runner.py:155-161 (→ `status="failed"`, `_write_failed_artifacts`) never fires. Instead, after the drain, `node_error = final_state.get("error")` at runner.py:172 flips `terminal_status = "failed"` at runner.py:174 — so one lane's failure marks the **entire consultation failed** even though the graph completed.

4. **What the synthesizer sees.** `build_synthesizer_messages` iterates `state["research"]` emitting numbered `RESEARCHER REPORT (round N):` blocks (council.py:253-254, builder at council.py:245-266; called at council.py:718-723). The failed lane appears as one entry containing the literal string `(researcher lane failed: …)` interleaved with the surviving lanes' real reports; the prompt has no failure semantics, so the LLM synthesizes a confident answer over the surviving content (possibly quoting the tombstone verbatim). The answer is real, not an error placeholder (council.py:753-758).

5. **Storage.** `storage.write_consultation` is called at runner.py:199 (storage.py:200-207) and writes `summary.md` with `status: failed` and `error:` in the YAML front-matter (storage.py:108-110) **over a body that is the actual synthesizer answer** (storage.py:117-120). `transcript.md` includes the tombstone turn (storage.py:123-158); `metadata.json` carries status/error (storage.py:166-169).

**User-perceived failure mode (single lane):** a status/answer mismatch — they get a usable, partially-grounded answer in the summary body while the poll endpoint and front-matter say `failed` with a raw error string, falsely signaling the whole consultation died. The status is also nondeterministic if multiple lanes fail sequentially (last-write wins).

**The two-lane case is the real catastrophe:** with ≥2 lanes failing in the same fan-out, both write `error`/`_role_failed` concurrently to the reducer-less LastValue channels (graph.py:74-75). Per langgraph's documented concurrent-write semantics for the pinned `>=0.2,<0.4` range (consultants/pyproject.toml:17 — semantics inferred, langgraph isn't vendored here), that raises `InvalidUpdateError` out of `compiled.stream`, hitting runner.py:155-161: `_write_failed_artifacts` (runner.py:211-237) writes `final_answer = "(consultation failed: …)"` and an empty transcript — **all successful-lane work is discarded**.

**One hardening change:** edit the tombstone at **council.py:562-573** so that when the failure occurs on a `Send` lane (state carries `plan_item`/`lane_idx`), the return omits the non-additive `error` and `_role_failed` keys while keeping the additive `research` tombstone string and `turns` entry. That single edit removes the concurrent-write conflict — the only mode that destroys successful-lane work — and lets runner.py:172-174 stay meaningful for the single-researcher re-route path. Optionally pair it with an additive `lane_errors: Annotated[list, operator.add]` field in `CouncilState` (graph.py:73-75) so runner.py:174 can distinguish "one lane degraded, answer fine" from "mandatory role failed" instead of flipping everything to `failed`.

CONFIDENCE: 0.85 — the two-lane `InvalidUpdateError` claim rests on langgraph's documented LastValue semantics rather than repo code, and "success paths never write `error`" is as-reported rather than exhaustively enumerated; all single-lane path citations were re-verified directly.

---
