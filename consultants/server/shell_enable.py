"""Whether a run gets the sandboxed shell, and how it says so.

Plan E5 (design 04 §5.8). The shell is **off by default** and turned on
only by:

* the request — ``"shell"`` in an ask or follow-up body (``--shell`` on
  the CLI): ``true`` / ``"read"`` / ``{"mode": "read", "net": "none"}``,
  or ``false`` to turn it off;
* the user knob ``[tools.shell] default = "always"``.

A follow-up that turns it on banks the setting on the consultancy root,
so later follow-ups inherit it until one sends ``false``; one that says
nothing inherits whatever the root holds. The setting is a dict
``{"mode", "net", "since"}`` — ``since`` is what the answer's footer
names, because a shell inherited silently is a wider reach nobody
re-approved (04 risk 9).

Off means ``None`` everywhere: no tool, no footer, no new record field —
a run without the shell is byte-identical to one before E.
"""
from __future__ import annotations

from typing import Any, Optional

MODES = ("read", "write")
NETS = ("none",)


class ShellRequestError(ValueError):
    """The request names a shell this engine cannot give."""


def parse(raw: Any) -> Optional[dict] | bool:
    """A body's ``shell`` value: ``None`` (not mentioned), ``False``
    (turn it off), or a validated ``{"mode", "net"}``."""
    if raw is None:
        return None
    if raw is False:
        return False
    if raw is True:
        raw = {}
    elif isinstance(raw, str):
        raw = {"mode": raw}
    if not isinstance(raw, dict):
        raise ShellRequestError(
            "shell must be true, false, a mode string or an object")
    mode = raw.get("mode") or "read"
    net = raw.get("net") or "none"
    scopes = raw.get("scopes")
    if mode not in MODES:
        raise ShellRequestError(f"shell mode must be one of {MODES}")
    if scopes is not None and (
            not isinstance(scopes, list) or not scopes
            or not all(isinstance(x, str) and x.strip() for x in scopes)):
        raise ShellRequestError("shell scopes must be a non-empty list of "
                                "paths")
    if scopes and mode != "write":
        raise ShellRequestError("shell scopes name what write mode may "
                                "change; they need mode 'write'")
    if net != "none":
        raise ShellRequestError(
            "shell network access (net=proxy|host) is plan E6 and not built "
            "yet; the sandbox runs with net=none")
    out = {"mode": mode, "net": net}
    if scopes:      # only when given: a run without them is unchanged
        out["scopes"] = list(scopes)
    return out


def _shell_cfg(cfg: Any):
    return getattr(getattr(cfg, "tools", None), "shell", None)


def from_config(cfg: Any) -> Optional[dict]:
    sc = _shell_cfg(cfg)
    if sc is None or getattr(sc, "default", "off") != "always":
        return None
    return {"mode": sc.mode, "net": sc.net, "since": "config (default=always)"}


def resolve_ask(cfg: Any, raw: Any) -> Optional[dict]:
    asked = parse(raw)
    if asked is False:
        return None
    if asked is None:
        return from_config(cfg)
    return dict(asked, since="the ask")


def resolve_follow_up(cfg: Any, raw: Any, banked: Optional[dict],
                      k: int) -> tuple[Optional[dict], Optional[dict]]:
    """``(this run's shell, the root's new banked shell)`` for follow-up
    number ``k`` of the consultancy."""
    asked = parse(raw)
    if asked is False:
        return None, None
    if asked is not None:
        got = dict(asked, since=f"follow-up #{k}")
        return got, got
    if banked:
        return dict(banked), banked
    cfgd = from_config(cfg)
    return cfgd, banked


def preflight_error(cfg: Any, shell: Optional[dict],
                    cwd: Optional[str] = None) -> Optional[str]:
    """Why this host cannot give ``shell``, or ``None``.

    A requested shell on a host that cannot sandbox it stops the run
    before it starts — never a weaker shell, never a silent no-shell."""
    if not shell:
        return None
    tools = getattr(cfg, "tools", None)
    if tools is not None and not getattr(tools, "enabled", True):
        return ("the sandboxed shell was requested but [tools] enabled = "
                "false: the fixed builtin surface has no shell. Turn the "
                "registry back on (config set-tools --enabled true) or run "
                "without --shell.")
    if shell.get("mode") == "write" and cwd is not None:
        from claude_hooks.sandbox.views import ViewError, toplevel
        if shell.get("scopes"):
            from claude_hooks.sandbox.clone_views import resolve_scopes
            try:
                resolve_scopes(cwd, shell["scopes"])
            except ViewError as e:
                return f"shell write scopes: {e}"
        elif toplevel(cwd) is None:
            return (f"shell mode 'write' gives each lane a private git "
                    f"worktree, and {cwd} is not in a git work tree. Name "
                    f"the directories the council may change with "
                    f"--write-scope PATH (each lane gets a reflink clone of "
                    f"them), use --shell read, or run from a git root.")
    from claude_hooks.sandbox import backend_for
    sc = _shell_cfg(cfg)
    pre = backend_for(getattr(sc, "backend", "auto") or "auto").preflight()
    if pre.ok:
        return None
    return (f"the sandboxed shell was requested ({shell.get('since')}) but "
            f"this host cannot provide it: {pre.reason()}. Run without it "
            f"(--no-shell, or [tools.shell] default = \"off\") or fix the "
            f"host (claude-consultants shell-preflight).")


def provenance(shell: Optional[dict]) -> str:
    if not shell:
        return ""
    scopes = shell.get("scopes")
    return (f"shell: enabled since {shell.get('since') or 'this run'} "
            f"({shell.get('mode', 'read')}"
            + (f" on {', '.join(scopes)}" if scopes else "")
            + f", net {shell.get('net', 'none')})")


def with_provenance(answer: str, shell: Optional[dict]) -> str:
    line = provenance(shell)
    if not line or not answer:
        return answer
    return f"{answer.rstrip()}\n\n---\n_{line}_\n"


def answer_with_shell(answer: str, shell: Optional[dict], *, cwd: str,
                      sid: str) -> str:
    """The run's answer as the user gets it: in write mode the lanes'
    views are frozen (changes captured as patches, worktrees removed) and
    a "Sandbox changes" section lists what is held; then the provenance
    footer. Idempotent. Off returns ``answer`` unchanged."""
    if not shell:
        return answer
    section = ""
    if shell.get("mode") == "write":
        try:
            from claude_hooks.sandbox.views import session_views, summary
            views = session_views(sid=sid, cwd=cwd)
            if views is not None:
                views.freeze_all()
                section = summary(views)
        except Exception as e:     # never lose the answer over this
            section = (f"## Sandbox changes\n\nThe lanes' views could not "
                       f"be collected ({e}); `claude-consultants sandbox gc` "
                       f"retries.")
    if section and answer:
        answer = f"{answer.rstrip()}\n\n{section}\n"
    elif section:
        answer = section + "\n"
    return with_provenance(answer, shell)


def settings_for(cfg: Any, shell: dict):
    """The provider's :class:`ShellSettings` from config + this run's
    request (mode and net come from the request)."""
    from claude_hooks.tool_registry.shell_provider import ShellSettings
    sc = _shell_cfg(cfg)
    scopes = tuple(shell.get("scopes") or ())
    if sc is None:
        return ShellSettings(mode=shell["mode"], net=shell["net"],
                             write_scopes=scopes)
    return ShellSettings(
        mode=shell["mode"], net=shell["net"], backend=sc.backend,
        write_scopes=scopes,
        ro_binds=tuple(sc.ro_binds), timeout_s=sc.timeout_s,
        max_timeout_s=sc.max_timeout_s, memory_mib=sc.memory_mib,
        pids=sc.pids, cpu_pct=sc.cpu_pct, lane_disk_gib=sc.lane_disk_gib,
        free_floor_gib=sc.free_floor_gib, free_floor_pct=sc.free_floor_pct)


__all__ = ["parse", "resolve_ask", "resolve_follow_up", "preflight_error",
           "provenance", "with_provenance", "settings_for", "from_config",
           "ShellRequestError"]
