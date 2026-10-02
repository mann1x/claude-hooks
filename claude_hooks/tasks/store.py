"""The task index over a borrowed store connection.

Same borrowing rule as the mailbox (:mod:`claude_hooks.mailbox.store`):
the provider's connection and its lock, never a second connection, and
every failure path rolls back — an aborted transaction left behind makes
the next recall fail, which reads as an empty store.
"""
from __future__ import annotations

import base64
import json
import logging
import math
import struct
from typing import Any, Iterable, Optional, Sequence

from claude_hooks.mailbox.filters import Page, like_pattern, split_terms
from claude_hooks.mailbox.store import _cursor
from claude_hooks.tasks import schema
from claude_hooks.tasks.model import Task, stamp

log = logging.getLogger("claude_hooks.tasks.store")

_JSON_FIELDS = ("tags", "depends", "links", "sessions")

#: Searched by ``query``: every keyword must appear in one of these.
_TEXT_COLUMNS = ("id", "title", "area", "description", "log", "tags")

#: How much of the log goes into the embedding. The latest lines say what
#: the task is *now*; the first ones are mostly "created".
_EMBED_LOG_LINES = 3
_EMBED_MAX_CHARS = 2000


def embed_text(task: Task) -> str:
    parts = [task.title]
    if task.area:
        parts.append(f"area: {task.area}")
    if task.description:
        parts.append(task.description)
    logs = task.log_lines[:_EMBED_LOG_LINES]
    if logs:
        parts.append("\n".join(logs))
    return "\n".join(parts)[:_EMBED_MAX_CHARS]


def pack_vec(vec: Sequence[float]) -> str:
    return base64.b64encode(struct.pack(f"<{len(vec)}f", *vec)).decode("ascii")


def unpack_vec(text: str) -> list[float]:
    raw = base64.b64decode(text)
    return list(struct.unpack(f"<{len(raw) // 4}f", raw))


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class TaskIndex:
    def __init__(self, connect, lock, *, dialect: str = "postgres"):
        self._connect = connect
        self._lock = lock
        self.dialect = dialect
        self._ready = False

    # ─── plumbing ────────────────────────────────────────────────────

    def _q(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.dialect == "postgres" else sql

    @staticmethod
    def _rollback(conn) -> None:
        try:
            conn.rollback()
        except Exception:      # pragma: no cover — already broken
            log.debug("tasks rollback failed", exc_info=True)

    def ensure_schema(self) -> None:
        if self._ready:
            return
        with self._lock:
            conn = self._connect()
            try:
                with _cursor(conn) as cur:
                    for stmt in schema.statements(self.dialect):
                        cur.execute(stmt)
                conn.commit()
                self._ready = True
            except Exception:
                self._rollback(conn)
                raise

    def _run(self, fn):
        """``fn(cur)`` in one transaction, committed or rolled back."""
        self.ensure_schema()
        with self._lock:
            conn = self._connect()
            try:
                with _cursor(conn) as cur:
                    out = fn(cur)
                conn.commit()
                return out
            except Exception:
                self._rollback(conn)
                raise

    def _decode(self, row: dict) -> dict:
        for k in _JSON_FIELDS:
            if k in row and isinstance(row[k], str):
                try:
                    row[k] = json.loads(row[k])
                except ValueError:
                    row[k] = [] if k != "links" else {}
        if "archived" in row:
            row["archived"] = bool(row["archived"])
        return row

    # ─── projects ────────────────────────────────────────────────────

    def register_project(self, project: str, prefix: str, root: str,
                         host: str) -> None:
        sql = self._q(
            "INSERT INTO task_projects (project, prefix, root, host, updated) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT (project) DO UPDATE SET "
            "prefix = EXCLUDED.prefix, root = EXCLUDED.root, "
            "host = EXCLUDED.host, updated = EXCLUDED.updated")
        self._run(lambda cur: cur.execute(
            sql, (project, prefix, root, host, stamp())))

    def projects(self) -> list[dict]:
        cols = schema.PROJECT_COLUMNS

        def fn(cur):
            cur.execute(f"SELECT {', '.join(cols)} FROM task_projects "
                        "ORDER BY project")
            return [dict(zip(cols, r)) for r in cur.fetchall()]
        return self._run(fn)

    def prefixes_taken(self, *, except_project: str = "") -> set[str]:
        return {p["prefix"] for p in self.projects()
                if p["project"] != except_project}

    # ─── rows ────────────────────────────────────────────────────────

    def upsert(self, project: str, task: Task, *, root: str, host: str,
               file_hash: str, archived: bool) -> None:
        values = {
            "project": project, "id": task.id, "prefix": task.prefix,
            "num": task.number, "title": task.title, "status": task.status,
            "priority": task.priority, "area": task.area,
            "tags": json.dumps(task.tags), "depends": json.dumps(task.depends),
            "plan": task.plan, "due": task.due,
            "links": json.dumps(task.links), "created": task.created,
            "updated": task.updated, "sessions": json.dumps(task.sessions),
            "description": task.description, "acceptance": task.acceptance,
            "log": "\n".join(task.log_lines), "body": task.to_markdown(),
            "root": root, "host": host, "file_hash": file_hash,
            "archived": bool(archived) if self.dialect == "postgres"
            else int(bool(archived)),
            "indexed_at": stamp(),
        }
        cols = schema.COLUMNS
        sets = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols
                         if c not in ("project", "id"))
        sql = self._q(
            f"INSERT INTO tasks ({', '.join(cols)}) VALUES "
            f"({', '.join('?' for _ in cols)}) "
            f"ON CONFLICT (project, id) DO UPDATE SET {sets}")
        self._run(lambda cur: cur.execute(sql, tuple(values[c] for c in cols)))

    def upsert_many(self, project: str, items: Iterable[tuple]) -> int:
        n = 0
        for task, kw in items:
            self.upsert(project, task, **kw)
            n += 1
        return n

    def delete(self, project: str, task_id: str) -> None:
        sql = self._q("DELETE FROM tasks WHERE project = ? AND id = ?")
        self._run(lambda cur: cur.execute(sql, (project, task_id)))

    def hashes(self, project: str) -> dict[str, str]:
        sql = self._q("SELECT id, file_hash FROM tasks WHERE project = ?")

        def fn(cur):
            cur.execute(sql, (project,))
            return {r[0]: r[1] for r in cur.fetchall()}
        return self._run(fn)

    def get(self, project: str, task_id: str) -> Optional[dict]:
        cols = schema.COLUMNS
        sql = self._q(f"SELECT {', '.join(cols)} FROM tasks "
                      "WHERE project = ? AND id = ?")

        def fn(cur):
            cur.execute(sql, (project, task_id))
            r = cur.fetchone()
            return self._decode(dict(zip(cols, r))) if r else None
        return self._run(fn)

    def find(self, *, project: Optional[str] = None,
             statuses: Optional[Sequence[str]] = None,
             area: Optional[str] = None, tag: Optional[str] = None,
             query: Optional[str] = None, since: Optional[str] = None,
             until: Optional[str] = None, order: str = "updated",
             page: int = 1, limit: int = 20) -> Page:
        where: list[str] = []
        args: list[Any] = []
        if project:
            where.append("project = ?")
            args.append(project)
        if statuses:
            where.append(f"status IN ({', '.join('?' for _ in statuses)})")
            args.extend(statuses)
        if area:
            # An area matches itself and everything under it: R9 ⊇ R9.gepo.
            where.append("(LOWER(area) = ? OR LOWER(area) LIKE ? ESCAPE '\\')")
            args.extend([area.lower(), like_pattern(area)[1:-1] + ".%"])
        if tag:
            where.append("LOWER(tags) LIKE ? ESCAPE '\\'")
            args.append(like_pattern(f'"{tag}"'))
        for term in split_terms(query):
            ors = " OR ".join(f"LOWER({c}) LIKE ? ESCAPE '\\'"
                              for c in _TEXT_COLUMNS)
            where.append(f"({ors})")
            args.extend([like_pattern(term)] * len(_TEXT_COLUMNS))
        if since:
            where.append("updated >= ?")
            args.append(since)
        if until:
            where.append("updated < ?")
            args.append(until)
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        orders = {
            "updated": "updated DESC, num DESC",
            "oldest": "updated ASC, num ASC",
            "id": "project, num ASC",
        }
        cols = schema.COLUMNS
        sel = self._q(f"SELECT {', '.join(cols)} FROM tasks{clause} "
                      f"ORDER BY {orders.get(order, orders['updated'])} "
                      "LIMIT ? OFFSET ?")
        cnt = self._q(f"SELECT COUNT(*) FROM tasks{clause}")

        def fn(cur):
            cur.execute(cnt, tuple(args))
            total = int(cur.fetchone()[0])
            cur.execute(sel, (*args, limit, (page - 1) * limit))
            rows = [self._decode(dict(zip(cols, r))) for r in cur.fetchall()]
            return Page(rows=rows, total=total, page=page, page_size=limit)
        return self._run(fn)

    def all_rows(self, project: str) -> list[dict]:
        """Every row of one project (bounded by the project's size)."""
        cols = schema.COLUMNS
        sql = self._q(f"SELECT {', '.join(cols)} FROM tasks WHERE project = ?")

        def fn(cur):
            cur.execute(sql, (project,))
            return [self._decode(dict(zip(cols, r))) for r in cur.fetchall()]
        return self._run(fn)

    def counts(self, project: str) -> dict[str, int]:
        sql = self._q("SELECT status, COUNT(*) FROM tasks WHERE project = ? "
                      "GROUP BY status")

        def fn(cur):
            cur.execute(sql, (project,))
            return {r[0]: int(r[1]) for r in cur.fetchall()}
        return self._run(fn)

    # ─── embeddings ──────────────────────────────────────────────────

    def needing_embedding(self, model: str, *, project: Optional[str] = None,
                          limit: int = 50) -> list[dict]:
        """Rows whose vector is missing, stale, or from another model."""
        cols = ("project", "id", "title", "area", "description", "log",
                "file_hash", "embed_hash", "embed_model")
        where = ("(embedding IS NULL OR embed_hash IS NULL "
                 "OR embed_hash <> file_hash OR embed_model IS NULL "
                 "OR embed_model <> ?)")
        args: list = [model]
        if project:
            where += " AND project = ?"
            args.append(project)
        sql = self._q(f"SELECT {', '.join(cols)} FROM tasks WHERE {where} "
                      "ORDER BY archived, updated DESC LIMIT ?")

        def fn(cur):
            cur.execute(sql, (*args, limit))
            return [dict(zip(cols, r)) for r in cur.fetchall()]
        return self._run(fn)

    def set_embedding(self, project: str, task_id: str, vec: Sequence[float],
                      *, file_hash: str, model: str) -> None:
        sql = self._q("UPDATE tasks SET embedding = ?, embed_hash = ?, "
                      "embed_model = ? WHERE project = ? AND id = ?")
        self._run(lambda cur: cur.execute(
            sql, (pack_vec(vec), file_hash, model, project, task_id)))

    def similar(self, vec: Sequence[float], model: str, *,
                project: Optional[str] = None,
                statuses: Optional[Sequence[str]] = None,
                k: int = 5) -> list[tuple[float, dict]]:
        cols = ("project", "id", "title", "status", "priority", "area",
                "updated", "embedding")
        where = ["embedding IS NOT NULL", "embed_model = ?"]
        args: list = [model]
        if project:
            where.append("project = ?")
            args.append(project)
        if statuses:
            where.append(f"status IN ({', '.join('?' for _ in statuses)})")
            args.extend(statuses)
        sql = self._q(f"SELECT {', '.join(cols)} FROM tasks "
                      f"WHERE {' AND '.join(where)}")

        def fn(cur):
            cur.execute(sql, tuple(args))
            return [dict(zip(cols, r)) for r in cur.fetchall()]
        scored = []
        for row in self._run(fn):
            try:
                v = unpack_vec(row.pop("embedding"))
            except (ValueError, struct.error):
                continue
            scored.append((cosine(vec, v), row))
        scored.sort(key=lambda s: s[0], reverse=True)
        return scored[:k]
