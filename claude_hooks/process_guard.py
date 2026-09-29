"""Reject commands that kill or wait on themselves, and waiters that
cannot see a failure.

Two faults have recurred across every project for months, written down
as rules in memory, buglogs and cerebrum files dozens of times, and kept
recurring anyway — the rule is easy to state and easy to break:

1. **Self-match.** ``pkill -f PAT`` / ``pgrep -f PAT`` match the full
   command line of every process, and the command that runs them is
   itself a process whose command line contains PAT: the Bash tool runs
   everything as ``bash -c "... eval '<command>' && pwd -P ..."``, and
   ``ssh host 'CMD'`` runs ``bash -c 'CMD'`` on the remote side. So the
   kill takes down its own shell (exit 144 locally, 255 over ssh, the
   rest of the command never runs), and ``while pgrep -f PAT`` is always
   true, so the waiter never ends. The ``[x]yz`` bracket trick hides the
   pattern only while the bare name appears nowhere else in the same
   command — a heredoc, a ``nohup`` target, an ``mv`` path defeat it.

2. **Blind waiters.** A background waiter that exits only on success —
   ``until [ -e DONE ]``, ``until grep -q READY log``, ``until curl -sf``
   — never exits when the job dies, so nothing tells the session, and
   the operator finds out by asking why nothing happened.

This module decides both from the command text alone, precisely enough
to reject only real faults. It parses the command (:mod:`shell_ast`),
follows it into every context it creates — the local wrapper, the remote
``bash -c`` of each ``ssh``, nested ``bash -c``, heredocs fed to a
shell's stdin — and knows which command lines are alive in each. The
rules it relies on were measured (bash 5.1 locally, 5.2 on bs2):

* ``bash -c 'CMD'`` with a single simple command, or with a plain
  ``a; b`` list, execs the **last** command in place: the shell is gone
  by the time it runs, so ``ssh h 'cd /x; pkill -f foo'`` is safe.
  ``pkill -f foo; true``, ``&&``, pipelines, groups and anything after
  it keep the shell — and its command line — alive.
* The Bash tool's own wrapper always runs ``&& pwd -P`` after the
  command, so locally the carrier is always alive.
* A script sent on stdin (``ssh h bash -s <<'EOF'``) is in no argv.
* ``$(...)`` runs in a fork of the shell, with the shell's argv.

Anything it cannot parse, or a pattern it cannot resolve (``"$P"`` set
elsewhere), gets no opinion.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from claude_hooks.shell_ast import (
    AndOr, Case, For, FuncDef, Group, If, Loop, Pipeline, Seq, Simple, Word,
    try_parse,
)

# ------------------------------------------------------------------ #
# Findings
# ------------------------------------------------------------------ #


@dataclass
class Finding:
    rule: str           # self-kill | self-match | kills-ssh | blind-waiter
    where: str          # "locally" / "on bs2"
    command: str        # the offending command, as written
    detail: str
    fix: str

    def render(self) -> str:
        return (f"**{self.rule}** {self.where}: `{_clip(self.command)}`\n"
                f"{self.detail}\nFix: {self.fix}")


def _clip(s: str, n: int = 160) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[:n - 1] + "…"


# ------------------------------------------------------------------ #
# Processes alive while a command runs
# ------------------------------------------------------------------ #


@dataclass
class Carrier:
    """A process whose command line exists while commands in a context run."""
    cmdline: str
    comm: str
    label: str                  # how to name it in a message
    exec_away: bool = False     # the shell execs its tail command in place


@dataclass
class Ctx:
    where: str
    carriers: list
    remote: bool
    env: dict = field(default_factory=dict)
    shell: Optional[Carrier] = None     # the -c shell of this context
    tail: Optional[Simple] = None       # the command that shell execs
    stdin_script: Optional[str] = None  # what a shell here would read on stdin
    timeout_wrapped: bool = False
    depth: int = 0
    usage: dict = field(default_factory=dict)   # var -> "kill" when later killed
    own: Optional[Carrier] = None       # the process `$$` names here

    def alive_for(self, cmd: Optional[Simple], *, spare_own: bool = False) -> list:
        """Carriers alive while ``cmd`` runs (``None`` = inside a $() fork).
        ``spare_own`` drops the shell ``$$`` names, for a kill whose
        guard or filter excludes ``$$``."""
        out = []
        for c in self.carriers:
            if c is self.shell and cmd is not None and cmd is self.tail:
                continue
            if spare_own and c is self.own:
                continue
            out.append(c)
        return out

    def names_self(self, text: str) -> bool:
        """Does ``text`` refer to this shell's own PID?"""
        if "$$" in text or "BASHPID" in text:
            return True
        for k, v in self.env.items():
            if v == "$$" and re.search(r"\$\{?" + re.escape(k) + r"\b", text):
                return True
        return False


_WRAPPER_HEAD = ("/bin/bash -c source {home}/.claude/shell-snapshots/"
                 "snapshot-bash-0000000000000-xxxxxx.sh 2>/dev/null || true && "
                 "shopt -u extglob 2>/dev/null || true && { \\builtin unalias -- "
                 "'unsetenv'; \\builtin unset -f -- 'unsetenv'; } >/dev/null 2>&1 "
                 "|| true && eval '")
_WRAPPER_TAIL = "' && pwd -P >| /tmp/claude-0000-cwd"


def bash_tool_cmdline(command: str, home: Optional[str] = None) -> str:
    """The command line the Bash tool's shell has while ``command`` runs
    (measured on Claude Code 2.1.280: every ``'`` becomes ``'"'"'``)."""
    home = home or os.path.expanduser("~")
    return (_WRAPPER_HEAD.replace("{home}", home) + command.replace("'", "'\"'\"'")
            + _WRAPPER_TAIL)


# ------------------------------------------------------------------ #
# Words
# ------------------------------------------------------------------ #

_VAR = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")


def _resolve(w: Word, env: dict) -> Optional[str]:
    """The word's value with known variables substituted, or None when
    something in it cannot be known statically."""
    if w.subs:
        return None
    v = w.value
    if not w.expands:
        return v
    unknown = False

    def sub(m):
        nonlocal unknown
        name = m.group(1)
        if name in env:
            return env[name]
        if name in ("HOME",):
            return os.path.expanduser("~")
        unknown = True
        return m.group(0)
    out = _VAR.sub(sub, v)
    if unknown or "$" in out.replace("$$", ""):
        return None
    return out


def _argv(cmd: Simple, env: dict) -> list:
    """Resolved argv; an unresolvable word stays None. An unquoted
    ``$VAR`` holding several words is split, as bash would."""
    out: list = []
    for w in cmd.words:
        m = re.fullmatch(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?", w.raw)
        if m and m.group(1) in env:
            out.extend(env[m.group(1)].split())
            continue
        out.append(_resolve(w, env))
    return out


_WRAPPERS = {"sudo", "doas", "env", "nohup", "nice", "ionice", "setsid",
             "stdbuf", "time", "command", "exec", "builtin", "chrt",
             "taskset", "unbuffer", "timeout", "sshpass", "caffeinate",
             "flock", "systemd-run", "numactl", "xvfb-run"}
_WRAPPER_ARGOPTS = {
    "sudo": set("ugCDhprtTU"), "env": set("uCS"), "nice": {"n"},
    "ionice": set("cnp"), "timeout": set("sk"), "sshpass": set("pfPe"),
    "chrt": set(), "taskset": {"c"}, "numactl": set("CNmpi"),
    "flock": set("wEe"), "systemd-run": set("pEu"),
}


def _base(a: Optional[str]) -> str:
    return (a or "").rsplit("/", 1)[-1]


def _strip_wrappers(argv: list) -> tuple[list, bool]:
    """Drop ``sudo``/``nohup``/``timeout N``/... ; report whether a
    ``timeout`` bounded the command."""
    timed = False
    while argv and _base(argv[0]) in _WRAPPERS:
        name = _base(argv[0])
        i = 1
        takes = _WRAPPER_ARGOPTS.get(name, set())
        while i < len(argv):
            a = argv[i]
            if a is None:
                break
            if name == "env" and re.match(r"^[A-Za-z_]\w*=", a):
                i += 1
                continue
            if a == "--":
                i += 1
                break
            if a.startswith("--"):
                i += 1 + (1 if "=" not in a and name in ("timeout",) and a in ("--signal", "--kill-after") else 0)
                continue
            if a.startswith("-") and len(a) > 1:
                if name == "nice" and re.fullmatch(r"-\d+", a):
                    i += 1
                    continue
                flag = a[1]
                i += 1
                if flag in takes and len(a) == 2:
                    i += 1
                continue
            break
        if name == "timeout" and i < len(argv):
            timed = True
            i += 1                              # DURATION
        elif name == "taskset" and i < len(argv) and "c" not in takes:
            i += 1                              # mask
        elif name == "flock" and i < len(argv):
            i += 1                              # lockfile
        elif name == "chrt" and i < len(argv) and (argv[i] or "").isdigit():
            i += 1
        argv = argv[i:]
    return argv, timed


# ------------------------------------------------------------------ #
# Pattern matching as procps / psmisc / grep do it
# ------------------------------------------------------------------ #

_POSIX_CLASSES = {"alpha": "a-zA-Z", "digit": "0-9", "alnum": "a-zA-Z0-9",
                  "upper": "A-Z", "lower": "a-z", "space": r"\s",
                  "punct": r"!-/:-@\[-`{-~", "xdigit": "0-9A-Fa-f",
                  "word": r"\w", "blank": r" \t"}


def _bre_to_ere(p: str) -> str:
    """GNU BRE (plain ``grep``) to ERE: ``\\|`` ``\\(`` ``\\{`` ``\\+`` ``\\?``
    are the operators, their bare forms are literals (measured: ``grep
    'a\\|b'`` alternates, ``grep -E`` and procps take ``\\|`` literally)."""
    out, i, in_br = [], 0, False
    while i < len(p):
        c = p[i]
        if in_br:
            out.append(c)
            if c == "]" and not (out[-2:-1] == ["["] or out[-3:-1] == ["[", "^"]):
                in_br = False
            i += 1
            continue
        if c == "[":
            in_br = True
            out.append(c)
        elif c == "\\" and i + 1 < len(p):
            n = p[i + 1]
            out.append(n if n in "|(){}+?" else "\\" + n)
            i += 2
            continue
        elif c in "|(){}+?":
            out.append("\\" + c)
        else:
            out.append(c)
        i += 1
    return "".join(out)


def _ere(pattern: str, *, fixed: bool = False, icase: bool = False,
         exact: bool = False, word: bool = False,
         basic: bool = False) -> Optional[re.Pattern]:
    if basic and not fixed:
        pattern = _bre_to_ere(pattern)
    if fixed:
        body = re.escape(pattern)
    else:
        body = re.sub(r"\[:(\w+):\]",
                      lambda m: _POSIX_CLASSES.get(m.group(1), m.group(0)),
                      pattern)
        body = body.replace(r"\<", r"\b").replace(r"\>", r"\b")
    if word:
        body = rf"\b(?:{body})\b"
    if exact:
        body = rf"^(?:{body})$"
    try:
        return re.compile(body, re.IGNORECASE if icase else 0)
    except re.error:
        return None


# ------------------------------------------------------------------ #
# Process matchers
# ------------------------------------------------------------------ #


@dataclass
class Matcher:
    tool: str                   # pkill / pgrep / killall / pidof / ps|grep
    regex: Optional[re.Pattern]
    full: bool                  # against the command line (else comm)
    kills: bool                 # sends a real signal itself
    cmd: Simple
    pattern: str
    extra_lines: list = field(default_factory=list)   # e.g. grep's own ps line
    post_filters: list = field(default_factory=list)  # (regex, invert)
    lists_cmdline: bool = False     # pgrep -a / -l: lines carry the command
    spares_self: bool = False       # a later filter/guard excludes `$$`


_PGREP_ARGOPTS = set("dgGPstuUF") | {"--signal", "--pidfile", "--ns",
                                     "--nslist", "--session", "--group",
                                     "--parent", "--euid", "--uid",
                                     "--terminal", "--delimiter", "--pgroup"}
_NARROWING = set("PgstGF") | {"--parent", "--pgroup", "--session",
                              "--terminal", "--group", "--pidfile", "--ns",
                              "--nslist"}


def _current_users() -> set:
    users = {"0", "root", "$USER", "$(whoami)", "$(id -u)", "$LOGNAME"}
    for k in ("USER", "LOGNAME", "USERNAME"):
        if os.environ.get(k):
            users.add(os.environ[k])
    return users


def _parse_procps(argv: list, tool: str, cmd: Simple) -> Optional[Matcher]:
    full = icase = exact = invert = lists = False
    newest_or_oldest = None
    sig: Optional[str] = None
    pattern: Optional[str] = None
    user_ok = True
    i = 1
    while i < len(argv):
        a = argv[i]
        if a is None:
            return None
        if pattern is None and a.startswith("-") and len(a) > 1 and a != "--":
            if a.startswith("--"):
                name, _, val = a.partition("=")
                if name in ("--full",):
                    full = True
                elif name in ("--ignore-case",):
                    icase = True
                elif name in ("--exact",):
                    exact = True
                elif name in ("--inverse",):
                    invert = True
                elif name in ("--newest", "--oldest"):
                    newest_or_oldest = name
                elif name in ("--list-full", "--list-name"):
                    lists = True
                if name in _NARROWING:
                    return None
                if name in ("--euid", "--uid"):
                    if not val:
                        i += 1
                        val = argv[i] if i < len(argv) else ""
                    user_ok = all(u in _current_users() for u in (val or "").split(","))
                elif name == "--signal":
                    if not val:
                        i += 1
                        val = argv[i] if i < len(argv) else ""
                    sig = val
                elif name in _PGREP_ARGOPTS and not val:
                    i += 1
                i += 1
                continue
            if tool == "pkill" and re.fullmatch(r"-[A-Z0-9]+|-SIG[A-Z0-9]+", a) and not re.fullmatch(r"-[fxinovcaelwAr]+", a):
                sig = a[1:]
                i += 1
                continue
            j = 1
            while j < len(a):
                f = a[j]
                if f == "f":
                    full = True
                elif f == "i":
                    icase = True
                elif f == "x":
                    exact = True
                elif f == "v":
                    invert = True
                elif f in "no":
                    newest_or_oldest = f
                elif f in "al":
                    lists = True
                elif f in _NARROWING:
                    return None
                elif f in "uU":
                    val = a[j + 1:] or (argv[i + 1] if i + 1 < len(argv) else "")
                    if not a[j + 1:]:
                        i += 1
                    user_ok = all(u in _current_users() for u in (val or "").split(","))
                    break
                elif f in _PGREP_ARGOPTS:
                    if not a[j + 1:]:
                        i += 1
                    break
                j += 1
            i += 1
            continue
        if a == "--":
            i += 1
            continue
        if pattern is None:
            pattern = a
        i += 1
    if invert or not user_ok or newest_or_oldest in ("o", "--oldest"):
        return None
    if pattern is None:
        pattern = ""
    kills = tool == "pkill" and (sig or "").upper().lstrip("SIG") not in ("0",)
    rx = _ere(pattern, icase=icase, exact=exact)
    if rx is None:
        return None
    m = Matcher(tool, rx, full, kills, cmd, pattern)
    m.lists_cmdline = lists
    return m


def _parse_killall(argv: list, cmd: Simple) -> list:
    regex = icase = False
    names = []
    user_ok = True
    i = 1
    while i < len(argv):
        a = argv[i]
        if a is None:
            return []
        if a.startswith("-") and len(a) > 1 and not names:
            if a in ("-r", "--regexp"):
                regex = True
            elif a in ("-I", "--ignore-case"):
                icase = True
            elif a in ("-e", "--exact"):
                pass                    # names are matched exactly anyway
            elif a in ("-u", "--user"):
                i += 1
                user_ok = (argv[i] if i < len(argv) else "") in _current_users()
            elif a in ("-s", "--signal", "-o", "--older-than", "-y",
                       "--younger-than", "-n", "--ns"):
                if a in ("-o", "--older-than", "-y", "--younger-than", "-n", "--ns"):
                    return []
                i += 1
            elif re.fullmatch(r"-[a-zA-Z]+", a) and set(a[1:]) <= set("rIeqvwigZ"):
                regex = regex or "r" in a
                icase = icase or "I" in a
            i += 1
            continue
        names.append(a)
        i += 1
    if not user_ok:
        return []
    out = []
    for n in names:
        rx = _ere(n, icase=icase, exact=True) if regex else _ere(n, fixed=True, icase=icase, exact=True)
        if rx is not None:
            out.append(Matcher("killall", rx, False, True, cmd, n))
    return out


# ------------------------------------------------------------------ #
# ps | grep pipelines
# ------------------------------------------------------------------ #

def _ps_shows_args(argv: list) -> bool:
    opts = " ".join(a or "" for a in argv[1:])
    if re.search(r"(^|\s)-\w*[fF]", opts):
        return True
    if re.search(r"\b(args|cmd|command)\b", opts):
        return True
    if re.search(r"(^|\s)[a-zA-Z]*u[a-zA-Z]*(\s|$)", opts):   # BSD aux
        return True
    return False


def _grep_filter(argv: list) -> Optional[tuple[list, bool, bool]]:
    """(regexes, invert, quiet_or_count) for a grep stage, or None."""
    tool = _base(argv[0])
    fixed = tool == "fgrep"
    extended = tool == "egrep"
    icase = invert = word = exact = quiet = False
    pats: list = []
    i = 1
    while i < len(argv):
        a = argv[i]
        if a is None:
            return None
        if a == "--":
            i += 1
            break
        if a.startswith("--"):
            name, _, val = a.partition("=")
            if name == "--regexp":
                pats.append(val or (argv[i + 1] if i + 1 < len(argv) else ""))
                if not val:
                    i += 1
            elif name in ("--invert-match",):
                invert = True
            elif name in ("--ignore-case",):
                icase = True
            elif name in ("--fixed-strings",):
                fixed = True
            elif name in ("--extended-regexp", "--perl-regexp"):
                extended = True
            elif name in ("--quiet", "--silent", "--count", "--max-count"):
                quiet = True
            i += 1
            continue
        if a.startswith("-") and len(a) > 1 and not (pats and not a[1:].isalpha()):
            j = 1
            while j < len(a):
                f = a[j]
                if f == "e":
                    rest = a[j + 1:]
                    if rest:
                        pats.append(rest)
                    else:
                        i += 1
                        pats.append(argv[i] if i < len(argv) else "")
                    break
                if f in "fABCmd":
                    if f == "m":
                        quiet = True
                    if not a[j + 1:]:
                        i += 1
                    break
                invert |= f == "v"
                icase |= f == "i"
                fixed |= f == "F"
                extended |= f in "EP"
                word |= f == "w"
                exact |= f == "x"
                quiet |= f in "qc"
                j += 1
            i += 1
            continue
        break
    if not pats and i < len(argv):
        pats.append(argv[i])
    if not pats or any(p is None for p in pats):
        return None
    rxs = []
    for p in pats:
        for alt in p.split("\n"):
            rx = _ere(alt, fixed=fixed, icase=icase, word=word, exact=exact,
                      basic=not extended)
            if rx is None:
                return None
            rxs.append(rx)
    return rxs, invert, quiet


_AWK_TERM = re.compile(r"\s*(!?)\s*(?:\$0\s*~\s*)?/((?:\\/|[^/])+)/\s*")


def _awk_filters(argv: list) -> Optional[list]:
    """An awk program's line selection as sequential filters: its pattern
    must be a conjunction of ``/re/`` / ``!/re/`` terms. ``[]`` for a
    program with no pattern (it passes every line); None for anything
    else — no opinion."""
    prog = None
    i = 1
    while i < len(argv):
        a = argv[i]
        if a is None:
            return None
        if a in ("-v", "-F", "-f"):
            if a == "-f":
                return None
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        prog = a
        break
    if prog is None:
        return None
    pat = prog.split("{", 1)[0]
    if not pat.strip():
        return []
    out = []
    for term in pat.split("&&"):
        m = _AWK_TERM.fullmatch(term)
        if not m:
            return None
        rx = _ere(m.group(2).replace("\\/", "/"))
        if rx is None:
            return None
        out.append(([rx], bool(m.group(1))))
    return out


def _narrows_to_one(cmds: list, k: int, ctx: Ctx) -> bool:
    """``| head -1`` after a listing keeps its lowest PID — the target when
    it runs, not this (newest) shell. No opinion on those."""
    for d in cmds[k + 1:]:
        if isinstance(d, Simple):
            a, _ = _strip_wrappers(_argv(d, ctx.env))
            if a and a[0] and _base(a[0]) == "head":
                rest = " ".join(x or "" for x in a[1:])
                if re.fullmatch(r"-1|-n\s*1|-n1|--lines=1", rest.strip()):
                    return True
    return False


def _downstream(cmds: list, k: int, ctx: Ctx) -> tuple[list, list, bool]:
    """Line filters applied after stage ``k`` (grep / awk), their own
    command lines, and whether any of them excludes ``$$`` (or narrows the
    list to its oldest entry)."""
    filters, own, spares = [], [], _narrows_to_one(cmds, k, ctx)
    for d in cmds[k + 1:]:
        if not isinstance(d, Simple):
            if isinstance(d, Loop) and ctx.names_self(d.raw):
                spares = True
            break
        if ctx.names_self(d.raw):
            spares = True
        dargv, _ = _strip_wrappers(_argv(d, ctx.env))
        if not dargv or dargv[0] is None:
            break
        name = _base(dargv[0])
        if name in ("grep", "egrep", "fgrep"):
            if ctx.names_self(d.raw):
                continue                # `grep -v $$`: handled as spares
            g = _grep_filter(dargv)
            if g is None:
                return filters, own, spares
            rxs, inv, _ = g
            filters.append((rxs, inv))
            own.append(" ".join(a for a in dargv if a is not None))
        elif name == "awk":
            a = _awk_filters(dargv)
            if a is None:
                break
            filters += a
            own.append(" ".join(x for x in dargv if x is not None))
            break                       # its output is no longer the listing
        else:
            break
    return filters, own, spares


def _ps_matchers(pipe: Pipeline, ctx: Ctx) -> list:
    """``ps ... | grep PAT [| grep -v X] ...`` as matchers over ps lines."""
    out = []
    cmds = [c for c in pipe.cmds]
    for k, c in enumerate(cmds):
        if not isinstance(c, Simple):
            continue
        argv, _ = _strip_wrappers(_argv(c, ctx.env))
        if not argv or _base(argv[0]) != "ps" or not _ps_shows_args(argv):
            continue
        filters, own_lines, spares = _downstream(cmds, k, ctx)
        positive = next((rxs for rxs, inv in filters if not inv), None)
        if positive is None:
            continue
        m = Matcher("ps|grep", None, True, False, c,
                    " / ".join(r.pattern for r in positive))
        m.post_filters = filters
        m.extra_lines = own_lines
        m.spares_self = spares
        out.append(m)
    return out


def _survives(line: str, filters: list) -> bool:
    for rxs, inv in filters:
        hit = any(r.search(line) for r in rxs)
        if hit == inv:
            return False
    return True


def _hits(m: Matcher, carriers: Iterable[Carrier], *, include_own: bool) -> list:
    """Carriers ``m`` would select."""
    found = []
    for c in carriers:
        if m.tool == "ps|grep":
            if _survives(c.cmdline, m.post_filters):
                found.append(c)
            continue
        target = c.cmdline if m.full else c.comm[:15]
        if m.regex is not None and m.regex.search(target):
            if m.post_filters and m.lists_cmdline and not _survives(
                    f"12345 {c.cmdline}", m.post_filters):
                continue
            found.append(c)
    if include_own and m.tool == "ps|grep":
        for line in m.extra_lines:
            if _survives(line, m.post_filters):
                found.append(Carrier(line, "grep", "the grep in its own pipeline"))
                break
    return found


# ------------------------------------------------------------------ #
# The walk
# ------------------------------------------------------------------ #

LIVENESS = {"pgrep", "pidof", "ps", "systemctl", "docker", "podman",
            "kubectl", "squeue", "sacct", "qstat", "bjobs", "tmux", "screen",
            "jobs", "wait", "lsof", "fuser", "nvidia-smi", "pstree", "top",
            "service", "supervisorctl", "pm2", "launchctl", "sc", "tasklist"}
_FAILURE = re.compile(
    r"err|fail|fatal|traceback|exception|panic|abort|killed|\boom\b|"
    r"out of memory|segfault|segmentation|core dump|cancel|timed.?out|"
    r"denied|refused|crash|died|\bdead\b|exit(ed)?[ _-]?(code|status)|"
    r"non.?zero|unhealthy|stopped|conclusion|terminat|signal|inactive|"
    r"exited|failure|\bgone\b|exit|\brc=|status", re.IGNORECASE)
_DEADLINE = re.compile(r"-(lt|le|gt|ge|eq|ne)\b|\(\(.*[<>].*\)\)|\bSECONDS\b|"
                       r"date\s+\+%s|\$\(\(.*[<>]", re.S)
_SHELLS = {"bash", "sh", "dash", "zsh", "ksh", "ash", "busybox"}
_NON_EXEC = {"cd", "echo", "printf", "kill", "wait", "test", "[", "[[",
             "true", "false", ":", "export", "set", "source", ".", "eval",
             "read", "exit", "return", "break", "continue", "local",
             "declare", "shift", "trap", "ulimit", "umask", "alias",
             "unset", "type", "hash", "let", "pushd", "popd", "shopt"}


class _Walker:
    def __init__(self, *, local_carriers: list, background: bool,
                 max_depth: int = 6, streaming: bool = False):
        self.streaming = streaming
        self.findings: list[Finding] = []
        self.local_carriers = local_carriers
        self.background = background
        self.max_depth = max_depth
        self._seen: set = set()

    # -------------------------------------------------------- contexts

    def _inherit(self, ctx: Ctx, c: Simple) -> list:
        """What a child started by ``c`` runs alongside. A backgrounded
        simple command (``nohup bash x.sh &``) is exec'd in a fork, so it
        does not carry this shell's argv, and this shell may be gone
        before the child's commands run — claim nothing about it."""
        if getattr(self, "_bg", False):
            return []
        return ctx.alive_for(c)

    def nested(self, script: str, parent: Ctx, *, where: str,
               carriers: list, shell: Optional[Carrier], remote: bool,
               stdin_script: Optional[str] = None,
               timeout_wrapped: bool = False,
               own: Optional[Carrier] = None) -> None:
        if parent.depth >= self.max_depth:
            return
        seq = try_parse(script)
        if seq is None:
            return
        ctx = Ctx(where, carriers, remote, env={}, shell=shell,
                  stdin_script=stdin_script, depth=parent.depth + 1,
                  timeout_wrapped=parent.timeout_wrapped or timeout_wrapped,
                  own=own or shell)
        if shell is not None and shell.exec_away:
            ctx.tail = _exec_tail(seq)
        _prime_usage(seq, ctx)
        self.seq(seq, ctx, role="stmt")

    # -------------------------------------------------------- tree

    def seq(self, s: Seq, ctx: Ctx, role: str, spare: bool = False) -> None:
        for n, item in enumerate(s.items):
            last = n == len(s.items) - 1
            bg = s.seps[n] == "&" if n < len(s.seps) else False
            self.andor(item, ctx, role if last else ("stmt" if role == "cond" else role),
                       bg=bg, spare=spare)

    def andor(self, a: AndOr, ctx: Ctx, role: str, bg: bool = False,
              spare: bool = False) -> None:
        for n, p in enumerate(a.parts):
            last = n == len(a.parts) - 1
            self.pipeline(p, ctx, role if last else "cond", bg=bg, spare=spare)

    def pipeline(self, p: Pipeline, ctx: Ctx, role: str, bg: bool = False,
                 spare: bool = False) -> None:
        sink = _kill_sink(p, ctx)
        for m in _ps_matchers(p, ctx):
            m.spares_self = m.spares_self or spare
            self.judge_probe(m, ctx, "kill" if sink else role, p)
        for k, c in enumerate(p.cmds):
            last = k == len(p.cmds) - 1
            crole = role if (last and len(p.cmds) == 1) else (
                "kill" if (sink and k < len(p.cmds) - 1) else
                ("cond" if role == "cond" and last else "piped"))
            post = _downstream(p.cmds, k, ctx) if not last else ([], [], False)
            self.command(c, ctx, crole, pipe=p,
                         stdin_from=p.cmds[k - 1] if k else None,
                         bg=bg and len(p.cmds) == 1, post=post,
                         spare=spare or post[2])

    def command(self, c, ctx: Ctx, role: str, pipe=None, stdin_from=None,
                bg: bool = False, post=([], [], False), spare: bool = False) -> None:
        if isinstance(c, Simple):
            self.simple(c, ctx, role, stdin_from=stdin_from, bg=bg, post=post,
                        spare=spare)
        elif isinstance(c, Loop):
            self.seq(c.cond, ctx, "cond")
            self.seq(c.body, ctx, "stmt")
            self.check_loop(c, ctx)
        elif isinstance(c, If):
            for cond, body in c.clauses:
                self.seq(cond, ctx, "cond")
                self.seq(body, ctx, "stmt")
            if c.orelse:
                self.seq(c.orelse, ctx, "stmt")
        elif isinstance(c, For):
            kills = any(_is_kill(s, ctx) for s in _simples(c.body))
            spares = spare or ctx.names_self(c.raw) or _inspects_candidate(c)
            for w in c.words or []:
                for sub in w.subs:
                    # a loop over the PIDs that kills none of them is a listing
                    self.seq(sub, ctx, "kill" if kills else "listing", spare=spares)
            self.seq(c.body, ctx, "stmt")
        elif isinstance(c, Group):
            self.seq(c.body, ctx, role)
        elif isinstance(c, Case):
            for sub in c.word.subs:
                self.seq(sub, ctx, "cond")
            for _, body in c.arms:
                self.seq(body, ctx, "stmt")
        elif isinstance(c, FuncDef):
            self.command(c.body, ctx, "stmt")

    def simple(self, c: Simple, ctx: Ctx, role: str, stdin_from=None,
               bg: bool = False, post=([], [], False), spare: bool = False) -> None:
        self._bg = bg
        # assignments first: they feed later resolution
        for name, w in c.assigns:
            for sub in w.subs:
                usage = _var_usage(name, ctx)
                self.seq(sub, ctx, usage or "capture",
                         spare=spare or bool(usage and ctx.usage.get(name + "#spared")))
            v = _resolve(w, ctx.env)
            if v is not None and not c.words:
                ctx.env[name] = v
            elif not c.words:
                ctx.env.pop(name, None)
        raw_argv = _argv(c, ctx.env)
        argv, timed = _strip_wrappers(raw_argv)
        name = _base(argv[0]) if argv and argv[0] else ""
        # command substitutions inside the words run in forks of the shell
        for w in c.words:
            for sub in w.subs:
                sub_role = ("kill" if name == "kill" and not _kill_is_probe(argv) else
                            "cond" if name in ("test", "[", "[[") or role == "cond" else
                            "listing" if name in ("echo", "printf", "logger") else "capture")
                self.seq(sub, ctx, sub_role, spare=spare or ctx.names_self(c.raw))
        if not argv or argv[0] is None:
            return
        if name == "export":
            for a in argv[1:]:
                if a and "=" in a:
                    k, _, v = a.partition("=")
                    ctx.env[k] = v
            return
        if name in ("pkill", "pgrep"):
            m = _parse_procps(argv, name, c)
            if m is not None:
                m.post_filters, m.extra_lines = post[0], post[1]
                m.spares_self = spare or post[2]
                if m.kills:
                    self.judge_kill(m, ctx)
                else:
                    self.judge_probe(m, ctx, role, None)
        elif name == "killall":
            for m in _parse_killall(argv, c):
                self.judge_kill(m, ctx)
        elif name == "pidof":
            for a in argv[1:]:
                if a and not a.startswith("-"):
                    rx = _ere(a, fixed=True, exact=True)
                    if rx:
                        self.judge_probe(Matcher("pidof", rx, False, False, c, a), ctx, role, None)
        self.ssh_killers(name, argv, c, ctx)
        # contexts this command creates
        if name == "ssh":
            self.enter_ssh(argv, c, ctx, timed)
        elif name in _SHELLS:
            self.enter_shell(argv, c, ctx, timed, stdin_from)
        elif name == "watch":
            rest = [a for a in argv[1:] if a and not a.startswith("-")]
            if rest and all(a is not None for a in argv):
                script = " ".join(rest)
                sh = Carrier("sh -c " + script, "sh", "the `sh -c` that `watch` runs")
                self.nested(script, ctx, where=ctx.where,
                            carriers=self._inherit(ctx, c) + [sh], shell=sh,
                            remote=ctx.remote)

    # -------------------------------------------------------- spawners

    def enter_ssh(self, argv: list, c: Simple, ctx: Ctx, timed: bool) -> None:
        split = _ssh_split(argv)
        if split is None:
            return
        host, user, rest = split
        sshd = Carrier(f"sshd: {user or 'root'}@notty", "sshd",
                       "the remote sshd session")
        where = f"on {host}"
        stdin = _stdin_text(c)
        if not rest:
            if stdin is not None:
                login = Carrier("-bash", "bash", "the remote login shell")
                self.nested(stdin, ctx, where=where, carriers=[sshd, login],
                            shell=None, remote=True, timeout_wrapped=timed,
                            own=login)
            return
        script = " ".join(rest)
        sh = Carrier("bash -c " + script, "bash",
                     f"the remote `bash -c` that ssh starts on {host}", exec_away=True)
        self.nested(script, ctx, where=where, carriers=[sshd, sh], shell=sh,
                    remote=True, stdin_script=stdin, timeout_wrapped=timed)

    def enter_shell(self, argv: list, c: Simple, ctx: Ctx, timed: bool,
                    stdin_from) -> None:
        if any(a is None for a in argv):
            return
        name = _base(argv[0])
        i, script, has_c = 1, None, False
        while i < len(argv):
            a = argv[i]
            if a.startswith("-") and len(a) > 1 and a != "-" and not a.startswith("--"):
                if "c" in a[1:]:
                    has_c = True
                if "o" in a[1:] or "O" in a[1:]:
                    i += 1
                i += 1
                continue
            if a.startswith("--"):
                i += 1
                continue
            break
        me = Carrier(" ".join(argv), name, f"the `{_clip(' '.join(argv[:2]), 40)}` it starts")
        if has_c:
            if i >= len(argv):
                return
            script = argv[i]
            sh = Carrier(" ".join(argv), name,
                         f"the `{name} -c` shell {ctx.where}", exec_away=True)
            self.nested(script, ctx, where=ctx.where,
                        carriers=self._inherit(ctx, c) + [sh], shell=sh,
                        remote=ctx.remote, stdin_script=_stdin_text(c),
                        timeout_wrapped=timed)
            return
        if i < len(argv) and argv[i] != "-":
            # a script file: read it when it is here to read
            if ctx.remote:
                return
            p = Path(os.path.expanduser(argv[i]))
            try:
                if p.is_file() and p.stat().st_size < 256_000:
                    text = p.read_text(errors="replace")
                else:
                    return
            except OSError:
                return
            self.nested(text, ctx, where=ctx.where,
                        carriers=self._inherit(ctx, c) + [me], shell=None,
                        remote=ctx.remote, timeout_wrapped=timed, own=me)
            return
        stdin = _stdin_text(c)
        if stdin is None and stdin_from is None and ctx.stdin_script is not None:
            stdin = ctx.stdin_script
        if stdin is None:
            return
        self.nested(stdin, ctx, where=ctx.where,
                    carriers=self._inherit(ctx, c) + [me], shell=None,
                    remote=ctx.remote, timeout_wrapped=timed, own=me)

    # -------------------------------------------------------- judgements

    def _add(self, f: Finding) -> None:
        key = (f.rule, f.where, f.command)
        if key not in self._seen:
            self._seen.add(key)
            self.findings.append(f)

    def judge_kill(self, m: Matcher, ctx: Ctx) -> None:
        hits = _hits(m, ctx.alive_for(m.cmd, spare_own=m.spares_self),
                     include_own=False)
        if not hits:
            return
        victim = hits[0]
        self._add(Finding(
            "self-kill", ctx.where, m.cmd.raw or " ".join(m.cmd.argv),
            f"Its pattern `{m.pattern}` also matches {victim.label} — "
            f"{_why(m, victim)} — and that process is alive while the kill "
            "runs. So it kills its own "
            + ("connection (ssh exits 255)" if ctx.remote else "shell (exit 144)")
            + " and nothing after it runs.",
            _fix_kill(m, ctx, victim)))

    def judge_probe(self, m: Matcher, ctx: Ctx, role: str, pipe) -> None:
        if role not in ("cond", "kill", "capture"):
            return
        hits = _hits(m, ctx.alive_for(None if role != "cond" or pipe else m.cmd,
                                      spare_own=m.spares_self and role == "kill"),
                     include_own=role == "cond")
        if not hits:
            return
        victim = hits[0]
        if role == "kill":
            rule, effect = "self-kill", ("the PIDs it hands to kill include "
                                         f"{victim.label}, so the kill takes down "
                                         "its own " + ("connection" if ctx.remote else "shell"))
        elif role == "cond":
            rule, effect = "self-match", ("so the test is always true: a "
                                          "`while` on it never ends and an `if` "
                                          "always sees a process")
        else:
            rule, effect = "self-match", (f"so its result always includes {victim.label}"
                                          " — a count or PID list taken from it is wrong")
        self._add(Finding(
            rule, ctx.where, m.cmd.raw or " ".join(m.cmd.argv),
            f"`{m.pattern}` matches {victim.label} ({_why(m, victim)}), {effect}.",
            _fix_probe(m, ctx, victim)))

    def ssh_killers(self, name: str, argv: list, c: Simple, ctx: Ctx) -> None:
        """Killing the ssh daemon itself (remote, or local under ssh)."""
        under_ssh = ctx.remote or any(k.comm == "sshd" for k in ctx.carriers)
        if not under_ssh:
            return
        a = [x or "" for x in argv]
        hit = False
        if name in ("systemctl", "service"):
            verbs = {"stop", "kill", "mask"}
            units = {"ssh", "sshd", "ssh.service", "sshd.service",
                     "ssh.socket", "dropbear", "dropbear.service"}
            hit = bool(verbs & set(a)) and bool(units & set(a)) or (
                "disable" in a and "--now" in a and bool(units & set(a)))
        elif name == "fuser" and any(x == "-k" or (x.startswith("-") and "k" in x and not x.startswith("--")) for x in a[1:]):
            hit = any(re.fullmatch(r"(22|0)/tcp", x) for x in a) or (
                "tcp" in a and "22" in a)
        elif name == "kill":
            # `kill -9 -1` / `kill -- -1`: every process the user may signal
            args = a[1:]
            hit = (len(args) >= 2 and args[-1] == "-1"
                   and (args[-2] == "--" or args[-2].startswith("-")))
        if hit:
            self._add(Finding(
                "kills-ssh", ctx.where, c.raw or " ".join(a),
                "This takes down the ssh daemon or every process of the user, "
                "including the connection this command arrived on.",
                "Kill the specific PIDs you mean; never stop sshd on a host you "
                "reach over ssh (`restart` keeps existing sessions)."))

    # -------------------------------------------------------- waiters

    def check_loop(self, loop: Loop, ctx: Ctx) -> None:
        if not self.background:
            return
        if self.streaming:
            # a Monitor delivers every line it prints: a loop that checks or
            # emits failure anywhere is covered, not only through its exits
            whole = _simples(loop.cond) + _simples(loop.body)
            text = "\n".join(" ".join(w.value for w in x.words[1:]) for x in whole)
            text += "\n" + "\n".join(sub_text for sub_text in _sub_texts(whole))
            if _covered(text, whole + _sub_simples(whole), ctx):
                return
        if not any(_base((_strip_wrappers(_argv(s, ctx.env))[0] or [None])[0]) == "sleep"
                   for s in _simples(loop.body) + _simples(loop.cond)):
            return
        exits = _loop_exits(loop)
        if not exits:
            return                                  # endless by design
        if ctx.timeout_wrapped:
            return
        cond_text, simples = _exit_material(exits, ctx)
        if not simples and not cond_text.strip():
            return
        if _covered(cond_text, simples, ctx):
            return
        self._add(Finding(
            "blind-waiter", ctx.where, _clip(loop.raw, 200),
            "This background waiter exits only when its success condition "
            "becomes true. If the job crashes, hangs or is killed, it waits "
            "forever and nothing tells you.",
            "give it an exit for failure too — e.g. `until [ -e DONE ] || ! "
            "kill -0 $PID 2>/dev/null; do sleep 30; done`, or match the failure "
            "lines as well (`grep -qE 'DONE|Traceback|Error|Killed'`), or bound "
            "it (`timeout 3h …`, or a counter `[ $i -ge 360 ] && break`) — and "
            "after it exits, report which way it ended."))


# ------------------------------------------------------------------ #
# helpers for the walk
# ------------------------------------------------------------------ #

def _sub_simples(simples: list) -> list:
    out = []
    for s in simples:
        for w in s.words + [w for _n, w in s.assigns]:
            for sub in w.subs:
                inner = _simples(sub)
                out += inner + _sub_simples(inner)
    return out


def _sub_texts(simples: list) -> list:
    return [" ".join(w.value for w in x.words[1:]) for x in _sub_simples(simples)]


def _simples(node) -> list:
    from claude_hooks.shell_ast import iter_simple
    return iter_simple(node)


def _ssh_split(argv: list) -> Optional[tuple[str, Optional[str], list]]:
    """(host, user, remote words) of an ssh argv, or None."""
    takes = set("BbcDEeFIiJLlmOoPpQRSWw")
    i, host, user = 1, None, None
    while i < len(argv):
        a = argv[i]
        if a is None:
            return None
        if a.startswith("-") and len(a) > 1:
            j = 1
            while j < len(a):
                if a[j] in takes:
                    val = a[j + 1:] or (argv[i + 1] if i + 1 < len(argv) else "")
                    if not a[j + 1:]:
                        i += 1
                    if a[j] == "l":
                        user = val
                    break
                j += 1
            i += 1
            continue
        host = a
        i += 1
        break
    if not host:
        return None
    if "@" in host:
        user, host = host.split("@", 1)
    rest = argv[i:]
    if any(a is None for a in rest):
        return None
    return host, user, rest


def _stdin_text(c: Simple) -> Optional[str]:
    for r in c.redirects:
        if r.op in ("<<", "<<-") and r.heredoc is not None:
            return r.heredoc
        if r.op == "<<<" and r.target is not None and not r.target.subs:
            return r.target.value
    return None


def _exec_tail(seq: Seq) -> Optional[Simple]:
    """The command ``bash -c`` execs in place (measured, bash 5.1/5.2)."""
    if not seq.items:
        return None
    if any(sep not in (";", "\n") for sep in seq.seps[:-1]):
        return None
    if seq.seps and seq.seps[-1] == "&":
        return None
    last = seq.items[-1]
    if len(last.parts) != 1 or len(last.parts[0].cmds) != 1 or last.parts[0].negated:
        return None
    cmd = last.parts[0].cmds[0]
    if not isinstance(cmd, Simple) or not cmd.words:
        return None
    if cmd.words[0].value in _NON_EXEC:
        return None
    return cmd


def _inspects_candidate(loop: For) -> bool:
    """``for p in $(pgrep ...)`` whose body looks at each PID before killing
    it (its /proc cmdline, ps -p, a grep) — it chooses, so no opinion."""
    var = re.escape(loop.var)
    body = loop.raw.split(" do", 1)[-1]
    return bool(re.search(r"/proc/\$\{?" + var + r"\b|ps\s+[^|;]*-p\s*\"?\$\{?" + var + r"\b", body))


def _is_kill(s: Simple, ctx: Ctx) -> bool:
    argv, _ = _strip_wrappers(_argv(s, ctx.env))
    return bool(argv) and _base(argv[0]) in ("kill", "pkill") and not _kill_is_probe(argv)


def _kill_is_probe(argv: list) -> bool:
    a = [x or "" for x in argv[1:]]
    return "-0" in a or ("-s" in a and "0" in a) or "--signal=0" in a


def _kill_sink(p: Pipeline, ctx: Ctx) -> bool:
    """Does this pipeline end by killing what flows into it?"""
    for c in p.cmds[1:]:
        if isinstance(c, Simple):
            argv, _ = _strip_wrappers(_argv(c, ctx.env))
            if not argv or argv[0] is None:
                continue
            n = _base(argv[0])
            if n == "xargs":
                rest = [a for a in argv[1:] if a and not a.startswith("-")]
                if rest and _base(rest[0]) in ("kill", "pkill") and not _kill_is_probe(rest):
                    return True
            if n == "kill" and not _kill_is_probe(argv):
                return True
        elif isinstance(c, Loop):
            if any(_is_kill(s, ctx) for s in _simples(c.body)):
                return True
    return False


def _var_usage(name: str, ctx: Ctx) -> Optional[str]:
    """How a captured variable is used later (``P=$(pgrep ...)`` then
    ``kill $P`` makes the capture a kill); None = unknown."""
    return ctx.usage.get(name)


def _why(m: Matcher, c: Carrier) -> str:
    if m.tool == "ps|grep":
        return "its line in the `ps` listing survives every grep in the pipeline"
    if not m.full:
        return f"its process name is `{c.comm}`"
    rx = m.regex
    mt = rx.search(c.cmdline) if rx else None
    if mt:
        s = max(0, mt.start() - 30)
        frag = c.cmdline[s:mt.end() + 30]
        return f"its command line contains `…{' '.join(frag.split())}…`"
    return "its command line matches"


def _fix_kill(m: Matcher, ctx: Ctx, victim: Carrier) -> str:
    if ctx.remote and victim.comm != "sshd":
        return ("make the kill the last command of the ssh string (`ssh h "
                "'cd /x; pkill -f PAT'` — bash execs it in place), or send the "
                "script on stdin, whose text is in no argv: `ssh h bash -s "
                "<<'EOF'` … `EOF`. Or look the PIDs up in one call and kill "
                "them by PID in a second.")
    if victim.comm == "sshd":
        return "match the process you mean, not sshd — kill it by PID."
    if not m.full:
        return (f"`{victim.comm}` is this session's own process; name the "
                "program you mean, or kill by PID.")
    return ("use a pattern that matches the target but not this command's own "
            "text — `[x]yz` works only if the bare name appears nowhere else in "
            "the same command (not in a path, heredoc, nohup target or a later "
            "step). Put the kill in its own call, or kill by PID.")


def _fix_probe(m: Matcher, ctx: Ctx, victim: Carrier) -> str:
    return ("wait on a PID (`while kill -0 $PID 2>/dev/null`), a pidfile or a "
            "sentinel file, or use a `[x]yz` pattern with no bare copy of the "
            "name anywhere in the same command"
            + ("; over ssh, sending the script on stdin (`ssh h bash -s "
               "<<'EOF'`) keeps its text out of every argv" if ctx.remote else "")
            + ".")


def _loop_exits(loop: Loop) -> list:
    """(kind, material) for every way out of the loop."""
    exits = []
    cond_cmds = _simples(loop.cond)
    trivially_true = (len(cond_cmds) == 1 and cond_cmds[0].words and
                      cond_cmds[0].words[0].value in ("true", ":") and loop.kind == "while") or (
        len(cond_cmds) == 1 and cond_cmds[0].words and
        cond_cmds[0].words[0].value == "false" and loop.kind == "until")
    if not trivially_true:
        exits.append(("cond", loop.cond))
    exits += _breaks(loop.body, guards=[])
    return exits


def _breaks(node, guards: list) -> list:
    """Every break/exit in ``node`` with the conditions that guard it."""
    out = []
    if isinstance(node, Seq):
        for it in node.items:
            out += _breaks(it, guards)
    elif isinstance(node, AndOr):
        for n, p in enumerate(node.parts):
            out += _breaks(p, guards + node.parts[:n])
    elif isinstance(node, Pipeline):
        for c in node.cmds:
            out += _breaks(c, guards)
    elif isinstance(node, Simple):
        if node.words and node.words[0].value in ("break", "exit", "return"):
            out.append(("break", guards))
    elif isinstance(node, If):
        prior = []
        for cond, body in node.clauses:
            out += _breaks(body, guards + prior + [cond])
            prior.append(cond)
        if node.orelse:
            out += _breaks(node.orelse, guards + prior)
    elif isinstance(node, Case):
        for pats, body in node.arms:
            out += _breaks(body, guards + [("case", pats, node.word)])
    elif isinstance(node, Group):
        out += _breaks(node.body, guards)
    elif isinstance(node, (For,)):
        out += _breaks(node.body, guards)
    return out


def _exit_material(exits: list, ctx: Ctx) -> tuple[str, list]:
    text_parts, simples = [], []

    def add(node):
        if isinstance(node, tuple) and node and node[0] == "case":
            text_parts.append(node[1])
            for sub in node[2].subs:
                add(sub)
            text_parts.append(node[2].value)
            return
        for s in _simples(node):
            simples.append(s)
            text_parts.append(" ".join(w.value for w in s.words[1:]))
            for w in s.words:
                for sub in w.subs:
                    add(sub)
            for _n, w in s.assigns:
                for sub in w.subs:
                    add(sub)
    for kind, mat in exits:
        if kind == "cond":
            add(mat)
        else:
            if not mat:
                text_parts.append("<unconditional>")
            for g in mat:
                add(g)
    return "\n".join(text_parts), simples


def _covered(text: str, simples: list, ctx: Ctx) -> bool:
    if "<unconditional>" in text:
        return True
    if _FAILURE.search(text) or _DEADLINE.search(text):
        return True
    for s in simples:
        argv, _ = _strip_wrappers(_argv(s, ctx.env))
        if not argv or argv[0] is None:
            continue
        n = _base(argv[0])
        if n in ("grep", "egrep"):
            g = _grep_filter(argv)
            # `grep -qE 'DONE|VOID|port bound'`: several outcomes are named,
            # so the waiter was written to end on more than success
            if g and len(g[0]) == 1 and "|" in g[0][0].pattern.replace("\\|", ""):
                return True
            if g and len(g[0]) > 1:
                return True
        if n in LIVENESS:
            return True
        if n == "kill" and _kill_is_probe(argv):
            return True
        if n in ("test", "[", "[[") and any(a and "/proc/" in a for a in argv):
            return True
        if n == "read":
            return True
        if n == "tail" and any(a and a.startswith("--pid") for a in argv):
            return True
        if n == "ssh":
            split = _ssh_split(argv)
            inner = try_parse(" ".join(split[2])) if split and split[2] else None
            if inner is not None:
                sub_s = _simples(inner)
                sub_t = "\n".join(" ".join(w.value for w in x.words[1:]) for x in sub_s)
                if _covered(sub_t, sub_s, ctx):
                    return True
    return False


# ------------------------------------------------------------------ #
# Captured PID lists: `P=$(pgrep -f X); kill $P`
# ------------------------------------------------------------------ #

def _prime_usage(seq, ctx: Ctx) -> None:
    """Mark variables whose captured value is later handed to kill, and
    whether the text around that kill excludes ``$$``."""
    spared = ctx.names_self(_source_text(seq))
    for s in _simples(seq):
        argv = [w.raw for w in s.words]
        if argv and _base(argv[0]) == "kill" and not _kill_is_probe(argv):
            for w in s.words[1:]:
                for m in _VAR.finditer(w.raw):
                    ctx.usage[m.group(1)] = "kill"
                    if spared:
                        ctx.usage[m.group(1) + "#spared"] = True


def _source_text(seq) -> str:
    return "\n".join(s.raw for s in _simples(seq))


# ------------------------------------------------------------------ #
# Entry points
# ------------------------------------------------------------------ #

def local_carriers(command: str, ancestors: Optional[list] = None) -> list:
    out = [Carrier(bash_tool_cmdline(command), "bash",
                   "the Bash tool's own `bash -c`, which holds this whole command")]
    for comm, cmdline in ancestors or []:
        label = {"claude": "this Claude Code session",
                 "sshd": "the ssh session you are logged in through",
                 "tmux: server": "the tmux server this session runs in",
                 "tmux": "the tmux server this session runs in"}.get(
                     comm, f"an ancestor of this session (`{comm}`)")
        out.append(Carrier(cmdline or comm, comm, label))
    return out


def check_bash(command: str, *, background: bool = False,
               ancestors: Optional[list] = None,
               streaming: bool = False) -> list[Finding]:
    """Findings for a Bash tool command; [] when it is fine or unknown."""
    if not command or "process-guard: allow" in command:
        return []
    if not re.search(r"pkill|pgrep|killall|pidof|\bps\b|\bkill\b|fuser|"
                     r"systemctl|service|\bsleep\b", command):
        return []
    w = _Walker(local_carriers=local_carriers(command, ancestors),
                background=background, streaming=streaming)
    seq = try_parse(command)
    if seq is None:
        return []
    ctx = Ctx("locally", list(w.local_carriers), remote=False)
    ctx.own = ctx.carriers[0]
    _prime_usage(seq, ctx)
    w.seq(seq, ctx, role="stmt")
    return w.findings


def check_monitor(command: str, ancestors: Optional[list] = None) -> list[Finding]:
    """Monitor streams events until it exits or expires. A self-match
    still kills it, and a filter that can only say "success" stays silent
    through a crash."""
    if not command or "process-guard: allow" in command:
        return []
    findings = check_bash(command, background=True, ancestors=ancestors,
                          streaming=True)
    seq = try_parse(command)
    if seq is None:
        return findings
    for s in _simples(seq):
        argv, _ = _strip_wrappers(_argv(s, {}))
        if not argv or argv[0] is None or _base(argv[0]) != "tail":
            continue
        if not any(a and (a in ("-f", "-F", "--follow") or re.fullmatch(r"-\w*[fF]\w*", a) or a.startswith("--follow")) for a in argv[1:]):
            continue
        if any(a and a.startswith("--pid") for a in argv):
            continue
        # the tail's pipeline: find grep filters after it
        pipe = _pipeline_of(seq, s)
        if pipe is None:
            continue
        idx = pipe.cmds.index(s)
        pats = []
        for d in pipe.cmds[idx + 1:]:
            if isinstance(d, Simple):
                dargv, _ = _strip_wrappers(_argv(d, {}))
                if dargv and dargv[0] and _base(dargv[0]) in ("grep", "egrep", "fgrep"):
                    g = _grep_filter(dargv)
                    if g and not g[1]:
                        pats += [r.pattern for r in g[0]]
        if pats and not any(_FAILURE.search(p) for p in pats):
            findings.append(Finding(
                "blind-waiter", "locally", s.raw,
                f"This monitor only emits lines matching `{' | '.join(pats)}`. "
                "If the job crashes or dies, the log stops or prints an error "
                "this filter drops, and the monitor stays silent — silence looks "
                "the same as \"still running\".",
                "add the failure signatures to the same alternation "
                "(`grep -E --line-buffered 'PATTERN|Traceback|Error|FAILED|"
                "Killed|OOM'`), or watch the process too (`tail --pid=$PID -f`)."))
    return findings


def _pipeline_of(node, target: Simple) -> Optional[Pipeline]:
    if isinstance(node, Pipeline):
        if target in node.cmds:
            return node
        for c in node.cmds:
            r = _pipeline_of(c, target)
            if r:
                return r
        return None
    children = []
    if isinstance(node, Seq):
        children = node.items
    elif isinstance(node, AndOr):
        children = node.parts
    elif isinstance(node, Loop):
        children = [node.cond, node.body]
    elif isinstance(node, If):
        children = [x for cl in node.clauses for x in cl] + ([node.orelse] if node.orelse else [])
    elif isinstance(node, (For, Group)):
        children = [node.body]
    elif isinstance(node, Case):
        children = [b for _, b in node.arms]
    for ch in children:
        r = _pipeline_of(ch, target)
        if r:
            return r
    return None


_STATUS_INTENT = re.compile(
    r"\b(check|status|finish|finished|done|complete|completed|progress|wait|"
    r"poll|ready|result|landed|still running|running|job|build|run|train|"
    r"deploy|waiter|monitor)\b", re.IGNORECASE)
PROMPT_NOTE = ("(When this fires: a job that failed, crashed, stalled or "
               "vanished is an outcome to report, not a reason to keep waiting "
               "— confirm the process is still alive or its output still "
               "growing, not only whether it has succeeded yet.)")


def augment_prompt(prompt: str) -> Optional[str]:
    """For CronCreate / ScheduleWakeup: the note that makes the check look
    for failure too, or None when the prompt needs nothing."""
    if not prompt or PROMPT_NOTE in prompt:
        return None
    p = prompt.strip()
    if p.startswith("/") or p.startswith("<<"):
        return None
    if not _STATUS_INTENT.search(p) or _FAILURE.search(p):
        return None
    return prompt.rstrip() + "\n\n" + PROMPT_NOTE


def render(findings: list[Finding]) -> str:
    head = ("process_guard rejected this command — "
            + ("it would kill or match itself" if any(f.rule in ("self-kill", "self-match", "kills-ssh") for f in findings)
               else "its waiter cannot notice a failure")
            + ":\n\n")
    return (head + "\n\n".join(f.render() for f in findings)
            + "\n\nRewrite the command with the fix and run it again — this "
              "does not need the user. (Nothing was executed.)")


def ancestors_from_proc(start_pid: Optional[int] = None) -> list:
    """(comm, cmdline) of the processes above this hook, Linux only.
    The hook's own launchers (claude-hook shims, run.py) are skipped:
    they are gone by the time the tool runs."""
    out = []
    pid = start_pid or os.getppid()
    seen = set()
    while pid and pid > 1 and pid not in seen and len(out) < 32:
        seen.add(pid)
        try:
            with open(f"/proc/{pid}/stat", "rb") as fh:
                stat = fh.read().decode(errors="replace")
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                cmdline = fh.read().replace(b"\0", b" ").decode(errors="replace").strip()
        except OSError:
            break
        rp = stat.rfind(")")
        comm = stat[stat.find("(") + 1:rp]
        ppid = int(stat[rp + 2:].split()[1])
        if "claude-hook" not in cmdline and "run.py" not in cmdline:
            out.append((comm, cmdline))
        pid = ppid
    return out


__all__ = ["Finding", "check_bash", "check_monitor", "augment_prompt",
           "render", "ancestors_from_proc", "bash_tool_cmdline"]
