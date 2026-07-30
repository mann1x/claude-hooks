"""
Layer 2 of the store gate: cross-host admission control on the embedder.

:mod:`claude_hooks.store_lock` serialises stores *within* a host. That
closes the race we actually observed, but it is a per-host lock file, so
two machines sharing one embedder still get two concurrent stores. On
solidpc/pandorum both hosts embed against the same llamafile and write
to the same Postgres, so the shared, contended resource is the
**embedder** — not the memory backend.

Gating on the embedder rather than the store is deliberate: a Postgres
advisory lock would work only for the ``pgvector`` provider and leave
``sqlite_vec`` / ``qdrant`` / ``memory_kg`` unprotected. The embedder is
the one component every backend funnels through, so keying on it keeps
this backend-agnostic.

How busy-detection works (and why it looks odd)
-----------------------------------------------

llama.cpp splits its HTTP surface in two, which a measurement on solidpc
(2026-07-25, during a 29 s embed) makes obvious:

===========  ======  =====================
endpoint     idle    during a long embed
===========  ======  =====================
``/health``  0.00 s  0.00 s
``/props``   0.00 s  0.00 s
``/slots``   0.00 s  5.6 s - 11.7 s
``/metrics`` 0.00 s  8.6 s
===========  ======  =====================

``/health`` and ``/props`` are answered directly by the HTTP threads, so
they stay instant under full CPU load — which also proves the HTTP
thread pool is *not* the bottleneck and that raising ``--threads-http``
would change nothing. ``/slots`` and ``/metrics`` instead **queue a task
into the single-consumer inference loop**, so they cannot be answered
until that loop is free.

That stall is the signal. We probe ``/slots`` with a short timeout:

* answers quickly  -> the inference loop is free; parse ``is_processing``
* times out        -> the loop is busy, which is exactly what we wanted
  to know
* 404/501/error    -> not a llama.cpp server (Ollama exposes
  ``/api/ps``; OpenAI-compatible endpoints expose nothing), or
  ``--no-slots`` is set -> unknown, and the caller proceeds

What this is NOT
----------------

**Advisory backpressure, not mutual exclusion.** Two hosts can observe
"idle" in the same instant and both proceed (TOCTOU), and a busy server
occasionally answers fast between micro-batches — measured ~20% of
probes during one sustained embed. So this reduces cross-host pile-up on
the embedder; it cannot close a cross-host dedup race. llama.cpp offers
no primitive that could.

Only the *background* store path should use this. Interactive recall
must never wait on it: a probe costs up to ``probe_timeout`` seconds
precisely when the embedder is busy, which is exactly when recall is
most latency-sensitive.

Callers should invoke this **inside** the local
:func:`claude_hooks.store_lock.store_gate`, so at most one process per
host is probing. Each probe enqueues a task on the server, and an
unbounded probe storm would add load to the very thing being protected.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

log = logging.getLogger("claude_hooks.embedder_gate")

# Short: this is the busy oracle, not a data fetch. When the loop is
# free, /slots answers in ~0 s; anything slower means it is queued behind
# inference. Long enough to absorb LAN jitter, short enough that reading
# "busy" stays cheap.
DEFAULT_PROBE_TIMEOUT_S = 1.0

# Total time a background store will wait for the embedder to quiet down
# before giving up and proceeding anyway. Bounded because the store must
# still happen -- see the failure policy in store_lock.
DEFAULT_MAX_WAIT_S = 60.0

# Gap between probes. Each probe enqueues a task on the server, so this
# is deliberately coarse.
DEFAULT_POLL_INTERVAL_S = 2.0

# Results
IDLE = "idle"
BUSY = "busy"
UNKNOWN = "unknown"


def slots_url(embed_url: str) -> Optional[str]:
    """Derive the ``/slots`` URL from a configured embedding endpoint.

    ``http://host:38092/embedding`` -> ``http://host:38092/slots``.
    Returns ``None`` when the input is not a usable http(s) URL.
    """
    if not embed_url:
        return None
    try:
        parts = urlsplit(embed_url)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return urlunsplit((parts.scheme, parts.netloc, "/slots", "", ""))


def probe(embed_url: str,
          timeout: float = DEFAULT_PROBE_TIMEOUT_S) -> str:
    """One busy-check. Returns :data:`IDLE`, :data:`BUSY` or
    :data:`UNKNOWN`.

    A timeout is reported as :data:`BUSY` by design — see the module
    docstring: ``/slots`` is served from the inference loop, so a slow
    answer *is* the occupancy signal.
    """
    url = slots_url(embed_url)
    if not url:
        return UNKNOWN
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        # 404 (no such route) / 501 (disabled) -> not a gateable server.
        log.debug("embedder gate: /slots HTTP %s — not gateable", e.code)
        return UNKNOWN
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        # urllib surfaces a read timeout as URLError(socket.timeout) or
        # TimeoutError depending on version; both mean "did not answer
        # in time", i.e. the inference loop is busy. A genuine
        # connection refusal also lands here — treated as busy is wrong,
        # but harmless: the caller only ever *waits* longer, and the
        # store still proceeds when the budget runs out.
        log.debug("embedder gate: /slots did not answer in %.2fs (%s)",
                  timeout, e)
        return BUSY
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return UNKNOWN
    if not isinstance(data, list):
        return UNKNOWN
    busy = sum(1 for s in data
               if isinstance(s, dict) and s.get("is_processing"))
    return BUSY if busy else IDLE


def wait_for_capacity(embed_url: str,
                      max_wait: float = DEFAULT_MAX_WAIT_S,
                      probe_timeout: float = DEFAULT_PROBE_TIMEOUT_S,
                      poll_interval: float = DEFAULT_POLL_INTERVAL_S) -> str:
    """Block until the embedder looks idle, the budget expires, or the
    endpoint turns out not to be gateable.

    Returns the final observation: :data:`IDLE` (go now), :data:`BUSY`
    (gave up waiting — caller proceeds anyway), or :data:`UNKNOWN` (not a
    llama.cpp ``/slots`` server; no gating possible).

    Never raises. The caller must proceed regardless of the result: a
    store that is delayed forever is a dropped memory, which is the
    failure this whole subsystem exists to prevent.
    """
    if max_wait <= 0:
        return UNKNOWN
    deadline = time.monotonic() + max_wait
    started = time.monotonic()
    result = UNKNOWN
    while True:
        result = probe(embed_url, timeout=probe_timeout)
        if result in (IDLE, UNKNOWN):
            break
        if time.monotonic() >= deadline:
            log.debug(
                "embedder gate: still busy after %.1fs — proceeding anyway",
                time.monotonic() - started,
            )
            break
        time.sleep(poll_interval)
    waited = time.monotonic() - started
    if result == IDLE and waited >= poll_interval:
        log.debug("embedder gate: embedder freed after %.1fs", waited)
    return result


def config_from(cfg: dict) -> dict:
    """Read the ``hooks.stop.embedder_gate`` block.

    Off by default: it only helps when several *hosts* share one
    embedder, and it costs a probe per store. Hosts with a private
    embedder gain nothing from it.
    """
    block = ((cfg.get("hooks") or {}).get("stop") or {}).get(
        "embedder_gate") or {}
    return {
        "enabled": bool(block.get("enabled", False)),
        "max_wait_s": float(block.get("max_wait_s", DEFAULT_MAX_WAIT_S)),
        "probe_timeout_s": float(
            block.get("probe_timeout_s", DEFAULT_PROBE_TIMEOUT_S)),
        "poll_interval_s": float(
            block.get("poll_interval_s", DEFAULT_POLL_INTERVAL_S)),
    }


def embed_url_from(cfg: dict, provider_names: list) -> Optional[str]:
    """Find the embedding endpoint the given providers will use.

    Providers may each carry their own ``embedder_options``; they
    normally point at the same engine, so the first http(s) URL wins.
    ``CLAUDE_HOOKS_EMBED_URL`` overrides (used by tests).
    """
    override = os.environ.get("CLAUDE_HOOKS_EMBED_URL")
    if override:
        return override
    providers = cfg.get("providers") or {}
    for name in provider_names or list(providers):
        opts = (providers.get(name) or {}).get("embedder_options") or {}
        url = opts.get("url")
        if url and slots_url(url):
            return url
    return None
