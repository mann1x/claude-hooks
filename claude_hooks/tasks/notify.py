"""Closing a task tells the projects it unblocks (ch-28 phase 2).

When ``task-done`` / ``task-cancel`` closes the last open dependency of
a task in another project, the session that works in that project is
mailed and its doorbell is rung (the reply to ``mailbox-send`` carries
the ``SendMessage`` line). The session is found the way a person would:
the live registration whose working directory is the project's indexed
root on its host. Nothing is written into the other project; it sees
the task ready in its own ``task-ready`` either way, and the mail only
makes it see it now.
"""
from __future__ import annotations

import logging
import os
from pathlib import PurePath
from typing import Iterable, Optional

log = logging.getLogger("claude_hooks.tasks")


def _norm(p: str) -> str:
    return os.path.normcase(os.path.normpath(str(p))).rstrip("\\/")


def _under(cwd: str, root: str) -> bool:
    if not cwd or not root:
        return False
    c, r = _norm(cwd), _norm(root)
    if c == r:
        return True
    try:
        return PurePath(r) in PurePath(c).parents
    except ValueError:
        return False


def project_session(store, index, project: str) -> Optional[tuple]:
    """``(alias, host)`` of the live session working in ``project``'s
    indexed root, the most recently seen one first; None when no session
    is registered there."""
    row = index.project_row(project)
    if not row:
        return None
    best = None
    for s in store.sessions():
        if row["host"] and s.host != row["host"]:
            continue
        if not _under(getattr(s, "cwd", ""), row["root"]):
            continue
        if best is None or (s.last_seen or "") > (best.last_seen or ""):
            best = s
    return (best.alias, best.host) if best else None


def mail_unblocked(mailer, index, *, closed_id: str, closed_title: str,
                   closed_project: str, status: str,
                   unblocked: Iterable[dict]) -> list[str]:
    """Mail each other project's session about the tasks this close made
    ready; return one line per project for the closing session's reply
    (the send's own reply, doorbell included, or why nothing was sent)."""
    by_project: dict[str, list[dict]] = {}
    for n in unblocked:
        if n["project"] != closed_project:
            by_project.setdefault(n["project"], []).append(n)
    out = []
    for project, nodes in sorted(by_project.items()):
        ids = ", ".join(n["id"] for n in nodes)
        try:
            who = project_session(mailer.store, index, project)
        except Exception:
            log.info("tasks: session lookup failed for %s", project,
                     exc_info=True)
            who = None
        if who is None:
            out.append(f"{project}: no session is registered in its "
                       f"checkout; it will see {ids} ready in task-ready")
            continue
        alias, host = who
        if (alias, host) == (getattr(mailer, "alias", None),
                             getattr(mailer, "host", None)):
            continue                    # our own session: the reply says it
        verb = "done" if status == "done" else status
        lines = "\n".join(f"- {n['id']} · {n.get('title', '')}" for n in nodes)
        caveat = ("" if status == "done" else
                  f"\n\n{closed_id} was {status}, not done: check that "
                  "what waited for it still makes sense.")
        body = (f"{closed_id} ({closed_project}: {closed_title}) is {verb}. "
                f"It was the last open dependency of:\n{lines}\n\n"
                f"They are ready now (task-ready).{caveat}\n\n"
                "— sent by the task graph when a dependency closes "
                "(hooks.tasks.unblock_mail turns it off)")
        try:
            reply = mailer.call("mailbox-send", {
                "to": f"{alias}@{host}",
                "subject": f"{closed_id} {verb}: {ids} ready in {project}",
                "body": body})
        except Exception as e:
            out.append(f"{project}: mail to {alias}@{host} failed ({e})")
            continue
        out.append(f"{project}: {reply.strip()}")
    return out
