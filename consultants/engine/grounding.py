"""The council's grounding, sized by its roles' windows (ch-24).

Every role starts from the same head: system turns plus the project's
anchor files (``CLAUDE.md``, ``README.md``, …, from
``claude_hooks.caliber_proxy.prompt.build_grounding_messages``). The
anchors had a fixed 50,000-byte budget, the caliber proxy's, whatever the
window: harmless at 262k–1M tokens, but on a 32,768 window the head came
to 18,416 tokens (csl-2026-10-25-0931-a7d3). Compaction never folds the
head, so every fold landed at ~23k tokens and the next ``read_file`` put
the lane past its window again: 145 compaction calls for 67 research
calls.

The anchors now take at most :data:`WINDOW_SHARE` of the smallest
window any enabled role and model resolves (the same
:func:`budget.resolve_window` and budget layers the council budgets
against), at :data:`CHARS_PER_TOKEN`. The cap only ever lowers the
default, so a window of ~167k tokens or more sends the same grounding as
before and the prefix cache is untouched.
"""
from __future__ import annotations

import logging
from typing import Any, Mapping, Optional, Sequence

log = logging.getLogger(__name__)

#: Share of the smallest window the anchor files may take.
WINDOW_SHARE = 0.10
#: Characters per token for that share: below the ~3.6 rendered prompts
#: measure, so the cap errs towards a smaller head.
CHARS_PER_TOKEN = 3.0


def smallest_window(cfg: Any, clients: Mapping[str, Any],
                    roles: Sequence[str]) -> Optional[int]:
    """The smallest window any of ``roles`` resolves over its models
    (main + extra), or ``None`` when none resolves. The 32,768 fallback
    for a backend that declared nothing does not count. Never raises."""
    from consultants.engine import budget, control
    try:
        layers = control.budget_layers(cfg)
    except Exception:  # noqa: BLE001 — sizing never fails a run
        layers = {}
    windows: list[int] = []
    for role in roles:
        rc = (getattr(cfg, "roles", None) or {}).get(role)
        client = clients.get(role)
        if rc is None or client is None:
            continue
        for model in [rc.model, *(getattr(rc, "extra_models", None) or [])]:
            if not model:
                continue
            knobs = (budget.knobs_from(layers.get("*"), layers.get(role),
                                       layers.get(f"{role}@{model}"))
                     if layers else budget.DEFAULT_KNOBS)
            try:
                win = budget.resolve_window(client, model, knobs)
            except Exception:  # noqa: BLE001
                log.debug("grounding: no window for %s/%s", role, model,
                          exc_info=True)
                continue
            # "default" is nothing declared, never a window: an unanswered
            # probe must not shrink the grounding to 32,768's share.
            if win.tokens and win.source != "default":
                windows.append(int(win.tokens))
    return min(windows) if windows else None


def anchor_bytes(window: Optional[int]) -> Optional[int]:
    """The anchor budget for ``window``, or ``None`` to keep the default
    (no window known, or the share is not below the default)."""
    if not window:
        return None
    from claude_hooks.caliber_proxy.prompt import _default_anchor_bytes
    cap = int(window * WINDOW_SHARE * CHARS_PER_TOKEN)
    return cap if cap < _default_anchor_bytes() else None


def council_grounding(cwd: str, cfg: Any, clients: Mapping[str, Any],
                      roles: Sequence[str]) -> list[dict]:
    """The grounding messages every role of this run is given."""
    from claude_hooks.caliber_proxy.prompt import build_grounding_messages
    window = smallest_window(cfg, clients, roles)
    cap = anchor_bytes(window)
    if cap is not None:
        log.info("grounding: anchors capped at %d chars (smallest window "
                 "%d tokens)", cap, window)
    # Named only under the window cap: at the default the block stays as
    # it was, cut included (this repo's CLAUDE.md is past 50,000 chars).
    return build_grounding_messages(cwd, max_anchor_bytes=cap,
                                    tools_available=True,
                                    note_cut=cap is not None)


__all__ = ["WINDOW_SHARE", "CHARS_PER_TOKEN", "smallest_window",
           "anchor_bytes", "council_grounding"]
