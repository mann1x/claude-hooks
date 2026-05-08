"""Unit tests for the SQLite-backed `MessageRecorder` (Phase 1 of the
v1.1 message-history plan). The recorder is intentionally decoupled
from the rest of the engine, so these tests use only stdlib + the
recorder module — no graph spinup."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from consultants.engine.recorder import (
    SCHEMA_VERSION,
    MessageRecorder,
    RecorderMeta,
    load_role_messages,
)


def _meta(sid: str = "csl-test-0001", **overrides) -> RecorderMeta:
    defaults = dict(
        sid=sid,
        cwd="/tmp/proj",
        question="why is the sky blue?",
        effort="medium",
        topology="council",
        models={"planner": "kimi-k2.6:cloud", "synthesizer": "kimi-k2.6:cloud"},
    )
    defaults.update(overrides)
    return RecorderMeta(**defaults)


class TestSchemaAndMeta(unittest.TestCase):
    def test_creates_db_with_expected_tables_and_indexes(self):
        with TemporaryDirectory() as td:
            db = Path(td) / "transcript.db"
            rec = MessageRecorder(db, meta=_meta())
            try:
                self.assertTrue(db.exists())
                conn = sqlite3.connect(str(db))
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                self.assertIn("meta", tables)
                self.assertIn("events", tables)
                indexes = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='index'"
                    )
                }
                self.assertIn("idx_events_role_kind", indexes)
                self.assertIn("idx_events_ts", indexes)
                conn.close()
            finally:
                rec.close()

    def test_writes_meta_row_with_running_status(self):
        with TemporaryDirectory() as td:
            db = Path(td) / "transcript.db"
            rec = MessageRecorder(
                db,
                meta=_meta(parent_sid="csl-parent-001", subject_baseline_tag="bench-2026-05-07"),
            )
            try:
                conn = sqlite3.connect(str(db))
                row = conn.execute(
                    "SELECT schema_version, sid, status, parent_sid, "
                    "subject_baseline_tag, models_json FROM meta"
                ).fetchone()
                conn.close()
                self.assertEqual(row[0], SCHEMA_VERSION)
                self.assertEqual(row[1], "csl-test-0001")
                self.assertEqual(row[2], "running")
                self.assertEqual(row[3], "csl-parent-001")
                self.assertEqual(row[4], "bench-2026-05-07")
                self.assertEqual(json.loads(row[5]).get("planner"), "kimi-k2.6:cloud")
            finally:
                rec.close()

    def test_wal_journal_mode_is_set(self):
        with TemporaryDirectory() as td:
            db = Path(td) / "transcript.db"
            rec = MessageRecorder(db, meta=_meta())
            try:
                conn = sqlite3.connect(str(db))
                mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
                conn.close()
                self.assertEqual(mode.lower(), "wal")
            finally:
                rec.close()

    def test_creates_parent_dirs_when_missing(self):
        with TemporaryDirectory() as td:
            db = Path(td) / "deep" / "nested" / "transcript.db"
            self.assertFalse(db.parent.exists())
            rec = MessageRecorder(db, meta=_meta())
            try:
                self.assertTrue(db.exists())
            finally:
                rec.close()

    def test_repeated_open_with_same_sid_does_not_duplicate_meta(self):
        with TemporaryDirectory() as td:
            db = Path(td) / "transcript.db"
            rec1 = MessageRecorder(db, meta=_meta())
            rec1.close()
            # Second open on the same file (e.g. tooling reads)
            rec2 = MessageRecorder(db, meta=_meta())
            try:
                conn = sqlite3.connect(str(db))
                count = conn.execute("SELECT COUNT(*) FROM meta").fetchone()[0]
                conn.close()
                self.assertEqual(count, 1)
            finally:
                rec2.close()


class TestRecording(unittest.TestCase):
    def test_record_llm_inserts_event_with_payloads_serialized(self):
        with TemporaryDirectory() as td:
            db = Path(td) / "transcript.db"
            rec = MessageRecorder(db, meta=_meta())
            try:
                rec.record_llm(
                    role="planner",
                    round=1,
                    model="kimi-k2.6:cloud",
                    request={"messages": [{"role": "user", "content": "hi"}]},
                    response={"choices": [{"message": {"content": "plan: x"}}]},
                    prompt_tokens=42,
                    completion_tokens=7,
                    duration_ms=250,
                )
                conn = sqlite3.connect(str(db))
                rows = conn.execute(
                    "SELECT kind, role, round, model, prompt_tokens, "
                    "completion_tokens, duration_ms, request_json, response_json "
                    "FROM events"
                ).fetchall()
                conn.close()
                self.assertEqual(len(rows), 1)
                kind, role, rnd, model, ptok, ctok, dur, req_j, resp_j = rows[0]
                self.assertEqual(kind, "llm_call")
                self.assertEqual(role, "planner")
                self.assertEqual(rnd, 1)
                self.assertEqual(model, "kimi-k2.6:cloud")
                self.assertEqual(ptok, 42)
                self.assertEqual(ctok, 7)
                self.assertEqual(dur, 250)
                self.assertEqual(json.loads(req_j)["messages"][0]["content"], "hi")
                self.assertEqual(
                    json.loads(resp_j)["choices"][0]["message"]["content"], "plan: x"
                )
            finally:
                rec.close()

    def test_record_tool_computes_output_chars(self):
        with TemporaryDirectory() as td:
            db = Path(td) / "transcript.db"
            rec = MessageRecorder(db, meta=_meta())
            try:
                rec.record_tool(
                    role="researcher",
                    round=2,
                    lane_idx=0,
                    tool="read_file",
                    args='{"path": "src/foo.py"}',
                    output="line1\nline2\n",
                    duration_ms=15,
                )
                conn = sqlite3.connect(str(db))
                row = conn.execute(
                    "SELECT kind, role, round, lane_idx, tool, args, output, "
                    "output_chars FROM events"
                ).fetchone()
                conn.close()
                self.assertEqual(row[0], "tool_call")
                self.assertEqual(row[1], "researcher")
                self.assertEqual(row[2], 2)
                self.assertEqual(row[3], 0)
                self.assertEqual(row[4], "read_file")
                self.assertEqual(row[5], '{"path": "src/foo.py"}')
                self.assertEqual(row[6], "line1\nline2\n")
                self.assertEqual(row[7], len("line1\nline2\n"))
            finally:
                rec.close()

    def test_record_node_validates_kind(self):
        with TemporaryDirectory() as td:
            rec = MessageRecorder(Path(td) / "transcript.db", meta=_meta())
            try:
                rec.record_node(role="planner", kind="node_enter")
                rec.record_node(role="planner", kind="node_exit", duration_ms=300)
                with self.assertRaises(ValueError):
                    rec.record_node(role="planner", kind="bogus")
            finally:
                rec.close()

    def test_events_ordered_by_ts(self):
        with TemporaryDirectory() as td:
            db = Path(td) / "transcript.db"
            rec = MessageRecorder(db, meta=_meta())
            try:
                for i in range(5):
                    rec.record_llm(
                        role="researcher",
                        round=1,
                        lane_idx=0,
                        model="qwen3.5:cloud",
                        request={"i": i},
                        response={"i": i},
                    )
                    time.sleep(0.001)  # ensure monotonic ts in case of clock granularity
                conn = sqlite3.connect(str(db))
                rows = conn.execute(
                    "SELECT request_json FROM events ORDER BY ts"
                ).fetchall()
                conn.close()
                seen = [json.loads(r[0])["i"] for r in rows]
                self.assertEqual(seen, [0, 1, 2, 3, 4])
            finally:
                rec.close()

    def test_safe_dumps_handles_non_serializable(self):
        with TemporaryDirectory() as td:
            db = Path(td) / "transcript.db"
            rec = MessageRecorder(db, meta=_meta())
            try:
                # set() is not JSON-serializable; default=str should
                # stringify it instead of raising.
                rec.record_llm(
                    role="planner",
                    request={"weird": {1, 2, 3}},
                    response={"ok": True},
                )
                conn = sqlite3.connect(str(db))
                row = conn.execute("SELECT request_json FROM events").fetchone()
                conn.close()
                # Just confirm we got valid JSON back.
                self.assertIsInstance(json.loads(row[0]), dict)
            finally:
                rec.close()


class TestThreadSafety(unittest.TestCase):
    def test_concurrent_writes_from_many_threads(self):
        with TemporaryDirectory() as td:
            db = Path(td) / "transcript.db"
            rec = MessageRecorder(db, meta=_meta())
            errors: list[Exception] = []

            def worker(lane: int, n: int) -> None:
                try:
                    for i in range(n):
                        rec.record_llm(
                            role="researcher",
                            round=1,
                            lane_idx=lane,
                            model="qwen3.5:cloud",
                            request={"lane": lane, "i": i},
                            response={"ok": True},
                        )
                except Exception as exc:
                    errors.append(exc)

            try:
                threads = [
                    threading.Thread(target=worker, args=(lane, 20))
                    for lane in range(4)
                ]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
                self.assertEqual(errors, [])
                conn = sqlite3.connect(str(db))
                total = conn.execute(
                    "SELECT COUNT(*) FROM events WHERE kind='llm_call'"
                ).fetchone()[0]
                per_lane = dict(
                    conn.execute(
                        "SELECT lane_idx, COUNT(*) FROM events GROUP BY lane_idx"
                    )
                )
                conn.close()
                self.assertEqual(total, 4 * 20)
                self.assertEqual(per_lane, {0: 20, 1: 20, 2: 20, 3: 20})
            finally:
                rec.close()


class TestLifecycle(unittest.TestCase):
    def test_finalize_truncates_wal_after_concurrent_writes(self):
        # Regression: finalize must close every peer (writer-thread)
        # connection BEFORE running PRAGMA wal_checkpoint(TRUNCATE);
        # otherwise the truncate silently downgrades to PASSIVE and
        # the on-disk -wal file stays at its high-water mark even
        # after the consultation completes. Caught on the live
        # 2026-05-07 smoke run where transcript.db-wal sat at 1 MB
        # next to a 1 MB main DB.
        import os

        with TemporaryDirectory() as td:
            db = Path(td) / "transcript.db"
            rec = MessageRecorder(db, meta=_meta())

            errors: list[Exception] = []

            def worker(lane: int) -> None:
                try:
                    for i in range(40):
                        rec.record_llm(
                            role="researcher", round=1, lane_idx=lane,
                            model="qwen3.5:cloud",
                            request={"lane": lane, "i": i, "pad": "x" * 200},
                            response={"ok": True, "pad": "y" * 200},
                        )
                except Exception as exc:
                    errors.append(exc)

            try:
                threads = [threading.Thread(target=worker, args=(lane,))
                           for lane in range(4)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
                self.assertEqual(errors, [])

                rec.finalize(status="completed")

                wal_path = db.with_suffix(db.suffix + "-wal")
                # The -wal file may not even exist after a clean
                # truncate (SQLite removes it once size goes to 0).
                # If it exists, it must be empty.
                if wal_path.exists():
                    self.assertEqual(
                        os.path.getsize(wal_path), 0,
                        f"-wal file should be 0 bytes after finalize, "
                        f"got {os.path.getsize(wal_path)} bytes",
                    )
            finally:
                rec.close()

    def test_finalize_updates_meta_and_runs_vacuum(self):
        with TemporaryDirectory() as td:
            db = Path(td) / "transcript.db"
            rec = MessageRecorder(db, meta=_meta())
            try:
                rec.record_llm(
                    role="synthesizer",
                    request={"x": "y"},
                    response={"answer": "ok"},
                )
                rec.finalize(status="completed")
                conn = sqlite3.connect(str(db))
                status, finished_at, err = conn.execute(
                    "SELECT status, finished_at, error FROM meta"
                ).fetchone()
                conn.close()
                self.assertEqual(status, "completed")
                self.assertIsNotNone(finished_at)
                self.assertIsNone(err)
            finally:
                rec.close()

    def test_finalize_with_failure_records_error(self):
        with TemporaryDirectory() as td:
            db = Path(td) / "transcript.db"
            rec = MessageRecorder(db, meta=_meta())
            try:
                rec.finalize(status="failed", error="researcher bombed")
                conn = sqlite3.connect(str(db))
                status, err = conn.execute(
                    "SELECT status, error FROM meta"
                ).fetchone()
                conn.close()
                self.assertEqual(status, "failed")
                self.assertEqual(err, "researcher bombed")
            finally:
                rec.close()

    def test_finalize_rejects_unknown_status(self):
        with TemporaryDirectory() as td:
            rec = MessageRecorder(Path(td) / "transcript.db", meta=_meta())
            try:
                with self.assertRaises(ValueError):
                    rec.finalize(status="banana")
            finally:
                rec.close()

    def test_close_is_idempotent_and_blocks_further_writes(self):
        with TemporaryDirectory() as td:
            db = Path(td) / "transcript.db"
            rec = MessageRecorder(db, meta=_meta())
            rec.close()
            rec.close()  # idempotent
            # Further record_* calls become no-ops, NOT raises — the
            # graph might keep firing callbacks during teardown.
            rec.record_llm(role="planner", request={}, response={})
            # And explicit follow-up after close raises only when we
            # try to acquire a connection internally.
            with self.assertRaises(RuntimeError):
                rec._conn()  # noqa: SLF001 (intentional)

    def test_context_manager_finalizes_on_clean_exit(self):
        with TemporaryDirectory() as td:
            db = Path(td) / "transcript.db"
            with MessageRecorder(db, meta=_meta()) as rec:
                rec.record_llm(role="planner", request={}, response={})
            conn = sqlite3.connect(str(db))
            status = conn.execute("SELECT status FROM meta").fetchone()[0]
            conn.close()
            self.assertEqual(status, "completed")

    def test_context_manager_marks_failed_on_exception(self):
        with TemporaryDirectory() as td:
            db = Path(td) / "transcript.db"
            with self.assertRaises(RuntimeError):
                with MessageRecorder(db, meta=_meta()) as rec:
                    rec.record_llm(role="planner", request={}, response={})
                    raise RuntimeError("boom")
            conn = sqlite3.connect(str(db))
            status, err = conn.execute("SELECT status, error FROM meta").fetchone()
            conn.close()
            self.assertEqual(status, "failed")
            self.assertIn("boom", err or "")


class TestLoadRoleMessages(unittest.TestCase):
    """Phase 4 reader: reconstruct per-role / per-lane LLM message
    threads from a finalized transcript.db. Used by the reopen path
    and the warm-session population to keep both code paths
    symmetric."""

    def _make_db(self, td: Path,
                 events: list[dict]) -> Path:
        db = td / "transcript.db"
        rec = MessageRecorder(db, meta=_meta())
        try:
            for ev in events:
                rec.record_llm(**ev)
            rec.finalize(status="completed")
        finally:
            rec.close()
        return db

    def test_returns_none_when_file_missing(self):
        with TemporaryDirectory() as td:
            rm, rlm = load_role_messages(Path(td) / "nope.db")
            self.assertIsNone(rm)
            self.assertIsNone(rlm)

    def test_returns_none_on_corrupt_file(self):
        with TemporaryDirectory() as td:
            bad = Path(td) / "bad.db"
            bad.write_text("not a sqlite db")
            rm, rlm = load_role_messages(bad)
            self.assertIsNone(rm)
            self.assertIsNone(rlm)

    def test_returns_none_when_no_llm_calls(self):
        with TemporaryDirectory() as td:
            db = Path(td) / "empty.db"
            rec = MessageRecorder(db, meta=_meta())
            try:
                rec.record_node(role="planner", kind="node_enter")
                rec.finalize(status="completed")
            finally:
                rec.close()
            rm, rlm = load_role_messages(db)
            self.assertIsNone(rm)
            self.assertIsNone(rlm)

    def test_single_role_thread_reconstructs_messages(self):
        with TemporaryDirectory() as td:
            req = {
                "messages": [
                    {"role": "system", "content": "you are helpful"},
                    {"role": "user", "content": "what is 2+2?"},
                ],
            }
            resp = {
                "choices": [{"message": {
                    "role": "assistant", "content": "4",
                }}],
            }
            db = self._make_db(Path(td), [
                dict(role="planner", model="m", request=req, response=resp),
            ])
            rm, rlm = load_role_messages(db)
            self.assertIsNotNone(rm)
            self.assertIn("planner", rm)
            thread = rm["planner"]
            # request.messages + final assistant message
            self.assertEqual(len(thread), 3)
            self.assertEqual(thread[0]["role"], "system")
            self.assertEqual(thread[-1]["content"], "4")
            # No lanes for non-fan-out roles.
            self.assertIsNone(rlm)

    def test_multi_iter_role_keeps_only_last_request(self):
        # Researcher pattern: each iter's request.messages already
        # contains everything from the prior iter plus that round's
        # tool results. The reader must take only the LAST iter's
        # request to get the complete thread.
        with TemporaryDirectory() as td:
            req1 = {"messages": [
                {"role": "user", "content": "find foo"}]}
            resp1 = {"choices": [{"message": {
                "role": "assistant", "content": None,
                "tool_calls": [{"id": "t1", "function": {
                    "name": "read_file", "arguments": "{}"}}],
            }}]}
            req2 = {"messages": [
                {"role": "user", "content": "find foo"},
                {"role": "assistant", "tool_calls": [{"id": "t1"}]},
                {"role": "tool", "content": "def foo(): pass"},
            ]}
            resp2 = {"choices": [{"message": {
                "role": "assistant",
                "content": "found foo at foo.py:1",
            }}]}
            db = self._make_db(Path(td), [
                dict(role="researcher", round=1, lane_idx=0,
                     model="m", request=req1, response=resp1),
                dict(role="researcher", round=1, lane_idx=0,
                     model="m", request=req2, response=resp2),
            ])
            rm, rlm = load_role_messages(db)
            thread = rm["researcher"]
            # Last request had 3 messages; +1 final assistant = 4
            self.assertEqual(len(thread), 4)
            self.assertEqual(thread[0]["content"], "find foo")
            self.assertEqual(thread[2]["content"], "def foo(): pass")
            self.assertEqual(thread[-1]["content"],
                             "found foo at foo.py:1")
            # Per-lane thread also populated for lane 0.
            self.assertIsNotNone(rlm)
            self.assertIn("researcher", rlm)
            self.assertIn(0, rlm["researcher"])
            self.assertEqual(len(rlm["researcher"][0]), 4)

    def test_multi_lane_fan_out_each_lane_independent(self):
        with TemporaryDirectory() as td:
            def _req(lane: int) -> dict:
                return {"messages": [
                    {"role": "user", "content": f"lane {lane}"}]}
            def _resp(lane: int) -> dict:
                return {"choices": [{"message": {
                    "role": "assistant",
                    "content": f"finding from lane {lane}",
                }}]}
            db = self._make_db(Path(td), [
                dict(role="researcher", round=1, lane_idx=0,
                     model="m", request=_req(0), response=_resp(0)),
                dict(role="researcher", round=1, lane_idx=1,
                     model="m", request=_req(1), response=_resp(1)),
                dict(role="researcher", round=1, lane_idx=2,
                     model="m", request=_req(2), response=_resp(2)),
            ])
            rm, rlm = load_role_messages(db)
            self.assertIn("researcher", rlm)
            lanes = rlm["researcher"]
            self.assertEqual(set(lanes.keys()), {0, 1, 2})
            for lane_idx in (0, 1, 2):
                thread = lanes[lane_idx]
                self.assertEqual(thread[0]["content"],
                                 f"lane {lane_idx}")
                self.assertEqual(thread[-1]["content"],
                                 f"finding from lane {lane_idx}")
            # Flat _role_messages picks the most recent llm_call
            # across lanes (lane 2 here, by ts ordering).
            self.assertEqual(rm["researcher"][-1]["content"],
                             "finding from lane 2")

    def test_skips_rows_with_unparseable_payloads(self):
        # Defensive: if request_json or response_json got truncated
        # (recorder process killed mid-write), the reader skips
        # cleanly without raising.
        with TemporaryDirectory() as td:
            db = Path(td) / "transcript.db"
            rec = MessageRecorder(db, meta=_meta())
            # Bypass the recorder's safe_dumps and write garbage
            # directly so we can assert the reader's tolerance.
            try:
                conn = rec._conn()  # noqa: SLF001 — test-only
                conn.execute(
                    "INSERT INTO events (ts, kind, role, round, "
                    "request_json, response_json) VALUES "
                    "(?, 'llm_call', 'planner', 1, ?, ?)",
                    (time.time(), "{not valid json", "also bad"),
                )
                conn.commit()
                rec.finalize(status="completed")
            finally:
                rec.close()
            rm, rlm = load_role_messages(db)
            # Both fields are None — no usable rows.
            self.assertIsNone(rm)
            self.assertIsNone(rlm)


if __name__ == "__main__":
    unittest.main()
