# Consultation transcript — csl-2026-10-27-1448-4ba4

_Question_: In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:

1. CouncilState's additive reducers (consultants/engine/graph.py)
2. consultants/server/runner.py's compiled.stream loop
3. The synthesizer's input messages — what does it actually see?
4. The storage.py artifact writer

Answer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line.


## Planner

1. Examine `claude_hooks/agent_loop/runner.py:run_loop` to determine if exceptions are caught internally or propagated to the caller.
2. Trace the exception propagation in `consultants/server/runner.py` within the `compiled.stream` loop to see if a failure in one parallel lane crashes the entire stream or is isolated.
3. Analyze `consultants/engine/graph.py` for `CouncilState` additive reducers to see how they handle missing or errored researcher outputs during state aggregation.
4. Inspect the logic that constructs the synthesizer's input messages to identify what the synthesizer sees when a researcher lane fails.
5. Examine `consultants/engine/storage.py` to verify how the artifact writer handles failed lanes (e.g., writing error logs vs. omitting the artifact).
6. Determine the final status return value in the `compiled.stream` path to see if it surfaces as `status=failed` or returns a degraded answer.

---

## Researcher (round 1)

## Findings

**1. `run_loop` does not catch exceptions — they propagate to `researcher_node`.**
- `claude_hooks/agent_loop/runner.py:138` — `run_loop` has no try/except around its body. `chat_fn(payload)` is called bare at `claude_hooks/agent_loop/runner.py:230`; a chat_fn raising after exhausting retries propagates straight out.
- Tool-executor non-string: `output = tool_executor(...)` at `claude_hooks/agent_loop/runner.py:122`, then `len(output)` at `runner.py:126` — a non-string return raises `TypeError` inside run_loop and propagates the same way.

**2. `researcher_node` converts the exception into a tombstone delta — the lane is isolated, the graph does NOT crash.**
- `consultants/engine/council.py:547-556` — `loop_runner(...)` is wrapped in `try/except Exception`; on failure it logs and returns a tombstone (`consultants/engine/council.py:562-573`): `error: "researcher failed: {e}"`, `_role_failed: "researcher"`, `research: ["(researcher lane failed: {e})"]`, `research_rounds_used: 1`, and a zero-token `RoleTurn`. The comment at `council.py:557-561` cites audit-high `csl-...-faa2`: without the tombstone the synthesizer would "write a confidently-degraded answer over the surviving lanes."
- Regression-tested: `tests/test_consultants_council.py:531-561` (`test_loop_failure_emits_tombstone_research`) asserts the tombstone lands in the additive `research` list.

**3. Additive reducers merge the tombstone cleanly with surviving lanes.**
- `consultants/engine/graph.py:64` — `research: Annotated[list, operator.add]`; `graph.py:68` — `turns: Annotated[list, operator.add]`; token counters at `graph.py:71-72`. Fan-out lanes are spawned via `Send` at `graph.py:358-385` with empty `research`/`turns` deltas, so the failed lane's tombstone concatenates alongside the surviving lanes' findings.
- Caveat: `error` at `graph.py:74` is a plain channel (last-write-wins, no reducer) — if two lanes fail, only one error string survives in final state.

**4. `compiled.stream` loop: no exception raised, so the crash branch is never taken — but the run is still marked failed.**
- `consultants/server/runner.py:136-161` — the `except Exception` at `runner.py:155` only fires if a node escapes; since `researcher_node` catches, `status="failed"` via that path does not happen.
- However `runner.py:172-174` — `node_error = final_state.get("error")`; `terminal_status = "failed" if node_error else "completed"`. The tombstone's `error` key makes `node_error` truthy → **status="failed"** even though the synthesizer produced a real answer. `runner.py:201-202` sets `state.status`/`state.error` accordingly.

**5. Synthesizer input: it sees surviving lanes' reports PLUS the literal tombstone string.**
- `consultants/engine/council.py:718-723` — `build_synthesizer_messages(question, plan, state["research"], critique)`; `research` contains `"(researcher lane failed: HTTP timeout exhausted retries)"` inline next to surviving lanes' findings. So the synthesizer can explicitly caveat the gap — the answer is coherent and gap-aware, not silently degraded.

**6. Storage: artifacts are written normally, with the failure recorded.**
- `consultants/server/runner.py:177-199` — `ConsultationResult` gets `final_answer=final_state.get("final_answer")` (the real synthesized answer), `status=terminal_status` ("failed"), `error=node_error`; `storage.write_consultation` at `runner.py:199`.
- `consultants/engine/storage.py:108` — `status: failed` in summary.md YAML front-matter; `storage.py:109-110` — `error:` line; `storage.py:117-120` — body is the real final answer. Transcript (`storage.py:123-158`) includes the tombstone RoleTurn. Writes are atomic (`storage.py:174-192`).

## Verdict

The council does **not** fail wholesale and does **not** crash the stream. It produces a coherent, gap-aware final answer (synthesizer sees surviving lanes + explicit tombstone). But the consultation is **labeled `status="failed"`** with `error="researcher failed: …"` because `runner.py:174` treats any node-level `error` as terminal — a lane-level researcher failure is conflated with a terminal planner/synthesizer failure. User-perceived failure mode: a summary.md containing a usable answer whose front-matter says `status: failed` — degraded labeling, not a degraded answer (the reverse of the pre-faa2 bug, where the answer was degraded but labeled clean).

## One hardening change

**`consultants/server/runner.py:174`** — only treat the run as terminal-failure when a terminal role failed or no answer exists:

```python
terminal_status = (
    "failed"
    if node_error and (
        final_state.get("_role_failed") in ("planner", "synthesizer")
        or not (final_state.get("final_answer") or "").strip()
    )
    else "completed"
)
```

This keeps lane-level researcher/critic tombstones as `status="completed"` with `error` still populated (front-matter `error:` line at `storage.py:109-110` still records the lane failure), while planner/synthesizer failures and empty answers remain `failed`. Evidence insufficient on one edge: whether any downstream consumer (CLI/statusline) branches on `status` vs `error` — I did not read `consultants/cli.py` or `consultants/server/app.py`; the change is safe for storage.py, which renders both fields independently.

---

## Researcher (round 1)

## Findings

**1. The exception never escapes `run_loop` — it's caught at the researcher node**
- `claude_hooks/agent_loop/runner.py:138-234` — `run_loop` has no internal try/except around `chat_fn(payload)` (runner.py:230) or tool execution; any HTTP-timeout-exhausted-retries or tool-executor TypeError propagates straight out of `run_loop`.
- `consultants/engine/council.py:547-556` — `researcher_node` wraps the `loop_runner(...)` call in `try/except Exception`; a crashed lane is caught here, logged, and converted to a **tombstone return**: `research: ["(researcher lane failed: {e})"]` plus `error`/`_role_failed` keys (council.py:562-573). The comment at council.py:557-561 says this tombstone exists precisely so the synthesizer "sees the gap" instead of the lane silently contributing nothing (the audit-high csl-…-faa2 finding).
- Consequence: the LangGraph superstep completes; `compiled.stream` does **not** raise; the `except` at `consultants/server/runner.py:155-161` (`status="failed"`, "graph crashed") is **not** hit for a lane failure.

**2. Additive reducers (graph.py) merge the tombstone like any lane result**
- `consultants/engine/graph.py:64,68` — `research: Annotated[list, operator.add]` and `turns: Annotated[list, operator.add]`; the tombstone's `research` delta (council.py:566) concatenates with surviving lanes' reports. Lane payloads start empty (`"research": [], "turns": []` at graph.py:377-378) so there's no double-count.
- `consultants/engine/graph.py:74-75` — `error` and `_role_failed` are **plain channels (no reducer)** → last-write-wins. If two lanes fail concurrently in the same superstep, only one `error` string survives (nondeterministic which). The surviving single failure still marks the channel.
- `consultants/engine/graph.py:416-421` — critic's conditional edge still routes to synthesizer; nothing inspects `error` to abort.

**3. runner.py stream loop: status flips to "failed" despite a real answer**
- `consultants/server/runner.py:137-141` — `values` mode payload becomes `final_state` after each superstep, so the merged (tombstone-inclusive) state is what's read.
- `consultants/server/runner.py:172-174` — `node_error = final_state.get("error")`; `terminal_status = "failed" if node_error else "completed"`. A single researcher-lane tombstone therefore makes the **whole consultation `status="failed"`**, even though the synthesizer ran and produced a real `final_answer`.
- `consultants/server/runner.py:186-199` — the artifact is still written with the real `final_answer` and `status=terminal_status` ("failed") + `error=node_error`.
- `consultants/server/runner.py:201-206` — `state.status = "failed"`, `state.error = node_error` returned to the poll API; `consultants/server/app.py:64-70` surfaces exactly that to the client. (Only an exception escaping `run_council` itself hits the app-level crash path at app.py:322-323 — not this scenario.)

**4. What the synthesizer actually sees**
- `consultants/engine/council.py:245-266` — `build_synthesizer_messages` renders each `research` entry as `RESEARCHER REPORT (round i)`. The failed lane appears as a report whose entire body is the raw string `(researcher lane failed: <exception repr>)` (council.py:253-254 + 562). There is no structured "this lane failed" framing — the synthesizer must interpret the tombstone text, and the raw exception text can leak into the final answer verbatim. Surviving lanes' reports appear normally alongside it.

**5. storage.py artifact writer**
- `consultants/engine/storage.py:108-110` — front-matter gets `status: failed` and an `error:` line.
- `consultants/engine/storage.py:119-120, 129-136` — `summary.md` body is the real `final_answer`; `transcript.md` renders every turn including the tombstone researcher turn (content = the failure string). Nothing is omitted; the artifact set is complete and coherent, just labeled failed.

**Verdict:** the council **does** produce a coherent, degraded answer (synthesized from surviving lanes + an inline failure tombstone), but the user perceives `status=failed` with `error="researcher failed: …"` — an over-reporting failure mode: a partial lane loss is indistinguishable from a terminal consultation failure. Only a synthesizer/planner failure (council.py:727-747, 442-454) or a graph crash (runner.py:155-161) is genuinely terminal.

**Recommended hardening (ONE change):** at `consultants/server/runner.py:174`, stop deriving status from the scalar `error` channel alone — e.g. `terminal_status = "failed" if final_state.get("_role_failed") in ("planner", "synthesizer") else "completed"` (researcher/critic tombstones already preserve a usable answer; optionally log `node_error` as a warning, as runner.py:204-206 already does for `_role_failed`). This makes a lost lane surface as a completed consultation with the tombstone visible in transcript.md instead of a false `status=failed`.

---

## Critic (round 2)

DECISION: ready

The researcher's trace is accurate and verified against the source. A researcher lane failure is caught in `consultants/engine/council.py:547-573`, which emits a "tombstone" delta. Because `research` and `turns` use additive reducers (`consultants/engine/graph.py:64,68`), the failure is merged as a string `(researcher lane failed: ...)` alongside surviving results. The synthesizer receives this string in its context (`consultants/engine/council.py:253-254`), allowing it to produce a coherent, gap-aware answer. However, because `error` is a plain channel (`consultants/engine/graph.py:74`), any lane failure sets `final_state.get("error")`, which `consultants/server/runner.py:174` uses to mark the entire consultation as `status="failed"`. The storage layer faithfully writes the real answer but with a "failed" label (`consultants/engine/storage.py:108-110`).

CORRECTIONS:
- report 1: claimed `storage.py:108` is `_yaml_front_matter` — actual `storage.py:108` is `lines.append(f"status: {_yaml_str(result.status)}")` (within `_yaml_front_matter`).

Recommended hardening:
`consultants/server/runner.py:174` — Change to:
```python
terminal_status = "failed" if node_error and (final_state.get("_role_failed") in ("planner", "synthesizer") or not (final_state.get("final_answer") or "").strip()) else "completed"
```

---

## Synthesizer

The council produces a coherent, gap-aware answer, but the consultation is incorrectly labeled as `status="failed"`. The user perceives a "false failure" where a usable answer exists in `summary.md`, but the file's metadata and API status indicate a terminal error.

**Failure Path Trace:**

1.  **Exception Handling:** `claude_hooks/agent_loop/runner.py` does not catch exceptions in `run_loop` (`runner.py:230` for chat calls, `runner.py:122-126` for tool execution). The exception propagates to `consultants/engine/council.py:547-556`, where `researcher_node` catches it and emits a "tombstone" delta containing `research: ["(researcher lane failed: {e})"]`, `error: "researcher failed: {e}"`, and `_role_failed: "researcher"` (`council.py:562-573`).
2.  **Additive Reducers:** In `consultants/engine/graph.py:64,68`, the `research` and `turns` channels use `operator.add`. The tombstone string is concatenated into the research list alongside successful lane results. However, the `error` channel at `graph.py:74` is a plain channel (last-write-wins), meaning the failure string persists in the final state.
3.  **Stream Loop:** The `compiled.stream` loop in `consultants/server/runner.py` does not crash because the exception was handled by the node. However, at `runner.py:172-174`, the runner checks `final_state.get("error")`. Because the tombstone populated this channel, `terminal_status` is set to `"failed"`, regardless of whether the synthesizer successfully ran.
4.  **Synthesizer Input:** The synthesizer receives the `research` list via `build_synthesizer_messages` (`consultants/engine/council.py:245-266`). It sees the literal string `(researcher lane failed: ...)` as one of the researcher reports (`council.py:253-254`), allowing it to synthesize a coherent answer while explicitly acknowledging the missing data.
5.  **Artifact Writer:** `consultants/engine/storage.py:108-110` writes `status: failed` and the `error:` string into the `summary.md` YAML front-matter. The actual `final_answer` is still written to the body of the file (`storage.py:117-120`), and the tombstone turn is recorded in the transcript (`storage.py:123-158`).

**Recommended Hardening:**

Modify `consultants/server/runner.py:174` to distinguish between lane-level failures (which are non-terminal if an answer is produced) and terminal role failures:

```python
terminal_status = (
    "failed"
    if node_error and (
        final_state.get("_role_failed") in ("planner", "synthesizer")
        or not (final_state.get("final_answer") or "").strip()
    )
    else "completed"
)
```

---
