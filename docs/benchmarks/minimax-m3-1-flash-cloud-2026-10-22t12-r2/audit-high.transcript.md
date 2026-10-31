# Consultation transcript — csl-2026-10-22-1752-35a9

_Question_: In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:

1. CouncilState's additive reducers (consultants/engine/graph.py)
2. consultants/server/runner.py's compiled.stream loop
3. The synthesizer's input messages — what does it actually see?
4. The storage.py artifact writer

Answer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line.


## Planner

1. Trace exception propagation inside `claude_hooks/agent_loop/runner.py`: verify that `chat_fn(payload)` at `claude_hooks/agent_loop/runner.py:230` is unguarded, while a tool result is passed directly to `tool_executor` at `claude_hooks/agent_loop/runner.py:122` and then measured with `len(output)` at `claude_hooks/agent_loop/runner.py:126`, establishing how timeout and non-string tool failures escape `run_loop`.
2. Verify that `council.researcher_node` catches either `run_loop` exception and converts it into an `error`, `_role_failed`, additive `research` tombstone, and researcher turn at `consultants/engine/council.py:547-573`, rather than allowing the exception to escape the LangGraph node.
3. Inspect `CouncilState` channel definitions at `consultants/engine/graph.py:50-80` and the `Send` payloads at `consultants/engine/graph.py:349-385`; determine how successful and failed lane deltas merge, and specifically test whether concurrent failures writing the non-additive `error` or `_role_failed` channels can raise a LangGraph concurrent-update error.
4. Trace the fan-out topology and graph termination at `consultants/engine/graph.py:388-425` to establish whether one caught lane failure prevents later synthesizer execution, and use a minimal LangGraph invocation if necessary to distinguish a single failed lane from multiple simultaneously failed lanes.
5. Inspect `compiled.stream` consumption and exception handling at `consultants/server/runner.py:124-161`, verifying which stream events update `final_state` and what separately occurs if LangGraph itself re-raises a concurrent-update or other graph-level exception.
6. Reconstruct the synthesizer input through `consultants/engine/council.py:715-727` and `consultants/engine/council.py:245-266`, confirming whether the synthesizer receives the failed-lane tombstone, successful lane reports, the planner plan, and any critic text, and whether it still emits a `final_answer`.
7. Trace terminal status and artifact construction at `consultants/server/runner.py:171-207`, `consultants/server/runner.py:211-237`, and `consultants/engine/storage.py:97-120,123-169,200-208`; run the relevant test or a minimal consultation to verify whether a caught researcher failure writes a readable degraded answer with `status=failed`, while an uncaught graph exception writes only the placeholder failed answer.

---

## Researcher (round 1)

## Findings (verified)

**1. Where the lane exception is born**
- `run_loop` calls `chat_fn(payload)` with no try/except: `claude_hooks/agent_loop/runner.py:230`. An upstream failure raised by `ChatClient.chat` (retries exhausted → `RuntimeError("ollama chat HTTP …")` at `claude_hooks/get_advice/chat_client.py:285-287`, or re-raised `URLError`/`OSError` at `claude_hooks/get_advice/chat_client.py:288-302`) propagates straight out of `run_loop`.
- Tool result handling is equally unguarded: `output = tool_executor(name, args_str, cwd)` at `claude_hooks/agent_loop/runner.py:122`, then `len(output)` at `claude_hooks/agent_loop/runner.py:126` — a non-`str`/non-sized return raises `TypeError` there. The production executor is safe (`claude_hooks/caliber_proxy/tools.py:439` coerces to `str`; tool errors are returned as `"error: …"` strings at `claude_hooks/caliber_proxy/tools.py:434-438`), and `traced_tool` re-raises (`consultants/engine/trace.py:217-222`), so today the realistic trigger is the HTTP path.
- Consequence: a lane that dies at iteration *k* loses **all** evidence it had already gathered — `run_loop` never returns, so `_loop_messages` (`claude_hooks/agent_loop/runner.py:310`) is never handed back.

**2. The exception is caught — the graph does not abort**
- `council.researcher_node` wraps the `loop_runner(...)` call in `try/except Exception` (`consultants/engine/council.py:547-555`) and returns a tombstone delta: `error`, `_role_failed`, additive `research`, `research_rounds_used: 1`, and a researcher `RoleTurn` (`consultants/engine/council.py:562-573`). The comment at `consultants/engine/council.py:557-561` names the exact hazard this prevents (audit-high `csl-…-faa2`).
- Unit tests pin that contract: `tests/test_consultants_council.py:531-561` (tombstone in `research`, turn emitted, counter +1) and `tests/test_consultants_council.py:514-529` (`error` set).

**3. Reducer merge semantics (`CouncilState`)**
- Additive (concatenating / summing) channels: `research` (`consultants/engine/graph.py:64`), `turns` (`consultants/engine/graph.py:68`), `research_rounds_used` / `critic_reroutes_used` / `total_prompt_tokens` / `total_completion_tokens` (`consultants/engine/graph.py:69-72`).
- **Non-additive** (last-value) channels: `error` and `_role_failed` — declared as bare `Optional[str]` with **no `Annotated` reducer** (`consultants/engine/graph.py:74-75`).
- The `Send` payload per lane seeds `research: []`, `turns: []`, `research_rounds_used: 0`, token counters 0 (`consultants/engine/graph.py:377-381`), so each lane contributes only a delta and the additive channels merge cleanly across the ≤3 lanes (`FANOUT_MAX_LANES = 3`, `consultants/engine/council.py:307`).
- Risk flagged, **not verifiable here**: two lanes failing in the same superstep both write the reducer-less `error` / `_role_failed` keys (`consultants/engine/council.py:564-565`) — the classic "two parallel writes to a non-aggregated channel" pattern. LangGraph is deliberately absent from this environment (`consultants/engine/council.py:5-10`, `consultants/engine/graph.py:3-9`, `tests/test_consultants_council.py:5-8`), so I could not execute a LangGraph invocation to confirm whether that raises `InvalidUpdateError` (total loss) or silently keeps one writer's value. Evidence insufficient on the LangGraph-internal side; the repo-side facts above are verified.

**4. The graph still reaches the synthesizer**
- Fan-out replaces only the `planner → researcher` edge (`consultants/engine/graph.py:391-394`); the conditional edge emits the `Send` list (`consultants/engine/graph.py:349-385`, wired at `consultants/engine/graph.py:399-404`).
- The post-researcher edge is unconditional from `plan_topology` (`consultants/engine/graph.py:213-219`): at low/medium the critic node is dropped (`consultants/server/runner.py:58-63`) so the edge is `researcher → synthesizer`; at high/max it is `researcher → critic → synthesizer` with the critic edge conditional (`consultants/engine/graph.py:412-421`).
- Nothing in the pipeline inspects `state["error"]` to short-circuit; `route_after_critic` only reads `critic_decision` / counters (`consultants/engine/council.py:366-390`). **A single caught lane failure does not prevent the synthesizer from running.**
- Side effect worth knowing: `research_rounds_used` is additive and each lane adds 1 (`consultants/engine/council.py:567`), so 3 lanes make it 3 — already ≥ `researcher_rounds_max` at medium (1) and high (3), so the critic's "needs_more_research" re-route is effectively dead after a fan-out (`consultants/engine/council.py:383-389`).

**5. What the synthesizer actually sees**
- `synthesizer_node` passes `state.get("research")` verbatim into the prompt (`consultants/engine/council.py:718-723`).
- `build_synthesizer_messages` renders each entry as `"\nRESEARCHER REPORT (round {i}): {r}"` (`consultants/engine/council.py:253-254`) — so the failed lane appears as a *report* whose body is the literal string `"(researcher lane failed: <exc>)"` (`consultants/engine/council.py:562`).
- What the synthesizer does **not** get: the lane index / `plan_item` of the dead lane. The tombstone text carries only the exception (`consultants/engine/council.py:562`), so the synthesizer must infer the missing sub-question by matching the surviving reports against the plan. Nothing in `SYNTHESIZER_SYSTEM` (`consultants/engine/council.py:156-168`) tells it to flag the gap. The critic sees the same list (`consultants/engine/council.py:231-242`), and any re-route re-runs the *full* plan, not the lost lane's sub-plan (`consultants/engine/council.py:524-527`).

**6. Terminal status — the user-perceived failure**
- `node_error = final_state.get("error")`; `terminal_status = "failed" if node_error else "completed"` (`consultants/server/runner.py:172-174`). The `error` channel keeps its value across supersteps, so the lane's error survives to the final state.
- Therefore: **a complete synthesizer answer exists, and the session is still reported `status: "failed"`** (`consultants/server/runner.py:189-201`; `state.error = node_error` at `consultants/server/runner.py:202`).
- The `/consultants` skill reacts to `failed` by surfacing the role error and suggesting a model switch, and only fetches the result on `completed` (`.claude/skills/consultants/SKILL.md:92-93`, `.claude/skills/consultants/SKILL.md:136-144`) — so the user is told "the council failed" and is never shown the degraded answer, even though it is on disk and served by `GET /v1/consult/{sid}/result` regardless of status (`consultants/server/app.py:275-279`).
- The `graph crashed` path (`consultants/server/runner.py:155-161`) is a *different* failure: it writes `final_answer = "(consultation failed: …)"` with `turns=[]` (`consultants/server/runner.py:211-237`) → `transcript.md` has zero sections (`consultants/engine/storage.py:129-158`) → total loss of the answer. That is what the multi-lane concurrent-write risk (finding 3) would produce if it raises.

**7. Artifacts on disk**
- `write_consultation` is called with `final_answer` = the synthesizer's real text and `status="failed"`, `error` = the lane error (`consultants/server/runner.py:177-199`).
- `summary.md`: YAML front-matter carries `status: "failed"` and `error: "researcher failed: …"` (`consultants/engine/storage.py:108-110`, `consultants/engine/storage.py:97-114`), body is the degraded answer (`consultants/engine/storage.py:117-120`).
- `transcript.md`: contains a `## Researcher` section whose body is the tombstone text (`consultants/engine/council.py:568-572` → `consultants/engine/storage.py:129-136`), plus the synthesizer turn — so the gap *is* recorded, just not in the answer.
- `metadata.json`: `asdict(result)` with `status` / `error` / `turns` (`consultants/engine/storage.py:166-169`).

## Answer

- **Does the council still produce a coherent answer?** Yes. The exception is caught at `consultants/engine/council.py:555`, converted to a tombstone at `consultants/engine/council.py:562-573`, merged into the additive `research` channel, and the graph proceeds to the synthesizer (`consultants/engine/graph.py:213-219`). The synthesizer produces a normal final answer built from the surviving lanes plus one literal `"(researcher lane failed: …)"` report line (`consultants/engine/council.py:253-254`).
- **Does it surface as `status=failed`?** Yes — and that is the defect. `terminal_status` is derived from the mere presence of `state["error"]` (`consultants/server/runner.py:174`), with no regard for whether a final answer was produced. So a *single* lane failure labels a fully-formed consultation as failed.
- **Perceived failure mode:** the user polls, sees `status: "failed"`, `error: "researcher failed: <exc>"`, and the skill's failure branch (`.claude/skills/consultants/SKILL.md:136-144`) tells them a role died and to go switch the researcher's model — the degraded answer sitting in `summary.md` is never surfaced. If they read `summary.md` themselves, they get a confidently-written answer that silently omits the dead lane's sub-question, because the tombstone is unlabeled (`consultants/engine/council.py:562`) and the synthesizer prompt gives no instruction to hedge on it (`consultants/engine/council.py:156-168`). Net: **a degraded-but-usable answer is reported to the user as a total failure.** (The one exception: a *simultaneous* multi-lane failure may escalate to a genuine graph crash → `status=failed` with `final_answer = "(consultation failed: …)"` and an empty transcript; I could not confirm that LangGraph path in this env — see finding 3.)

## Recommended single hardening

**Derive the terminal status from answer availability, not from the mere presence of a node error — `consultants/server/runner.py:174`:**

```python
# today
terminal_status = "failed" if node_error else "completed"
# proposed
terminal_status = ("failed"
                   if (node_error and not final_state.get("final_answer"))
                   else "completed")
```

Rationale: it is the only change that makes the single-lane case behave correctly end-to-end — the answer is already written to `summary.md`/`transcript.md`/`metadata.json` with `error` preserved (`consultants/server/runner.py:186-198`, `consultants/engine/storage.py:108-110`), so the failure stays auditable in the artifact while the session no longer reports a total loss. It needs no LangGraph-internal assumptions.

Secondary, **not** the recommendation (flagged for the critic): the reducer-less `error` / `_role_failed` channels (`consultants/engine/graph.py:74-75`) are the one place where a *concurrent* multi-lane failure could destroy the answer outright; giving them a merge-safe reducer (e.g. keep-first-non-empty) plus `researcher_node` writing the lane identity into the tombstone text (`consultants/engine/council.py:562`) would close that gap — but its benefit depends on LangGraph behavior I could not execute here.

---

## Researcher (round 1)

## Research report — lane 2: `compiled.stream` → synthesizer → artifact writer

Tooling note: I only have read-only tools (no shell), so **nothing was executed**; every claim below is from file reads/greps. LangGraph is not vendored here (`requirements-dev.txt` has no langgraph; `install.py` has no langgraph reference) so I could not run a live fan-out — items marked *(inference)* are LangGraph-semantics deductions from the reducer declarations, not observed behavior.

### 1. Where the exception is actually caught (before it ever reaches LangGraph)

- `claude_hooks/agent_loop/runner.py:230` — `final = chat_fn(payload)` is unguarded; a `ChatClient` HTTP/timeout failure re-raises after the retry budget (`claude_hooks/get_advice/chat_client.py:285`, `:302`).
- `claude_hooks/agent_loop/runner.py:122` + `:126` — a non-string `tool_executor` return blows up on `len(output)`; `claude_hooks/caliber_proxy/tools.py:439` guarantees `str`, but `consultants/engine/trace.py:226` (`len(out or "")`) raises the same way for a non-str.
- Both propagate out of `run_loop` and are caught by the single `except Exception` around the loop call: `consultants/engine/council.py:547` (try) → `consultants/engine/council.py:555` (except) → tombstone return at `consultants/engine/council.py:562-573`: `error="researcher failed: {e}"`, `_role_failed="researcher"`, `research=[err_text]`, `research_rounds_used=1`, one `RoleTurn`.
- Residual unhandled path: anything raised **outside** that try (e.g. `build_researcher_messages` at `consultants/engine/council.py:521`, `LoopConfig` build at `:537`) escapes as a LangGraph *node* error → graph-level exception.
- `traced_tool` re-raises after recording the span (`consultants/engine/trace.py:222`).

### 2. CouncilState reducers and the fan-out merge

- `consultants/engine/graph.py:64` `research: Annotated[list, operator.add]`; `:68` `turns`; `:69-72` int sums → the tombstone string concatenates with surviving lanes' reports; the failed lane's `RoleTurn` lands in the transcript.
- `consultants/engine/graph.py:74` `error: Optional[str]` and `:75` `_role_failed: Optional[str]` have **no reducer** → LastValue channels. A single failed lane = one write (fine); **two lanes failing in the same superstep = two writes to the same LastValue channel → LangGraph `InvalidUpdateError` (inference, unverified here)** → this aborts the whole graph.
- Send payloads inject empty additive deltas per lane (`consultants/engine/graph.py:377-382`) so there is no double-count; `plan_item`/`lane_idx` are Send-injected per-lane fields (`:79-80`).
- Topology: planner → conditional fan-out (`:349-386`, `:399-404`); researcher→synthesizer unconditional edge (`:218-219`, `:391-397`); all lanes must finish before the synthesizer fires.

### 3. `compiled.stream` consumption (`consultants/server/runner.py:136-161`)

- `consultants/server/runner.py:137-138` — `compiled.stream(initial, stream_mode=["updates","values"])`.
- `consultants/server/runner.py:139-140` — only `"values"` payloads update `final_state` (full merged state); `"updates"` payloads (`:144-154`) are used *solely* to flip `state.progress[role]`.
- Consequence of fan-out: the first lane's update sets `progress["researcher"]="done"` and advances the next role while sibling lanes are still running (`:145-153`) — progress is cosmetic and premature.
- `consultants/server/runner.py:155-161` — **any** exception escaping the stream (including a graph-level `InvalidUpdateError`) sets `state.status="failed"`, `state.error="graph crashed: …"`, and calls `_write_failed_artifacts`.
- `consultants/server/runner.py:211-237` — that fallback writes `final_answer=f"(consultation failed: {exc})"`, `turns=[]`, `models={}` → the surviving lanes' evidence and the planner plan are **discarded from disk**.
- `consultants/server/runner.py:163-169` — roles still `in_progress` are flipped to `done` after a clean drain.

### 4. What the synthesizer actually sees

- `consultants/engine/council.py:718-723` — synthesizer input = `question`, `state["plan"]`, `state["research"]`, `state["critique"]`.
- `consultants/engine/council.py:253-254` — every element of `research` is rendered as `RESEARCHER REPORT (round N): …`, so the tombstone string `"(researcher lane failed: HTTP timeout exhausted retries)"` arrives **verbatim, formatted as if it were a report** — indistinguishable from evidence.
- `consultants/engine/council.py:255-256` — critic verdict is included only if truthy; at `low`/`medium` the critic node is dropped entirely (`consultants/server/runner.py:58-59`), so no verdict at all.
- `consultants/engine/council.py:726` + `:754` — the synthesizer still emits a real `final_answer`; the tombstone does **not** abort or short-circuit it (only a synthesizer-side exception does, `:727-747`).
- The synthesizer prompt explicitly forbids mentioning roles/process (`consultants/engine/council.py:164-165`), so the tombstone is most likely silently dropped → a confident answer that quietly omits the failed lane's sub-question.

### 5. Terminal status + artifacts

- `consultants/server/runner.py:172-174` — `node_error = final_state.get("error")`; `terminal_status = "failed" if node_error else "completed"` → **one caught lane failure marks the whole consultation failed even though a full answer was produced**.
- `consultants/server/runner.py:177-198` — the result carries the real `final_answer` (`:186`), the real `turns` (`:187`), `status="failed"` (`:189`) and `error="researcher failed: …"` (`:190`).
- `consultants/engine/storage.py:200-208` — writes `summary.md` / `transcript.md` / `metadata.json` atomically; `summary.md` front-matter carries `status: failed` + `error:` (`consultants/engine/storage.py:108-110`) with the degraded answer as the body (`:117-120`); the transcript shows the failed lane as `## Researcher (round 1)` (`:129-136`, `:161-163`).
- `consultants/server/app.py:246-279` — `/v1/consult/{sid}/result` serves those artifacts regardless of status; `consultants/server/app.py:322-323` only marks failed if the *runner itself* crashes.

### 6. The user-perceived failure mode

- The API/session status is `failed` with `error="researcher failed: …"` (`consultants/server/runner.py:201-202`, `consultants/server/app.py:61-75`).
- The consuming skill keys on status: it only fetches `/result` **"Once `status: completed`"** (`.claude/skills/consultants/SKILL.md:104-106`) and on `failed` only surfaces the role + error (`.claude/skills/consultants/SKILL.md:136-147`).
- Net effect: the user sees "consultation failed — researcher failed: …" and **never sees the answer that two surviving lanes paid for and that is sitting in `summary.md`** — a hard-looking failure with no answer attached, i.e. the degraded answer is invisible.
- The intended mitigation for the *silent-degradation* half of this already exists and is tested: the additive tombstone (`consultants/engine/council.py:562-573`, asserted in `tests/test_consultants_council.py:531-561`).

### 7. Answers to the two posed questions

- **One lane fails (unhandled exception inside `run_loop`):** the council *does* produce a coherent-but-incomplete answer, and it is written to `summary.md`/`metadata.json` — but the consultation surfaces as `status=failed` (`consultants/server/runner.py:174`) and the skill never prints it → the user perceives a total failure with a wasted budget.
- **Two or more lanes fail in the same superstep:** `error`/`_role_failed` are LastValue channels (`consultants/engine/graph.py:74-75`), so the graph is expected to raise `InvalidUpdateError` → `consultants/server/runner.py:155-161` → `status=failed` with **only** the placeholder `(consultation failed: …)` and empty turns/models (`consultants/server/runner.py:224-225`, `:221`): total loss of the consultation.

### 8. ONE hardening change (recommended)

- **`consultants/server/runner.py:174`** — replace the blanket `terminal_status = "failed" if node_error else "completed"` with a status that distinguishes *"no answer was produced"* from *"an answer was produced but a lane failed"* (e.g. `"failed"` only when `final_state.get("final_answer")` is empty, else a `"degraded"` status that still populates `error`). This single expression is the only place where a **caught** per-lane failure is escalated to a consultation-level hard failure, and it feeds both the poll API (`consultants/server/runner.py:201`) and the persisted artifact (`storage.py:108-110`). Coupling to flag (not a second recommendation): the skill's status vocabulary (`.claude/skills/consultants/SKILL.md:87-93`, `:136-147`) must learn the new value, otherwise the agent reports an unknown state and the degraded answer stays unsurfaced.

---

## Critic (round 2)

DECISION: ready

The single-lane path is sufficiently established: `run_loop` propagates the exception (`claude_hooks/agent_loop/runner.py:122-126,230`), `researcher_node` catches it and returns an additive tombstone plus `error`/`_role_failed` (`consultants/engine/council.py:547-573`), and the reducers merge the tombstone with surviving reports (`consultants/engine/graph.py:60-75`; `consultants/engine/graph.py:375-381`). The synthesizer receives that literal tombstone as a researcher report (`consultants/engine/council.py:253-254,718-726`), so the council attempts a normal final answer rather than aborting. However, coherence is model-dependent: the code guarantees a synthesizer invocation and `final_answer` assignment, not that the generated prose acknowledges the missing lane; the prompt even says not to mention roles/process (`consultants/engine/council.py:156-167`). The user nevertheless perceives a hard failure: any lane error makes terminal status `failed` (`consultants/server/runner.py:171-190`), and the skill fetches/prints the answer only for `completed`, while for `failed` it only reports the role error (`.claude/skills/consultants/SKILL.md:104-120,134-147`). The degraded answer and tombstone are still persisted in `summary.md` and `transcript.md` (`consultants/server/runner.py:177-199`; `consultants/engine/storage.py:108-120,123-158`). The single hardening should be at `consultants/server/runner.py:174`: set status to `completed` when `final_state["final_answer"]` is present, retaining `node_error` in the result and artifacts for audit. This makes the existing skill surface the degraded answer without requiring a second status-vocabulary change. The separately discussed concurrent LastValue-channel risk is not needed to answer the stated single-lane case.

CORRECTIONS:
- report 1: claimed `.claude/skills/consultants/SKILL.md` was not found/unverified — actual file exists and its relevant status/result/failure branches are at `.claude/skills/consultants/SKILL.md:87-93,104-120,134-147`.
- report 2: claimed `.claude/skills/consultants/SKILL.md` was not found/unverified — actual file exists and confirms the reported behavior at `.claude/skills/consultants/SKILL.md:87-93,104-120,134-147`.
- report 1: claimed the council produces a “coherent answer” — actual code establishes only that the synthesizer runs and its returned text is assigned as `final_answer`; semantic coherence and explicit acknowledgment of the missing lane are not guaranteed (`consultants/engine/council.py:253-266,718-758`).
- report 2: claimed the council produces a “coherent-but-incomplete answer” — same qualification: synthesis is attempted, but coherence is model-dependent (`consultants/engine/council.py:253-266,718-758`).

---

## Synthesizer

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

---
