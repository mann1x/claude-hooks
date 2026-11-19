"""One checkout indexes a project (ch-28 phase 0, docs/PLAN-task-graph.md).

Index rows are keyed on the project name, which every checkout of a
repository shares. Before this, a session in an older release worktree
rewrote the rows with that branch's copies and deleted every row whose
file the branch lacked, and a worktree could hand out a number the main
checkout already had. The rules, agreed with the opencoti integrator
(mail #2331 / #2332): a linked worktree never writes the index and never
creates or closes a task; on a branch it may edit the tasks it has (a
worker's change travels on its branch); detached, or named by
``read_only_worktrees``, it changes nothing.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from claude_hooks.tasks.files import (
    Checkout, TaskDir, checkout_of, git_main_root,
)
from claude_hooks.tasks.service import TaskService
from claude_hooks.tasks.store import TaskIndex
from tests.test_tasks import _SqliteConn


def make_worktree(main: Path, path: Path, *, name: str = "",
                  head: str = "ref: refs/heads/wt/x") -> Path:
    """Lay out a linked worktree as git does: ``<path>/.git`` names
    ``<main>/.git/worktrees/<name>``, whose ``commondir`` is ``../..``."""
    name = name or path.name
    admin = main / ".git" / "worktrees" / name
    admin.mkdir(parents=True)
    (admin / "commondir").write_text("../..\n", encoding="utf-8")
    (admin / "HEAD").write_text(head + "\n", encoding="utf-8")
    path.mkdir(parents=True, exist_ok=True)
    (path / ".git").write_text(f"gitdir: {admin}\n", encoding="utf-8")
    return path


class _Repo(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()
        self.main = self.tmp / "opencoti"
        (self.main / ".git").mkdir(parents=True)
        db = _SqliteConn(self.tmp / "t.db")
        self.addCleanup(db.close)
        self.index = TaskIndex(db, db.lock, dialect="sqlite")

    def svc(self, root: Path, *, host: str = "solidpc",
            read_only=()) -> TaskService:
        return TaskService(root, self.index, host=host, session_id="s1",
                           read_only_worktrees=read_only)

    def seed_main(self, n: int = 3) -> TaskService:
        s = self.svc(self.main)
        s.init(prefix="op", project="opencoti")
        for i in range(n):
            s.create(f"task {i + 1}")
        return s

    def snapshot_into(self, wt: Path) -> None:
        """The committed task folder as a worktree carries it."""
        shutil.copytree(self.main / ".claude-hooks", wt / ".claude-hooks")

    def rows(self) -> dict:
        return {r["id"]: (r["title"], r["root"])
                for r in self.index.all_rows("opencoti")}


class CheckoutKindTests(_Repo):
    def test_main_and_no_git_are_main(self):
        self.assertEqual(checkout_of(self.main), Checkout())
        plain = self.tmp / "plain"
        plain.mkdir()
        self.assertFalse(checkout_of(plain).mirror)

    def test_a_worktree_on_a_branch_is_a_worker(self):
        wt = make_worktree(self.main, self.tmp / "opencoti-w-drafter")
        co = checkout_of(wt)
        self.assertEqual(co.kind, "worker")
        self.assertEqual(co.main_root, self.main)
        self.assertEqual(git_main_root(wt), self.main)

    def test_a_detached_worktree_is_read_only(self):
        wt = make_worktree(self.main, self.tmp / "opencoti-rel-c14r17",
                           head="0123456789abcdef0123456789abcdef01234567")
        self.assertEqual(checkout_of(wt).kind, "read-only")

    def test_a_glob_makes_a_branch_worktree_read_only(self):
        wt = make_worktree(self.main, self.tmp / "opencoti-rel-c14r10",
                           head="ref: refs/heads/release/c14-r10")
        self.assertEqual(checkout_of(wt).kind, "worker")
        self.assertEqual(checkout_of(wt, ["opencoti-rel-*"]).kind,
                         "read-only")
        self.assertEqual(checkout_of(wt, ["other-*"]).kind, "worker")

    def test_a_submodule_or_bare_repository_has_no_main_checkout(self):
        sub = self.tmp / "sub"
        admin = self.main / ".git" / "modules" / "sub"
        admin.mkdir(parents=True)
        sub.mkdir()
        (sub / ".git").write_text(f"gitdir: {admin}\n", encoding="utf-8")
        self.assertFalse(checkout_of(sub).mirror)       # no commondir
        bare = self.tmp / "bare.git"
        (bare / "worktrees" / "w").mkdir(parents=True)
        (bare / "worktrees" / "w" / "commondir").write_text("../..\n")
        (bare / "worktrees" / "w" / "HEAD").write_text("ref: refs/heads/x\n")
        w = self.tmp / "w"
        w.mkdir()
        (w / ".git").write_text(f"gitdir: {bare / 'worktrees' / 'w'}\n")
        self.assertIsNone(git_main_root(w))

    def test_a_relative_gitdir_resolves(self):
        wt = self.tmp / "rel-wt"
        make_worktree(self.main, wt)
        (wt / ".git").write_text("gitdir: ../opencoti/.git/worktrees/rel-wt\n")
        self.assertEqual(git_main_root(wt), self.main)


class MirrorLeavesTheIndexAloneTests(_Repo):
    def test_reconcile_in_an_older_worktree_changes_no_row(self):
        main = self.seed_main(3)
        wt = make_worktree(self.main, self.tmp / "opencoti-rel-c12",
                           head="ref: refs/heads/c12-cut")
        self.snapshot_into(wt)
        main.create("task 4, after the release branched")
        # The branch's copy of op-2 differs from main's.
        f = wt / ".claude-hooks" / "tasks" / "op-2.md"
        f.write_text(f.read_text().replace("task 2", "task 2 (old)"))
        before = self.rows()
        self.assertEqual(len(before), 4)

        stats = self.svc(wt).reconcile()

        self.assertEqual(stats.indexed + stats.removed, 0)
        self.assertEqual(self.rows(), before)
        self.assertEqual(self.index.project_row("opencoti")["root"],
                         str(self.main))

    def test_snapshot_in_a_mirror_reads_its_own_files(self):
        self.seed_main(2)
        wt = make_worktree(self.main, self.tmp / "opencoti-w-x")
        self.snapshot_into(wt)
        ids = {r["id"] for r in self.svc(wt).snapshot()}
        self.assertEqual(ids, {"op-1", "op-2"})


class WorkerTests(_Repo):
    def setUp(self):
        super().setUp()
        self.seed_main(2)
        self.wt = make_worktree(self.main, self.tmp / "opencoti-w-drafter")
        self.snapshot_into(self.wt)
        self.w = self.svc(self.wt)
        self.before = self.rows()

    def test_create_and_close_are_refused_naming_the_main_checkout(self):
        for call in (lambda: self.w.create("new"),
                     lambda: self.w.set_status("op-1", "done"),
                     lambda: self.w.set_status("op-1", "cancelled")):
            with self.assertRaises(ValueError) as cm:
                call()
            self.assertIn(str(self.main), str(cm.exception))
            self.assertIn("worker worktree", str(cm.exception))
        self.assertFalse((self.wt / ".claude-hooks/tasks/op-3.md").exists())
        self.assertEqual(self.rows(), self.before)

    def test_its_own_task_is_edited_in_the_worktree_file(self):
        main_text = (self.main / ".claude-hooks/tasks/op-1.md").read_text()
        self.w.set_status("op-1", "active", "worker started")
        self.w.note("op-1", "probe ran")
        self.w.update("op-1", acceptance=["a"], check=[])
        text = (self.wt / ".claude-hooks/tasks/op-1.md").read_text()
        self.assertIn("status: active", text)
        self.assertIn("probe ran", text)
        self.assertEqual(
            (self.main / ".claude-hooks/tasks/op-1.md").read_text(),
            main_text)
        self.assertEqual(self.rows(), self.before)

    def test_a_worktree_with_no_folder_starts_no_project(self):
        bare = make_worktree(self.main, self.tmp / "opencoti-w-empty")
        with self.assertRaises(ValueError):
            self.svc(bare).create("would start project opencoti-w-empty")
        self.assertFalse((bare / ".claude-hooks").exists())

    def test_a_worktree_tracking_another_project_is_its_own_main(self):
        other = make_worktree(self.main, self.tmp / "side")
        s = self.svc(other)
        TaskDir(other).init(prefix="sd", project="side-project")
        self.assertFalse(s.checkout.mirror)
        self.assertEqual(s.create("own task").id, "sd-1")


class ReadOnlyTests(_Repo):
    def test_every_write_is_refused_and_reads_work(self):
        self.seed_main(2)
        wt = make_worktree(self.main, self.tmp / "opencoti-rel-c14r10",
                           head="ref: refs/heads/release/c14-r10")
        self.snapshot_into(wt)
        s = self.svc(wt, read_only=["opencoti-rel-*"])
        for call in (lambda: s.note("op-1", "x"),
                     lambda: s.set_status("op-1", "active"),
                     lambda: s.update("op-1", title="y"),
                     lambda: s.link("op-1", "commit", "abc"),
                     lambda: s.create("z")):
            with self.assertRaises(ValueError) as cm:
                call()
            self.assertIn("read-only worktree", str(cm.exception))
        self.assertEqual(s.show("op-1")[0].title, "task 1")
        # It shows the main checkout's live list, not its snapshot.
        self.svc(self.main).create("created on main after the release")
        s2 = self.svc(wt, read_only=["opencoti-rel-*"])
        self.assertEqual(len(s2.board()["ready"]), 3)
        self.assertEqual(s2.dir.root, self.main)
        self.assertEqual(s2.origin, wt)
        self.assertEqual(s2.reconcile().indexed, 0)


class OwnershipTests(_Repo):
    def clone(self, name="opencoti-clone") -> Path:
        c = self.tmp / name
        (c / ".git").mkdir(parents=True)
        self.snapshot_into(c)
        return c

    def test_another_live_root_never_takes_the_rows(self):
        main = self.seed_main(2)
        main.create("only on main")
        c = self.clone()
        before = self.rows()
        other = self.svc(c)
        self.assertFalse(other._writes_index())
        self.assertEqual(other.reconcile().indexed, 0)
        other.note("op-1", "clone edit")          # its file, not the index
        self.assertEqual(self.rows(), before)
        self.assertEqual(self.index.project_row("opencoti")["root"],
                         str(self.main))

    def test_another_host_never_takes_the_rows(self):
        self.seed_main(1)
        self.assertFalse(self.svc(self.main, host="pandorum")._writes_index())

    def test_a_vanished_root_on_this_host_is_taken_over(self):
        self.seed_main(1)
        c = self.clone()
        shutil.rmtree(self.main / ".claude-hooks")
        s = self.svc(c)
        self.assertTrue(s._writes_index())
        self.assertEqual(self.index.project_row("opencoti")["root"], str(c))

    def test_rows_registered_from_a_worktree_move_to_its_main(self):
        wt = make_worktree(self.main, self.tmp / "opencoti-w-old")
        self.seed_main(1)
        self.snapshot_into(wt)
        self.index.register_project("opencoti", "op", str(wt), "solidpc")
        self.assertTrue(self.svc(self.main)._writes_index())
        self.assertEqual(self.index.project_row("opencoti")["root"],
                         str(self.main))

    def test_adopt_moves_the_rows_and_is_refused_in_a_mirror(self):
        self.seed_main(2)
        c = self.clone()
        s = self.svc(c)
        s.adopt()
        self.assertEqual(self.index.project_row("opencoti")["root"], str(c))
        self.assertEqual({k: v[1] for k, v in self.rows().items()},
                         {"op-1": str(c), "op-2": str(c)})
        wt = make_worktree(self.main, self.tmp / "opencoti-w-a")
        self.snapshot_into(wt)
        with self.assertRaises(ValueError):
            self.svc(wt).adopt()


class IdFloorTests(_Repo):
    def test_a_lagging_checkout_never_reuses_an_indexed_number(self):
        main = self.seed_main(2)
        c = self.tmp / "opencoti-clone"
        (c / ".git").mkdir(parents=True)
        self.snapshot_into(c)                      # holds op-1, op-2
        main.create("op-3 on main")
        main.create("op-4 on main")
        t = self.svc(c).create("made on the lagging clone")
        self.assertEqual(t.id, "op-5")

    def test_the_folder_floor_alone_still_counts(self):
        d = TaskDir(self.main)
        d.init(prefix="op", project="opencoti")
        from claude_hooks.tasks.model import Task

        def make(i):
            return Task(id=i, title="t")
        self.assertEqual(d.create(make, floor=0)[0].id, "op-1")
        self.assertEqual(d.create(make, floor=9)[0].id, "op-10")
        self.assertEqual(d.create(make, floor=2)[0].id, "op-11")


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class RealGitWorktreeTests(unittest.TestCase):
    def test_git_worktree_add_is_read_as_git_writes_it(self):
        with tempfile.TemporaryDirectory() as t:
            base = Path(t).resolve()
            main = base / "repo"
            main.mkdir()

            def git(*a, cwd=main):
                subprocess.run(["git", *a], cwd=cwd, check=True,
                               capture_output=True)
            git("init", "-q")
            (main / "f").write_text("x")
            git("add", "f")
            git("-c", "user.name=t", "-c", "user.email=t@example.invalid",
                "commit", "-q", "-m", "c")
            git("worktree", "add", "-q", "-b", "wt/x", str(base / "w"))
            git("worktree", "add", "-q", "--detach", str(base / "d"))
            self.assertEqual(checkout_of(base / "w"),
                             Checkout("worker", main))
            self.assertEqual(checkout_of(base / "d").kind, "read-only")
            self.assertEqual(checkout_of(main), Checkout())


if __name__ == "__main__":
    unittest.main()
