"""
rtk command rewriter.

Shells out to ``rtk rewrite <cmd>`` (from https://github.com/rtk-ai/rtk)
to transparently substitute verbose ``find`` / ``grep`` / ``git log`` /
``du`` style commands with terser rtk equivalents. rtk claims 60-90%
token savings on matching commands.

**This module is optional.** It depends on an external binary (``rtk``
>= 0.23.0) which may not be installed. All failure modes are benign:

  * ``rtk`` not found → return None (pass-through)
  * ``rtk`` version too old → return None
  * ``rtk rewrite`` exits non-zero → return None
  * rewritten == original → return None
  * subprocess times out → return None

Ported from rtfpessoa/code-factory's hooks/rtk-rewrite.sh:
https://github.com/rtfpessoa/code-factory/blob/main/hooks/rtk-rewrite.sh

Note: there is a name collision — the rtk we want is
https://github.com/rtk-ai/rtk (token-saving CLI proxy), NOT the Rust
crate ``rtk`` for FFI type generation. If ``rtk --version`` shows a
version like 0.1.x without a ``rewrite`` subcommand, you have the
wrong rtk.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from typing import Optional

log = logging.getLogger("claude_hooks.rtk_rewrite")

# Cache the version probe keyed by binary path (and resolved fullpath) so we
# don't re-invoke ``rtk --version`` on every tool call. Keyed-by-binary so
# differing ``rtk_bin`` configs across projects don't pollute each other.
# Reset by tests via :func:`reset_rtk_cache`.
_CACHED_STATE: dict[str, dict] = {}
_MIN_VERSION = (0, 23, 0)


def reset_rtk_cache() -> None:
    """Reset the cached rtk availability state. For tests."""
    global _CACHED_STATE
    _CACHED_STATE = {}


def rewrite_command(
    command: str,
    *,
    timeout: float = 3.0,
    min_version: Optional[tuple[int, int, int]] = None,
    rtk_bin: str = "rtk",
) -> Optional[str]:
    """Return the rtk-rewritten command, or None if no rewrite applies.

    Returns None (no rewrite) on every failure mode — missing binary,
    old version, subprocess error, timeout, unchanged output.
    """
    if not command or not command.strip():
        return None

    state = _probe_rtk(rtk_bin=rtk_bin, min_version=min_version or _MIN_VERSION)
    if not state.get("usable"):
        return None

    rtk_path = state["path"]
    try:
        result = subprocess.run(
            [rtk_path, "rewrite", command],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        log.debug("rtk rewrite subprocess failed: %s", e)
        return None

    if result.returncode != 0:
        # rtk exits 1 when no rewrite applies — this is normal, not an error.
        return None

    rewritten = (result.stdout or "").strip()
    if not rewritten or rewritten == command.strip():
        return None

    return rewritten


def _probe_rtk(
    *,
    rtk_bin: str,
    min_version: tuple[int, int, int],
) -> dict:
    """Return cached {'usable': bool, 'path': str, 'version': (maj,min,pat)}.

    Runs ``rtk --version`` once per (binary, min_version) pair and caches
    the result. Cache key includes min_version so a stricter config can't
    inherit a "usable" verdict from a looser one.
    """
    cache_key = f"{rtk_bin}@{'.'.join(map(str, min_version))}"
    cached = _CACHED_STATE.get(cache_key)
    if cached is not None:
        return cached

    state: dict = {"usable": False, "path": "", "version": None, "reason": ""}

    path = shutil.which(rtk_bin)
    if not path:
        state["reason"] = "not found on PATH"
        _CACHED_STATE[cache_key] = state
        log.debug("rtk not on PATH — hook will pass through silently")
        return state

    state["path"] = path
    try:
        result = subprocess.run(
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        state["reason"] = f"version probe failed: {e}"
        _CACHED_STATE[cache_key] = state
        return state

    version = _parse_version(result.stdout or result.stderr or "")
    state["version"] = version

    if version is None:
        state["reason"] = "could not parse rtk --version output"
        _CACHED_STATE[cache_key] = state
        log.warning("rtk at %s: could not parse version — hook disabled", path)
        return state

    if version < min_version:
        state["reason"] = (
            f"rtk {'.'.join(map(str, version))} is older than required "
            f"{'.'.join(map(str, min_version))} (need 'rtk rewrite' subcommand)"
        )
        _CACHED_STATE[cache_key] = state
        log.warning(
            "rtk %s at %s is too old (need >= %s); hook disabled. Upgrade or "
            "remove unrelated 'rtk' binaries (see docs).",
            ".".join(map(str, version)),
            path,
            ".".join(map(str, min_version)),
        )
        return state

    state["usable"] = True
    _CACHED_STATE[cache_key] = state
    log.info("rtk %s usable at %s", ".".join(map(str, version)), path)
    return state


def _parse_version(text: str) -> Optional[tuple[int, int, int]]:
    """Extract (major, minor, patch) from any string like ``rtk 1.2.3``."""
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", text)
    if not m:
        return None
    try:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def build_rewrite_response(tool_input: dict, rewritten: str) -> dict:
    """Build the Claude Code PreToolUse ``allow`` response with updatedInput."""
    updated = dict(tool_input)
    updated["command"] = rewritten
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": "RTK auto-rewrite (token savings)",
            "updatedInput": updated,
        }
    }
