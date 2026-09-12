"""Git history tools — "when did this regress, and why was it changed?"

M-C of ``docs/PLAN-council-tool-surface.md``. The council could read the
working tree but had no access to its history, so questions of the form
*when did this break* were structurally unanswerable: ``read_file``
shows what the code is, never what it was.

Read-only by construction, and not merely by intent. Every call is a
fixed ``argv`` — never a shell string — built from an allowlist of
observation-only subcommands. There is no code path here that can reach
``commit``, ``checkout``, ``gc`` or anything else that writes, so the
"read-only shell is undecidable from a command string" problem that
makes M-D hard does not apply: nothing is parsed from model output into
a command.

The composed tool is the point
==============================

``git_log`` / ``git_blame`` / ``git_diff`` / ``git_show`` are the
primitives, but a model answering "when did this regress" with them
takes four or five round-trips and usually picks wrong. ``git_history``
wraps ``git log -L`` — git's own line-history walk — which answers the
question directly: give it a file and optionally a symbol and it returns
the commits that touched *those lines*, newest first, with messages.
"""

from __future__ import annotations

import logging
import os
import subprocess
from typing import Optional

from claude_hooks.tool_registry.base import ToolProvider
from claude_hooks.tool_registry.policy import AUTO

log = logging.getLogger("claude_hooks.tool_registry.git")

#: Subcommands this provider will run. Observation only.
_ALLOWED_SUBCOMMANDS = frozenset({
    "log", "blame", "diff", "show", "rev-parse",
})

#: Hard ceiling on returned bytes. ``git log -p`` over a long-lived file
#: can be megabytes, and an unbounded tool result would blow the model's
#: context far more effectively than it would answer the question.
MAX_OUTPUT_CHARS = 20_000

#: Per-invocation wall clock. ``git log -L`` on a large repo is the slow
#: case; beyond this the answer is not worth the stall.
TIMEOUT_SECONDS = 30

_TRUNC = "\n…(truncated — narrow the range or the path)"


# ====================================================================== #
# Helpers
# ====================================================================== #
def _resolve_in_roots(path: str, cwd: str,
                      extra_roots: tuple[str, ...]) -> Optional[str]:
    """Resolve ``path`` and confirm it sits inside an allowed root.

    Mirrors the multi-root sandbox the path tools already enforce — a
    git provider that could blame ``/etc/shadow``'s repo would be a side
    door around it.
    """
    roots = [os.path.realpath(r) for r in (cwd, *extra_roots) if r]
    candidate = os.path.realpath(
        path if os.path.isabs(path) else os.path.join(cwd, path))
    for root in roots:
        if candidate == root or candidate.startswith(root + os.sep):
            return candidate
    return None


def _run_git(args: list[str], cwd: str) -> str:
    sub = args[0] if args else ""
    if sub not in _ALLOWED_SUBCOMMANDS:
        # Unreachable from the public tools below; this guards against a
        # future edit adding a subcommand without thinking about it.
        return f"error: git subcommand {sub!r} is not permitted"
    try:
        proc = subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True, text=True,
            # git emits UTF-8 regardless of platform, but ``text=True``
            # decodes with the locale codepage — cp1252 on a stock
            # Windows box, which dies on the first em dash in a commit
            # message. Pin the encoding, and never let a decode error
            # take down a read-only history query: a mojibake byte in
            # one author name is not worth losing the whole log.
            encoding="utf-8", errors="replace",
            timeout=TIMEOUT_SECONDS, check=False,
        )
    except FileNotFoundError:
        return "error: git is not installed or not on PATH"
    except subprocess.TimeoutExpired:
        return (f"error: git {sub} timed out after {TIMEOUT_SECONDS}s — "
                f"narrow the path or the revision range")
    except Exception as e:  # pragma: no cover - defensive
        return f"error: git {sub} failed: {e}"

    if proc.returncode != 0:
        err = (proc.stderr or "").strip() or f"exit {proc.returncode}"
        if "not a git repository" in err.lower():
            return ("error: not a git repository — this project has no "
                    "history to inspect")
        return f"error: git {sub}: {err}"

    out = proc.stdout or ""
    if len(out) > MAX_OUTPUT_CHARS:
        return out[:MAX_OUTPUT_CHARS] + _TRUNC
    return out or f"(git {sub} produced no output)"


def _arg(args: dict, key: str, default: str = "") -> str:
    v = args.get(key, default)
    return v.strip() if isinstance(v, str) else default


def _int_arg(args: dict, key: str, default: int, *, cap: int) -> int:
    try:
        return max(1, min(cap, int(args.get(key, default))))
    except (TypeError, ValueError):
        return default


def _rev_ok(rev: str) -> bool:
    """Reject anything that could be read as an option.

    Revisions go into ``argv`` so there is no shell to inject into, but
    a value like ``--output=/etc/x`` would still be read by git as a
    flag. Refusing leading dashes closes that.
    """
    return bool(rev) and not rev.startswith("-")


# ====================================================================== #
# Provider
# ====================================================================== #
class GitToolProvider(ToolProvider):
    """History access, confined to the same roots as the path tools."""

    name = "git"
    prefix = ""

    def __init__(self, extra_roots: tuple[str, ...] = ()):
        self.extra_roots = tuple(r for r in extra_roots if r)

    # ------------------------------------------------------------------ #
    def default_level(self, tool: str) -> str:
        return AUTO

    def is_read_only(self, tool: str) -> bool:
        return True

    def taints(self) -> bool:
        return False

    # ------------------------------------------------------------------ #
    def execute(self, tool: str, raw_args: str, cwd: str) -> str:
        import json
        try:
            args = json.loads(raw_args) if raw_args else {}
        except json.JSONDecodeError as e:
            return f"error: tool arguments not valid JSON: {e}"
        if not isinstance(args, dict):
            return "error: tool arguments must be a JSON object"

        impl = {
            "git_log": self._log,
            "git_blame": self._blame,
            "git_diff": self._diff,
            "git_show": self._show,
            "git_history": self._history,
        }.get(tool)
        if impl is None:
            return f"error: unknown tool {tool!r}"
        try:
            return impl(args, cwd)
        except Exception as e:  # pragma: no cover - defensive
            log.warning("git tool %s raised: %s", tool, e)
            return f"error: tool raised: {e}"

    # ------------------------------------------------------------------ #
    def _path_or_error(self, args: dict, cwd: str, *,
                       key: str = "path") -> tuple[Optional[str], str]:
        raw = _arg(args, key)
        if not raw:
            return None, f"error: {key} is required"
        resolved = _resolve_in_roots(raw, cwd, self.extra_roots)
        if resolved is None:
            return None, (f"error: {raw!r} is outside the allowed roots")
        return os.path.relpath(resolved, os.path.realpath(cwd)), ""

    def _log(self, args: dict, cwd: str) -> str:
        n = _int_arg(args, "max_count", 20, cap=200)
        argv = ["log", f"-n{n}", "--date=short",
                "--pretty=format:%h %ad %an — %s"]
        if args.get("path"):
            rel, err = self._path_or_error(args, cwd)
            if err:
                return err
            argv += ["--follow", "--", rel]
        return _run_git(argv, cwd)

    def _blame(self, args: dict, cwd: str) -> str:
        rel, err = self._path_or_error(args, cwd)
        if err:
            return err
        argv = ["blame", "--date=short", "-w"]
        start, end = args.get("start_line"), args.get("end_line")
        if start:
            try:
                lo = max(1, int(start))
                hi = max(lo, int(end)) if end else lo + 40
                argv += ["-L", f"{lo},{hi}"]
            except (TypeError, ValueError):
                return "error: start_line/end_line must be integers"
        argv += ["--", rel]
        return _run_git(argv, cwd)

    def _diff(self, args: dict, cwd: str) -> str:
        argv = ["diff", "--stat" if args.get("stat_only") else "--patch"]
        for key in ("from_rev", "to_rev"):
            rev = _arg(args, key)
            if rev:
                if not _rev_ok(rev):
                    return f"error: invalid revision {rev!r}"
                argv.append(rev)
        if args.get("path"):
            rel, err = self._path_or_error(args, cwd)
            if err:
                return err
            argv += ["--", rel]
        return _run_git(argv, cwd)

    def _show(self, args: dict, cwd: str) -> str:
        rev = _arg(args, "rev")
        if not _rev_ok(rev):
            return "error: rev is required and must not start with '-'"
        return _run_git(["show", "--stat", "--patch", rev], cwd)

    def _history(self, args: dict, cwd: str) -> str:
        """``git log -L`` — the "when did this change" answer.

        With a symbol, git's ``:funcname:file`` form walks the history
        of that function specifically. Without one, a line range. Both
        return the commits that touched *those lines*, which is the
        actual question, rather than every commit that touched the file.
        """
        rel, err = self._path_or_error(args, cwd)
        if err:
            return err
        n = _int_arg(args, "max_count", 10, cap=50)
        symbol = _arg(args, "symbol")
        if symbol:
            if ":" in symbol:
                return "error: symbol must not contain ':'"
            spec = f":{symbol}:{rel}"
        else:
            start = _int_arg(args, "start_line", 1, cap=1_000_000)
            end = _int_arg(args, "end_line", start + 40, cap=1_000_000)
            spec = f"{start},{max(start, end)}:{rel}"
        out = _run_git(["log", f"-n{n}", "-L", spec,
                        "--date=short"], cwd)
        if out.startswith("error:") and symbol:
            return (f"{out}\n(hint: -L :symbol: needs a function git can "
                    f"locate in {rel}; try start_line/end_line instead)")
        return out

    # ------------------------------------------------------------------ #
    def specs(self) -> list[dict]:
        def fn(name, desc, props, required=()):
            return {"type": "function", "function": {
                "name": name, "description": desc,
                "parameters": {"type": "object", "properties": props,
                               "required": list(required)}}}

        path_prop = {"type": "string",
                     "description": "Repo-relative file path."}
        return [
            fn("git_history",
               "Why/when did these lines change? Walks the history of a "
               "specific function or line range (git log -L) and returns "
               "the commits that touched them, newest first. Prefer this "
               "over git_log when investigating a regression.",
               {"path": path_prop,
                "symbol": {"type": "string", "description":
                           "Function/method name to follow. Omit to use "
                           "start_line/end_line."},
                "start_line": {"type": "integer"},
                "end_line": {"type": "integer"},
                "max_count": {"type": "integer",
                              "description": "Commits to return, default 10."}},
               ["path"]),
            fn("git_log",
               "Recent commits, newest first. Pass a path to follow one "
               "file's history across renames.",
               {"path": path_prop,
                "max_count": {"type": "integer",
                              "description": "Default 20, max 200."}}),
            fn("git_blame",
               "Who last changed each line, with commit and date. Pass a "
               "line range to keep the output small.",
               {"path": path_prop,
                "start_line": {"type": "integer"},
                "end_line": {"type": "integer"}},
               ["path"]),
            fn("git_diff",
               "Diff between two revisions (or the working tree when "
               "omitted). Use stat_only first to see which files moved.",
               {"from_rev": {"type": "string"},
                "to_rev": {"type": "string"},
                "path": path_prop,
                "stat_only": {"type": "boolean"}}),
            fn("git_show",
               "One commit in full: message, stat and patch.",
               {"rev": {"type": "string",
                        "description": "Commit-ish, e.g. a hash or tag."}},
               ["rev"]),
        ]


__all__ = ["GitToolProvider", "MAX_OUTPUT_CHARS", "TIMEOUT_SECONDS"]
