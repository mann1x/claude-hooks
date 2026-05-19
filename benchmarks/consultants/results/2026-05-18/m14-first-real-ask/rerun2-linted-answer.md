---
session_id: csl-2026-05-18-1156-115c
created: "2026-05-18T11:56:01"
question: In the M14 store reaper at consultants/engine/store_reaper.py, the critical invariant is that research originals only get deleted after a successful distillation write to the project namespace. Walk me through the exact code path that enforces this, and identify any edge case where it could fail.
models:
  critic: "gemini-3-flash-preview:cloud"
  planner: "gemini-3-flash-preview:cloud"
  researcher: "gemini-3-flash-preview:cloud"
  synthesizer: "gemma4:31b-cloud"
  tool_executor: "gemma4:31b-cloud"
topology: council
effort: xhigh
duration_seconds: 593.62
status: completed
cwd: /srv/dev-disk-by-label-opt/dev/claude-hooks
---

The invariant is enforced via a synchronous sequential execution chain in `sweep_once` that requires both distillation and persistence to return successfully before the deletion method is invoked.

**Enforcement Code Path**
1. **Distillation**: The reaper calls `_distill_group` at `consultants/engine/store_reaper.py:301 [in sweep_once, not _distill_group]`.
2. **Persistence**: If distillation succeeds, it immediately calls `_write_summary` at `consultants/engine/store_reaper.py:302 [in sweep_once, not _write_summary]`, which invokes `write_distilled_summary` at `consultants/engine/store_reaper.py:392 [in _write_summary, not write_distilled_summary]` to commit the data to the project namespace.
3. **Exception Guard**: Both the distillation and write steps are wrapped in a `try...except DistillationFailed` block (`consultants/engine/store_reaper.py:303`). If any error occurs during these steps, the reaper executes `continue` at `consultants/engine/store_reaper.py:310`, bypassing the deletion logic.
4. **Deletion**: The call to `_delete_rows` at `consultants/engine/store_reaper.py:313 [in sweep_once, not _delete_rows]` (which eventually calls `self._provider.delete_by_hashes` at `consultants/engine/store_reaper.py:416 [in _delete_rows, not delete_by_hashes]`) is physically located after the `try/except` block, making it reachable only upon the successful completion of the write.

**Failure Edge Cases**
* **Intentional Invariant Bypass (Cost Gate)**: If a research group contains fewer than `min_entries` (default 3), the reaper skips distillation entirely and deletes the rows immediately at `consultants/engine/store_reaper.py:290, 296`. This results in total data loss for low-volume sessions.
* **Atomicity Gap (Duplicate Summaries)**: There is no distributed transaction between the project namespace write and the store deletion. If the process crashes after `_write_summary` (`consultants/engine/store_reaper.py:302 [in sweep_once, not _write_summary]`) but before `_delete_rows` (`consultants/engine/store_reaper.py:313 [in sweep_once, not _delete_rows]`), the distilled summary is persisted, but the research originals remain. The next reaper sweep will re-distill these originals, potentially creating duplicate summaries.
* **Partial Deletion**: If the underlying provider's `delete_by_hashes` call (`consultants/engine/store_reaper.py:416 [in _delete_rows, not delete_by_hashes]`) fails mid-batch, some originals may be deleted while others persist, leading to an inconsistent state where a full distillation summary exists but only partial originals remain.
* **Silent Write Failures**: If the storage provider accepts the write at `consultants/engine/store_reaper.py:392 [in _write_summary, not write_distilled_summary]` but fails to make it durable (e.g., due to an internal buffer crash), the reaper will proceed to delete the originals based on the returned "success" signal.

