---
session_id: csl-2026-09-23-2018-0987
created: "2026-09-23T20:18:27"
question: "In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:\n\n1. CouncilState's additive reducers (consultants/engine/graph.py)\n2. consultants/server/runner.py's compiled.stream loop\n3. The synthesizer's input messages — what does it actually see?\n4. The storage.py artifact writer\n\nAnswer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line."
models:
  critic: "glm-5.3-flash:cloud"
  planner: "glm-5.3-flash:cloud"
  researcher: "glm-5.3-flash:cloud"
  synthesizer: "glm-5.3-flash:cloud"
topology: council
effort: high
duration_seconds: 105.61
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-09-23/tmp/claude-hooks-bench-bench-baseline-2026-05-07-l2Px
---

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
