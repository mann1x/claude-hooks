"""
Detached background-store helper (Tier 1.3 latency reduction).

The Stop hook's dedup-and-store fan-out costs ~200-500 ms per noteworthy
turn — one `provider.recall(summary[:500], k=3)` for dedup plus one
`provider.store(summary, metadata)` per provider, both network calls
to the MCP server. The user has no need for that work to complete
before Claude Code returns from the hook.

This module provides:

- :func:`spawn` — fork a detached subprocess that runs the same
  dedup-and-store logic the Stop hook would have run inline. Returns
  immediately. Mirrors the ``claudemem_reindex._spawn_reindex`` pattern
  (`subprocess.Popen` with `start_new_session=True` and stdio piped to
  DEVNULL), so the parent never blocks waiting for the child.

- :func:`main` — entry point invoked by the spawned subprocess via
  ``python -m claude_hooks.store_async``. Reads a JSON payload from
  stdin (provider config + summary + metadata + dedup thresholds),
  rebuilds providers via :func:`dispatcher.build_providers`, and runs
  the same fan-out the inline path does. Errors are logged to the same
  ``claude-hooks.log`` file via the standard logging setup.

The detach is opt-in via ``hooks.stop.detach_store: true``. Default is
False because:

1. Tests pass FakeProvider objects directly to ``stop.handle()`` —
   they cannot survive an interpreter boundary, so the detached path
   would skip the test's provider entirely. Tests that need to assert
   on store side-effects must run inline.

2. Until the daemon (Tier 3.8) ships, every detached store pays the
   ~50 ms Python-interpreter startup cost. Net savings vs inline are
   real (~150-300 ms per turn) but the daemon path will be cheaper
   still and replace this one as the default.

3. Any failure in the detached child is logged but never surfaced in
   the systemMessage — opt-in semantics make that explicit.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys

from claude_hooks._popen import detach_kwargs, windowless_python_executable

log = logging.getLogger("claude_hooks.store_async")


def spawn(payload: dict) -> bool:
    """Fork a detached ``python -m claude_hooks.store_async`` subprocess.

    ``payload`` is a JSON-serialisable dict with keys:

    - ``config``: full claude-hooks config dict (used to rebuild providers
      and reconfigure logging in the child).
    - ``summary``: str — the turn summary to store.
    - ``metadata``: dict — metadata passed to ``provider.store(...)``.
    - ``provider_names``: list[str] — explicit allow-list of providers
      whose ``store_mode == "auto"`` (computed by the parent so the
      child doesn't have to repeat that filter).

    Returns True on successful spawn (the child is running detached);
    False on any error so callers can fall back to inline execution.
    Never raises.
    """
    try:
        body = json.dumps(payload).encode("utf-8")
    except (TypeError, ValueError) as e:
        log.debug("store_async.spawn: payload not serialisable: %s", e)
        return False

    try:
        # Windows: hook context arrives via .cmd → python.exe with a
        # console attached; without CREATE_NO_WINDOW the detached
        # store child would inherit / flash that console. v1.10.1
        # additionally swaps python.exe → pythonw.exe (windowless
        # subsystem) via ``windowless_python_executable`` so the
        # interpreter itself doesn't auto-allocate a console at
        # startup — same defence the LSP daemon spawn now uses.
        # POSIX: start_new_session=True suffices.
        proc = subprocess.Popen(
            [windowless_python_executable(),
             "-m", "claude_hooks.store_async"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            **detach_kwargs(),
        )
    except OSError as e:
        log.debug("store_async.spawn: Popen failed: %s", e)
        return False

    try:
        assert proc.stdin is not None
        proc.stdin.write(body)
        proc.stdin.close()
    except (OSError, BrokenPipeError) as e:
        log.debug("store_async.spawn: stdin write failed: %s", e)
        try:
            proc.kill()
        except OSError:
            pass
        return False

    _deprioritise(proc)
    log.debug("store_async: spawned pid=%s", proc.pid)
    return True


# Background stores are explicitly *not* latency-critical: nothing waits
# on them and the store gate already caps them at one in flight. Running
# them below default priority means that when they do overlap with an
# interactive recall (which IS on the user's critical path, under a hook
# timeout), the kernel resolves the contention in recall's favour. This
# is the client-side complement to raising the embedder's own priority
# in embedding_manager.
_STORE_NICE = 10


def _deprioritise(proc: subprocess.Popen) -> None:
    """Drop the detached store child below default scheduling priority.

    Best-effort and never fatal: *raising* niceness needs no privilege
    on POSIX, but the call can still fail if the child already exited.
    Windows is handled at creation time by ``detach_kwargs``' priority
    class when available.
    """
    if sys.platform.startswith("win"):
        return
    try:
        os.setpriority(os.PRIO_PROCESS, proc.pid, _STORE_NICE)
    except (OSError, PermissionError, AttributeError) as e:
        log.debug("store_async: could not lower priority: %s", e)


def main() -> int:
    """Subprocess entry point. Reads payload from stdin, runs dedup+store."""
    raw = sys.stdin.buffer.read()
    if not raw:
        return 0
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        # No logger yet — print to stderr (DEVNULL'd by parent, but kept
        # here for direct invocation / debugging).
        print(f"store_async: invalid payload: {e}", file=sys.stderr)
        return 1

    cfg = payload.get("config") or {}
    summary = payload.get("summary") or ""
    metadata = payload.get("metadata") or {}
    provider_names = payload.get("provider_names") or []

    # Reuse dispatcher's logging setup so output ends up in the same
    # log file as inline hook runs.
    from claude_hooks.dispatcher import _setup_logging, build_providers
    _setup_logging(cfg)
    log.debug(
        "store_async.main: %d provider(s) requested: %s",
        len(provider_names), provider_names,
    )

    if not summary or not provider_names:
        return 0

    providers = [
        p for p in build_providers(cfg) if p.name in provider_names
    ]
    if not providers:
        log.debug("store_async.main: no providers rebuilt — payload had %s but config produced none", provider_names)
        return 0

    _run_dedup_and_store(cfg, summary, metadata, providers)
    return 0


def _run_dedup_and_store(
    cfg: dict, summary: str, metadata: dict, providers: list,
) -> None:
    """Mirror of ``stop._dedup_and_store`` for use in the detached child.

    Kept as a tiny local copy so the child doesn't need to import the
    Stop hook module (which pulls in transcript parsing, observation
    classification, etc. — none of which the child needs).

    The whole dedup+store is wrapped in
    :func:`claude_hooks.store_lock.store_gate` so only one detached
    store runs at a time across every session on the host. The gate has
    to cover *both* halves, not just the write: dedup-recall and store
    must be atomic with respect to other stores, or two children each
    finish their dedup before either writes, neither sees the other, and
    both store the same summary (the duplicate pairs observed on
    solidpc 2026-07-25). Serialising also caps background embed load at
    one in-flight request so interactive recall keeps the embedder's
    remaining capacity. See ``store_lock`` for the failure policy.
    """
    from claude_hooks._parallel import parallel_map
    from claude_hooks.store_lock import store_gate

    def _do(provider):
        provider_cfg = ((cfg.get("providers") or {}).get(provider.name)) or {}
        dedup_threshold = float(provider_cfg.get("dedup_threshold", 0.0))
        if dedup_threshold > 0.0 and len(summary) >= 100:
            try:
                from claude_hooks.dedup import should_store as dedup_ok
                if not dedup_ok(summary, provider, threshold=dedup_threshold):
                    log.info(
                        "store_async: skipping store to %s: near-duplicate",
                        provider.name,
                    )
                    return None
            except Exception as e:
                log.debug("store_async dedup failed for %s: %s", provider.name, e)
        try:
            provider.store(summary, metadata=metadata)
            log.info("store_async: stored to %s", provider.name)
        except Exception as e:
            log.warning("store_async: %s store failed: %s", provider.name, e)
        return None

    with store_gate() as held:
        if not held:
            log.warning(
                "store_async: proceeding without the store gate — "
                "a duplicate is possible",
            )
        parallel_map(_do, providers)


if __name__ == "__main__":
    sys.exit(main())
