"""Task operations: the files first, then the index.

Every change is written to the task file before the row. If the index
write fails, the change is still recorded where a person (and the next
:meth:`TaskService.reconcile`) will find it; the reverse order would
leave a row describing a change that never happened.

The index is optional. A host without pgvector or sqlite_vec still has
tasks: listing and ``ready`` read the files instead, and only search
across projects and semantic recall need the index.
"""
from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

from claude_hooks._atomic import write_text_atomic
from claude_hooks.tasks.files import TaskDir, TaskNotFound, file_hash
from claude_hooks.tasks.model import (
    ACCEPTANCE, CLOSED_STATUSES, DESCRIPTION, OPEN_STATUSES, PRIORITIES,
    Task, normalise_status, parse_stamp, stamp, utcnow,
)
from claude_hooks.tasks.store import TaskIndex, embed_text

log = logging.getLogger("claude_hooks.tasks")

STATE_FILE = ".index-state.json"
#: What may appear in the task folder that is not a task. Per-host state
#: now lives under ``~/.claude`` (``files.state_dir_for``); its old names
#: stay listed for folders that still hold a copy from before the move.
GITIGNORE = (f"{STATE_FILE}\n"
             ".*.lock\n"          # in-flight writes
             ".nudge-*.json\n"    # Stop-nudge memory, per session
             ".cc-*.json\n")      # Claude Code task-id mapping, per session
_LOCK_WAIT_S = 3.0
_LOCK_STALE_S = 30.0

#: Taskwarrior's coefficients, cut down to the fields we keep.
URGENCY = {
    "priority": {"H": 6.0, "M": 3.9, "L": 1.8},
    "active": 4.0,
    "blocking": 8.0,
    "blocked": -5.0,
    "waiting": -3.0,
    "age_max": 2.0,         # reached at one year
    "due_max": 12.0,        # due now or overdue; fades over two weeks
}

LINK_KINDS = ("commits", "files", "mail", "consultancies", "plans", "urls",
              "tasks")


@dataclass
class ReconcileStats:
    scanned: int = 0
    indexed: int = 0
    removed: int = 0
    unreadable: int = 0

    def __str__(self) -> str:
        return (f"{self.scanned} files, {self.indexed} re-indexed, "
                f"{self.removed} rows removed, {self.unreadable} unreadable")


def _acceptance_text(items) -> str:
    if items is None:
        return ""
    if isinstance(items, str):
        return items.strip("\n")
    out = []
    for it in items:
        s = str(it).strip()
        if not s:
            continue
        out.append(s if s.startswith("- [") else f"- [ ] {s}")
    return "\n".join(out)


def urgency(task: dict, *, blocking: int, blocked: bool,
            now: Optional[datetime] = None) -> float:
    now = now or utcnow()
    u = URGENCY["priority"].get(task.get("priority") or "M", 3.9)
    status = task.get("status")
    if status == "active":
        u += URGENCY["active"]
    if status == "waiting":
        u += URGENCY["waiting"]
    if blocking:
        u += URGENCY["blocking"]
    if blocked:
        u += URGENCY["blocked"]
    created = parse_stamp(task.get("created") or "")
    if created:
        days = max(0.0, (now - created).total_seconds() / 86400)
        u += URGENCY["age_max"] * min(1.0, days / 365)
    due = parse_stamp(task.get("due") or "")
    if due:
        days_left = (due - now).total_seconds() / 86400
        if days_left <= 0:
            u += URGENCY["due_max"]
        elif days_left < 14:
            u += URGENCY["due_max"] * (1 - days_left / 14)
    return round(u, 2)


class TaskService:
    def __init__(self, root, index: Optional[TaskIndex] = None, *,
                 host: str = "", session_id: str = "",
                 embedder: Optional[Callable[[str], Optional[list]]] = None,
                 embed_model: str = "", embed_on_write: bool = True):
        self.dir = TaskDir(Path(root))
        self.index = index
        self.host = host
        self.session_id = session_id or ""
        self.embedder = embedder
        self.embed_model = embed_model
        #: False keeps writes free of the embedder round-trip (seconds on
        #: a CPU embedder); the caller runs :meth:`embed_pending` later.
        self.embed_on_write = embed_on_write
        #: Regenerate TASKS.md after each write. Bulk callers (the
        #: importer) turn it off and write the board once at the end.
        self.board_on_write = True

    # ─── setup ───────────────────────────────────────────────────────

    @property
    def project(self) -> str:
        return self.dir.project

    @property
    def initialised(self) -> bool:
        return self.dir.exists

    def init(self, *, prefix: Optional[str] = None,
             project: Optional[str] = None) -> dict:
        if self.dir.exists:
            return self.dir.config()
        taken: set[str] = set()
        name = project or self.dir.root.name
        if self.index is not None:
            try:
                taken = self.index.prefixes_taken(except_project=name)
            except Exception:
                log.warning("tasks: could not read taken prefixes",
                            exc_info=True)
        cfg = self.dir.init(prefix=prefix, project=project, taken=taken)
        # Per-host state and in-flight locks never belong in a commit.
        write_text_atomic(self.dir.dir / ".gitignore", GITIGNORE)
        self._register()
        return cfg

    def _register(self) -> None:
        if self.index is None:
            return
        try:
            self.index.register_project(self.project, self.dir.prefix,
                                        str(self.dir.root), self.host)
        except Exception:
            log.warning("tasks: project registration failed", exc_info=True)

    # ─── locking ─────────────────────────────────────────────────────

    @contextmanager
    def _locked(self, task_id: str):
        """Serialise read-modify-write of one task across processes."""
        lock = self.dir.dir / f".{task_id}.lock"
        deadline = time.monotonic() + _LOCK_WAIT_S
        while True:
            try:
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                break
            except FileExistsError:
                try:
                    if time.time() - lock.stat().st_mtime > _LOCK_STALE_S:
                        lock.unlink()
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() > deadline:
                    raise TimeoutError(f"task {task_id} is being written by "
                                       "another session; try again")
                time.sleep(0.05)
        try:
            yield
        finally:
            try:
                lock.unlink()
            except FileNotFoundError:
                pass

    # ─── index sync ──────────────────────────────────────────────────

    def _index_one(self, task: Task, path: Path, text: str) -> None:
        if self.index is None:
            return
        try:
            self.index.upsert(self.project, task, root=str(self.dir.root),
                              host=self.host, file_hash=file_hash(text),
                              archived=path.parent.name == "archive")
        except Exception:
            log.warning("tasks: index write failed for %s (the file is "
                        "written; the next reconcile repairs the row)",
                        task.id, exc_info=True)
            return
        self._embed_one(task, file_hash(text))

    def _embed_one(self, task: Task, fhash: str) -> None:
        if (self.embedder is None or not self.embed_model
                or not self.embed_on_write):
            return
        try:
            vec = self.embedder(embed_text(task))
            if vec:
                self.index.set_embedding(self.project, task.id, vec,
                                         file_hash=fhash,
                                         model=self.embed_model)
        except Exception:
            log.info("tasks: embedding %s deferred", task.id, exc_info=True)

    def _load_state(self) -> dict:
        from claude_hooks.tasks.files import read_state
        state = read_state(self.dir.dir, STATE_FILE)
        return state if isinstance(state, dict) else {}

    def reconcile(self) -> ReconcileStats:
        """Bring the index in line with the files; the files win.

        Unchanged files are recognised by ``(mtime_ns, size)`` from a
        per-host state file, so a quiet project costs one directory
        scan, not a read of every task.
        """
        stats = ReconcileStats()
        if self.index is None or not self.dir.exists:
            return stats
        self._register()
        state = self._load_state()
        new_state: dict = {}
        known = self.index.hashes(self.project)
        seen: set[str] = set()
        for tf in self.dir.scan():
            stats.scanned += 1
            key = f"{tf.mtime_ns}:{tf.size}:{int(tf.archived)}"
            cached = state.get(tf.id)
            if cached and cached[0] == key:
                h = cached[1]
            else:
                h = None
            if h is not None and known.get(tf.id) == h:
                seen.add(tf.id)
                new_state[tf.id] = [key, h]
                continue
            try:
                text = tf.path.read_text(encoding="utf-8")
                task = Task.from_markdown(text, source=str(tf.path))
            except Exception:
                stats.unreadable += 1
                log.warning("tasks: cannot read %s", tf.path, exc_info=True)
                continue
            if task.id != tf.id:
                stats.unreadable += 1
                log.warning("tasks: %s holds id %s; skipped", tf.path, task.id)
                continue
            h = file_hash(text)
            seen.add(tf.id)
            new_state[tf.id] = [key, h]
            if known.get(tf.id) != h:
                self.index.upsert(self.project, task, root=str(self.dir.root),
                                  host=self.host, file_hash=h,
                                  archived=tf.archived)
                stats.indexed += 1
        for gone in set(known) - seen:
            self.index.delete(self.project, gone)
            stats.removed += 1
        try:
            from claude_hooks.tasks.files import write_state
            write_state(self.dir.dir, STATE_FILE, new_state)
        except OSError:
            log.debug("tasks: state file not written", exc_info=True)
        return stats

    def embed_pending(self, limit: int = 50) -> int:
        """Embed rows missing a current vector; returns how many."""
        if self.index is None or self.embedder is None or not self.embed_model:
            return 0
        n = 0
        for row in self.index.needing_embedding(
                self.embed_model, project=self.project, limit=limit):
            try:
                task, _, text = self.dir.read(row["id"])
            except (TaskNotFound, Exception):
                continue
            vec = self.embedder(embed_text(task))
            if not vec:
                break
            self.index.set_embedding(self.project, task.id, vec,
                                     file_hash=file_hash(text),
                                     model=self.embed_model)
            n += 1
        return n

    # ─── writes ──────────────────────────────────────────────────────

    def create(self, title: str, *, description: str = "",
               priority: str = "M", area: str = "",
               tags: Sequence[str] = (), depends: Sequence[str] = (),
               plan: str = "", due: str = "", acceptance=None,
               status: str = "pending", note: str = "") -> Task:
        title = " ".join(str(title or "").split())
        if not title:
            raise ValueError("a task needs a title")
        if not self.dir.exists:
            self.init()
        priority = (priority or "M").upper()[:1]
        if priority not in PRIORITIES:
            raise ValueError(f"priority is one of {', '.join(PRIORITIES)}")
        status = normalise_status(status)
        deps = self._check_depends(depends)

        def make(task_id: str) -> Task:
            now = utcnow()
            t = Task(id=task_id, title=title, status=status,
                     priority=priority, area=area.strip(),
                     tags=_clean_list(tags), depends=deps, plan=plan.strip(),
                     due=_due(due), created=stamp(now))
            t.set_section(DESCRIPTION, description)
            if acceptance:
                t.set_section(ACCEPTANCE, _acceptance_text(acceptance))
            t.add_log(note or "created", session=self.session_id, when=now)
            t.touch(self.session_id, now)
            return t

        task, path, text = self.dir.create(make)
        self._index_one(task, path, text)
        self._refresh_board()
        return task

    def _modify(self, task_id: str, change: Callable[[Task], None]) -> Task:
        task_id = task_id.strip().lower()
        with self._locked(task_id):
            task, _, _ = self.dir.read(task_id)
            change(task)
            task.touch(self.session_id)
            path, text = self.dir.write(task)
        self._index_one(task, path, text)
        self._refresh_board()
        return task

    def _refresh_board(self) -> None:
        if not self.board_on_write:
            return
        try:
            from claude_hooks.tasks.board import write_board
            write_board(self)
        except Exception:
            log.warning("tasks: TASKS.md not regenerated", exc_info=True)

    def set_status(self, task_id: str, status: str, note: str = "") -> Task:
        status = normalise_status(status)

        def change(t: Task) -> None:
            if t.status == status and not note:
                return
            prev = t.status
            t.status = status
            msg = f"{prev} → {status}" if prev != status else status
            t.add_log(f"{msg}: {note}" if note else msg,
                      session=self.session_id)
        return self._modify(task_id, change)

    def note(self, task_id: str, text: str) -> Task:
        if not str(text or "").strip():
            raise ValueError("a note needs text")
        return self._modify(task_id, lambda t: t.add_log(
            text, session=self.session_id) and None)

    def update(self, task_id: str, *, title: Optional[str] = None,
               priority: Optional[str] = None, area: Optional[str] = None,
               tags: Optional[Sequence[str]] = None,
               add_tags: Sequence[str] = (), remove_tags: Sequence[str] = (),
               depends: Optional[Sequence[str]] = None,
               add_depends: Sequence[str] = (),
               remove_depends: Sequence[str] = (),
               plan: Optional[str] = None, due: Optional[str] = None,
               description: Optional[str] = None, acceptance=None,
               check: Sequence[int] = (), uncheck: Sequence[int] = (),
               note: str = "") -> Task:
        if priority is not None:
            priority = priority.upper()[:1]
            if priority not in PRIORITIES:
                raise ValueError(f"priority is one of {', '.join(PRIORITIES)}")
        new_deps = (self._check_depends(depends, self_id=task_id)
                    if depends is not None else None)
        added_deps = self._check_depends(add_depends, self_id=task_id)

        def change(t: Task) -> None:
            what = []
            if title is not None and title.strip() != t.title:
                t.title = " ".join(title.split())
                what.append("title")
            if priority is not None and priority != t.priority:
                what.append(f"priority {t.priority}→{priority}")
                t.priority = priority
            if area is not None and area.strip() != t.area:
                t.area = area.strip()
                what.append(f"area {t.area or '-'}")
            if tags is not None:
                t.tags = _clean_list(tags)
                what.append("tags")
            for tg in _clean_list(add_tags):
                if tg not in t.tags:
                    t.tags.append(tg)
                    what.append(f"+{tg}")
            for tg in _clean_list(remove_tags):
                if tg in t.tags:
                    t.tags.remove(tg)
                    what.append(f"-{tg}")
            if new_deps is not None:
                t.depends = new_deps
                what.append("depends")
            for d in added_deps:
                if d not in t.depends:
                    t.depends.append(d)
                    what.append(f"depends +{d}")
            for d in _clean_list(remove_depends):
                if d.lower() in t.depends:
                    t.depends.remove(d.lower())
                    what.append(f"depends -{d}")
            if plan is not None and plan.strip() != t.plan:
                t.plan = plan.strip()
                what.append("plan")
            if due is not None:
                t.due = _due(due)
                what.append(f"due {t.due or '-'}")
            if description is not None:
                t.set_section(DESCRIPTION, description)
                what.append("description")
            if acceptance is not None:
                t.set_section(ACCEPTANCE, _acceptance_text(acceptance))
                what.append("acceptance")
            if check or uncheck:
                t.set_section(ACCEPTANCE, _toggle(t.acceptance, check, uncheck))
                what.append("acceptance " + " ".join(
                    [f"✓{i}" for i in check] + [f"✗{i}" for i in uncheck]))
            if what or note:
                line = "updated " + ", ".join(what) if what else ""
                if note:
                    line = f"{line}: {note}" if line else note
                t.add_log(line, session=self.session_id)
        return self._modify(task_id, change)

    def link(self, task_id: str, kind: str, value: str, note: str = "") -> Task:
        kind = kind.strip().lower()
        if not kind.endswith("s"):
            kind += "s"
        if kind == "commitss":
            kind = "commits"
        if kind not in LINK_KINDS:
            raise ValueError(f"link kind is one of {', '.join(LINK_KINDS)}")
        value = str(value).strip()
        if not value:
            raise ValueError("a link needs a value")

        def change(t: Task) -> None:
            if kind == "plans" and not t.plan:
                t.plan = value
            else:
                items = list(t.links.get(kind) or [])
                if value in items:
                    return
                items.append(value)
                t.links[kind] = items
            t.add_log(f"linked {kind[:-1]} {value}" + (f": {note}" if note
                                                        else ""),
                      session=self.session_id)
        return self._modify(task_id, change)

    def _check_depends(self, deps: Optional[Iterable[str]], *,
                       self_id: str = "") -> list[str]:
        out = []
        for d in _clean_list(deps or ()):
            d = d.lower()
            if d == self_id.lower():
                raise ValueError("a task cannot depend on itself")
            if self.dir.exists and self.dir.find(d) is None:
                raise ValueError(f"no task {d} to depend on")
            out.append(d)
        return out

    # ─── reads ───────────────────────────────────────────────────────

    def show(self, task_id: str) -> tuple[Task, Path]:
        task, path, _ = self.dir.read(task_id)
        return task, path

    def snapshot(self) -> list[dict]:
        """Every task as a row, from the index when there is one."""
        if self.index is not None:
            try:
                self.reconcile()
                return self.index.all_rows(self.project)
            except Exception:
                log.warning("tasks: index unavailable, reading files",
                            exc_info=True)
        rows = []
        for task, tf, text in self.dir.iter_tasks():
            rows.append({
                "id": task.id, "num": task.number, "title": task.title,
                "status": task.status, "priority": task.priority,
                "area": task.area, "tags": task.tags,
                "depends": task.depends, "plan": task.plan, "due": task.due,
                "created": task.created, "updated": task.updated,
                "archived": tf.archived,
                "log": "\n".join(task.log_lines[:3]),
                "description": task.description,
            })
        return rows

    def board(self, rows: Optional[list[dict]] = None) -> dict:
        """Open tasks split into active / ready / waiting / blocked, each
        ordered by urgency, plus recently closed ones."""
        rows = self.snapshot() if rows is None else rows
        status_of = {r["id"]: r["status"] for r in rows}
        blocking: dict[str, int] = {}
        for r in rows:
            if r["status"] in OPEN_STATUSES:
                for d in r.get("depends") or []:
                    blocking[d] = blocking.get(d, 0) + 1
        out: dict = {"active": [], "ready": [], "waiting": [], "blocked": [],
                     "closed": []}
        now = utcnow()
        for r in rows:
            if r["status"] in CLOSED_STATUSES:
                out["closed"].append(r)
                continue
            open_deps = [d for d in r.get("depends") or []
                         if status_of.get(d) in OPEN_STATUSES]
            r = dict(r, open_depends=open_deps)
            r["urgency"] = urgency(r, blocking=blocking.get(r["id"], 0),
                                   blocked=bool(open_deps), now=now)
            if r["status"] == "active":
                out["active"].append(r)
            elif r["status"] == "waiting":
                out["waiting"].append(r)
            elif open_deps:
                out["blocked"].append(r)
            else:
                out["ready"].append(r)
        for k in ("active", "ready", "waiting", "blocked"):
            out[k].sort(key=lambda r: (-r["urgency"], r.get("num", 0)))
        out["closed"].sort(key=lambda r: r.get("updated") or "", reverse=True)
        return out


def _clean_list(items) -> list[str]:
    if items is None:
        return []
    if isinstance(items, str):
        items = items.replace(",", " ").split()
    return [str(i).strip() for i in items if str(i).strip()]


def _due(text: str) -> str:
    if not text:
        return ""
    dt = parse_stamp(str(text).strip())
    if dt is None:
        raise ValueError(f"due must be a date like 2026-10-15, not {text!r}")
    return stamp(dt)


def _toggle(text: str, check: Sequence[int], uncheck: Sequence[int]) -> str:
    """Tick or untick acceptance items by their 1-based position."""
    lines = text.splitlines()
    boxes = [i for i, ln in enumerate(lines)
             if ln.strip().lower().startswith(("- [ ]", "- [x]"))]
    for n, mark in [(c, "x") for c in check] + [(u, " ") for u in uncheck]:
        if not 1 <= int(n) <= len(boxes):
            raise ValueError(f"acceptance has {len(boxes)} items; "
                             f"no item {n}")
        i = boxes[int(n) - 1]
        ln = lines[i]
        j = ln.index("- [")
        lines[i] = ln[:j] + f"- [{mark}]" + ln[j + 5:]
    return "\n".join(lines)
