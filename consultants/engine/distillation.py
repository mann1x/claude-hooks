"""M14 — Caliber-style distillation of expiring research entries.

When the consultants-daemon's :class:`~consultants.engine.store_reaper.StoreReaperThread`
finds expiring rows in a session's ``("research")`` namespace, this
module's :class:`Distiller` reads them, calls an LLM with a focused
rubric (retain citations + decisions + gotchas; drop process
narration), and writes the resulting summary into the durable
``("project", project_id)`` namespace **before** the reaper deletes
the originals.

This converts the consultants store from "every session's findings
live for N days then disappear" into "every session's findings live
for N days then collapse into one durable project-level entry" —
episodic short-term → semantic long-term, mirroring how humans
consolidate working memory into autobiographical memory.

Critical invariant — the reaper only deletes originals on a
successful distillation write. If every model in the
``[model] + fallback_models`` chain fails, the originals stay in
place and the next sweep tick retries. This avoids the "TTL deleted
my findings before distillation could capture them" failure mode.

User-locked defaults (2026-05-17):
    - ``model = "gemma4:31b-cloud"`` (M11c-2 tool_executor winner)
    - ``fallback_models = ["glm-5.2:cloud"]`` (~64k ctx fallback)
    - ``min_entries_per_distillation = 3`` — small cost gate
    - ``max_session_entries = 50`` — prompt size cap (~30k tokens)
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

log = logging.getLogger("consultants.engine.distillation")


# ============================================================== #
# Errors
# ============================================================== #


class DistillationFailed(Exception):
    """Raised when every model in the configured fallback chain fails.

    The reaper catches this and keeps the originals in place — the
    next sweep tick will retry. **Do not** lower this to a log
    warning; the silent-failure-then-delete shape is exactly the
    invariant M14 is designed to avoid.
    """


# ============================================================== #
# project_id derivation
# ============================================================== #


def project_id_from_cwd(cwd: Any) -> str:
    """Stable 12-char project identifier from an absolute cwd path.

    The same cwd always yields the same id; different cwds yield
    different ids (SHA-256 collision-resistant). Relative paths are
    resolved first so ``./.`` and the absolute equivalent collapse
    into the same project.

    The truncation at 12 hex chars matches the M8
    ``Namespaces.project`` shape ("project", short_id) and keeps the
    namespace key compact in vector-store metadata.

    Args:
        cwd: filesystem path (str or Path). Empty / None falls back
            to the literal string ``"<unknown-cwd>"`` so the daemon
            never crashes on a row with missing metadata.

    Returns:
        12-char hex string.
    """
    s = str(cwd or "<unknown-cwd>")
    if s and s != "<unknown-cwd>":
        try:
            s = str(Path(s).resolve())
        except (OSError, RuntimeError):
            pass  # caller passed something path-like that can't resolve
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:12]


# ============================================================== #
# Prompt assembly
# ============================================================== #


# The distillation rubric — borrowed from the Caliber grounding
# proxy's findings-extraction style, adapted for the consultants
# council context (LLM-to-LLM, no human reader, no markdown
# ceremony, dense citations).
DISTILLATION_SYSTEM_PROMPT = """\
You are a distillation specialist for an LLM research council. \
The council retained N research findings from a single session \
that are about to expire. Your job is to write ONE durable \
summary that preserves what matters and drops what doesn't.

RETAIN:
- File:line citations and concrete code references
- Decisions reached and the reasoning that grounded them
- Surprising findings or non-obvious gotchas
- Open questions explicitly flagged for follow-up

DROP:
- Process narration ("I will now examine...", "Next, I'll grep \
for...")
- Retry traces and dead-end paths
- Prose padding without code references
- Restated user questions
- Findings that contradict more recent findings in this set

Output a single concise markdown block, ≤ 800 words, suitable \
for recall by future sessions. Use bullet lists for findings, \
plain prose for narrative context. Cite every claim with \
``path:line`` style. Do NOT preface with "Here is the summary" \
or similar — the entire response will be stored verbatim.\
"""


def _extract_finding_text(row: Any) -> str:
    """Best-effort pull of the indexed text off an ExpiringRow.

    The row's content was produced by ``ProviderBackedStore._do_put``
    which JSON-serializes the user's value dict via
    ``_extract_indexable_text``. If the value dict had a
    ``"text"`` field, the JSON form contains it; otherwise the
    whole dict serializes.

    Either way we return ``row.content`` — the daemon prompt
    handles both shapes by treating the field as opaque text.
    """
    return getattr(row, "content", "") or ""


def _row_metadata(row: Any) -> dict:
    """Pull the metadata dict off a row, tolerating absent / non-dict
    values. Used for plan_item / lane_idx extraction in the prompt."""
    md = getattr(row, "metadata", None)
    return md if isinstance(md, dict) else {}


def build_distillation_prompt(
    sid: str,
    rows: Sequence[Any],
    *,
    max_session_entries: int = 50,
    cwd_hint: Optional[str] = None,
    question_hint: Optional[str] = None,
) -> list[dict]:
    """Build the OpenAI-shape messages list for one session's group.

    Args:
        sid: the session id the rows came from. Stamped in the user
            message so the LLM's summary is self-locating.
        rows: ExpiringRow sequence; will be truncated to
            ``max_session_entries`` (oldest first kept) to cap
            prompt token cost.
        max_session_entries: hard cap. The reaper sweeps in
            ``expires_at`` ascending order so the truncation drops
            the most-recently-aged-out rows last.
        cwd_hint: project cwd recovered from row metadata. The
            reaper hands this in once per group so we don't re-scan
            every row.
        question_hint: the user's original question, if any row's
            metadata carries one (M8 stores it as ``"question"``).
    """
    if not rows:
        return []
    rows = list(rows)[:max_session_entries]
    lines: list[str] = []
    lines.append(f"Session: {sid}")
    if cwd_hint:
        lines.append(f"Cwd: {cwd_hint}")
    if question_hint:
        lines.append(f"Question: {question_hint}")
    # Date range for forensic context — helps the LLM weight more
    # recent findings if they contradict earlier ones.
    created_ats = [
        _row_metadata(r).get("created_at") for r in rows
    ]
    created_ats = [c for c in created_ats if isinstance(c, str)]
    if created_ats:
        lines.append(
            f"Date range: {min(created_ats)} → {max(created_ats)}"
        )
    lines.append(f"Findings (N={len(rows)}):")
    for i, r in enumerate(rows, start=1):
        md = _row_metadata(r)
        lane = md.get("lane_idx")
        plan = md.get("plan_item") or ""
        # Truncate plan/lane to keep header short — the finding body
        # is what matters.
        plan_short = plan[:60] + "…" if len(str(plan)) > 60 else plan
        head = f"{i}."
        if lane is not None:
            head += f" (lane {lane})"
        if plan_short:
            head += f" plan={plan_short!r}"
        lines.append(head)
        text = _extract_finding_text(r) or "(empty)"
        lines.append(text)
        lines.append("")  # blank line between findings
    user = "\n".join(lines)
    return [
        {"role": "system", "content": DISTILLATION_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


# ============================================================== #
# Distiller — fallback chain over the chat client factory
# ============================================================== #


class Distiller:
    """Calls the distillation LLM with a fallback chain.

    Holds no per-session state; the reaper instantiates one
    Distiller per app lifetime and reuses it across sweeps. The
    chat-client factory is parameterized so tests can inject a
    fake without touching Ollama / cloud routing.

    Args:
        distillation_cfg: ``StoreDistillationConfig`` from
            ``cfg.store.distillation``. Carries the primary model,
            fallback list, and prompt-cost caps.
        ollama_base_url: passed through to
            :func:`make_agent_chat_client`. Bare-model identifiers
            route to Ollama via this URL; ``llamafile://<label>``
            identifiers route to the daemon-supervised llamafile.
        chat_client_factory: override the default
            ``make_agent_chat_client``. Useful for tests; the
            production path leaves this ``None`` and uses the
            standard factory.
    """

    def __init__(
        self,
        distillation_cfg: Any,
        ollama_base_url: str,
        *,
        chat_client_factory: Optional[Any] = None,
    ):
        self.cfg = distillation_cfg
        self.ollama_base_url = ollama_base_url
        if chat_client_factory is None:
            from claude_hooks.get_advice.chat_client import (
                make_agent_chat_client,
            )
            chat_client_factory = make_agent_chat_client
        self._make_client = chat_client_factory

    def _models_to_try(self) -> list[str]:
        """Primary model first, then fallbacks in order. Empty / blank
        entries are dropped defensively."""
        primary = (self.cfg.model or "").strip()
        fallbacks = [
            m.strip() for m in (self.cfg.fallback_models or [])
            if isinstance(m, str) and m.strip()
        ]
        out: list[str] = []
        if primary:
            out.append(primary)
        for f in fallbacks:
            if f not in out:
                out.append(f)
        return out

    def distill_session(
        self,
        sid: str,
        rows: Sequence[Any],
        *,
        cwd_hint: Optional[str] = None,
        question_hint: Optional[str] = None,
    ) -> str:
        """Run distillation for one (sid, group) and return the
        summary text.

        Raises :class:`DistillationFailed` when **all** models in
        the fallback chain raise. The reaper translates that into
        "keep the originals; next tick retries".
        """
        if not self.cfg or not getattr(self.cfg, "enabled", False):
            raise DistillationFailed(
                "distillation disabled by config (cfg.store.distillation.enabled = False)",
            )
        if not rows:
            raise DistillationFailed("empty rows; nothing to distill")
        models = self._models_to_try()
        if not models:
            raise DistillationFailed(
                "no distillation model configured "
                "(cfg.store.distillation.model is empty)",
            )
        messages = build_distillation_prompt(
            sid, rows,
            max_session_entries=self.cfg.max_session_entries,
            cwd_hint=cwd_hint,
            question_hint=question_hint,
        )
        if not messages:
            raise DistillationFailed("prompt build returned empty")

        last_err: Optional[Exception] = None
        for model in models:
            try:
                client = self._make_client(
                    model, self.ollama_base_url,
                )
                payload = {
                    "model": model,
                    "messages": messages,
                    # Short max_tokens — the rubric caps output at
                    # ~800 words. Letting the model run unbounded
                    # would waste tokens and dilute the summary.
                    "max_tokens": 1500,
                    "temperature": 0.3,
                    "stream": False,
                }
                resp = client.chat(payload)
                content = _extract_response_text(resp)
                if content and content.strip():
                    return content.strip()
                last_err = DistillationFailed(
                    f"model {model!r} returned empty content",
                )
            except Exception as e:
                log.warning(
                    "distillation: model %s failed (%s); "
                    "falling back",
                    model, e,
                )
                last_err = e
        raise DistillationFailed(
            f"all {len(models)} distillation models failed: {last_err}"
        )


def _extract_response_text(resp: Any) -> str:
    """Pull the assistant content out of an OpenAI-shape response.

    Tolerates dict / object shapes and missing fields — every shape
    that doesn't produce text is treated as "empty content" and
    surfaces as a fallback trigger.
    """
    if not resp:
        return ""
    # OpenAI dict shape: {"choices": [{"message": {"content": "..."}}]}
    if isinstance(resp, dict):
        choices = resp.get("choices") or []
        if choices and isinstance(choices[0], dict):
            msg = choices[0].get("message") or {}
            content = msg.get("content")
            if isinstance(content, str):
                return content
    return ""


# ============================================================== #
# Project-namespace write helper
# ============================================================== #


def write_distilled_summary(
    store: Any,
    *,
    summary: str,
    sid: str,
    project_id: str,
    original_count: int,
    distilled_at: Optional[str] = None,
    extra_meta: Optional[dict] = None,
) -> str:
    """Write ONE distilled summary entry into the ``("project", pid)``
    namespace via the consultants BaseStore.

    The key is deterministic on ``(sid, content_hash)`` so re-running
    distillation on the same data is a silent no-op (the BaseStore
    upserts by key, but the providers also collide on content_hash).

    Returns the key written, for logging + tests.
    """
    if not store:
        raise DistillationFailed(
            "no store provided; can't write distilled summary",
        )
    if not summary or not summary.strip():
        raise DistillationFailed(
            "empty summary; refusing to write",
        )
    if distilled_at is None:
        distilled_at = datetime.now(timezone.utc).isoformat()
    # Stable key: ``distill-<sid_short>-<hash>``. The sid prefix
    # makes the key human-readable in store listings; the hash
    # prevents collision when one sid distills twice (rare — the
    # reaper deletes originals so the second sweep finds nothing).
    sid_short = (sid or "unknown")[:24]
    h = hashlib.sha256(summary.encode("utf-8")).hexdigest()[:12]
    key = f"distill-{sid_short}-{h}"
    value: dict = {
        "text": summary,
        "distilled_from_sid": sid,
        "distilled_at": distilled_at,
        "original_count": int(original_count),
    }
    if extra_meta:
        value.update(extra_meta)
    # Use the Namespaces factory so the (head, project_id) tuple
    # shape stays the M8 convention.
    from consultants.engine.store import Namespaces
    ns = Namespaces.project(project_id)
    # Index on "text" so the summary is recall-searchable; metadata
    # carries the rest as side data.
    #
    # Post-#212: ProviderBackedStore._do_put propagates durable-
    # write failures. Convert them to DistillationFailed so the
    # reaper's existing `except DistillationFailed` block at
    # sweep_once treats this as "originals stay; retry next tick"
    # rather than "summary landed; safe to delete." This is the
    # M14 critical invariant — research originals only delete
    # after a *successful* project-namespace write.
    try:
        store.put(ns, key, value, index=["text"])
    except DistillationFailed:
        raise
    except Exception as e:
        raise DistillationFailed(
            f"durable write to project namespace failed: {e!r}",
        ) from e
    return key
