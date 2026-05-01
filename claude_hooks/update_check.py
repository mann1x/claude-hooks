"""Self-update check for claude-hooks.

Polls GitHub releases at most once every 24 hours and surfaces a
notice in the Stop hook when a newer tag is published. Designed to
run on the long-lived ``claude-hooks-daemon`` so the Stop hook
itself never blocks on network I/O.

Behaviour
---------
- 24-hour cadence: ``run_due_check()`` is a no-op until the
  configured interval has elapsed since the last attempt.
- Retries: a failed check is retried up to ``max_retries`` times at
  ``retry_pause_seconds`` apart. After the final failure the next
  attempt is deferred until the normal 24h window expires.
- Silent on failure: timeouts, DNS errors, JSON parse errors all
  resolve to "no update available"; nothing is raised to the caller.
- Notification budget: when an update is found, the Stop hook emits
  the notice at most ``max_notifications`` times, then silences it
  until the next successful check finds either a newer release or
  the user has upgraded.
- Runtime disable: ``update_check.enabled = false`` in
  ``config/claude-hooks.json`` stops both the daemon poll and the
  Stop-hook notification immediately.

State file
----------
Persists in ``~/.claude/claude-hooks-update-state.json``. Schema is
forward-compatible: unknown keys are preserved.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from claude_hooks import __version__ as CURRENT_VERSION

log = logging.getLogger(__name__)

DEFAULT_STATE_PATH = Path.home() / ".claude" / "claude-hooks-update-state.json"
DEFAULT_REPO = "mann1x/claude-hooks"
DEFAULT_INTERVAL_SECONDS = 24 * 60 * 60       # 24h
DEFAULT_RETRY_PAUSE_SECONDS = 5 * 60          # 5min
DEFAULT_MAX_RETRIES = 5
DEFAULT_TIMEOUT_SECONDS = 5
DEFAULT_MAX_NOTIFICATIONS = 10

_SEMVER_RX = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$")


# --------------------------------------------------------------------------- #
# Version comparison
# --------------------------------------------------------------------------- #
def parse_version(s: str) -> Optional[tuple[int, int, int]]:
    """Parse ``vX.Y.Z`` / ``X.Y.Z`` (with optional pre-release suffix).

    Returns ``None`` for unparseable input rather than raising — the
    update path must never crash the caller.
    """
    if not isinstance(s, str):
        return None
    m = _SEMVER_RX.match(s.strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def is_newer(latest: str, current: str) -> bool:
    """``True`` iff ``latest`` is strictly greater than ``current``.

    Pre-release suffixes are ignored for the comparison; we only ship
    GA tags publicly. Returns ``False`` on any parse failure so we
    never accidentally announce a phantom upgrade.
    """
    a = parse_version(latest)
    b = parse_version(current)
    if a is None or b is None:
        return False
    return a > b


# --------------------------------------------------------------------------- #
# Config + state
# --------------------------------------------------------------------------- #
def get_cfg(config: dict) -> dict:
    """Resolve ``update_check`` section with defaults filled in."""
    raw = (config.get("update_check") or {}) if isinstance(config, dict) else {}
    return {
        "enabled": bool(raw.get("enabled", False)),
        "interval_seconds": int(raw.get("interval_seconds", DEFAULT_INTERVAL_SECONDS)),
        "retry_pause_seconds": int(raw.get("retry_pause_seconds", DEFAULT_RETRY_PAUSE_SECONDS)),
        "max_retries": int(raw.get("max_retries", DEFAULT_MAX_RETRIES)),
        "github_repo": str(raw.get("github_repo", DEFAULT_REPO)),
        "timeout_seconds": int(raw.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)),
        "max_notifications": int(raw.get("max_notifications", DEFAULT_MAX_NOTIFICATIONS)),
        "state_path": str(raw.get("state_path") or DEFAULT_STATE_PATH),
    }


def _state_path(cfg: dict) -> Path:
    return Path(cfg["state_path"]).expanduser()


def load_state(cfg: dict) -> dict:
    """Load persisted state. Missing file → empty dict (fresh install)."""
    path = _state_path(cfg)
    try:
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        log.debug("update_check: state read failed (%s); starting fresh", e)
        return {}


def save_state(cfg: dict, state: dict) -> None:
    """Atomic-replace write to the state file. Silent on error."""
    path = _state_path(cfg)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except Exception as e:
        log.debug("update_check: state write failed: %s", e)


# --------------------------------------------------------------------------- #
# Scheduler — when should the next check run?
# --------------------------------------------------------------------------- #
def should_run_check(state: dict, cfg: dict, *, now: Optional[float] = None) -> bool:
    """Decide whether the daemon thread should fire a check now.

    Returns ``True`` when:
    - never run before, OR
    - in retry window AND ``next_retry_at`` has passed, OR
    - past the 24h interval since the last attempt.

    Returns ``False`` while a retry is scheduled in the future, or
    while we're still inside the post-success cooldown.
    """
    if not cfg["enabled"]:
        return False
    now = now if now is not None else time.time()

    next_retry_at = state.get("next_retry_at")
    if isinstance(next_retry_at, (int, float)):
        if now < next_retry_at:
            return False
        return True

    last_attempt = state.get("last_check_at")
    if not isinstance(last_attempt, (int, float)):
        return True
    return (now - last_attempt) >= cfg["interval_seconds"]


# --------------------------------------------------------------------------- #
# GitHub fetch
# --------------------------------------------------------------------------- #
def fetch_latest_tag(repo: str, *, timeout: float) -> Optional[str]:
    """GET ``api.github.com/repos/{repo}/releases/latest``. Returns the
    tag string (e.g. ``v1.0.1``) or ``None`` on any failure (timeout,
    HTTP error, JSON parse error, missing field)."""
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"claude-hooks/{CURRENT_VERSION}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
        data = json.loads(body)
        tag = data.get("tag_name")
        if isinstance(tag, str) and tag:
            return tag
        return None
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            ConnectionError, OSError, json.JSONDecodeError) as e:
        log.debug("update_check: fetch_latest_tag failed: %s", e)
        return None
    except Exception as e:  # paranoia — never raise to caller
        log.debug("update_check: fetch_latest_tag unexpected error: %s", e)
        return None


# --------------------------------------------------------------------------- #
# The check operation
# --------------------------------------------------------------------------- #
def run_due_check(config: dict, *, now: Optional[float] = None,
                  fetch=fetch_latest_tag) -> Optional[dict]:
    """Run the update check if due. Returns the new state on action,
    ``None`` when nothing was attempted (disabled or not yet due).

    The ``fetch`` callable is injectable for tests. Real callers can
    leave it at the default.
    """
    cfg = get_cfg(config)
    if not cfg["enabled"]:
        return None
    state = load_state(cfg)
    now = now if now is not None else time.time()
    if not should_run_check(state, cfg, now=now):
        return None

    try:
        tag = fetch(cfg["github_repo"], timeout=cfg["timeout_seconds"])
    except Exception as e:
        # Defensive: the production fetcher already swallows network
        # errors and returns None, but a custom or future fetcher must
        # not be able to bring down the daemon thread.
        log.debug("update_check: fetch raised %s; treating as failure", e)
        tag = None
    state["last_check_at"] = now

    if tag is None:
        # Failure path — schedule a retry, or back off to next 24h
        # window once max_retries is exceeded.
        retry_count = int(state.get("retry_count", 0)) + 1
        if retry_count > cfg["max_retries"]:
            state["retry_count"] = 0
            state["next_retry_at"] = None
            log.info(
                "update_check: %d retries exhausted; next attempt in %ds",
                cfg["max_retries"], cfg["interval_seconds"],
            )
        else:
            state["retry_count"] = retry_count
            state["next_retry_at"] = now + cfg["retry_pause_seconds"]
            log.info(
                "update_check: attempt failed (%d/%d); retrying in %ds",
                retry_count, cfg["max_retries"], cfg["retry_pause_seconds"],
            )
        save_state(cfg, state)
        return state

    # Success path — clear retry window, record the tag.
    state["retry_count"] = 0
    state["next_retry_at"] = None
    state["last_success_at"] = now
    state["latest_version"] = tag
    state["current_version_at_check"] = CURRENT_VERSION

    update_available = is_newer(tag, CURRENT_VERSION)
    state["update_available"] = update_available

    # Reset notification counter when:
    # - the latest version we're notifying about has changed, or
    # - the user has upgraded (no update available anymore)
    prior_notified = state.get("notified_for_version")
    if not update_available:
        state["notification_count"] = 0
        state["notified_for_version"] = None
    elif prior_notified != tag:
        state["notification_count"] = 0
        state["notified_for_version"] = tag

    save_state(cfg, state)
    log.info(
        "update_check: latest=%s current=%s update_available=%s",
        tag, CURRENT_VERSION, update_available,
    )
    return state


# --------------------------------------------------------------------------- #
# Stop-hook surface
# --------------------------------------------------------------------------- #
def pending_notification(config: dict) -> Optional[str]:
    """Read state and return the message string the Stop hook should
    surface, or ``None`` when nothing is pending. Does NOT mutate
    state — call ``consume_notification`` after a successful render."""
    cfg = get_cfg(config)
    if not cfg["enabled"]:
        return None
    state = load_state(cfg)
    if not state.get("update_available"):
        return None
    latest = state.get("latest_version")
    if not isinstance(latest, str) or not latest:
        return None
    if not is_newer(latest, CURRENT_VERSION):
        # The user may have upgraded since the last check. Suppress
        # silently; the next successful check will clean up the state.
        return None
    count = int(state.get("notification_count", 0))
    if count >= cfg["max_notifications"]:
        return None
    return (
        f"[claude-hooks] update available: {latest} (current {CURRENT_VERSION}) — "
        f"https://github.com/{cfg['github_repo']}/releases/tag/{latest}"
    )


def consume_notification(config: dict) -> None:
    """Increment the displayed-count after the Stop hook has emitted
    a notice this turn. Idempotent on missing state."""
    cfg = get_cfg(config)
    if not cfg["enabled"]:
        return
    state = load_state(cfg)
    if not state.get("update_available"):
        return
    state["notification_count"] = int(state.get("notification_count", 0)) + 1
    save_state(cfg, state)


# --------------------------------------------------------------------------- #
# Daemon background thread
# --------------------------------------------------------------------------- #
class UpdateCheckThread(threading.Thread):
    """Daemon-thread that wakes every ``poll_seconds`` and runs the
    update check when due. Catches all exceptions; never propagates."""

    def __init__(self, config_loader, *, stop_event: threading.Event,
                 poll_seconds: int = 60, name: str = "update-check"):
        super().__init__(name=name, daemon=True)
        self._config_loader = config_loader
        self._stop_event = stop_event
        self._poll_seconds = poll_seconds

    def run(self) -> None:  # pragma: no cover - thread loop
        log.debug("update_check thread started")
        while not self._stop_event.is_set():
            try:
                cfg = self._config_loader()
                run_due_check(cfg)
            except Exception as e:
                log.debug("update_check thread tick failed: %s", e)
            # Sleep in small slices so shutdown is responsive.
            self._stop_event.wait(self._poll_seconds)
        log.debug("update_check thread exiting")


def now_iso() -> str:
    """Helper used by serialisers wanting a human-readable timestamp."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
