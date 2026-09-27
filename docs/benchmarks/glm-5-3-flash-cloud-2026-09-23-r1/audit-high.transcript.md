# Consultation transcript — csl-2026-09-23-2018-0987

_Question_: In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:

1. CouncilState's additive reducers (consultants/engine/graph.py)
2. consultants/server/runner.py's compiled.stream loop
3. The synthesizer's input messages — what does it actually see?
4. The storage.py artifact writer

Answer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line.


## Planner

1. In consultants/engine/graph.py: locate CouncilState's field/reducer annotations (operator.add or custom additive reducers), the Send()-based fan-out node dispatching researcher lanes, and whether each researcher node wraps agent_loop.runner.run_loop in try/except or lets the exception propagate as a node error.
2. In agent_loop/runner.py run_loop: find the HTTP retry loop (which exception type escapes when the retry budget exhausts) and the tool-result insertion point (which line raises when tool_executor returns a non-string, e.g. str concat or message validation); report whether run_loop has an internal catch-all.
3. In consultants/server/runner.py: trace the compiled.stream(...) consumption loop — enumerate every try/except around chunk iteration, what happens to the generator when an exception arrives mid-stream, and cite the exact lines where run status is set to completed vs failed.
4. In the synthesizer node (consultants/engine/graph.py + its prompt builder): determine which state field(s) feed its input messages, what it sees when one lane's reducer bucket is empty/absent, and whether a raised lane exception prevents the synthesizer node from executing at all — check add_node wiring for retry_policy/error handling config at the graph build site.
5. In storage.py: find the artifact writer's call sites relative to the stream loop (per-chunk, on-completion, or finally block) and what gets persisted when the stream raises mid-run — partial artifact, failed-status record, or nothing.
6. Reconcile steps 1–5 into the end-to-end verdict: identify the exact divergence line in consultants/server/runner.py where a single failed lane yields a degraded synthesis (missing-lane answer) versus an aborted consultation surfaced as status=failed, and note any LangGraph superstep-abort semantics observed in the stream behavior.

---

## Researcher (round 1)

## Findings

**1. CouncilState reducers & fan-out (consultants/engine/graph.py)**
- Additive reducers: `research: Annotated[list, operator.add]` graph.py:64, `turns` graph.py:68, token/round counters graph.py:69-72. `error` (graph.py:74) and `_role_failed` (graph.py:75) are **plain overwrite channels** — last lane to complete wins.
- Send fan-out: `_fanout_after_planner` graph.py:349-385 emits one `Send("researcher", {...})` per lane with `research: []`, `turns: []`, token counters zeroed (graph.py:377-381) so lane deltas are purely additive; wired via `add_conditional_edges` graph.py:399-404.

**2. Does the lane exception escape? — No.**
- `researcher_node` wraps the `run_loop` call in try/except: council.py:547-554 (call), council.py:555 (`except Exception as e`). On failure it returns a **tombstone**: `"error": f"researcher failed: {e}"`, `"_role_failed": "researcher"`, and — crucially — `"research": [err_text]` with `err_text = f"(researcher lane failed: {e})"` (council.py:562-573). The comment at council.py:557-561 states this is deliberate: a crashed lane that contributed nothing previously caused the synthesizer to write a "confidently-degraded answer over the surviving lanes" (audit finding csl-...-faa2). So the failure is converted into a state delta; the node never raises.
- Same tombstone pattern for planner (council.py:452-462), critic (council.py:683-694, defaults `critic_decision: "ready"`), synthesizer (council.py:736-747, placeholder final_answer).

**3. What escapes run_loop (claude_hooks/agent_loop/runner.py)**
- **No internal catch-all**: `final = chat_fn(payload)` at runner.py:230 sits inside the bare `for` loop with no try. Retry exhaustion escapes as: HTTPError non-retryable after budget → `RuntimeError(...)` chat_client.py:285; URLError/OSError (timeout) → re-raise chat_client.py:302; last-resort `raise last_exc` chat_client.py:305.
- **Non-string tool_executor result**: `output = tool_executor(name, args_str, cwd)` runner.py:122; `len(output)` at runner.py:126 raises `TypeError` immediately for `int`/`None` (dedup cache `seen[key] = output` runner.py:128 and `"content": output` runner.py:133 don't validate). A dict/list survives `len()` and json serialization but Ollama 400s on the next `chat_fn` call → non-retryable HTTPError → RuntimeError chat_client.py:285. Either way: exception out of run_loop → caught at council.py:555 → tombstone. Identical downstream path.

**4. compiled.stream loop (consultants/server/runner.py)**
- Stream loop runner.py:136-154 consumes `(mode, payload)`; the node-internal try/except means **the generator never raises** for a failed lane. The outer `except Exception` at runner.py:155-161 only fires when a node *itself* raises (e.g., an exception outside researcher_node's try, like a KeyError in `build_researcher_messages`) — that path sets `state.status="failed"` and calls `_write_failed_artifacts` (runner.py:211-237) with `final_answer=f"(consultation failed: {exc})"` and empty turns.
- Post-stream: `node_error = final_state.get("error")` runner.py:172; `terminal_status = "failed" if node_error else "completed"` **runner.py:174**. Because the failed lane's tombstone sets `error` and nothing ever clears it, **terminal_status is "failed" even when the synthesizer produced a full answer**.
- Artifacts: `storage.ConsultationResult` built at runner.py:177-198 with `final_state.get("final_answer", "")` (runner.py:186) and `status=terminal_status` (runner.py:189); written via `storage.write_consultation` runner.py:199 → summary.md/transcript.md/metadata.json atomically (storage.py:200-207). Status lands in YAML front-matter at storage.py:108.

**5. What the synthesizer sees**
- `build_synthesizer_messages(state["question"], state.get("plan",""), state.get("research") or [], state.get("critique"))` council.py:718-723. The `research` list contains the surviving lanes' real reports **plus the literal string `"(researcher lane failed: <exception>)"`** merged by the additive reducer. The synthesizer has no structured signal that the run is degraded — it sees the tombstone as if it were one lane's findings and either compensates for the gap or may echo the failure text into the answer.

**6. Storage writer**
- storage.py:117-120 `render_summary` = front-matter + `final_answer`; transcript renders each `RoleTurn` including the failure turn (storage.py:129-157). No failure-specific handling — it faithfully records whatever `runner.py` hands it.

## Answer

**The council produces a coherent, degraded answer — but surfaces it as status=failed.** Concretely: the lane exception is caught inside `researcher_node` (council.py:555), converted to a tombstone in the additively-merged `research` channel, the critic/synthesizer proceed normally, and `summary.md` contains a real synthesizer answer built from surviving lanes (with one lane replaced by the tombstone text). But `error` (graph.py:74, overwrite reducer) stays set, so runner.py:174 forces `terminal_status="failed"`; the session shows failed (runner.py:201) and metadata.json/status front-matter say failed. **Failure mode the user perceives:** a status=failed consultation whose summary.md actually contains a usable answer — a status/artifact contradiction. Only if the exception fires *outside* the node's try (or in another node's non-wrapped code) does it hit runner.py:155 and become a genuine full failure with placeholder answer and no turns.

## Recommended hardening (ONE change)

**consultants/server/runner.py:172-174** — replace the blunt `terminal_status = "failed" if node_error else "completed"` with degraded-aware classification: when `node_error` is set but `_role_failed` in `("researcher", "critic")` **and** `final_state.get("final_answer")` is non-empty, mark the run `completed` (or a new `"degraded"` status) while still persisting `error` in metadata; reserve `failed` for synthesizer/planner failures or empty `final_answer`. This is a 3-line change at the exact decision point, requires no graph/state changes, and eliminates the failed-status/valid-answer contradiction without losing the error breadcrumb.

---

## Researcher (round 1)

## Findings — lane exception → end-to-end path

**1. Lane exception is caught, not propagated to LangGraph.** `researcher_node` wraps the `run_loop` call in try/except: `consultants/engine/council.py:547-555`. Any exception from `claude_hooks.agent_loop.runner.run_loop` (HTTP timeout exhausting retries, tool_executor raising on a non-string, etc.) is caught at `council.py:555` and converted to a **tombstone return** (`council.py:562-573`): `"error": "researcher failed: {e}"`, `"_role_failed": "researcher"`, `"research": ["(researcher lane failed: {e})"]`, plus a `RoleTurn`. The comment at `council.py:557-561` says this tombstone was deliberately added to fix the audit-high finding (crashed lane silently contributing nothing → confidently-degraded synthesis). Note `graph.py` `add_node` calls carry **no `retry_policy`** (`consultants/engine/graph.py:319-340`), so retries are entirely inside `run_loop`.

**2. Additive reducers absorb the tombstone.** `research`, `turns`, `research_rounds_used`, `total_*_tokens` are `Annotated[..., operator.add]` channels (`consultants/engine/graph.py:64-72`); Send lanes start each with empty/zero (`graph.py:377-381`) and the tombstone merges like any lane delta. **However `error` and `_role_failed` are plain, non-annotated channels** (`graph.py:74-75`) — last-value-wins, no reducer.

**3. Stream loop / runner.** The compiled stream loop (`consultants/server/runner.py:137-161`) only sets `status="failed"` if the *stream itself raises* (LangGraph superstep abort — runner.py:155-161, which calls `_write_failed_artifacts` writing `models={}, turns=[], tokens=0`, runner.py:211-237). A tombstoned lane does **not** raise; the superstep merges normally. After drain, runner reads `node_error = final_state.get("error")` and sets `terminal_status = "failed" if node_error else "completed"` (`runner.py:172-174`) — **this is the divergence line**: single failed lane ⇒ degraded synthesis still produced, but session surfaced as `status="failed"` with `error` set (`runner.py:201-202`). App-level fallback `runner crashed` path is `consultants/server/app.py:318-324`.

**4. What the synthesizer actually sees.** `synthesizer_node` feeds `state.get("research")` into `build_synthesizer_messages` (`council.py:715-723`; builder at `council.py:245-266`). The tombstone string renders as a regular `RESEARCHER REPORT (round i): (researcher lane failed: ...)` — the synthesizer is never told it's a failure marker, and executes regardless (no error gate). So the council **does produce a coherent, degraded answer** that names the gap only insofar as the model interprets the tombstone text.

**5. Storage.** Artifacts are written once after the stream drains — not in a `finally`, not per-chunk: `storage.write_consultation(result, ...)` at `runner.py:177-199`. `storage.write_consultation` writes all three files atomically (tmp+rename) — `consultants/engine/storage.py:200-207`, `_atomic_write_text` at `storage.py:174-192`. On tombstone path: full artifacts with `status: "failed"`, `error` in front-matter (`storage.py:108-110`), `final_answer` = degraded synthesis. On stream-crash path: only the `_write_failed_artifacts` stub (`runner.py:160, 211-237`).

**Verdict:** The consultation does NOT abort. One failed lane ⇒ degraded-but-coherent answer (surviving lanes' reports + tombstone string), yet the session is misleadingly surfaced as `status="failed"` because any residual `error` channel flips `terminal_status` (`runner.py:174`). The user perceives a failed consultation in the poll/statusline even though `summary.md` contains a usable answer.

**Latent second bug (evidence-grounded):** if **two lanes fail in the same superstep**, both tombstones return `"error"`/`"_role_failed"` — non-annotated channels with no reducer receiving two values in one step will raise LangGraph's InvalidUpdateError, which the runner catches at `runner.py:155` ⇒ whole consultation aborted to `status="failed"` with zero-turn artifacts.

**ONE hardening recommendation:** at `consultants/engine/council.py:562-573`, stop setting the non-annotated `"error"`/`"_role_failed"` keys on per-lane tombstones (keep the `"research"` tombstone + `RoleTurn`; record lane failure on an additive-safe channel instead, e.g. append to `turns` and let the runner derive status from `_role_failed` presence at `runner.py:172-174`). This fixes both the misleading `status="failed"` for single-lane failures and the multi-lane same-superstep channel-conflict abort.

---

## Critic (round 2)

DECISION: ready

Both rounds independently corroborate every load-bearing claim: lane exceptions are caught inside `researcher_node` and converted to tombstones (council.py:555, 562-573) rather than propagating; the tombstone merges through the `operator.add` channels while `error`/`_role_failed` stay last-write (graph.py:64-75); the divergence point is `terminal_status = "failed" if node_error else "completed"` at runner.py:172-174, producing a usable `summary.md` under a misleading `status=failed` (full artifacts via storage.py:200-207, error in front-matter at storage.py:108-110); and the stream-crash path (`_write_failed_artifacts`, runner.py:155-161, 211-237) is only reachable for exceptions outside the node-level try. The synthesizer-input question is answered concretely (tombstone renders as an ordinary `RESEARCHER REPORT`, no structured degradation signal). Two caveats for synthesis, neither a research gap: (1) the rounds disagree on the ONE fix — round 1's runner.py:172-174 classification change preserves the error breadcrumb but wouldn't fix the claimed multi-lane same-superstep channel conflict; round 2's council.py:562-573 fix addresses both but drops the breadcrumb unless the runner derives status from `_role_failed`. The trade-off data needed to choose is present. (2) The multi-lane InvalidUpdateError claim rests on LangGraph library semantics, not a cited source line — treat it as plausible-but-unverified and weight the fix choice accordingly; the primary verdict does not depend on it.

---

## Synthesizer

**Verdict: the council still produces a coherent, degraded answer built from the surviving lanes — but it is misleadingly surfaced as `status=failed`.** The consultation does not abort. Only an exception escaping *outside* a node's own try/except triggers a genuine full failure.

**1. CouncilState additive reducers — the exception never reaches LangGraph.**
- `research: Annotated[list, operator.add]` (graph.py:64), `turns` (graph.py:68), round/token counters (graph.py:69-72) absorb per-lane deltas; Send lanes start each with empty/zero values (graph.py:377-381). `error` and `_role_failed` are plain last-write channels (graph.py:74-75).
- `researcher_node` wraps the `run_loop` call in try/except (council.py:547-555) and converts any exception into a tombstone: `"error": f"researcher failed: {e}"`, `"_role_failed": "researcher"`, `"research": [f"(researcher lane failed: {e})"]`, plus a zero-token RoleTurn (council.py:562-573). The comment at council.py:557-561 says this is deliberate — a crashed lane contributing nothing previously caused confidently-degraded synthesis (audit finding csl-…-faa2).
- Whatever escapes `run_loop` — non-retryable `RuntimeError` from chat_client.py:285 after retry exhaustion, or `TypeError` at `len(output)` when the tool_executor returns an int/None (claude_hooks/agent_loop/runner.py:122-126) — hits the same `except` at council.py:555. Same tombstone, same downstream path.

**2. compiled.stream loop (consultants/server/runner.py).**
- The stream loop (runner.py:136-161) never sees the failure — the node returned normally, so the superstep merges cleanly. The outer `except Exception` at runner.py:155-161 fires only if a node itself raises; that path sets `status="failed"`, `error=f"graph crashed: {e}"`, and calls `_write_failed_artifacts` (runner.py:160) — the only true abort path.
- After drain, the divergence line: `node_error = final_state.get("error")` (runner.py:172); `terminal_status = "failed" if node_error else "completed"` (runner.py:174). Because the tombstone set `error` and nothing clears it, **a single failed lane forces `terminal_status="failed"` even though the synthesizer produced a full answer.**

**3. Synthesizer input — what it actually sees.**
`build_synthesizer_messages(state["question"], state.get("plan",""), state.get("research") or [], state.get("critique"))` (council.py:718-723). The `research` list contains the surviving lanes' real reports **plus the literal string `(researcher lane failed: <exception>)`** merged by the additive reducer. It renders as an ordinary `RESEARCHER REPORT (round i)` entry (builder at council.py:245-266) — there is no structured degradation signal and no error gate, so the synthesizer runs and either compensates for the gap or may echo the tombstone text. `add_node` wiring carries no `retry_policy` (graph.py:319-340), so retries are entirely inside `run_loop`.

**4. storage.py artifact writer.**
`storage.write_consultation` is called once after the stream drains (runner.py:199), writing summary.md/transcript.md/metadata.json atomically via tmp+rename (storage.py:200-207, `_atomic_write_text` at storage.py:174-192). On the tombstone path the artifacts are complete: `status` lands in YAML front-matter (storage.py:108), `error` right below it (storage.py:109-110), and `final_answer` (runner.py:186) is the real degraded synthesis. Only the stream-crash path writes the `_write_failed_artifacts` stub instead (runner.py:160, 211-237).

**Failure mode the user perceives:** a status/summary contradiction — the poll/session shows `failed` (runner.py:201-202, error propagated at runner.py:190, 202) while `summary.md` contains a usable answer synthesized from the surviving lanes. The transcript faithfully includes the tombstone turn (storage.py:129-157).

**Caveat (plausible, not code-verified):** if two lanes fail in the same superstep, both tombstones write to the non-reduced `error`/`_role_failed` channels (graph.py:74-75); under LangGraph's concurrent-update semantics this may raise InvalidUpdateError, caught at runner.py:155 → genuine abort. This rests on library semantics, not a cited source line.

**Hardening (ONE change):** `consultants/server/runner.py:172-174` — replace the blunt `terminal_status = "failed" if node_error else "completed"` with degraded-aware classification: when `node_error` is set but `final_state.get("final_answer")` is non-empty and `_role_failed` is a non-synthesizer role, mark the run `completed` (or a new `degraded` status) while still persisting `error` via runner.py:190/202; reserve `failed` for planner/synthesizer failures or empty `final_answer`. Three lines at the exact divergence point, no state changes, preserves the error breadcrumb. (It would not fix the unverified multi-lane channel conflict; that would require the tombstone in council.py:562-573 to stop writing `error`/`_role_failed` — but that fix trades away the breadcrumb unless the runner derives status from `_role_failed`, so the runner-side change is the safer single move.)

---
