"""Oracle for trivial-02-strlen. Imports the model-produced
``strlen.py`` from ``$CODER_SANDBOX``, verifies the function is
correct AND that the model honored the "no len()" constraint."""

import ast
import os
import sys
from pathlib import Path

SANDBOX = Path(os.environ["CODER_SANDBOX"])
sys.path.insert(0, str(SANDBOX))

strlen_mod = None
pytest_fail_reason = ""
try:
    import strlen as strlen_mod  # type: ignore[import]
except ImportError as e:
    pytest_fail_reason = str(e)


def _fn():
    if strlen_mod is None:
        raise AssertionError(
            f"strlen module not importable: {pytest_fail_reason}"
        )
    return getattr(strlen_mod, "strlen", None)


def test_basic_short():
    fn = _fn()
    assert callable(fn), "expected function `strlen` in strlen.py"
    assert fn("hello") == 5


def test_basic_long():
    fn = _fn()
    assert fn("the quick brown fox") == 19


def test_empty():
    fn = _fn()
    assert fn("") == 0


def test_single_char():
    fn = _fn()
    assert fn("x") == 1


def test_unicode_codepoint_count():
    fn = _fn()
    # 5 codepoints; built-in len() returns 5 too.
    assert fn("héllo") == 5


def test_constraint_no_len_call():
    """Read the source AST and reject any direct ``len(...)`` call.
    The model was told not to use the builtin; this catches the
    obvious bypass."""
    src_path = SANDBOX / "strlen.py"
    assert src_path.is_file(), f"strlen.py not found at {src_path}"
    tree = ast.parse(src_path.read_text(encoding="utf-8"))
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            # ``len(...)`` — bare name call.
            if isinstance(fn, ast.Name) and fn.id == "len":
                violations.append(f"line {node.lineno}: len() call")
    assert not violations, (
        "constraint violated: " + "; ".join(violations)
    )
