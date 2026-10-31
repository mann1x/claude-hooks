---
session_id: csl-2026-10-27-1127-63b4
created: "2026-10-27T11:27:49"
question: "In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:\n\n1. CouncilState's additive reducers (consultants/engine/graph.py)\n2. consultants/server/runner.py's compiled.stream loop\n3. The synthesizer's input messages — what does it actually see?\n4. The storage.py artifact writer\n\nAnswer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line."
models:
  critic: "glm-5.3-flash:cloud"
  planner: "glm-5.3-flash:cloud"
  researcher: "glm-5.3-flash:cloud"
  synthesizer: "glm-5.3-flash:cloud"
topology: council
effort: high
duration_seconds: 191.74
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-10-27/tmp/claude-hooks-bench-bench-baseline-2026-05-07-g1tB
---

**Bottom line: the council still produces a coherent, degraded answer — but the whole consultation is mislabeled `status="failed"` to the user.** The lane exception never aborts the LangGraph superstep; it's caught inside the node and converted to a tombstone that flows through the normal additive reducers. Only the runner's post-stream status check turns it into a failure.

**1. CouncilState's additive reducers (consultants/engine/graph.py)**
`researcher_node` wraps the entire `run_loop` call in `try` (`council.py:547`) / `except Exception` (`council.py:555`), so an HTTP timeout that exhausts the retry budget, or a `TypeError` from a non-string tool return, never escapes the node. The except returns a tombstone delta: `err_text` at `council.py:562`, then `error`, `_role_failed: "researcher"`, `research: ["(researcher lane failed: …)"]`, and a zero-token `RoleTurn` (`council.py:563-573`). On the reducer side, `research`, `turns`, and the token/round counters are `Annotated[..., operator.add]` (`graph.py:64,68-72`), so the tombstone simply concatenates with the surviving lanes' deltas — Send lanes seed empty `research`/`turns` so deltas merge cleanly (`graph.py:377-378`). Success-path lanes return delta-only values with **no** `error`/`_role_failed` keys (`council.py:658-664`), so the failing lane is the sole writer on those channels and no LangGraph concurrent-write conflict occurs. Note `error` and `_role_failed` are plain `Optional[str]` channels with no reducer (`graph.py:74-75`) — this only becomes a hazard if *two* lanes fail in the same superstep (unverified against the installed langgraph; both would write different strings to a last-value channel, likely raising `InvalidUpdateError` and crashing the whole run).

**2. runner.py's compiled.stream loop**
The graph completes normally on a single-lane failure, so the `except` around `compiled.stream` (`runner.py:155-161`, stream call at `runner.py:137-138`) is **not** taken — `_write_failed_artifacts` never runs. The last `"values"` payload becomes `final_state` (`runner.py:139-141`). The mislabel happens post-stream: `node_error = final_state.get("error")` and `terminal_status = "failed" if node_error else "completed"` (`runner.py:172-174`). Because the tombstone set `error`, the entire consultation is stamped failed even though the synthesizer ran and artifacts were written with the real answer (`runner.py:186-199`). `node_error`/`node_failed` are still persisted to metadata (`runner.py:190,201-202`), so the lane failure remains visible there.

**3. What the synthesizer actually sees**
`build_synthesizer_messages` enumerates every string in `state["research"]` as a numbered "RESEARCHER REPORT (round i)" entry (`council.py:253-254`) — so it receives N−1 real reports **plus the tombstone string as an ordinary report**. The synthesizer prompt has no missing/incomplete-research handling (`council.py:156-168`), but the tombstone text is self-describing, so a competent synthesizer can flag the gap. One loss: any partial turns the crashed lane emitted inside `run_loop` before raising are dropped (run_loop only returns messages on the success path); the tombstone `RoleTurn` carries 0 tokens and no tool calls.

**4. storage.py artifact writer**
`write_consultation` (`storage.py:200`) writes `summary.md` with `status: failed` and `error:` front-matter (`storage.py:108-110`) but the **full coherent `final_answer` as the body** (`storage.py:117-120`), plus the transcript with partial turns from surviving lanes and the tombstone turn. So the artifact is degraded-but-readable while its front-matter says failed.

**Failure mode the user perceives:** polling the status endpoint shows `status=failed` with `error="researcher failed: …"` (`consultants/server/app.py:64,70`), while the result artifact contains a complete answer built from the surviving lanes — worst of both worlds: a good answer stamped failed, polluting session status and any downstream consumers of `metadata.json`.

**One hardening change:** reclassify lane-level degradation at `consultants/server/runner.py:174` — fail only when the synthesizer itself died or the answer is empty:

```python
terminal_status = ("failed"
                   if node_failed == "synthesizer"
                   or not (final_state.get("final_answer") or "").strip()
                   else "completed")
```

The lane's `error` still flows to `state.error` and metadata (`runner.py:190,202`), so the degradation stays visible in artifacts; only the misleading top-level status changes.

Unverified (secondary, does not change the verdict): `claude_hooks/agent_loop/runner.py` internals (the exact exception type escaping `run_loop` — irrelevant since `council.py:555` catches any `Exception`); `trace.py` re-raise behavior (`traced_node` wraps the node whose own try/except fires first); the two-lane `InvalidUpdateError` edge (langgraph package not inspectable in-tree; pinned `langgraph>=0.2,<0.4`); the tombstone test at `tests/test_consultants_council.py:531-558` (corroborated by two reports and the delta-only convention comment at `:396`, not read directly).
