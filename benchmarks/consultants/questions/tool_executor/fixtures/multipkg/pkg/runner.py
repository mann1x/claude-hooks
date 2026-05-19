"""Pipeline runner — orchestrates the parser, validator, and
serializer to turn a config file into a runnable plan and then
into output bytes.

This is the package's entry point in normal usage; everything
else under ``pkg`` is a supporting helper.
"""

from __future__ import annotations

from . import parser, serializer, validator


def run(config_text: str) -> bytes:
    """Parse, validate, and serialize ``config_text``.

    Raises ``ValueError`` when validation fails.
    """
    ast = parser.parse(config_text)
    validator.validate(ast)
    return serializer.dump(ast)
