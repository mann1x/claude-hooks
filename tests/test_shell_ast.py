"""The bash parser behind the process guard.

It only has to be right about what the guard reads — argv after quote
removal, the text ssh sends, heredoc bodies, where loops and conditions
are — and it must refuse what it cannot parse rather than guess.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from claude_hooks.shell_ast import (  # noqa: E402
    For, If, Loop, ParseError, Simple, iter_simple, parse, try_parse,
)


def simple(src: str) -> Simple:
    return iter_simple(parse(src))[0]


class WordTests(unittest.TestCase):

    def test_quote_removal(self):
        self.assertEqual(simple("""pkill -f 'a b' "c d" e\\ f""").argv,
                         ["pkill", "-f", "a b", "c d", "e f"])

    def test_adjacent_quotes_concatenate(self):
        # the Bash tool's own escaping of a single quote
        self.assertEqual(simple("""echo 'it'"'"'s'""").argv, ["echo", "it's"])

    def test_dollar_before_closing_dquote_is_literal(self):
        # "…--no-warmup$" is a regex anchor, not bash's $"…" locale string
        self.assertEqual(simple('pkill -f "srv.*--no-warmup$"').argv[-1],
                         "srv.*--no-warmup$")

    def test_ansi_c_quoting(self):
        self.assertEqual(simple("echo $'a\\tb'").argv[-1], "a\tb")

    def test_command_substitution_is_parsed(self):
        w = simple('kill $(pgrep -f "x y")').words[1]
        self.assertTrue(w.expands)
        self.assertEqual(iter_simple(w.subs[0])[0].argv, ["pgrep", "-f", "x y"])

    def test_nested_substitution_and_backticks(self):
        w = simple('echo "$(echo $(pgrep a))" `pgrep b`').words
        self.assertEqual(len(w[1].subs), 1)
        self.assertEqual(iter_simple(w[2].subs[0])[0].argv, ["pgrep", "b"])

    def test_assignment_split_from_words(self):
        s = simple('P=$(pgrep -f x) Q=1 cmd')
        self.assertEqual([n for n, _ in s.assigns], ["P", "Q"])
        self.assertEqual(s.argv, ["cmd"])


class StructureTests(unittest.TestCase):

    def test_heredoc_body_is_attached_not_parsed(self):
        seq = parse("ssh h bash -s <<'EOF'\npkill -f zz\necho ok\nEOF\necho after")
        cmds = iter_simple(seq)
        self.assertEqual([c.argv[0] for c in cmds], ["ssh", "echo"])
        self.assertEqual(cmds[0].redirects[0].heredoc, "pkill -f zz\necho ok\n")
        self.assertTrue(cmds[0].redirects[0].heredoc_quoted)

    def test_loops_and_conditions(self):
        seq = parse("until ! pgrep -f x; do sleep 5; done")
        loop = seq.items[0].parts[0].cmds[0]
        self.assertIsInstance(loop, Loop)
        self.assertEqual(loop.kind, "until")
        self.assertTrue(loop.cond.items[0].parts[0].negated)

    def test_if_for_case(self):
        seq = parse("for p in $(pgrep x); do if [ $p != $$ ]; then kill $p; fi; done\n"
                    "case $s in failed|error) exit 1;; *) :;; esac")
        self.assertIsInstance(seq.items[0].parts[0].cmds[0], For)
        body = seq.items[0].parts[0].cmds[0].body
        self.assertIsInstance(body.items[0].parts[0].cmds[0], If)

    def test_separators_are_recorded(self):
        seq = parse("a; b & c\nd")
        self.assertEqual(seq.seps, [";", "&", "\n", ""])

    def test_pipelines_and_andor(self):
        a = parse("ps aux | grep x | awk '{print $2}' && echo ok || echo no").items[0]
        self.assertEqual(a.ops, ["&&", "||"])
        self.assertEqual(len(a.parts[0].cmds), 3)

    def test_double_bracket_keeps_operators(self):
        s = simple('[[ $x =~ (a|b) ]]')
        self.assertEqual(s.words[0].value, "[[")

    def test_function_definition(self):
        seq = parse('t() { timeout 45 tmux "$@"; }\nt ls')
        self.assertEqual(iter_simple(seq)[-1].argv, ["t", "ls"])


class RefusalTests(unittest.TestCase):
    """Unparseable input is refused, never guessed at."""

    def test_unterminated_quote(self):
        with self.assertRaises(ParseError):
            parse("echo 'oops")
        self.assertIsNone(try_parse("echo 'oops"))

    def test_bash_syntax_error_is_ours_too(self):
        # bash: syntax error near unexpected token `('
        self.assertIsNone(try_parse("print(f'{x}')"))


if __name__ == "__main__":
    unittest.main()
