---
session_id: csl-2026-09-23-2030-b3da
created: "2026-09-23T20:30:47"
question: "In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:\n\n1. CouncilState's additive reducers (consultants/engine/graph.py)\n2. consultants/server/runner.py's compiled.stream loop\n3. The synthesizer's input messages — what does it actually see?\n4. The storage.py artifact writer\n\nAnswer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line."
models:
  critic: "glm-5.3-flash:cloud"
  planner: "glm-5.3-flash:cloud"
  researcher: "glm-5.3-flash:cloud"
  synthesizer: "glm-5.3-flash:cloud"
topology: council
effort: high
duration_seconds: 97.46
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/bench-home-2026-09-23/tmp/claude-hooks-bench-bench-baseline-2026-05-07-xbWO
---

**Bottom line: the council does NOT hard-fail. It produces a real, synthesized answer — but the runner mislabels the entire consultation `status=failed`, so the user is told it failed even though the artifacts on disk contain a coherent answer.**

**1. CouncilState reducers (`consultants/engine/graph.py`)**

- `research`, `turns`, and token counters are additive: `Annotated[list, operator.add]` / int-sum (`consultants/engine/graph.py:64`, `:68-72`), so parallel Send lanes merge by concatenation/sum. Each lane starts with empty lists and zeroed counters (`graph.py:377-381`).
- `error` and `_role_failed` are plain Optional channels — **last-write-wins** (`graph.py:74-75`); with multiple failing lanes, only one error string survives.

**2. Exception origin → node boundary**

- `run_loop` has **no try/except at all** (`claude_hooks/agent_loop/runner.py:138`). Chat retry exhaustion raises `RuntimeError` from `claude_hooks/get_advice/chat_client.py:285-287`, `:302`; a non-string tool output raises `TypeError` at `len(output)` (`claude_hooks/agent_loop/runner.py:126`, bare call at `:122`). Both propagate straight out of `run_loop`.
- But the exception never reaches LangGraph: `researcher_node` wraps the loop call in try/except (`consultants/engine/council.py:547-555`) and returns a **tombstone partial** (`council.py:562-573`): `error="researcher failed: ..."`, `_role_failed="researcher"`, a synthetic `research` entry `"(researcher lane failed: {e})"`, and a zero-token RoleTurn. The reducers see a tombstone *entry*, not a missing key — the graph proceeds normally. (Caveat: an exception outside the try, e.g. in `build_researcher_messages` at `council.py:521-527`, escapes and hits the hard-fail path instead.)
- The `error` channel persists through critic and synthesizer because those nodes return partials without an `error` key (`council.py:705-712`, `:753-758`).

**3. `compiled.stream` loop (`consultants/server/runner.py`)**

- The try/except at `consultants/server/runner.py:136-161` is a graph-crash guard only; it does **not** fire in this scenario — the stream drains normally.
- The pivotal line is after the stream: `terminal_status = "failed" if node_error else "completed"` (`consultants/server/runner.py:172-174`). One lane's tombstone sets `error`, which **flips the whole consultation to `failed`** even though every node ran.

**4. Synthesizer input**

- `synthesizer_node` builds messages from `state.get("research") or []` (`consultants/engine/council.py:718-723`), so it sees the surviving lanes' real reports **plus** the tombstone string rendered as `"\nRESEARCHER REPORT (round {i}):\n{r}"` (`council.py:254`) — structurally indistinguishable from a real lane's output; nothing flags coverage as incomplete beyond the prose. The pipeline continues (critic → route → synthesizer) and produces a genuine `final_answer` over partial evidence. Total failure (placeholder answer) only occurs if the synthesizer itself dies (`council.py:736-747`) or the graph crashes outside a node.

**5. Storage (`consultants/engine/storage.py`)**

- Artifacts are fully written regardless: `write_consultation` (`consultants/server/runner.py:199`) does atomic tmp+rename writes of `summary.md` / `transcript.md` / `metadata.json` (`consultants/engine/storage.py:200-207`; `_atomic_write_text` `:174-192`). The tombstone turn appears in the transcript via `render_transcript` (`storage.py:129-136`), and `status="failed"` + the error land in the YAML front-matter and metadata (`storage.py:108-110`, `:166-169`). The placeholder-writer `_write_failed_artifacts` (`runner.py:211-237`) is only reached if the stream itself raises.

**User-perceived failure mode**

Degraded-but-mislabeled. The poll reports `status=failed` with `error="researcher failed: ..."` via `SessionState.public_dict` (`consultants/server/app.py:61-75`, set at `runner.py:201-202`), while `/result` and the on-disk `summary.md` simultaneously contain a coherent answer built from the surviving lanes. Status and artifact contradict each other; downstream consumers keying on `status` will discard a usable consultation that merely lost one lane of evidence.

**ONE hardening change**

Fix the status promotion at **`consultants/server/runner.py:174`**: mark `failed` only for terminal failures — `final_answer` empty/placeholder or `_role_failed == "synthesizer"`; when the failure is lane-level (`_role_failed == "researcher"`) and a real `final_answer` exists, report `completed` (or a `degraded` status) with the lane error surfaced as a non-fatal warning in metadata. Catching inside `run_loop` (`claude_hooks/agent_loop/runner.py:138`) is the wrong layer — `researcher_node`'s tombstone (`council.py:555`) already contains the failure in-band; the bug is purely the runner treating any lane `error` as terminal.
