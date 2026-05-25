"""Wrap-up synthesis used by the PreCompact hook.

When Claude Code is about to auto-compact the conversation, the
PreCompact hook fires. By then there may be no model-turn left
to invoke the ``/wrapup`` skill, so this module deterministically
synthesises the mechanically-extractable parts of the same eight
sections the skill produces, writes them to a file the next
session can read, and returns the markdown so the hook can emit
it as ``additionalContext`` (which lands inside the compaction
window).

What we CAN extract deterministically from the transcript:

- branch + last commit (git, in ``cwd``)
- files modified this session (Edit / Write / MultiEdit tool inputs)
- bash commands run (Bash tool inputs)
- ssh sessions touched (heuristic: bash commands starting with ``ssh``)
- plans referenced (regex over text content for ``docs/PLAN-*.md``)
- background tasks / monitors / scheduled wake-ups (tool name lookup)

What we CANNOT extract — these need the model:

- Open items (what work is incomplete)
- Next items (what to do next)
- Subjective "session snapshot" prose

We render the latter as ``_(needs model — invoke /wrapup)_`` placeholders
so the next session sees the gap clearly instead of a confidently-empty
section.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# A small, dependency-free transcript reader so this module can be
# imported without dragging the Stop hook's helpers along.
def read_transcript(path: str) -> list[dict]:
    """Load a JSONL transcript file. Returns ``[]`` on any error."""
    try:
        p = Path(os.path.expanduser(path))
        if not p.exists():
            return []
        out: list[dict] = []
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return out
    except OSError:
        return []


# Machine-extractable sentinels around the reconnect command block in
# section 7. wrapup_recovery lifts the fenced block between these markers
# verbatim into the post-compact additionalContext. Kept as module
# constants so both producer (this file) and consumer (wrapup_recovery)
# agree on the exact bytes. Emitted ONLY when a connection exists.
RECONNECT_SENTINEL_BEGIN = "<!-- RECONNECT:BEGIN -->"
RECONNECT_SENTINEL_END = "<!-- RECONNECT:END -->"
# Backwards-friendly short aliases used in the render code below.
_RECONNECT_BEGIN = RECONNECT_SENTINEL_BEGIN
_RECONNECT_END = RECONNECT_SENTINEL_END

_PLAN_RX = re.compile(r"docs/PLAN-[A-Za-z0-9_-]+\.md")
# Capture http(s) URLs and ws(s) URLs. Stop on whitespace, quotes,
# closing brackets, and common markdown trailers.
_URL_RX = re.compile(r"\b(?:https?|wss?)://[^\s'\"<>)\]}`]+")
# Bare IPv4 (with optional :port). Excludes octets > 255 via the
# alternation. Anchored on word boundaries so we don't pick stuff up
# inside paths like /16/2026.
_IPV4_RX = re.compile(
    r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d?\d)\.){3}"
    r"(?:25[0-5]|2[0-4]\d|[01]?\d?\d)"
    r"(?::\d{1,5})?\b"
)
# Best-effort pod / instance / container ID heuristic. Targets the
# patterns that show up in remote-compute platforms: RunPod
# ("vh4z3xq8mn-8888.proxy.runpod.net"), Modal, Vast.ai, etc. Looks
# for a 8-15 alphanumeric token followed by dash-port or .proxy. /
# .runpod. / .modal. / .vast. domain. Generic enough to catch most
# pod-style endpoints without false-matching arbitrary text.
_POD_ID_RX = re.compile(
    # Lower bound is {3,…} so vast.ai's short proxy prefixes ("ssh5.vast.ai")
    # match, not just RunPod's long hashes. The platform-domain anchor
    # keeps the loosened prefix from false-matching arbitrary tokens.
    r"\b([a-z0-9]{3,20})-?\d{0,5}?\.(?:proxy\.)?"
    r"(?:runpod|modal|vast|lambdalabs|paperspace)\.[a-z.]+",
    re.IGNORECASE,
)
# IPv6 — coarse pattern, dotted form. Skips loopback ::1 and unspec.
_IPV6_RX = re.compile(
    r"\b(?:[0-9a-f]{1,4}:){2,7}[0-9a-f]{1,4}\b",
    re.IGNORECASE,
)


def _msg_content(msg: dict) -> list:
    """Return the content list from a transcript message, or empty list."""
    inner = msg.get("message") or {}
    content = inner.get("content")
    if content is None:
        content = msg.get("content")
    if isinstance(content, list):
        return content
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return []


def _iter_tool_uses(transcript: list[dict]):
    """Yield ``(tool_name, tool_input)`` for every tool_use block in
    the transcript. Robust against schema drift."""
    for msg in transcript:
        if not isinstance(msg, dict):
            continue
        for block in _msg_content(msg):
            if not isinstance(block, dict):
                continue
            if block.get("type") != "tool_use":
                continue
            yield (block.get("name") or "", block.get("input") or {})


def _iter_text_blocks(transcript: list[dict]):
    """Yield text strings from any content block."""
    for msg in transcript:
        if not isinstance(msg, dict):
            continue
        for block in _msg_content(msg):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                yield block.get("text") or ""


# --------------------------------------------------------------------------- #
# AskUserQuestion preservation (#217)
# --------------------------------------------------------------------------- #
# The 2026-05-18 #214 regression matrix lost two question shapes (Q2 and
# Q3) across a context-compaction boundary because the AskUserQuestion
# Q&A pairs weren't recorded anywhere in the pre-compact summary.
# Mechanically-extractable artefacts (files, commits, bash) survived;
# the user's explicit decisions did not.
#
# This block adds a third class of mechanical extraction: walk the
# transcript pairing AskUserQuestion ``tool_use`` blocks with their
# ``tool_result`` responses (matched by ``tool_use_id``), parse the
# canonical "User has answered your questions: ..." answer string, and
# surface every (question, chosen-answer) pair in a dedicated wrap-up
# section. Cheap — one extra pass over the transcript, no model calls,
# no network. Safe — if the parse fails for any pair, the pair is
# silently dropped (better empty than wrong).


def _parse_aq_answers(
    question_texts: list[str], result_str: str,
) -> dict[str, str]:
    """Extract ``{question: answer}`` from one AskUserQuestion
    tool_result string.

    The canonical answer-string shape is::

        User has answered your questions: "Q1?"="A1", "Q2?"="A2", ...

    Sometimes the closing prose differs — the harness appends a "You
    can now continue ..." sentence, or a "selected preview:" block when
    the user picked a preview option. Anchoring on the **known question
    text** (passed in from the matching tool_use) avoids parsing the
    trailing prose at all: we look for the literal ``"Q"="`` marker
    and then take whatever's between that and the next plausible
    terminator.

    Returns a dict mapping every question we found an answer for. Pairs
    we couldn't parse are silently omitted (better empty than wrong —
    the wrap-up reader sees the dropped question explicitly only if we
    chose to mark it).
    """
    out: dict[str, str] = {}
    for q in question_texts:
        if not q:
            continue
        marker = '"' + q + '"="'
        i = result_str.find(marker)
        if i < 0:
            continue
        start = i + len(marker)
        # Find the closest plausible end-of-answer. Order matters —
        # the longest match wins ties.
        terminators = (
            '", "',                # next pair marker
            '" selected preview',  # preview-option suffix
            '". You can now',      # canonical trailing prose
            '" You can now',       # space-style variant
            '\\". You can now',    # double-escaped variant
        )
        end = -1
        for term in terminators:
            j = result_str.find(term, start)
            if j >= 0 and (end < 0 or j < end):
                end = j
        if end < 0:
            # Last resort: take to the next bare close-quote.
            end = result_str.find('"', start)
            if end < 0:
                end = len(result_str)
        out[q] = result_str[start:end].strip()
    return out


def collect_ask_user_questions(transcript: list[dict]) -> list[tuple[str, str]]:
    """Return every AskUserQuestion ``(question, answer)`` pair from the
    transcript in chronological order.

    Walks the transcript twice: first pass indexes every
    ``AskUserQuestion`` tool_use by id so the second pass can match
    tool_results back to the questions they answered. Defensive against
    schema drift (missing ids, list-shaped tool_result content) — any
    malformed pair is silently dropped rather than emitted as garbage.
    """
    use_by_id: dict[str, list[str]] = {}
    seen_ids: set[str] = set()
    order: list[str] = []
    for msg in transcript:
        if not isinstance(msg, dict):
            continue
        for block in _msg_content(msg):
            if not isinstance(block, dict):
                continue
            if block.get("type") != "tool_use":
                continue
            if block.get("name") != "AskUserQuestion":
                continue
            tuid = block.get("id") or ""
            inp = block.get("input") or {}
            qs = inp.get("questions") or []
            qtexts = [
                str(q.get("question") or "").strip()
                for q in qs if isinstance(q, dict)
            ]
            qtexts = [q for q in qtexts if q]
            if tuid and qtexts and tuid not in seen_ids:
                seen_ids.add(tuid)
                use_by_id[tuid] = qtexts
                order.append(tuid)

    out: list[tuple[str, str]] = []
    answered: set[str] = set()
    for msg in transcript:
        if not isinstance(msg, dict):
            continue
        for block in _msg_content(msg):
            if not isinstance(block, dict):
                continue
            if block.get("type") != "tool_result":
                continue
            tuid = block.get("tool_use_id") or ""
            if tuid not in use_by_id or tuid in answered:
                continue
            content = block.get("content") or ""
            if isinstance(content, list):
                # Modern API also serves tool_result content as a list
                # of {type: text, text: ...} blocks — flatten.
                parts: list[str] = []
                for c in content:
                    if isinstance(c, dict):
                        parts.append(c.get("text") or "")
                    else:
                        parts.append(str(c))
                content = "".join(parts)
            if not isinstance(content, str) or not content:
                continue
            answers = _parse_aq_answers(use_by_id[tuid], content)
            for q in use_by_id[tuid]:
                a = answers.get(q, "").strip()
                if a:
                    out.append((q, a))
            answered.add(tuid)
    return out


# --------------------------------------------------------------------------- #
# Mechanical extraction
# --------------------------------------------------------------------------- #
def collect_modified_files(transcript: list[dict]) -> list[str]:
    """Files passed to Edit / Write / MultiEdit / NotebookEdit, dedup
    while preserving first-seen order."""
    seen: dict[str, None] = {}
    modifying = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
    for name, inp in _iter_tool_uses(transcript):
        if name not in modifying:
            continue
        path = inp.get("file_path") or inp.get("notebook_path") or ""
        if path and path not in seen:
            seen[path] = None
    return list(seen.keys())


def collect_bash_commands(transcript: list[dict]) -> list[str]:
    """Bash tool ``command`` strings, in order, dedup-preserving."""
    seen: dict[str, None] = {}
    for name, inp in _iter_tool_uses(transcript):
        if name != "Bash":
            continue
        cmd = (inp.get("command") or "").strip()
        if cmd and cmd not in seen:
            seen[cmd] = None
    return list(seen.keys())


def collect_ssh_targets(bash_commands: list[str]) -> list[str]:
    """Best-effort: pull host arguments out of bash ``ssh`` commands."""
    rx = re.compile(r"\bssh\b\s+(?:-[\w]+\s+\S+\s+)*([^\s|;&]+)")
    out: dict[str, None] = {}
    for cmd in bash_commands:
        for m in rx.finditer(cmd):
            host = m.group(1)
            if host and "@" in host or (host and re.match(r"^[\w.-]+$", host)):
                out[host] = None
    return list(out.keys())


# An ``ssh`` word that starts an actual invocation (line start or after a
# shell separator), but NOT the ``ssh-keygen`` / ``ssh-copy-id`` / … family.
_SSH_INVOKE_RX = re.compile(r"(?:^|[\s;&|(])ssh\b", re.IGNORECASE)
_SSH_TOOL_RX = re.compile(
    r"\bssh-(?:keygen|copy-id|add|keyscan|agent)\b", re.IGNORECASE
)
# Markers that an ssh command is a real *connection* (not ``ssh --help``):
# a ``user@host``, an explicit ``-p <port>``, an IPv4 literal, or a known
# pod-platform domain. Any one is enough.
_SSH_CONN_MARKER_RX = re.compile(
    r"@[\w.-]+"
    r"|(?:^|\s)-p\s*\d{1,5}\b"
    r"|\b\d{1,3}(?:\.\d{1,3}){3}\b"
    r"|[\w-]+\.(?:runpod|modal|vast|lambdalabs|paperspace)\.[a-z.]+",
    re.IGNORECASE,
)


def collect_ssh_invocations(bash_commands: list[str]) -> list[str]:
    """Return the FULL ``ssh`` command lines that look like real
    connections, verbatim and dedup-preserving order.

    Unlike :func:`collect_ssh_targets` (which keeps only the bare host
    token), this preserves the **entire** invocation — ``-p <port>``,
    ``-i <key>``, ``-L/-R`` tunnels, user, host — because that's exactly
    what a vast.ai / RunPod pod needs to reconnect (the port is mandatory
    and was previously dropped). Filters out the ``ssh-keygen`` family
    and non-connection invocations like ``ssh --help``.
    """
    out: dict[str, None] = {}
    for cmd in bash_commands:
        c = (cmd or "").strip()
        if not c or not _SSH_INVOKE_RX.search(c):
            continue
        if _SSH_TOOL_RX.search(c):
            continue
        if not _SSH_CONN_MARKER_RX.search(c):
            continue
        if c not in out:
            out[c] = None
    return list(out.keys())


def build_reconnect_lines(bash_commands: list[str]) -> list[str]:
    """Return the reconnect command(s) worth preserving across a compact.

    ``[]`` when no ssh connection was made. Otherwise the **initial**
    invocation and, when it differs, the **last** one — vast.ai pods
    often start on a proxy connection and switch to a faster direct
    connection once ready, and the last command is the best-effort
    "last known-good" target. (We can't see exit codes in the
    transcript, so "last seen" is the closest signal.)
    """
    inv = collect_ssh_invocations(bash_commands)
    if not inv:
        return []
    if len(inv) == 1:
        return [inv[0]]
    return [inv[0], inv[-1]]


def collect_endpoints(transcript: list[dict],
                      bash_commands: list[str]) -> dict[str, list[str]]:
    """Extract everything that looks like a remote endpoint or
    connection identifier from the transcript: URLs, IPv4/IPv6
    addresses, and pod-style hostnames.

    Sweeps BOTH text blocks (where the assistant or user mentioned
    URLs/pod IDs in prose) AND bash commands (where curl / wget /
    ssh-tunnel targets live). The previous synth only looked at
    ``ssh`` bash commands, which missed everything that wasn't an
    interactive ssh session — most notably RunPod / Modal proxy URLs
    and any IP mentioned in connection strings.

    Returns a dict with keys ``urls``, ``ips``, ``pod_ids`` —
    dedup-preserving order, capped per category to keep the synth
    readable.
    """
    urls: dict[str, None] = {}
    ips: dict[str, None] = {}
    pods: dict[str, None] = {}

    def _scan(text: str):
        if not text:
            return
        for m in _URL_RX.finditer(text):
            url = m.group(0).rstrip(".,;:!?")
            urls[url] = None
        for m in _IPV4_RX.finditer(text):
            ips[m.group(0)] = None
        for m in _IPV6_RX.finditer(text):
            v = m.group(0)
            # Skip pure-digit timestamps and other false matches: an
            # IPv6 address must contain at least 2 colons.
            if v.count(":") >= 2:
                ips[v] = None
        for m in _POD_ID_RX.finditer(text):
            # Capture both the bare id and the full hostname so the
            # next session sees the connection target verbatim.
            pods[m.group(0)] = None

    for txt in _iter_text_blocks(transcript):
        _scan(txt)
    for cmd in bash_commands:
        _scan(cmd)
    # Pod-style hostnames are also URLs — make sure they're surfaced
    # even when the prose mentioned them as bare hostnames.
    return {
        "urls": list(urls.keys())[:30],
        "ips": list(ips.keys())[:30],
        "pod_ids": list(pods.keys())[:15],
    }


def collect_plan_references(transcript: list[dict]) -> list[str]:
    """``docs/PLAN-*.md`` references seen in any text block."""
    out: dict[str, None] = {}
    for txt in _iter_text_blocks(transcript):
        for m in _PLAN_RX.finditer(txt):
            out[m.group(0)] = None
    return list(out.keys())


def collect_background_tasks(transcript: list[dict]) -> list[str]:
    """Tool calls hinting at long-lived watchers: Bash with
    ``run_in_background``, Monitor, ScheduleWakeup, CronCreate."""
    out: list[str] = []
    for name, inp in _iter_tool_uses(transcript):
        if name == "Monitor":
            desc = (inp.get("description") or "").strip()
            out.append(f"Monitor: {desc[:80]}" if desc else "Monitor: (no description)")
        elif name == "ScheduleWakeup":
            reason = (inp.get("reason") or "").strip()
            out.append(f"ScheduleWakeup: {reason[:80]}" if reason else "ScheduleWakeup")
        elif name == "CronCreate":
            cron = inp.get("cron") or ""
            prompt = (inp.get("prompt") or "").strip().replace("\n", " ")
            out.append(f"CronCreate {cron}: {prompt[:80]}" if cron else "CronCreate")
        elif name == "Bash" and inp.get("run_in_background"):
            out.append(
                "Bash (background): "
                + (inp.get("description") or inp.get("command") or "")[:80]
            )
    # Dedup preserving order.
    seen: dict[str, None] = {}
    for x in out:
        seen[x] = None
    return list(seen.keys())


# --------------------------------------------------------------------------- #
# Git + filesystem context
# --------------------------------------------------------------------------- #
def git_context(cwd: str) -> dict:
    """Return ``{branch, head, recent_commits}`` for ``cwd`` if it's a
    git repo. All keys default to empty/[] on any failure."""
    out = {"branch": "", "head": "", "recent_commits": []}
    cwd = cwd or "."
    if not os.path.isdir(cwd):
        return out
    try:
        out["branch"] = subprocess.check_output(
            ["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"],
            text=True, stderr=subprocess.DEVNULL, timeout=5,
        ).strip()
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return out
    try:
        out["head"] = subprocess.check_output(
            ["git", "-C", cwd, "rev-parse", "--short", "HEAD"],
            text=True, stderr=subprocess.DEVNULL, timeout=5,
        ).strip()
    except subprocess.SubprocessError:
        pass
    try:
        log = subprocess.check_output(
            ["git", "-C", cwd, "log", "--oneline", "-15"],
            text=True, stderr=subprocess.DEVNULL, timeout=5,
        )
        out["recent_commits"] = [l for l in log.splitlines() if l.strip()]
    except subprocess.SubprocessError:
        pass
    return out


# --------------------------------------------------------------------------- #
# Output location
# --------------------------------------------------------------------------- #
def resolve_output_path(cwd: str, session_id: str, *, now: Optional[datetime] = None) -> Path:
    """Pick an on-disk location for the synthesised wrap-up.

    Preference order, mirroring the wrapup skill:
    1. ``<cwd>/.wolf/wrapup-pre-compact-<ts>.md`` if ``.wolf/`` exists
    2. ``<cwd>/docs/wrapup/wrapup-pre-compact-<ts>.md`` if cwd is writable
    3. ``~/.claude/wrapup-pre-compact/<session>-<ts>.md`` (always-writable fallback)
    """
    now = now or datetime.now(timezone.utc)
    ts = now.strftime("%Y-%m-%dT%H-%M-%S")
    fname = f"wrapup-pre-compact-{ts}.md"

    if cwd:
        wolf_dir = Path(cwd) / ".wolf"
        if wolf_dir.is_dir():
            return wolf_dir / fname
        docs_wrapup = Path(cwd) / "docs" / "wrapup"
        try:
            docs_wrapup.mkdir(parents=True, exist_ok=True)
            return docs_wrapup / fname
        except OSError:
            pass

    fallback = Path.home() / ".claude" / "wrapup-pre-compact"
    try:
        fallback.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    sid = (session_id or "session")[:32]
    return fallback / f"{sid}-{ts}.md"


# --------------------------------------------------------------------------- #
# Main entry point
# --------------------------------------------------------------------------- #
def synthesize_markdown(
    transcript: list[dict],
    *,
    cwd: str = "",
    session_id: str = "",
    now: Optional[datetime] = None,
) -> str:
    """Return the markdown wrap-up summary. Sections that need model
    judgment are explicitly marked rather than fabricated."""
    now = now or datetime.now(timezone.utc)
    ts_human = now.strftime("%Y-%m-%d %H:%M UTC")

    git = git_context(cwd)
    modified = collect_modified_files(transcript)
    bash = collect_bash_commands(transcript)
    ssh_hosts = collect_ssh_targets(bash)
    reconnect = build_reconnect_lines(bash)
    plans = collect_plan_references(transcript)
    bg = collect_background_tasks(transcript)
    endpoints = collect_endpoints(transcript, bash)
    # #217 (2026-05-18): preserve user-decided AskUserQuestion answers
    # across the compaction boundary. The May-18 #214 matrix lost Q2/Q3
    # shapes this way; mechanically-extractable now.
    aq_pairs = collect_ask_user_questions(transcript)

    out: list[str] = []
    out.append(f"# Pre-compact wrap-up ({ts_human})")
    out.append("")
    out.append(
        "_Auto-synthesised by the claude-hooks PreCompact hook before "
        "context auto-compaction. Mechanically-extractable sections are "
        "filled in; sections needing model judgment are marked. To "
        "complete the missing parts, invoke `/wrapup` in the next "
        "session._"
    )
    out.append("")

    # 1 — Session snapshot
    out.append("## 1. Session snapshot")
    out.append("")
    if cwd:
        out.append(f"- Working directory: `{cwd}`")
    if session_id:
        out.append(f"- Session id: `{session_id}`")
    if git["branch"] or git["head"]:
        out.append(
            f"- Repo: branch `{git['branch'] or '(unknown)'}`, "
            f"HEAD `{git['head'] or '(unknown)'}`"
        )
    out.append("- Narrative: _needs model — invoke `/wrapup` to fill in_")
    out.append("")

    # User decisions captured this session (#217). Unnumbered so the
    # canonical /wrapup numbering (1-8) stays intact, but positioned
    # at the top where post-compact attention lands first. Each pair
    # is the literal question shown to the user + the option label
    # they chose. Cap at 30 to keep the wrap-up readable; sessions
    # this active have other context-loss problems too.
    if aq_pairs:
        out.append("## User decisions captured this session")
        out.append("")
        out.append(
            "_Verbatim AskUserQuestion exchanges from this session, in "
            "order. Preserved here because the May-2026 #214 regression "
            "matrix lost Q2/Q3 across a compaction boundary — these "
            "decisions are load-bearing for resumed work and the "
            "summarizer can't reconstruct them from prose alone._"
        )
        out.append("")
        for q, a in aq_pairs[:30]:
            # Compact one-line form. Truncate long answers to keep the
            # block scannable; the full text lives in the transcript.
            q_short = q if len(q) <= 160 else q[:157] + "..."
            a_short = a if len(a) <= 200 else a[:197] + "..."
            out.append(f"- **Q:** {q_short}")
            out.append(f"  **A:** {a_short}")
        if len(aq_pairs) > 30:
            out.append(f"- _… and {len(aq_pairs) - 30} earlier pair(s) "
                       "omitted — see transcript for the full series._")
        out.append("")

    # 2 — Achievements (mechanical)
    out.append("## 2. Session achievements")
    out.append("")
    if git["recent_commits"]:
        out.append("Recent commits (most recent first, capped at 15):")
        out.append("")
        for line in git["recent_commits"]:
            out.append(f"- `{line}`")
        out.append("")
    if modified:
        out.append(f"Files modified this session ({len(modified)}):")
        out.append("")
        for p in modified[:30]:
            out.append(f"- `{p}`")
        if len(modified) > 30:
            out.append(f"- … and {len(modified) - 30} more")
        out.append("")
    if not git["recent_commits"] and not modified:
        out.append("_(no commits or file edits detected this session)_")
        out.append("")

    # 3 — Open items
    out.append("## 3. Open items")
    out.append("")
    out.append("_needs model — invoke `/wrapup` to fill in._")
    out.append("")

    # 4 — Next items
    out.append("## 4. Next items")
    out.append("")
    out.append("_needs model — invoke `/wrapup` to fill in._")
    out.append("")

    # 5 — Plans in use
    out.append("## 5. Plans referenced")
    out.append("")
    if plans:
        for p in plans:
            out.append(f"- [{p}]({p})")
        out.append("")
    else:
        out.append("_(no `docs/PLAN-*.md` references seen this session)_")
        out.append("")

    # 6 — Active monitorings
    out.append("## 6. Active monitorings to re-establish")
    out.append("")
    if bg:
        for x in bg:
            out.append(f"- {x}")
        out.append("")
    else:
        out.append("_(no Monitor / ScheduleWakeup / CronCreate / background Bash detected)_")
        out.append("")

    # 7 — Connection state (pods, hosts, URLs, IPs)
    out.append("## 7. Connection state (re-attach targets)")
    out.append("")
    has_any = bool(reconnect or ssh_hosts or endpoints["urls"]
                   or endpoints["ips"] or endpoints["pod_ids"])
    # Reconnect commands first — the load-bearing bit for pods. Wrapped in
    # HTML-comment sentinels so wrapup_recovery can lift the fenced block
    # verbatim into the post-compact additionalContext. The sentinels are
    # emitted ONLY when there's a connection, so a session with no remote
    # work costs zero extra tokens downstream.
    if reconnect:
        out.append("**Reconnect commands** (initial + last known-good — "
                   "best-effort, verify before trusting):")
        out.append("")
        out.append(_RECONNECT_BEGIN)
        out.append("```")
        if len(reconnect) == 1:
            out.append(reconnect[0])
        else:
            out.append("# initial")
            out.append(reconnect[0])
            out.append("# last known-good")
            out.append(reconnect[1])
        out.append("```")
        out.append(_RECONNECT_END)
        out.append("")
    if endpoints["pod_ids"]:
        out.append("**Pod / instance hostnames:**")
        out.append("")
        for h in endpoints["pod_ids"]:
            out.append(f"- `{h}`")
        out.append("")
    if ssh_hosts:
        out.append("**SSH targets:**")
        out.append("")
        for h in ssh_hosts:
            out.append(f"- `{h}`")
        out.append("")
    if endpoints["urls"]:
        out.append("**URLs mentioned:**")
        out.append("")
        for u in endpoints["urls"]:
            out.append(f"- {u}")
        out.append("")
    if endpoints["ips"]:
        out.append("**IP addresses:**")
        out.append("")
        for ip in endpoints["ips"]:
            out.append(f"- `{ip}`")
        out.append("")
    if not has_any:
        out.append("_(no remote endpoints, ssh targets, URLs, or IPs detected)_")
        out.append("")

    # 8 — Restore checklist (boilerplate)
    out.append("## 8. Restore checklist")
    out.append("")
    out.append("```")
    out.append("git status -sb")
    out.append("git log --oneline -10")
    if cwd and Path(cwd, "tests").is_dir():
        out.append("/root/anaconda3/envs/claude-hooks/bin/python -m pytest tests/ -q --tb=line | tail -5")
    if plans:
        out.append("# Plans referenced this session:")
        for p in plans:
            out.append(f"# - {p}")
    out.append("```")
    out.append("")

    return "\n".join(out)


def write_to_disk(markdown: str, output_path: Path) -> Optional[Path]:
    """Persist the synthesis. Returns the path on success, None on failure."""
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(markdown, encoding="utf-8")
        return output_path
    except OSError:
        return None
