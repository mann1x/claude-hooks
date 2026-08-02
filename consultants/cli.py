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
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional

from consultants import config as cc


# ----------------------- routing --------------------------------- #

DEFAULT_ENGINE_URL = "http://127.0.0.1:38095"
DEFAULT_FORWARDER_URL = "http://127.0.0.1:38096"

# Sentinel for a bare ``--allow-extra`` (flag present, no value) — the
# CLI resolves it to the configured ``allow_extra`` default. argparse
# uses this as the ``const`` for the nargs='?' option.
_ALLOW_EXTRA_BARE = -1


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
    if getattr(args, "skip_preflight", False):
        body["skip_preflight"] = True
    # --trace / --no-trace are deprecated in v1.1 (the JSONL trace
    # was replaced by the per-session transcript.db). The flag is
    # still accepted but no longer forwarded to the engine; warn
    # operators who pass it explicitly so they update tooling.
    if args.trace is True or args.trace is False:
        _warn_trace_deprecated()
    out = _http("POST", f"{base}/v1/consult", body=body)
    # M5: --wait → block until terminal, then print the result.
    if getattr(args, "wait", False):
        return _consult_wait(args, base, out)
    print(json.dumps({"ok": True, **out}, indent=2))
    return 0


# M5: per-run terminal statuses. Anything that is NOT "running" ends
# the poll loop; "completed" is the only one that yields a result.
_RUN_TERMINAL_OK = "completed"


def _wait_for_terminal(base: str, sid: str, *,
                       interval: float, timeout: float,
                       sleep_fn=time.sleep,
                       now_fn=time.monotonic) -> dict:
    """Poll ``GET /v1/consult/{sid}`` until ``status != "running"``.

    Returns the final status record. Raises ``CLIError`` if a positive
    ``timeout`` elapses while the run is still going (the run is NOT
    cancelled — it keeps executing server-side). ``sleep_fn`` / ``now_fn``
    are injectable for deterministic tests.
    """
    deadline = (now_fn() + timeout) if timeout and timeout > 0 else None
    while True:
        rec = _http("GET", f"{base}/v1/consult/{sid}")
        if str(rec.get("status") or "") != "running":
            return rec
        if deadline is not None and now_fn() >= deadline:
            raise CLIError(
                f"--wait timed out after {timeout:.0f}s; session {sid} "
                f"is still running (it keeps going server-side). Poll "
                f"it with `status {sid}` / `result {sid}`.",
                exit_code=1,
            )
        sleep_fn(interval)


def _fetch_result(base: str, sid: str, *,
                  attempts: int = 4, sleep_fn=time.sleep) -> dict:
    """GET the result, retrying a brief window. There's a small race
    where ``status`` flips to ``completed`` a beat before summary.md +
    metadata.json land on disk; the result route 404/409s in that gap.
    Retry a few times rather than surface the transient error."""
    last: Optional[Exception] = None
    for i in range(max(1, attempts)):
        try:
            return _http("GET", f"{base}/v1/consult/{sid}/result")
        except CLIError as e:
            last = e
            if i < attempts - 1:
                sleep_fn(0.5)
    assert last is not None
    raise last


def _consult_wait(args, base: str, run_record: dict) -> int:
    """M5 blocking path: poll the freshly-started run to a terminal
    status, then print the result (``completed``) or the failure record.
    Output shape mirrors ``result`` / ``status`` so a Workflow can parse
    ``.ok`` uniformly."""
    sid = run_record.get("sid")
    if not sid:
        # Nothing to wait on (shouldn't happen) — emit the run record.
        print(json.dumps({"ok": True, **run_record}, indent=2))
        return 0
    interval = max(0.2, float(getattr(args, "poll_interval", 2.0) or 2.0))
    timeout = float(getattr(args, "wait_timeout", 0.0) or 0.0)
    rec = _wait_for_terminal(base, sid, interval=interval, timeout=timeout)
    if str(rec.get("status") or "") == _RUN_TERMINAL_OK:
        result = _fetch_result(base, sid)
        print(json.dumps({"ok": True, **result}, indent=2))
        return 0
    # Terminal but not completed (failed / cancelled). Surface the run
    # record with ok=False so callers key on JSON, not just the rc.
    print(json.dumps({"ok": False, **rec}, indent=2))
    return 1


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
    cwd = str(Path(args.cwd or os.getcwd()).resolve())
    body["cwd"] = cwd
    if getattr(args, "add_dir", None):
        # v1.8+: extends the parent's extra_roots with this follow-up's
        # entries. The engine merges the two lists (parent first, then
        # this turn's, dedup'd) before running the executor.
        body["extra_roots"] = list(args.add_dir)
    if getattr(args, "skip_preflight", False):
        body["skip_preflight"] = True
    # Consultancy review loop: ``--allow-extra [N]`` / ``--force`` is
    # the over-cap approval carrier. In the Claude Code harness the
    # skill re-issues the followup with this flag after the user
    # approves; a bare flag resolves to the configured ``allow_extra``
    # default, an explicit N overrides. The engine raises the cap by
    # this many rounds for THIS consultancy only (never config).
    allow_extra = _resolve_allow_extra(args, cwd)
    if allow_extra is not None:
        body["allow_extra"] = allow_extra
    out = _http("POST",
                f"{base}/v1/consult/{args.parent_sid}/follow-up",
                body=body)
    # The engine returns ``ok: false`` + ``reason: followup_limit_reached``
    # (HTTP 200) when the cap is hit without an override — pass that
    # structured refusal through verbatim so the skill keys on it.
    print(json.dumps({"ok": True, **out}, indent=2))
    return 0


def _resolve_allow_extra(args, cwd: str) -> Optional[int]:
    """Resolve the ``--allow-extra`` / ``--force`` flag to the integer
    sent to the engine, or ``None`` when neither was passed.

    - ``--allow-extra`` bare (sentinel -1) or ``--force`` → the
      configured ``allow_extra`` default (read from the merged config).
    - ``--allow-extra N`` (N >= 1) → N verbatim.
    """
    raw = getattr(args, "allow_extra", None)
    force = bool(getattr(args, "force", False))
    if raw is None and not force:
        return None
    if raw is not None and raw != _ALLOW_EXTRA_BARE and raw >= 1:
        return int(raw)
    # Bare flag or --force → configured default.
    try:
        cfg = cc.load_config(Path(cwd))
        return max(1, int(cfg.allow_extra))
    except Exception:
        return 1


def cmd_accept(args, base: str) -> int:
    """Consultancy review loop: mark the consultancy ACCEPTED
    (terminal) once Claude is satisfied with the council's answer.
    Resolves any sid to its consultancy root. Idempotent."""
    body = {"cwd": str(Path(args.cwd or os.getcwd()).resolve())}
    if getattr(args, "note", None):
        body["note"] = args.note
    out = _http("POST", f"{base}/v1/consult/{args.sid}/accept", body=body)
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
    s = cfg.store
    return {
        "topology": cfg.topology,
        "effort": cfg.effort,
        "effort_budget": cfg.effort_budget,
        # Consultancy review loop: the followup cap + per-approval
        # grant size. The skill renders these and the auto-followup
        # loop respects max_followups before stopping to ask the user.
        "max_followups": cfg.max_followups,
        "allow_extra": cfg.allow_extra,
        # Adversary / verify budget (M1). adversary role enablement is
        # surfaced via the per-role block; these are the flat knobs.
        "verify_budget": cfg.verify_budget,
        "adversary_strictness": cfg.adversary_strictness,
        "adversary_checkpoint": cfg.adversary_checkpoint,
        "adversary_checkpoint_timeout_s": cfg.adversary_checkpoint_timeout_s,
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
        # M-A: the tool surface. Same rule as the store block below —
        # every knob `set-tools` can mutate must be visible here, or the
        # skill's dialog cannot show the user what they are changing.
        "tools": {
            "enabled": cfg.tools.enabled,
            "git": cfg.tools.git,
            "all_roles": cfg.tools.all_roles,
            "default_level": cfg.tools.default_level,
            "permissions": dict(cfg.tools.permissions),
        },
        # M8 + M14 (#220): expose the cross-session store block so
        # `config show` reveals the same knobs that `set-store{,-ttl,
        # -distillation}` mutate. The skill renders this; the test
        # suite asserts on the round-trip.
        "store": {
            "enabled": s.enabled,
            "backend": s.backend,
            "enable_at_efforts": list(s.enable_at_efforts),
            "recall_limit": s.recall_limit,
            "pgvector_dsn": s.pgvector_dsn,
            "pgvector_table": s.pgvector_table,
            "sqlite_vec_path": s.sqlite_vec_path,
            "embedder": s.embedder,
            "ttl": {
                "enabled": s.ttl.enabled,
                "research_days": s.ttl.research_days,
                "tool_results_hours": s.ttl.tool_results_hours,
                "project_days": s.ttl.project_days,
                "user_days": s.ttl.user_days,
                "refresh_on_read": s.ttl.refresh_on_read,
                "jitter_pct": s.ttl.jitter_pct,
            },
            "distillation": {
                "enabled": s.distillation.enabled,
                "model": s.distillation.model,
                "fallback_models": list(s.distillation.fallback_models),
                "sweep_interval_seconds": s.distillation.sweep_interval_seconds,
                "min_entries_per_distillation":
                    s.distillation.min_entries_per_distillation,
                "max_session_entries": s.distillation.max_session_entries,
                "max_groups_per_sweep": s.distillation.max_groups_per_sweep,
                "pace_seconds_between_distillations":
                    s.distillation.pace_seconds_between_distillations,
            },
        },
        "extras_active": cc.extras_active(cfg.effort),
        "mandatory_roles": sorted(cc.MANDATORY_ROLES),
        "valid_efforts": sorted(cc.EFFORT_BUDGETS),
        "valid_service_modes": sorted(cc.VALID_SERVICE_MODES),
        "valid_store_backends": list(cc.VALID_STORE_BACKENDS),
        "valid_verify_budgets": list(cc.VERIFY_BUDGET_TIERS),
        "valid_adversary_strictness": list(cc.ADVERSARY_STRICTNESS_LEVELS),
    }


def cmd_config_show(args, base: str) -> int:
    if getattr(args, "user", False):
        cfg = cc.load_config(None)            # forced user-global view
    else:
        # Default to the cwd so `config show` reflects the ACTIVE scope
        # (per-project when its override flag is on) without needing --cwd.
        cfg = cc.load_config(Path(args.cwd or os.getcwd()).resolve())
    return _emit_config(cfg, args)


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
            **_resolve_active_scope(args),
        )
    except ValueError as e:
        raise CLIError(str(e), exit_code=2) from None
    return _emit_config(cfg, args)


def cmd_config_set_effort(args, base: str) -> int:
    try:
        cfg = cc.set_effort(args.tier, **_resolve_active_scope(args))
    except ValueError as e:
        raise CLIError(str(e), exit_code=2) from None
    return _emit_config(cfg, args)


def cmd_config_set_max_followups(args, base: str) -> int:
    try:
        cfg = cc.set_max_followups(args.value, **_resolve_active_scope(args))
    except ValueError as e:
        raise CLIError(str(e), exit_code=2) from None
    return _emit_config(cfg, args)


def cmd_config_set_allow_extra(args, base: str) -> int:
    try:
        cfg = cc.set_allow_extra(args.value, **_resolve_active_scope(args))
    except ValueError as e:
        raise CLIError(str(e), exit_code=2) from None
    return _emit_config(cfg, args)


def cmd_config_set_verify_budget(args, base: str) -> int:
    try:
        cfg = cc.set_verify_budget(args.tier, **_resolve_active_scope(args))
    except ValueError as e:
        raise CLIError(str(e), exit_code=2) from None
    return _emit_config(cfg, args)


def cmd_config_set_adversary_strictness(args, base: str) -> int:
    try:
        cfg = cc.set_adversary_strictness(
            args.level, **_resolve_active_scope(args))
    except ValueError as e:
        raise CLIError(str(e), exit_code=2) from None
    return _emit_config(cfg, args)


def cmd_config_set_adversary_checkpoint(args, base: str) -> int:
    enabled = str(args.state).lower() in ("on", "true", "1", "yes", "enable")
    try:
        cfg = cc.set_adversary_checkpoint(
            enabled, timeout_s=getattr(args, "timeout", None),
            **_resolve_active_scope(args),
        )
    except ValueError as e:
        raise CLIError(str(e), exit_code=2) from None
    return _emit_config(cfg, args)


def cmd_config_set_service_mode(args, base: str) -> int:
    try:
        cfg = cc.set_service_mode(args.mode, **_resolve_active_scope(args))
    except ValueError as e:
        raise CLIError(str(e), exit_code=2) from None
    return _emit_config(cfg, args, follow_up=(
        "Run `python install.py` to install/uninstall the systemd "
        "unit, then restart `claude-hooks-daemon`."))


def cmd_config_set_override_user_global(args, base: str) -> int:
    """``config set-override-user-global on|off`` — flip the per-project
    file's ``override_user_global`` directive.

    Inherently project-scoped: the flag lives ONLY in
    ``<cwd>/.claude-hooks/consultants.toml`` and is read/written only
    there (no ``--user``/``--project``). The file is created as a full
    snapshot if absent. With the flag OFF the per-project file is
    ignored everywhere — engine and every ``config`` command read
    user-global. The emitted config + ``active_config`` reflect the
    EFFECTIVE post-flip view (``load_config`` honors the new flag)."""
    enabled = str(args.state).lower() in ("on", "true", "1", "yes", "enable")
    cwd = Path(args.cwd or os.getcwd()).resolve()
    try:
        cc.set_override_user_global(enabled, cwd=cwd)
    except (ValueError, OSError) as e:
        raise CLIError(str(e), exit_code=2) from None
    # Pin cwd so the active_config probe + post-flip load_config both
    # resolve the project file we just wrote, regardless of how --cwd
    # was (or wasn't) passed.
    args.cwd = str(cwd)
    cfg = cc.load_config(cwd)
    return _emit_config(cfg, args)


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


def _parse_cli_bool(raw: Optional[str], *, flag: str) -> Optional[bool]:
    """Parse the canonical CLI bool ladder used by set-role.

    Returns ``None`` when ``raw`` is None (= "leave unchanged"). Raises
    :class:`CLIError` on anything other than the documented synonyms
    so a typo stays loud instead of silently flipping a wrong knob.
    """
    if raw is None:
        return None
    s = raw.strip().lower()
    if s in ("true", "yes", "1", "on"):
        return True
    if s in ("false", "no", "0", "off"):
        return False
    raise CLIError(
        f"{flag} must be true/false (got {raw!r})",
        exit_code=2,
    )


def cmd_config_set_store(args, base: str) -> int:
    """``config set-store`` — top-level [store] block knobs."""
    try:
        enabled = _parse_cli_bool(args.enabled, flag="--enabled")
        cfg = cc.set_store(
            enabled=enabled,
            backend=args.backend,
            recall_limit=args.recall_limit,
            sqlite_vec_path=args.sqlite_vec_path,
            pgvector_dsn=args.pgvector_dsn,
            pgvector_table=args.pgvector_table,
            embedder=args.embedder,
            add_enable_at_effort=args.add_effort,
            remove_enable_at_effort=args.remove_effort,
            clear_enable_at_efforts=bool(args.clear_efforts),
            **_resolve_active_scope(args),
        )
    except ValueError as e:
        raise CLIError(str(e), exit_code=2) from None
    return _emit_config(cfg, args)


def cmd_config_set_tools(args, base: str) -> int:
    """``config set-tools`` — the [tools] block (M-A tool surface)."""
    try:
        perm = None
        if args.permission:
            if len(args.permission) != 2:
                raise ValueError(
                    "--permission takes exactly two values: TOOL LEVEL")
            perm = (args.permission[0], args.permission[1])
        cfg = cc.set_tools(
            enabled=_parse_cli_bool(args.enabled, flag="--enabled"),
            git=_parse_cli_bool(args.git, flag="--git"),
            all_roles=_parse_cli_bool(args.all_roles, flag="--all-roles"),
            default_level=args.default_level,
            approval_timeout_s=args.approval_timeout,
            set_permission=perm,
            clear_permission=args.clear_permission,
            clear_all_permissions=bool(args.clear_permissions),
            **_resolve_active_scope(args),
        )
    except ValueError as e:
        raise CLIError(str(e), exit_code=2) from None
    return _emit_config(cfg, args)


def cmd_config_set_store_ttl(args, base: str) -> int:
    """``config set-store-ttl`` — per-namespace TTL + #215 jitter."""
    try:
        enabled = _parse_cli_bool(args.enabled, flag="--enabled")
        refresh = _parse_cli_bool(args.refresh_on_read,
                                  flag="--refresh-on-read")
        cfg = cc.set_store_ttl(
            enabled=enabled,
            research_days=args.research_days,
            tool_results_hours=args.tool_results_hours,
            project_days=args.project_days,
            user_days=args.user_days,
            refresh_on_read=refresh,
            jitter_pct=args.jitter_pct,
            **_resolve_active_scope(args),
        )
    except ValueError as e:
        raise CLIError(str(e), exit_code=2) from None
    return _emit_config(cfg, args)


def cmd_config_set_store_distillation(args, base: str) -> int:
    """``config set-store-distillation`` — M14 sweep + #215 pacing."""
    try:
        enabled = _parse_cli_bool(args.enabled, flag="--enabled")
        cfg = cc.set_store_distillation(
            enabled=enabled,
            model=args.model,
            add_fallback_model=args.add_fallback_model,
            remove_fallback_model=args.remove_fallback_model,
            clear_fallback_models=bool(args.clear_fallback_models),
            sweep_interval_seconds=args.sweep_interval_seconds,
            min_entries_per_distillation=args.min_entries_per_distillation,
            max_session_entries=args.max_session_entries,
            max_groups_per_sweep=args.max_groups_per_sweep,
            pace_seconds_between_distillations=(
                args.pace_seconds_between_distillations
            ),
            **_resolve_active_scope(args),
        )
    except ValueError as e:
        raise CLIError(str(e), exit_code=2) from None
    return _emit_config(cfg, args)


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


def _emit_coder(cfg, args) -> int:
    """Coder-route command output: JSON (coder block + active_config) on
    stdout, scope warn on stderr. Mirrors ``_emit_config`` but carries
    the trimmed ``coder`` view the routing dialog renders."""
    active = _active_config_block(args)
    print(json.dumps(
        {"ok": True, "coder": _coder_route_block(cfg),
         "active_config": active}, indent=2))
    _warn_active_config(active)
    return 0


def cmd_config_coder_list(args, base: str) -> int:
    # ``list`` is read-only: reflect the ACTIVE scope by default (cwd),
    # matching ``config show``. ``--user`` forces the user-global view.
    if getattr(args, "user", False):
        cfg = cc.load_config(None)
    else:
        cfg = cc.load_config(Path(args.cwd or os.getcwd()).resolve())
    return _emit_coder(cfg, args)


def _resolve_active_scope(args) -> dict:
    """Resolve where a ``config set-*`` reads + writes.

    ``--user`` forces user-global; ``--project`` forces the per-project
    file (created if absent); otherwise AUTO — the per-project file iff
    it exists AND its ``override_user_global`` directive is on. ``cwd``
    defaults to the current working directory. Returns the
    ``{scope, cwd}`` kwargs the config mutators expect."""
    cwd = Path(getattr(args, "cwd", None) or os.getcwd()).resolve()
    if getattr(args, "user", False):
        return {"scope": "user", "cwd": None}
    if getattr(args, "project", False):
        return {"scope": "project", "cwd": cwd}
    if cc.project_override_active(cwd):
        return {"scope": "project", "cwd": cwd}
    return {"scope": "user", "cwd": None}


def _active_config_block(args) -> dict:
    """Describe which config scope is active for ``args`` (cwd default =
    current dir). Surfaced as the ``active_config`` field on every config
    command so the skill + direct callers see the effective scope.

    Honors the explicit ``--user`` / ``--project`` overrides so the
    reported scope matches what the command actually read/wrote; in
    AUTO it is project iff the per-project file exists AND its
    ``override_user_global`` directive is on (the ``project_override_active``
    rule)."""
    probe = Path(getattr(args, "cwd", None) or os.getcwd()).resolve()
    proj_path = cc.project_config_path(probe)
    raw = cc._read_toml(proj_path)
    exists = proj_path.exists()
    if getattr(args, "user", False):
        scope = "user"
    elif getattr(args, "project", False):
        scope = "project"
    elif cc.project_override_active(probe):
        scope = "project"
    else:
        scope = "user"
    return {
        "scope": scope,
        # Report the file's own directive only when the file has parseable
        # content. An empty / comments-only / corrupt file parses to {} —
        # `project_override_active` keys "active" on `bool(raw)`, so we
        # mirror that here (None, not the _override_flag({})→True default)
        # to keep the block and the warn from disagreeing.
        "override_user_global": (
            cc._override_flag(raw) if raw else None
        ),
        "project_config_path": str(proj_path),
        "project_file_exists": exists,
        "user_config_path": str(cc.user_config_path()),
    }


def _warn_active_config(block: dict) -> None:
    """One stderr line so direct-CLI users (and logs) always see which
    scope a config command acted on. stdout stays pure JSON. Silent only
    for plain user-global with no per-project file in play (nothing
    surprising to surface)."""
    scope = block.get("scope")
    path = block.get("project_config_path")
    exists = block.get("project_file_exists")
    flag = block.get("override_user_global")
    if scope == "project":
        # ``--project`` can force project scope onto a flag-off file, so
        # render the file's actual directive, not a hardcoded "on".
        print(f"[consultants] active config: PER-PROJECT "
              f"({path}; override_user_global={'on' if flag else 'off'}) — "
              f"reads/writes land here. Use --user for user-global.",
              file=sys.stderr)
    elif exists and flag:
        # Scope is user-global but an active per-project file exists — the
        # only way to get here is an explicit --user override (AUTO would
        # have picked the project). Make the override visible.
        print(f"[consultants] active config: user-global (forced via "
              f"--user) — a per-project file is present and normally "
              f"active ({path}).", file=sys.stderr)
    elif exists:
        print(f"[consultants] active config: user-global — a per-project "
              f"file exists but override_user_global=off ({path}).",
              file=sys.stderr)


def _emit_config(cfg, args, **extra) -> int:
    """Shared config-command output: JSON (config dump + active_config)
    on stdout, plus the scope warn on stderr. Replaces the per-handler
    print boilerplate."""
    smart = _read_claude_hooks_consultants_block().get("smart_start") or {}
    active = _active_config_block(args)
    print(json.dumps(
        {"ok": True, **_config_dump(cfg, smart_block=smart),
         "active_config": active, **extra}, indent=2))
    _warn_active_config(active)
    return 0


def _add_scope_args(p) -> None:
    """Add the standard ``--project/--user/--cwd`` scope flags to a
    ``config set-*`` subparser."""
    p.add_argument("--project", action="store_true",
                   help="Force the per-project .claude-hooks/"
                        "consultants.toml (created if absent).")
    p.add_argument("--user", action="store_true",
                   help="Force the user-global config, ignoring any active "
                        "per-project file.")
    p.add_argument("--cwd", help="Project root (default: current dir).")


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
            **_resolve_active_scope(args),
        )
    except ValueError as e:
        raise CLIError(str(e), exit_code=2) from None
    return _emit_coder(cfg, args)


def cmd_config_coder_unset(args, base: str) -> int:
    try:
        cfg = cc.unset_coder_route(
            args.language, **_resolve_active_scope(args))
    except ValueError as e:
        raise CLIError(str(e), exit_code=2) from None
    return _emit_coder(cfg, args)


def cmd_config_coder_set_default(args, base: str) -> int:
    try:
        cfg = cc.set_coder_default_route(
            primary=args.primary,
            fallback=args.fallback,
            **_resolve_active_scope(args),
        )
    except ValueError as e:
        raise CLIError(str(e), exit_code=2) from None
    return _emit_coder(cfg, args)


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
    # Human summary of any injections to stderr (stdout stays JSON).
    injs = out.get("injections") or []
    if injs:
        print(f"injections ({len(injs)}):", file=sys.stderr)
        for inj in injs:
            print(
                f"  {inj.get('id')}  status={inj.get('status')}"
                f"  phase={inj.get('phase_at_apply')}"
                f"  target={inj.get('target_role')}"
                f"  routed={inj.get('routed')}",
                file=sys.stderr,
            )
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
    # Concise human summary to stderr; full JSON (with the uniform
    # status contract) stays on stdout for tooling.
    summary = (
        f"inject {out.get('status')}: id={out.get('injection_id')}"
        f" routed={out.get('routed')}"
    )
    for extra in ("queue_position", "reason", "error"):
        if out.get(extra) is not None:
            summary += f" {extra}={out.get(extra)}"
    print(summary, file=sys.stderr)
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
    # M4: ``--adversarial-focus ""`` is a meaningful "clear it" signal,
    # so test against None (flag omitted) not falsiness.
    if getattr(args, "adversarial_focus", None) is not None:
        rc["adversarial_focus"] = args.adversarial_focus
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
            "--confidence / --strictness / --adversarial-focus / "
            "--enable / --disable",
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


def cmd_adversary_ack(args, base: str) -> int:
    """POST /v1/consult/<sid>/adversary-ack — release the M2 adversary
    checkpoint early. Sets a flag the runner's checkpoint poll reads; it
    does NOT re-invoke the graph (the runner is the sole resumer). Safe
    to call when no checkpoint is open — a harmless no-op."""
    body = {"reason": args.reason or "adversary-ready"}
    out = _http("POST",
                f"{base}/v1/consult/{args.sid}/adversary-ack", body=body)
    print(json.dumps({"ok": True, **out}, indent=2))
    return 0


def cmd_cancel(args, base: str) -> int:
    """POST /v1/consult/<sid>/cancel — flip cancel_requested.
    ``--keep-partial`` is the default; pass ``--discard-partial`` to
    delete the checkpoint file too.

    Note what the default does *not* do: no node reads
    ``cancel_requested`` (audit 2026-08-02), so on a run that is
    mid-graph a keep-partial cancel records the request and the run
    streams to completion. ``--discard-partial`` closes the session,
    which the runner's wait loops do break on. The response carries
    ``stops_the_run`` so the distinction is visible rather than
    inferred.
    """
    body = {
        "discard_partial": bool(args.discard_partial),
        "reason": args.reason or "user-cancel",
    }
    out = _http("POST",
                f"{base}/v1/consult/{args.sid}/cancel", body=body)
    print(json.dumps({"ok": True, **out}, indent=2))
    if not out.get("stops_the_run", True):
        print(
            "note: cancel recorded, but the run is not stopped — no node "
            "acts on the flag. Use --discard-partial to close the "
            "session.",
            file=sys.stderr,
        )
    return 0


#: Event kinds worth a human's attention. ``llm_call`` / ``tool_call``
#: are the bulk of the stream — a single ``xhigh`` council emits
#: hundreds, each with a full payload — and reading them is how the
#: consumer loses the events that actually demand a response. These
#: are the ones that mark a state change.
_MILESTONE_KINDS = (
    "node_enter", "node_exit", "awaiting_adversary",
    "adversary_resumed", "interrupt", "error", "complete", "lifecycle",
    # M-A approval channel. ``awaiting_tool_approval`` is the one event
    # in the stream that BLOCKS a lane until someone answers, so it must
    # never be filtered out of the monitor view.
    "awaiting_tool_approval", "tool_approval_resolved",
    "tool_approval_auto",
)


def _compact_event_line(event_type: str, data: dict) -> str:
    """One line per event: time, kind, and only the fields that
    distinguish this event from the next one of the same kind."""
    import datetime as _dt

    ts = data.get("ts")
    when = ""
    if isinstance(ts, (int, float)):
        when = _dt.datetime.fromtimestamp(ts).strftime("%H:%M:%S") + " "
    bits = []
    for key in ("role", "round", "lane_idx", "status", "reason",
                "final_answer_present", "deadline_ts", "timeout_s",
                # approval-channel fields: an operator staring at a
                # parked lane needs the tool, the id to ack, and the
                # verdict once it lands.
                "tool", "level", "request_id", "resolution", "allowed"):
        val = data.get(key)
        if val not in (None, ""):
            bits.append(f"{key}={val}")
    return f"{when}{event_type:<18} " + " ".join(bits)


def cmd_tool_ack(args, base: str) -> int:
    """``tool-ack <sid> --allow|--deny`` — answer a parked ``ask_human``
    tool-approval request.

    There is deliberately no default verdict: guessing either way is
    the failure the channel exists to prevent, so ``--allow`` /
    ``--deny`` is a required, mutually-exclusive pair.

    ``--all-of-tool`` / ``--all-matching <glob>`` answer for a class of
    calls instead of one. The rule is session-scoped — authorization is
    per council, so every role and x-tier lane inherits it — and it
    releases the already-parked requests it matches. Answer one call at
    a time on a wide council and the queue outruns you: the first live
    run parked four ``read_file`` requests in 90 seconds.
    """
    body: dict = {"allow": bool(args.allow)}
    if args.request_id:
        body["request_id"] = args.request_id
    if args.reason:
        body["reason"] = args.reason
    if args.all_matching:
        body["scope"] = "glob"
        body["pattern"] = args.all_matching
    elif args.all_of_tool:
        body["scope"] = "tool"
    out = _http("POST", f"{base}/v1/consult/{args.sid}/tool-ack",
                body=body)
    print(json.dumps({"ok": True, **out}, indent=2))
    still = out.get("pending") or []
    if still and not (args.all_matching or args.all_of_tool):
        print(
            f"note: {len(still)} request(s) still parked. "
            "--all-of-tool or --all-matching '<glob>' answers the class "
            "instead of one call at a time.",
            file=sys.stderr,
        )
    return 0


def cmd_events(args, base: str) -> int:
    """GET /v1/consult/<sid>/events — SSE stream over runtime_events.

    Streams indefinitely (until the session terminates or the user
    hits ^C). The endpoint supports Last-Event-ID resume; pass
    ``--since`` to skip events older than the given id.

    By default the raw SSE records are printed verbatim, which is what
    a machine consumer wants and what a human consumer drowns in. Pass
    ``--milestones`` (or ``--kinds a,b``) to filter to state changes
    and render one compact line each — the form in which "the council
    is waiting for you" is actually visible.
    """
    kinds: Optional[set] = None
    if getattr(args, "kinds", None):
        kinds = {k.strip() for k in args.kinds.split(",") if k.strip()}
    elif getattr(args, "milestones", False):
        kinds = set(_MILESTONE_KINDS)
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
    def _emit(record_lines: list[str]) -> None:
        if kinds is None:
            print("\n".join(record_lines))
            print()  # blank line separator in the CLI output
            return
        event_type = ""
        payload: dict = {}
        for ln in record_lines:
            if ln.startswith("event:"):
                event_type = ln[len("event:"):].strip()
            elif ln.startswith("data:"):
                try:
                    payload = json.loads(ln[len("data:"):].strip())
                except (ValueError, TypeError):
                    payload = {}
        # ``kind`` inside the payload is authoritative when present —
        # the SSE ``event:`` name is derived from it but a few
        # lifecycle records carry a coarser type.
        kind = payload.get("kind") or event_type
        if kind not in kinds and event_type not in kinds:
            return
        print(_compact_event_line(kind or event_type, payload),
              flush=True)

    try:
        record_lines: list[str] = []
        while True:
            raw = resp.readline()
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
            if line == "":
                if record_lines:
                    _emit(record_lines)
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


def cmd_skill_eval_tool_executor(args, base: str) -> int:
    """Run the tool_executor skill-eval suite (M11c). Thin wrapper
    around ``benchmarks.consultants.tool_executor_bench.main`` —
    same shape as :func:`cmd_skill_eval_coder` and
    :func:`cmd_skill_eval_stall` but the suite measures the
    tool_executor role (reading + reasoning over a codebase via
    tool calls), not code generation or streaming cadence.

    Exit codes mirror the bench script: 0 on success, 1 on no
    trials (empty match), 2 when --live is set but --accept-cost
    isn't.
    """
    try:
        from benchmarks.consultants.tool_executor_bench import (
            main as _bench_main,
        )
    except ImportError as e:
        raise CLIError(
            "benchmarks.consultants.tool_executor_bench is not "
            "importable. Run from the repo root or set PYTHONPATH. "
            f"Underlying: {e}"
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
    # ``--judge-model`` distinguishes "user set ''" (disable judge)
    # from "user didn't pass the flag" (use bench default). The
    # CLI's default is None ⇒ forward only when explicitly set,
    # mirroring the coder bench's treatment.
    if args.judge_model is not None:
        argv.extend(["--judge-model", args.judge_model])
    if args.trials is not None:
        argv.extend(["--trials", str(args.trials)])
    if args.output_dir:
        argv.extend(["--output-dir", args.output_dir])
    for t in (args.tier or []):
        argv.extend(["--tier", t])
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
    c.add_argument(
        "--skip-preflight", dest="skip_preflight", action="store_true",
        help=(
            "Start even when none of the files the question names are "
            "readable under the session's roots. The pre-flight exists "
            "because a council that cannot see its subject answers "
            "confidently from nothing and only shows it 55 minutes "
            "later; skip it when the question is greenfield (every path "
            "it names is one you want created) and you know the roots "
            "are right."
        ),
    )
    # M5: --wait turns the otherwise-async consult into a blocking call —
    # POST, then poll until terminal, then print the RESULT (same shape
    # as `result`) instead of the initial run record. Removes the
    # Workflow-authoring footgun of hand-rolling a poll loop. The bare
    # (no --wait) path is byte-identical to before.
    c.add_argument(
        "--wait", action="store_true", default=False,
        help="Block until the consultation finishes, then print the "
             "final result (poll loop). Without it, returns the run "
             "record immediately and you poll `status`/`result`.",
    )
    c.add_argument(
        "--poll-interval", dest="poll_interval", type=float, default=2.0,
        metavar="SECONDS",
        help="Seconds between status polls when --wait is set "
             "(default 2.0; floored at 0.2).",
    )
    c.add_argument(
        "--wait-timeout", dest="wait_timeout", type=float, default=0.0,
        metavar="SECONDS",
        help="Client-side ceiling for --wait, in seconds. 0 (default) "
             "waits indefinitely — the engine has its own deadline. A "
             "positive value aborts the wait (the run keeps going "
             "server-side; poll it later).",
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
    fu.add_argument(
        "--skip-preflight", dest="skip_preflight", action="store_true",
        help=(
            "Start even when none of the files the question names are "
            "readable under the session's roots. The pre-flight exists "
            "because a council that cannot see its subject answers "
            "confidently from nothing and only shows it 55 minutes "
            "later; skip it when the question is greenfield (every path "
            "it names is one you want created) and you know the roots "
            "are right."
        ),
    )
    fu.add_argument(
        "--allow-extra", dest="allow_extra", nargs="?", type=int,
        const=_ALLOW_EXTRA_BARE, default=None, metavar="N",
        help=(
            "Consultancy review loop: authorize follow-ups past the "
            "max_followups cap for THIS consultancy only (one-off, no "
            "config change). Bare `--allow-extra` grants the configured "
            "allow_extra default; `--allow-extra N` grants N. In the "
            "Claude Code harness the skill adds this after you approve "
            "in chat; it's also the escape hatch for scripted callers."
        ),
    )
    fu.add_argument(
        "--force", action="store_true", default=False,
        help="Alias for a bare --allow-extra (grant the configured "
             "default number of extra rounds for this consultancy).",
    )
    fu.set_defaults(fn=cmd_follow_up)

    # accept — consultancy review loop: mark the consultancy ACCEPTED.
    ac = sub.add_parser(
        "accept",
        help="Mark a consultancy ACCEPTED (terminal) once you're "
             "satisfied with the council's answer. Accepts any sid in "
             "the chain; resolves to the consultancy root.")
    ac.add_argument("sid")
    ac.add_argument("--cwd", help="Project root (default: cwd).")
    ac.add_argument("--note", help="Optional note recorded with the "
                                   "acceptance.")
    ac.set_defaults(fn=cmd_accept)

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
        choices=("lax", "normal", "strict", "adversarial"),
        help="Critic strictness preset. 'adversarial' is the live-only "
             "M4 dial — actively hunt to break the evidence.",
    )
    ctl.add_argument(
        "--adversarial-focus",
        dest="adversarial_focus", default=None,
        help="Free-text attack brief threaded into the next critic + "
             "meta-critic prompt (M4). Pass '' to clear it.",
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

    # adversary-ack — release the M2 adversary checkpoint early.
    ack = sub.add_parser(
        "adversary-ack",
        help="Release the engine adversary checkpoint early (after "
             "injecting your red-team brief). No-op if no checkpoint is "
             "open; the council auto-resumes at its deadline regardless.",
    )
    ack.add_argument("sid")
    ack.add_argument("--reason", default="adversary-ready",
                     help="Human-readable ack reason (logged).")
    ack.set_defaults(fn=cmd_adversary_ack)

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

    # tool-ack — answer a parked ask_human tool approval (M-A).
    ta = sub.add_parser(
        "tool-ack",
        help="Approve or deny a parked tool call awaiting approval.",
    )
    ta.add_argument("sid")
    grp = ta.add_mutually_exclusive_group(required=True)
    grp.add_argument("--allow", dest="allow", action="store_true",
                     help="Approve the parked call.")
    grp.add_argument("--deny", dest="allow", action="store_false",
                     help="Refuse it. The lane gets an error string and "
                          "reroutes; it does not crash.")
    ta.add_argument("--request-id", dest="request_id", default=None,
                    help="Which request to answer. Omit to answer the "
                         "oldest pending one (the common case of a "
                         "single parked call).")
    ta.add_argument("--reason", default=None,
                    help="Recorded with the decision for the "
                         "post-mortem.")
    scope_grp = ta.add_mutually_exclusive_group()
    scope_grp.add_argument(
        "--all-of-tool", dest="all_of_tool", action="store_true",
        help="Apply the verdict to EVERY future call to this tool in "
             "this council, and release the parked ones it covers.",
    )
    scope_grp.add_argument(
        "--all-matching", dest="all_matching", default=None,
        metavar="GLOB",
        help="Apply the verdict to calls to this tool whose target "
             "matches GLOB (e.g. 'src/**', '*.py'). Session-scoped: "
             "every role and x-tier lane inherits it.",
    )
    ta.set_defaults(fn=cmd_tool_ack)

    # events — SSE stream.
    ev = sub.add_parser(
        "events",
        help="Tail the SSE event stream for a session.",
    )
    ev.add_argument("sid")
    ev.add_argument(
        "--milestones", action="store_true",
        help=(
            "Filter to state-change events (node_enter / node_exit / "
            "awaiting_adversary / interrupt / error / complete) and "
            "render one compact line each. The unfiltered stream is "
            "dominated by llm_call and tool_call records — hundreds "
            "per council — which is how a human consumer misses the "
            "events that need an answer."
        ),
    )
    ev.add_argument(
        "--kinds", default=None, metavar="A,B",
        help=("Comma-separated event kinds to keep (implies the "
              "compact renderer). Overrides --milestones."),
    )
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
                    help="Project root (loads project overrides too; "
                         "default: current dir → shows the ACTIVE scope).")
    cs.add_argument("--user", action="store_true",
                    help="Force the user-global view, ignoring any active "
                         "per-project file.")
    cs.set_defaults(fn=cmd_config_show)

    cr = cfg_sub.add_parser("set-role", help="Configure one role.")
    cr.add_argument("role", choices=cc.ROLES)
    cr.add_argument("--model", help="Ollama tag for this role.")
    cr.add_argument("--ctx",
                    help="Pin a context length (or 'auto' / 0 to clear).")
    cr.add_argument("--enabled",
                    help="true|false — toggle the role on/off.")
    _add_scope_args(cr)
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
    _add_scope_args(ce)
    ce.set_defaults(fn=cmd_config_set_effort)

    cmf = cfg_sub.add_parser(
        "set-max-followups",
        help="Set the consultancy followup cap (>= 0). Auto-issued "
             "followups stop at this many before the skill asks you "
             "to allow more.")
    cmf.add_argument("value", type=int)
    _add_scope_args(cmf)
    cmf.set_defaults(fn=cmd_config_set_max_followups)

    cae = cfg_sub.add_parser(
        "set-allow-extra",
        help="Set the per-approval grant size (>= 1) — how many extra "
             "followups each over-cap approval adds for a consultancy.")
    cae.add_argument("value", type=int)
    _add_scope_args(cae)
    cae.set_defaults(fn=cmd_config_set_allow_extra)

    cvb = cfg_sub.add_parser(
        "set-verify-budget",
        help="Set the Workflow skeptic-panel budget "
             "(minimal | bounded | generous).")
    cvb.add_argument("tier", choices=cc.VERIFY_BUDGET_TIERS)
    _add_scope_args(cvb)
    cvb.set_defaults(fn=cmd_config_set_verify_budget)

    cas = cfg_sub.add_parser(
        "set-adversary-strictness",
        help="Set adversary / critic-dial strictness "
             "(soft | normal | strict).")
    cas.add_argument("level", choices=cc.ADVERSARY_STRICTNESS_LEVELS)
    _add_scope_args(cas)
    cas.set_defaults(fn=cmd_config_set_adversary_strictness)

    cac = cfg_sub.add_parser(
        "set-adversary-checkpoint",
        help="Enable/disable the engine adversary checkpoint "
             "(pause-for-red-team before synthesis); optional --timeout.")
    cac.add_argument("state", choices=("on", "off"))
    cac.add_argument("--timeout", type=int, default=None,
                     help="Auto-resume timeout in seconds (>= 1).")
    _add_scope_args(cac)
    cac.set_defaults(fn=cmd_config_set_adversary_checkpoint)

    cog = cfg_sub.add_parser(
        "set-override-user-global",
        help="Flip the per-project override_user_global directive on|off. "
             "Project-scoped only: lives in <cwd>/.claude-hooks/"
             "consultants.toml (created if absent). OFF makes the engine "
             "and every config command ignore the per-project file.")
    cog.add_argument("state", choices=("on", "off"))
    cog.add_argument("--cwd", help="Project root (default: current dir).")
    cog.set_defaults(fn=cmd_config_set_override_user_global)

    csm = cfg_sub.add_parser("set-service-mode",
                             help="Set service mode "
                             "(always-on | smart-start).")
    csm.add_argument("mode", choices=cc.VALID_SERVICE_MODES)
    _add_scope_args(csm)
    csm.set_defaults(fn=cmd_config_set_service_mode)

    cit = cfg_sub.add_parser("set-idle-timeout",
                             help="Smart-start idle timeout (seconds).")
    cit.add_argument("seconds", type=int)
    cit.set_defaults(fn=cmd_config_set_idle_timeout)

    # ----- #220: [store] / [store.ttl] / [store.distillation] -----
    # Pre-#220 these blocks were TOML-only. The three subparsers below
    # mirror the set-role pattern: every knob is optional, None means
    # "leave unchanged", and validation lives in consultants.config.
    css = cfg_sub.add_parser(
        "set-store",
        help=("Configure the [store] block (cross-session memory). "
              "Pre-#220 this required hand-editing the TOML."),
    )
    css.add_argument("--enabled",
                     help="true|false — toggle the store on/off.")
    css.add_argument("--backend",
                     choices=cc.VALID_STORE_BACKENDS,
                     help="memory | pgvector | sqlite_vec.")
    css.add_argument("--recall-limit", dest="recall_limit", type=int,
                     help="Top-K results returned by peer_findings recall "
                          "(default 5).")
    css.add_argument("--sqlite-vec-path", dest="sqlite_vec_path",
                     help="Override path for sqlite_vec backend "
                          "(default ~/.claude/consultants-store.db).")
    css.add_argument("--pgvector-dsn", dest="pgvector_dsn",
                     help="postgres://user:pass@host:5432/db for pgvector.")
    css.add_argument("--pgvector-table", dest="pgvector_table",
                     help="Table name on the pgvector backend (default "
                          "consultants_store).")
    css.add_argument("--embedder",
                     help="Embedder identifier (e.g. llamafile / ollama).")
    css.add_argument("--add-effort", dest="add_effort",
                     help="Enable the store at this effort tier "
                          "(low|medium|high|max|xmedium|xhigh|xmax). "
                          "Default set = high/max/xhigh/xmax. Idempotent.")
    css.add_argument("--remove-effort", dest="remove_effort",
                     help="Drop a tier from enable_at_efforts.")
    css.add_argument("--clear-efforts", dest="clear_efforts",
                     action="store_true",
                     help="Empty enable_at_efforts (store becomes "
                          "inert at every tier).")
    _add_scope_args(css)
    css.set_defaults(fn=cmd_config_set_store)

    ctl = cfg_sub.add_parser(
        "set-tools",
        help=("Configure the [tools] block — which tools the council can "
              "reach and what each one needs to run."),
    )
    ctl.add_argument("--enabled",
                     help="true|false — the composable tool registry. "
                          "false restores the fixed builtin surface.")
    ctl.add_argument("--git",
                     help="true|false — read-only git history tools: "
                          "git_history (\"when did this regress?\" via "
                          "git log -L), git_log / blame / diff / show.")
    ctl.add_argument("--all-roles", dest="all_roles",
                     help="true|false — give every role the same tools as "
                          "the researcher (planner / critic / meta_critic / "
                          "synthesizer / adversary). Costs one LLM call per "
                          "tool iteration per role per lane.")
    ctl.add_argument("--default-level", dest="default_level",
                     choices=cc.VALID_PERMISSION_LEVELS,
                     help="Rung for a tool nothing else names. "
                          "auto runs silently; ask_assistant routes to "
                          "the assistant; ask_human needs a person; "
                          "deny refuses.")
    ctl.add_argument("--approval-timeout", dest="approval_timeout",
                     type=float, default=None, metavar="SECONDS",
                     help="How long a parked ask_human tool call waits "
                          "before it is DENIED (default 600, minimum "
                          "180). Only ask_human parks — ask_assistant "
                          "auto-approves per the ladder — so this is "
                          "the spend gate. Timeout denies on purpose: "
                          "absence of an approver never authorizes "
                          "spend. The floor exists because a shorter "
                          "deadline is un-answerable once poll latency "
                          "and a human decision are in the loop, and a "
                          "deadline nobody can meet is 'deny' that also "
                          "costs the wall-clock.")
    ctl.add_argument("--permission", nargs=2, metavar=("TOOL", "LEVEL"),
                     help="Pin one tool to a rung, e.g. "
                          "--permission git_diff auto.")
    ctl.add_argument("--clear-permission", dest="clear_permission",
                     metavar="TOOL",
                     help="Drop one tool's pin (back to its default).")
    ctl.add_argument("--clear-permissions", dest="clear_permissions",
                     action="store_true",
                     help="Drop every per-tool pin.")
    _add_scope_args(ctl)
    ctl.set_defaults(fn=cmd_config_set_tools)

    cst = cfg_sub.add_parser(
        "set-store-ttl",
        help=("Configure the [store.ttl] block: per-namespace TTL, "
              "refresh-on-read, #215 jitter."),
    )
    cst.add_argument("--enabled",
                     help="true|false — master TTL switch.")
    cst.add_argument("--research-days", dest="research_days", type=float,
                     help="Days before research entries expire "
                          "(default 30). 0 or negative = never.")
    cst.add_argument("--tool-results-hours", dest="tool_results_hours",
                     type=float,
                     help="Hours before tool_results entries expire "
                          "(default 24). 0 or negative = never.")
    cst.add_argument("--project-days", dest="project_days", type=float,
                     help="Days before ('project', pid) entries expire "
                          "(default never). 0 or negative = never.")
    cst.add_argument("--user-days", dest="user_days", type=float,
                     help="Days before ('user', uid) entries expire "
                          "(default never). 0 or negative = never.")
    cst.add_argument("--refresh-on-read", dest="refresh_on_read",
                     help="true|false — bump expires_at forward on every "
                          "successful recall hit (default true).")
    cst.add_argument("--jitter-pct", dest="jitter_pct", type=float,
                     help="#215 cohort jitter — 0.0..1.0. Spreads aligned "
                          "writes across ±jitter of the nominal TTL so "
                          "the reaper doesn't see N sessions expire on "
                          "one tick. Default 0.1 (=±10%%).")
    _add_scope_args(cst)
    cst.set_defaults(fn=cmd_config_set_store_ttl)

    csd = cfg_sub.add_parser(
        "set-store-distillation",
        help=("Configure the [store.distillation] block: M14 sweep + "
              "#215 cap + pace."),
    )
    csd.add_argument("--enabled",
                     help="true|false — master distillation switch. "
                          "OFF = research originals just delete at TTL.")
    csd.add_argument("--model",
                     help="Primary distillation LLM "
                          "(default gemma4:31b-cloud).")
    csd.add_argument("--add-fallback-model", dest="add_fallback_model",
                     help="Append a model to the fallback chain "
                          "(idempotent; dedup'd against the primary).")
    csd.add_argument("--remove-fallback-model",
                     dest="remove_fallback_model",
                     help="Drop a model from the fallback chain.")
    csd.add_argument("--clear-fallback-models",
                     dest="clear_fallback_models",
                     action="store_true",
                     help="Empty the fallback chain (primary-only).")
    csd.add_argument("--sweep-interval-seconds",
                     dest="sweep_interval_seconds", type=float,
                     help="Reaper sweep cadence (default 3600 = 1 h, "
                          "minimum 30).")
    csd.add_argument("--min-entries-per-distillation",
                     dest="min_entries_per_distillation", type=int,
                     help="Cost gate — research groups below this "
                          "count delete without LLM call (default 3).")
    csd.add_argument("--max-session-entries",
                     dest="max_session_entries", type=int,
                     help="Truncation cap before prompt assembly "
                          "(default 50; bounds per-call token cost).")
    csd.add_argument("--max-groups-per-sweep",
                     dest="max_groups_per_sweep", type=int,
                     help="#215 — cap successful distillations per "
                          "sweep tick. Cost-gated skips and tool_results "
                          "deletes don't count. Default 5; 0 = uncapped.")
    csd.add_argument("--pace-seconds-between-distillations",
                     dest="pace_seconds_between_distillations", type=float,
                     help="#215 — sleep N seconds between consecutive "
                          "distillations within one sweep. Default 5.0; "
                          "0.0 disables. Sliced 0.5 s for shutdown.")
    _add_scope_args(csd)
    csd.set_defaults(fn=cmd_config_set_store_distillation)

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
              "default). The map is seeded from the coder_med v1.0 "
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
                    help="Project root (loads project overrides too; "
                         "default: current dir → ACTIVE scope).")
    cl.add_argument("--user", action="store_true",
                    help="Force the user-global view.")
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
                      help="Primary model tag (e.g. glm-5.2:cloud).")
    cset.add_argument("--fallback", default=None,
                      help="Fallback model tag. Empty string clears "
                           "the failover model on an existing entry.")
    _add_scope_args(cset)
    cset.set_defaults(fn=cmd_config_coder_set)

    cunset = coder_sub.add_parser(
        "unset",
        help=("Remove a per-language coder route. The language then "
              "falls through to the global default route. Idempotent."),
    )
    cunset.add_argument("language",
                        help="Language id to remove.")
    _add_scope_args(cunset)
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
    _add_scope_args(csd)
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

    # ----- skill-eval tool_executor (M11c) ----- #
    se_te = se_sub.add_parser(
        "tool_executor",
        help=("Run the tool_executor suite (M11c). Picks the default "
              "for cfg.roles.tool_executor.model based on the 8-"
              "question × 4-tier reading + reasoning corpus."),
    )
    se_te_mode = se_te.add_mutually_exclusive_group(required=True)
    se_te_mode.add_argument(
        "--dry-run", action="store_true",
        help="Stub ChatClients; validates the harness without "
             "cloud spend.",
    )
    se_te_mode.add_argument(
        "--live", action="store_true",
        help="Real ChatClients against --ollama-base. Requires "
             "--accept-cost.",
    )
    se_te.add_argument(
        "--accept-cost", action="store_true",
        help="Required with --live. Acknowledges Ollama-Pro token "
             "spend (see the summary line).",
    )
    se_te.add_argument(
        "--models", default=None,
        help="Comma-separated model list. Default: the M11b-mlang "
             "cohort minus deepseek-v4-flash + "
             "gemini-3-flash-preview (6 models).",
    )
    se_te.add_argument(
        "--ollama-base", default=None,
        help="Override the cloud proxy URL. Default: read from "
             "config or 192.168.178.2:11433.",
    )
    se_te.add_argument(
        "--judge-model", default=None,
        help="Model used as the answer-quality judge (one call "
             "per trial). Set to '' to skip. Default: "
             "gemma4:31b-cloud.",
    )
    se_te.add_argument(
        "--trials", type=int, default=None,
        help="Trials per (question × model). Default 1 (set by "
             "the bench).",
    )
    se_te.add_argument(
        "--output-dir", default=None,
        help=("Per-run output directory. Default: "
              "benchmarks/consultants/results/<YYYY-MM-DD>/"
              "tool_executor/"),
    )
    se_te.add_argument(
        "--tier", action="append",
        choices=("trivial", "easy", "medium", "hard"),
        help="Filter by tier (repeatable). Default: all tiers.",
    )
    se_te.add_argument(
        "--id", action="append",
        help="Filter by question id (repeatable). Default: all.",
    )
    se_te.add_argument(
        "--smoke", action="store_true",
        help="Smoke mode: trivial tier × 1 model × 1 trial. "
             "End-to-end validation at minimal spend.",
    )
    se_te.set_defaults(fn=cmd_skill_eval_tool_executor)

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
    except OSError as e:
        # Safety net for config-write failures (read-only FS, permission
        # denied, unwritable .claude-hooks dir) from the project-scope
        # set-* handlers, which catch only ValueError. Emit the same
        # structured shape every CLIError path guarantees instead of a
        # raw traceback.
        print(json.dumps({"ok": False, "error": f"filesystem error: {e}"}),
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
