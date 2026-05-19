"""Tokenizer and parser for the demo configuration DSL.

Reads a tiny indentation-sensitive config language and emits an
AST. Used by ``pkg.runner`` to turn a config file into a runnable
pipeline.
"""

from __future__ import annotations


def tokenize(text: str) -> list[str]:
    """Split ``text`` into whitespace-separated tokens."""
    return text.split()


def parse(text: str) -> list[tuple[str, str]]:
    """Return a list of ``(key, value)`` pairs."""
    out: list[tuple[str, str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        out.append((k.strip(), v.strip()))
    return out
