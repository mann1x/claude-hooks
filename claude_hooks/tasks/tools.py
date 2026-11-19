"""The task MCP tools, on the memory servers that already run.

Same reasoning as the mailbox: tools on the servers every client already
has need no skill load and no slash command, so a session finds them
without having to remember they exist. Named the way models expect from
issue trackers (create / ready / start / done / note), after beads'
verbs.

Writes never wait for the embedder. They return as soon as the file and
the row are written; a background thread embeds what changed.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Optional

from claude_hooks.tasks.files import TaskNotFound
from claude_hooks.tasks.model import (
    CLOSED_STATUSES, OPEN_STATUSES, STATUSES, is_task_id,
)
from claude_hooks.tasks.render import board_text, row_line, rows_text

log = logging.getLogger("claude_hooks.tasks.tools")

TOOL_NAMES = (
    "task-create", "task-ready", "task-list", "task-show", "task-start",
    "task-done", "task-wait", "task-cancel", "task-note", "task-update",
    "task-link", "task-gate",
)

_ID = {"type": "string", "description": "task id, e.g. bm-42"}
_NOTE = {"type": "string",
         "description": "optional log line: what happened, the result"}
_LIST = {"type": "array", "items": {"type": "string"}}


def tool_catalog() -> list[dict]:
    status_tool = {
        "task-start": "Mark a task active: you are working on it now.",
        "task-done": "Close a task as done. Put the outcome in `note` "
                     "(result, commit, where it is).",
        "task-wait": "Park a task on something outside the session (a "
                     "running job, an answer). Say what in `note`.",
        "task-cancel": "Close a task that will not be done. Say why in "
                       "`note`.",
    }
    tools = [
        {
            "name": "task-create",
            "description": (
                "Create a task in this project's persistent task list "
                "(files under .claude-hooks/tasks/, survives compaction and "
                "new sessions). Use for any piece of work worth tracking "
                "beyond this turn. Returns the id (e.g. bm-42)."),
            "inputSchema": {"type": "object", "properties": {
                "title": {"type": "string", "description": "one line"},
                "description": {"type": "string",
                                "description": "what and why"},
                "priority": {"type": "string", "enum": ["H", "M", "L"]},
                "area": {"type": "string",
                         "description": "dotted area, e.g. R9.gepo"},
                "tags": _LIST,
                "depends": {**_LIST, "description": (
                    "ids this waits for, from any project on this host "
                    "(e.g. op-347); it stays blocked until each is done "
                    "or cancelled")},
                "plan": {"type": "string",
                         "description": "path of the plan it belongs to"},
                "due": {"type": "string", "description": "YYYY-MM-DD"},
                "acceptance": {**_LIST,
                               "description": "checklist items for done"},
                "start": {"type": "boolean", "default": False,
                          "description": "create it already active"},
            }, "required": ["title"]},
        },
        {
            "name": "task-ready",
            "description": (
                "What to work on: active tasks, then open unblocked ones by "
                "urgency (priority, blocking others, age, due date), plus "
                "counts of waiting and blocked ones."),
            "inputSchema": {"type": "object", "properties": {
                "limit": {"type": "integer", "default": 10},
                "area": {"type": "string"},
            }},
        },
        {
            "name": "task-list",
            "description": (
                "List tasks, newest change first, one line each. Filter by "
                "status (open, closed, all, or one status), area (includes "
                "sub-areas), tag, keywords (all must match), since/until "
                "(YYYY-MM-DD). project='all' lists every project on this "
                "store. Pages of 20."),
            "inputSchema": {"type": "object", "properties": {
                "status": {"type": "string", "default": "open"},
                "area": {"type": "string"},
                "tag": {"type": "string"},
                "query": {"type": "string"},
                "since": {"type": "string"},
                "until": {"type": "string"},
                "project": {"type": "string"},
                "page": {"type": "integer", "default": 1},
                "limit": {"type": "integer", "default": 20},
            }},
        },
        {
            "name": "task-show",
            "description": "The whole task: fields, description, "
                           "acceptance, log, where its file is, and its "
                           "graph across projects (dependencies, "
                           "dependents, parent / children, related).",
            "inputSchema": {"type": "object", "properties": {"id": _ID},
                            "required": ["id"]},
        },
    ]
    for name, desc in status_tool.items():
        tools.append({"name": name, "description": desc,
                      "inputSchema": {"type": "object", "properties": {
                          "id": _ID, "note": _NOTE}, "required": ["id"]}})
    tools += [
        {
            "name": "task-note",
            "description": (
                "Append a line to a task's log: a result, a decision, a "
                "number, a path. The log is the task's history; keep the "
                "description for what the task is."),
            "inputSchema": {"type": "object", "properties": {
                "id": _ID, "text": {"type": "string"}},
                "required": ["id", "text"]},
        },
        {
            "name": "task-update",
            "description": (
                "Change fields of a task. Only what you pass changes. "
                "`check` / `uncheck` tick acceptance items by number "
                "(1-based). Every change is logged."),
            "inputSchema": {"type": "object", "properties": {
                "id": _ID,
                "title": {"type": "string"},
                "priority": {"type": "string", "enum": ["H", "M", "L"]},
                "area": {"type": "string"},
                "add_tags": _LIST, "remove_tags": _LIST,
                "add_depends": _LIST, "remove_depends": _LIST,
                "plan": {"type": "string"},
                "due": {"type": "string"},
                "description": {"type": "string",
                                "description": "replaces the description"},
                "acceptance": {**_LIST,
                               "description": "replaces the checklist"},
                "check": {"type": "array", "items": {"type": "integer"}},
                "uncheck": {"type": "array", "items": {"type": "integer"}},
                "note": _NOTE,
            }, "required": ["id"]},
        },
        {
            "name": "task-link",
            "description": (
                "Attach something to a task: a commit, file, mail id, "
                "consultancy, plan, url or another task. Graph links take "
                "a task id from any project: parent (its epic; one), "
                "discovered-from, related, caused-by, supersedes (closes "
                "the replaced task when it is in this project), "
                "duplicate-of (closes this one). Only depends blocks."),
            "inputSchema": {"type": "object", "properties": {
                "id": _ID,
                "kind": {"type": "string", "enum": [
                    "commit", "file", "mail", "consultancy", "plan", "url",
                    "task", "parent", "discovered-from", "related",
                    "caused-by", "supersedes", "duplicate-of"]},
                "value": {"type": "string"},
                "note": _NOTE,
            }, "required": ["id", "kind", "value"]},
        },
        {
            "name": "task-gate",
            "description": (
                "Make a task wait on something outside the task list; it "
                "stays blocked until the gate opens. Gates: mail:<id> (mail "
                "answered: acked with a note, or a reply naming #id), "
                "human:<what> (a person's go-ahead; resolve it by hand), "
                "timer:<ISO time>, commit:<sha>@<branch> (on the branch), "
                "gh-pr:<owner/repo>#<n> (merged), gh-run:<owner/repo>#<id> "
                "(succeeded). action: add / resolve / remove one gate, or "
                "check (every gate of the task, or of the project without "
                "id, GitHub included; resolved ones are recorded)."),
            "inputSchema": {"type": "object", "properties": {
                "id": _ID,
                "action": {"type": "string",
                           "enum": ["add", "resolve", "remove", "check"]},
                "gate": {"type": "string",
                         "description": "e.g. mail:2331, human:deploy"},
                "note": _NOTE,
            }, "required": ["action"]},
        },
    ]
    return tools


class TaskTools:
    """Dispatch for the catalog above, bound to one session's project."""

    def __init__(self, service_factory: Callable[[], Any], *,
                 background_embed: bool = True):
        self._factory = service_factory
        self._svc = None
        self._background = background_embed
        self._embedding = threading.Lock()
        self.session_fn: Optional[Callable[[], str]] = None
        #: Returns the session's mailbox tools (or None); set by the memory
        #: servers so a close can mail the projects it unblocks.
        self.mailer_fn: Optional[Callable[[], Any]] = None

    @property
    def svc(self):
        if self._svc is None:
            self._svc = self._factory()
        if self.session_fn is not None:
            sid = self.session_fn()
            if sid:
                self._svc.session_id = sid
        return self._svc

    def call(self, name: str, args: dict) -> str:
        args = args or {}
        try:
            return self._call(name, args)
        except TaskNotFound as e:
            return f"{e}. task-list shows what exists."
        except (ValueError, TimeoutError) as e:
            return f"Not done: {e}"

    # ─── dispatch ────────────────────────────────────────────────────

    def _call(self, name: str, a: dict) -> str:
        svc = self.svc
        if name == "task-create":
            t = svc.create(
                a.get("title", ""), description=a.get("description", ""),
                priority=a.get("priority") or "M", area=a.get("area", ""),
                tags=a.get("tags") or (), depends=a.get("depends") or (),
                plan=a.get("plan", ""), due=a.get("due", ""),
                acceptance=a.get("acceptance"),
                status="active" if a.get("start") else "pending")
            self._after_write()
            return f"created {row_line(_row(t))}\nfile: {_rel(svc, t.id)}"
        if name == "task-ready":
            return self._ready(int(a.get("limit") or 10), a.get("area"))
        if name == "task-list":
            return self._list(a)
        if name == "task-show":
            task, path = svc.show(_id(a))
            text = f"{path}\n\n{task.to_markdown()}"
            try:
                lines = svc.relations(task.id)
            except Exception:
                log.info("tasks: graph view failed", exc_info=True)
                lines = []
            if lines:
                text += "\n## Graph\n" + "\n".join(f"- {ln}" for ln in lines)
            return text
        if name in ("task-start", "task-done", "task-wait", "task-cancel"):
            status = {"task-start": "active", "task-done": "done",
                      "task-wait": "waiting",
                      "task-cancel": "cancelled"}[name]
            t = svc.set_status(_id(a), status, a.get("note", ""))
            self._after_write()
            out = row_line(_row(t))
            if status in CLOSED_STATUSES and svc.last_unblocked:
                out += "\n" + self._unblocked(t, status, svc.last_unblocked)
            return out
        if name == "task-note":
            t = svc.note(_id(a), a.get("text", ""))
            self._after_write()
            return f"noted on {t.id}: {t.log_lines[0]}"
        if name == "task-update":
            fields = {k: a[k] for k in (
                "title", "priority", "area", "plan", "due", "description",
                "acceptance") if k in a and a[k] is not None}
            for k in ("add_tags", "remove_tags", "add_depends",
                      "remove_depends", "check", "uncheck"):
                if a.get(k):
                    fields[k] = a[k]
            t = svc.update(_id(a), note=a.get("note", ""), **fields)
            self._after_write()
            return f"{row_line(_row(t))}\n{t.log_lines[0]}"
        if name == "task-gate":
            return self._gate(a)
        if name == "task-link":
            t = svc.link(_id(a), a.get("kind", ""), a.get("value", ""),
                         a.get("note", ""))
            self._after_write()
            # A closing link (duplicate-of) logs the close after the link.
            line = next((ln for ln in t.log_lines[:2] if " linked " in ln),
                        t.log_lines[0])
            extra = f"\n{t.log_lines[0]}" if t.log_lines[0] != line else ""
            return f"{t.id}: {line}{extra}"
        return f"unknown task tool {name}"

    def _gate(self, a: dict) -> str:
        svc = self.svc
        action = str(a.get("action") or "").strip().lower()
        if action == "check":
            tid = _id(a) if a.get("id") else ""
            mailer = self.mailer_fn() if self.mailer_fn else None
            res = svc.check_gates(task_id=tid,
                                  mail_store=getattr(mailer, "store", None))
            self._after_write()
            if not res:
                return "No open gates" + (f" on {tid}." if tid else ".")
            return "\n".join(gate_line(t, g, v) for t, g, v in res)
        t = svc.gate(_id(a), action, a.get("gate", ""), a.get("note", ""))
        self._after_write()
        return f"{row_line(_row(t))}\n{t.log_lines[0]}"

    def _unblocked(self, t, status: str, nodes: list) -> str:
        """What this close made ready, and the mail to the other projects'
        sessions (ch-28 phase 2)."""
        svc = self.svc
        here = svc.project
        items = ", ".join(n["id"] + (f" ({n['project']})"
                                     if n["project"] != here else "")
                          for n in nodes)
        lines = [f"Ready now: {items}"]
        if status != "done":
            lines.append(f"({t.id} was {status}, not done: check that what "
                         "waited for it still makes sense.)")
        if any(n["project"] != here for n in nodes) and _unblock_mail_on():
            mailer = self.mailer_fn() if self.mailer_fn else None
            if (mailer is None or svc.index is None
                    or not getattr(mailer, "alias", None)):
                # No mailbox, or no sending identity (the CLI).
                lines.append("(no mail sent from here: the other projects "
                             "will see it in task-ready)")
            else:
                from claude_hooks.tasks.notify import mail_unblocked
                lines += mail_unblocked(
                    mailer, svc.index, closed_id=t.id, closed_title=t.title,
                    closed_project=here, status=status, unblocked=nodes)
        return "\n".join(lines)

    def _ready(self, limit: int, area: Optional[str]) -> str:
        svc = self.svc
        if not svc.initialised:
            return (f"No task list yet in {svc.project}. task-create starts "
                    "one.")
        board = svc.board()
        if area:
            for k in ("active", "ready", "waiting", "blocked"):
                board[k] = [r for r in board[k] if _in_area(r, area)]
        return board_text(board, svc.project, per_section=limit, closed=0)

    def _list(self, a: dict) -> str:
        svc = self.svc
        status = (a.get("status") or "open").lower()
        statuses = {"open": OPEN_STATUSES, "closed": CLOSED_STATUSES,
                    "all": None}.get(status, (status,))
        if statuses and any(s not in STATUSES for s in statuses):
            return (f"Not done: unknown status {status!r}; use open, closed, "
                    f"all or one of {', '.join(STATUSES)}.")
        page = max(1, int(a.get("page") or 1))
        limit = max(1, min(int(a.get("limit") or 20), 100))
        project = a.get("project")
        if svc.index is None:
            if project and project not in ("", svc.project):
                return ("Listing other projects needs the SQL store "
                        "(pgvector or sqlite_vec); this host has none.")
            rows = [r for r in svc.snapshot()
                    if (statuses is None or r["status"] in statuses)
                    and (not a.get("area") or _in_area(r, a["area"]))]
            rows.sort(key=lambda r: r.get("updated") or "", reverse=True)
            total = len(rows)
            rows = rows[(page - 1) * limit: page * limit]
        else:
            svc.reconcile()
            from claude_hooks.mailbox.filters import parse_when
            since = parse_when(a.get("since"))
            until = parse_when(a.get("until"), end=True)
            res = svc.index.find(
                project=None if project == "all" else (project or svc.project),
                statuses=statuses, area=a.get("area"), tag=a.get("tag"),
                query=a.get("query"),
                since=since.strftime("%Y-%m-%dT%H:%M:%SZ") if since else None,
                until=until.strftime("%Y-%m-%dT%H:%M:%SZ") if until else None,
                page=page, limit=limit)
            total, rows = res.total, res.rows
        if not rows:
            return "No tasks match."
        pages = (total + limit - 1) // limit
        head = (f"{total} task(s), page {page} of {pages}"
                + (f" — next: page={page + 1}" if page < pages else ""))
        return head + "\n" + rows_text(rows, project=project == "all")

    def _after_write(self) -> None:
        svc = self.svc
        if not self._background or svc.embedder is None:
            return
        if not self._embedding.acquire(blocking=False):
            return                      # a pass is already running

        def run():
            try:
                svc.embed_pending(limit=20)
            except Exception:
                log.info("tasks: background embedding failed", exc_info=True)
            finally:
                self._embedding.release()
        threading.Thread(target=run, name="task-embed", daemon=True).start()


def gate_line(task_id: str, gate: str, v) -> str:
    mark = {True: "open", False: "closed", None: "unchecked"}[v.resolved]
    if v.failed:
        mark = "stuck"
    return f"{task_id} {gate}: {mark} — {v.why}"


def _unblock_mail_on() -> bool:
    try:
        from claude_hooks.config import load_config
        from claude_hooks.tasks.hook import settings
        return bool(settings(load_config()).get("unblock_mail", True))
    except Exception:
        return True


def _id(a: dict) -> str:
    raw = str(a.get("id") or "").strip().lower().lstrip("#")
    if not is_task_id(raw):
        raise ValueError(f"{a.get('id')!r} is not a task id (e.g. bm-42)")
    return raw


def _row(t) -> dict:
    return {"id": t.id, "status": t.status, "priority": t.priority,
            "area": t.area, "title": t.title, "updated": t.updated}


def _rel(svc, task_id: str) -> str:
    p = svc.dir.find(task_id)
    try:
        return str(p.relative_to(svc.dir.root)) if p else ""
    except ValueError:
        return str(p)


def _in_area(r: dict, area: str) -> bool:
    a = (r.get("area") or "").lower()
    want = area.lower()
    return a == want or a.startswith(want + ".")


def tools_for_provider(provider, *, cwd: Optional[str] = None,
                       session_id: Optional[str] = None) -> TaskTools:
    """Bound to the session's project; works with no provider (files)."""
    from claude_hooks.tasks import service_for

    def factory():
        svc = service_for(provider, cwd=cwd, session_id=_live_sid(session_id))
        svc.embed_on_write = False
        return svc
    tools = TaskTools(factory)
    if not session_id:
        # An MCP server outlives the session it was spawned in: stamp each
        # log line with the session running now, not the first one.
        tools.session_fn = lambda: _live_sid(None)
    return tools


def _live_sid(session_id: Optional[str]) -> str:
    if session_id:
        return session_id
    from claude_hooks.resources.api import session_id_here
    return session_id_here()
