"""
PreToolUse handler — runs the safety scanner and the memory-warning
recall on every Bash/Edit/Write tool call.

Order of operations:

  1. rtk_rewrite (opt-in): if ``rtk`` (rtk-ai/rtk) is installed,
     substitute verbose ``find``/``grep``/``git log`` commands with
     terser equivalents. Emits ``updatedInput`` so subsequent stages
     see the rewritten command too.

  2. safety_scan (opt-in — defaults to running on rtk-rewritten
     commands): content-based pattern match on the (possibly rewritten)
     command. On match → emit ``permissionDecision: "ask"`` with reason
     and the rewritten command shown in ``updatedInput``. We never
     auto-deny. Defaults to auto-running after any rtk rewrite because
     the rtk ``allow`` decision would otherwise bypass the settings.json
     allow-list; users can opt out with ``rtk_scan_rewrites: false``.

  3. memory warn (opt-in): query the configured providers for past
     mistakes and inject them as ``additionalContext``. Advisory only.

All stages are disabled by default and enabled independently in
``config/claude-hooks.json`` under ``hooks.pre_tool_use``.

Safety-scan patterns are ported from rtfpessoa/code-factory's
``hooks/command-safety-scanner.sh``.
"""

from __future__ import annotations

import logging
from typing import Optional

from claude_hooks.providers import Provider

log = logging.getLogger("claude_hooks.hooks.pre_tool_use")


def handle(*, event: dict, config: dict, providers: list[Provider]) -> Optional[dict]:
    hook_cfg = (config.get("hooks") or {}).get("pre_tool_use") or {}
    tool_name = event.get("tool_name", "")
    tool_input = event.get("tool_input") or {}

    # Stages 1+2: rtk rewriter + safety scanner (Bash only).
    #
    # Order matters: rtk first, then safety_scan on the *rewritten* command.
    #
    # IMPORTANT SAFETY INVARIANT: when rtk produces a rewrite we WILL emit
    # ``permissionDecision`` — either "ask" (if safety_scan matched) or
    # "allow" (if clean). Either decision BYPASSES the ``~/.claude/
    # settings.json`` allow-list because Claude Code honours explicit
    # hook decisions. To keep the user-configured safety net intact, we
    # therefore run safety_scan on rtk-rewritten commands REGARDLESS of
    # the ``safety_scan_enabled`` flag. The flag still gates standalone
    # scanning (when rtk didn't rewrite) so users who only want rtk
    # don't get scanner prompts on the unchanged command paths.
    if tool_name == "Bash":
        cmd = (tool_input.get("command", "") or "").strip()
        effective_cmd = cmd
        rewritten_input: Optional[dict] = None

        if hook_cfg.get("rtk_rewrite_enabled", False) and cmd:
            rewrite = _run_rtk_rewrite_raw(cmd, hook_cfg)
            if rewrite:
                effective_cmd = rewrite
                rewritten_input = dict(tool_input)
                rewritten_input["command"] = rewrite

        # Safety scan applies when:
        #   (a) user explicitly enabled safety_scan, OR
        #   (b) rtk rewrote the command AND ``rtk_scan_rewrites`` is true
        #       (the default) — to prevent the allow-list bypass described
        #       above. Users who want rtk rewrites WITHOUT the safety
        #       scanner (accepting that rtk's allow overrides their
        #       settings.json rules) can set ``rtk_scan_rewrites: false``.
        run_scan = (
            hook_cfg.get("safety_scan_enabled", False)
            or (
                rewritten_input is not None
                and hook_cfg.get("rtk_scan_rewrites", True)
            )
        )
        if run_scan and effective_cmd:
            scan = _run_safety_scan_raw(effective_cmd, hook_cfg)
            if scan is not None:
                _name, reason = scan
                response = {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "ask",
                        "permissionDecisionReason": reason,
                    }
                }
                if rewritten_input is not None:
                    response["hookSpecificOutput"]["updatedInput"] = rewritten_input
                return response

        if rewritten_input is not None:
            # Safe after scan — auto-approve the rewrite.
            if hook_cfg.get("rtk_log_rewrites", False):
                log.info("rtk rewrote: %s -> %s", cmd[:120], effective_cmd[:120])
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                    "permissionDecisionReason": "RTK auto-rewrite (token savings)",
                    "updatedInput": rewritten_input,
                }
            }

    # Stage 2: memory-warn recall — only runs if the master toggle is on.
    if not hook_cfg.get("enabled", False):
        return None

    warn_tools = set(hook_cfg.get("warn_on_tools") or [])
    if warn_tools and tool_name not in warn_tools:
        return None

    probe = _probe_string(tool_name, tool_input)
    if not probe:
        return None

    # Port 5 from thedotmack/claude-mem — file-read gate. For Read /
    # Edit / MultiEdit on a file we already have memories about, skip
    # the pattern filter and always emit additionalContext so the
    # model sees prior observations before opening the file. Claude-
    # mem's original version uses permissionDecision:deny; we emit
    # advisory context (never block) — same safety net, less friction.
    file_read_gate = bool(hook_cfg.get("file_read_gate", False))
    gate_tools = set(hook_cfg.get("file_read_gate_tools")
                     or ["Read", "Edit", "MultiEdit"])
    gate_bypasses_patterns = (
        file_read_gate and tool_name in gate_tools
        and tool_name != "Bash"  # never skip patterns on Bash
    )

    patterns = hook_cfg.get("warn_on_patterns") or []
    if patterns and not gate_bypasses_patterns \
       and not any(p.lower() in probe.lower() for p in patterns):
        return None

    snippets: list[str] = []
    for provider in providers:
        try:
            mems = provider.recall(probe, k=3)
        except Exception as e:
            log.warning("provider %s recall failed: %s", provider.name, e)
            continue
        for m in mems:
            snippet = m.text.strip().splitlines()[0][:200]
            snippets.append(f"- ({provider.name}) {snippet}")

    if not snippets:
        return None

    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": (
                "## ⚠ Past memory matched this command\n\n"
                + "\n".join(snippets)
                + "\n\n_Hooks are advisory only — proceed if the context still warrants it._"
            ),
        }
    }


def _run_rtk_rewrite_raw(cmd: str, hook_cfg: dict) -> Optional[str]:
    """Attempt rtk rewrite on a Bash command. Returns the rewritten string or None."""
    try:
        from claude_hooks.rtk_rewrite import rewrite_command
    except Exception as e:
        log.debug("rtk_rewrite module import failed: %s", e)
        return None

    timeout = float(hook_cfg.get("rtk_timeout", 3.0))
    min_ver_cfg = hook_cfg.get("rtk_min_version") or "0.23.0"
    try:
        parts = tuple(int(p) for p in str(min_ver_cfg).split(".")[:3])
        if len(parts) < 3:
            parts = parts + (0,) * (3 - len(parts))
        min_version = parts  # type: ignore[assignment]
    except (ValueError, TypeError):
        min_version = (0, 23, 0)

    return rewrite_command(cmd, timeout=timeout, min_version=min_version)


def _run_safety_scan_raw(cmd: str, hook_cfg: dict) -> Optional[tuple[str, str]]:
    """Scan a Bash command. Returns (pattern_name, reason) on match, else None."""
    try:
        from claude_hooks.safety_scan import (
            compile_patterns,
            default_log_dir,
            log_match,
            scan_command,
        )
    except Exception as e:
        log.debug("safety_scan module import failed: %s", e)
        return None

    patterns = compile_patterns(
        extra=hook_cfg.get("safety_extra_patterns") or [],
        use_defaults=hook_cfg.get("safety_use_defaults", True),
    )
    match = scan_command(cmd, patterns)
    if not match:
        return None

    pattern_name, reason = match
    log.info("safety_scan matched %s on command: %s", pattern_name, cmd[:200])

    if hook_cfg.get("safety_log_enabled", True):
        log_dir_cfg = hook_cfg.get("safety_log_dir")
        from claude_hooks.config import expand_user_path
        log_dir = expand_user_path(log_dir_cfg) if log_dir_cfg else default_log_dir()
        retention = int(hook_cfg.get("safety_log_retention_days", 90))
        log_match(
            log_dir=log_dir,
            pattern_name=pattern_name,
            reason=reason,
            command=cmd,
            retention_days=retention,
        )

    return pattern_name, reason


def _probe_string(tool_name: str, tool_input: dict) -> str:
    if tool_name == "Bash":
        return tool_input.get("command", "")
    if tool_name in ("Edit", "Write", "MultiEdit"):
        return tool_input.get("file_path", "")
    if tool_name == "Read":
        return tool_input.get("file_path", "")
    return ""
