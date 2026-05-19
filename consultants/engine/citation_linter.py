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
  characters before the cite, and the cited line falls inside a
  different (or no) function/class, annotate ``path:line [in
  <actual>, not <claimed>]``. Catches the round-2 fabrication
  mode that the bounds check alone misses. The lookup prefers
  the on-disk code_graph artifact at ``graphify-out/graph.json``
  (mtime-cached in-process; 27× faster than stdlib :mod:`ast`
  parsing in the warm-cache case) and falls back to on-demand
  ``ast.parse`` when the graph is missing, predates
  ``EXTRACTOR_VERSION=2`` (no ``end_line`` field), or doesn't
  cover the cited file. See
  :mod:`claude_hooks.code_graph.enclosing` for the lookup API.
* path + line + AST all agree → leave the cite unchanged.

The linter is intentionally **non-blocking**: it never rejects
or rewrites the answer's prose, only annotates citations. The
user sees the synthesizer's reasoning AND the linter's verdict
on each cite side-by-side and can judge. Non-Python files (no
parse possible with stdlib ast and no code_graph extractor
today) skip the symbol-mismatch check and only get path +
bounds verification.

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
    """Cross-check the cite's claimed symbol against the actual file
    text at the cited line range.

    Only runs on ``.py`` files (extending to other languages is fine
    and trivial, but the claimed-symbol extractor today only
    recognises Python identifiers — and our high-fanout fab-test
    corpus is all Python).

    Looks back ``_SYMBOL_PROXIMITY_CHARS`` characters from the cite
    for a backtick-wrapped identifier — that is the "claimed
    symbol". Then reads the cited line range (with a small ±1 line
    margin to absorb off-by-one prose) and checks whether the
    claimed identifier appears as a word-boundary substring there.

    2026-05-18 (#205) — the symbol-match rule used to ask "is line N
    *inside the def of* the claimed symbol?" and flagged every cite
    where the enclosing function differed. That over-flags by ~95%
    on natural prose: "the reaper *calls* ``_distill_group`` at
    ``store_reaper.py:301``" puts the cite at the CALL site, whose
    enclosing function is ``sweep_once`` — but the line genuinely
    contains ``self._distill_group(…)`` and the model's claim is
    accurate.

    The current rule:
    * Symbol text appears at the cited line range (±1 margin) →
      cite is a valid reference / call / definition; no flag.
    * Symbol text does NOT appear at the cited line range → cite
      is unsupported by the file content at that location; flag
      with the enclosing-function context for the annotation
      message so the user knows where line N really lives.

    This keeps the genuine wrong-line catch (the gemma "_distill_group
    at line 128" forensic — line 128 has no ``_distill_group`` text
    anywhere in its source) while eliminating the call-site false
    positive class.

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

    # Compare bare leaf names (drop class-qualifier dots so
    # ``StoreReaper.sweep_once`` matches the bare ``sweep_once``
    # identifier in source text).
    claimed_leaf = claimed.rsplit(".", 1)[-1]

    # #205: primary check — does the symbol text actually appear at
    # the cited line range? Cheap (single file read), language-
    # agnostic, and accurate for the "X at file:N" prose pattern.
    if _symbol_appears_in_range(
        resolved, claimed_leaf, line_start, line_end,
    ):
        return None  # valid reference / call / definition

    # Symbol text NOT at cited line — genuine wrong-line claim.
    # Look up the enclosing function for the annotation message so
    # the user knows where line N really lives. Prefer the on-disk
    # code_graph (#200, O(1) mtime-cached) and fall back to ast.parse.
    actual = _enclosing_symbol_via_graph(
        path, line_start, allowed_roots=allowed_roots,
    )
    if actual is None:
        actual = _enclosing_symbol_at_line(resolved, line_start)

    line_repr = (
        f"{line_start}-{line_end}" if line_end is not None
        else str(line_start)
    )
    full_cite = f"{path}:{line_repr}"

    if actual is None:
        return CitationIssue(
            original_match=full_cite,
            replacement=(
                f"{full_cite} [no {claimed_leaf} at this line; "
                f"module scope]"
            ),
            reason=(
                f"line {line_start} does not contain {claimed_leaf}; "
                f"line is in module scope"
            ),
            path=path,
            line_start=line_start,
            line_end=line_end,
        )
    if actual != claimed_leaf:
        return CitationIssue(
            original_match=full_cite,
            replacement=(
                f"{full_cite} [no {claimed_leaf} at this line; "
                f"line is in {actual}]"
            ),
            reason=(
                f"line {line_start} does not contain {claimed_leaf}; "
                f"line is inside {actual}"
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


def _enclosing_symbol_via_graph(
    path: str,
    line: int,
    *,
    allowed_roots: Sequence[str],
) -> Optional[str]:
    """Try the on-disk code_graph for the enclosing symbol.

    Walks the allowed_roots in order — for each root, asks the graph
    "what symbol contains ``<path>:<line>``?" against that root's
    ``graphify-out/graph.json``. The first root whose graph both
    exists AND covers the file wins; returns ``None`` if no root
    has a usable graph entry for the file.

    Importantly, returns ``None`` for both:

    * "No graph available" (caller should fall back to ast.parse).
    * "Graph says line is in module scope" (caller should treat as
      the latter via the same ast.parse fallback — defensive in case
      the graph build is incomplete).

    Distinguishing the two would let us avoid the redundant ast
    parse when the graph is authoritative, but the safety margin is
    cheap: ast.parse on a single file is ~10 ms even for large
    modules, and only fires when the graph disagrees.
    """
    try:
        from claude_hooks.code_graph.enclosing import (
            enclosing_symbol_at, graph_covers_file,
        )
    except ImportError:
        return None
    p = Path(path)
    if p.is_absolute():
        # Absolute path in a cite: we can only map it to a relative
        # form against a root that contains it. The graph's lookup
        # keys are realpath-canonical relatives, so try each root.
        for root in allowed_roots:
            if not root:
                continue
            root_p = Path(root)
            try:
                rel = str(p.resolve().relative_to(root_p.resolve()))
            except ValueError:
                continue
            if graph_covers_file(root_p, rel):
                return enclosing_symbol_at(root_p, rel, line)
        return None
    for root in allowed_roots:
        if not root:
            continue
        root_p = Path(root)
        if not graph_covers_file(root_p, path):
            continue
        return enclosing_symbol_at(root_p, path, line)
    return None


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

# Per-process cache so a single lint pass that hits the same file
# repeatedly (typical: 5–30 cites pointing at one file) pays the
# file-read once. Keyed by absolute path + mtime so an edit during
# a long-running session invalidates cleanly.
_FILE_LINES_CACHE: dict[str, tuple[float, list[str]]] = {}


def _load_file_lines(path: Path) -> Optional[list[str]]:
    """Return file contents as a list of lines (1-indexed by adding
    ``[None] + lines`` later), mtime-cached. ``None`` on read error.
    """
    try:
        st = path.stat()
    except OSError:
        return None
    key = str(path)
    cached = _FILE_LINES_CACHE.get(key)
    if cached is not None and cached[0] == st.st_mtime:
        return cached[1]
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    lines = text.splitlines()
    _FILE_LINES_CACHE[key] = (st.st_mtime, lines)
    return lines


# Word-boundary regex cache so we compile each identifier once
# across a lint pass.
_SYMBOL_RE_CACHE: dict[str, "re.Pattern[str]"] = {}


def _symbol_appears_in_range(
    path: Path,
    symbol: str,
    line_start: int,
    line_end: Optional[int],
    *,
    slack: int = 1,
) -> bool:
    """True iff ``symbol`` appears as a word-boundary substring in
    the cited line range (with ±``slack`` extra lines on each side).

    The slack absorbs natural off-by-one prose — a model claiming
    "X at file:N" when X actually lives at N±1 (e.g. the decorator
    line vs the def line) shouldn't be flagged as fabricating.

    Word-boundary matching prevents false positives on substring
    collisions like ``_distill`` matching inside ``_distill_group``
    — though for our case (whole identifiers in code) this is more
    a safety net than a frequent concern.
    """
    if not symbol:
        return False
    lines = _load_file_lines(path)
    if not lines:
        return False
    # 1-index the line array for natural arithmetic; sentinel at [0]
    # so ``lines_1[N]`` returns line N.
    n = len(lines)
    lo = max(1, line_start - slack)
    hi = min(n, (line_end if line_end is not None else line_start) + slack)
    if lo > hi:
        return False
    pattern = _SYMBOL_RE_CACHE.get(symbol)
    if pattern is None:
        # Escape regex metachars in identifiers (unlikely but safe).
        pattern = re.compile(rf"\b{re.escape(symbol)}\b")
        _SYMBOL_RE_CACHE[symbol] = pattern
    for ln in range(lo, hi + 1):
        if pattern.search(lines[ln - 1]):
            return True
    return False


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
