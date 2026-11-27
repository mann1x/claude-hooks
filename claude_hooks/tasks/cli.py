"""``claude-hooks-tasks``: the task list from a terminal.

Runs against the project the current directory belongs to, like ``git``.
The verbs mirror the MCP tools, so what a person types and what a
session calls produce the same files.

    claude-hooks-tasks ready
    claude-hooks-tasks list [--status all] [--area R9] [-q words] [--all-projects]
    claude-hooks-tasks show bm-42
    claude-hooks-tasks add "title" [-p H] [--area R9] [--depends bm-41]
    claude-hooks-tasks start|done|wait|cancel bm-42 [-m "note"]
    claude-hooks-tasks note bm-42 "text"
    claude-hooks-tasks update bm-42 [--add-depends op-296] [-p H] [--defer 2026-12-01] [--check 1 2] [-m "why"]
    claude-hooks-tasks gate bm-42 add mail:2331   # waits until answered
    claude-hooks-tasks gates            # check every open gate, GitHub too
    claude-hooks-tasks graph [bm-42] [--up|--down] [--all] [--mermaid]
    claude-hooks-tasks blocked          # what blocks each blocked task
    claude-hooks-tasks init [--prefix bm]
    claude-hooks-tasks reindex          # reconcile files → index, embed
    claude-hooks-tasks where            # this checkout, and who indexes it
    claude-hooks-tasks adopt            # index the project from this checkout
    claude-hooks-tasks board            # regenerate TASKS.md
    claude-hooks-tasks import --transcript <jsonl> [--dry-run]
"""
from __future__ import annotations

import argparse
import sys
from typing import Optional


def _service(args, *, embed: bool = False):
    from claude_hooks.tasks import service_for, sql_provider
    provider = None
    if not args.no_index:
        try:
            from claude_hooks.config import load_config
            from claude_hooks.dispatcher import build_providers
            provider = sql_provider(build_providers(load_config()))
        except Exception as e:
            print(f"(no index: {e})", file=sys.stderr)
    return service_for(provider, cwd=args.dir, embed=embed)


def _tools(args):
    from claude_hooks.tasks.tools import TaskTools
    tools = TaskTools(lambda: _service(args), background_embed=False)
    tools.mailer_fn = lambda: _MailStoreOnly(tools.svc)
    return tools


class _MailStoreOnly:
    """The host's mailbox store without a sending identity: the CLI can
    check mail gates but sends no mail (closing lists what it unblocks)."""

    def __init__(self, svc):
        self.store = None
        if svc.index is not None:
            try:
                from claude_hooks.config import load_config
                from claude_hooks.dispatcher import build_providers
                from claude_hooks.mailbox.integration import store_for_provider
                from claude_hooks.tasks import sql_provider
                self.store = store_for_provider(
                    sql_provider(build_providers(load_config())))
            except Exception:
                self.store = None

    def call(self, name, args):
        return "(the CLI sends no mail; the project sees it in task-ready)"


def main(argv: Optional[list[str]] = None) -> int:
    from claude_hooks.mcp_stdio import force_utf8_stdio
    force_utf8_stdio()
    ap = argparse.ArgumentParser(prog="claude-hooks-tasks",
                                 description="Persistent task list.")
    ap.add_argument("-C", "--dir", help="project directory (default: cwd)")
    ap.add_argument("--no-index", action="store_true",
                    help="files only; skip the SQL index")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("ready", help="what to work on now")
    p.add_argument("-n", "--limit", type=int, default=15)
    p.add_argument("--area")

    p = sub.add_parser("list", help="list tasks")
    p.add_argument("--status", default="open")
    p.add_argument("--area")
    p.add_argument("--tag")
    p.add_argument("-q", "--query")
    p.add_argument("--since")
    p.add_argument("--until")
    p.add_argument("--all-projects", action="store_true")
    p.add_argument("--page", type=int, default=1)
    p.add_argument("-n", "--limit", type=int, default=30)

    p = sub.add_parser("show", help="show one task")
    p.add_argument("id")

    p = sub.add_parser("add", help="create a task")
    p.add_argument("title")
    p.add_argument("-d", "--description", default="")
    p.add_argument("-p", "--priority", default="M")
    p.add_argument("--area", default="")
    p.add_argument("--tags", nargs="*", default=[])
    p.add_argument("--depends", nargs="*", default=[])
    p.add_argument("--plan", default="")
    p.add_argument("--due", default="")
    p.add_argument("--accept", nargs="*", default=None,
                   help="acceptance checklist items")
    p.add_argument("--start", action="store_true")

    for verb in ("start", "done", "wait", "cancel"):
        p = sub.add_parser(verb, help=f"mark a task {verb}")
        p.add_argument("id")
        p.add_argument("-m", "--note", default="")

    p = sub.add_parser("update", help="change fields of a task (only what "
                                      "you pass changes; logged)")
    p.add_argument("id")
    p.add_argument("-t", "--title")
    p.add_argument("-p", "--priority", choices=["H", "M", "L", "h", "m", "l"])
    p.add_argument("--area")
    p.add_argument("--plan")
    p.add_argument("--due", help="YYYY-MM-DD; empty clears")
    p.add_argument("--defer", help="YYYY-MM-DD: hide from ready until "
                                   "then; empty clears")
    p.add_argument("-d", "--description", help="replaces the description")
    p.add_argument("--accept", nargs="*", default=None,
                   help="replaces the acceptance checklist")
    p.add_argument("--add-depends", nargs="+", default=[], metavar="ID",
                   help="any project's id (op-296); unknown ids and "
                        "cycles are refused")
    p.add_argument("--remove-depends", nargs="+", default=[], metavar="ID")
    p.add_argument("--add-tags", nargs="+", default=[], metavar="TAG")
    p.add_argument("--remove-tags", nargs="+", default=[], metavar="TAG")
    p.add_argument("--check", nargs="+", type=int, default=[], metavar="N",
                   help="tick acceptance items (1-based)")
    p.add_argument("--uncheck", nargs="+", type=int, default=[],
                   metavar="N")
    p.add_argument("-m", "--note", default="", help="log line: why")

    p = sub.add_parser("note", help="append a log line")
    p.add_argument("id")
    p.add_argument("text")

    p = sub.add_parser("gate", help="add / resolve / remove a gate on a "
                                    "task (mail:<id>, human:<what>, …)")
    p.add_argument("id")
    p.add_argument("action", choices=["add", "resolve", "remove", "check"])
    p.add_argument("gate", nargs="?", default="")
    p.add_argument("-m", "--note", default="")

    p = sub.add_parser("gates", help="check every open gate (GitHub too)")

    p = sub.add_parser("graph", help="dependency layers across projects")
    p.add_argument("id", nargs="?")
    p.add_argument("--up", action="store_true", help="what waits for it")
    p.add_argument("--down", action="store_true", help="what it waits for")
    p.add_argument("--all", action="store_true", help="every project")
    p.add_argument("--mermaid", action="store_true")

    sub.add_parser("blocked", help="every blocked task and what blocks it")

    p = sub.add_parser("link", help="attach a commit/file/mail/plan/…")
    p.add_argument("id")
    p.add_argument("kind")
    p.add_argument("value")

    p = sub.add_parser("init", help="create the task folder")
    p.add_argument("--prefix")
    p.add_argument("--project")

    sub.add_parser("reindex", help="sync files into the index and embed")

    sub.add_parser("where", help="this checkout's standing and the "
                                 "project's indexing checkout")

    sub.add_parser("adopt", help="make this checkout the one that indexes "
                                 "the project (after moving it)")

    sub.add_parser("board", help="regenerate TASKS.md")

    p = sub.add_parser("import", help="import Claude Code task calls")
    p.add_argument("--transcript", required=True,
                   help="session .jsonl (or a pre-filtered extract)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--session", default="",
                   help="session id to credit (default: from the file)")

    args = ap.parse_args(argv)
    cmd = args.cmd

    if cmd in ("ready", "list", "show", "add", "start", "done", "wait",
               "cancel", "note", "link", "gate", "gates", "graph",
               "blocked", "update"):
        a: dict = {}
        name = f"task-{cmd}"
        if cmd == "ready":
            a = {"limit": args.limit, "area": args.area}
        elif cmd == "list":
            a = {"status": args.status, "area": args.area, "tag": args.tag,
                 "query": args.query, "since": args.since,
                 "until": args.until, "page": args.page,
                 "limit": args.limit,
                 "project": "all" if args.all_projects else None}
        elif cmd == "show":
            a = {"id": args.id}
        elif cmd == "add":
            name = "task-create"
            a = {"title": args.title, "description": args.description,
                 "priority": args.priority.upper(), "area": args.area,
                 "tags": args.tags, "depends": args.depends,
                 "plan": args.plan, "due": args.due,
                 "acceptance": args.accept, "start": args.start}
        elif cmd in ("start", "done", "wait", "cancel"):
            a = {"id": args.id, "note": args.note}
        elif cmd == "note":
            a = {"id": args.id, "text": args.text}
        elif cmd == "update":
            a = {"id": args.id, "note": args.note,
                 "title": args.title, "area": args.area, "plan": args.plan,
                 "due": args.due, "defer": args.defer,
                 "description": args.description, "acceptance": args.accept,
                 "priority": args.priority.upper() if args.priority else None,
                 "add_depends": args.add_depends,
                 "remove_depends": args.remove_depends,
                 "add_tags": args.add_tags, "remove_tags": args.remove_tags,
                 "check": args.check, "uncheck": args.uncheck}
            # "" is a change (it clears); None and [] were not passed.
            if all(v is None or v == [] for k, v in a.items()
                   if k not in ("id", "note")):
                print("Not done: nothing to change — pass a field "
                      "(see claude-hooks-tasks update -h)")
                return 1
        elif cmd == "link":
            a = {"id": args.id, "kind": args.kind, "value": args.value}
        elif cmd == "gate":
            name = "task-gate"
            a = {"id": args.id, "action": args.action, "gate": args.gate,
                 "note": args.note}
        elif cmd == "gates":
            name = "task-gate"
            a = {"action": "check"}
        elif cmd in ("graph", "blocked"):
            name = "task-graph"
            a = {"view": "blocked"} if cmd == "blocked" else {
                "id": args.id, "all": args.all,
                "format": "mermaid" if args.mermaid else "text",
                "direction": ("up" if args.up and not args.down else
                              "down" if args.down and not args.up
                              else "both")}
        out = _tools(args).call(name, a)
        print(out)
        return 1 if out.startswith(("Not done", "no task")) else 0

    if cmd == "init":
        svc = _service(args)
        cfg = svc.init(prefix=args.prefix, project=args.project)
        print(f"{svc.dir.dir}: project {cfg['project']}, prefix "
              f"{cfg['prefix']}")
        return 0

    if cmd == "where":
        svc = _service(args)
        co = svc.checkout
        print(f"checkout: {svc.origin} ({co.kind}"
              + (f" worktree of {co.main_root})" if co.mirror else ")"))
        if svc.dir.root != svc.origin:
            print(f"reads:    {svc.dir.dir}")
        if svc.dir.exists:
            print(f"project:  {svc.project}, prefix {svc.dir.prefix}")
        if svc.index is None:
            print("index:    none on this host")
        elif svc.dir.exists:
            row = svc.index.project_row(svc.project)
            print("indexed:  " + (f"from {row['root']} on {row['host']}"
                                  if row else "not registered"))
            print("writes:   " + ("yes" if svc._writes_index() else "no"))
        return 0

    if cmd == "adopt":
        svc = _service(args)
        try:
            print(svc.adopt())
        except ValueError as e:
            print(e)
            return 1
        return 0

    if cmd == "reindex":
        svc = _service(args, embed=True)
        if svc.index is None:
            print("no SQL store configured; nothing to index")
            return 1
        if not svc._writes_index():
            co = svc.checkout
            print(f"{svc.origin} does not index {svc.project}"
                  + (f": a {co.kind} worktree of {co.main_root}"
                     if co.mirror else " (see `claude-hooks-tasks where`)"))
            return 1
        print(svc.reconcile())
        total = 0
        while True:
            n = svc.embed_pending(limit=50)
            total += n
            if n == 0:
                break
            print(f"  embedded {total}…", flush=True)
        print(f"embedded {total}")
        return 0

    if cmd == "board":
        from claude_hooks.tasks.board import write_board
        svc = _service(args)
        path = write_board(svc)
        print(path)
        return 0

    if cmd == "import":
        from claude_hooks.tasks.importer import run_import
        svc = _service(args)
        return run_import(svc, args.transcript, dry_run=args.dry_run,
                          session=args.session)
    return 2


if __name__ == "__main__":
    sys.exit(main())
