---
session_id: csl-2026-05-09-0900-48d6
created: "2026-05-09T09:00:19"
question: "In the consultants engine's Send-API fan-out path, suppose one of the parallel researcher lanes raises an unhandled exception inside agent_loop.runner.run_loop (e.g. an HTTP timeout that exhausts the retry budget, or a tool_executor returning a non-string). Trace the exact failure path through:\n\n1. CouncilState's additive reducers (consultants/engine/graph.py)\n2. consultants/server/runner.py's compiled.stream loop\n3. The synthesizer's input messages — what does it actually see?\n4. The storage.py artifact writer\n\nAnswer concretely: does the council still produce a coherent answer to the user, or does the entire consultation fail and surface as status=failed? If it produces a degraded answer, what's the failure mode the user perceives? Cite path:line for every claim. Recommend exactly ONE hardening change with path:line."
models:
  critic: "gemma4:31b-cloud"
  planner: "gemma4:31b-cloud"
  researcher: "gemma4:31b-cloud"
  synthesizer: "gemma4:31b-cloud"
topology: council
effort: high
duration_seconds: 310.99
status: completed
cwd: /tmp/claude-hooks-bench-bench-baseline-2026-05-07-c512
---

The council produces a coherent but degraded answer, though the overall consultation is marked as `status=failed`.

*   **CouncilState's additive reducers:** `consultants/engine/graph.py:64, 68` use `operator.add` for the `research` and `turns` channels. When a researcher lane fails, `consultants/engine/council.py:563-573` returns a "tombstone" dictionary containing error strings. These are concatenated into the global state lists alongside successful results.
*   **Runner's compiled.stream loop:** Local exceptions are caught within the researcher node wrapper at `consultants/engine/council.py:547-555`, which prevents the global exception handler at `consultants/server/runner.py:155` from triggering. However, because the node returns an error string, `consultants/server/runner.py:174` detects `final_state.get("error")` and sets `terminal_status = "failed"`.
*   **Synthesizer's input messages:** The synthesizer receives the merged research list via `consultants/engine/council.py:718-723`. It sees the valid reports from successful lanes and the tombstone strings (e.g., `"(researcher lane failed: ...)"`) as standard input evidence.
*   **Storage.py artifact writer:** `consultants/server/runner.py:177-199` invokes `storage.write_consultation`. The resulting `summary.md` and `metadata.json` are marked `status="failed"` (`consultants/engine/storage.py:108-110`), but the actual `final_answer` produced by the synthesizer is still written to the file (`consultants/engine/storage.py:117-120`).

**Perceived Failure Mode:** The user receives a functional response synthesized from the surviving lanes, but the system's metadata and the YAML front-matter of `summary.md` report the consultation as failed.

**Hardening Recommendation:**
Modify `consultants/engine/council.py:718-723` (`build_synthesizer_messages`) to count entries starting with `(researcher lane failed:` and inject an explicit warning into the synthesizer's prompt (e.g., *"Warning: X researcher lanes failed; your evidence is incomplete"*) to prevent confident hallucinations regarding the missing data.
