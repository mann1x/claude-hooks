# python-very_hard-01-parser-combinator

Implement a minimal **parser-combinator** library with three
combinators (`Lit`, `Seq`, `Or`, `Many`) and a parsing entry
point that handles the regex-like grammar `a (b|c)*`.

## API

```python
class ParseError(Exception):
    """Raised when parsing fails. Has .pos giving the input
    offset where the failure occurred."""

def Lit(s: str):
    """Returns a parser that matches the literal string s
    exactly. Consumes len(s) characters on success."""

def Seq(*parsers):
    """Returns a parser that matches each child in order. Yields
    a tuple of their results."""

def Or(*parsers):
    """Returns a parser that tries each child in order; succeeds
    on the first one that consumes. The chosen child's result is
    the result of the Or."""

def Many(parser):
    """Returns a parser that matches zero-or-more of its child.
    Yields a list of the child's results."""

def parse(grammar, text: str):
    """Run the grammar against text. Returns the result on
    success, raises ParseError on failure or unconsumed input."""
```

## Required behavior

- Each `Lit`/`Seq`/`Or`/`Many` returns a **callable** (a parser).
  A parser takes `(text, pos) -> (value, new_pos)` and raises
  `ParseError(pos)` on failure.
- `Seq` fails on the first child that fails; no backtracking.
- `Or` is non-greedy at the alternative level: tries left-to-
  right, commits on first success. If a child fails BEFORE
  consuming any character, fall through to the next alternative.
  If a child fails AFTER consuming, propagate the failure
  (no backtracking).
- `Many` is greedy: keeps applying its child until it fails. If
  the failing attempt consumed any input, propagate failure.
- `parse` raises `ParseError` if any input remains unconsumed
  after the grammar matches.

## Spec test

The driver grammar `Seq(Lit("a"), Many(Or(Lit("b"), Lit("c"))))`
should:

- Parse `"a"` → `('a', [])`
- Parse `"ab"` → `('a', ['b'])`
- Parse `"abc"` → `('a', ['b', 'c'])`
- Parse `"acbcb"` → `('a', ['c', 'b', 'c', 'b'])`
- **Fail** on `"b"` (no leading `a`)
- **Fail** on `"abx"` (unconsumed `x`)
- **Fail** on `""` (no input)

## Constraints

- Single file `solution.py` exposing the four combinators +
  `ParseError` + `parse` at module top.
- Stdlib only; no `re`, no `parsy`, no `pyparsing`.
- No mutual recursion required — the grammar is fixed depth.

