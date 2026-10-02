"""Task tracking in the hooks: the part that makes the list impossible to
forget.

- **SessionStart** (every source: startup, resume, ``/clear``, compaction)
  injects the project's open work. After a compaction this *is* the fix
  for "restore the tasks": the list comes back without the model having
  to remember that one exists.
- **UserPromptSubmit** injects any task the prompt names (``bm-42``) and
  the tasks most similar to the prompt, open ones first.
- **Stop** nudges once when the turn changed files while a task is active
  but no task was touched — the same once-per-change rule as the mail
  nudge, never on a continuation.
- **PostToolUse** on Claude Code's own ``TaskCreate`` / ``TaskUpdate``
  mirrors them here, so a session where the built-in tools exist still
  ends up in the record.

Everything here soft-fails: a task block that cannot be built is left
out, never allowed to delay or break the turn.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Optional

from claude_hooks.tasks.model import OPEN_STATUSES
from claude_hooks.tasks.render import counts_line, row_line

log = logging.getLogger("claude_hooks.tasks.hook")

DEFAULTS = {
    "enabled": True,
    "session_start": True,
    "prompt_mentions": True,
    "prompt_recall": True,
    "recall_k": 3,
    # Measured on backup_models with qwen3-embedding (2026-10-02): the
    # task a prompt is about scores 0.74-0.81, its close relatives
    # 0.62-0.65, same-domain noise 0.58-0.61, off-topic prompts <= 0.44.
    "recall_min_score": 0.62,
    "stop_nudge": True,
    "mirror_builtin": True,
    "session_ready": 5,
}

_MENTION = re.compile(r"(?<![\w-])([a-z][a-z0-9]{0,7})-(\d{1,6})(?![\w-])",
                      re.IGNORECASE)
_EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
_TASK_TOOL = re.compile(r"(^|__)task-")


def settings(config: dict) -> dict:
    section = ((config or {}).get("hooks") or {}).get("tasks") or {}
    return {**DEFAULTS, **section}


def _service(event: dict, config: dict, providers, *, embed: bool = False):
    from claude_hooks.tasks import service_for, sql_provider
    root = os.environ.get("CLAUDE_PROJECT_DIR") or event.get("cwd") or None
    svc = service_for(sql_provider(providers), cwd=root,
                      session_id=str(event.get("session_id") or ""),
                      embed=embed)
    svc.embed_on_write = False
    return svc


# ─── SessionStart ────────────────────────────────────────────────────


def session_block(*, event: dict, config: dict, providers) -> str:
    s = settings(config)
    if not (s["enabled"] and s["session_start"]):
        return ""
    try:
        svc = _service(event, config, providers)
        if not svc.initialised:
            return (f"**Tasks:** no task list in {svc.dir.root.name} yet. "
                    "For work that outlives this turn, `task-create` starts "
                    "one (files under `.claude-hooks/tasks/`, kept across "
                    "compaction and sessions).")
        board = svc.board()
        return render_session(board, svc.project, int(s["session_ready"]))
    except Exception:
        log.warning("tasks: session block failed", exc_info=True)
        return ""


def render_session(board: dict, project: str, ready_n: int = 5) -> str:
    lines = [f"**Tasks ({project}):** {counts_line(board)}"]
    for r in board.get("active") or []:
        lines.append(f"- {row_line(r)}")
    ready = board.get("ready") or []
    for r in ready[:ready_n]:
        lines.append(f"- {row_line(r)}")
    if len(ready) > ready_n:
        lines.append(f"- … {len(ready) - ready_n} more ready")
    lines.append("Full board: `TASKS.md` · `task-ready`, `task-show <id>`, "
                 "`task-note` / `task-done` as work moves.")
    return "\n".join(lines)


# ─── UserPromptSubmit ────────────────────────────────────────────────


def mentioned_ids(prompt: str, prefixes: set) -> list[str]:
    seen: list[str] = []
    for m in _MENTION.finditer(prompt or ""):
        tid = f"{m.group(1).lower()}-{int(m.group(2))}"
        if m.group(1).lower() in prefixes and tid not in seen:
            seen.append(tid)
    return seen[:5]


def _summary(task, path) -> str:
    head = row_line({"id": task.id, "status": task.status,
                     "priority": task.priority, "area": task.area,
                     "title": task.title, "updated": task.updated})
    out = [f"- {head}"]
    desc = " ".join(task.description.split())
    if desc:
        out.append(f"  {desc[:400]}{'…' if len(desc) > 400 else ''}")
    for ln in task.log_lines[:3]:
        out.append(f"  {ln[2:]}")
    done, total = task.acceptance_counts()
    if total:
        out.append(f"  acceptance {done}/{total}")
    return "\n".join(out)


class PromptRecall:
    """Started before memory recall and joined after it, so the extra
    embedding overlaps the one recall already pays for."""

    def __init__(self, *, event: dict, config: dict, providers):
        self.event, self.config, self.providers = event, config, providers
        self.result = ""
        self._thread: Optional[threading.Thread] = None

    def start(self) -> "PromptRecall":
        s = settings(self.config)
        if not s["enabled"]:
            return self
        self._thread = threading.Thread(target=self._run, name="task-recall",
                                        daemon=True)
        self._thread.start()
        return self

    def join(self, timeout: float = 20.0) -> str:
        if self._thread is not None:
            self._thread.join(timeout)
        return self.result

    def _run(self) -> None:
        try:
            self.result = prompt_block(event=self.event, config=self.config,
                                       providers=self.providers)
        except Exception:
            log.warning("tasks: prompt block failed", exc_info=True)


def prompt_block(*, event: dict, config: dict, providers) -> str:
    s = settings(config)
    prompt = (event.get("prompt") or "").strip()
    if not prompt:
        return ""
    svc = _service(event, config, providers, embed=bool(s["prompt_recall"]))
    if not svc.initialised:
        return ""
    parts: list[str] = []
    shown: set[str] = set()
    if s["prompt_mentions"]:
        prefixes = {svc.dir.prefix}
        if svc.index is not None:
            try:
                prefixes |= {p["prefix"] for p in svc.index.projects()}
            except Exception:
                pass
        for tid in mentioned_ids(prompt, prefixes):
            if not tid.startswith(svc.dir.prefix + "-"):
                continue        # another project's: no local file to read
            try:
                task, path = svc.show(tid)
            except Exception:
                continue
            parts.append(_summary(task, path))
            shown.add(tid)
    if (s["prompt_recall"] and svc.index is not None and svc.embedder
            and len(prompt) >= 30):
        try:
            from claude_hooks.prompt_origin import classify
            if classify(prompt, event.get("transcript_path")).synthetic:
                raise StopIteration
            vec = svc.embedder(prompt[:2000])
            hits = svc.index.similar(vec, svc.embed_model,
                                     project=svc.project,
                                     k=int(s["recall_k"]) * 3) if vec else []
            ranked = []
            for score, row in hits:
                if row["id"] in shown:
                    continue
                if row["status"] in OPEN_STATUSES:
                    score += 0.03
                if score >= float(s["recall_min_score"]):
                    ranked.append((score, row))
            ranked.sort(key=lambda x: x[0], reverse=True)
            for score, row in ranked[:int(s["recall_k"])]:
                parts.append(f"- {row_line(row)} ({score:.2f})")
        except StopIteration:
            pass
        except Exception:
            log.info("tasks: semantic task recall skipped", exc_info=True)
    if not parts:
        return ""
    return "**Tasks related to this prompt:**\n" + "\n".join(parts)


# ─── Stop nudge ──────────────────────────────────────────────────────


def _turn_tools(transcript_path: str) -> list[dict]:
    """tool_use blocks of the last turn (since the last real user text)."""
    if not transcript_path:
        return []
    try:
        lines = Path(transcript_path).read_text(
            encoding="utf-8", errors="replace").splitlines()[-400:]
    except OSError:
        return []
    uses: list[dict] = []
    for raw in reversed(lines):
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        msg = row.get("message") or {}
        content = msg.get("content")
        if row.get("type") == "user" and (
                isinstance(content, str) or any(
                    isinstance(b, dict) and b.get("type") == "text"
                    for b in content or [])):
            break
        for b in content or [] if isinstance(content, list) else []:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                uses.append(b)
    return uses


def stop_nudge(*, event: dict, config: dict, providers) -> Optional[str]:
    """A reason to block the stop once, or None.

    Fires when the turn edited files, a task is active, and no task tool
    was called. Tracked by the set of active ids plus the edited files,
    so the same situation never nudges twice.
    """
    s = settings(config)
    if not (s["enabled"] and s["stop_nudge"]):
        return None
    if event.get("stop_hook_active"):
        return None
    uses = _turn_tools(event.get("transcript_path") or "")
    if not uses:
        return None
    if any(_TASK_TOOL.search(str(u.get("name") or "")) for u in uses):
        return None
    edited = sorted({str((u.get("input") or {}).get("file_path") or "")
                     for u in uses if u.get("name") in _EDIT_TOOLS} - {""})
    if not edited:
        return None
    try:
        svc = _service(event, config, providers)
        if not svc.initialised:
            return None
        active = [r for r in svc.snapshot() if r["status"] == "active"]
    except Exception:
        log.info("tasks: stop nudge skipped", exc_info=True)
        return None
    if not active:
        return None
    key = "|".join(sorted(r["id"] for r in active)) + "#" + "|".join(edited)
    if not _first_time(svc, str(event.get("session_id") or ""), key):
        return None
    ids = ", ".join(f"{r['id']} ({r['title'][:60]})" for r in active[:3])
    return (f"Task tracking: this turn changed {len(edited)} file(s) while "
            f"{ids} {'is' if len(active) == 1 else 'are'} active, and no "
            "task was updated. If the work moved a task, record it now "
            "(`task-note`, `task-done`, or `task-create` for new work); "
            "if not, carry on. This reminder fires once per change.")


def _first_time(svc, session_id: str, key: str) -> bool:
    path = svc.dir.dir / f".nudge-{(session_id or 'none')[:8]}.json"
    try:
        seen = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        seen = []
    if key in seen:
        return False
    seen = (seen + [key])[-50:]
    try:
        from claude_hooks._atomic import write_text_atomic
        write_text_atomic(path, json.dumps(seen))
    except OSError:
        return False        # cannot remember it: do not risk nagging
    return True


# ─── Claude Code's own task tools ────────────────────────────────────


_CC_CREATED = re.compile(r"Task #(\d+) created")
_CC_STATUS = {"pending": "pending", "in_progress": "active",
              "completed": "done", "deleted": "cancelled"}


def mirror_builtin(*, event: dict, config: dict, providers) -> None:
    """PostToolUse on TaskCreate / TaskUpdate: keep a copy here."""
    s = settings(config)
    if not (s["enabled"] and s["mirror_builtin"]):
        return
    name = event.get("tool_name")
    if name not in ("TaskCreate", "TaskUpdate"):
        return
    inp = event.get("tool_input") or {}
    try:
        svc = _service(event, config, providers)
        sid = str(event.get("session_id") or "")
        mpath = svc.dir.dir / f".cc-{sid[:8] or 'none'}.json"
        try:
            mapping = json.loads(mpath.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            mapping = {}
        if name == "TaskCreate":
            resp = event.get("tool_response")
            text = resp if isinstance(resp, str) else json.dumps(resp)
            m = _CC_CREATED.search(text or "")
            title = str(inp.get("subject") or "").strip()
            if not title:
                return
            t = svc.create(title, description=str(inp.get("description")
                                                   or ""),
                           note="mirrored from Claude Code task"
                                + (f" #{m.group(1)}" if m else ""))
            if m:
                mapping[m.group(1)] = t.id
        else:
            ours = mapping.get(str(inp.get("taskId") or "").lstrip("#"))
            if not ours:
                return
            status = _CC_STATUS.get(str(inp.get("status") or ""))
            if status:
                svc.set_status(ours, status)
            fields = {}
            if inp.get("subject"):
                fields["title"] = str(inp["subject"])
            if inp.get("description"):
                fields["description"] = str(inp["description"])
            if fields:
                svc.update(ours, **fields)
        from claude_hooks._atomic import write_text_atomic
        write_text_atomic(mpath, json.dumps(mapping))
    except Exception:
        log.info("tasks: built-in task mirror failed", exc_info=True)



__all__ = ["session_block", "prompt_block", "PromptRecall", "stop_nudge",
           "mirror_builtin", "settings", "render_session", "mentioned_ids"]
