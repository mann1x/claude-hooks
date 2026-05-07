"""``claude-consultants`` CLI — drives the engine over HTTP.

Subcommands return JSON on stdout for skill consumption. Exit code
0 = success, 1 = upstream error / network problem, 2 = bad
arguments. Stderr carries human-readable error text.

Routing: reads ``config/claude-hooks.json`` to decide whether to
talk to the engine directly (always-on mode) or to the daemon's
forwarder (smart-start mode). Both expose the same ``/v1/*`` shape;
only the base URL differs.

Stdlib only — uses ``urllib.request`` for HTTP. The consultants
engine itself runs in the dedicated ``claude-hooks-consultants``
conda env, but the CLI is intentionally lightweight so it can run
from any Python the user has on PATH.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional

from consultants import config as cc


# ----------------------- routing --------------------------------- #

DEFAULT_ENGINE_URL = "http://127.0.0.1:38095"
DEFAULT_FORWARDER_URL = "http://127.0.0.1:38096"


def _read_claude_hooks_consultants_block() -> dict:
    """Load the ``hooks.consultants`` block from
    ``config/claude-hooks.json`` if present. Falls back to defaults
    on any read error so the CLI keeps working before install.py has
    been re-run."""
    candidates = [
        Path(__file__).resolve().parent.parent / "config" / "claude-hooks.json",
        Path.home() / ".config" / "claude-hooks" / "claude-hooks.json",
    ]
    for p in candidates:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        hooks = data.get("hooks") or {}
        c = hooks.get("consultants")
        if isinstance(c, dict):
            return c
    return {}


def resolve_endpoint(*, override: Optional[str] = None) -> str:
    """Return the base URL the CLI should hit.

    Precedence: ``--endpoint`` flag > env var ``CONSULTANTS_URL`` >
    smart-start forwarder if smart-start is enabled in config >
    engine URL from config > built-in default.
    """
    if override:
        return override.rstrip("/")
    env = os.environ.get("CONSULTANTS_URL")
    if env:
        return env.rstrip("/")
    block = _read_claude_hooks_consultants_block()
    smart = block.get("smart_start") or {}
    if smart.get("enabled"):
        return str(smart.get("forwarder_url")
                   or DEFAULT_FORWARDER_URL).rstrip("/")
    return str(block.get("engine_url") or DEFAULT_ENGINE_URL).rstrip("/")


# ----------------------- http helpers ---------------------------- #

class CLIError(Exception):
    """Raised for any user-visible failure. Carries an exit code."""
    def __init__(self, msg: str, *, exit_code: int = 1):
        super().__init__(msg)
        self.exit_code = exit_code


_TRACE_DEPRECATION_LOGGED = False


def _warn_trace_deprecated() -> None:
    """One-shot deprecation notice for --trace / --no-trace. v1.1
    replaced the JSONL trace stream with the per-session
    transcript.db; the flags are no-ops now and will be removed in
    v1.2."""
    global _TRACE_DEPRECATION_LOGGED
    if _TRACE_DEPRECATION_LOGGED:
        return
    _TRACE_DEPRECATION_LOGGED = True
    print(
        "WARNING: --trace / --no-trace are deprecated in v1.1 and "
        "will be removed in v1.2. The per-session transcript.db "
        "(under <cwd>/.claude-hooks/consultants/<sid>/) records "
        "the same data. Use scripts/consultants_trace_summary.py "
        "<sid> for the waterfall view.",
        file=sys.stderr,
    )


def _http(method: str, url: str, *, body: Optional[dict] = None,
          timeout: float = 600.0) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read()
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode(errors="replace")
        except Exception:
            err_body = "<unreadable>"
        try:
            err = json.loads(err_body).get("detail", err_body)
        except json.JSONDecodeError:
            err = err_body
        raise CLIError(
            f"HTTP {e.code} from {url}: {err}", exit_code=1,
        ) from e
    except urllib.error.URLError as e:
        raise CLIError(
            f"Could not reach {url}: {e.reason}. Is the consultants "
            f"service running? Try `systemctl --user status "
            f"claude-hooks-consultants` or `python install.py`.",
            exit_code=1,
        ) from e
    return json.loads(payload) if payload else {}


# ----------------------- consult / status / result --------------- #

def cmd_consult(args, base: str) -> int:
    body = {
        "message": args.message,
        "cwd": str(Path(args.cwd or os.getcwd()).resolve()),
    }
    if args.effort:
        body["effort"] = args.effort
    # --trace / --no-trace are deprecated in v1.1 (the JSONL trace
    # was replaced by the per-session transcript.db). The flag is
    # still accepted but no longer forwarded to the engine; warn
    # operators who pass it explicitly so they update tooling.
    if args.trace is True or args.trace is False:
        _warn_trace_deprecated()
    out = _http("POST", f"{base}/v1/consult", body=body)
    print(json.dumps({"ok": True, **out}, indent=2))
    return 0


def cmd_status(args, base: str) -> int:
    out = _http("GET", f"{base}/v1/consult/{args.sid}")
    print(json.dumps({"ok": True, **out}, indent=2))
    return 0


def cmd_follow_up(args, base: str) -> int:
    """Live-session iteration: spawn a follow-up that reuses
    the prior session's plan + research + warm ChatClients.
    Returns a NEW sid; poll it just like a fresh consult.

    The engine auto-reopens the parent if it was closed or
    evicted — pass ``--cwd`` to enable the disk-fallback path
    when the engine no longer has the parent in memory."""
    body = {"message": args.message}
    if args.effort:
        body["effort"] = args.effort
    if args.trace is True or args.trace is False:
        _warn_trace_deprecated()
    # Always include cwd so cold-path follow-ups (parent evicted /
    # service restarted) can reopen from disk without a separate
    # reopen call.
    body["cwd"] = str(Path(args.cwd or os.getcwd()).resolve())
    out = _http("POST",
                f"{base}/v1/consult/{args.parent_sid}/follow-up",
                body=body)
    print(json.dumps({"ok": True, **out}, indent=2))
    return 0


def cmd_reopen(args, base: str) -> int:
    """Restore a closed / evicted session to the in-memory pool.

    Three paths the engine can take, all return 200:
      - already warm  → no-op (``already_open: true``)
      - closed in memory → flip closed=False (``source: in-memory-reopen``)
      - evicted but on disk → reconstruct from artifacts under cwd
        (``source: disk``)

    Pass ``--cwd`` to point at the project; defaults to the current
    directory.
    """
    body = {
        "cwd": str(Path(args.cwd or os.getcwd()).resolve()),
    }
    out = _http("POST", f"{base}/v1/consult/{args.sid}/reopen",
                body=body)
    print(json.dumps({"ok": True, **out}, indent=2))
    return 0


def cmd_close(args, base: str) -> int:
    """Explicitly release a session's warm ChatClients. On-disk
    artifacts are preserved, so a later ``follow-up`` or
    ``reopen`` against this sid auto-reloads the session at the
    cost of one /api/show probe per role on the first call."""
    out = _http("POST", f"{base}/v1/consult/{args.sid}/close",
                body={})
    print(json.dumps({"ok": True, **out}, indent=2))
    return 0


def cmd_list_open(args, base: str) -> int:
    """List all in-memory sessions (running + completed-not-yet-closed).
    Closed sessions linger briefly for late polls but don't show
    here."""
    out = _http("GET", f"{base}/v1/sessions/open")
    print(json.dumps({"ok": True, **out}, indent=2))
    return 0


def cmd_result(args, base: str) -> int:
    out = _http("GET", f"{base}/v1/consult/{args.sid}/result")
    print(json.dumps({"ok": True, **out}, indent=2))
    return 0


def cmd_list(args, base: str) -> int:
    cwd = str(Path(args.cwd or os.getcwd()).resolve())
    qs = urllib.parse.urlencode({"cwd": cwd, "limit": args.limit})
    out = _http("GET", f"{base}/v1/sessions?{qs}")
    print(json.dumps({"ok": True, **out}, indent=2))
    return 0


def cmd_show(args, base: str) -> int:
    """Local read of a session's summary.md — does not hit the
    engine. Useful when the service is down.

    With ``--raw``, dumps the structured event log from
    ``transcript.db`` (one row per line as JSON) instead of the
    rendered summary. Optional ``--filter role=researcher`` and
    ``--filter kind=tool_call`` add WHERE clauses; ``--limit N``
    caps the number of rows."""
    cwd = Path(args.cwd or os.getcwd()).resolve()
    from consultants.engine import storage
    sdir = storage.session_dir(cwd, args.sid)
    if getattr(args, "raw", False):
        return _cmd_show_raw(args, sdir)
    summary_path = sdir / storage.SUMMARY_FILENAME
    metadata_path = sdir / storage.METADATA_FILENAME
    if not summary_path.exists():
        raise CLIError(f"no summary for sid {args.sid} under {sdir}")
    out = {
        "ok": True,
        "sid": args.sid,
        "summary_markdown": summary_path.read_text(encoding="utf-8"),
        "metadata": (json.loads(metadata_path.read_text(encoding="utf-8"))
                     if metadata_path.exists() else None),
    }
    print(json.dumps(out, indent=2))
    return 0


# Whitelist of WHERE-clause columns acceptable from --filter. Anything
# outside this set is rejected so we never interpolate user input into
# SQL — only the value side is parameterized.
_RAW_FILTER_COLUMNS = (
    "role", "kind", "model", "tool", "round", "lane_idx",
)


def _parse_raw_filters(filters: list[str]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for f in filters or []:
        if "=" not in f:
            raise CLIError(
                f"--filter must be key=value, got {f!r} "
                f"(e.g. --filter role=researcher)"
            )
        k, _, v = f.partition("=")
        k = k.strip()
        if k not in _RAW_FILTER_COLUMNS:
            raise CLIError(
                f"unknown --filter column {k!r}. Allowed: "
                + ", ".join(_RAW_FILTER_COLUMNS)
            )
        out.append((k, v.strip()))
    return out


def _cmd_show_raw(args, sdir: Path) -> int:
    """Implementation of ``show --raw`` — dumps the events table as
    one JSON object per line ordered by ts. Tolerates partial /
    in-flight db files (mode=ro, fall back gracefully on missing
    columns)."""
    import sqlite3
    from consultants.engine import storage as _storage
    db_path = sdir / _storage.TRANSCRIPT_DB_FILENAME
    if not db_path.is_file():
        raise CLIError(
            f"no transcript.db for sid {args.sid} under {sdir}. "
            f"Older v1.0 sessions don't have one — show --raw is "
            f"only meaningful for v1.1+ sessions."
        )
    pairs = _parse_raw_filters(getattr(args, "filter", None) or [])
    where_sql = ""
    params: list = []
    if pairs:
        clauses = []
        for col, val in pairs:
            clauses.append(f"{col} = ?")
            # Coerce integers for numeric columns; sqlite3 binds
            # bare strings as TEXT which won't match an INTEGER row.
            if col in ("round", "lane_idx"):
                try:
                    params.append(int(val))
                except ValueError:
                    raise CLIError(
                        f"--filter {col} expects an integer, "
                        f"got {val!r}"
                    )
            else:
                params.append(val)
        where_sql = " WHERE " + " AND ".join(clauses)
    limit = int(getattr(args, "limit", 0) or 0)
    limit_sql = f" LIMIT {limit}" if limit > 0 else ""

    try:
        conn = sqlite3.connect(
            f"file:{db_path}?mode=ro", uri=True, timeout=5.0,
        )
    except sqlite3.OperationalError as e:
        raise CLIError(f"could not open {db_path}: {e}")
    try:
        cur = conn.execute(
            "SELECT event_id, ts, kind, role, round, lane_idx, "
            "model, prompt_tokens, completion_tokens, "
            "tool, args, output, output_chars, duration_ms, error "
            "FROM events" + where_sql
            + " ORDER BY ts" + limit_sql,
            params,
        )
        cols = [d[0] for d in cur.description]
        for row in cur:
            ev = dict(zip(cols, row))
            print(json.dumps(ev, ensure_ascii=False, default=str))
    except sqlite3.DatabaseError as e:
        raise CLIError(f"transcript.db query failed: {e}")
    finally:
        conn.close()
    return 0


# ----------------------- config --------------------------------- #

def _config_dump(cfg: cc.ConsultantsConfig, *, smart_block: dict) -> dict:
    return {
        "topology": cfg.topology,
        "effort": cfg.effort,
        "effort_budget": cfg.effort_budget,
        "service": {
            "mode": cfg.service.mode,
            "http_port": cfg.service.http_port,
        },
        "smart_start": {
            "enabled": bool(smart_block.get("enabled", False)),
            "idle_timeout_seconds": int(
                smart_block.get("idle_timeout_seconds", 1800)),
            "forwarder_url": smart_block.get(
                "forwarder_url", DEFAULT_FORWARDER_URL),
        },
        "endpoint": resolve_endpoint(),
        "roles": {
            r: {
                "enabled": cfg.roles[r].enabled,
                "model": cfg.roles[r].model,
                "ctx_max": cfg.roles[r].ctx_max,
                "ctx_max_explicit": cfg.roles[r].ctx_max_explicit,
            }
            for r in cc.ROLES
        },
        "mandatory_roles": sorted(cc.MANDATORY_ROLES),
        "valid_efforts": sorted(cc.EFFORT_BUDGETS),
        "valid_service_modes": sorted(cc.VALID_SERVICE_MODES),
    }


def cmd_config_show(args, base: str) -> int:
    cwd = Path(args.cwd).resolve() if args.cwd else None
    cfg = cc.load_config(cwd)
    block = _read_claude_hooks_consultants_block()
    smart = block.get("smart_start") or {}
    print(json.dumps({"ok": True, **_config_dump(cfg, smart_block=smart)},
                     indent=2))
    return 0


def cmd_config_set_role(args, base: str) -> int:
    enabled: Optional[bool] = None
    if args.enabled is not None:
        if args.enabled.lower() in ("true", "yes", "1", "on"):
            enabled = True
        elif args.enabled.lower() in ("false", "no", "0", "off"):
            enabled = False
        else:
            raise CLIError(
                f"--enabled must be true/false, got {args.enabled!r}",
                exit_code=2,
            )
    ctx_max: Optional[int] = None
    if args.ctx is not None:
        if args.ctx.lower() in ("auto", ""):
            ctx_max = 0  # cc.set_role treats 0 as "clear"
        else:
            try:
                ctx_max = int(args.ctx)
            except ValueError:
                raise CLIError(
                    f"--ctx must be integer or 'auto', got {args.ctx!r}",
                    exit_code=2,
                ) from None
    try:
        cfg = cc.set_role(
            args.role,
            model=args.model,
            ctx_max=ctx_max,
            enabled=enabled,
            scope="project" if args.project else "user",
            cwd=Path(args.cwd or os.getcwd()).resolve()
            if args.project else None,
        )
    except ValueError as e:
        raise CLIError(str(e), exit_code=2) from None
    block = _read_claude_hooks_consultants_block()
    smart = block.get("smart_start") or {}
    print(json.dumps({"ok": True, **_config_dump(cfg, smart_block=smart)},
                     indent=2))
    return 0


def cmd_config_set_effort(args, base: str) -> int:
    try:
        cfg = cc.set_effort(args.tier)
    except ValueError as e:
        raise CLIError(str(e), exit_code=2) from None
    block = _read_claude_hooks_consultants_block()
    smart = block.get("smart_start") or {}
    print(json.dumps({"ok": True, **_config_dump(cfg, smart_block=smart)},
                     indent=2))
    return 0


def cmd_config_set_service_mode(args, base: str) -> int:
    try:
        cfg = cc.set_service_mode(args.mode)
    except ValueError as e:
        raise CLIError(str(e), exit_code=2) from None
    block = _read_claude_hooks_consultants_block()
    smart = block.get("smart_start") or {}
    out = _config_dump(cfg, smart_block=smart)
    out["follow_up"] = (
        "Run `python install.py` to install/uninstall the systemd "
        "unit, then restart `claude-hooks-daemon`."
    )
    print(json.dumps({"ok": True, **out}, indent=2))
    return 0


def cmd_config_set_idle_timeout(args, base: str) -> int:
    if args.seconds < 60 or args.seconds > 86400:
        raise CLIError(
            "idle timeout must be 60..86400 seconds (1 min .. 24 h)",
            exit_code=2,
        )
    # This setting lives in ``config/claude-hooks.json`` not the
    # consultants TOML — we update the JSON in-place.
    candidates = [
        Path(__file__).resolve().parent.parent
        / "config" / "claude-hooks.json",
    ]
    target: Optional[Path] = None
    for p in candidates:
        if p.exists():
            target = p
            break
    if target is None:
        raise CLIError(
            "config/claude-hooks.json not found — run install.py first",
            exit_code=2,
        )
    raw = json.loads(target.read_text(encoding="utf-8"))
    hooks = raw.setdefault("hooks", {})
    consultants = hooks.setdefault("consultants", {})
    smart = consultants.setdefault("smart_start", {})
    smart["idle_timeout_seconds"] = args.seconds
    target.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "ok": True,
        "idle_timeout_seconds": args.seconds,
        "follow_up": "Restart claude-hooks-daemon for the change to take effect.",
    }, indent=2))
    return 0


def cmd_config_list_models(args, base: str) -> int:
    """Proxy /api/tags from the configured Ollama upstream and filter
    to tools-capable models. The skill uses this to populate the
    AskUserQuestion options."""
    upstream = (
        os.environ.get("CALIBER_GROUNDING_UPSTREAM")
        or os.environ.get("OLLAMA_HOST")
        or "http://192.168.178.2:11433"
    )
    try:
        with urllib.request.urlopen(f"{upstream}/api/tags",
                                    timeout=10.0) as resp:
            tags = json.loads(resp.read())
    except urllib.error.URLError as e:
        raise CLIError(
            f"Could not reach Ollama at {upstream}: {e.reason}",
        ) from None
    models = []
    for m in tags.get("models") or []:
        name = m.get("name") or m.get("model") or ""
        if not name:
            continue
        # Cloud tags end with ":cloud"; local tags vary. Don't filter
        # by capability here — that would require an extra /api/show
        # per model. Skill ranks instead.
        models.append({
            "name": name,
            "size": m.get("size"),
            "modified_at": m.get("modified_at"),
        })
    print(json.dumps({"ok": True, "upstream": upstream,
                      "models": models}, indent=2))
    return 0


# ----------------------- argparse wiring ------------------------- #

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="claude-consultants",
        description="Drive the /consultants agentic engine.",
    )
    p.add_argument("--endpoint",
                   help="Override the consultants service URL "
                        "(default: read from config).")
    sub = p.add_subparsers(dest="cmd", required=True)

    # consult
    c = sub.add_parser("consult", help="Start a council consultation.")
    c.add_argument("--message", required=True,
                   help="The question to consult on.")
    c.add_argument("--cwd",
                   help="Project root (default: current directory).")
    c.add_argument("--effort", choices=tuple(cc.EFFORT_BUDGETS),
                   help="Override effort tier for this consultation.")
    # DEPRECATED in v1.1 — kept as a no-op so muscle memory doesn't
    # break. v1.1 always writes the structured event log to
    # transcript.db; there's no on/off switch any more.
    c.add_argument("--trace", dest="trace",
                   action="store_true", default=None,
                   help="DEPRECATED: no-op in v1.1 (replaced by "
                        "per-session transcript.db). Will be removed "
                        "in v1.2.")
    c.add_argument("--no-trace", dest="trace", action="store_false",
                   help="DEPRECATED: no-op in v1.1.")
    c.set_defaults(fn=cmd_consult)

    # status
    s = sub.add_parser("status", help="Poll a session's status.")
    s.add_argument("sid")
    s.set_defaults(fn=cmd_status)

    # result
    r = sub.add_parser("result", help="Fetch a completed session's "
                       "summary + metadata.")
    r.add_argument("sid")
    r.set_defaults(fn=cmd_result)

    # follow-up — live-session iteration. Returns a NEW sid.
    fu = sub.add_parser(
        "follow-up",
        help="Live-session iteration: spawn a follow-up that reuses "
             "the parent's plan, research, and warm ChatClients.")
    fu.add_argument("parent_sid",
                    help="The sid of the prior consultation. Must "
                         "still be in memory (not closed / reaped).")
    fu.add_argument("--message", required=True,
                    help="The focused follow-up question.")
    fu.add_argument("--effort", choices=tuple(cc.EFFORT_BUDGETS),
                    help="Override effort for this follow-up "
                         "(default: same as parent).")
    fu.add_argument("--trace", dest="trace",
                    action="store_true", default=None,
                    help="DEPRECATED: no-op in v1.1.")
    fu.add_argument("--no-trace", dest="trace", action="store_false",
                    help="DEPRECATED: no-op in v1.1.")
    fu.add_argument("--cwd",
                    help="Project root for disk-fallback when the "
                         "parent isn't in engine memory (default: "
                         "current dir). Always sent so closed / "
                         "evicted parents auto-reopen.")
    fu.set_defaults(fn=cmd_follow_up)

    # reopen — disk-fallback to restore a closed / evicted session.
    ro = sub.add_parser(
        "reopen",
        help="Restore a closed or evicted session to the engine's "
             "in-memory pool. Auto-runs on follow-up; this exposes "
             "the same path explicitly for inspect-before-iterate.")
    ro.add_argument("sid")
    ro.add_argument("--cwd",
                    help="Project root (default: current dir).")
    ro.set_defaults(fn=cmd_reopen)

    # close — explicit release of warm ChatClients.
    cl = sub.add_parser(
        "close",
        help="Close an in-memory session, releasing its warm engine "
             "handles. Reversible — a later `follow-up` or "
             "`reopen` re-loads the session from disk artifacts.")
    cl.add_argument("sid")
    cl.set_defaults(fn=cmd_close)

    # list-open — what sessions are alive in the engine right now?
    lo = sub.add_parser(
        "list-open",
        help="List in-memory sessions (warm or recently completed). "
             "Closed sessions don't appear here.")
    lo.set_defaults(fn=cmd_list_open)

    # list
    l_ = sub.add_parser("list", help="List sessions for a project.")
    l_.add_argument("--cwd", help="Project root (default: cwd).")
    l_.add_argument("--limit", type=int, default=50)
    l_.set_defaults(fn=cmd_list)

    # show
    sh = sub.add_parser(
        "show",
        help="Print a stored session's summary (no engine call). With "
             "--raw, dump the events table from transcript.db as one "
             "JSON object per line.",
    )
    sh.add_argument("sid")
    sh.add_argument("--cwd", help="Project root (default: cwd).")
    sh.add_argument(
        "--raw", action="store_true",
        help="Dump the events table from transcript.db instead of "
             "rendering summary.md. Output is one JSON object per "
             "line, ordered by ts.",
    )
    sh.add_argument(
        "--filter", action="append", default=None, metavar="COL=VAL",
        help="Filter --raw output by column. Repeatable. Allowed "
             "columns: role, kind, model, tool, round, lane_idx. "
             "Examples: --filter role=researcher --filter kind=tool_call",
    )
    sh.add_argument(
        "--limit", type=int, default=0,
        help="Cap the number of rows in --raw output. 0 (default) "
             "= unlimited.",
    )
    sh.set_defaults(fn=cmd_show)

    # config
    cfg = sub.add_parser("config", help="Inspect or modify config.")
    cfg_sub = cfg.add_subparsers(dest="config_cmd", required=True)

    cs = cfg_sub.add_parser("show", help="Print full config snapshot.")
    cs.add_argument("--cwd",
                    help="Project root (loads project overrides too).")
    cs.set_defaults(fn=cmd_config_show)

    cr = cfg_sub.add_parser("set-role", help="Configure one role.")
    cr.add_argument("role", choices=cc.ROLES)
    cr.add_argument("--model", help="Ollama tag for this role.")
    cr.add_argument("--ctx",
                    help="Pin a context length (or 'auto' / 0 to clear).")
    cr.add_argument("--enabled",
                    help="true|false — toggle the role on/off.")
    cr.add_argument("--project", action="store_true",
                    help="Save under per-project scope (.claude-hooks/) "
                         "instead of user-global.")
    cr.add_argument("--cwd",
                    help="Project root (only used with --project).")
    cr.set_defaults(fn=cmd_config_set_role)

    ce = cfg_sub.add_parser("set-effort", help="Set effort tier.")
    ce.add_argument("tier", choices=tuple(cc.EFFORT_BUDGETS))
    ce.set_defaults(fn=cmd_config_set_effort)

    csm = cfg_sub.add_parser("set-service-mode",
                             help="Set service mode "
                             "(always-on | smart-start).")
    csm.add_argument("mode", choices=cc.VALID_SERVICE_MODES)
    csm.set_defaults(fn=cmd_config_set_service_mode)

    cit = cfg_sub.add_parser("set-idle-timeout",
                             help="Smart-start idle timeout (seconds).")
    cit.add_argument("seconds", type=int)
    cit.set_defaults(fn=cmd_config_set_idle_timeout)

    clm = cfg_sub.add_parser("list-models",
                             help="Available Ollama tags.")
    clm.set_defaults(fn=cmd_config_list_models)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    base = resolve_endpoint(override=args.endpoint)
    try:
        return args.fn(args, base)
    except CLIError as e:
        print(json.dumps({"ok": False, "error": str(e)}), file=sys.stderr)
        return e.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
