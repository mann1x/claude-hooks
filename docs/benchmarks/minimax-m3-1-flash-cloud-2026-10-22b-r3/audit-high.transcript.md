# Consultation transcript — csl-2026-10-22-1703-1dde

_Question_: In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:

1. CouncilState's additive reducers (consultants/engine/graph.py)
2. consultants/server/runner.py's compiled.stream loop
3. The synthesizer's input messages — what does it actually see?
4. The storage.py artifact writer

Answer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line.


## Planner

1. Trace `CouncilState` channel reducers and the `Send` payload in `consultants/engine/graph.py:50-80` and `consultants/engine/graph.py:342-404`, verifying whether an exceptioning lane returns any state update or whether LangGraph aborts the parallel superstep.
2. Inspect `researcher_node` in `consultants/engine/council.py` (not yet checked) to trace the call into `agent_loop.runner.run_loop`, identify every local `try/except` boundary, and determine how timeout exhaustion or a non-string tool-executor result becomes an `error`/`_role_failed` state versus an uncaught exception.
3. Inspect `agent_loop.runner.run_loop` in `claude_hooks/agent_loop/runner.py` (not yet checked) to verify whether its retry-budget exhaustion and tool-result type validation raise exceptions or return failure strings, and record the exact exception path.
4. Trace the exception through `compiled.stream(...)` in `consultants/server/runner.py:124-161`, determining whether LangGraph propagates a lane exception to the generator and whether the `except Exception` block sets `state.status="failed"`, writes failure artifacts, and returns before synthesis.
5. Inspect `synthesizer_node` and its input-message construction in `consultants/engine/council.py` (not yet checked), verifying whether the synthesizer ever runs after a failed fan-out lane and, if it does, whether missing lane research appears as absent evidence, an explicit error marker, or malformed messages.
6. Inspect `storage.write_consultation` and `ConsultationResult` in `consultants/engine/storage.py` (not yet checked), verifying which fields are persisted for the normal and graph-crash paths and whether the failed artifact contains a coherent answer, only `(consultation failed: ...)`, or no answer.
7. Check the Send-API and failure-path tests under `tests/` (exact files not yet identified) for a scenario where one researcher lane raises, and use the expected status/artifact assertions to confirm whether the consultation is wholly `failed` or completed with degraded research.
8. Select and justify exactly one hardening change at the narrowest verified boundary—preferably catching a researcher-lane exception and returning a structured per-lane failure update so synthesis can continue—using the confirmed `researcher_node` or graph wrapper line as the implementation site.

---

## Researcher (round 1)

## Findings — consultants Send-API fan-out failure path

**State model & reducers**
- `CouncilState` declares `research`, `turns` as `Annotated[list, operator.add]` and `research_rounds_used` / `critic_reroutes_used` / `total_prompt_tokens` / `total_completion_tokens` as `Annotated[int, operator.add]` — `consultants/engine/graph.py:64-72`.
- `error` and `_role_failed` are plain `Optional[str]` with **no reducer** (`consultants/engine/graph.py:74-75`) → LangGraph LastValue channel, i.e. at most one write per superstep.
- Fan-out router returns one `Send("researcher", {...})` per lane, capped at `FANOUT_MAX_LANES = 3` (`consultants/engine/graph.py:349-386`; `consultants/engine/council.py:307`).
- Each `Send` payload carries `plan_item` + `lane_idx` plus **zeroed** additive deltas (`research: []`, `turns: []`, counts 0) so lane deltas concat cleanly (`consultants/engine/graph.py:358-385`, esp. `:373-382`).

**Lane failure handling inside the node**
- `researcher_node` wraps the *only* `run_loop` call in try/except (`consultants/engine/council.py:547-554`) and, on exception, returns a tombstone update: `error="researcher failed: {e}"`, `_role_failed="researcher"`, `research=[f"(researcher lane failed: {e})"]`, `research_rounds_used=1`, one `RoleTurn` (`consultants/engine/council.py:555-573`).
- So an exception inside `run_loop` does **not** escape the node and does **not** abort the superstep; it is converted into a visible research delta (test asserts exactly this: `tests/test_consultants_council.py:531-561`).
- The rest of the node body is **not** protected: `_extract_text(final)` / `_usage_from(final)` run outside the try (`consultants/engine/council.py:574-576`) — a malformed `final` (e.g. non-dict) raises `AttributeError` *out of the node*, i.e. an uncaught graph-level error.

**How the two named exceptions actually manifest inside `run_loop`**
- `run_loop` has **no** try/except around `chat_fn(payload)` (`claude_hooks/agent_loop/runner.py:230`) nor around `execute_tool_calls` (`claude_hooks/agent_loop/runner.py:300`).
- HTTP timeout: `ChatClient.chat` retries `max_retries = 8` (`claude_hooks/get_advice/chat_client.py:31`, loop at `:212`) with `timeout_s = 600` per attempt (`:22`); on `URLError`/`OSError` exhaustion it re-raises (`:288-302`), on non-retryable HTTP it raises `RuntimeError` (`:283-287`). `TracedChat` re-raises unchanged (`consultants/engine/trace.py:179-181`) → propagates out of `run_loop` → caught at `consultants/engine/council.py:555`.
- Non-string tool result: `output = tool_executor(...)` then `len(output)` in the log call (`claude_hooks/agent_loop/runner.py:122-127`) → `TypeError` only for non-sized values (int/None/bool); a *sized* non-string (list/dict) passes silently into the tool message `content` (`claude_hooks/agent_loop/runner.py:129-134`) and reaches the upstream request unstringified. In production the executor cannot produce this: `caliber_proxy.tools.execute` never raises and coerces with `str(result)` (`claude_hooks/caliber_proxy/tools.py:420-439`, coercion at `:439`) — the non-string case is only reachable via an injected executor.
- Note: retry exhaustion can burn up to 9 × 600 s *per* attempt chain, and the parallel superstep waits for the slowest lane — a single timing-out lane can stall the whole fan-out for tens of minutes before the tombstone path runs.

**Runner / stream loop**
- `compiled.stream(initial, stream_mode=["updates","values"])` is wrapped in `try/except Exception` (`consultants/server/runner.py:136-161`). A graph-level exception → `state.status="failed"`, `state.error="graph crashed: {e}"`, `_write_failed_artifacts(...)`, early `return` (`:157-161`) — the synthesizer never runs.
- On the normal path, `node_error = final_state.get("error")` and `terminal_status = "failed" if node_error else "completed"` (`consultants/server/runner.py:172-174`), then `state.status = terminal_status` / `state.error = node_error` (`:201-202`) — **a single failed lane makes the whole session `status="failed"` even though the graph completed and a final answer was produced**.
- `_write_failed_artifacts` writes `final_answer="(consultation failed: {exc})"` and `turns=[]` (`consultants/server/runner.py:211-237`, esp. `:224-225`) — in a graph-crash the surviving lanes' work is lost on disk.

**What the synthesizer actually sees**
- `build_synthesizer_messages` enumerates `state["research"]` as sequential "RESEARCHER REPORT (round N)" blocks (`consultants/engine/council.py:245-266`, esp. `:253-254`). The tombstone text `(researcher lane failed: {e})` therefore appears **inline as one of the round reports**, with no lane identity, no plan-item context, and no indication that a sibling lane failed.
- `state["plan_item"]` / `lane_idx` never reach the research text — the node returns `research: [text]` only (`consultants/engine/council.py:658-664`), so the synthesizer cannot attribute findings or gaps to a specific lane. A 3-lane fan-out is presented as "rounds 1–3" that never existed as rounds.
- The critic sees the same list (`consultants/engine/council.py:231-242`, `:237-238`), so it can legitimately say `needs_more_research` about a lane that is dead.

**Artifact writer**
- `write_consultation` writes `summary.md` (front-matter with `status` + `error`, body = `final_answer`), `transcript.md` (one `## <Role>` section per `RoleTurn`, `turns` merged additively) and `metadata.json` (`consultants/engine/storage.py:200-208`, `:117-120`, `:123-158`, `:166-169`).
- The failed lane's tombstone `RoleTurn` is present in `turns` (additive) so it does show in `transcript.md` (`consultants/engine/storage.py:129-136`), and `error` lands in `metadata.json`/`summary.md` front-matter — but `retries_by_role` is always `{}` (`consultants/server/runner.py:196-197`; `consultants/engine/storage.py:71-72`), so the artifact records no evidence of how many upstream retries were burned.

**User-perceived surface**
- Poll returns `status="failed"` + `error="researcher failed: …"` (`consultants/server/app.py:61-75`; `consultants/cli.py:142-145`).
- The documented skill flow only fetches `result` on `status: completed` (`.claude/skills/consultants/SKILL.md:92-93`, `:104-132`); on `failed` it instructs the agent to surface role+error and point at `/consultants--config` (`.claude/skills/consultants/SKILL.md:136-147`). The answer *is* retrievable regardless of status (`consultants/cli.py:148-151`; `consultants/server/app.py:246-279`) but the documented flow never asks for it.

**Unverified secondary risk (evidence insufficient to execute)**
- Two lanes failing in the *same* superstep each write the reducer-less `error` / `_role_failed` channels (`consultants/engine/graph.py:74-75`), which in LangGraph's LastValue semantics is a multi-write-per-step error. I could not execute this: langgraph is not installed in this repo's test env (`tests/test_consultants_council.py:4-8`; `consultants/pyproject.toml:16-27`). If it raises, the path is `consultants/server/runner.py:155-161` → `status="failed"`, `summary.md` = `(consultation failed: …)`, `turns=[]` — the surviving lanes' findings are lost entirely. This is the only path that matches "entire consultation fails"; the single-lane path does not take it.

**Recommended single hardening change**
- `consultants/server/runner.py:174` — derive terminal status from answer availability, not from the mere presence of any node error, e.g. keep `failed` only when no usable `final_answer` was produced or the synthesizer itself failed (`node_failed == "synthesizer"`), and keep `state.error` / `metadata.error` populated for the lane failure. Rationale: the single-lane path *does* produce a coherent, degraded answer (`consultants/engine/council.py:555-573` → `:658-664` → `consultants/server/runner.py:186-199`), yet `status="failed"` (`consultants/server/runner.py:174`, `:201`) makes the skill flow discard it (`.claude/skills/consultants/SKILL.md:136-147`). Secondary gap to close later: widen the try in `consultants/engine/council.py:547-573` to cover `:574-576`, and re-verify the multi-failed-lane LastValue collision against a real langgraph install.

---

## Researcher (round 1)

## Sub-research lane 2 — findings (all cites from tool reads this turn)

### A. Where the exception is actually caught (the "unhandled" premise fails)

- `consultants/engine/council.py:547-554` — the entire `loop_runner(...)` call (`agent_loop.runner.run_loop`) sits inside `try:`.
- `consultants/engine/council.py:555-573` — `except Exception as e:` → logs, then returns a **tombstone update** instead of re-raising: `err_text = f"(researcher lane failed: {e})"` (`council.py:562`), `"error": f"researcher failed: {e}"` (`council.py:564`), `"_role_failed": "researcher"` (`council.py:565`), `"research": [err_text]` (`council.py:566`), `"research_rounds_used": 1` (`council.py:567`), plus a `RoleTurn` (`council.py:568-572`).
- Therefore the three named triggers are all caught there: chat retry exhaustion raises out of `run_loop`'s `final = chat_fn(payload)` (`claude_hooks/agent_loop/runner.py:230`) after `ChatClient` gives up (`claude_hooks/get_advice/chat_client.py:285` for HTTP, `:302` for URLError/OSError); a raising `tool_executor` propagates from `claude_hooks/agent_loop/runner.py:122`; a non-string tool result trips `len(output)` at `claude_hooks/agent_loop/runner.py:126` (the production executor coerces first — `claude_hooks/caliber_proxy/tools.py:439` — but a raw/non-coercing executor does not).
- The node wrapper does **not** add a catch-all: `consultants/engine/graph.py:332-333` → `consultants/engine/trace.py:238-244` (`traced_node`) re-raises. So exceptions raised *outside* the `try` in `researcher_node` (e.g. `final.get("_loop_messages")` at `council.py:601` on a non-dict return) **do** escape the node and abort the superstep.

### B. Reducer semantics for the fan-out merge

- `consultants/engine/graph.py:64` `research: Annotated[list, operator.add]`; `:68` `turns`; `:69` `research_rounds_used: Annotated[int, operator.add]`; `:71-72` token sums — these are the channels a tombstone can safely write.
- `consultants/engine/graph.py:74` `error: Optional[str]` and `:75` `_role_failed: Optional[str]` have **no** Annotated reducer → LangGraph default LastValue channel. **Inference (unverified):** two lanes failing in the *same* fan-out superstep both write `error`/`_role_failed`, which LastValue rejects (one value per step). langgraph is **not vendored in this repo** (`glob **/langgraph/**/*.py` → no matches; dep pinned `consultants/pyproject.toml:17` `langgraph>=0.2,<0.4`), so the exact exception could not be confirmed.
- `consultants/engine/graph.py:349-386` — fan-out router emits one `Send("researcher", {...})` per lane with `plan_item`/`lane_idx` and zeroed additive counters (`:373-381`); `:79-80` declare those as Send-only fields.
- Side effect of additive `research_rounds_used`: every lane contributes +1 (`council.py:567`, `council.py:660`), so a 2-lane run reports `research_rounds_used=2`. `council.py:383-389` then forces `route_after_critic` → synthesizer whenever `rounds_used >= caps.researcher_rounds_max` (=1 at medium, `council.py:70`). So a failed lane can never be re-run via a critic reroute.

### C. What the synthesizer actually sees

- `consultants/engine/council.py:718-723` → `build_synthesizer_messages(..., state.get("research") or [], ...)`; `council.py:253-254` renders each list element as `"\nRESEARCHER REPORT (round {i}):\n{r.strip()}"` where `i` is the **list index**, not a real round.
- So the synthesizer receives the failed lane's tombstone as one more "RESEARCHER REPORT" section, whose text is `(researcher lane failed: <exc>)` — with **no lane id** (`council.py:562` has no `lane_idx`/`plan_item`, even though both are in scope at `council.py:515`), and the round number is a positional index.
- The critic sees the same tombstone (`council.py:231-242`, `council.py:670`).
- The synthesizer's own instructions forbid surfacing the process: `council.py:164-166` ("Do NOT mention the council, the roles, or the process — the user only wants the answer"). Nothing in `build_synthesizer_messages` (`council.py:245-266`) marks the tombstone as a coverage gap. Net: the synthesizer is given one fewer lane's evidence and no reliable signal that a sub-question was dropped.

### D. Session status derivation (the user-visible contradiction)

- `consultants/server/runner.py:172-174` — `node_error = final_state.get("error")`; `terminal_status = "failed" if node_error else "completed"`. A tombstoned lane sets `error` (`council.py:564`), so **one failed lane ⇒ whole session `status="failed"`**, even though the synthesizer still ran and produced `final_answer`.
- `consultants/server/runner.py:186-199` — that same `final_answer` is written into the artifact with `status=terminal_status` and `error=node_error`.
- `consultants/server/runner.py:201-206` — `state.status`/`state.error` set to the same values; `:204-206` only logs a warning when `_role_failed` is set.
- Only a **graph-level** crash produces the "no answer" artifact: `consultants/server/runner.py:155-161` catches exceptions escaping `compiled.stream(...)` (`:137-138`) → `state.status="failed"`, `state.error=f"graph crashed: {e}"` → `_write_failed_artifacts` (`:211-237`), which writes `final_answer=f"(consultation failed: {exc})"` (`:224`), `turns=[]` (`:225`), `models={}` (`:221`) — i.e. planner output, transcript and tokens are all discarded.
- `consultants/server/app.py:313-324` — a runner-level exception is caught too → `status="failed"`, `error="runner crashed: ..."`; no artifacts are written at all in that case.

### E. Artifact writer (what lands on disk)

- `consultants/engine/storage.py:200-208` — `write_consultation` writes `summary.md`, `transcript.md`, `metadata.json` atomically.
- `consultants/engine/storage.py:97-114` — front-matter carries `status` (`:108`) and `error` (`:109-110`); body = `result.final_answer` (`:117-120`).
- `consultants/engine/storage.py:129-158` — `transcript.md` iterates `result.turns`; the failed lane's tombstone turn appears as a `## Researcher` / `## Researcher (round N)` section with content `(researcher lane failed: ...)` (`:136`).
- `consultants/engine/storage.py:166-169` — `metadata.json` = `asdict(result)` ⇒ `error`, `status`, `final_answer` and the tombstone turn all persist.
- So the *node-failure* artifact is a **full, coherent answer plus `status: failed` + `error: researcher failed: ...`**; the *graph-crash* artifact is `(consultation failed: ...)` with an empty transcript.

### F. What the user perceives

- `consultants/server/app.py:246-279` — `/v1/consult/{sid}/result` returns `summary_markdown` + `metadata` whenever the files exist, independent of status; `consultants/cli.py:148-151` prints it; `.claude/skills/consultants--show/SKILL.md:31-38` prints the answer verbatim and then a footer containing `status: completed|failed`.
- `scripts/consultants_benchmark.sh:195-197` treats `completed|failed` as terminal and writes it into the results table (`scripts/consultants_bench_row.py:134`, `:148`) — so a run that *did* answer is recorded as `failed`.
- `consultants/engine/sessions_index.py:82`, `:127` persist that same status into the per-project index.

### G. Tests (coverage shape)

- `tests/test_consultants_council.py:514-529` — loop failure ⇒ `update["error"]` contains "researcher failed".
- `tests/test_consultants_council.py:531-561` — the hardening test (audit-high `csl-...-faa2`): the tombstone must land in `research` ("researcher lane failed" + "HTTP timeout"), contribute a `turns` entry, and bump `research_rounds_used` by 1.
- `tests/test_consultants_council.py:477-512` — Send-lane focus (`plan_item` ⇒ only that lane's item, `Sub-research lane 2`).
- `tests/test_consultants_council.py:649-677` — synthesizer failure ⇒ placeholder `final_answer` + tombstone turn.
- `tests/test_consultants_storage.py:121-136` — `status: failed` + `error:` render into front-matter.
- `tests/test_consultants_server.py:284-297` — runner crash ⇒ `status == "failed"` (stub runner, not a real graph).
- **Gap:** no test drives a real compiled graph with one failing lane; there is no test asserting the artifact/status/`final_answer` combination for the lane-failure path. `tests/test_consultants_council.py:738-755` shows graph-level tests are skipped when langgraph is absent.

### H. Real-run evidence (fan-out does work)

- `.claude-hooks/consultants/csl-2026-10-22-1701-a0f1/metadata.json:47` and `:59` — two `role: "researcher"` turns in one session (fan-out merged), `:23` `status: "completed"`, `:8` `error: null`. So the Send merge is proven; the *failing* lane is not.
- Caveat: that artifact contains fields the repo's `ConsultationResult` does not define (`failure_class` `:33`, `stop_reason` `:49`, `cwd_display` `:4`, `decision` `:5`) — the running service build differs from this repo snapshot.

### I. Evidence gaps (explicit)

- langgraph source unavailable → the multi-lane `LastValue` conflict for `error`/`_role_failed` is an inference from `consultants/engine/graph.py:74-75` (no Annotated reducer), not a verified exception.
- No repo test or artifact demonstrates a failed lane end-to-end; the "coherent answer + status=failed" combination is derived from `council.py:562-573` + `runner.py:172-199` + `storage.py:97-120`.

### Recommendation — ONE hardening change

**Make the per-lane researcher failure an additive, lane-scoped record instead of a shared LastValue write — implement at `consultants/engine/council.py:562-573` (the tombstone return), with the new channel declared beside `consultants/engine/graph.py:74`.**

- Why this site: it is the narrowest boundary that already owns the failure (`council.py:555` `except`), so the change needs no new plumbing, and it fixes three coupled defects at once: (1) two lanes failing in the same superstep no longer both write the non-additive `error`/`_role_failed` channels (`graph.py:74-75`); (2) the marker becomes lane-scoped — `council.py:515` already has `plan_item`/`lane_idx` in scope, so the synthesizer can be told *which* sub-question is missing instead of receiving an unlabeled `(researcher lane failed: ...)` report at `council.py:253-254`; (3) `runner.py:172-174` can then distinguish "one lane degraded" from "consultation failed" instead of collapsing both into `status=failed` while still shipping a full answer in `summary.md`.
- Do **not** widen the change: `council.py:585-639` (empty-text fallback) and `council.py:383-389` (rounds-cap suppression of critic reroutes) are separate defects and should stay out.

---

## Critic (round 2)

DECISION: ready

The evidence establishes the single-lane path sufficiently: an exception raised by `run_loop` is caught by `researcher_node` and converted into a tombstone research delta plus `error`/`_role_failed` (`consultants/engine/council.py:547-573`); additive channels merge that delta with surviving lanes (`consultants/engine/graph.py:60-72`), while the reducer-less error channels are only relevant to the separately flagged, unverified multi-lane conflict. The graph therefore continues to synthesis, whose messages include the tombstone as a positional “RESEARCHER REPORT” (`consultants/engine/council.py:245-266`). The runner nevertheless labels the completed consultation `status="failed"` because `error` is set (`consultants/server/runner.py:171-202`), while still persisting the synthesizer’s answer and transcript (`consultants/server/runner.py:176-199`; `consultants/engine/storage.py:200-208`). Thus the consultation does not wholly abort in the named single-lane case: it produces a degraded answer, but the user-facing terminal status is `failed`; only an exception escaping `compiled.stream` takes the graph-crash path, replaces the answer with `(consultation failed: ...)`, and discards turns (`consultants/server/runner.py:136-161,211-237`). The one recommended hardening change should be at `consultants/server/runner.py:171-174`: derive terminal status from whether a usable final answer exists, preserving the lane error separately, so a degraded-but-answered consultation is not reported as wholly failed.

---

## Synthesizer

**Bottom line:** For a single researcher lane, the consultation does **not** wholly abort: `researcher_node` catches the exception, records a tombstone, and the synthesizer still produces an answer. However, the server labels the entire consultation `status="failed"` even though that answer and the surviving lanes’ work are persisted. The user therefore sees a usable but degraded answer presented as a failed consultation.

1. **Failure and reducer path**

   The `run_loop` call is inside `researcher_node`’s `try` block (`consultants/engine/council.py:547-554`). Therefore, a retry-exhausted HTTP exception or an exception raised by an injected tool executor is caught at `consultants/engine/council.py:555-573`; it does not escape that node.

   The catch returns:

   - `error="researcher failed: …"`
   - `_role_failed="researcher"`
   - one `research` tombstone, `(researcher lane failed: …)`
   - `research_rounds_used=1`
   - one researcher `RoleTurn` containing the tombstone  
   (`consultants/engine/council.py:562-573`).

   `research`, `turns`, and `research_rounds_used` are additive channels, so this tombstone and the surviving lanes’ results merge (`consultants/engine/graph.py:60-72`). The lane-specific Send payload includes `plan_item` and `lane_idx`, but the failure record itself does not retain either identifier (`consultants/engine/graph.py:76-80`; `consultants/engine/council.py:562-573`).

   `error` and `_role_failed` have no reducer (`consultants/engine/graph.py:74-75`). Consequently, two lanes failing in the same parallel superstep may conflict under LangGraph’s last-value semantics. That specific multi-lane failure is **unverified** because the available test environment lacks LangGraph; it must not be conflated with the ordinary single-lane path.

2. **`compiled.stream` behavior**

   For the named single-lane case, no exception reaches `compiled.stream`, so the graph continues through the remaining pipeline. The stream loop accumulates merged state from LangGraph’s `"values"` stream mode specifically to preserve additive fan-out results (`consultants/server/runner.py:124-140`).

   If an exception really escapes the graph—such as an exception raised after the `researcher_node` catch boundary—the server catches it around `compiled.stream` (`consultants/server/runner.py:136-161`). That graph-crash path sets `status="failed"`, records `error="graph crashed: …"`, writes the failure artifacts, and returns before synthesis (`consultants/server/runner.py:155-161`).

3. **What the synthesizer sees**

   The synthesizer receives the merged `state["research"]` list (`consultants/engine/council.py:718-723`). Each element is rendered as a positional `RESEARCHER REPORT (round N)` section (`consultants/engine/council.py:245-266`).

   Thus, the failed lane appears to the synthesizer as an ordinary report whose body is:

   ```text
   (researcher lane failed: <exception>)
   ```

   It is not identified by lane number or plan item, and the “round N” label is merely the element’s list position (`consultants/engine/council.py:253-254`; `consultants/engine/council.py:562-566`). The synthesizer can therefore produce a syntactically complete answer from the surviving evidence, but it has no reliable way to associate the missing evidence with the specific sub-question that failed.

4. **Storage and user-visible failure mode**

   The normal completion path stores the synthesizer’s `final_answer`, all merged turns, the lane error, and the derived status (`consultants/server/runner.py:171-199`). Because the failed lane populated `error`, the runner unconditionally derives `terminal_status="failed"` (`consultants/server/runner.py:171-174`).

   The artifacts therefore contain:

   - `summary.md`: front matter with `status: failed` and the researcher error, followed by the synthesizer’s complete answer (`consultants/engine/storage.py:97-120`);
   - `transcript.md`: the surviving turns plus the failed researcher’s tombstone turn (`consultants/engine/storage.py:123-158`);
   - `metadata.json`: the serialized result, including `status`, `error`, `final_answer`, and turns (`consultants/engine/storage.py:166-169 [no final_answer at this line; line is in render_metadata]`).

   All three files are written atomically through `write_consultation` (`consultants/engine/storage.py:200-208`).

   The perceived failure mode is therefore: **the answer exists, but the consultation is reported as wholly failed because one supporting lane failed**. A result-retrieval client that exposes the stored answer can return the degraded answer, while a status-gated client may withhold it because the terminal status is `failed` (`consultants/server/app.py:246-279 [no failed at this line; line is in create_app]`).

   By contrast, a graph-level crash does not produce a coherent answer: the failure artifact substitutes `(consultation failed: …)` and discards all turns (`consultants/server/runner.py:211-237`).

**Exactly one recommended hardening change**

At `consultants/server/runner.py:171-174`, derive the terminal status from whether a usable `final_answer` was actually produced, while retaining the researcher lane’s error separately. A consultation with a synthesizer answer and only a researcher-lane failure should be completed-but-degraded, not labeled as having produced no consultation result. This is the narrowest change that corrects the contradictory user-visible outcome without changing fan-out, synthesis, or storage semantics.

---
