"""Gates: a task that waits on something outside the task list (ch-28 phase 3).

A gate is a string in the task's ``links`` map under ``gates`` (older
code keeps an unknown ``links`` key and writes it back unchanged, and the
index already stores ``links``, so no schema change). An open gate blocks
like an open dependency. When a gate resolves it is removed from the
list and the task's log says why; the log is its history.

| gate | resolves when |
|---|---|
| ``mail:<id>`` | mail #id is answered: acked with a note, or replied to |
| ``human:<what>`` | ``task-gate resolve`` (a person's go-ahead) |
| ``timer:<ISO time>`` | the time has passed |
| ``commit:<sha>@<branch>`` | the branch contains the commit |
| ``gh-pr:<owner/repo>#<n>`` | the pull request is merged |
| ``gh-run:<owner/repo>#<id>`` | the workflow run succeeded |

``mail``, ``timer`` and ``commit`` are cheap and are checked at session
start; ``gh-*`` call GitHub and are checked only on request
(``task-gate`` with ``action: check``, ``claude-hooks-tasks gates
--check``). A gate that fails for good (a PR closed unmerged, a failed
run, cancelled mail) is reported, never resolved.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from claude_hooks.tasks.model import parse_stamp, utcnow

GATE_KINDS = ("mail", "human", "timer", "commit", "gh-pr", "gh-run")
#: Checked at session start; the rest only on request.
CHEAP_KINDS = ("mail", "timer", "commit")
_SUBPROCESS_TIMEOUT_S = 10

_SHA = re.compile(r"^[0-9a-fA-F]{7,40}$")
_REPO_NUM = re.compile(r"^([\w.-]+/[\w.-]+)#(\d+)$")


@dataclass
class Verdict:
    """``resolved`` True / False, or None when it could not be checked.
    ``failed`` marks a gate that will never resolve on its own."""
    resolved: Optional[bool]
    why: str
    failed: bool = False


def parse(gate: str) -> tuple[str, str]:
    """``(kind, argument)``; raises ValueError on a malformed gate."""
    gate = str(gate or "").strip()
    kind, sep, arg = gate.partition(":")
    kind, arg = kind.strip().lower(), arg.strip()
    if not sep or kind not in GATE_KINDS or not arg:
        raise ValueError(
            f"a gate is <kind>:<what>, kind one of {', '.join(GATE_KINDS)} "
            f"(e.g. mail:2331, human:deploy, timer:2026-11-20T10:00Z, "
            f"commit:abc1234@main, gh-pr:owner/repo#42); not {gate!r}")
    if kind == "mail":
        if not arg.lstrip("#").isdigit():
            raise ValueError(f"a mail gate takes a mail id, not {arg!r}")
        arg = arg.lstrip("#")
    elif kind == "timer":
        dt = parse_stamp(arg)
        if dt is None:
            raise ValueError(f"a timer gate takes a time like "
                             f"2026-11-20T10:00Z, not {arg!r}")
        arg = dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    elif kind == "commit":
        sha, at, branch = arg.partition("@")
        if not _SHA.match(sha) or not at or not branch.strip():
            raise ValueError(f"a commit gate is <sha>@<branch>, not {arg!r}")
        arg = f"{sha.lower()}@{branch.strip()}"
    elif kind in ("gh-pr", "gh-run"):
        if not _REPO_NUM.match(arg):
            raise ValueError(f"a {kind} gate is <owner/repo>#<number>, "
                             f"not {arg!r}")
    return kind, arg


def normalise(gate: str) -> str:
    kind, arg = parse(gate)
    return f"{kind}:{arg}"


class Checker:
    """Evaluates gates. ``mail_store`` is the host's mailbox store (None:
    mail gates are unchecked); ``root`` the project checkout, for commit
    gates; ``now`` for timers."""

    def __init__(self, *, mail_store=None, root: Optional[Path] = None,
                 now: Optional[datetime] = None, run=None):
        self.mail_store = mail_store
        self.root = Path(root) if root else None
        self.now = now
        self._run = run or _run

    def check(self, gate: str, *, cheap_only: bool = False) -> Verdict:
        try:
            kind, arg = parse(gate)
        except ValueError as e:
            return Verdict(None, str(e), failed=True)
        if cheap_only and kind not in CHEAP_KINDS:
            return Verdict(None, "checked on request")
        return getattr(self, "_" + kind.replace("-", "_"))(arg)

    def _human(self, arg: str) -> Verdict:
        return Verdict(False, f"waits for a person: {arg}")

    def _timer(self, arg: str) -> Verdict:
        due = parse_stamp(arg)
        now = self.now or utcnow()
        if due is not None and now >= due:
            return Verdict(True, f"time reached ({arg})")
        return Verdict(False, f"until {arg}")

    def _mail(self, arg: str) -> Verdict:
        if self.mail_store is None:
            return Verdict(None, "no mailbox on this host")
        try:
            st = self.mail_store.message_state(int(arg))
        except Exception as e:
            return Verdict(None, f"mailbox unavailable ({e})")
        if st is None:
            return Verdict(None, f"no mail #{arg}", failed=True)
        if st.get("cancelled_at"):
            return Verdict(False, f"mail #{arg} was cancelled", failed=True)
        if st.get("ack_body"):
            return Verdict(True, f"mail #{arg} answered by "
                                 f"{st.get('to_alias') or 'its recipient'}: "
                                 f"{_short(st['ack_body'])}")
        if st.get("reply_id"):
            return Verdict(True, f"mail #{arg} answered by "
                                 f"{st.get('to_alias') or 'its recipient'} "
                                 f"in #{st['reply_id']}")
        state = "read, not answered" if st.get("read_at") else "unread"
        return Verdict(False, f"mail #{arg} {state}")

    def _commit(self, arg: str) -> Verdict:
        sha, _, branch = arg.partition("@")
        if self.root is None or not shutil.which("git"):
            return Verdict(None, "no git here")
        rc, _ = self._run(["git", "-C", str(self.root), "merge-base",
                           "--is-ancestor", sha, branch])
        if rc == 0:
            return Verdict(True, f"{sha[:12]} is on {branch}")
        if rc == 1:
            return Verdict(False, f"{sha[:12]} is not on {branch} yet")
        return Verdict(None, f"git cannot tell ({sha[:12]} or {branch} "
                             "unknown here; fetch?)")

    def _gh(self, args: list[str]) -> Optional[dict]:
        if not shutil.which("gh"):
            return None
        rc, out = self._run(["gh", *args])
        if rc != 0:
            return None
        try:
            return json.loads(out)
        except ValueError:
            return None

    def _gh_pr(self, arg: str) -> Verdict:
        repo, num = _REPO_NUM.match(arg).groups()
        d = self._gh(["pr", "view", num, "-R", repo, "--json", "state"])
        if d is None:
            return Verdict(None, f"gh cannot read {arg}")
        state = str(d.get("state") or "").upper()
        if state == "MERGED":
            return Verdict(True, f"{arg} merged")
        if state == "CLOSED":
            return Verdict(False, f"{arg} closed without merging",
                           failed=True)
        return Verdict(False, f"{arg} {state.lower() or 'open'}")

    def _gh_run(self, arg: str) -> Verdict:
        repo, num = _REPO_NUM.match(arg).groups()
        d = self._gh(["run", "view", num, "-R", repo, "--json",
                      "status,conclusion"])
        if d is None:
            return Verdict(None, f"gh cannot read {arg}")
        status = str(d.get("status") or "")
        concl = str(d.get("conclusion") or "")
        if status != "completed":
            return Verdict(False, f"{arg} {status or 'pending'}")
        if concl == "success":
            return Verdict(True, f"{arg} succeeded")
        return Verdict(False, f"{arg} {concl or 'failed'}", failed=True)


def _short(text: str, n: int = 80) -> str:
    t = " ".join(str(text).split())
    return t if len(t) <= n else t[: n - 1] + "…"


def _run(cmd: list[str]) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=_SUBPROCESS_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):
        return 128, ""
    return p.returncode, p.stdout
