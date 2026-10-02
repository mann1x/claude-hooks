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
    claude-hooks-tasks init [--prefix bm]
    claude-hooks-tasks reindex          # reconcile files → index, embed
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
    return TaskTools(lambda: _service(args), background_embed=False)


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

    p = sub.add_parser("note", help="append a log line")
    p.add_argument("id")
    p.add_argument("text")

    p = sub.add_parser("link", help="attach a commit/file/mail/plan/…")
    p.add_argument("id")
    p.add_argument("kind")
    p.add_argument("value")

    p = sub.add_parser("init", help="create the task folder")
    p.add_argument("--prefix")
    p.add_argument("--project")

    sub.add_parser("reindex", help="sync files into the index and embed")

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
               "cancel", "note", "link"):
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
        elif cmd == "link":
            a = {"id": args.id, "kind": args.kind, "value": args.value}
        out = _tools(args).call(name, a)
        print(out)
        return 1 if out.startswith(("Not done", "no task")) else 0

    if cmd == "init":
        svc = _service(args)
        cfg = svc.init(prefix=args.prefix, project=args.project)
        print(f"{svc.dir.dir}: project {cfg['project']}, prefix "
              f"{cfg['prefix']}")
        return 0

    if cmd == "reindex":
        svc = _service(args, embed=True)
        if svc.index is None:
            print("no SQL store configured; nothing to index")
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
