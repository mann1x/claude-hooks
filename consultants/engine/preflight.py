"""Pre-flight reachability check for the paths a question names.

A council run that cannot see the code it was asked about does not
fail — it produces a confident answer built on nothing, and the only
signal is a wall of ``[unverified — file not found]`` annotations at
the very end. The csl-2026-08-02-0532-d737 consultancy spent
**55 minutes of cloud inference** to reach that state because one
allowed root never reached the run.

The check is the cheapest possible: the question already names the
files it is about (``eval/scorers.py:600``, ``a2at/tools_dataset.py``),
and whether those files are readable under the session's roots is a
handful of ``stat`` calls. Doing it before the first token is spent
turns a 55-minute wrong answer into an instant, actionable refusal.

Deciding what is a *blocking* problem
=====================================

Refusing on any unresolvable path would break the ordinary "write me
``pkg/newthing.py``" question, where the file legitimately does not
exist yet. The discriminator is the **parent directory**:

* path resolves under a root                → ``ok``
* path missing but its directory resolves   → ``creatable`` (warn only;
  this is the "please add a file here" shape)
* neither the path nor its directory resolve → ``unreachable``
  (blocking; the run cannot see this part of the tree at all)

That keeps the refusal to the case it was built for — a root the
session was never given — while leaving every normal question alone.

Deliberately stdlib-only so the main claude-hooks test suite can
exercise it without the LangChain stack installed.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

# Same shape as the citation linter's path pattern, minus the
# mandatory ``:line`` suffix — a question says "look at
# ``eval/scorers.py``" as often as it cites a line. Requires at least
# one directory separator so prose words with a dot ("v1.8", "etc.")
# and bare filenames ("Makefile") don't register.
_SEGMENT = r"[A-Za-z0-9_][A-Za-z0-9_.\-]*"
_PATH_RE = re.compile(
    # Leading ``/`` captured deliberately: a question saying "read
    # /shared/dev/x/eval/y.py" must be checked as the absolute path it
    # is. Dropping the slash and testing ``shared/dev/…`` under each
    # root would report a readable file as unreachable and refuse a
    # run that would have worked.
    rf"(?P<path>/?(?:{_SEGMENT}/)+{_SEGMENT}\.\w+)"
    r"(?::(?P<line_start>\d+)(?:-(?P<line_end>\d+))?)?"
)

#: Chars that, immediately before a match, mean it isn't a standalone
#: path reference: part of a URL (``//``), of a longer path (``/``),
#: or of a ``host:path`` form (``:``, ``@``).
_PRECEDING_REJECT = frozenset("/:@")

#: Scheme prefixes anywhere in the 12 chars before a match mean it is
#: a URL tail, not a repo path.
_URL_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9+.\-]*://\S*$")

#: A question naming nothing but unreachable paths is the wrong-roots
#: signature. One unreachable path among several readable ones is far
#: more likely to be a typo or a file the asker expects to be created,
#: so a mixed result warns rather than blocks. See :meth:`Preflight
#: .blocking`.
_ALL_UNREACHABLE = 1.0


@dataclass(frozen=True)
class PathRef:
    """One path the question named, and whether the run can read it."""
    raw: str            # exactly as it appeared, e.g. "eval/x.py:600"
    path: str           # the path part alone, e.g. "eval/x.py"
    status: str         # "ok" | "creatable" | "unreachable"
    resolved: Optional[str] = None   # absolute path, when status == ok


@dataclass(frozen=True)
class Preflight:
    """The verdict. ``refs`` is in document order, de-duplicated."""
    refs: list[PathRef] = field(default_factory=list)
    roots: list[str] = field(default_factory=list)

    @property
    def ok(self) -> list[PathRef]:
        return [r for r in self.refs if r.status == "ok"]

    @property
    def creatable(self) -> list[PathRef]:
        return [r for r in self.refs if r.status == "creatable"]

    @property
    def unreachable(self) -> list[PathRef]:
        return [r for r in self.refs if r.status == "unreachable"]

    @property
    def blocking(self) -> bool:
        """True when the question named paths and **none** of them are
        readable — the wrong-roots signature.

        Requires at least one unreachable ref, so a question that
        names no paths at all (the common case) never blocks.
        """
        if not self.refs or not self.unreachable:
            return False
        readable = len(self.ok) + len(self.creatable)
        if readable:
            return False
        return len(self.unreachable) / len(self.refs) >= _ALL_UNREACHABLE

    def message(self, *, display_roots: Optional[Sequence[str]] = None,
                ) -> str:
        """The refusal text. Names the paths, the roots that were
        actually searched, and the fix — in that order, because that
        is the order the operator needs them."""
        shown = list(display_roots or self.roots)
        paths = ", ".join(r.path for r in self.unreachable)
        roots_txt = ("\n".join(f"  - {r}" for r in shown)
                     or "  (none)")
        return (
            f"pre-flight refused: none of the {len(self.refs)} file(s) "
            f"this question names are readable under the session's "
            f"allowed roots.\n"
            f"Unreachable: {paths}\n"
            f"Roots searched:\n{roots_txt}\n"
            f"Nothing was spent. Re-run with the right roots — put the "
            f"subject repo in --cwd and pass secondary trees with "
            f"--add-dir."
        )

    def warning(self) -> Optional[str]:
        """Non-blocking note for the mixed case, or ``None``."""
        if self.blocking or not self.unreachable:
            return None
        paths = ", ".join(r.path for r in self.unreachable)
        return (
            f"{len(self.unreachable)} of {len(self.refs)} path(s) named "
            f"in the question are unreachable under the session's roots "
            f"({paths}); continuing because others resolved. Citations "
            f"to those paths will come back unverified."
        )


def extract_paths(text: str) -> list[tuple[str, str]]:
    """Return ``(raw_match, path)`` pairs in document order, deduped
    on the path. URLs and path fragments are filtered out."""
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for m in _PATH_RE.finditer(text or ""):
        start = m.start()
        if start and text[start - 1] in _PRECEDING_REJECT:
            continue
        if _URL_RE.search(text[max(0, start - 12):start]):
            continue
        path = m.group("path")
        if path in seen:
            continue
        seen.add(path)
        out.append((m.group(0), path))
    return out


def _under_a_root(p: Path, roots: Sequence[str]) -> bool:
    """True when ``p`` sits inside one of ``roots``.

    Both sides are realpath'd: roots arrive canonicalised, while a
    question is written with whatever the operator types
    (``/shared/dev/x`` for a symlink to ``/srv/…/dev/x``), and a
    string compare would call the same directory two places.
    """
    try:
        real = os.path.realpath(str(p))
    except OSError:  # pragma: no cover — defensive
        return False
    for root in roots:
        if not root:
            continue
        try:
            rroot = os.path.realpath(root)
        except OSError:  # pragma: no cover — defensive
            continue
        if real == rroot or real.startswith(rroot + os.sep):
            return True
    return False


def _resolve(path: str, roots: Sequence[str]) -> Optional[str]:
    """First root under which ``path`` is an existing file, or None.

    Relative paths follow ``citation_linter._resolve_path`` exactly —
    tried under each root in order, first hit wins — because the
    pre-flight's job is to predict the linter's later verdict.

    Absolute paths are handled more strictly than the linter, which
    accepts any absolute path that exists. Here it must also sit under
    an allowed root: a file the sandbox will refuse to open is not
    readable *by this run*, and saying otherwise would let through the
    exact situation the check exists to catch.
    """
    p = Path(path)
    if p.is_absolute():
        if p.is_file() and _under_a_root(p, roots):
            return str(p)
        return None
    for root in roots:
        if not root:
            continue
        candidate = Path(root) / path
        if candidate.is_file():
            return str(candidate)
    return None


def _parent_exists(path: str, roots: Sequence[str]) -> bool:
    parent = os.path.dirname(path)
    if not parent:
        # A bare ``foo.py`` can't reach here (the regex demands a
        # directory segment), but be safe: no directory to check
        # means we can't claim it's creatable.
        return False
    p = Path(parent)
    if p.is_absolute():
        return p.is_dir() and _under_a_root(p, roots)
    return any(
        (Path(root) / parent).is_dir() for root in roots if root
    )


def check_question_paths(question: str, *,
                         roots: Sequence[str]) -> Preflight:
    """Classify every path the question names against ``roots``."""
    root_list = [r for r in roots if r]
    refs: list[PathRef] = []
    for raw, path in extract_paths(question):
        resolved = _resolve(path, root_list)
        if resolved is not None:
            refs.append(PathRef(raw=raw, path=path, status="ok",
                                resolved=resolved))
        elif _parent_exists(path, root_list):
            refs.append(PathRef(raw=raw, path=path, status="creatable"))
        else:
            refs.append(PathRef(raw=raw, path=path, status="unreachable"))
    return Preflight(refs=refs, roots=root_list)


__all__ = [
    "PathRef",
    "Preflight",
    "check_question_paths",
    "extract_paths",
]
