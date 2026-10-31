"""The findings ledger: ``<project>/.claude-hooks/findings/``.

The files are the record (docs/PLAN-council-memory.md). One file per
finding, named by an id whose prefix is the project's task prefix plus
``f`` (``chf-3``), derived once and written into ``config.toml``.

Who may do what:

- :meth:`Ledger.record` — anyone, the council included: a new
  ``candidate``, or evidence added to the finding it matches.
- :meth:`Ledger.settle` / :meth:`Ledger.reopen` — only ``user`` or
  ``assistant``, always with a reason; ``fixed`` needs a commit. There is
  no other way to change a status, so nothing the council runs can
  settle what it found.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterable, Iterator, Optional, Sequence

from claude_hooks._atomic import write_text_atomic
from claude_hooks.findings.model import (
    CANDIDATE, CLAIM, DECIDERS, DECISION, EVIDENCE, FIXED, LOG, REPRODUCE,
    SETTLED,
    SEVERITIES, Finding, normalise_status,
)
from claude_hooks.tasks.files import (
    TASKS_DIR, derive_prefix, project_root, read_config, render_config,
)
from claude_hooks.tasks.model import TaskFormatError, stamp

FINDINGS_DIR = Path(".claude-hooks") / "findings"
CONFIG = "config.toml"
#: Where a merged finding goes: out of the listing, its id kept so it is
#: never reused and a later tag of it still finds where it went.
MERGED_DIR = "merged"
MERGED_INTO = "merged_into"
_MERGE_HOPS = 20
_FILE = re.compile(r"^([a-z][a-z0-9]*)-(\d+)\.md$")
_ALLOCATE_ATTEMPTS = 50


class FindingNotFound(LookupError):
    pass


def findings_root(cwd: Optional[str] = None) -> Path:
    """The project a ledger belongs to: one that already has a ledger,
    else the tasks rule (:func:`claude_hooks.tasks.files.project_root`)."""
    start = Path(cwd or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    try:
        start = start.resolve()
    except OSError:
        start = start.absolute()
    for d in (start, *start.parents):
        if (d / FINDINGS_DIR / CONFIG).is_file():
            return d
    return project_root(str(start))


# ─── matching ────────────────────────────────────────────────────────

_STOP = frozenset("""
the and that this with from into when then than there their they them what
which while where will would could should does doesn have has had been being
were was are not but its it's also only just more most some such very each
file files line lines code tool tools call calls test tests case cases
""".split())
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]{3,}")
#: Lines within this many of each other in the same file are the same place.
LINE_WINDOW = 40
#: Shared words a claim at the same place needs to be the same finding.
#: Two claims at one line can differ: "grep launders an outside write"
#: (a defect) and "grep credits a full-file read" (by design) share
#: grep.ts:260 and score 0.25; restatements of one claim score 0.5+.
SAME_PLACE_WORDS = 0.35


def _stem(w: str) -> str:
    w = w.lower()
    for suf in ("ations", "ation", "ings", "ing", "ers", "ies", "es", "ed",
                "er", "ly", "s"):
        if len(w) > len(suf) + 3 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def words(text: str) -> set[str]:
    return {_stem(w) for w in _WORD.findall(text or "")
            if w.lower() not in _STOP}


def _split_path(p: str) -> tuple[str, Optional[int]]:
    p = str(p or "").strip().strip("`").replace("\\", "/")
    m = re.match(r"^(.*?):(\d+)(?:-\d+)?$", p)
    if m:
        return m.group(1).lstrip("./"), int(m.group(2))
    return p.lstrip("./"), None


def _same_file(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if a == b:
        return True
    long, short = (a, b) if len(a) >= len(b) else (b, a)
    # A bare name (``grep.ts``, as answers cite) is the file of that name;
    # the word threshold keeps unrelated claims in same-named files apart.
    return long.endswith("/" + short)


def place_score(a: Sequence[str], b: Sequence[str]) -> float:
    """1.0 when two path lists share a place (same file, lines within
    :data:`LINE_WINDOW`), 0.5 when they share only a file, else 0."""
    best = 0.0
    for pa in a or ():
        fa, la = _split_path(pa)
        for pb in b or ():
            fb, lb = _split_path(pb)
            if not _same_file(fa, fb):
                continue
            if la is None or lb is None or abs(la - lb) <= LINE_WINDOW:
                return 1.0
            best = 0.5
    return best


def word_score(a: str, b: str) -> float:
    wa, wb = words(a), words(b)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def same_finding(f: Finding, claim: str, paths: Sequence[str]) -> bool:
    """Whether a new claim is this finding again: the same place and some
    of the same words, or most of the same words where neither names a
    place."""
    text = f"{f.title}\n{f.claim}"
    place = place_score(f.paths, paths)
    wscore = word_score(text, claim)
    if place >= 1.0:
        return wscore >= SAME_PLACE_WORDS
    if not f.paths or not paths:
        return wscore >= 0.5
    return False


# ─── the ledger ──────────────────────────────────────────────────────


class Ledger:
    """One project's findings."""

    def __init__(self, root: Path):
        # Resolved: the prefix comes from the directory's name, and
        # Path(".").name is "" (it gave "taf", from "task").
        self.root = Path(root).resolve()
        self.dir = self.root / FINDINGS_DIR
        self._config: Optional[dict] = None

    @classmethod
    def for_cwd(cls, cwd: Optional[str] = None) -> "Ledger":
        return cls(findings_root(cwd))

    # config --------------------------------------------------------

    @property
    def exists(self) -> bool:
        return (self.dir / CONFIG).is_file()

    def config(self) -> dict:
        if self._config is None:
            self._config = read_config(self.dir / CONFIG)
        return self._config

    def init(self) -> dict:
        if self.exists:
            return self.config()
        task_prefix = read_config(self.root / TASKS_DIR / CONFIG).get("prefix")
        base = task_prefix or derive_prefix(self.root.name)
        cfg = {"project": self.root.name, "prefix": f"{base}f"}
        self.dir.mkdir(parents=True, exist_ok=True)
        write_text_atomic(self.dir / CONFIG, render_config(cfg).replace(
            "# Task tracking for this project (claude-hooks).",
            "# Council findings for this project (claude-hooks)."))
        self._config = cfg
        return cfg

    @property
    def prefix(self) -> str:
        return self.init()["prefix"]

    # files ---------------------------------------------------------

    def path_for(self, fid: str) -> Path:
        return self.dir / f"{fid.strip().lower()}.md"

    def merged_path(self, fid: str) -> Path:
        return self.dir / MERGED_DIR / f"{fid.strip().lower()}.md"

    def resolve(self, fid: str) -> Optional[str]:
        """The id a finding lives under now: ``fid`` itself, or the
        finding it was merged into (followed through chains); None when
        neither exists."""
        cur = (fid or "").strip().lower()
        for _ in range(_MERGE_HOPS):
            if not cur:
                return None
            if self.path_for(cur).is_file():
                return cur
            m = self.merged_path(cur)
            if not m.is_file():
                return None
            try:
                cur = str(Finding.from_markdown(m.read_text(encoding="utf-8"))
                          .extra.get(MERGED_INTO) or "").strip().lower()
            except (OSError, UnicodeDecodeError, TaskFormatError):
                return None
        return None

    def read(self, fid: str) -> Finding:
        p = self.path_for(fid)
        if not p.is_file():
            to = self.resolve(fid)
            if to and to != fid.strip().lower():
                raise FindingNotFound(f"{fid} was merged into {to}")
            raise FindingNotFound(f"no finding {fid} in {self.dir}")
        return Finding.from_markdown(p.read_text(encoding="utf-8"), source=str(p))

    def write(self, f: Finding) -> Path:
        p = self.path_for(f.id)
        write_text_atomic(p, f.to_markdown())
        return p

    def iter_findings(self) -> Iterator[Finding]:
        """Every readable finding, by number; unreadable files skipped."""
        try:
            entries = sorted(
                (e for e in os.scandir(self.dir)
                 if _FILE.match(e.name.lower()) and e.is_file()),
                key=lambda e: int(_FILE.match(e.name.lower()).group(2)))
        except FileNotFoundError:
            return
        for e in entries:
            try:
                yield Finding.from_markdown(
                    Path(e.path).read_text(encoding="utf-8"), source=e.path)
            except (OSError, UnicodeDecodeError, TaskFormatError):
                continue

    def _next_number(self) -> int:
        n = 0
        pre = self.prefix
        names = []
        for d in (self.dir, self.dir / MERGED_DIR):
            try:
                names += os.listdir(d)
            except FileNotFoundError:
                pass
        for name in names:
            m = _FILE.match(name.lower())
            if m and m.group(1) == pre:
                n = max(n, int(m.group(2)))
        return n + 1

    def _create(self, factory) -> Finding:
        self.init()
        n = self._next_number()
        for _ in range(_ALLOCATE_ATTEMPTS):
            fid = f"{self.prefix}-{n}"
            dest = self.path_for(fid)
            try:
                fd = os.open(dest, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                n += 1
                continue
            os.close(fd)
            try:
                f = factory(fid)
                self.write(f)
            except BaseException:
                try:
                    dest.unlink()
                except OSError:
                    pass
                raise
            return f
        raise RuntimeError(f"could not allocate a finding id in {self.dir}")

    # operations ----------------------------------------------------

    def match(self, claim: str, paths: Sequence[str] = ()) -> Optional[Finding]:
        """The finding this claim restates, if any; a settled one first,
        since that is the one a council must hear about."""
        hits = [f for f in self.iter_findings() if same_finding(f, claim, paths)]
        hits.sort(key=lambda f: (not f.settled,
                                 -place_score(f.paths, paths),
                                 -word_score(f"{f.title}\n{f.claim}", claim)))
        return hits[0] if hits else None

    def record(self, title: str, claim: str, *, paths: Iterable[str] = (),
               symbols: Iterable[str] = (), severity: str = "M",
               evidence: str = "", reproduce: str = "", council: str = "",
               known: str = "") -> tuple[Finding, bool]:
        """A council's claim: evidence on the finding it matches, else a
        new candidate. ``known`` is an id the claim was tagged with (the
        answer said ``(known: csf-7)``); when it names a finding, that is
        the match. Returns ``(finding, created)``. Never changes a
        status."""
        paths = [str(p) for p in paths if str(p).strip()]
        symbols = [str(s) for s in symbols if str(s).strip()]
        if not (title or "").strip() or not (claim or "").strip():
            raise ValueError("a finding needs a title and a claim")
        hit = None
        known = self.resolve(known) if known else None
        if known:
            try:
                hit = self.read(known)
            except (OSError, ValueError):
                hit = None
        if hit is None:
            hit = self.match(f"{title}\n{claim}", paths)
        if hit is not None:
            if evidence:
                hit.add_evidence(evidence, council=council)
            elif council and council not in hit.councils:
                hit.councils.append(council)
            for p in paths:          # a bare name of a path it has is not new
                if p not in hit.paths and place_score(hit.paths, [p]) < 1.0:
                    hit.paths.append(p)
            for s in symbols:
                if s not in hit.symbols:
                    hit.symbols.append(s)
            if reproduce and not hit.reproduce:
                hit.set_section(REPRODUCE, reproduce)
            seen = f"seen again{' in ' + council if council else ''}"
            if not (council and any(ln.endswith(seen) for ln in
                                    hit.section(LOG).split("\n"))):
                hit.add_log(seen)
            hit.touch()
            self.write(hit)
            return hit, False
        sev = (severity or "M").upper()[:1]
        if sev not in SEVERITIES:
            sev = "M"

        def factory(fid: str) -> Finding:
            now = stamp()
            f = Finding(id=fid, title=" ".join(title.split()), severity=sev,
                        paths=paths, symbols=symbols, created=now, updated=now)
            f.set_section(CLAIM, claim)
            if reproduce:
                f.set_section(REPRODUCE, reproduce)
            if evidence:
                f.add_evidence(evidence, council=council)
            elif council:
                f.councils.append(council)
            f.add_log(f"recorded as candidate{' by ' + council if council else ''}")
            return f
        return self._create(factory), True

    def settle(self, fid: str, status: str, reason: str, *, by: str,
               commit: str = "", task: str = "") -> Finding:
        """Decide a finding. Only ``user`` / ``assistant``, with a reason;
        ``fixed`` needs ``commit``."""
        status = normalise_status(status)
        if status not in SETTLED:
            raise ValueError(f"settle to one of {', '.join(SETTLED)}; "
                             "use reopen to make it a candidate again")
        self._check_decider(by, reason)
        if status == FIXED and not (commit or "").strip():
            raise ValueError("a fixed finding names the commit that fixed it")
        f = self.read(fid)
        old = f.status
        f.status = status
        f.settled_by = by
        f.settled_at = stamp()
        if commit:
            f.commit = commit.strip()
        if task:
            f.task = task.strip()
        f.set_section(DECISION, reason.strip())
        f.add_log(f"settled {status} by {by} (was {old}): {reason}")
        f.touch()
        self.write(f)
        return f

    def reopen(self, fid: str, reason: str, *, by: str) -> Finding:
        """Back to ``candidate``: new evidence against a decision."""
        self._check_decider(by, reason)
        f = self.read(fid)
        old = f.status
        f.status = CANDIDATE
        f.settled_by = f.settled_at = ""
        f.add_log(f"reopened by {by} (was {old}): {reason}")
        f.touch()
        self.write(f)
        return f

    def merge(self, fid: str, into: str, reason: str, *, by: str) -> Finding:
        """Fold ``fid``, a finding that restates ``into``, into it. Only
        ``user`` / ``assistant``, with a reason. ``into`` keeps its own
        status and decision and gains ``fid``'s evidence, councils, paths,
        symbols and (when it has none) reproduction; ``fid`` moves to
        ``merged/`` with ``merged_into`` set, so its id is never reused
        and a later tag of it lands on ``into``. Returns ``into``."""
        self._check_decider(by, reason)
        a, b = fid.strip().lower(), into.strip().lower()
        if a == b:
            raise ValueError("a finding cannot be merged into itself")
        dup = self.read(a)
        tgt = self.read(b)
        for line in reversed(dup.evidence_lines):
            if line not in tgt.evidence_lines:
                tgt._prepend(EVIDENCE, line)
        for c in dup.councils:
            if c not in tgt.councils:
                tgt.councils.append(c)
        for p in dup.paths:
            if p not in tgt.paths and place_score(tgt.paths, [p]) < 1.0:
                tgt.paths.append(p)
        for sym in dup.symbols:
            if sym not in tgt.symbols:
                tgt.symbols.append(sym)
        if dup.reproduce and not tgt.reproduce:
            tgt.set_section(REPRODUCE, dup.reproduce)
        tgt.add_log(f"merged {dup.id} ({dup.title}, was {dup.status}) by "
                    f"{by}: {reason}")
        tgt.touch()
        self.write(tgt)
        dup.extra[MERGED_INTO] = tgt.id
        dup.add_log(f"merged into {tgt.id} by {by}: {reason}")
        dup.touch()
        dest = self.merged_path(dup.id)
        dest.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(dest, dup.to_markdown())
        self.path_for(dup.id).unlink()
        return tgt

    def note(self, fid: str, text: str) -> Finding:
        if not (text or "").strip():
            raise ValueError("an empty note says nothing")
        f = self.read(fid)
        f.add_log(text)
        f.touch()
        self.write(f)
        return f

    def list(self, status: Optional[str] = None) -> list[Finding]:
        want = None
        if status:
            want = (SETTLED if status == "settled"
                    else (normalise_status(status),))
        return [f for f in self.iter_findings()
                if want is None or f.status in want]

    def relevant(self, text: str, paths: Sequence[str] = (), *, k: int = 12
                 ) -> list[Finding]:
        """The findings a council on ``text`` (and ``paths``) should hear
        about, best first: a shared place counts most, then shared words."""
        scored = []
        for f in self.iter_findings():
            s = place_score(f.paths, paths) + word_score(
                f"{f.title}\n{f.claim}\n{' '.join(f.paths)}", text)
            if s > 0.05:
                scored.append((s, f))
        scored.sort(key=lambda x: -x[0])
        return [f for _, f in scored[:k]]

    @staticmethod
    def _check_decider(by: str, reason: str) -> None:
        if by not in DECIDERS:
            raise ValueError(f"only {' or '.join(DECIDERS)} settle a finding "
                             f"(got {by!r}); a council records candidates")
        if not (reason or "").strip():
            raise ValueError("a decision needs a reason: the run, the commit, "
                             "or where the design says so")


__all__ = ["Ledger", "FindingNotFound", "findings_root", "same_finding",
           "place_score", "word_score", "FINDINGS_DIR"]

