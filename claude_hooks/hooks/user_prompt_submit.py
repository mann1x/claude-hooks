"""
UserPromptSubmit handler — recall from all enabled providers and inject
the results as ``additionalContext``.

Delegates to the shared :mod:`claude_hooks.recall` pipeline so the same
logic is reused by the compact-recall path in ``session_start.py``.
"""

from __future__ import annotations

import logging
from typing import Optional

from claude_hooks.providers import Provider

log = logging.getLogger("claude_hooks.hooks.user_prompt_submit")


#: Recall per synthetic prompt kind: "off" (none), "plain" (recall on
#: the raw text, no HyDE expansion) or "full" (as for a user prompt).
#: A notification is harness XML about a finished job; a scheduled prompt
#: is an instruction written in advance, worth a plain recall but not an
#: LLM expansion on every tick. Override per kind under
#: ``hooks.user_prompt_submit.synthetic_recall``.
DEFAULT_SYNTHETIC_RECALL = {"task-notification": "off", "scheduled": "plain"}
_RECALL_MODES = ("off", "plain", "full")


def synthetic_recall_mode(hook_cfg: dict, kind: str) -> str:
    cfg = hook_cfg.get("synthetic_recall") or {}
    mode = (cfg.get(kind) if isinstance(cfg, dict) else None) \
        or DEFAULT_SYNTHETIC_RECALL.get(kind, "full")
    return mode if mode in _RECALL_MODES else "off"


def _without_hyde(config: dict) -> dict:
    hooks = dict(config.get("hooks") or {})
    ups = dict(hooks.get("user_prompt_submit") or {})
    ups["hyde_enabled"] = False
    hooks["user_prompt_submit"] = ups
    out = dict(config)
    out["hooks"] = hooks
    return out


def recall_block(*, event: dict, config: dict, providers) -> str:
    """The recalled-memory block for this prompt, or "".

    Shared by :func:`handle` and the partial-disable path in
    :mod:`claude_hooks.hook_parts` — a copy of this decision there kept
    running HyDE on notifications after the handler stopped (2026-10-01).
    """
    hook_cfg = (config.get("hooks") or {}).get("user_prompt_submit") or {}
    if not hook_cfg.get("enabled", True):
        return ""
    prompt = (event.get("prompt") or "").strip()
    min_chars = int(hook_cfg.get("min_prompt_chars", 30))
    skip_recall = len(prompt) < min_chars

    # Most prompts this hook sees were not written by the user: background
    # task notifications and scheduled wake-ups / cron ticks start turns
    # too (see claude_hooks.prompt_origin). Recall on them is a HyDE call
    # over notification XML, or the same memories re-injected every tick.
    from claude_hooks.prompt_origin import classify
    origin = classify(prompt, event.get("transcript_path"))
    log.debug("prompt origin: %s (decided by %s)", origin.kind,
              origin.decided_by)
    recall_mode = "full"
    if origin.synthetic:
        recall_mode = str(synthetic_recall_mode(hook_cfg, origin.kind))
        log.info("prompt is a %s (decided by %s): recall=%s",
                 origin.kind, origin.decided_by, recall_mode)
    recall_config = config
    if recall_mode == "off":
        skip_recall = True
    elif recall_mode == "plain":
        recall_config = _without_hyde(config)

    additional_context: str = ""
    if not skip_recall:
        from claude_hooks.recall import run_recall
        additional_context = run_recall(
            prompt,
            config=recall_config,
            providers=providers,
            hook_name="user_prompt_submit",
            cwd=event.get("cwd", ""),
            max_total_chars=int(hook_cfg.get("max_total_chars", 4000)),
            progressive=bool(hook_cfg.get("progressive")),
        ) or ""
    else:
        log.debug("prompt too short (%d < %d) — skipping recall", len(prompt), min_chars)

    return additional_context


def handle(*, event: dict, config: dict, providers: list[Provider]) -> Optional[dict]:
    hook_cfg = (config.get("hooks") or {}).get("user_prompt_submit") or {}
    if not hook_cfg.get("enabled", True):
        return None

    additional_context = recall_block(event=event, config=config,
                                      providers=providers)

    # Prepend a pointer to any recent pre-compact wrap-up file, so
    # the post-compaction assistant reliably picks up the saved
    # state summary even when the inline additionalContext gets
    # trimmed across the compaction boundary.
    from claude_hooks.wrapup_recovery import format_recovery_block
    recovery = format_recovery_block(event.get("cwd", ""), config)
    if recovery:
        additional_context = (
            f"{recovery}\n\n{additional_context}" if additional_context else recovery
        )

    # Prepend the "## Now" block so the model has a fresh, local-TZ
    # timestamp every turn — anchors ETAs and scheduled-trigger
    # reasoning that would otherwise drift on UTC-only datetime.now().
    # Mail that arrived between turns. One indexed SELECT, soft-fail:
    # no announcement beats a delayed prompt, since the model can always
    # call mailbox-list itself.
    from claude_hooks.mailbox import hook as _mailbox
    mailbox_block = _mailbox.announce_block(
        event=event, config=config, providers=providers)
    if mailbox_block:
        additional_context = (f"{additional_context}\n\n{mailbox_block}"
                              if additional_context else mailbox_block)

    from claude_hooks.now_block import prepend_to_context
    final_context = prepend_to_context(additional_context, config)
    if not final_context:
        return None

    return {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": final_context,
        }
    }
