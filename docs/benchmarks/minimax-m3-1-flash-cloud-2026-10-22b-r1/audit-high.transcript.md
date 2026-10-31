# Consultation transcript — csl-2026-10-22-1618-1e3a

_Question_: In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:

1. CouncilState's additive reducers (consultants/engine/graph.py)
2. consultants/server/runner.py's compiled.stream loop
3. The synthesizer's input messages — what does it actually see?
4. The storage.py artifact writer

Answer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line.


## Planner

1. Trace `researcher_node` in `consultants/engine/council.py:481-573` to verify that an exception from `run_loop` is caught and converted into a tombstone update containing `error`, `_role_failed`, one `research` entry, `research_rounds_used=1`, and a failed `RoleTurn` (`consultants/engine/council.py:547-573`).
2. Inspect the `CouncilState` channel declarations in `consultants/engine/graph.py:50-80` and the `Send` payload construction in `consultants/engine/graph.py:349-385` to verify how surviving-lane and failed-lane `research`, `turns`, counters, `error`, and `_role_failed` values merge after parallel fan-out.
3. Verify LangGraph’s error semantics around the researcher node and `compiled.stream` in `consultants/server/runner.py:124-161`, specifically whether a returned node error is merged into state or propagates as an exception that triggers the `except Exception` block.
4. Inspect `build_synthesizer_messages` and `synthesizer_node` in `consultants/engine/council.py:245-266,715-758` to determine the exact synthesizer inputs, including the failed researcher tombstone text, and whether `state["error"]` is included in the prompt.
5. Inspect `consultants/engine/storage.py`—not read in the available turn—to verify how `ConsultationResult`, `RoleTurn`, `write_consultation`, and artifact files serialize a completed consultation containing both surviving and failed researcher turns.
6. Trace the runner’s post-stream status and artifact path in `consultants/server/runner.py:163-205` to establish whether `final_state["error"]` produces `status="failed"` despite a synthesizer answer, versus the graph-crash path at `consultants/server/runner.py:155-161,211-237`.
7. Inspect the Send-API exception-handling configuration or installed LangGraph behavior around `StateGraph`/`Send` in `consultants/engine/graph.py:310-429` and relevant project tests, to verify whether one unhandled lane exception aborts the superstep or is isolated, then recommend exactly one hardening change at the affected line.

---

## Researcher (round 1)

## Verdict

The council does **not** abort — but the session is nonetheless reported `status=failed` **with a complete answer sitting in `summary.md`**. That contradiction is the real failure mode.

---

## Findings

**A. The lane exception is fully contained inside the node**
- `run_loop` has no internal guard around the chat call (`claude_hooks/agent_loop/runner.py:230`) or the tool executor (`claude_hooks/agent_loop/runner.py:122`); a non-string tool result blows up at `len(output)` (`claude_hooks/agent_loop/runner.py:125`) — `traced_tool` returns the executor's value uncoerced (`consultants/engine/trace.py:218-219`), while caliber's own `execute` does coerce (`claude_hooks/caliber_proxy/tools.py:439`).
- `researcher_node` wraps `loop_runner` in `except Exception` and returns a tombstone update — it never re-raises (`consultants/engine/council.py:547-573`): `error="researcher failed: …"`, `_role_failed="researcher"`, `research=[err_text]`, `research_rounds_used=1`, one failed `RoleTurn` (round=1, because the Send payload seeds `research_rounds_used: 0`, `consultants/engine/graph.py:379`).
- Pinned by tests: `tests/test_consultants_council.py:531-561` (tombstone in `research`, `"researcher lane failed"` text, `research_rounds_used == 1`).

**B. Reducer merge outcome (3 lanes, 1 dead)**
- `research` list-concat (`consultants/engine/graph.py:64`), `turns` list-concat (`:68`), `research_rounds_used` int-add (`:69`); `error` and `_role_failed` are plain `Optional[str]` single-value channels (`:74-75`).
- Merged state: `research = [laneA, "(researcher lane failed: …)", laneB]` in completion order (`:64`); `turns` = 3 entries; `research_rounds_used = 3` — it counts **lanes, not rounds** (1 from the tombstone `consultants/engine/council.py:567` + 1 per surviving lane `:660`).
- Consequence of the inflated counter: `route_after_critic` gates re-routes on `rounds_used >= caps.researcher_rounds_max` (`consultants/engine/council.py:383-389`) and the `high` cap is 3 (`consultants/engine/council.py:72`) → with 3 lanes the critic's "needs_more_research" can **never** re-route; the verdict is dropped.

**C. `compiled.stream` never sees the exception**
- Because the node returns a dict instead of raising, the `try/except` around `compiled.stream` (`consultants/server/runner.py:136-161`) is not entered; the "graph crashed" branch (`:155-161`) is reserved for graph-level faults.
- `final_state` is taken from the `"values"` stream mode (`consultants/server/runner.py:137-140`), so it holds the merged `research` list **and** `error` **and** `final_answer`.
- The lane failure is then converted to a terminal verdict by `terminal_status = "failed" if node_error else "completed"` (`consultants/server/runner.py:172-174`) → `state.status = "failed"`, `state.error = "researcher failed: …"` (`:201-203`).

**D. What the synthesizer actually sees**
- `synthesizer_node` passes only question / plan / `state["research"]` / critique (`consultants/engine/council.py:718-723`); `build_synthesizer_messages` renders each research entry as a `RESEARCHER REPORT (round i):` block (`consultants/engine/council.py:253-254`).
- So the synthesizer sees the tombstone literally as one of the reports: `"(researcher lane failed: <exc>)"` — unlabeled, no lane identity, no "this sub-question is unanswered" framing. `state["error"]` is **not** in the prompt (the builder takes no error arg, `consultants/engine/council.py:245-248`).
- The synthesizer prompt forbids discussing the process (`consultants/engine/council.py:164-165`; self-critic variant `:189-190`) → the dead lane's sub-topic is silently absent from the answer rather than flagged.

**E. Artifacts**
- `write_consultation` is called with the *degraded* answer and the failed verdict: `final_answer=final_state.get("final_answer","")` (`consultants/server/runner.py:186`), `status=terminal_status` (`:189`), `error=node_error` (`:190`).
- `summary.md` front-matter gets `status: "failed"` + `error: "researcher failed: …"` (`consultants/engine/storage.py:108-110`) while the body is the synthesizer's real answer (`consultants/engine/storage.py:117-120`).
- `metadata.json` carries `status=failed` + `error` + the full `final_answer` (`consultants/engine/storage.py:166-169`).
- `transcript.md` contains the failed lane's tombstone turn next to the surviving lanes' turns (`consultants/engine/storage.py:129-136`, turn built at `consultants/engine/council.py:568-572`).

**F. Perceived failure mode**
- Poll returns `status: failed` + the researcher error (`consultants/server/runner.py:201-203` → `consultants/server/app.py:61-75`).
- The `/consultants` skill fetches the result **only** on `completed` (`.claude/skills/consultants/SKILL.md:92`, `:104-110`) and on `failed` tells the user to surface the role error and switch that role's model (`.claude/skills/consultants/SKILL.md:136-144`).
- Net user experience: "consultation failed — researcher failed: HTTP timeout…" and **no answer**, even though two of three lanes' findings and a real synthesized answer are on disk. The wasted work is invisible.

**G. Flagged risk (externally dependent, not verifiable in-repo)**
- `error` / `_role_failed` are single-value channels (`consultants/engine/graph.py:74-75`). If **two** lanes fail in the same superstep, LangGraph's LastValue channel rejects concurrent writes (`InvalidUpdateError`, "can receive only one value per step") → the graph aborts, `consultants/server/runner.py:155-161` writes `status=failed` with `"(consultation failed: …)"` and **no answer at all**. LangGraph is imported only at `consultants/engine/graph.py:241-242` and pinned `>=0.2,<0.4` (`consultants/pyproject.toml:17`); it is not vendored here, so I could not confirm this from repo source — high confidence, unverified in this repo.

---

## Recommended single hardening change

**`consultants/server/runner.py:174`** — stop letting a surviving council answer be reported as a terminal failure:

```python
terminal_status = "failed" if node_error else "completed"
```

Derive `terminal_status` from whether a usable answer exists (`final_state.get("final_answer")`), reserving `"failed"` for the case where no answer was produced (e.g. `_role_failed == "synthesizer"`, whose tombstone answer is the `(consultation incomplete: …)` placeholder from `consultants/engine/council.py:739-741`), while keeping `node_error` in `state.error` and in the artifact front-matter (`consultants/engine/storage.py:109-110`) so the researcher-lane failure stays visible to the user.

Why this one: the tombstone already guarantees the synthesizer sees the gap (`consultants/engine/council.py:566` → `:253-254`), and the artifacts already contain a coherent, degraded answer — the only thing standing between the user and that answer is the one-line status computation at `consultants/server/runner.py:174`, which the skill then acts on (`.claude/skills/consultants/SKILL.md:92`, `:136-144`). Fixing the reducer on `consultants/engine/graph.py:74-75` would additionally close risk G, but it does not change what the user perceives in the single-lane-failure case.

---

## Researcher (round 1)

## Findings (researcher lane 2)

**Failure classification — where the exception actually lands**

- `run_loop` is called inside a `try` in `researcher_node` — `consultants/engine/council.py:547-554`; any exception from `agent_loop.runner.run_loop` (chat timeout after retries: `claude_hooks/get_advice/chat_client.py:285-302` raises `RuntimeError`/re-raises `OSError`; `TypeError` from `len(output)` on a non-string tool result at `claude_hooks/agent_loop/runner.py:122-127`) propagates out of `run_loop` and is **caught** at `consultants/engine/council.py:555`.
- The caught exception produces a tombstone update: `error`, `_role_failed="researcher"`, `research=[err_text]`, `research_rounds_used=1`, and a `RoleTurn` whose content is `(researcher lane failed: {e})` — `consultants/engine/council.py:562-572`.
- The production tool executor cannot produce a non-string: `claude_hooks/caliber_proxy/tools.py:439` coerces (`str(result)`) and `:436-438` swallows tool exceptions into `"error: ..."` strings; wired at `consultants/server/runner.py:106`. So the non-string scenario is only reachable with an injected executor.
- Uncaught-exception gap: the *post-loop* parsing is **outside** the try — `text = _extract_text(final)` and `pt, ct = _usage_from(final)` at `consultants/engine/council.py:575-576`, and `final.get("_loop_messages")` at `:601`. A malformed `final` (non-dict) raises here → escapes the node → LangGraph aborts the run → caught by the runner's graph-crash handler `consultants/server/runner.py:155-161`.

**1. CouncilState reducers (`consultants/engine/graph.py`)**

- `research` (`:64`), `turns` (`:68`), `research_rounds_used` (`:69`), `total_*_tokens` (`:71-72`) are additive (`operator.add`); `error` (`:74`) and `_role_failed` (`:75`) are single-value (`Optional[str]`, no reducer).
- Fan-out lanes are `Send("researcher", {...})` with `research: []`, `turns: []`, `research_rounds_used: 0` seeds — `consultants/engine/graph.py:358-385` (seeds at `:377-381`).
- Consequence: a failed lane's tombstone merges cleanly with surviving lanes; the `error` channel is written **once** (only the failing lane), so no concurrent-write problem in the single-lane case. Caveat (unverified in-repo — langgraph is not installed here; pinned `langgraph>=0.2,<0.4` at `consultants/pyproject.toml:17`): `error`/`_role_failed` are LastValue channels, so **two** lanes failing in the same superstep would issue two concurrent writes to `consultants/engine/graph.py:74-75` and could raise `InvalidUpdateError`, which would abort the whole graph. I could not confirm LangGraph's runtime behavior in this environment.
- The additive `research_rounds_used` counts *lanes*, not rounds (`consultants/engine/council.py:567` returns 1 per lane, `consultants/engine/graph.py:69` adds) — so after a 3-lane fan-out the counter is 3, which already exceeds `researcher_rounds_max=1` at medium effort (`consultants/engine/council.py:70`) and forces `route_after_critic` → synthesizer (`consultants/engine/council.py:383-389`). The failed lane is never retried.

**2. `compiled.stream` loop (`consultants/server/runner.py`)**

- Stream is consumed with `stream_mode=["updates","values"]`; `final_state` is taken from the last `values` payload — `consultants/server/runner.py:137-141`. So the merged post-fan-out state (surviving reports + tombstone) is what the runner sees.
- A node-level exception (as opposed to a node *returning* an error) is caught at `consultants/server/runner.py:155-161` → `state.status="failed"`, `state.error="graph crashed: {e}"`, and `_write_failed_artifacts` (`:211-237`) writes `status="failed"`, `final_answer="(consultation failed: {exc})"`, `turns=[]`, `models={}` — i.e. **all** surviving-lane work and the answer are discarded.
- Normal path: `node_error = final_state.get("error")` (`:172`) and `terminal_status = "failed" if node_error else "completed"` (`:174`) — the mere presence of a researcher's `error` string sets terminal status to **failed** even though `final_answer` is populated (`:186`). `state.status = terminal_status` (`:201`), `state.error = node_error` (`:202`). `_role_failed` is only logged as a warning (`:204-206`).

**3. Synthesizer's actual input**

- `build_synthesizer_messages` renders every element of `state["research"]` as a numbered `RESEARCHER REPORT (round N)` block — `consultants/engine/council.py:245-266` (loop at `:253-254`).
- The failed lane therefore appears as one extra report whose text is `(researcher lane failed: <exception>)` (`consultants/engine/council.py:562`). It carries **no lane identity** — no `lane_idx`, no sub-plan text — so the synthesizer cannot tell which sub-question of the plan is missing.
- The critic sees the same tombstone (`consultants/engine/council.py:231-242`, `:237-238`).
- The synthesizer prompt forbids mentioning the pipeline and demands a confident answer — `consultants/engine/council.py:156-168` ("Do NOT mention the council, the roles, or the process"). So the gap is silently absorbed: the answer silently omits the failed sub-topic, or leaks the raw exception text into user-facing prose.

**4. Artifact writer (`consultants/engine/storage.py`)**

- `write_consultation` writes `summary.md`, `transcript.md`, `metadata.json` atomically — `consultants/engine/storage.py:200-208`, atomic rename at `:174-192`.
- `summary.md` = YAML front matter + the synthesizer's answer body — `consultants/engine/storage.py:117-120`; front matter carries `status: failed` and `error: researcher failed: …` (`:108-110`). So the good answer **is** on disk, but tagged failed.
- `transcript.md` does show the failure: the tombstone turn's content is rendered (`:129-136`), and `_is_multi_round` (`:161-163`) labels repeated researcher turns as separate rounds.
- `metadata.json` = `asdict(result)` (`:166-169`) → includes `status`, `error`, all turns, token totals.

**Net answer to the question**

- The council *does* still produce a coherent, complete answer: the failing lane is caught at `consultants/engine/council.py:555`, the graph continues to critic and synthesizer, and `final_answer` is written to `summary.md` (`consultants/server/runner.py:186`, `consultants/engine/storage.py:119`).
- But the user **perceives total failure**: `terminal_status` becomes `"failed"` solely because `final_state["error"]` is set (`consultants/server/runner.py:172-174,201`), so the status poll reports `failed` (`consultants/server/app.py:61-75`, `:231-243`) and the agent-facing skill flow only fetches the result when `status: completed` (`.claude/skills/consultants/SKILL.md:104-107`) and otherwise tells the agent to surface the role error and go fix the model config (`.claude/skills/consultants/SKILL.md:134-144`). The on-disk answer is never shown.
- Secondary degradation: the surviving lanes' evidence is silently incomplete; the synthesizer has no marker that a sub-plan was skipped (`consultants/engine/council.py:562`, no lane label), and the failed lane is never retried (`consultants/engine/council.py:383-389`).
- Only in the graph-crash sub-case (post-`run_loop` parse error, `consultants/engine/council.py:575-576`, or two lanes failing concurrently into the LastValue channels `consultants/engine/graph.py:74-75`) does everything collapse to `status=failed` with an empty transcript (`consultants/server/runner.py:211-237`).

**ONE recommended hardening change**

- `consultants/server/runner.py:174` — replace `terminal_status = "failed" if node_error else "completed"` with a distinction between *terminal* and *degraded* failures, e.g. treat the session as `failed` only when the failing role is the synthesizer or no `final_answer` was produced, and otherwise as `completed` while keeping `node_error` in `state.error` / metadata (`:190`, `:202`). Rationale: the answer already exists (`consultants/engine/council.py:753-758` → `consultants/server/runner.py:186` → `consultants/engine/storage.py:119`); only the status conflation hides it from the user, and the skill contract (`.claude/skills/consultants/SKILL.md:92-93,104-107`) will then deliver it.

---

## Critic (round 2)

DECISION: ready

For an exception raised **inside `run_loop`**, `researcher_node` catches it and returns a tombstone containing `error`, `_role_failed="researcher"`, one research string, one failed `RoleTurn`, and a research-round increment (`consultants/engine/council.py:547-573`). The additive `research`/`turns` reducers merge that tombstone with surviving lanes, while `error` and `_role_failed` are single-value channels (`consultants/engine/graph.py:60-75`). The graph therefore continues; `compiled.stream` does not enter its graph-crash handler, and its final merged state includes both the tombstone and the synthesized `final_answer` (`consultants/server/runner.py:124-161`).

The synthesizer receives the tombstone literally as another “RESEARCHER REPORT,” but without the failed lane’s identity or sub-question; `state["error"]` itself is not included (`consultants/engine/council.py:245-266`, `consultants/engine/council.py:715-723`). It can therefore produce a useful but degraded answer that silently omits the failed subtopic, potentially leaking the raw exception despite the prompt discouraging discussion of role/process failures (`consultants/engine/council.py:156-168`).

The runner nevertheless classifies the consultation as `status="failed"` solely because `final_state["error"]` is set (`consultants/server/runner.py:171-174`, `consultants/server/runner.py:186-202`). The artifact writer persists the synthesized body while marking the artifact failed and retaining the error (`consultants/engine/storage.py:108-120`, `consultants/engine/storage.py:129-136`, `consultants/engine/storage.py:166-169`, `consultants/engine/storage.py:200-208`). Thus the user/API perceives a failed consultation even though a degraded synthesized answer exists on disk. The graph-crash path instead discards surviving work and writes only a generic failure answer (`consultants/server/runner.py:155-161`, `consultants/server/runner.py:211-237`).

Exactly one hardening change: at `consultants/server/runner.py:174`, derive terminal failure from absence of a usable `final_answer`/terminal synthesizer failure rather than from any `error`; retain the researcher error in `state.error` and artifacts. This exposes the already-produced degraded answer while preserving failure visibility.

The concurrent multi-lane-failure behavior of LangGraph’s single-value channels was not verified, but it is outside the specified single-exception path and is not load-bearing here.

---

## Synthesizer

**The council does not abort, but the consultation is nevertheless surfaced as `status="failed"` even though a degraded synthesized answer exists.** The user therefore perceives a total failure, while `summary.md` contains the answer.

- **Failure capture:** An exception raised inside `run_loop` is caught by `researcher_node`; it does not propagate out of the node. The catch returns a tombstone containing `error`, `_role_failed="researcher"`, one research entry, `research_rounds_used=1`, and a failed researcher turn (`consultants/engine/council.py:547-573`).

- **Reducer merge:** `research` and `turns` use additive list reducers, and `research_rounds_used` uses integer addition, so the tombstone merges with the surviving lanes’ results (`consultants/engine/graph.py:60-69`). `error` and `_role_failed` are single-value channels (`consultants/engine/graph.py:74-75`). Thus the final state contains the surviving research, the literal failure report, the researcher error, and a synthesized `final_answer`.

- **Streaming path:** Because the failing researcher returned a state update rather than raising, `compiled.stream` continues through the graph and does not enter its graph-crash handler. The runner takes the last merged `"values"` payload as `final_state` (`consultants/server/runner.py:124-161`).

- **What the synthesizer sees:** Every element of `state["research"]` becomes a `RESEARCHER REPORT (round N)` block (`consultants/engine/council.py:245-266`). Therefore, the synthesizer sees the tombstone literally as:

  ```text
  RESEARCHER REPORT (round N):
  (researcher lane failed: <exception>)
  ```

  The tombstone does not identify the failed lane or its sub-question, and `state["error"]` is not passed into the message builder (`consultants/engine/council.py:245-248`, `consultants/engine/council.py:715-723`). Consequently, the answer may be usable, but it is not guaranteed to be complete or coherent for the missing subtopic: it may silently omit that topic or expose the raw exception in the answer.

- **User-visible result:** The runner classifies the consultation as failed whenever any `error` string exists, regardless of whether `final_answer` was produced (`consultants/server/runner.py:171-174 [no final_answer at this line; line is in run_council]`, `consultants/server/runner.py:186-202`). It persists that synthesized answer, but with `status="failed"` and the researcher error (`consultants/server/runner.py:176-199`).

- **Artifacts:** `summary.md` contains both failed front matter and the actual synthesized body (`consultants/engine/storage.py:108-120`). `transcript.md` includes the failed researcher tombstone alongside the surviving turns (`consultants/engine/storage.py:129-136`), while `metadata.json` serializes the result, including its failed status, error, turns, and final answer (`consultants/engine/storage.py:166-169`).

- **True graph-crash distinction:** If the graph itself raises—such as an exception outside the guarded `run_loop` call—the runner instead writes a generic failed answer, no turns, and discards surviving work (`consultants/server/runner.py:155-161`, `consultants/server/runner.py:211-237`). That is distinct from the specified exception inside `run_loop`.

**Exactly one hardening change:** change the status calculation at `consultants/server/runner.py:174` so a researcher-lane error does not automatically make a usable synthesized answer terminal. Treat the consultation as failed only when there is no usable `final_answer` or the synthesizer itself failed; otherwise return the degraded answer while retaining the researcher error in `state.error` and the artifacts. The existing error persistence remains at `consultants/server/runner.py:189-202` and `consultants/engine/storage.py:108-110`.

---
