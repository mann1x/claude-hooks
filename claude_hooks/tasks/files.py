"""The task folder: ``<project>/.claude-hooks/tasks/``.

The files are the record. The database rows are an index of them, so
everything a person needs — reading a task, fixing a typo, closing one
by hand — works with an editor and nothing else, and a host with no SQL
store still has working tasks.

Layout::

    .claude-hooks/tasks/
      config.toml        prefix = "bm", project = "backup_models"
      bm-42.md           open tasks
      archive/bm-7.md    done and cancelled ones

The prefix is derived once and written down, never re-derived: a rename
of the directory must not renumber every reference in old notes.
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Optional

from claude_hooks._atomic import write_text_atomic
from claude_hooks.tasks.model import (
    CLOSED_STATUSES, Task, TaskFormatError, split_id,
)

TASKS_DIR = Path(".claude-hooks") / "tasks"
ARCHIVE = "archive"
CONFIG = "config.toml"
_FILE = re.compile(r"^([a-z][a-z0-9]*)-(\d+)\.md$")
_KV = re.compile(r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+?)\s*$')

#: Room for this many concurrent creators racing for the next number
#: before giving up. Each loser simply takes the next one.
_ALLOCATE_ATTEMPTS = 50



#: Per-host state about a task folder — the reconcile cache, which nudges
#: a session has had, the map from Claude Code's task ids to ours — lives
#: under ``~/.claude``, not in the folder. None of it is a task, and the
#: hook daemon cannot write project folders: its unit runs with
#: ``ProtectSystem=strict`` and only ``~/.claude`` writable, so the cache
#: write failed on every reconcile there. ``CLAUDE_HOOKS_TASKS_STATE_DIR``
#: overrides the root.
STATE_ROOT_ENV = "CLAUDE_HOOKS_TASKS_STATE_DIR"


def state_dir_for(task_dir: Path) -> Path:
    """``~/.claude/claude-hooks-tasks/<project>-<hash>`` for a task folder.

    Keyed on the folder's resolved path, so two checkouts with the same
    name never share state; the project name is only there to make the
    directory recognisable.
    """
    override = os.environ.get(STATE_ROOT_ENV)
    root = (Path(override) if override
            else Path.home() / ".claude" / "claude-hooks-tasks")
    try:
        resolved = str(Path(task_dir).resolve())
    except OSError:
        resolved = str(task_dir)
    project = Path(task_dir).parent.parent.name or "root"
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in project)
    digest = hashlib.sha1(resolved.encode("utf-8")).hexdigest()[:12]
    return root / f"{safe}-{digest}"


def read_state(task_dir: Path, name: str):
    """JSON state ``name`` for a task folder, or None.

    Falls back to the file's old place inside the task folder, so state
    written before it moved is not lost.
    """
    import json
    for path in (state_dir_for(task_dir) / name, Path(task_dir) / name):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return None


def write_state(task_dir: Path, name: str, value) -> None:
    """Write JSON state ``name`` for a task folder under ``~/.claude``,
    then drop the copy at its old place in the folder where that is
    allowed (it is not, from the daemon; the fallback read covers it)."""
    import json
    d = state_dir_for(task_dir)
    d.mkdir(parents=True, exist_ok=True)
    write_text_atomic(d / name, json.dumps(value))
    try:
        (Path(task_dir) / name).unlink()
    except OSError:
        pass

class TaskNotFound(LookupError):
    pass


def project_root(cwd: Optional[str] = None) -> Path:
    """The project a session works in.

    ``CLAUDE_PROJECT_DIR`` when there is no explicit ``cwd`` — a hook's
    cwd is wherever the session last ``cd``'d, never the project. Then
    walk up from there: an existing task folder wins (a subdirectory
    belongs to the project that already tracks tasks), then a ``.git``
    boundary, then the directory itself.
    """
    start = Path(cwd or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    try:
        start = start.resolve()
    except OSError:
        start = start.absolute()
    for d in (start, *start.parents):
        if (d / TASKS_DIR / CONFIG).is_file():
            return d
    for d in (start, *start.parents):
        if (d / ".git").exists():
            return d
    return start


def derive_prefix(name: str, taken: Iterable[str] = ()) -> str:
    """Initials of the project name, made unique against ``taken``.

    ``backup_models`` → ``bm``, ``claude-hooks`` → ``ch``,
    ``lm-evaluation-harness`` → ``leh``; a one-word name gives its first
    two letters (``opencoti`` → ``op``). On a clash the next letters of
    the last word are added, then a digit.
    """
    taken = {t.lower() for t in taken}
    words = [w for w in re.split(r"[^A-Za-z0-9]+|(?<=[a-z])(?=[A-Z])", name)
             if w]
    words = [w.lower() for w in words] or ["task"]
    if not words[0][0].isalpha():
        words.insert(0, "t")
    base = ("".join(w[0] for w in words) if len(words) > 1
            else words[0][:2])
    base = re.sub(r"[^a-z0-9]", "", base) or "t"
    if base not in taken:
        return base
    tail = words[-1][1:] if len(words) > 1 else words[0][2:]
    cand = base
    for c in tail:
        cand += c
        if cand not in taken:
            return cand
    n = 2
    while f"{base}{n}" in taken:
        n += 1
    return f"{base}{n}"


@dataclass
class TaskFile:
    path: Path
    mtime_ns: int
    size: int

    @property
    def id(self) -> str:
        return self.path.stem.lower()

    @property
    def archived(self) -> bool:
        return self.path.parent.name == ARCHIVE


def file_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class TaskDir:
    """One project's task folder."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.dir = self.root / TASKS_DIR
        self.archive = self.dir / ARCHIVE
        self._config: Optional[dict] = None

    # ─── config ──────────────────────────────────────────────────────

    @property
    def exists(self) -> bool:
        return (self.dir / CONFIG).is_file()

    def config(self) -> dict:
        if self._config is None:
            self._config = read_config(self.dir / CONFIG)
        return self._config

    @property
    def prefix(self) -> str:
        p = self.config().get("prefix", "")
        if not p:
            raise RuntimeError(f"no task prefix configured in {self.dir}")
        return p

    @property
    def project(self) -> str:
        return self.config().get("project") or self.root.name

    def init(self, *, prefix: Optional[str] = None,
             project: Optional[str] = None,
             taken: Iterable[str] = ()) -> dict:
        """Create the folder and its config once; idempotent after that."""
        if self.exists:
            return self.config()
        name = project or self.root.name
        cfg = {"project": name,
               "prefix": (prefix or derive_prefix(name, taken)).lower()}
        if not re.match(r"^[a-z][a-z0-9]*$", cfg["prefix"]):
            raise ValueError(f"prefix must be lowercase letters/digits, "
                             f"starting with a letter: {cfg['prefix']!r}")
        self.archive.mkdir(parents=True, exist_ok=True)
        write_text_atomic(self.dir / CONFIG, render_config(cfg))
        self._config = cfg
        return cfg

    # ─── files ───────────────────────────────────────────────────────

    def path_for(self, task_id: str, *, archived: bool) -> Path:
        return (self.archive if archived else self.dir) / f"{task_id}.md"

    def find(self, task_id: str) -> Optional[Path]:
        task_id = task_id.strip().lower()
        for archived in (False, True):
            p = self.path_for(task_id, archived=archived)
            if p.is_file():
                return p
        return None

    def read(self, task_id: str) -> tuple[Task, Path, str]:
        """The task, where it lives, and the text it was parsed from."""
        p = self.find(task_id)
        if p is None:
            raise TaskNotFound(f"no task {task_id} in {self.dir}")
        text = p.read_text(encoding="utf-8")
        return Task.from_markdown(text, source=str(p)), p, text

    def write(self, task: Task) -> tuple[Path, str]:
        """Write ``task`` where its status puts it; drop any other copy."""
        archived = task.status in CLOSED_STATUSES
        dest = self.path_for(task.id, archived=archived)
        other = self.path_for(task.id, archived=not archived)
        text = task.to_markdown()
        write_text_atomic(dest, text)
        try:
            other.unlink()
        except FileNotFoundError:
            pass
        return dest, text

    def scan(self) -> list[TaskFile]:
        out: list[TaskFile] = []
        for d in (self.dir, self.archive):
            try:
                entries = list(os.scandir(d))
            except FileNotFoundError:
                continue
            for e in entries:
                if not _FILE.match(e.name.lower()) or not e.is_file():
                    continue
                st = e.stat()
                out.append(TaskFile(Path(e.path), st.st_mtime_ns, st.st_size))
        return out

    def iter_tasks(self) -> Iterator[tuple[Task, TaskFile, str]]:
        """Every readable task; unreadable files are skipped, not fatal."""
        for tf in self.scan():
            try:
                text = tf.path.read_text(encoding="utf-8")
                yield Task.from_markdown(text, source=str(tf.path)), tf, text
            except (OSError, UnicodeDecodeError, TaskFormatError):
                continue

    def max_number(self) -> int:
        n = int(self.config().get("next", "1") or 1) - 1
        pre = self.prefix
        for tf in self.scan():
            try:
                p, num = split_id(tf.id)
            except ValueError:
                continue
            if p == pre:
                n = max(n, num)
        return n

    def create(self, task_factory) -> tuple[Task, Path, str]:
        """Claim the next free id with an exclusive create, then fill it.

        ``O_EXCL`` is the allocator: two sessions creating at the same
        moment both compute the same next number, one wins the create,
        the other moves on. No lock file to leave behind.
        """
        self.archive.mkdir(parents=True, exist_ok=True)
        n = self.max_number() + 1
        for _ in range(_ALLOCATE_ATTEMPTS):
            task_id = f"{self.prefix}-{n}"
            dest = self.path_for(task_id, archived=False)
            if self.path_for(task_id, archived=True).exists():
                n += 1
                continue
            try:
                fd = os.open(dest, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                n += 1
                continue
            os.close(fd)
            try:
                task = task_factory(task_id)
                path, text = self.write(task)
            except BaseException:
                try:
                    dest.unlink()
                except OSError:
                    pass
                raise
            return task, path, text
        raise RuntimeError(f"could not allocate a task id in {self.dir}")


# ─── config.toml (flat string keys only) ─────────────────────────────


def read_config(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    out: dict = {}
    for ln in text.splitlines():
        if ln.lstrip().startswith("#"):
            continue
        m = _KV.match(ln)
        if not m:
            continue
        v = m.group(2)
        if v.startswith('"') and '"' in v[1:]:
            v = v[1:v.index('"', 1)]
        else:
            v = v.split("#", 1)[0].strip()
        out[m.group(1)] = v
    return out


def render_config(cfg: dict) -> str:
    head = ("# Task tracking for this project (claude-hooks).\n"
            "# The prefix is fixed once tasks exist: ids like "
            f"{cfg.get('prefix', 'xx')}-42 are\n# referenced from notes, "
            "mail and commits.\n")
    body = "".join(f'{k} = "{v}"\n' for k, v in cfg.items())
    return head + body
