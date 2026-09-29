"""A small bash parser, for static checks over commands before they run.

It exists for :mod:`claude_hooks.process_guard`, which has to know things
a regex cannot tell it: which words are the arguments of ``pkill``, what
string an ``ssh`` sends to the remote shell, whether a ``pgrep`` is a
loop condition or a listing, what a heredoc feeds and to whom. So it
parses the command the way bash does — quotes, escapes, ``$(...)``,
backticks, heredocs, pipelines, ``&&``/``||``, loops, ``if``, ``case``,
functions — into a small tree.

It is not a complete bash grammar and does not try to be. A command it
cannot parse raises :class:`ParseError`, and callers treat that as "no
opinion": a guard built on this must never reject what it did not
understand.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional, Union


class ParseError(ValueError):
    pass


@dataclass
class Word:
    raw: str                    # the source text
    value: str                  # quotes removed; expansions left as source text
    subs: list = field(default_factory=list)   # Seq per $(...) / `...` / <(...)
    quoted: bool = False
    expands: bool = False       # $var, ${...}, $(...), `...` outside single quotes


@dataclass
class Redirect:
    op: str
    target: Optional[Word] = None
    heredoc: Optional[str] = None       # body, for << and <<-
    heredoc_quoted: bool = False


@dataclass
class Simple:
    words: list
    assigns: list                       # [(name, Word)]
    redirects: list
    raw: str = ""

    @property
    def argv(self) -> list[str]:
        return [w.value for w in self.words]


@dataclass
class Pipeline:
    cmds: list
    negated: bool = False


@dataclass
class AndOr:
    parts: list                         # [Pipeline]
    ops: list                           # "&&" / "||" between parts


@dataclass
class Seq:
    items: list                         # [AndOr]
    seps: list                          # separator after each: ";" "&" "\n" ""


@dataclass
class Loop:
    kind: str                           # "while" / "until"
    cond: Seq
    body: Seq
    redirects: list = field(default_factory=list)
    raw: str = ""


@dataclass
class If:
    clauses: list                       # [(cond Seq, body Seq)]
    orelse: Optional[Seq] = None
    redirects: list = field(default_factory=list)


@dataclass
class For:
    var: str
    words: Optional[list]               # None for the arithmetic form
    body: Seq
    redirects: list = field(default_factory=list)
    raw: str = ""


@dataclass
class Group:
    body: Seq
    subshell: bool
    redirects: list = field(default_factory=list)


@dataclass
class Case:
    word: Word
    arms: list                          # [(patterns str, Seq)]
    redirects: list = field(default_factory=list)


@dataclass
class FuncDef:
    name: str
    body: object


Node = Union[Simple, Loop, If, For, Group, Case, FuncDef]

_META = set(" \t\n;&|()<>")
_RESERVED_END = {"do", "done", "then", "fi", "elif", "else", "esac", "}", "in"}
_REDIR = re.compile(r"(\d+|\{[A-Za-z_]\w*\})?(<<<|<<-|<<|&>>|&>|>>|>\||<&|>&|<>|<|>)")
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\[[^\]]*\])?\+?=")
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def parse(src: str) -> Seq:
    """Parse ``src``; raise :class:`ParseError` on anything unsupported."""
    p = _Parser(src)
    seq = p.parse_seq(set())
    p.skip_ws(newlines=True)
    if p.i < len(p.s):
        raise ParseError(f"unexpected {p.s[p.i:p.i + 20]!r} at {p.i}")
    if p.pending:
        raise ParseError("unterminated heredoc")
    return seq


def try_parse(src: str) -> Optional[Seq]:
    try:
        return parse(src)
    except (ParseError, RecursionError, IndexError):
        return None


class _Parser:
    def __init__(self, src: str):
        self.s = src
        self.i = 0
        self.pending: list[tuple[Redirect, str, bool]] = []

    # ---------------------------------------------------------- lexing

    def at_end(self) -> bool:
        return self.i >= len(self.s)

    def ch(self, k: int = 0) -> str:
        j = self.i + k
        return self.s[j] if j < len(self.s) else ""

    def skip_ws(self, newlines: bool = False) -> None:
        while not self.at_end():
            c = self.ch()
            if c in " \t":
                self.i += 1
            elif c == "\\" and self.ch(1) == "\n":
                self.i += 2
            elif c == "#":
                while not self.at_end() and self.ch() != "\n":
                    self.i += 1
            elif c == "\n" and newlines:
                self.newline()
            else:
                return

    def newline(self) -> None:
        assert self.ch() == "\n"
        self.i += 1
        if self.pending:
            self.read_heredocs()

    def read_heredocs(self) -> None:
        pending, self.pending = self.pending, []
        for redir, delim, strip in pending:
            lines = []
            while True:
                if self.at_end():
                    # bash accepts EOF as the end of a heredoc, with a warning
                    break
                j = self.s.find("\n", self.i)
                line = self.s[self.i:] if j < 0 else self.s[self.i:j]
                self.i = len(self.s) if j < 0 else j + 1
                check = line.lstrip("\t") if strip else line
                if check == delim:
                    break
                lines.append(line)
            redir.heredoc = "\n".join(lines) + ("\n" if lines else "")

    def peek_op(self) -> str:
        s, i = self.s, self.i
        for op in ("&&", "||", ";;&", ";;", ";&", "|&", "|", ";", "&", "(", ")", "\n"):
            if s.startswith(op, i):
                return op
        return ""

    def peek_word(self) -> Optional[str]:
        """The next plain word, without consuming it (reserved-word check)."""
        m = re.compile(r"[^\s;&|()<>'\"\\$`]+").match(self.s, self.i)
        if not m:
            return None
        end = m.end()
        if end < len(self.s) and self.s[end] not in _META:
            return None
        return m.group(0)

    def expect_word(self, w: str) -> None:
        self.skip_ws(newlines=True)
        if self.peek_word() != w:
            raise ParseError(f"expected {w!r} at {self.i}: {self.s[self.i:self.i + 20]!r}")
        self.i += len(w)

    # ---------------------------------------------------------- words

    def scan_balanced(self, open_: str, close: str) -> str:
        """From an ``open_`` at self.i, return text through the matching
        ``close``, quote-aware. Used for ${...}, $((...)), arrays, extglob."""
        start = self.i
        depth = 0
        while not self.at_end():
            c = self.ch()
            if c == "\\":
                self.i += 2
                continue
            if c == "'" and open_ != "{":
                j = self.s.find("'", self.i + 1)
                if j < 0:
                    raise ParseError("unterminated quote")
                self.i = j + 1
                continue
            if c == '"':
                self.read_dquote(Word("", ""))
                continue
            if c == open_:
                depth += 1
            elif c == close:
                depth -= 1
                if depth == 0:
                    self.i += 1
                    return self.s[start:self.i]
            self.i += 1
        raise ParseError(f"unbalanced {open_}")

    def read_cmdsub(self, w: Word) -> None:
        """At ``$(`` — parse the inner list on this same stream."""
        start = self.i
        self.i += 2
        seq = self.parse_seq({")"})
        self.skip_ws(newlines=True)
        if self.ch() != ")":
            raise ParseError("unterminated $(")
        self.i += 1
        w.subs.append(seq)
        w.value += self.s[start:self.i]
        w.expands = True

    def read_backtick(self, w: Word) -> None:
        start = self.i
        self.i += 1
        buf = []
        while True:
            if self.at_end():
                raise ParseError("unterminated backtick")
            c = self.ch()
            if c == "\\" and self.ch(1) in "`\\$":
                buf.append(self.ch(1))
                self.i += 2
                continue
            if c == "`":
                self.i += 1
                break
            buf.append(c)
            self.i += 1
        w.subs.append(parse("".join(buf)))
        w.value += self.s[start:self.i]
        w.expands = True

    def read_dollar(self, w: Word, in_dquote: bool = False) -> None:
        nxt = self.ch(1)
        if in_dquote and nxt in "'\"":
            w.value += "$"              # `"...$"` / `"$'"`: a literal dollar
            self.i += 1
            return
        if nxt == "(":
            if self.ch(2) == "(":
                start = self.i
                self.i += 1
                self.scan_balanced("(", ")")
                w.value += self.s[start:self.i]
                w.expands = True
            else:
                self.read_cmdsub(w)
        elif nxt == "{":
            start = self.i
            self.i += 1
            self.scan_balanced("{", "}")
            w.value += self.s[start:self.i]
            w.expands = True
        elif nxt == "'":
            self.i += 2
            buf = []
            while True:
                if self.at_end():
                    raise ParseError("unterminated $'")
                c = self.ch()
                if c == "\\":
                    e = self.ch(1)
                    buf.append({"n": "\n", "t": "\t", "\\": "\\", "'": "'",
                                '"': '"', "e": "\x1b", "a": "\a", "r": "\r",
                                "0": "\0"}.get(e, "\\" + e))
                    self.i += 2
                    continue
                if c == "'":
                    self.i += 1
                    break
                buf.append(c)
                self.i += 1
            w.value += "".join(buf)
            w.quoted = True
        elif nxt == '"':
            self.i += 1
        else:
            m = _NAME.match(self.s, self.i + 1)
            if m:
                w.value += "$" + m.group(0)
                self.i = m.end()
                w.expands = True
            elif nxt and nxt in "@*#?$!-0123456789":
                w.value += "$" + nxt
                self.i += 2
                w.expands = True
            else:
                w.value += "$"
                self.i += 1

    def read_dquote(self, w: Word) -> None:
        assert self.ch() == '"'
        self.i += 1
        w.quoted = True
        while True:
            if self.at_end():
                raise ParseError("unterminated double quote")
            c = self.ch()
            if c == '"':
                self.i += 1
                return
            if c == "\\":
                e = self.ch(1)
                if e in '$`"\\':
                    w.value += e
                elif e == "\n":
                    pass
                else:
                    w.value += "\\" + e
                self.i += 2
            elif c == "$":
                self.read_dollar(w, in_dquote=True)
            elif c == "`":
                self.read_backtick(w)
            else:
                w.value += c
                self.i += 1

    def read_word(self) -> Optional[Word]:
        start = self.i
        w = Word("", "")
        while not self.at_end():
            c = self.ch()
            if c in _META:
                if c in "<>" and self.ch(1) == "(" and self.i == start:
                    # process substitution <(...) / >(...)
                    self.i += 1
                    sub = Word("", "")
                    self.read_cmdsub_at_paren(sub)
                    w.subs += sub.subs
                    w.value += c + sub.value
                    w.expands = True
                    continue
                if c == "(" and (_ASSIGN.match(w.value) and w.value.endswith("=")
                                 or (w.value and w.value[-1] in "@!+*?")):
                    w.value += self.scan_balanced("(", ")")
                    continue
                break
            if c == "\\":
                if self.ch(1) == "\n":
                    self.i += 2
                    continue
                w.value += self.ch(1)
                w.quoted = True
                self.i += 2
            elif c == "'":
                j = self.s.find("'", self.i + 1)
                if j < 0:
                    raise ParseError("unterminated single quote")
                w.value += self.s[self.i + 1:j]
                w.quoted = True
                self.i = j + 1
            elif c == '"':
                self.read_dquote(w)
            elif c == "$":
                self.read_dollar(w)
            elif c == "`":
                self.read_backtick(w)
            else:
                w.value += c
                self.i += 1
        if self.i == start:
            return None
        w.raw = self.s[start:self.i]
        return w

    def read_cmdsub_at_paren(self, w: Word) -> None:
        """At the ``(`` of ``<(`` — same as $( with a one-char prefix."""
        start = self.i
        self.i += 1
        seq = self.parse_seq({")"})
        self.skip_ws(newlines=True)
        if self.ch() != ")":
            raise ParseError("unterminated process substitution")
        self.i += 1
        w.subs.append(seq)
        w.value += self.s[start:self.i]

    def read_redirect(self) -> Optional[Redirect]:
        m = _REDIR.match(self.s, self.i)
        if not m:
            return None
        # "2>" is a redirect only when the digits are glued to the operator
        op = m.group(2)
        if op in ("<", ">") and self.s.startswith("(", m.end()):
            return None                 # process substitution
        self.i = m.end()
        self.skip_ws()
        if op in ("<<", "<<-"):
            target = self.read_word()
            if target is None:
                raise ParseError("heredoc without delimiter")
            r = Redirect(op, target, heredoc="", heredoc_quoted=target.quoted)
            self.pending.append((r, target.value, op == "<<-"))
            return r
        target = self.read_word()
        if target is None and op not in (">&", "<&"):
            raise ParseError(f"redirect {op} without target")
        return Redirect(op, target)

    # ---------------------------------------------------------- grammar

    def at_terminator(self, terms: set) -> bool:
        if self.at_end():
            return True
        op = self.peek_op()
        if op == ")" and ")" in terms:
            return True
        if op in (";;", ";&", ";;&") and ";;" in terms:
            return True
        w = self.peek_word()
        return w is not None and w in terms

    def parse_seq(self, terms: set) -> Seq:
        items, seps = [], []
        while True:
            self.skip_ws(newlines=True)
            while self.ch() == ";" and not self.peek_op().startswith(";;"):
                self.i += 1
                self.skip_ws(newlines=True)
            if self.at_terminator(terms) or self.peek_op() == ")":
                break
            items.append(self.parse_andor(terms))
            self.skip_ws()
            op = self.peek_op()
            if op in (";", "&"):
                self.i += 1
                seps.append(op)
            elif op == "\n":
                self.newline()
                seps.append("\n")
            else:
                seps.append("")
                self.skip_ws(newlines=True)
                if not self.at_terminator(terms) and self.peek_op() != ")":
                    raise ParseError(f"unexpected {self.s[self.i:self.i + 20]!r}")
                break
        return Seq(items, seps)

    def parse_andor(self, terms: set) -> AndOr:
        parts = [self.parse_pipeline(terms)]
        ops = []
        while True:
            self.skip_ws()
            op = self.peek_op()
            if op not in ("&&", "||"):
                return AndOr(parts, ops)
            self.i += 2
            ops.append(op)
            self.skip_ws(newlines=True)
            parts.append(self.parse_pipeline(terms))

    def parse_pipeline(self, terms: set) -> Pipeline:
        self.skip_ws()
        negated = False
        if self.peek_word() == "!":
            self.i += 1
            negated = True
            self.skip_ws()
        if self.peek_word() == "time":
            self.i += 4
            self.skip_ws()
        cmds = [self.parse_command(terms)]
        while True:
            self.skip_ws()
            op = self.peek_op()
            if op not in ("|", "|&"):
                return Pipeline(cmds, negated)
            self.i += len(op)
            self.skip_ws(newlines=True)
            cmds.append(self.parse_command(terms))

    def trailing_redirects(self) -> list:
        out = []
        while True:
            self.skip_ws()
            r = self.read_redirect()
            if r is None:
                return out
            out.append(r)

    def parse_command(self, terms: set):
        self.skip_ws()
        start = self.i
        if self.s.startswith("((", self.i):
            text = self.scan_balanced("(", ")")
            return Simple([Word(text, text)], [], self.trailing_redirects(), text)
        if self.ch() == "(":
            self.i += 1
            body = self.parse_seq({")"})
            self.skip_ws(newlines=True)
            if self.ch() != ")":
                raise ParseError("unterminated (")
            self.i += 1
            return Group(body, True, self.trailing_redirects())
        w = self.peek_word()
        if w == "{":
            self.i += 1
            body = self.parse_seq({"}"})
            self.expect_word("}")
            return Group(body, False, self.trailing_redirects())
        if w in ("while", "until"):
            self.i += len(w)
            cond = self.parse_seq({"do"})
            self.expect_word("do")
            body = self.parse_seq({"done"})
            self.expect_word("done")
            return Loop(w, cond, body, self.trailing_redirects(), self.s[start:self.i])
        if w == "if":
            self.i += 2
            clauses = []
            orelse = None
            while True:
                cond = self.parse_seq({"then"})
                self.expect_word("then")
                body = self.parse_seq({"elif", "else", "fi"})
                clauses.append((cond, body))
                self.skip_ws(newlines=True)
                nxt = self.peek_word()
                if nxt == "elif":
                    self.i += 4
                    continue
                if nxt == "else":
                    self.i += 4
                    orelse = self.parse_seq({"fi"})
                self.expect_word("fi")
                return If(clauses, orelse, self.trailing_redirects())
        if w in ("for", "select"):
            self.i += len(w)
            self.skip_ws()
            if self.s.startswith("((", self.i):
                self.scan_balanced("(", ")")
                var, words = "", None
            else:
                name = self.read_word()
                if name is None:
                    raise ParseError("for without variable")
                var = name.value
                words = []
                self.skip_ws(newlines=True)
                if self.peek_word() == "in":
                    self.i += 2
                    while True:
                        self.skip_ws()
                        if self.peek_op() in (";", "\n") or self.at_end():
                            break
                        wd = self.read_word()
                        if wd is None:
                            break
                        words.append(wd)
            self.skip_ws()
            if self.ch() == ";":
                self.i += 1
            self.expect_word("do")
            body = self.parse_seq({"done"})
            self.expect_word("done")
            return For(var, words, body, self.trailing_redirects(), self.s[start:self.i])
        if w == "case":
            self.i += 4
            self.skip_ws()
            word = self.read_word()
            self.expect_word("in")
            arms = []
            while True:
                self.skip_ws(newlines=True)
                if self.peek_word() == "esac":
                    self.i += 4
                    break
                if self.ch() == "(":
                    self.i += 1
                pstart = self.i
                depth = 0
                while not self.at_end():
                    c = self.ch()
                    if c in "'\"":
                        self.read_word()
                        continue
                    if c == "\\":
                        self.i += 2
                        continue
                    if c == "(":
                        depth += 1
                    elif c == ")":
                        if depth == 0:
                            break
                        depth -= 1
                    self.i += 1
                pats = self.s[pstart:self.i].strip()
                if self.ch() != ")":
                    raise ParseError("bad case pattern")
                self.i += 1
                body = self.parse_seq({";;", "esac"})
                arms.append((pats, body))
                self.skip_ws(newlines=True)
                op = self.peek_op()
                if op in (";;", ";&", ";;&"):
                    self.i += len(op)
            return Case(word, arms, self.trailing_redirects())
        if w == "function":
            self.i += 8
            self.skip_ws()
            name = self.read_word()
            self.skip_ws()
            if self.s.startswith("()", self.i):
                self.i += 2
            self.skip_ws(newlines=True)
            return FuncDef(name.value if name else "", self.parse_command(terms))
        if w == "[[":
            j = self.s.find("]]", self.i)
            if j < 0:
                raise ParseError("unterminated [[")
            text = self.s[self.i:j + 2]
            self.i = j + 2
            words = [Word(t, t.strip("'\"")) for t in text.split()]
            return Simple(words, [], self.trailing_redirects(), text)
        return self.parse_simple(start)

    def parse_simple(self, start: int):
        words, assigns, redirects = [], [], []
        while True:
            self.skip_ws()
            if self.at_end():
                break
            r = self.read_redirect()
            if r is not None:
                redirects.append(r)
                continue
            if self.ch() in "<>" and self.ch(1) == "(":
                wd = self.read_word()
                words.append(wd)
                continue
            if self.ch() in _META:
                if (self.ch() == "(" and words and len(words) == 1 and not assigns
                        and self.s.startswith("()", self.i)):
                    self.i += 2
                    self.skip_ws(newlines=True)
                    return FuncDef(words[0].value, self.parse_command(set()))
                break
            wd = self.read_word()
            if wd is None:
                break
            if not words and _ASSIGN.match(wd.raw):
                name, _, _ = wd.raw.partition("=")
                val = Word(wd.raw, wd.value.split("=", 1)[1] if "=" in wd.value else "",
                           wd.subs, wd.quoted, wd.expands)
                assigns.append((name.rstrip("+"), val))
                continue
            words.append(wd)
        if not words and not assigns and not redirects:
            raise ParseError(f"empty command at {self.i}: {self.s[self.i:self.i + 20]!r}")
        return Simple(words, assigns, redirects, self.s[start:self.i])


# ------------------------------------------------------------ traversal

def iter_simple(node) -> list:
    """Every Simple command in ``node``, depth-first, not descending into
    command substitutions (use each Word's ``subs`` for those)."""
    out: list = []

    def walk(n):
        if isinstance(n, Seq):
            for it in n.items:
                walk(it)
        elif isinstance(n, AndOr):
            for p in n.parts:
                walk(p)
        elif isinstance(n, Pipeline):
            for c in n.cmds:
                walk(c)
        elif isinstance(n, Simple):
            out.append(n)
        elif isinstance(n, Loop):
            walk(n.cond)
            walk(n.body)
        elif isinstance(n, If):
            for c, b in n.clauses:
                walk(c)
                walk(b)
            if n.orelse:
                walk(n.orelse)
        elif isinstance(n, For):
            walk(n.body)
        elif isinstance(n, Group):
            walk(n.body)
        elif isinstance(n, Case):
            for _, b in n.arms:
                walk(b)
        elif isinstance(n, FuncDef):
            walk(n.body)
    walk(node)
    return out
