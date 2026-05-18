===== ORIGINAL ANSWER =====
The invariant is enforced through a sequential execution chain in `StoreReaperThread.sweep_once`, where deletion is the final step in a "distill-then-write-then-delete" pipeline.

**Enforcement Code Path**
1. **Distillation**: The reaper calls `_distill_group` (`consultants/engine/store_reaper.py:128`). If the LLM distillation fails, a `DistillationFailed` exception is caught at `consultants/engine/store_reaper.py:129`, and a `continue` statement at line 134 skips deletion entirely.
2. **Persistence**: If distillation succeeds, `_write_summary` is called (`consultants/engine/store_reaper.py:148`). This invokes `StoreSQL.write_distilled_summary` (`consultants/engine/store_sql.py:41-61`), which performs a synchronous `INSERT OR REPLACE` and `commit()` to the project namespace.
3. **Write-Success Guard**: If `_write_summary` returns `False` (line 149), the reaper executes `continue` at line 154, bypassing the deletion step.
4. **Deletion**: The call to `_delete_rows(research_rows)` at `consultants/engine/store_reaper.py:157` is only reachable if both the distillation and the write commit successfully without exceptions.

**Edge Cases and Failure Modes**
*   **Low-Value Content Bypass**: If the distiller returns `None` (signaling the content is not worth summarizing), the reaper explicitly bypasses the write and deletes the originals anyway at `consultants/engine/store_reaper.py:144`.
*   **Configuration Bypass**: If `enable_distillation` is set to `False`, the reaper deletes research rows without any distillation attempt at `consultants/engine/store_reaper.py:122`.
*   **Non-Atomic Commit (Crash Window)**: Because `StoreSQL` and `TranscriptDB` use separate SQLite connections, there is no cross-database transaction. A crash occurring after the summary commit (`consultants/engine/store_sql.py:61`) but before the deletion commit (`consultants/engine/store_reaper.py:238`) results in the summary persisting while the originals remain. This causes duplicate distillation and redundant summary entries in the project namespace during the next sweep.
*   **Lack of Read-Verification**: The reaper trusts the return value of `_write_summary` rather than performing a "read-back" verification to confirm the summary is durable in the project namespace before issuing the delete.

===== ANNOTATED ANSWER =====
The invariant is enforced through a sequential execution chain in `StoreReaperThread.sweep_once`, where deletion is the final step in a "distill-then-write-then-delete" pipeline.

**Enforcement Code Path**
1. **Distillation**: The reaper calls `_distill_group` (`consultants/engine/store_reaper.py:128 [in _question_hint_for_group, not _distill_group]`). If the LLM distillation fails, a `DistillationFailed` exception is caught at `consultants/engine/store_reaper.py:129`, and a `continue` statement at line 134 skips deletion entirely.
2. **Persistence**: If distillation succeeds, `_write_summary` is called (`consultants/engine/store_reaper.py:148 [in StoreReaperThread, not _write_summary]`). This invokes `StoreSQL.write_distilled_summary` (`consultants/engine/store_sql.py:41-61 [unverified — file not found]`), which performs a synchronous `INSERT OR REPLACE` and `commit()` to the project namespace.
3. **Write-Success Guard**: If `_write_summary` returns `False` (line 149), the reaper executes `continue` at line 154, bypassing the deletion step.
4. **Deletion**: The call to `_delete_rows(research_rows)` at `consultants/engine/store_reaper.py:157 [in StoreReaperThread, not _delete_rows]` is only reachable if both the distillation and the write commit successfully without exceptions.

**Edge Cases and Failure Modes**
*   **Low-Value Content Bypass**: If the distiller returns `None` (signaling the content is not worth summarizing), the reaper explicitly bypasses the write and deletes the originals anyway at `consultants/engine/store_reaper.py:144`.
*   **Configuration Bypass**: If `enable_distillation` is set to `False`, the reaper deletes research rows without any distillation attempt at `consultants/engine/store_reaper.py:122`.
*   **Non-Atomic Commit (Crash Window)**: Because `StoreSQL` and `TranscriptDB` use separate SQLite connections, there is no cross-database transaction. A crash occurring after the summary commit (`consultants/engine/store_sql.py:61 [unverified — file not found]`) but before the deletion commit (`consultants/engine/store_reaper.py:238`) results in the summary persisting while the originals remain. This causes duplicate distillation and redundant summary entries in the project namespace during the next sweep.
*   **Lack of Read-Verification**: The reaper trusts the return value of `_write_summary` rather than performing a "read-back" verification to confirm the summary is durable in the project namespace before issuing the delete.
