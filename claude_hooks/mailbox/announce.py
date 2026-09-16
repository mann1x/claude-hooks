"""What the hooks inject, and — more importantly — what they do not.

**No hook ever injects a message body.** Not for high priority, not for
a short one. The announcement carries exactly four fields per message —
subject, sender, timestamp, priority — because that is enough to decide
*whether to interrupt what I am doing*, which is the only decision the
announcement exists to support. A body in the prompt is an interruption
whether or not it turned out to be urgent, and today's handover was
12 KB, which is the token drain avoided everywhere else in this
codebase.

Receipts are the one thing shown inline, and that is consistent rather
than an exception: an ack is short by construction and has *already
been delivered in full* at that point, so fetching it would cost more
than it saves.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Sequence

_PRIORITY_LABEL = {0: "", 1: "[high]", 2: "[high]", 3: "[urgent]"}


def _parse(ts) -> Optional[datetime]:
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    if isinstance(ts, str) and ts:
        try:
            dt = datetime.fromisoformat(ts)
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None


def ago(ts, *, now: Optional[datetime] = None) -> str:
    """Relative time, because an absolute one makes the reader do
    arithmetic to answer the only question they have — is this fresh?"""
    dt = _parse(ts)
    if dt is None:
        return "unknown time"
    now = now or datetime.now(timezone.utc)
    secs = max(0, int((now - dt).total_seconds()))
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{secs // 60} min ago"
    if secs < 86400:
        h = secs // 3600
        return f"{h} hour{'s' if h != 1 else ''} ago"
    d = secs // 86400
    return f"{d} day{'s' if d != 1 else ''} ago"


def _priority(p) -> str:
    try:
        n = int(p or 0)
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        return ""
    return _PRIORITY_LABEL.get(min(n, 3), "[high]")


def _sender(msg: dict) -> str:
    alias = msg.get("from_alias") or "unknown"
    host = msg.get("from_host") or ""
    return f"{alias}@{host}" if host else alias


def render(messages: Sequence[dict], receipts: Sequence[dict] = (), *,
           alias: str, host: str,
           now: Optional[datetime] = None) -> str:
    """The `## Messages` block, or "" when there is nothing to say.

    Returning "" rather than an empty heading matters: a section that
    appears every turn saying "no messages" is noise that trains the
    reader to skip the heading, which costs the real announcements
    their visibility.
    """
    if not messages and not receipts:
        return ""

    lines = ["## Messages", ""]

    if messages:
        n = len(messages)
        lines.append(f"**{n} unread** for `{alias}@{host}`:")
        lines.append("")
        for m in messages:
            tag = _priority(m.get("priority"))
            prefix = f"**{tag}** " if tag else ""
            lines.append(
                f"- {prefix}`{m.get('subject') or '(no subject)'}` — from "
                f"`{_sender(m)}`, {ago(m.get('created_at'), now=now)}")
        lines.append("")
        lines.append(
            "Read them with `mcp__pgvector__mailbox-read` when you reach a "
            "natural pause. Reading marks them read; reply with "
            "`mailbox-send` if the sender needs an answer, or attach a "
            "short note with `mailbox-ack` if that is enough.")

    if receipts:
        if messages:
            lines.append("")
        word = "receipt" if len(receipts) == 1 else "receipts"
        lines.append(f"**{len(receipts)} {word}** for messages you sent:")
        lines.append("")
        for r in receipts:
            reader = r.get("read_by") or "the recipient"
            note = (r.get("ack_body") or "").strip().replace("\n", " ")
            lines.append(
                f"- `{r.get('subject') or '(no subject)'}` — read by "
                f"`{reader}`, {ago(r.get('read_at'), now=now)}: "
                f"*\"{note}\"*")

    return "\n".join(lines)


def collision_note(others: Sequence, alias: str) -> str:
    """One line at SessionStart when this alias exists elsewhere.

    The only place a *sender* can be warned before being surprised by
    the ambiguity refusal later.
    """
    if not others:
        return ""
    hosts = sorted({o.host for o in others})
    where = ", ".join(hosts)
    return (f"Mailbox: alias `{alias}` is also registered on {where}. "
            f"Address it as "
            + " / ".join(f"{alias}@{h}" for h in hosts)
            + f", or {alias}* for all of them.")
