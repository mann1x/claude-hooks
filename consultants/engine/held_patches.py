"""Held patches in the answer (council run 6, mail #1648).

In write mode the lanes leave patches in their private views, frozen and
listed under "Sandbox changes" after the answer is written, so the
synthesizer never saw them. Run 6 showed what that costs:

- researcher-100's apply-patch.ts comment said a patch "had neither" the
  read gate nor the per-path lock; its diff added the read gate only,
  and the answer ranked "wrap in withFileLock" as if it were done;
- no one said which ranked defects a patch fixes: researcher-100's left
  three of them unfixed, with three of its own tests failing;
- coder-1 and researcher-100 both rewrote ``offsetIn`` in
  delimiter-balance.ts, one correctly, one on a false claim, and no one
  chose.

So, before the synthesizer writes, :func:`held_changes` reads every
live view's diff (writing nothing); :func:`held_block` hands it over
with :func:`overlaps` computed in code; the answer owes a ``## Held
patches`` section, checked by :func:`audit` and completed by
:func:`enforce`; and :func:`comment_items` puts every comment a patch
adds beside the code the hunk adds, for an auditor call.

Only patches that change source count: a lane that wrote tests only owes
no entry (its tests are evidence, cited as runs).
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Optional

from claude_hooks import test_reruns as _tr

log = logging.getLogger(__name__)

#: Diff text per lane, and in all, handed to the synthesizer.
LANE_DIFF_CHARS = 12_000
TOTAL_DIFF_CHARS = 48_000
COMMENT_MIN_WORDS = 8
COMMENT_MAX_ITEMS = 24
#: A patched file larger than this is audited from its diff alone.
POST_FILE_MAX_CHARS = 400_000

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_COMMENT = re.compile(r"^\s*(?://|#(?![!\w])|#\s|/\*|\*(?!\*?/?\s*$)|--\s|\"\"\"|''')")
HELD_HEADING = re.compile(r"^\s{0,3}(?:(?:#{1,6}\s*)+|\*\*)\s*held patch", re.I | re.M)


@dataclass
class Hunk:
    lane: str
    path: str
    old_start: int
    old_len: int
    new_start: int
    added: list = field(default_factory=list)
    removed: list = field(default_factory=list)
    text: str = ""
    new_file: bool = False

    @property
    def old_range(self) -> tuple[int, int]:
        return self.old_start, self.old_start + max(self.old_len, 1)


@dataclass
class LanePatch:
    lane: str
    diff: str
    hunks: list = field(default_factory=list)
    #: Source path -> the file as the lane left it (run 8: the auditor saw
    #: one hunk and called a default parameter "unreachable").
    post: dict = field(default_factory=dict)
    #: Known findings whose code a hunk changes (run 8: coder-0's MOVE
    #: change widened csf-8 and the answer counted it as fixing csf-7):
    #: ``{id, status, title, where}``.
    touches: list = field(default_factory=list)
    #: Options a hunk adds that no production file the patch changes
    #: passes (run 8: ``forgetReads`` on the controller, built without it
    #: in session-protocol.ts): ``{name, where, mentions}``.
    unwired: list = field(default_factory=list)

    def file_diff(self, path: str) -> str:
        """Every hunk this lane's patch makes to ``path``."""
        return "\n".join(h.text for h in self.hunks if h.path == path)

    @property
    def files(self) -> list[str]:
        return list(dict.fromkeys(h.path for h in self.hunks))

    @property
    def source_files(self) -> list[str]:
        return [p for p in self.files if not _tr.TEST_FILE.search(p)]

    @property
    def role_lane(self) -> tuple[str, Optional[int]]:
        role, _, idx = self.lane.rpartition("-")
        return (role, int(idx)) if idx.isdigit() else (self.lane, None)


def parse_diff(lane: str, diff: str) -> list[Hunk]:
    out: list[Hunk] = []
    path, old_path = "", ""
    cur: Optional[Hunk] = None
    for ln in (diff or "").split("\n"):
        if ln.startswith("diff "):
            cur, path, old_path = None, "", ""
            continue
        if ln.startswith("--- "):
            old_path = ln[4:].strip()
            continue
        if ln.startswith("+++ "):
            p = ln[4:].strip()
            if p == "/dev/null":
                p = old_path
            path = re.sub(r"^[ab]/", "", p.split("\t")[0])
            continue
        m = _HUNK.match(ln)
        if m and path:
            cur = Hunk(lane=lane, path=path, old_start=int(m.group(1)),
                       old_len=int(m.group(2) if m.group(2) is not None else 1),
                       new_start=int(m.group(3)), text=ln,
                       new_file=old_path == "/dev/null")
            out.append(cur)
            continue
        if cur is None:
            continue
        if ln.startswith("+"):
            cur.added.append(ln[1:])
        elif ln.startswith("-"):
            cur.removed.append(ln[1:])
        elif not ln.startswith((" ", "\\")) and ln:
            cur = None
            continue
        cur.text += "\n" + ln
    return out


def held_changes(cwd: str, sid: str) -> list[LanePatch]:
    """Every live view's changes that touch source, by lane; ``[]`` when
    the session has no write views or they cannot be read. Never raises."""
    if not cwd or not sid:
        return []
    try:
        from claude_hooks.sandbox.views import session_views
        views = session_views(sid=sid, cwd=cwd)
        if views is None:
            return []
        out = []
        for lane, rec in sorted(views.lanes().items()):
            if rec.get("state") != "active":
                continue
            diff = views.preview(lane)
            lp = LanePatch(lane=lane, diff=diff, hunks=parse_diff(lane, diff))
            if lp.source_files:
                lp.post = _post_files(views, lane, lp.source_files)
                out.append(lp)
        known = _ledger_findings(cwd)
        for lp in out:
            lp.touches = touched_findings(lp, known)
            lp.unwired = unwired_options(lp, views.repo)
        return out
    except Exception:  # noqa: BLE001 — never fail the synthesizer over this
        log.exception("held changes of %s unreadable", sid)
        return []


def _post_files(views, lane: str, paths: list[str]) -> dict:
    out = {}
    for rel in paths:
        try:
            hp = views.host_path(lane, os.path.join(views.repo, rel))
            if os.path.isfile(hp) and os.path.getsize(hp) <= POST_FILE_MAX_CHARS:
                with open(hp, encoding="utf-8", errors="replace") as fh:
                    out[rel] = fh.read()
        except Exception:  # noqa: BLE001 — the diff alone still audits
            log.debug("post-patch %s of %s unreadable", rel, lane, exc_info=True)
    return out


def _ledger_findings(cwd: str) -> list:
    try:
        from claude_hooks.findings import Ledger, findings_enabled
        from claude_hooks.findings.ledger import findings_root
        if not findings_enabled():
            return []
        lg = Ledger(findings_root(cwd))
        return list(lg.iter_findings()) if lg.exists else []
    except Exception:  # noqa: BLE001 — memory never fails the synthesizer
        log.exception("ledger unreadable for held patches in %s", cwd)
        return []


def _same_file(a: str, b: str) -> bool:
    a, b = a.replace("\\", "/"), b.replace("\\", "/")
    return a == b or a.endswith("/" + b) or b.endswith("/" + a)


def touched_findings(lp: LanePatch, findings: list) -> list[dict]:
    """The findings (any status but refuted) with a ``path:line`` inside
    one of the lane's source hunks, give or take the ledger's
    ``LINE_WINDOW``."""
    from claude_hooks.findings.ledger import LINE_WINDOW
    out, seen = [], set()
    for f in findings:
        if f.status == "refuted" or f.id in seen:
            continue
        for p in f.paths:
            path, _, line = str(p).rpartition(":")
            if not line.isdigit():
                continue
            n = int(line)
            h = next((h for h in lp.hunks if not _tr.TEST_FILE.search(h.path)
                      and _same_file(h.path, path)
                      and h.old_range[0] - LINE_WINDOW <= n
                      < h.old_range[1] + LINE_WINDOW), None)
            if h is not None:
                seen.add(f.id)
                out.append({"id": f.id, "status": f.status, "title": f.title,
                            "where": f"{h.path}:{n}"})
                break
    return out


#: An option a hunk adds: a TypeScript optional member (``name?: T``) or a
#: Python keyword parameter (``name=…`` / ``name: T = …`` in a ``def``).
_TS_OPTION = re.compile(r"^\s*(?:readonly\s+)?([A-Za-z_]\w*)\?\s*:")
_PY_DEF = re.compile(r"^\s*(?:async\s+)?def\s+\w+\s*\((.*)")
_PY_KW = re.compile(r"(?:^|,)\s*\*?\s*([a-z_]\w*)\s*(?::[^=,]+)?=")
_SOURCE_EXT = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py")
MENTIONS_MAX = 6


def _git_grep(repo: str, name: str) -> list[str]:
    import subprocess
    try:
        r = subprocess.run(["git", "-C", repo, "grep", "-n", "-w", "-I",
                            "--untracked", "-e", name], capture_output=True,
                           text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return []
    return [ln for ln in r.stdout.splitlines() if ln.strip()]


def unwired_options(lp: LanePatch, repo: str, grep=_git_grep) -> list[dict]:
    """Options the lane's source hunks add that no other non-test file the
    patch changes mentions, with where the user's tree already mentions
    the name outside the declaring file (``mentions``, ``path:line``)."""
    out, seen = [], set()
    for h in lp.hunks:
        if _tr.TEST_FILE.search(h.path) or not h.path.endswith(_SOURCE_EXT):
            continue
        names = []
        for k, ln in enumerate(h.added):
            m = _TS_OPTION.match(ln)
            if m:
                names.append((m.group(1), k))
            d = _PY_DEF.match(ln)
            if d:
                names += [(x, k) for x in _PY_KW.findall(d.group(1))
                          if x not in ("self", "cls")]
        for name, k in names:
            if name in seen or any(re.search(rf"\b{re.escape(name)}\b", r)
                                   for r in h.removed):
                continue
            seen.add(name)
            rx = re.compile(rf"\b{re.escape(name)}\b")
            wired = any(rx.search(text) for path, text in lp.post.items()
                        if not _same_file(path, h.path)
                        and not _tr.TEST_FILE.search(path))
            if wired:
                continue
            mentions = []
            for g in grep(repo, name) if repo else []:
                path, _, rest = g.partition(":")
                line = rest.partition(":")[0]
                if (_same_file(path, h.path) or _tr.TEST_FILE.search(path)
                        or not path.endswith(_SOURCE_EXT)
                        or path.startswith(".claude-hooks/")):
                    continue
                mentions.append(f"{path}:{line}")
            home = h.path.split("/")
            mentions.sort(key=lambda m: -len(os.path.commonprefix(
                [home, m.partition(":")[0].split("/")])))
            out.append({"name": name, "where": f"{h.path}:{h.new_start + k}",
                        "mentions": mentions[:MENTIONS_MAX]})
    return out


def overlaps(patches: list[LanePatch]) -> list[dict]:
    """Pairs of lanes changing the same lines of a source file:
    ``{path, lanes: (a, b), lines: (lo, hi)}``, ``hi`` exclusive."""
    found: dict[tuple, list[int]] = {}
    for i, a in enumerate(patches):
        for b in patches[i + 1:]:
            for ha in a.hunks:
                if _tr.TEST_FILE.search(ha.path):
                    continue
                for hb in b.hunks:
                    if hb.path != ha.path:
                        continue
                    (a0, a1), (b0, b1) = ha.old_range, hb.old_range
                    both_new = ha.new_file and hb.new_file
                    if both_new or (a0 < b1 and b0 < a1):
                        k = (ha.path, a.lane, b.lane)
                        lo, hi = (min(a0, b0), max(a1, b1)) if not both_new \
                            else (1, 1 + max(len(ha.added), len(hb.added)))
                        cur = found.get(k)
                        found[k] = [min(lo, cur[0]), max(hi, cur[1])] if cur \
                            else [lo, hi]
    return [{"path": p, "lanes": (a, b), "lines": tuple(r)}
            for (p, a, b), r in found.items()]


HELD_RULES = (
    "Write a `## Held patches` section with one entry per lane above, "
    "naming the lane as it is named here:\n"
    "- `fixes #<n> (C<k>)` for a ranked defect the patch fixes, only when "
    "C<k> is a run of THAT lane, after the patch, whose test for #<n> "
    "passed (exit 0). Without such a run write `addresses #<n>, not "
    "verified`.\n"
    "- `does not fix #<m>` for every ranked defect in a file the patch "
    "touches that it leaves as it is, and for a ranked fix the diff does "
    "not contain.\n"
    "- Where OVERLAPS lists two lanes changing the same lines, say which "
    "one to apply and why, or write `conflict: <lane> vs <lane>` with "
    "what differs. Never present both as fixes.\n"
    "- A comment a patch adds is a claim, like a report's: where it "
    "describes code the diff does not contain, say so.\n"
    "- KNOWN FINDINGS THIS PATCH TOUCHES: for each, say whether the patch "
    "fixes it, leaves it as it is, or widens it, naming its id. A patch "
    "that fixes one finding and widens another is not a clean fix.\n"
    "- NEW OPTIONS NO PRODUCTION CALLER PASSES: a fix that works only "
    "when that option is passed is not a fix of the product until a "
    "production caller passes it. Name the caller that does, or say the "
    "fix is not wired, naming the option.")


def _render_diff(p: LanePatch, room: int) -> str:
    out, used = [], 0
    for h in p.hunks:
        if _tr.TEST_FILE.search(h.path):
            continue
        t = f"{h.path}\n{h.text}"
        if used + len(t) > min(room, LANE_DIFF_CHARS):
            out.append("… (more hunks not shown; review: "
                       "`claude-consultants changes <sid> --diff`)")
            break
        out.append(t)
        used += len(t)
    return "\n".join(out)


def _unwired_line(u: dict) -> str:
    seen = ("; the user's tree mentions the name at "
            + ", ".join(u["mentions"]) + " (unchanged by this patch)"
            if u["mentions"] else "; nothing else in the tree mentions it")
    return f"- `{u['name']}` ({u['where']}){seen}"


def held_block(patches: list[LanePatch], ovl: list[dict]) -> str:
    """The HELD CHANGES block ("" with no source patches)."""
    if not patches:
        return ""
    out = ["HELD CHANGES (what each lane changed in its private view; "
           "nothing is applied to the user's tree. Tests they wrote are "
           "not shown here; their runs are under COMMANDS THE LANES RAN):",
           HELD_RULES]
    room = TOTAL_DIFF_CHARS
    for p in patches:
        tests = [f for f in p.files if f not in p.source_files]
        out.append(f"\n### lane `{p.lane}` — source: "
                   + ", ".join(f"`{f}`" for f in p.source_files)
                   + (f"; tests: {len(tests)} file(s)" if tests else ""))
        d = _render_diff(p, room)
        room -= len(d)
        out.append("```diff\n" + d + "\n```" if d else "(diff not shown: "
                   "over the block's size)")
        if p.touches:
            out.append("KNOWN FINDINGS THIS PATCH TOUCHES:\n" + "\n".join(
                f"- {t['id']} [{t['status']}] {t['title']} ({t['where']})"
                for t in p.touches))
        if p.unwired:
            out.append("NEW OPTIONS NO PRODUCTION CALLER PASSES (no other "
                       "source file this patch changes mentions them):\n"
                       + "\n".join(_unwired_line(u) for u in p.unwired))
    if ovl:
        out.append("\nOVERLAPS (two lanes change the same lines):")
        for o in ovl:
            a, b = o["lanes"]
            out.append(f"- `{o['path']}` lines {o['lines'][0]}–"
                       f"{o['lines'][1] - 1}: `{a}` and `{b}`")
    return "\n".join(out)


# -------------------------------------------------------------- the audit
def _lane_rx(lane: str) -> re.Pattern:
    role, _, idx = lane.rpartition("-")
    if not idx.isdigit():
        return re.compile(re.escape(lane), re.I)
    return re.compile(rf"\b{re.escape(role)}[\s_:-]*(?:lane\s*)?#?{idx}\b", re.I)


#: A sub-heading naming a lane (run 8: ``**lane `coder-0`**`` under
#: ``### Held Patches`` was read as a heading of its own).
_LANE_HEADING = re.compile(r"\blane\b|\b(?:coder|researcher|tool_executor)"
                           r"[\s_:-]*#?\d+\b", re.I)


def _held_spans(lines: list[str]) -> list[tuple[str, int, int]]:
    """``(heading, a, b)`` of the held section, then of each sub-section
    after it whose heading names a lane ("" for the section itself)."""
    from consultants.engine.evidence import _sections
    secs = _sections(lines)
    for k, (h, a, b) in enumerate(secs):
        if HELD_HEADING.match("## " + h):
            out = [("", a, b)]
            for h2, a2, b2 in secs[k + 1:]:
                if not _LANE_HEADING.search(h2) or HELD_HEADING.match("## " + h2):
                    break
                out.append((h2, a2, b2))
            return out
    return []


def _held_section(lines: list[str]) -> Optional[tuple[int, int]]:
    spans = _held_spans(lines)
    return (spans[0][1], spans[-1][2]) if spans else None


def _blocks(lines: list[str], a: int, b: int) -> list[str]:
    """The section's entries: a list item with its continuation lines, a
    table row, or a paragraph."""
    out: list[str] = []
    for ln in lines[a:b]:
        s = ln.strip()
        if not s:
            out.append("")
            continue
        if re.match(r"^(?:[-*+]|\d{1,3}[.)])\s|^\|", s) or not out or out[-1] == "":
            out.append(ln)
        else:
            out[-1] += "\n" + ln
    return [x for x in out if x.strip()]


_NOT_FIX = re.compile(r"\b(?:does\s+not|doesn['’]t|not|never|no)\s+fix(?:es)?\b"
                      r"[^;.\n]*", re.I)
_FIX = re.compile(r"\bfix(?:es|ed)?\s+#?(\d{1,3}|[a-z]{1,6}f-\d+)\b([^;\n]*)",
                  re.I)


def _ref(tok: str):
    """A ranked number as an int, a ledger id (``csf-7``) as it is."""
    return int(tok) if tok.isdigit() else tok.lower()


@dataclass
class HeldAudit:
    missing: list = field(default_factory=list)     # lanes with no entry
    open: list = field(default_factory=list)        # overlaps not adjudicated
    unbacked: list = field(default_factory=list)    # (lane, n) fixes with no green run
    touched: list = field(default_factory=list)     # (lane, touch) not assessed
    unwired: list = field(default_factory=list)     # (lane, option) not named

    @property
    def ok(self) -> bool:
        return not (self.missing or self.open or self.unbacked
                    or self.touched or self.unwired)


def audit(text: str, patches: list[LanePatch], ovl: list[dict],
          rows: Optional[list[dict]]) -> HeldAudit:
    from consultants.engine.evidence import cited
    a = HeldAudit()
    if not patches:
        return a
    lines = (text or "").split("\n")
    blocks: list[str] = []
    for h, x, y in _held_spans(lines):
        if h:       # a lane's sub-section is that lane's one entry
            blocks.append(h + "\n" + "\n".join(lines[x:y]))
        else:
            blocks += _blocks(lines, x, y)
    by_n = {r["n"]: r for r in rows or []}
    for p in patches:
        rx = _lane_rx(p.lane)
        mine = [b for b in blocks if rx.search(b)]
        if not mine:
            a.missing.append(p)
            continue
        said = "\n".join(mine)
        for t in p.touches:
            if not re.search(rf"(?<![\w-]){re.escape(t['id'])}(?![\w-])", said):
                a.touched.append((p.lane, t))
        for u in p.unwired:
            if not re.search(rf"\b{re.escape(u['name'])}\b", said):
                a.unwired.append((p.lane, u))
        role, idx = p.role_lane
        for b in mine:
            for m in _FIX.finditer(_NOT_FIX.sub("", b)):
                runs = [by_n[n] for n in cited(m.group(2)) if n in by_n]
                green = any(r.get("role") == role and r.get("lane") == idx
                            and r.get("ran") and str(r.get("exit")) == "exit 0"
                            for r in runs)
                if not green:
                    a.unbacked.append((p.lane, _ref(m.group(1))))
    for o in ovl:
        ra, rb = (_lane_rx(x) for x in o["lanes"])
        if not any(ra.search(b) and rb.search(b) for b in blocks):
            a.open.append(o)
    return a


def revision_note(h: HeldAudit) -> str:
    parts = []
    if h.missing:
        parts.append("- `## Held patches` has no entry for these lanes, "
                     "whose patches change source. Say which ranked defects "
                     "each fixes (with its own green run) and which it does "
                     "not:\n" + "\n".join(
                         f"  - `{p.lane}`: " + ", ".join(p.source_files[:6])
                         for p in h.missing))
    if h.unbacked:
        parts.append("- These `fixes #n` cite no green run of the same lane "
                     "after its patch. Cite one, or write `addresses #n, not "
                     "verified`:\n" + "\n".join(
                         f"  - `{lane}` fixes #{n}" for lane, n in h.unbacked))
    if h.touched:
        parts.append("- These patches change the code of known findings "
                     "their entries do not mention. Say, naming the id, "
                     "whether each patch fixes it, leaves it, or widens it:\n"
                     + "\n".join(f"  - `{lane}` touches {t['id']} ({t['title']}"
                                 f", {t['where']})" for lane, t in h.touched))
    if h.unwired:
        parts.append("- These patches add options no production caller "
                     "they change passes. Name the caller that passes each, "
                     "or say the fix through it is not wired:\n"
                     + "\n".join(f"  - `{lane}`: " + _unwired_line(u)[2:]
                                 for lane, u in h.unwired))
    if h.open:
        parts.append("- These lanes change the same lines and your answer "
                     "does not choose. Say which one to apply and why, or "
                     "write `conflict: <lane> vs <lane>`:\n" + "\n".join(
                         f"  - `{o['path']}` lines {o['lines'][0]}–"
                         f"{o['lines'][1] - 1}: `{o['lanes'][0]}` and "
                         f"`{o['lanes'][1]}`" for o in h.open))
    return "\n".join(parts)


NOT_ASSESSED = ("not assessed by the answer (harness): which ranked defects "
                "it fixes is unknown; review it before applying")
UNBACKED_FIX = ("harness: no green run of this lane after its patch is "
                "cited for #{n}")
TOUCHED = ("`{lane}` changes the code of {id} ({title}, {where}); the "
           "answer does not say whether it fixes, leaves or widens it "
           "(harness) — check before applying")
UNWIRED = ("`{lane}` adds `{name}` ({where}), which no production file the "
           "patch changes passes: a fix through it is not wired into the "
           "product until a caller passes it (harness)")
CONFLICT = ("conflict, not adjudicated (harness): `{a}` and `{b}` both "
            "change `{path}` lines {lo}–{hi}; review both before applying "
            "either")


def _put_held(lines: list[str], new: list[str]) -> list[str]:
    from consultants.engine.evidence import _insert_under
    if _insert_under(lines, HELD_HEADING, new):
        return lines
    return lines + ["", "## Held patches", ""] + new


def enforce(text: str, h: HeldAudit) -> str:
    """The answer with what ``## Held patches`` still owes written in by
    the harness: an entry per lane it skipped, a conflict per overlap it
    did not adjudicate, a note on each ``fixes #n`` no green run backs."""
    if h.ok:
        return text
    lines = text.rstrip().split("\n")
    sec = _held_section(lines)
    if sec and h.unbacked:
        from consultants.engine.evidence import _HEADING
        owed = set(h.unbacked)
        lane, by_heading = None, False
        for i in range(*sec):
            ln = lines[i]
            named = [p for p, _ in owed if _lane_rx(p).search(ln)]
            heading = bool(_HEADING.match(ln))
            if named:
                lane, by_heading = named[0], heading
            elif heading:
                lane, by_heading = None, False
            elif re.match(r"^\s{0,3}(?:[-*+]|\d{1,3}[.)])\s|^\s*\|", ln) \
                    and not ln[:1].isspace() and not by_heading:
                lane = None              # a new entry for another lane
            for m in _FIX.finditer(_NOT_FIX.sub("", ln)):
                n = _ref(m.group(1))
                note = UNBACKED_FIX.format(n=n)
                if (lane, n) in owed and note not in ln:
                    lines[i] = lines[i].rstrip() + f" _({note})_"
    new = [f"- `{p.lane}` ({', '.join(p.source_files[:6])}): {NOT_ASSESSED}."
           for p in h.missing]
    new += ["- " + TOUCHED.format(lane=lane, **t) + "." for lane, t in h.touched
            if lane not in {p.lane for p in h.missing}]
    new += ["- " + UNWIRED.format(lane=lane, name=u["name"], where=u["where"])
            + "." for lane, u in h.unwired]
    new += ["- " + CONFLICT.format(a=o["lanes"][0], b=o["lanes"][1],
                                   path=o["path"], lo=o["lines"][0],
                                   hi=o["lines"][1] - 1) + "."
            for o in h.open]
    if new:
        lines = _put_held(lines, new)
    return "\n".join(lines).rstrip() + "\n"


# ------------------------------------------------- comments against the diff
COMMENT_AUDIT_SYSTEM = (
    "You check what a code comment says against the patch that adds it, "
    "and nothing else. FILES gives, once each, a lane's whole patch to a "
    "file (every hunk) and the file as that patch leaves it. Each item is "
    "a comment the patch adds, with the hunk it sits in and the file it "
    "belongs to. Judge the comment against the WHOLE patch and the patched "
    "file, not its own hunk alone: a call, a join or a guard added in "
    "another hunk of the file counts, and so does code the patch left as "
    "it was. Read the code as code: a parameter's default value is not an "
    "assignment that runs before the check, so look at what the callers "
    "pass. Decide, for each, whether the patched file contains the code "
    "the comment says the patch adds or does (a guard, a lock, a check, a "
    "call):\n"
    "- SHOWN: every change the comment claims is in the patched file, or "
    "the comment only explains code that is there.\n"
    "- NOT SHOWN: the comment claims code (or a fix of a named defect) "
    "that the patched file does not have.\n"
    "- CONTRADICTED: the patched code does the opposite of what the "
    "comment says.\n"
    "- PARTLY: some of what it claims is there, some is not; name what is "
    "missing.\n"
    "A comment describing the old behaviour implies the patch fixes it: "
    "judge whether the fix is there. Use only the text given. Answer one "
    "line per item and nothing else:\n"
    "`A<k>: SHOWN|NOT SHOWN|CONTRADICTED|PARTLY — <what is missing, or the "
    "added line you relied on>`")
COMMENT_NOTE = ("⚠ `{lane}` `{path}:{line}`: the comment it adds says "
                "“{comment}”, but its diff {verdict}: {why} (harness's audit "
                "of the patch)")


def comment_items(patches: list[LanePatch]) -> list[dict]:
    """Each run of comment lines a source hunk adds, of at least
    :data:`COMMENT_MIN_WORDS` words, with that hunk's code; taken in turn
    from each patched file so every one is reached before any gives a
    second (run 6: the apply-patch.ts comment came after 13 from
    other files)."""
    out = []
    for p in patches:
        for h in p.hunks:
            if _tr.TEST_FILE.search(h.path):
                continue
            runs, cur, at = [], [], None
            for k, ln in enumerate(h.added):
                if _COMMENT.match(ln):
                    if not cur:
                        at = k
                    cur.append(ln.strip())
                elif cur:
                    runs.append((at, cur))
                    cur = []
            if cur:
                runs.append((at, cur))
            code = [ln for ln in h.added if ln.strip() and not _COMMENT.match(ln)]
            for at, c in runs:
                text = " ".join(re.sub(r"^(?://+|#+|/\*+|\*+/?|--|\"\"\"|''')\s?",
                                       "", x) for x in c).strip()
                if len(re.findall(r"\w+", text)) < COMMENT_MIN_WORDS:
                    continue
                out.append({"lane": p.lane, "path": h.path,
                            "line": h.new_start + at, "comment": text,
                            "added": code, "removed": h.removed,
                            "file_diff": p.file_diff(h.path),
                            "post": p.post.get(h.path, "")})
    per: dict[tuple, list] = {}
    for it in out:
        per.setdefault(it["path"], []).append(it)
    fair = []
    while len(fair) < COMMENT_MAX_ITEMS and any(per.values()):
        for q in per.values():
            if q and len(fair) < COMMENT_MAX_ITEMS:
                fair.append(q.pop(0))
    return fair


def _file_key(it: dict) -> tuple[str, str]:
    return it["lane"], it["path"]


def _file_part(j: int, it: dict) -> str:
    diff = it.get("file_diff") or ""
    post = it.get("post") or ""
    body = "\n".join(f"{n:>5}  {ln}" for n, ln in
                     enumerate(post.split("\n"), start=1)) if post else ""
    return (f"F{j}. `{it['lane']}` `{it['path']}`\nTHE PATCH TO THIS FILE "
            f"(every hunk):\n```diff\n{diff}\n```"
            + (f"\nTHE FILE AS PATCHED:\n```\n{body}\n```" if body else
               "\n(the patched file is not available: judge from the patch)"))


def _comment_part(k: int, it: dict, j: int = 0) -> str:
    add = "\n".join("+ " + x for x in it["added"]) or "(no code added)"
    rem = "\n".join("- " + x for x in it["removed"])
    return (f"A{k}. COMMENT ({it['lane']}, {it['path']}:{it['line']}"
            + (f", file F{j}" if j else "") + f"): {it['comment']}\n"
            f"ITS HUNK ADDS:\n{add}"
            + (f"\nITS HUNK REMOVES:\n{rem}" if rem else ""))


def comment_audit_prompt(items: list[dict]) -> str:
    """FILES (each lane's whole patch to a file and the patched file,
    once), then every item whole: the comment and its hunk."""
    files: dict[tuple, int] = {}
    for it in items:
        files.setdefault(_file_key(it), len(files) + 1)
    first = {}
    for it in items:
        first.setdefault(_file_key(it), it)
    head = "FILES:\n\n" + "\n\n".join(_file_part(j, first[k])
                                        for k, j in files.items())
    return head + "\n\n===\n\nCOMMENTS:\n\n" + "\n\n---\n\n".join(
        _comment_part(k, it, files[_file_key(it)])
        for k, it in enumerate(items, start=1))


def comment_audit_batches(items: list[dict], budget_chars: int) -> list[list[int]]:
    """Item indexes in groups whose prompt fits ``budget_chars``, each
    file counted once per group; nothing is cut, so an item (with its
    file) larger than the budget is a group of its own."""
    groups: list[list[int]] = []
    used = 0
    seen: set = set()
    for i, it in enumerate(items):
        own = len(_comment_part(i + 1, it, 1)) + 9
        file_cost = len(_file_part(1, it)) + 4
        cost = own + (0 if _file_key(it) in seen else file_cost)
        if groups and used + cost <= budget_chars:
            groups[-1].append(i)
            used += cost
            seen.add(_file_key(it))
        else:
            groups.append([i])
            used = own + file_cost
            seen = {_file_key(it)}
    return groups


def apply_comment_audit(text: str, items: list[dict],
                        verdicts: dict[int, tuple[str, str]]) -> tuple[str, int]:
    notes = []
    for k, it in enumerate(items, start=1):
        v = verdicts.get(k)
        if not v or v[0] == "shown":
            continue
        c = it["comment"]
        notes.append("- " + COMMENT_NOTE.format(
            lane=it["lane"], path=it["path"], line=it["line"],
            comment=c if len(c) <= 200 else c[:200] + "…",
            verdict={"not shown": "does not contain it",
                     "contradicted": "does the opposite",
                     "partly": "contains only part of it"}[v[0]],
            why=v[1] or "no reason given") + ".")
    if not notes:
        return text, 0
    return "\n".join(_put_held(text.rstrip().split("\n"), notes)).rstrip() \
        + "\n", len(notes)


__all__ = ["held_changes", "overlaps", "held_block", "audit", "enforce",
           "revision_note", "comment_items", "comment_audit_prompt",
           "apply_comment_audit", "comment_audit_batches", "COMMENT_AUDIT_SYSTEM", "HeldAudit",
           "LanePatch", "Hunk", "parse_diff", "HELD_RULES"]
