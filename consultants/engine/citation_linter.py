"""Post-synthesis citation linter for the consultants engine.

After the synthesizer produces the final answer, this module scans
the text for ``path:line`` (and ``path:lineA-lineB``) patterns and
verifies each against the actual filesystem within the session's
allowed_roots. Fabricated and misplaced cites are annotated
inline (``path:line [unverified — <reason>]`` or
``path:line [in <actual_symbol>, not <claimed_symbol>]``) so the
user sees them rather than silently consuming them as truth.

The 2026-05-18 M14 first-real-ask re-run
(:file:`benchmarks/consultants/results/2026-05-18/m14-first-real-ask/rerun-report.md`)
caught two distinct fabrication modes that this linter neutralizes:

1. **Fake filename** — ``consultants/engine/store_sql.py`` cited
   in the final answer, but no such file exists anywhere in the
   repo. The researcher (``glm-5.1:cloud`` lane 5) invented it
   and the synthesizer relayed it forward.
2. **Wrong line within a real file** — ``store_reaper.py:128``
   cited for ``_distill_group``, but that function actually
   lives at line ``360``. The synthesizer interpolated a
   plausible-looking line number that doesn't exist in the
   right context.

Detection rules:

* path-not-found → replace cite with ``path:line [unverified —
  file not found]``.
* line beyond EOF → replace with ``path:line [unverified — file
  has N lines]``.
* AST symbol mismatch (Python files only) — when the answer
  mentions a symbol name within ``_SYMBOL_PROXIMITY_CHARS``
  characters before the cite, and the stdlib :mod:`ast` parse
  shows the cited line falls inside a different (or no)
  function/class, annotate ``path:line [in <actual>, not
  <claimed>]``. Catches the round-2 fabrication mode that the
  bounds check alone misses.
* path + line + AST all agree → leave the cite unchanged.

The linter is intentionally **non-blocking**: it never rejects
or rewrites the answer's prose, only annotates citations. The
user sees the synthesizer's reasoning AND the linter's verdict
on each cite side-by-side and can judge. Non-Python files (no
parse possible with stdlib ast) skip the symbol-mismatch check
and only get path + bounds verification — that's where the
on-disk code_graph would help if it ever covers consultants/.

Public API:

* :func:`extract_citations` — pure regex extraction, useful
  for tests + the validation script.
* :func:`verify_citation` — single-cite filesystem check,
  returns a :class:`CitationIssue` (or None when verified).
* :func:`lint_answer` — full pipeline: extract → verify →
  rewrite. Returns the annotated answer + a list of issues
  for logging.
"""

from __future__ import annotations

import ast
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Sequence

# How far back from a cite to scan the answer for a "claimed symbol"
# referenced in backticks. 80 chars covers natural-language patterns
# like "The reaper calls `_distill_group` (`path:line`)" or "the call
# to `_distill_group` at `path:line`". Tuning ceiling: too large and
# we hit unrelated symbols mentioned earlier in the same paragraph;
# too small and we miss the "(`path:line`)" pattern with a 30-char
# explanatory phrase between the symbol and the cite.
_SYMBOL_PROXIMITY_CHARS = 80

# Regex for backtick-wrapped identifiers within the proximity window.
# Matches `_distill_group`, `StoreReaperThread`, `write_distilled_summary`,
# `Class.method`, etc. Stops at non-identifier chars (so `_distill_group()`
# captures the bare identifier).
_BACKTICK_SYMBOL_RE = re.compile(
    r"`(?P<symbol>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)"
    r"(?:\([^`]*\))?`"
)

# Symbols never worth treating as "claimed function/class at this cite".
# Includes Python literals (None / True / False), keywords (return, raise,
# if, ...), builtin names (int, str, list, dict, ...), and short
# uninformative tokens. Filtering here means the symbol-mismatch
# annotation only fires when the synthesizer actually claimed a
# function/class lives at the cited line, not when it mentioned a
# language primitive nearby.
_CLAIMED_SYMBOL_DENYLIST = frozenset({
    # Literals / keywords
    "None", "True", "False",
    "and", "or", "not", "is", "in",
    "if", "elif", "else", "for", "while", "break", "continue",
    "return", "yield", "raise", "try", "except", "finally",
    "with", "as", "from", "import", "pass", "lambda", "global",
    "nonlocal", "del", "assert", "class", "def",
    # Common builtins / type names mentioned in prose
    "int", "str", "float", "bool", "bytes", "bytearray",
    "list", "tuple", "dict", "set", "frozenset",
    "None", "type", "object", "any", "all",
    "len", "range", "open", "print", "id", "hash",
    # Generic placeholders the synthesizer reaches for
    "self", "cls", "args", "kwargs", "Exception", "BaseException",
    "Error", "Warning", "Type", "Union", "Optional", "Any",
    "Callable", "Iterable", "Iterator", "Sequence", "Mapping",
})

# Patterns that look like an exception/error class name (suffix
# matches). When the closest claimed symbol matches this shape AND
# the surrounding text strongly suggests the cite refers to where
# the exception is RAISED or CAUGHT (not where the class is
# defined), skip the mismatch check — the cite is about a try/except
# region, not the exception class itself.
_EXCEPTION_NAME_SUFFIXES = ("Error", "Exception", "Failed", "Warning")

log = logging.getLogger("consultants.engine.citation_linter")


# ---------------------------------------------------------------- #
# Citation regex.
#
# Matches:
#   foo.py:123
#   path/to/foo.py:123
#   path/to/foo.py:123-145
#   src/x/y/file.ts:42
#   consultants/engine/store_reaper.py:128
#
# Excludes:
#   - URLs (https://github.com/.../blob/main/foo.py:123) — the
#     `://` separator makes them unappetizing to match and they're
#     usually intentional anchors anyway.
#   - Backtick-wrapped patterns are NOT excluded — the synthesizer
#     uses backticks for inline code, and a backtick-wrapped cite
#     still needs verification.
#
# Path component: one or more path segments separated by ``/``,
# each segment a non-empty run of word chars, dots, dashes, and
# underscores. Final segment ends with an extension (``.\w+``).
# Line: 1+ digits, optionally a ``-`` + 1+ digits range.
# ---------------------------------------------------------------- #

_PATH_SEGMENT = r"[A-Za-z0-9_][A-Za-z0-9_.\-]*"
_PATH_RE = (
    r"(?<![/:])"                       # not after `/` or `:` (avoid URLs/paths-of-paths)
    r"(?P<path>"
    rf"(?:{_PATH_SEGMENT}/)+"          # at least one directory segment
    rf"{_PATH_SEGMENT}\.\w+"           # final segment with extension
    r")"
    r":"
    r"(?P<line_start>\d+)"
    r"(?:-(?P<line_end>\d+))?"
)
CITATION_RE = re.compile(_PATH_RE)


@dataclass(frozen=True)
class CitationIssue:
    """One unverified citation found in the answer.

    ``original_match`` is the substring exactly as it appeared in
    the answer (so the rewriter can locate it for replacement).
    ``replacement`` is the annotated form to substitute in.
    ``reason`` is the human-readable explanation suitable for an
    operator log line.
    """
    original_match: str
    replacement: str
    reason: str
    path: str
    line_start: int
    line_end: Optional[int]


# ---------------------------------------------------------------- #
# Public API.
# ---------------------------------------------------------------- #

def extract_citations(
    text: str,
) -> list[tuple[str, str, int, Optional[int]]]:
    """Pure regex extraction of ``path:line[-line]`` citations.

    Returns tuples of ``(matched_substring, path, line_start, line_end)``
    in document order. ``line_end`` is ``None`` for single-line
    cites. The matched substring is exactly what appeared in the
    answer, useful as the replacement key.
    """
    out: list[tuple[str, str, int, Optional[int]]] = []
    for m in CITATION_RE.finditer(text):
        path = m.group("path")
        line_start = int(m.group("line_start"))
        line_end_str = m.group("line_end")
        line_end = int(line_end_str) if line_end_str else None
        out.append((m.group(0), path, line_start, line_end))
    return out


def verify_citation(
    path: str,
    line_start: int,
    line_end: Optional[int],
    *,
    allowed_roots: Sequence[str],
) -> Optional[CitationIssue]:
    """Verify a single citation against the filesystem.

    Tries each allowed root as a base prefix; the first root that
    yields a readable file wins. ``line_start`` must be ≤ the
    file's line count; when a range is given, ``line_end`` must
    also be ≤ the line count.

    Returns ``None`` when the citation verifies. Returns a
    :class:`CitationIssue` with a ready-to-substitute
    ``replacement`` string when it does not.

    The pre-call full match string is reconstructed here only for
    the issue's ``original_match`` field; the caller is expected
    to use the exact match span when doing the actual rewrite.
    """
    line_repr = (
        f"{line_start}-{line_end}" if line_end is not None
        else str(line_start)
    )
    full_cite = f"{path}:{line_repr}"

    resolved = _resolve_path(path, allowed_roots)
    if resolved is None:
        return CitationIssue(
            original_match=full_cite,
            replacement=f"{full_cite} [unverified — file not found]",
            reason="file not found in any allowed_root",
            path=path,
            line_start=line_start,
            line_end=line_end,
        )

    try:
        file_lines = _count_lines(resolved)
    except OSError as e:
        log.debug(
            "citation_linter: cannot count lines in %s: %s",
            resolved, e,
        )
        # Pessimistic: treat as unverified rather than silently OK.
        return CitationIssue(
            original_match=full_cite,
            replacement=f"{full_cite} [unverified — cannot read file]",
            reason=f"cannot count lines: {e}",
            path=path,
            line_start=line_start,
            line_end=line_end,
        )

    upper = line_end if line_end is not None else line_start
    if upper > file_lines:
        return CitationIssue(
            original_match=full_cite,
            replacement=(
                f"{full_cite} [unverified — file has {file_lines} lines]"
            ),
            reason=f"line {upper} > file_lines={file_lines}",
            path=path,
            line_start=line_start,
            line_end=line_end,
        )
    if line_start < 1:
        return CitationIssue(
            original_match=full_cite,
            replacement=f"{full_cite} [unverified — line < 1]",
            reason=f"line_start={line_start} below 1",
            path=path,
            line_start=line_start,
            line_end=line_end,
        )
    if line_end is not None and line_end < line_start:
        return CitationIssue(
            original_match=full_cite,
            replacement=(
                f"{full_cite} [unverified — range end before start]"
            ),
            reason=f"line_end={line_end} < line_start={line_start}",
            path=path,
            line_start=line_start,
            line_end=line_end,
        )
    return None


def lint_answer(
    answer_text: str,
    *,
    allowed_roots: Sequence[str],
) -> tuple[str, list[CitationIssue]]:
    """Run the full lint pipeline on an answer string.

    Returns ``(annotated_answer, issues)``. When no fabrications
    are found, ``annotated_answer == answer_text`` and
    ``issues == []`` — the linter is a no-op on clean answers.

    Idempotent on annotated text: an already-annotated cite of the
    form ``path:line [unverified — …]`` or ``[in X, not Y]`` is
    detected via substring check and skipped on subsequent passes.

    Three verification layers per cite, in order:

    1. **Path resolution** — does ``path`` exist under any of
       ``allowed_roots``? Miss → ``[unverified — file not found]``.
    2. **Line bounds** — is ``line`` within the file's line count?
       Beyond EOF → ``[unverified — file has N lines]``.
    3. **AST symbol match** (Python files only) — when the answer
       mentions a backticked symbol within
       ``_SYMBOL_PROXIMITY_CHARS`` characters BEFORE the cite, and
       the stdlib :mod:`ast` walk shows the cited line is inside a
       different function/class (or no function at all), annotate
       ``[in <actual>, not <claimed>]``. Catches the round-2
       fabrication mode the bounds check alone misses (e.g. the
       synthesizer claims ``_distill_group`` at line 128, but
       line 128 is actually in ``_question_hint_for_group``).
    """
    if not answer_text or not allowed_roots:
        return answer_text, []

    citations = extract_citations(answer_text)
    if not citations:
        return answer_text, []

    # Track per-match-text replacement: the first verification result
    # for an identical ``foo.py:123`` substring wins, because
    # ``str.replace`` will apply to every occurrence anyway. For
    # the symbol-mismatch check we need the SURROUNDING TEXT, which
    # differs across occurrences; so we resolve symbol context for
    # each unique (match, span_start) pair and apply the strictest.
    issues: list[CitationIssue] = []
    annotated_for_match: dict[str, str] = {}

    # Re-scan with positions so the symbol-proximity check can see
    # the context of each cite.
    for m in CITATION_RE.finditer(answer_text):
        full_match = m.group(0)
        path = m.group("path")
        line_start = int(m.group("line_start"))
        line_end_str = m.group("line_end")
        line_end = int(line_end_str) if line_end_str else None
        span_start = m.start()

        # Skip if this exact span is already annotated downstream
        # (idempotency on a re-linted answer).
        if _already_annotated_at(answer_text, m.end()):
            continue

        # Layer 1 + 2: filesystem + bounds.
        issue = verify_citation(
            path, line_start, line_end, allowed_roots=allowed_roots,
        )
        if issue is None:
            # Layer 3: AST symbol match for in-bounds Python cites.
            issue = _verify_symbol_match(
                answer_text, span_start, path, line_start, line_end,
                allowed_roots=allowed_roots,
            )
        if issue is None:
            continue
        # Same full_match seen twice with different proximity
        # contexts: prefer the strictest annotation. A failed
        # symbol check is more informative than a clean bounds
        # check, so once we have an issue, we keep it.
        annotated_for_match.setdefault(full_match, issue.replacement)
        issues.append(issue)

    annotated = answer_text
    for original, replacement in annotated_for_match.items():
        annotated = annotated.replace(original, replacement)
    return annotated, issues


def _already_annotated_at(text: str, idx: int) -> bool:
    """True iff a ``[unverified`` or ``[in `` marker immediately
    follows position ``idx`` (the linter's own previous annotation).
    Used to make :func:`lint_answer` idempotent on re-runs.
    """
    suffix = text[idx:idx + 16]
    return suffix.startswith(" [unverified") or suffix.startswith(" [in ")


def _verify_symbol_match(
    answer_text: str,
    cite_start: int,
    path: str,
    line_start: int,
    line_end: Optional[int],
    *,
    allowed_roots: Sequence[str],
) -> Optional[CitationIssue]:
    """Cross-check the cite's claimed symbol against AST ground truth.

    Only runs on ``.py`` files (stdlib :mod:`ast` parses them
    directly; non-Python files require a different parser and are
    skipped — those cites pass through with only path + bounds
    verification).

    Looks back ``_SYMBOL_PROXIMITY_CHARS`` characters from the cite
    for a backtick-wrapped identifier. If found, that's the
    "claimed symbol". Parses the cited file's AST and walks for
    function/class defs; the innermost def whose
    ``[lineno, end_lineno]`` range contains ``line_start`` is the
    "actual symbol". Mismatch → annotate; match → no issue.

    No-claimed-symbol (cite stands alone) → no annotation either —
    we don't insert "in X" for clean cites because that's noise.
    """
    if not path.endswith(".py"):
        return None

    claimed = _extract_claimed_symbol(answer_text, cite_start)
    if claimed is None:
        return None

    resolved = _resolve_path(path, allowed_roots)
    if resolved is None:
        return None  # already covered by verify_citation

    actual = _enclosing_symbol_at_line(resolved, line_start)

    # Compare bare leaf names (drop class-qualifier dots so
    # ``StoreReaper.sweep_once`` matches the AST node named
    # ``sweep_once`` inside class ``StoreReaper``).
    claimed_leaf = claimed.rsplit(".", 1)[-1]

    line_repr = (
        f"{line_start}-{line_end}" if line_end is not None
        else str(line_start)
    )
    full_cite = f"{path}:{line_repr}"

    if actual is None:
        return CitationIssue(
            original_match=full_cite,
            replacement=(
                f"{full_cite} [in <module scope>, "
                f"not {claimed_leaf}]"
            ),
            reason=(
                f"line {line_start} is not inside any function/class; "
                f"answer claims {claimed_leaf}"
            ),
            path=path,
            line_start=line_start,
            line_end=line_end,
        )
    if actual != claimed_leaf:
        return CitationIssue(
            original_match=full_cite,
            replacement=(
                f"{full_cite} [in {actual}, not {claimed_leaf}]"
            ),
            reason=(
                f"line {line_start} is inside {actual}; "
                f"answer claims {claimed_leaf}"
            ),
            path=path,
            line_start=line_start,
            line_end=line_end,
        )
    return None


def _extract_claimed_symbol(
    answer_text: str, cite_start: int,
) -> Optional[str]:
    """Return the last meaningful backtick-wrapped identifier in the
    ``_SYMBOL_PROXIMITY_CHARS``-char window before ``cite_start``,
    or ``None`` when no symbol is visible / all candidates are
    denylisted.

    "Meaningful" = not a path-shaped identifier (``foo/bar.py``),
    not a Python literal / keyword / common builtin (see
    :data:`_CLAIMED_SYMBOL_DENYLIST`), and not an exception class
    name (suffix matches :data:`_EXCEPTION_NAME_SUFFIXES` — those
    refer to a raised/caught exception, not the function the cite
    is supposed to point at).

    "Last" choice means closest-to-cite wins, which matches natural
    prose like "the reaper calls ``_distill_group``
    (``path:line``)".
    """
    window_start = max(0, cite_start - _SYMBOL_PROXIMITY_CHARS)
    window = answer_text[window_start:cite_start]
    last_match: Optional[str] = None
    for m in _BACKTICK_SYMBOL_RE.finditer(window):
        sym = m.group("symbol")
        leaf = sym.rsplit(".", 1)[-1]
        # Filter path-shaped matches (the citation's own path, or a
        # filename mentioned nearby).
        if "/" in sym or sym.endswith(".py") or sym.endswith(".js"):
            continue
        # Filter Python literals / keywords / common builtins.
        if leaf in _CLAIMED_SYMBOL_DENYLIST:
            continue
        # Filter exception-class names — the cite likely refers to
        # where the exception is raised/caught, not where the class
        # is defined.
        if any(leaf.endswith(sfx) for sfx in _EXCEPTION_NAME_SUFFIXES):
            continue
        last_match = sym
    return last_match


def _enclosing_symbol_at_line(
    file_path: Path, line: int,
) -> Optional[str]:
    """Return the name of the innermost function/class whose AST
    span contains ``line``, or ``None`` when the line is in
    module scope (imports / module-level statements / comments
    between defs) or the file fails to parse.
    """
    try:
        source = file_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        log.debug("citation_linter: read failed %s: %s", file_path, e)
        return None
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        log.debug("citation_linter: parse failed %s: %s", file_path, e)
        return None

    best_name: Optional[str] = None
    best_span: int = 10**9  # smaller = tighter = innermost
    for node in ast.walk(tree):
        if not isinstance(
            node,
            (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
        ):
            continue
        start = node.lineno
        end = getattr(node, "end_lineno", None) or start
        if not (start <= line <= end):
            continue
        span = end - start
        if span < best_span:
            best_span = span
            best_name = node.name
    return best_name


# ---------------------------------------------------------------- #
# Private helpers.
# ---------------------------------------------------------------- #

def _resolve_path(
    path: str,
    allowed_roots: Sequence[str],
) -> Optional[Path]:
    """Try to find ``path`` under any of ``allowed_roots``.

    Absolute paths bypass roots and are verified directly. Relative
    paths are tried under each root in order; first hit wins.
    Returns the resolved :class:`Path` on success, ``None`` on
    miss. Symlinks are NOT followed for the existence check — we
    care whether the path literal points at *something* readable,
    not what it resolves to underneath.
    """
    p = Path(path)
    if p.is_absolute():
        return p if p.is_file() else None
    for root in allowed_roots:
        if not root:
            continue
        candidate = Path(root) / path
        if candidate.is_file():
            return candidate
    return None


def _count_lines(path: Path) -> int:
    """Count newline-terminated lines in a text file.

    Reads in 64 KB chunks for predictable memory on large files
    (e.g. a 5 MB transcript wouldn't fit in stride at once). A
    file that doesn't end in a newline still counts its final
    line.
    """
    count = 0
    last_byte: int = 0
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(65536)
            if not chunk:
                break
            count += chunk.count(b"\n")
            last_byte = chunk[-1]
    if last_byte != 0 and last_byte != ord(b"\n"):
        count += 1  # final line without trailing newline
    return count


__all__ = [
    "CitationIssue",
    "CITATION_RE",
    "extract_citations",
    "verify_citation",
    "lint_answer",
]
