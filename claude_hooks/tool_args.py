"""Tool arguments read the way a model sends them, where exactly one reading
exists (plan D4; Cerebriline ``core/src/extensions/tools/schemas.ts``).

Each rule takes a value the schema would refuse and that can mean only one
thing:

boolean   ``"true"`` / ``"false"`` (any case, trimmed). Measured in the fork:
          a model sent ``line_numbers: "false"``. Here it was worse than a
          refusal — ``bool("false")`` is ``True``, so ``grep`` turned
          case-insensitive when told not to.
integer   a string of digits (``"108"``), or a float with no fraction.
number    a numeric string.
array     a single string where a list is asked for becomes a one-entry list
          (``paths: "src/a.py"``); a string that opens ``["`` and parses as
          a JSON list of strings is that list. One that opens ``["`` and does
          not parse is refused by name, never searched for as literal text.
object    the whole argument object sent as a JSON string (encoded twice).

Nothing else is guessed: a comma-separated string stays one string (a path
may hold a comma), ``"yes"`` stays a string, and a truncated payload is
never completed — the fork once saw a "repair" replace 14,127 bytes with 572.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional, Union

MALFORMED_ARRAY_MESSAGE = (
    "error: `{key}` looks like a JSON array sent as a string that did not "
    "survive the trip — it starts with `[` but does not parse ({why}). Send "
    "the field as an actual array of strings, one entry per item, rather "
    "than as text.")

_INT = re.compile(r"^\s*[-+]?\d+\s*$")
_NUM = re.compile(r"^\s*[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?\s*$")
_ARRAY_TEXT = re.compile(r"^\s*\[\s*[\"']")


def parse_arguments(raw: Any) -> Union[dict, str]:
    """The argument object, or an ``error: …`` string for the model."""
    if isinstance(raw, dict):
        return raw
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return {}
    try:
        args = json.loads(raw)
    except (TypeError, ValueError) as e:
        return f"error: tool arguments not valid JSON: {e}"
    if isinstance(args, str):
        # Encoded twice: the object, as a JSON string.
        try:
            inner = json.loads(args)
        except ValueError:
            inner = None
        if isinstance(inner, dict):
            return inner
    if not isinstance(args, dict):
        return "error: tool arguments must be a JSON object"
    return args


def _types(spec: dict) -> set[str]:
    t = spec.get("type")
    if isinstance(t, list):
        return {x for x in t if isinstance(x, str)}
    return {t} if isinstance(t, str) else set()


def _coerce_value(key: str, value: Any, spec: dict) -> tuple[Any, Optional[str]]:
    types = _types(spec)
    if not types or value is None:
        return value, None
    if isinstance(value, str):
        if "string" in types:
            return value, None
        if "boolean" in types:
            low = value.strip().lower()
            if low in ("true", "false"):
                return low == "true", None
        if "integer" in types and _INT.match(value):
            return int(value), None
        if "number" in types and _NUM.match(value):
            n = float(value)
            return (int(n) if n.is_integer() and _INT.match(value) else n), None
        if "array" in types:
            if _ARRAY_TEXT.match(value):
                try:
                    parsed = json.loads(value)
                except ValueError as e:
                    return value, MALFORMED_ARRAY_MESSAGE.format(key=key, why=e)
                if isinstance(parsed, list) and all(isinstance(x, str)
                                                    for x in parsed):
                    return parsed, None
                return value, None
            items = spec.get("items") or {}
            if not _types(items) or "string" in _types(items):
                return [value], None
        return value, None
    if (isinstance(value, float) and "integer" in types
            and "number" not in types and value.is_integer()):
        return int(value), None
    return value, None


def coerce(args: dict, parameters: Optional[dict]) -> Union[dict, str]:
    """``args`` with each property read against its schema type, or an
    ``error: …`` string naming the one field that cannot be read."""
    props = (parameters or {}).get("properties") or {}
    if not props or not isinstance(args, dict):
        return args
    out = dict(args)
    for key, spec in props.items():
        if key not in out or not isinstance(spec, dict):
            continue
        value, refused = _coerce_value(key, out[key], spec)
        if refused:
            return refused
        out[key] = value
    return out


__all__ = ["parse_arguments", "coerce", "MALFORMED_ARRAY_MESSAGE"]
