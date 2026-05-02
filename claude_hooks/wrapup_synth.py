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
    r"\b([a-z0-9]{8,15})-?\d{0,5}?\.(?:proxy\.)?"
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
    plans = collect_plan_references(transcript)
    bg = collect_background_tasks(transcript)
    endpoints = collect_endpoints(transcript, bash)

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
    has_any = bool(ssh_hosts or endpoints["urls"] or endpoints["ips"]
                   or endpoints["pod_ids"])
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
