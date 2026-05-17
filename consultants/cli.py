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
    if getattr(args, "add_dir", None):
        # v1.8+: forward extra allowed roots to the engine. The engine
        # unions them with settings-file auto-discovery and stores the
        # result on the session record so follow-ups inherit.
        body["extra_roots"] = list(args.add_dir)
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
    if getattr(args, "add_dir", None):
        # v1.8+: extends the parent's extra_roots with this follow-up's
        # entries. The engine merges the two lists (parent first, then
        # this turn's, dedup'd) before running the executor.
        body["extra_roots"] = list(args.add_dir)
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
                # Phase 9 — multi-model fan-out at x-tiers. Empty
                # for every base-tier-only configuration; the skill
                # uses presence to decide whether to render the
                # extras list.
                "extra_models": list(cfg.roles[r].extra_models),
            }
            for r in cc.ROLES
        },
        "extras_active": cc.extras_active(cfg.effort),
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
    # Phase 9: extras mutators. Validate at the CLI layer so the
    # JSON error-shape stays consistent with the rest of the CLI.
    add_extra = getattr(args, "add_model", None)
    remove_extra = getattr(args, "remove_model", None)
    clear_extras = bool(getattr(args, "clear_extras", False))
    if add_extra is not None and not add_extra.strip():
        raise CLIError("--add-model must be a non-empty model tag",
                       exit_code=2)
    try:
        cfg = cc.set_role(
            args.role,
            model=args.model,
            ctx_max=ctx_max,
            enabled=enabled,
            add_extra_model=add_extra,
            remove_extra_model=remove_extra,
            clear_extras=clear_extras,
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


# ----------------------- Task #111: config coder ---------------- #
# Sub-subparser under ``config coder`` for managing the per-language
# coder model routes + global default route. The single-source
# resolver lives in ``consultants.config.coder_resolve_route``; the
# CLI is a thin adapter onto the three mutators (set_coder_route /
# unset_coder_route / set_coder_default_route) plus a JSON pretty-
# printer for the ``list`` verb. Same --cwd/--project scope handling
# as ``set-role``.


def _route_to_dict(route) -> Optional[dict]:
    """Coerce a CoderLanguageRoute (or None) to a JSON-friendly dict."""
    if route is None:
        return None
    return {
        "primary": getattr(route, "primary", ""),
        "fallback": getattr(route, "fallback", ""),
    }


def _coder_route_block(cfg) -> dict:
    """Build the {default_route, routes_by_language} dict used by
    ``config coder list``'s JSON output. Pure-function — no I/O."""
    rc = cfg.roles["coder"]
    return {
        "model": rc.model,
        "default_route": _route_to_dict(rc.default_route),
        "routes_by_language": {
            lang: _route_to_dict(route)
            for lang, route in sorted(rc.routes_by_language.items())
        },
    }


def cmd_config_coder_list(args, base: str) -> int:
    cwd = Path(args.cwd).resolve() if args.cwd else None
    cfg = cc.load_config(cwd)
    print(json.dumps({"ok": True, "coder": _coder_route_block(cfg)},
                     indent=2))
    return 0


def _scope_kwargs(args) -> dict:
    """Translate ``args.project`` + ``args.cwd`` into the kwargs the
    config-mutator functions expect. Mirrors ``cmd_config_set_role``."""
    return {
        "scope": "project" if getattr(args, "project", False) else "user",
        "cwd": (
            Path(args.cwd or os.getcwd()).resolve()
            if getattr(args, "project", False) else None
        ),
    }


def cmd_config_coder_set(args, base: str) -> int:
    # ``primary``/``fallback`` are optional on update, required on
    # create — cc.set_coder_route enforces this. The CLI defers
    # entirely to the mutator's validation so the rules live in one
    # place.
    try:
        cfg = cc.set_coder_route(
            args.language,
            primary=args.primary,
            fallback=args.fallback,
            **_scope_kwargs(args),
        )
    except ValueError as e:
        raise CLIError(str(e), exit_code=2) from None
    print(json.dumps({"ok": True, "coder": _coder_route_block(cfg)},
                     indent=2))
    return 0


def cmd_config_coder_unset(args, base: str) -> int:
    try:
        cfg = cc.unset_coder_route(args.language, **_scope_kwargs(args))
    except ValueError as e:
        raise CLIError(str(e), exit_code=2) from None
    print(json.dumps({"ok": True, "coder": _coder_route_block(cfg)},
                     indent=2))
    return 0


def cmd_config_coder_set_default(args, base: str) -> int:
    try:
        cfg = cc.set_coder_default_route(
            primary=args.primary,
            fallback=args.fallback,
            **_scope_kwargs(args),
        )
    except ValueError as e:
        raise CLIError(str(e), exit_code=2) from None
    print(json.dumps({"ok": True, "coder": _coder_route_block(cfg)},
                     indent=2))
    return 0


# ----------------------- M9 control verbs ----------------------- #
# These mirror the seven HTTP control endpoints exposed by
# consultants.server.control_routes. Each handler is a thin
# adapter: parse argv → POST/GET → pretty-print the response.


def _parse_relative_time(spec: str) -> float:
    """Parse a relative-time string like ``+30m`` / ``+2h`` / ``+45s``.

    Returns the absolute ``time.time()`` value to put into
    ``deadline_ts``. The CLI's ``--time`` flag is the only entry
    point — the HTTP layer takes absolute timestamps because that's
    less ambiguous across daylight-saving rolls.

    Empty / non-prefixed strings raise CLIError. Bare integers
    (``30m``, ``2h``) are accepted in addition to the ``+`` prefix
    for ergonomic reasons.
    """
    import time as _time
    s = (spec or "").strip().lstrip("+").lower()
    if not s:
        raise CLIError("--time requires a value like +30m / +2h / +45s")
    unit = s[-1]
    if unit in ("s", "m", "h", "d"):
        try:
            n = float(s[:-1])
        except ValueError:
            raise CLIError(f"--time: cannot parse {spec!r}")
        mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
        delta = n * mult
    else:
        try:
            delta = float(s)
        except ValueError:
            raise CLIError(f"--time: cannot parse {spec!r}")
    if delta <= 0:
        raise CLIError("--time must be a positive delta")
    return _time.time() + delta


def cmd_state(args, base: str) -> int:
    """GET /v1/consult/<sid>/state — the M9 deep-state view (vs
    ``status``, which is the v1 lightweight progress poll)."""
    out = _http("GET", f"{base}/v1/consult/{args.sid}/state")
    print(json.dumps({"ok": True, **out}, indent=2))
    return 0


def cmd_inject(args, base: str) -> int:
    """POST /v1/consult/<sid>/inject — inject additional context
    into the in-flight consultation. The text can come from
    ``--message`` directly or from ``--file`` (whole-file read)."""
    if args.file:
        try:
            text = Path(args.file).read_text(encoding="utf-8")
        except OSError as e:
            raise CLIError(f"could not read {args.file}: {e}")
    else:
        text = args.message or ""
    body = {
        "role": args.role,
        "text": text,
        "source": args.source or "user",
    }
    out = _http("POST",
                f"{base}/v1/consult/{args.sid}/inject", body=body)
    print(json.dumps({"ok": True, **out}, indent=2))
    return 0


def cmd_control(args, base: str) -> int:
    """POST /v1/consult/<sid>/control — mutate one or more
    RuntimeControl knobs. Multiple flags coalesce into a single
    PATCH body; an empty PATCH is a 400 from the server."""
    rc: dict = {}
    if args.time:
        rc["deadline_ts"] = _parse_relative_time(args.time)
    if args.soft_target:
        rc["soft_target_ts"] = _parse_relative_time(args.soft_target)
    if args.max_rounds is not None:
        rc["max_rounds"] = int(args.max_rounds)
    if args.max_reroutes is not None:
        rc["max_reroutes"] = int(args.max_reroutes)
    if args.confidence is not None:
        rc["confidence_target"] = float(args.confidence)
    if args.strictness:
        rc["critic_strictness"] = args.strictness
    if args.enable:
        rc["enabled_roles"] = sorted({r.strip() for r in args.enable})
    if args.disable:
        # The server-side delta replaces enabled_roles outright, so
        # `disable` only makes sense when the caller knows the
        # current set. For ergonomics we GET /state first and
        # subtract.
        snap = _http("GET", f"{base}/v1/consult/{args.sid}/state")
        current = (
            (snap.get("runtime_control") or {}).get("enabled_roles")
            or []
        )
        kill = {r.strip() for r in args.disable}
        rc["enabled_roles"] = [r for r in current if r not in kill]
    if not rc:
        raise CLIError(
            "control requires at least one knob: "
            "--time / --soft-target / --max-rounds / --max-reroutes / "
            "--confidence / --strictness / --enable / --disable",
        )
    out = _http(
        "POST", f"{base}/v1/consult/{args.sid}/control",
        body={"runtime_control": rc},
    )
    print(json.dumps({"ok": True, **out}, indent=2))
    return 0


def cmd_pause(args, base: str) -> int:
    """POST /v1/consult/<sid>/interrupt — flip pause_requested.
    ``pause`` is the friendlier verb name; the HTTP route is
    ``/interrupt`` because that matches LangGraph's terminology."""
    body = {"reason": args.reason or "user-pause"}
    out = _http("POST",
                f"{base}/v1/consult/{args.sid}/interrupt", body=body)
    print(json.dumps({"ok": True, **out}, indent=2))
    return 0


def cmd_resume(args, base: str) -> int:
    """POST /v1/consult/<sid>/resume — clear the interrupt and
    re-enter via Command(resume=value). The actual graph re-invoke
    runs on the server's executor pool; this returns 200 with a
    ``mode: scheduled`` payload and the caller polls /state."""
    value: Any = None
    if args.value:
        try:
            value = json.loads(args.value)
        except json.JSONDecodeError:
            # Treat bare strings as the literal resume value.
            value = args.value
    body = {"value": value, "decision": args.decision or ""}
    out = _http("POST",
                f"{base}/v1/consult/{args.sid}/resume", body=body)
    print(json.dumps({"ok": True, **out}, indent=2))
    return 0


def cmd_cancel(args, base: str) -> int:
    """POST /v1/consult/<sid>/cancel — flip cancel_requested.
    ``--keep-partial`` is the default; pass ``--discard-partial`` to
    delete the checkpoint file too."""
    body = {
        "discard_partial": bool(args.discard_partial),
        "reason": args.reason or "user-cancel",
    }
    out = _http("POST",
                f"{base}/v1/consult/{args.sid}/cancel", body=body)
    print(json.dumps({"ok": True, **out}, indent=2))
    return 0


def cmd_events(args, base: str) -> int:
    """GET /v1/consult/<sid>/events — SSE stream over runtime_events.

    Streams indefinitely (until the session terminates or the user
    hits ^C). The endpoint supports Last-Event-ID resume; pass
    ``--since`` to skip events older than the given id.
    """
    headers = {}
    if args.since:
        headers["Last-Event-ID"] = str(int(args.since))
    url = f"{base}/v1/consult/{args.sid}/events"
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        # Long timeout — SSE connections are long-lived by design.
        resp = urllib.request.urlopen(req, timeout=86400.0)
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode(errors="replace")
        except Exception:
            err_body = "<unreadable>"
        raise CLIError(f"HTTP {e.code} from {url}: {err_body}")
    except urllib.error.URLError as e:
        raise CLIError(
            f"Could not reach {url}: {e.reason}",
        )
    # Read line-by-line and pretty-print each event block. SSE
    # records are separated by a blank line, so we accumulate
    # lines until we see one.
    try:
        record_lines: list[str] = []
        while True:
            raw = resp.readline()
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
            if line == "":
                if record_lines:
                    print("\n".join(record_lines))
                    print()  # blank line separator in the CLI output
                    record_lines = []
                continue
            record_lines.append(line)
    except KeyboardInterrupt:
        return 0
    return 0


# ----------------------- skill-eval (M11) ------------------------- #

def cmd_skill_eval_coder(args, base: str) -> int:
    """Run the coder skill-eval suite. Thin wrapper around
    ``benchmarks.consultants.coder_bench.main`` — keeps the bench
    script as the source of truth for behavior while giving users
    a friendly ``claude-consultants skill-eval coder`` entry point.

    Args translation:

    - The bench script wants ``--models`` as a comma-separated
      string; this wrapper accepts the same and passes through.
    - ``--id`` and ``--tier`` are repeatable on both sides.
    - The wrapper resolves a default ``--output-dir`` to the
      conventional date-stamped location if the user didn't supply
      one.

    Exit codes mirror the bench script: 0 on success, 1 on no
    trials (empty match), 2 when --live is set but --accept-cost
    isn't.
    """
    # Lazy import — keeps the CLI fast on the no-bench paths.
    try:
        from benchmarks.consultants.coder_bench import main as _bench_main
    except ImportError as e:
        raise CLIError(
            "benchmarks.consultants.coder_bench is not importable. "
            f"Run from the repo root or set PYTHONPATH. Underlying: {e}"
        )
    argv: list[str] = []
    if args.dry_run:
        argv.append("--dry-run")
    if args.live:
        argv.append("--live")
    if args.accept_cost:
        argv.append("--accept-cost")
    if args.models:
        argv.extend(["--models", args.models])
    if args.ollama_base:
        argv.extend(["--ollama-base", args.ollama_base])
    if args.judge_model is not None:
        argv.extend(["--judge-model", args.judge_model])
    if args.output_dir:
        argv.extend(["--output-dir", args.output_dir])
    for t in (args.tier or []):
        argv.extend(["--tier", t])
    for qid in (args.id or []):
        argv.extend(["--id", qid])
    if args.smoke:
        argv.append("--smoke")
    if getattr(args, "commit_report", False):
        argv.append("--commit-report")
    return int(_bench_main(argv))


def cmd_skill_eval_stall(args, base: str) -> int:
    """Run the stall skill-eval suite (M11a). Thin wrapper around
    ``benchmarks.consultants.stall_bench.main`` — same shape as
    :func:`cmd_skill_eval_coder` but the stall bench has no judge,
    no per-tier filter (the tier dimension is standalone vs
    council and is selected via ``--tier1`` / ``--tier2`` / ``--both``),
    and two trial-count knobs (``--trials-tier1`` / ``--trials-tier2``).

    Exit codes mirror the bench script: 0 on success, 1 on no
    trials (empty match), 2 when --live is set but --accept-cost
    isn't.
    """
    try:
        from benchmarks.consultants.stall_bench import main as _bench_main
    except ImportError as e:
        raise CLIError(
            "benchmarks.consultants.stall_bench is not importable. "
            f"Run from the repo root or set PYTHONPATH. Underlying: {e}"
        )
    argv: list[str] = []
    if args.dry_run:
        argv.append("--dry-run")
    if args.live:
        argv.append("--live")
    if args.accept_cost:
        argv.append("--accept-cost")
    if args.models:
        argv.extend(["--models", args.models])
    if args.ollama_base:
        argv.extend(["--ollama-base", args.ollama_base])
    if args.output_dir:
        argv.extend(["--output-dir", args.output_dir])
    # Tier selection — mutually exclusive in the parser, so at most
    # one of these is set. The bench's parser is also mutually
    # exclusive so we forward at most one.
    if args.tier1:
        argv.append("--tier1")
    elif args.tier2:
        argv.append("--tier2")
    elif args.both:
        argv.append("--both")
    if args.trials_tier1 is not None:
        argv.extend(["--trials-tier1", str(args.trials_tier1)])
    if args.trials_tier2 is not None:
        argv.extend(["--trials-tier2", str(args.trials_tier2)])
    for qid in (args.id or []):
        argv.extend(["--id", qid])
    if args.smoke:
        argv.append("--smoke")
    return int(_bench_main(argv))


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
    c.add_argument(
        "--add-dir", action="append", default=[], metavar="PATH",
        help=(
            "Additional directory the consultants' tools may read from "
            "(repeatable). Unioned with permissions.additionalDirectories "
            "from ~/.claude/settings.json and the project's "
            ".claude/settings*.json. Persisted on the session record so "
            "follow-ups inherit."
        ),
    )
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
    fu.add_argument(
        "--add-dir", action="append", default=[], metavar="PATH",
        help=(
            "Additional directory (repeatable) for the follow-up's "
            "tool sandbox. Merged with the parent's extra_roots "
            "(parent first, then this turn, dedup'd) before the "
            "executor runs."
        ),
    )
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

    # ---- M9 control verbs: drive an in-flight consultation ---- #

    # state — deep state snapshot (vs ``status`` which is the v1
    # progress-poll alias).
    st = sub.add_parser(
        "state",
        help="Fetch the live v2 state snapshot (runtime_control, "
             "interrupts, partial_synthesis, …).",
    )
    st.add_argument("sid")
    st.set_defaults(fn=cmd_state)

    # inject — surface additional context into a live consult.
    inj = sub.add_parser(
        "inject",
        help="Inject additional_context into a live consultation. "
             "Text from --message or --file.",
    )
    inj.add_argument("sid")
    inj.add_argument("--role", default="any",
                     choices=("any", "planner", "researcher",
                                "critic", "synthesizer"),
                     help="Target role for the inject (default: any).")
    src = inj.add_mutually_exclusive_group(required=True)
    src.add_argument("-m", "--message",
                     help="The text to inject inline.")
    src.add_argument("-f", "--file",
                     help="Read the inject text from a file.")
    inj.add_argument("--source", default="user",
                     help="Source label on the injected Doc "
                          "(default: user).")
    inj.set_defaults(fn=cmd_inject)

    # control — mutate one or more runtime_control knobs.
    ctl = sub.add_parser(
        "control",
        help="Mutate RuntimeControl knobs on a live consultation. "
             "Multiple flags batch into one PATCH.",
    )
    ctl.add_argument("sid")
    ctl.add_argument(
        "--time",
        help="Relative deadline extension (e.g. +30m / +2h / +45s). "
             "Sets deadline_ts to now + delta.",
    )
    ctl.add_argument(
        "--soft-target",
        help="Same shape as --time but for soft_target_ts.",
    )
    ctl.add_argument("--max-rounds", type=int,
                     help="Researcher max-rounds cap (non-negative).")
    ctl.add_argument("--max-reroutes", type=int,
                     help="Critic max-reroutes cap (non-negative).")
    ctl.add_argument("--confidence", type=float,
                     help="confidence_target in [0, 1].")
    ctl.add_argument(
        "--strictness",
        choices=("lax", "normal", "strict"),
        help="Critic strictness preset.",
    )
    ctl.add_argument(
        "--enable", action="append", default=[],
        help="Role to enable (repeatable). Replaces enabled_roles.",
    )
    ctl.add_argument(
        "--disable", action="append", default=[],
        help="Role to disable (repeatable). Subtracts from the "
             "current enabled_roles snapshot (issues a GET /state "
             "first).",
    )
    ctl.set_defaults(fn=cmd_control)

    # pause — friendlier alias for /interrupt.
    pause = sub.add_parser(
        "pause",
        help="Request a cooperative pause at the next node entry.",
    )
    pause.add_argument("sid")
    pause.add_argument("--reason", default="user-pause",
                       help="Human-readable reason (logged).")
    pause.set_defaults(fn=cmd_pause)

    # resume — Command(resume=...) re-entry.
    res = sub.add_parser(
        "resume",
        help="Resume a paused consultation with a value.",
    )
    res.add_argument("sid")
    res.add_argument(
        "--value",
        help="Resume value (JSON). Bare strings are accepted as "
             "literals.",
    )
    res.add_argument(
        "--decision", default="",
        help="Optional human-readable decision label.",
    )
    res.set_defaults(fn=cmd_resume)

    # cancel — flip cancel_requested.
    can = sub.add_parser(
        "cancel",
        help="Cancel a running consultation cooperatively.",
    )
    can.add_argument("sid")
    can.add_argument(
        "--discard-partial", action="store_true",
        help="Also delete the checkpoint file (default: keep).",
    )
    can.add_argument("--reason", default="user-cancel",
                     help="Human-readable cancel reason (logged).")
    can.set_defaults(fn=cmd_cancel)

    # events — SSE stream.
    ev = sub.add_parser(
        "events",
        help="Tail the SSE event stream for a session.",
    )
    ev.add_argument("sid")
    ev.add_argument(
        "--since", type=int,
        help="Resume from this event_id (Last-Event-ID).",
    )
    ev.set_defaults(fn=cmd_events)

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
    # Phase 9 — multi-model fan-out at x-prefixed effort tiers.
    # extra_models is silently ignored at base tiers (low/medium/
    # high/max) so a benchmark labeled `high` is never accidentally
    # multi-model. Researcher and critic are the fan-outable roles;
    # planner / synthesizer accept these flags for symmetry but
    # the engine doesn't consult their extras at runtime.
    cr.add_argument("--add-model", dest="add_model", default=None,
                    help="Append an Ollama tag to extra_models. Used "
                         "by xmedium/xhigh/xmax to fan out the role "
                         "across multiple models per lane. Idempotent "
                         "and dedup'd against the primary model.")
    cr.add_argument("--remove-model", dest="remove_model", default=None,
                    help="Remove an Ollama tag from extra_models.")
    cr.add_argument("--clear-extras", dest="clear_extras",
                    action="store_true",
                    help="Empty extra_models for this role.")
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

    # ----- Task #111: config coder ---------------------------------
    # Sub-namespace under ``config`` for the coder role's per-language
    # routing. Verbs: list / set / unset / set-default. All share the
    # standard --cwd/--project scope flags.
    coder_parser = cfg_sub.add_parser(
        "coder",
        help=("Manage the coder role's per-language model routing "
              "(primary + fallback per language, plus a global "
              "default). The map is seeded from the v1.0.1-mlang "
              "bench winners; overrides land here."),
    )
    coder_sub = coder_parser.add_subparsers(
        dest="coder_cmd", required=True,
    )

    cl = coder_sub.add_parser(
        "list",
        help="Print the resolved coder routing table.",
    )
    cl.add_argument("--cwd",
                    help="Project root (loads project overrides too).")
    cl.set_defaults(fn=cmd_config_coder_list)

    cset = coder_sub.add_parser(
        "set",
        help=("Upsert a per-language coder route. --primary is "
              "required on create; either flag alone works on "
              "update. Pass --fallback '' to clear failover."),
    )
    cset.add_argument("language",
                      help="Language id (e.g. python, csharp, cpp).")
    cset.add_argument("--primary",
                      help="Primary model tag (e.g. glm-5.1:cloud).")
    cset.add_argument("--fallback", default=None,
                      help="Fallback model tag. Empty string clears "
                           "the failover model on an existing entry.")
    cset.add_argument("--project", action="store_true",
                      help="Save under per-project scope "
                           "(.claude-hooks/) instead of user-global.")
    cset.add_argument("--cwd",
                      help="Project root (only used with --project).")
    cset.set_defaults(fn=cmd_config_coder_set)

    cunset = coder_sub.add_parser(
        "unset",
        help=("Remove a per-language coder route. The language then "
              "falls through to the global default route. Idempotent."),
    )
    cunset.add_argument("language",
                        help="Language id to remove.")
    cunset.add_argument("--project", action="store_true",
                        help="Save under per-project scope.")
    cunset.add_argument("--cwd",
                        help="Project root (only used with --project).")
    cunset.set_defaults(fn=cmd_config_coder_unset)

    csd = coder_sub.add_parser(
        "set-default",
        help=("Set or update the GLOBAL default coder route — used "
              "when a language has no per-language entry."),
    )
    csd.add_argument("--primary",
                     help="Primary model tag for the default route.")
    csd.add_argument("--fallback", default=None,
                     help="Fallback model tag. Empty string clears "
                          "the failover model on an existing default.")
    csd.add_argument("--project", action="store_true",
                     help="Save under per-project scope.")
    csd.add_argument("--cwd",
                     help="Project root (only used with --project).")
    csd.set_defaults(fn=cmd_config_coder_set_default)

    # ----- skill-eval (M11) — wraps benchmarks/consultants/*.py ----
    se = sub.add_parser(
        "skill-eval",
        help=("Run the Consultancy Skill-Eval Protocol against one or "
              "more candidate models. See "
              "docs/consultants-skill-eval-protocol.md."),
    )
    se_sub = se.add_subparsers(dest="protocol", required=True)
    se_coder = se_sub.add_parser(
        "coder",
        help=("Run the coder suite (M11b). Picks the default for "
              "cfg.roles.coder.model."),
    )
    se_coder_mode = se_coder.add_mutually_exclusive_group(required=True)
    se_coder_mode.add_argument(
        "--dry-run", action="store_true",
        help="Stub ChatClient; validates the harness without "
             "cloud spend.",
    )
    se_coder_mode.add_argument(
        "--live", action="store_true",
        help="Real ChatClients against --ollama-base. Requires "
             "--accept-cost.",
    )
    se_coder.add_argument(
        "--accept-cost", action="store_true",
        help="Required with --live. Acknowledges Ollama-Pro token "
             "spend (see the summary line).",
    )
    se_coder.add_argument(
        "--models", default=None,
        help="Comma-separated model list. Default: the 4-model "
             "candidate set from coder_bench.py.",
    )
    se_coder.add_argument(
        "--ollama-base", default=None,
        help="Override the cloud proxy URL. Default: read from "
             "config or 192.168.178.2:11433.",
    )
    se_coder.add_argument(
        "--judge-model", default=None,
        help="Model used as the code-quality judge. Set to '' to skip.",
    )
    se_coder.add_argument(
        "--output-dir", default=None,
        help=("Per-run output directory. Default: "
              "benchmarks/consultants/results/<YYYY-MM-DD>/coder/"),
    )
    se_coder.add_argument(
        "--tier", action="append",
        choices=("trivial", "easy", "medium", "hard"),
        help="Filter by tier (repeatable). Default: all tiers.",
    )
    se_coder.add_argument(
        "--id", action="append",
        help="Filter by question id (repeatable). Default: all.",
    )
    se_coder.add_argument(
        "--smoke", action="store_true",
        help="Shorthand for --tier trivial.",
    )
    se_coder.add_argument(
        "--commit-report", action="store_true",
        dest="commit_report",
        help=(
            "After the run, force-add report.md + metadata.json "
            "(+ quota.md if present) so they're staged for the "
            "next commit alongside the baselines.md row. Does NOT "
            "create a commit."
        ),
    )
    se_coder.set_defaults(fn=cmd_skill_eval_coder)

    # ----- skill-eval stall (M11a) ----- #
    se_stall = se_sub.add_parser(
        "stall",
        help=("Run the stall suite (M11a). Measures per-model "
              "streaming-token cadence + derives recommended "
              "(stall_threshold_s, hard_cap_s) thresholds."),
    )
    se_stall_mode = se_stall.add_mutually_exclusive_group(required=True)
    se_stall_mode.add_argument(
        "--dry-run", action="store_true",
        help="Stub ChatClients; validates the harness without "
             "cloud spend.",
    )
    se_stall_mode.add_argument(
        "--live", action="store_true",
        help="Real ChatClients against --ollama-base. Requires "
             "--accept-cost.",
    )
    se_stall.add_argument(
        "--accept-cost", action="store_true",
        help="Required with --live. Acknowledges Ollama-Pro token "
             "spend (see the summary line).",
    )
    se_stall.add_argument(
        "--models", default=None,
        help="Comma-separated model list. Default: the M11b-mlang "
             "cohort + gemini-3-flash-preview (7 models).",
    )
    se_stall.add_argument(
        "--ollama-base", default=None,
        help="Override the cloud proxy URL. Default: read from "
             "config or 192.168.178.2:11433.",
    )
    se_stall.add_argument(
        "--output-dir", default=None,
        help=("Per-run output directory. Default: "
              "benchmarks/consultants/results/<YYYY-MM-DD>/stall/"),
    )
    # Tier selection — mutually exclusive on the bench too.
    se_stall_tier = se_stall.add_mutually_exclusive_group()
    se_stall_tier.add_argument(
        "--tier1", action="store_true",
        help="Run only Tier 1 (standalone chat_streamed calls).",
    )
    se_stall_tier.add_argument(
        "--tier2", action="store_true",
        help="Run only Tier 2 (full council with single-model "
             "researcher pinned to the model under test).",
    )
    se_stall_tier.add_argument(
        "--both", action="store_true",
        help="Run both tiers (default).",
    )
    se_stall.add_argument(
        "--trials-tier1", type=int, default=None,
        help="Trials per (question × model) for Tier 1. "
             "Default 3 (set by the bench).",
    )
    se_stall.add_argument(
        "--trials-tier2", type=int, default=None,
        help="Trials per (question × model) for Tier 2. "
             "Default 2 (set by the bench).",
    )
    se_stall.add_argument(
        "--id", action="append",
        help="Filter by question id (repeatable). Default: all.",
    )
    se_stall.add_argument(
        "--smoke", action="store_true",
        help="Smoke mode: 1 question per tier × 2 models × 1 trial. "
             "End-to-end validation at minimal spend.",
    )
    se_stall.set_defaults(fn=cmd_skill_eval_stall)

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
