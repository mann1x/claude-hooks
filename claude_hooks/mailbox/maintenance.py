"""Daemon-side lifecycle for the mailbox.

Everything here is periodic housekeeping the mailbox cannot do for
itself: expired mail archived and removed, the archive trimmed to its
cap, and registry entries for sessions nobody has seen in a month
forgotten. :func:`claude_hooks.mailbox.archive.sweep` already performs
that cycle in the one order that is safe — archive, *then* delete, then
trim — so this module is the scheduler, not the policy.

It lives on ``claude-hooks-daemon`` for the same reason the embedding
and chat-model reapers do: it is the only process that is alive between
turns. A hook is the wrong home for work measured in days, because a
hook that has to run for maintenance to happen makes maintenance a
function of how often someone types.

Fail-open throughout. A host with no SQL-backed provider has no mailbox
and therefore nothing to sweep, which is not an error; and a sweep that
raises must never take the daemon with it, because the daemon is also
the hook executor.
"""
from __future__ import annotations

import logging
import random
import threading
import time
from typing import Callable, Optional

log = logging.getLogger("claude_hooks.mailbox.maintenance")

#: Once an hour. The work is bounded (``limit`` rows per sweep) and the
#: deadlines it enforces are measured in days, so a faster cadence buys
#: nothing and a slower one lets a burst of expiries pile up.
DEFAULT_INTERVAL_SECONDS = 3600.0

#: Wait before the first sweep. The daemon starts alongside a session,
#: and the first minutes are the ones competing with recall, HyDE and a
#: cold embedder for the same connection.
DEFAULT_INITIAL_DELAY_SECONDS = 300.0

#: Rows per sweep. A cohort that all expires on the same tick is
#: drained over several hours rather than in one transaction that holds
#: the connection the hooks are waiting on.
DEFAULT_LIMIT = 1000


#: Never sleep less than this between sweeps, whatever config says. A
#: zero or negative interval would spin on the same connection the
#: hooks are waiting for, which is a worse outcome than stale mail.
MIN_INTERVAL_SECONDS = 60.0


#: Fraction of the interval to scatter each sleep by. Two hosts share
#: one Postgres, so both daemons sweep the same table; without jitter
#: their hourly ticks can phase-lock and both archive the same expired
#: rows before either deletes them — the archive is append-only, so the
#: duplicate is permanent. Jitter does not make that impossible, only
#: rare; see the note in docs/mailbox.md.
_JITTER_FRACTION = 0.15


def sleep_seconds(interval: float, *, jitter: bool = False) -> float:
    """Clamp a configured cadence to something a daemon can survive."""
    try:
        value = max(MIN_INTERVAL_SECONDS, float(interval))
    except (TypeError, ValueError):
        value = DEFAULT_INTERVAL_SECONDS
    if jitter:
        spread = value * _JITTER_FRACTION
        value = max(MIN_INTERVAL_SECONDS,
                    value + random.uniform(-spread, spread))
    return value


def _enabled(cfg: Optional[dict]) -> bool:
    """Follows the mailbox's own switch — maintaining a mailbox nobody
    is allowed to use would be work for its own sake."""
    if not isinstance(cfg, dict):
        return False
    section = (cfg.get("hooks") or {}).get("mailbox") or {}
    if not section.get("enabled", False):
        return False
    return bool(section.get("maintenance", True))


def _settings(cfg: Optional[dict]) -> dict:
    section = {}
    if isinstance(cfg, dict):
        section = (cfg.get("hooks") or {}).get("mailbox") or {}
    out = {
        "interval": DEFAULT_INTERVAL_SECONDS,
        "limit": DEFAULT_LIMIT,
        "registry_days": None,
        "cap_bytes": None,
    }
    for key, name in (("interval", "maintenance_interval_seconds"),
                      ("limit", "maintenance_limit")):
        try:
            value = section.get(name)
            if value is not None:
                out[key] = type(out[key])(value)
        except (TypeError, ValueError):
            log.debug("mailbox: ignoring bad %s=%r", name, section.get(name))
    for key, name in (("registry_days", "registry_days"),
                      ("cap_bytes", "archive_cap_bytes")):
        try:
            value = section.get(name)
            if value is not None:
                out[key] = int(value)
        except (TypeError, ValueError):
            log.debug("mailbox: ignoring bad %s=%r", name, section.get(name))
    return out


def run_sweep(cfg: Optional[dict] = None,
              *, providers=None) -> Optional[dict]:
    """One maintenance cycle. Returns the sweep report, or None.

    None means "no sweep happened" — disabled, no store, or a failure
    already logged. It is deliberately not an empty report: a caller
    logging "0 expired" for a mailbox that was never reachable would be
    the same false negative this codebase keeps finding elsewhere.
    """
    if not _enabled(cfg):
        return None

    from claude_hooks.mailbox import archive
    from claude_hooks.mailbox.integration import store_for_provider

    if providers is None:
        try:
            from claude_hooks.dispatcher import build_providers
            providers = build_providers(cfg or {})
        except Exception as e:
            log.debug("mailbox: could not build providers: %s", e)
            return None

    store = None
    for provider in providers or []:
        try:
            store = store_for_provider(provider)
        except Exception as e:  # pragma: no cover - defensive
            log.debug("mailbox: provider %r unusable: %s",
                      getattr(provider, "name", provider), e)
            continue
        if store is not None:
            break
    if store is None:
        # No pgvector / sqlite_vec on this host. Not an error.
        return None

    opts = _settings(cfg)
    kwargs = {"limit": opts["limit"]}
    if opts["registry_days"] is not None:
        kwargs["registry_days"] = opts["registry_days"]
    if opts["cap_bytes"] is not None:
        kwargs["cap_bytes"] = opts["cap_bytes"]

    started = time.monotonic()
    try:
        report = archive.sweep(store, **kwargs)
    except Exception as e:
        log.warning("mailbox: sweep failed: %s", e, exc_info=True)
        return None
    elapsed = time.monotonic() - started

    # Logged at INFO only when it did something. An hourly "nothing to
    # do" line is how a log stops being read.
    if any((report.get("expired"), report.get("deleted"),
            report.get("dropped"), report.get("sessions_forgotten"))):
        log.info(
            "mailbox sweep: %d expired, %d deleted, %d archive file(s) "
            "dropped, %d session(s) forgotten (%.1fs)",
            report.get("expired", 0), report.get("deleted", 0),
            len(report.get("dropped") or []),
            report.get("sessions_forgotten", 0), elapsed,
        )
    else:
        log.debug("mailbox sweep: nothing to do (%.1fs)", elapsed)
    return report


class MailboxMaintenanceThread(threading.Thread):
    """Daemon-thread that sweeps the mailbox on a slow cadence.

    Re-reads config every tick, like :class:`UpdateCheckThread`, so the
    switch can be flipped without restarting the daemon — which matters
    because restarting this daemon also kills the managed llamafile.
    """

    def __init__(self, config_loader: Callable[[], dict], *,
                 stop_event: threading.Event,
                 interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
                 initial_delay_seconds: float = (
                     DEFAULT_INITIAL_DELAY_SECONDS),
                 name: str = "mailbox-maintenance"):
        super().__init__(name=name, daemon=True)
        self._config_loader = config_loader
        self._stop_event = stop_event
        self._interval = float(interval_seconds)
        self._initial_delay = float(initial_delay_seconds)

    def run(self) -> None:  # pragma: no cover - thread loop
        log.debug("mailbox maintenance thread started")
        # Scatter the first sweep too: two hosts brought up together —
        # a deploy, a power cut — would otherwise start in lockstep and
        # stay there.
        delay = self._initial_delay
        if delay > 0:
            delay *= random.uniform(0.5, 1.5)
        if self._stop_event.wait(delay):
            return
        while not self._stop_event.is_set():
            interval = self._interval
            try:
                cfg = self._config_loader()
                run_sweep(cfg)
                # Honour a config change to the cadence on the next
                # sleep rather than at the next restart.
                interval = float(_settings(cfg)["interval"])
            except Exception as e:
                log.debug("mailbox maintenance tick failed: %s", e)
            self._stop_event.wait(sleep_seconds(interval, jitter=True))
        log.debug("mailbox maintenance thread exiting")
