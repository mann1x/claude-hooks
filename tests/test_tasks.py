"""Task tracking, milestone 1: the file format, the folder, the index
and the service that keeps them in step (docs/PLAN-task-tracking.md)."""
from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import time
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path

from claude_hooks.tasks.files import (
    TaskDir, derive_prefix, project_root, read_config,
)
from claude_hooks.tasks.model import (
    Task, TaskFormatError, normalise_status, parse_value, render_value,
    split_id,
)
from claude_hooks.tasks.service import TaskService, urgency
from claude_hooks.tasks.store import (
    TaskIndex, cosine, embed_text, pack_vec, unpack_vec,
)


class _SqliteConn:
    def __init__(self, path: Path):
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.lock = threading.RLock()

    def __call__(self):
        return self.conn

    def close(self):
        self.conn.close()


class _Tmp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.root = self.tmp / "backup_models"
        self.root.mkdir()


# ─── format ──────────────────────────────────────────────────────────


class ValueTests(unittest.TestCase):
    def test_round_trip(self):
        for v in ["bm-41", "has space", 'quote " inside', "", "true",
                  ["bm-41", "bm-7"], ["a b", "c"], [],
                  {"commits": ["2f01f52"], "files": ["a/b.py"]}]:
            self.assertEqual(parse_value(render_value(v)), v, v)

    def test_comment_outside_string_only(self):
        self.assertEqual(parse_value("active   # pending | active"), "active")
        self.assertEqual(parse_value('"a # b"  # note'), "a # b")

    def test_unparseable_flow_is_kept_as_text(self):
        self.assertEqual(parse_value("[unclosed"), "[unclosed")


class TaskFileTests(unittest.TestCase):
    def _task(self):
        t = Task(id="bm-42", title="R9.run3: GEPO brevity", status="active",
                 priority="H", area="R9.gepo", tags=["gepo", "coderx"],
                 depends=["bm-41"], plan="docs/plans/p.md",
                 links={"commits": ["2f01f528b"]},
                 created="2026-08-26T18:50:00Z",
                 updated="2026-08-26T19:33:00Z", sessions=["4208dc59"])
        t.set_section("Description", "What and why.")
        t.set_section("Acceptance", "- [ ] router targets\n- [x] smoke")
        t.add_log("created", session="4208dc59",
                  when=datetime(2026, 8, 26, 18, 50, tzinfo=timezone.utc))
        t.add_log("RUNNING on bs2", session="4208dc59",
                  when=datetime(2026, 8, 26, 19, 33, tzinfo=timezone.utc))
        return t

    def test_round_trip_is_exact(self):
        t = self._task()
        text = t.to_markdown()
        back = Task.from_markdown(text)
        self.assertEqual(back.to_markdown(), text)
        self.assertEqual(back.depends, ["bm-41"])
        self.assertEqual(back.links, {"commits": ["2f01f528b"]})
        self.assertEqual(back.acceptance_counts(), (1, 2))

    def test_log_is_newest_first(self):
        lines = self._task().log_lines
        self.assertIn("RUNNING", lines[0])
        self.assertTrue(lines[1].endswith("created"))
        self.assertIn("[4208dc59]", lines[0])

    def test_extra_sections_and_fields_survive(self):
        text = self._task().to_markdown().replace(
            "---\n\n## Description",
            "owner: manni\n---\n\n## Description") + "\n## Notes\nmine\n"
        back = Task.from_markdown(text)
        self.assertEqual(back.extra, {"owner": "manni"})
        self.assertEqual(back.section("Notes"), "mine")
        self.assertIn("owner: manni", back.to_markdown())
        self.assertIn("## Notes\nmine", back.to_markdown())

    def test_hand_typed_status_is_mapped(self):
        text = self._task().to_markdown().replace("status: active",
                                                  "status: in_progress")
        self.assertEqual(Task.from_markdown(text).status, "active")
        self.assertEqual(normalise_status("completed"), "done")
        with self.assertRaises(ValueError):
            normalise_status("someday")

    def test_crlf_and_bom(self):
        text = "﻿" + self._task().to_markdown().replace("\n", "\r\n")
        self.assertEqual(Task.from_markdown(text).id, "bm-42")

    def test_errors_name_the_file(self):
        with self.assertRaises(TaskFormatError) as cm:
            Task.from_markdown("no front matter", source="x.md")
        self.assertIn("x.md", str(cm.exception))
        with self.assertRaises(TaskFormatError):
            Task.from_markdown("---\ntitle: x\n---\n")

    def test_ids(self):
        self.assertEqual(split_id("BM-42"), ("bm", 42))
        with self.assertRaises(ValueError):
            split_id("T-0042x")


# ─── folder ──────────────────────────────────────────────────────────


class PrefixTests(unittest.TestCase):
    def test_derivation(self):
        self.assertEqual(derive_prefix("backup_models"), "bm")
        self.assertEqual(derive_prefix("claude-hooks"), "ch")
        self.assertEqual(derive_prefix("lm-evaluation-harness"), "leh")
        self.assertEqual(derive_prefix("opencoti"), "op")
        self.assertEqual(derive_prefix("laserRMT"), "lr")
        self.assertEqual(derive_prefix("2048game"), "t2")

    def test_clash_extends_then_numbers(self):
        self.assertEqual(derive_prefix("backup_models", {"bm"}), "bmo")
        self.assertEqual(derive_prefix("opencoti", {"op"}), "ope")
        self.assertEqual(derive_prefix("ab", {"ab"}), "ab2")


class TaskDirTests(_Tmp):
    def test_init_once_and_config_is_plain(self):
        d = TaskDir(self.root)
        d.init()
        self.assertEqual(d.prefix, "bm")
        d2 = TaskDir(self.root)
        d2.init(prefix="zz")              # already initialised: ignored
        self.assertEqual(d2.prefix, "bm")
        cfg = read_config(d.dir / "config.toml")
        self.assertEqual(cfg, {"project": "backup_models", "prefix": "bm"})

    def test_bad_prefix_refused(self):
        with self.assertRaises(ValueError):
            TaskDir(self.root).init(prefix="B-M")

    def test_project_root_prefers_an_existing_task_folder(self):
        TaskDir(self.root).init()
        sub = self.root / "scripts" / "deep"
        sub.mkdir(parents=True)
        self.assertEqual(project_root(str(sub)), self.root.resolve())

    def test_project_root_uses_git_boundary(self):
        (self.root / ".git").mkdir()
        sub = self.root / "a"
        sub.mkdir()
        self.assertEqual(project_root(str(sub)), self.root.resolve())

    def test_concurrent_creates_get_distinct_ids(self):
        svc = TaskService(self.root)
        svc.init()
        ids: list[str] = []
        errors: list[BaseException] = []

        def worker(i):
            try:
                ids.append(TaskService(self.root).create(f"task {i}").id)
            except BaseException as e:        # pragma: no cover
                errors.append(e)
        threads = [threading.Thread(target=worker, args=(i,))
                   for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(sorted(ids, key=lambda s: int(s.split("-")[1])),
                         [f"bm-{n}" for n in range(1, 13)])

    def test_numbering_continues_past_archived(self):
        svc = TaskService(self.root)
        a = svc.create("one")
        svc.set_status(a.id, "done")
        self.assertEqual(svc.create("two").id, "bm-2")


# ─── service, files only ─────────────────────────────────────────────


class ServiceFileTests(_Tmp):
    def setUp(self):
        super().setUp()
        self.svc = TaskService(self.root, session_id="abcdef1234")

    def test_create_writes_a_readable_file(self):
        t = self.svc.create("Write the importer", description="why",
                            priority="h", area="M5", tags="import,tasks",
                            acceptance=["dry run reviewed", "- [x] parsed"])
        path = self.root / ".claude-hooks/tasks/bm-1.md"
        self.assertTrue(path.is_file())
        text = path.read_text(encoding="utf-8")
        self.assertIn("title: \"Write the importer\"", text)
        self.assertIn("priority: H", text)
        self.assertIn("- [ ] dry run reviewed", text)
        self.assertIn("- [x] parsed", text)
        self.assertIn("[abcdef12] created", text)
        self.assertEqual(t.tags, ["import", "tasks"])
        self.assertEqual(t.sessions, ["abcdef12"])

    def test_close_archives_and_reopen_restores(self):
        t = self.svc.create("x")
        self.svc.set_status(t.id, "done", "shipped")
        d = self.root / ".claude-hooks/tasks"
        self.assertFalse((d / "bm-1.md").exists())
        self.assertTrue((d / "archive/bm-1.md").exists())
        self.svc.set_status(t.id, "active")
        self.assertTrue((d / "bm-1.md").exists())
        self.assertFalse((d / "archive/bm-1.md").exists())
        log = self.svc.show(t.id)[0].log_lines
        self.assertIn("done → active", log[0])
        self.assertIn("pending → done: shipped", log[1])

    def test_update_logs_what_changed(self):
        t = self.svc.create("x", acceptance=["a", "b"])
        self.svc.update(t.id, priority="L", add_tags=["k"], check=[2],
                        note="half way")
        t2, _ = self.svc.show(t.id)
        self.assertEqual(t2.priority, "L")
        self.assertEqual(t2.acceptance_counts(), (1, 2))
        self.assertIn("priority M→L", t2.log_lines[0])
        self.assertIn("half way", t2.log_lines[0])
        with self.assertRaises(ValueError):
            self.svc.update(t.id, check=[5])

    def test_depends_must_exist_and_not_be_self(self):
        a = self.svc.create("a")
        with self.assertRaises(ValueError):
            self.svc.create("b", depends=["bm-99"])
        b = self.svc.create("b", depends=[a.id])
        self.assertEqual(b.depends, ["bm-1"])
        with self.assertRaises(ValueError):
            self.svc.update(b.id, add_depends=[b.id])

    def test_link_kinds(self):
        t = self.svc.create("x")
        self.svc.link(t.id, "commit", "abc123")
        self.svc.link(t.id, "commit", "abc123")      # no duplicate
        self.svc.link(t.id, "plan", "docs/p.md")
        t2, _ = self.svc.show(t.id)
        self.assertEqual(t2.links, {"commits": ["abc123"]})
        self.assertEqual(t2.plan, "docs/p.md")
        with self.assertRaises(ValueError):
            self.svc.link(t.id, "bogus", "x")

    def test_board_orders_and_blocks(self):
        a = self.svc.create("base", priority="L")
        b = self.svc.create("needs base", priority="H", depends=[a.id])
        c = self.svc.create("hot", priority="H")
        self.svc.set_status(c.id, "active")
        w = self.svc.create("waiting on bs2")
        self.svc.set_status(w.id, "waiting")
        board = self.svc.board()
        self.assertEqual([r["id"] for r in board["active"]], [c.id])
        self.assertEqual([r["id"] for r in board["blocked"]], [b.id])
        self.assertEqual([r["id"] for r in board["ready"]], [a.id])
        self.assertEqual([r["id"] for r in board["waiting"]], [w.id])
        # base unblocks two; blocking outweighs low priority.
        self.assertGreater(board["ready"][0]["urgency"], 8)
        self.svc.set_status(a.id, "done")
        board = self.svc.board()
        self.assertEqual([r["id"] for r in board["ready"]], [b.id])
        self.assertEqual([r["id"] for r in board["closed"]], [a.id])

    def test_hand_edit_is_respected(self):
        t = self.svc.create("x")
        p = self.root / ".claude-hooks/tasks/bm-1.md"
        p.write_text(p.read_text(encoding="utf-8").replace(
            "status: pending", "status: active"), encoding="utf-8")
        self.assertEqual(self.svc.board()["active"][0]["id"], t.id)

    def test_lock_serialises_writers(self):
        t = self.svc.create("x")
        errors = []

        def note(i):
            try:
                TaskService(self.root).note(t.id, f"n{i}")
            except BaseException as e:    # pragma: no cover
                errors.append(e)
        threads = [threading.Thread(target=note, args=(i,)) for i in range(8)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        self.assertEqual(errors, [])
        notes = [ln for ln in self.svc.show(t.id)[0].log_lines
                 if " n" in ln]
        self.assertEqual(len(notes), 8)       # no lost update

    def test_stale_lock_is_broken(self):
        t = self.svc.create("x")
        lock = self.root / f".claude-hooks/tasks/.{t.id}.lock"
        lock.write_text("")
        old = time.time() - 120
        os.utime(lock, (old, old))
        self.svc.note(t.id, "through")
        self.assertFalse(lock.exists())


class UrgencyTests(unittest.TestCase):
    def test_components(self):
        now = datetime(2026, 10, 2, tzinfo=timezone.utc)
        base = {"priority": "M", "status": "pending",
                "created": "2026-10-02T00:00:00Z"}
        u0 = urgency(base, blocking=0, blocked=False, now=now)
        self.assertEqual(u0, 3.9)
        self.assertEqual(urgency(dict(base, status="active"), blocking=0,
                                 blocked=False, now=now), 7.9)
        self.assertLess(urgency(base, blocking=0, blocked=True, now=now), u0)
        overdue = dict(base, due="2026-10-01T00:00:00Z")
        self.assertEqual(urgency(overdue, blocking=0, blocked=False, now=now),
                         15.9)


# ─── index ───────────────────────────────────────────────────────────


class _Embedder:
    """Deterministic bag-of-letters vectors: similar text, similar vec."""

    def __init__(self):
        self.calls = 0

    def __call__(self, text):
        self.calls += 1
        v = [0.0] * 26
        for c in text.lower():
            if "a" <= c <= "z":
                v[ord(c) - 97] += 1
        return v


class IndexHarness(_Tmp):
    dialect = "sqlite"

    def make_index(self):
        db = _SqliteConn(self.tmp / "t.db")
        self.addCleanup(db.close)
        return TaskIndex(db, db.lock, dialect="sqlite")

    def setUp(self):
        super().setUp()
        self.index = self.make_index()
        self.emb = _Embedder()
        self.svc = TaskService(self.root, self.index, host="solidpc",
                               session_id="s1", embedder=self.emb,
                               embed_model="letters")


class IndexTests(IndexHarness):
    def test_writes_reach_the_index(self):
        t = self.svc.create("Import backup_models tasks", area="M5",
                            tags=["import"])
        row = self.index.get("backup_models", t.id)
        self.assertEqual(row["title"], "Import backup_models tasks")
        self.assertEqual(row["tags"], ["import"])
        self.assertEqual(row["prefix"], "bm")
        self.assertFalse(row["archived"])
        self.svc.set_status(t.id, "done")
        self.assertTrue(self.index.get("backup_models", t.id)["archived"])
        self.assertEqual(self.index.projects()[0]["prefix"], "bm")

    def test_find_filters(self):
        self.svc.create("gepo brevity run", area="R9.gepo", tags=["gepo"])
        self.svc.create("router lora", area="R9", tags=["lora"])
        self.svc.create("unrelated", area="R10")
        f = self.index.find
        self.assertEqual(f(project="backup_models", area="R9").total, 2)
        self.assertEqual(f(project="backup_models", area="r9.gepo").total, 1)
        self.assertEqual(f(tag="lora").total, 1)
        self.assertEqual(f(query="gepo run").total, 1)
        self.assertEqual(f(query="nothing-like-this").total, 0)
        page = f(project="backup_models", limit=2)
        self.assertEqual((page.total, len(page.rows)), (3, 2))

    def test_reconcile_picks_up_hand_edits_and_deletions(self):
        a = self.svc.create("a")
        b = self.svc.create("b")
        p = self.root / ".claude-hooks/tasks" / f"{a.id}.md"
        p.write_text(p.read_text(encoding="utf-8").replace(
            'title: "a"', 'title: "a, edited"').replace("title: a",
                                                        "title: \"a, edited\""),
            encoding="utf-8")
        (self.root / ".claude-hooks/tasks" / f"{b.id}.md").unlink()
        stats = self.svc.reconcile()
        self.assertEqual((stats.indexed, stats.removed), (1, 1))
        self.assertEqual(self.index.get("backup_models", a.id)["title"],
                         "a, edited")
        self.assertIsNone(self.index.get("backup_models", b.id))
        # Quiet: nothing re-read, nothing written.
        again = self.svc.reconcile()
        self.assertEqual((again.indexed, again.removed), (0, 0))

    def test_reconcile_repairs_a_failed_index_write(self):
        broken = TaskService(self.root, None)       # wrote file, no index
        t = broken.create("written while the index was down")
        self.assertIsNone(self.index.get("backup_models", t.id))
        self.svc.reconcile()
        self.assertIsNotNone(self.index.get("backup_models", t.id))

    def test_unreadable_file_is_skipped_not_fatal(self):
        self.svc.create("fine")
        bad = self.root / ".claude-hooks/tasks/bm-9.md"
        bad.write_text("not a task", encoding="utf-8")
        stats = self.svc.reconcile()
        self.assertEqual(stats.unreadable, 1)
        self.assertEqual(len(self.svc.board()["ready"]), 1)

    def test_prefix_avoids_another_projects(self):
        self.svc.create("x")
        other = self.tmp / "bitmap_maker"
        other.mkdir()
        svc2 = TaskService(other, self.index, host="solidpc")
        self.assertEqual(svc2.create("y").id, "bma-1")

    def test_embedding_and_similarity(self):
        a = self.svc.create("zzz zebra")
        self.svc.create("aaa apple")
        self.assertEqual(self.emb.calls, 2)
        hits = self.index.similar(self.emb("zebra zoo"), "letters",
                                  project="backup_models", k=1)
        self.assertEqual(hits[0][1]["id"], a.id)
        # A model change makes every row stale.
        self.assertEqual(len(self.index.needing_embedding("other")), 2)
        self.assertEqual(self.index.needing_embedding("letters"), [])

    def test_embed_pending_catches_up(self):
        quiet = TaskService(self.root, self.index)   # no embedder
        quiet.create("later")
        self.assertEqual(len(self.index.needing_embedding("letters")), 1)
        self.assertEqual(self.svc.embed_pending(), 1)
        self.assertEqual(self.index.needing_embedding("letters"), [])

    def test_board_reads_the_index(self):
        a = self.svc.create("a")
        self.svc.create("b", depends=[a.id])
        board = self.svc.board()
        self.assertEqual([r["id"] for r in board["ready"]], [a.id])
        self.assertEqual(len(board["blocked"]), 1)


class VectorPackingTests(unittest.TestCase):
    def test_round_trip(self):
        v = [0.5, -1.25, 3.0]
        self.assertEqual(unpack_vec(pack_vec(v)), v)
        self.assertAlmostEqual(cosine(v, v), 1.0, places=6)
        self.assertEqual(cosine([1, 0], [1, 0, 0]), 0.0)

    def test_embed_text_uses_latest_log(self):
        t = Task(id="bm-1", title="T", area="A")
        t.set_section("Description", "D")
        for i in range(5):
            t.add_log(f"step{i}")
        text = embed_text(t)
        self.assertIn("step4", text)
        self.assertNotIn("step0", text)


# ─── Postgres dialect (opt-in) ───────────────────────────────────────


_PG_DSN = os.environ.get("CLAUDE_HOOKS_TEST_PG_DSN", "")


@unittest.skipUnless(_PG_DSN, "set CLAUDE_HOOKS_TEST_PG_DSN to run against "
                              "Postgres (a throwaway schema is used)")
class PostgresIndexTests(IndexTests):
    def make_index(self):
        import psycopg
        conn = psycopg.connect(_PG_DSN)
        schema = f"tasks_test_{uuid.uuid4().hex[:8]}"
        with conn.cursor() as cur:
            cur.execute(f"CREATE SCHEMA {schema}")
            cur.execute(f"SET search_path TO {schema}")
        conn.commit()

        def drop():
            conn.rollback()
            with conn.cursor() as cur:
                cur.execute(f"DROP SCHEMA {schema} CASCADE")
            conn.commit()
            conn.close()
        self.addCleanup(drop)
        lock = threading.RLock()
        return TaskIndex(lambda: conn, lock, dialect="postgres")


if __name__ == "__main__":
    unittest.main()
