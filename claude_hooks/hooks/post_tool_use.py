"""PostToolUse handler — IDE-style diagnostics fed back to the next turn.

Closes the "I didn't notice the import error until I ran the code" gap
that plagues LLM-driven coding the way it doesn't plague humans in
VSCode/JetBrains. Right after Claude Code applies an Edit / Write /
MultiEdit, this handler runs a fast static checker on the modified file
and emits the result as ``hookSpecificOutput.additionalContext`` so the
model sees the diagnostics in the very next prompt — before it claims
the change is done or moves on to the next file.

Default checker: ``ruff`` (Python). Single binary, ~50 ms cold,
catches most "trivial mistakes" — undefined names, unused imports,
import sort, missing ``__init__``, accidental ``print`` left in,
syntax errors, unreachable code, etc. We deliberately do *not* run
pyright/mypy here: they add 1-3 s per edit and surface a different
class of problem (types/arity) that we'd rather defer to a future
opt-in stage. Start with the cheap layer; only escalate if the
cheap layer isn't catching enough.

Other languages can be added later (gofmt+go vet for Go,
rust-analyzer/cargo check for Rust, etc.) under their own boolean
flags. For now: Python only, on by default.

Config (``hooks.post_tool_use``):

    "enabled": true,
    "ruff_enabled": true,
    "ruff_path": null,                      # auto-detect; absolute path overrides
    "ruff_args": ["check", "--output-format=concise", "--quiet"],
    "ruff_extensions": [".py"],
    "ruff_timeout": 5.0,
    "max_diagnostics": 50,                  # truncate at this many lines
    "log_invocations": false,               # debug-level by default
    "toml_comment_advisor_enabled": true,   # remind to add # comments on TOML edits
    "toml_comment_advisor_paths": [".claude-hooks/", "lsp-engine.toml"]

Failure modes (all silent — never block the user's next turn):
- ``ruff`` not found on PATH and no ``ruff_path``: log warning, skip.
- File doesn't exist (already moved/deleted by the edit): skip.
- Timeout: log warning, skip.
- Non-zero exit with stderr but no stdout: log + skip (likely a ruff
  config error, not a user-fixable diagnostic).
- Non-zero exit WITH stdout: that *is* the diagnostic — emit it.
  Ruff returns non-zero on lint hits by design.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from typing import Optional

from claude_hooks.providers import Provider

log = logging.getLogger("claude_hooks.hooks.post_tool_use")

# Tools that produce a file-path on success. Read tool calls don't
# change anything, so we ignore them.
_FILE_EDITING_TOOLS = {"Edit", "Write", "MultiEdit"}

# ``--no-cache`` is critical: the daemon runs with
# ``ReadWritePaths=/root/.claude`` so it can't write a ``.ruff_cache``
# alongside the project files. Skipping the cache costs single-digit
# milliseconds per invocation on a single file and avoids the
# "Failed to create temporary file" stderr that otherwise fires on
# every edit.
DEFAULT_RUFF_ARGS = ("check", "--output-format=concise", "--quiet", "--no-cache")
DEFAULT_RUFF_EXTENSIONS = (".py",)
DEFAULT_RUFF_TIMEOUT = 5.0
DEFAULT_MAX_DIAGNOSTICS = 50

# TOML files Claude is *most likely* hand-editing for behaviour
# control. We only nag on these — pyproject.toml and other
# tooling-owned TOMLs already have their own conventions and don't
# need a reminder. Match by substring against the relative path so
# both ``.claude-hooks/lsp-engine.toml`` (per-project) and a bare
# ``lsp-engine.toml`` at the project root get caught.
DEFAULT_TOML_ADVISOR_PATHS = (".claude-hooks/", "lsp-engine.toml")


def handle(*, event: dict, config: dict,
           providers: list[Provider]) -> Optional[dict]:  # noqa: ARG001
    hook_cfg = (config.get("hooks") or {}).get("post_tool_use") or {}
    if not hook_cfg.get("enabled", True):
        return None

    tool_name = event.get("tool_name", "")
    if tool_name not in _FILE_EDITING_TOOLS:
        return None

    tool_input = event.get("tool_input") or {}
    path = _extract_path(tool_input)
    if not path:
        return None
    # Resolve against cwd so a relative ``Edit`` path becomes the same
    # absolute path ruff sees.
    cwd = event.get("cwd") or os.getcwd()
    abs_path = path if os.path.isabs(path) else os.path.join(cwd, path)
    if not os.path.isfile(abs_path):
        log.debug("post_tool_use: %s does not exist (after edit?)", abs_path)
        return None

    blocks: list[str] = []

    # Ruff stage — Python files only by default.
    if hook_cfg.get("ruff_enabled", True):
        block = _run_ruff(abs_path, hook_cfg, project_cwd=cwd)
        if block:
            blocks.append(block)

    # TOML-comment advisor — when Claude edits a hand-edited TOML
    # config (``.claude-hooks/*.toml`` or ``lsp-engine.toml``),
    # remind it that the *whole point* of TOML over JSON is the
    # ``# reason: ...`` comment. Future sessions need the *why*
    # behind a non-default value, not just the *what*.
    if hook_cfg.get("toml_comment_advisor_enabled", True):
        block = _toml_comment_advisor(abs_path, hook_cfg, project_cwd=cwd)
        if block:
            blocks.append(block)

    # Future stages would tack on their own blocks here:
    #   gofmt / go vet for *.go
    #   cargo check for *.rs (cached, daemon-fronted)
    #   tsc --noEmit for *.ts (slow — gate behind opt-in)

    if not blocks:
        return None

    return {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": "\n\n".join(blocks),
        },
    }


def _extract_path(tool_input: dict) -> str:
    """Pull the file path out of the tool's input dict. Edit/Write use
    ``file_path``; MultiEdit uses ``file_path`` too (single file with
    multiple edits). Other tools may use ``path`` — fall back to it.
    """
    return (
        tool_input.get("file_path")
        or tool_input.get("path")
        or ""
    )


def _resolve_ruff(hook_cfg: dict) -> Optional[str]:
    """Locate the ``ruff`` binary, preferring an absolute path the user
    pinned in config (e.g. to point at the conda env's ruff regardless
    of the calling shell's PATH). Returns None if not found anywhere.
    """
    pinned = hook_cfg.get("ruff_path") or ""
    if pinned:
        if os.path.isfile(pinned) and os.access(pinned, os.X_OK):
            return pinned
        log.warning("post_tool_use: ruff_path %s is not an executable", pinned)
        return None
    # Common conda-env locations: prefer the one alongside the running
    # interpreter so we hit the venv's ruff before any global one.
    here = os.path.dirname(os.path.realpath(os.sys.executable))
    candidate = os.path.join(here, "ruff")
    if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
        return candidate
    found = shutil.which("ruff")
    if found:
        return found
    return None


def _run_ruff(path: str, hook_cfg: dict,
              project_cwd: str = "") -> Optional[str]:
    """Invoke ``ruff`` on ``path``. Returns a markdown block ready to
    drop into ``additionalContext``, or ``None`` when the file is
    clean / ruff is unavailable / the run failed for non-diagnostic
    reasons.

    ``project_cwd`` is the cwd from the hook event (the directory
    Claude Code was launched in), used to render the diagnostics
    header relative to the user's mental model rather than the
    daemon's process cwd.
    """
    extensions = tuple(hook_cfg.get("ruff_extensions") or DEFAULT_RUFF_EXTENSIONS)
    if not path.endswith(extensions):
        return None

    binary = _resolve_ruff(hook_cfg)
    if binary is None:
        log.warning(
            "post_tool_use: ruff not found on PATH and no ruff_path "
            "configured — skipping. Install with: pip install ruff",
        )
        return None

    args = list(hook_cfg.get("ruff_args") or DEFAULT_RUFF_ARGS)
    timeout = float(hook_cfg.get("ruff_timeout") or DEFAULT_RUFF_TIMEOUT)
    cmd = [binary, *args, path]

    if hook_cfg.get("log_invocations", False):
        log.info("post_tool_use: %s", " ".join(cmd))

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        log.warning("post_tool_use: ruff timed out after %.1fs on %s", timeout, path)
        return None
    except OSError as e:
        log.warning("post_tool_use: ruff invocation failed: %s", e)
        return None

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()

    # Clean file: ruff exits 0 with empty stdout. Nothing to inject.
    if proc.returncode == 0 and not stdout:
        return None

    # Hard failure (non-zero AND no stdout): this is ruff itself yelling
    # — config error, missing rule, etc. Log it for the operator but
    # don't pollute the model's context with our internal mess.
    if proc.returncode != 0 and not stdout:
        log.warning(
            "post_tool_use: ruff exit=%d with no stdout on %s; stderr=%s",
            proc.returncode, path, stderr[:300],
        )
        return None

    max_lines = int(hook_cfg.get("max_diagnostics") or DEFAULT_MAX_DIAGNOSTICS)
    lines = stdout.splitlines()
    truncated = False
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        truncated = True

    rel = _shorten_path(path, project_cwd)
    body = "\n".join(lines)
    suffix = (
        f"\n\n*(truncated to {max_lines} lines; rerun ruff manually for "
        "the full list)*" if truncated else ""
    )
    return (
        f"## Ruff diagnostics — `{rel}`\n\n"
        f"```\n{body}\n```{suffix}\n\n"
        f"_Fix these before claiming the edit is done. Suppress per-line "
        f"with `# noqa: <code>` only when intentional._"
    )


def _toml_comment_advisor(abs_path: str, hook_cfg: dict,
                          project_cwd: str = "") -> Optional[str]:
    """Inject a one-paragraph reminder when Claude edits a
    hand-edited TOML config, asking it to leave a ``# reason: ...``
    line above any non-default value.

    The advisor only fires for paths whose substring match one of
    ``toml_comment_advisor_paths`` — by default
    ``.claude-hooks/`` and ``lsp-engine.toml``. We deliberately do
    NOT advise on every TOML edit (pyproject.toml, cargo.toml, etc):
    those have their own conventions and a generic reminder there
    would be noise.
    """
    if not abs_path.endswith(".toml"):
        return None

    rel = _shorten_path(abs_path, project_cwd)
    needle_paths = tuple(
        hook_cfg.get("toml_comment_advisor_paths") or DEFAULT_TOML_ADVISOR_PATHS
    )
    rel_normalized = rel.replace("\\", "/")
    if not any(needle in rel_normalized for needle in needle_paths):
        return None

    return (
        f"## TOML edit reminder — `{rel}`\n\n"
        f"This is a hand-edited config file. The reason it's TOML "
        f"and not JSON is the `# ...` comment syntax — for any "
        f"non-default value you set or change, leave a one-line "
        f"comment above explaining **why** (e.g. "
        f"`# 50 instead of 200 because monorepo`). Future sessions "
        f"reading this file need the rationale, not just the value."
    )


def _shorten_path(abs_path: str, project_cwd: str = "") -> str:
    """Render ``/srv/.../project/foo/bar.py`` as the shortest meaningful
    suffix for the diagnostics header. Prefers ``project_cwd`` (the
    directory Claude Code was launched in, passed in the hook event)
    over the daemon's process cwd — otherwise paths come out as
    ``../../../../shared/dev/proj/foo.py`` which is harder to read.
    Falls back to absolute path if ``abs_path`` doesn't sit under
    ``project_cwd``.
    """
    if project_cwd:
        try:
            rel = os.path.relpath(abs_path, project_cwd)
        except ValueError:
            rel = abs_path
        else:
            # Don't return ``../...`` paths — those mean abs_path is
            # outside the project. Show the absolute path instead so
            # the user can see what's happening.
            if not rel.startswith(".."):
                return rel
        return abs_path
    try:
        return os.path.relpath(abs_path)
    except ValueError:
        return abs_path
