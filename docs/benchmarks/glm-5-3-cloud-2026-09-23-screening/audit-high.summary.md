---
session_id: csl-2026-09-23-1824-ef5e
created: "2026-09-23T18:24:58"
question: "In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:\n\n1. CouncilState's additive reducers (consultants/engine/graph.py)\n2. consultants/server/runner.py's compiled.stream loop\n3. The synthesizer's input messages — what does it actually see?\n4. The storage.py artifact writer\n\nAnswer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line."
models:
  critic: "glm-5.3:cloud"
  planner: "glm-5.3:cloud"
  researcher: "glm-5.3:cloud"
  synthesizer: "glm-5.3:cloud"
topology: council
effort: high
duration_seconds: 161.94
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-09-23/tmp/claude-hooks-bench-bench-baseline-2026-05-07-7w8z
---

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
