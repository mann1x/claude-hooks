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

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_COMMENT = re.compile(r"^\s*(?://|#(?![!\w])|#\s|/\*|\*(?!\*?/?\s*$)|--\s|\"\"\"|''')")
HELD_HEADING = re.compile(r"^\s{0,3}(?:#{1,6}\s*|\*\*)\s*held patch", re.I | re.M)


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
                out.append(lp)
        return out
    except Exception:  # noqa: BLE001 — never fail the synthesizer over this
        log.exception("held changes of %s unreadable", sid)
        return []


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
    "describes code the diff does not contain, say so.")


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


def _held_section(lines: list[str]) -> Optional[tuple[int, int]]:
    from consultants.engine.evidence import _sections
    for h, a, b in _sections(lines):
        if HELD_HEADING.match("## " + h):
            return a, b
    return None


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
_FIX = re.compile(r"\bfix(?:es|ed)?\s+#(\d{1,3})\b([^;\n]*)", re.I)


@dataclass
class HeldAudit:
    missing: list = field(default_factory=list)     # lanes with no entry
    open: list = field(default_factory=list)        # overlaps not adjudicated
    unbacked: list = field(default_factory=list)    # (lane, n) fixes with no green run

    @property
    def ok(self) -> bool:
        return not (self.missing or self.open or self.unbacked)


def audit(text: str, patches: list[LanePatch], ovl: list[dict],
          rows: Optional[list[dict]]) -> HeldAudit:
    from consultants.engine.evidence import cited
    a = HeldAudit()
    if not patches:
        return a
    lines = (text or "").split("\n")
    sec = _held_section(lines)
    blocks = _blocks(lines, *sec) if sec else []
    by_n = {r["n"]: r for r in rows or []}
    for p in patches:
        rx = _lane_rx(p.lane)
        mine = [b for b in blocks if rx.search(b)]
        if not mine:
            a.missing.append(p)
            continue
        role, idx = p.role_lane
        for b in mine:
            for m in _FIX.finditer(_NOT_FIX.sub("", b)):
                runs = [by_n[n] for n in cited(m.group(2)) if n in by_n]
                green = any(r.get("role") == role and r.get("lane") == idx
                            and r.get("ran") and str(r.get("exit")) == "exit 0"
                            for r in runs)
                if not green:
                    a.unbacked.append((p.lane, int(m.group(1))))
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
        owed = set(h.unbacked)
        lane = None
        for i in range(*sec):
            ln = lines[i]
            named = [p for p, _ in owed if _lane_rx(p).search(ln)]
            if named:
                lane = named[0]
            elif re.match(r"^\s{0,3}(?:[-*+]|\d{1,3}[.)])\s|^\s*\|", ln) \
                    and not ln[:1].isspace():
                lane = None              # a new entry for another lane
            for m in _FIX.finditer(_NOT_FIX.sub("", ln)):
                n = int(m.group(1))
                note = UNBACKED_FIX.format(n=n)
                if (lane, n) in owed and note not in ln:
                    lines[i] = lines[i].rstrip() + f" _({note})_"
    new = [f"- `{p.lane}` ({', '.join(p.source_files[:6])}): {NOT_ASSESSED}."
           for p in h.missing]
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
    "and nothing else. Each item below is a comment a patch adds, and "
    "under it the lines that hunk adds and removes. Decide, for each, "
    "whether the hunk contains the code the comment says the patch adds "
    "or does (a guard, a lock, a check, a call):\n"
    "- SHOWN: every change the comment claims is in the hunk's added "
    "lines, or the comment only explains code that is there.\n"
    "- NOT SHOWN: the comment claims code (or a fix of a named defect) "
    "that the hunk does not add.\n"
    "- CONTRADICTED: the hunk does the opposite of what the comment says.\n"
    "- PARTLY: some of what it claims is there, some is not; name what is "
    "missing.\n"
    "A comment describing the old behaviour implies the patch fixes it: "
    "judge whether the fix is there. Use only the lines given. Answer one "
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
                            "added": code, "removed": h.removed})
    per: dict[tuple, list] = {}
    for it in out:
        per.setdefault(it["path"], []).append(it)
    fair = []
    while len(fair) < COMMENT_MAX_ITEMS and any(per.values()):
        for q in per.values():
            if q and len(fair) < COMMENT_MAX_ITEMS:
                fair.append(q.pop(0))
    return fair


def _comment_part(k: int, it: dict) -> str:
    add = "\n".join("+ " + x for x in it["added"]) or "(no code added)"
    rem = "\n".join("- " + x for x in it["removed"])
    return (f"A{k}. COMMENT ({it['lane']}, {it['path']}:{it['line']}): "
            f"{it['comment']}\nTHE HUNK ADDS:\n{add}"
            + (f"\nTHE HUNK REMOVES:\n{rem}" if rem else ""))


def comment_audit_prompt(items: list[dict]) -> str:
    """Every item whole: the comment, all the code its hunk adds and
    removes."""
    return "\n\n---\n\n".join(_comment_part(k, it)
                                for k, it in enumerate(items, start=1))


def comment_audit_batches(items: list[dict], budget_chars: int) -> list[list[int]]:
    """Item indexes in groups whose prompt fits ``budget_chars``; a hunk
    is never cut, so one larger than the budget is a group of its own."""
    groups: list[list[int]] = []
    used = 0
    for i, it in enumerate(items):
        cost = len(_comment_part(i + 1, it)) + 9
        if groups and used + cost <= budget_chars:
            groups[-1].append(i)
            used += cost
        else:
            groups.append([i])
            used = cost
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
