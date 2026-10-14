"""History hygiene before a call is sized (plan Milestone C5).

Two rewrites of tool results already in a role's history, both ported
from the fork's ``message-builder.ts`` (``/shared/dev/cline``):

**Middle truncation.** A tool result past ``tool_result_max_chars``
(32,000; file contents ``file_result_max_chars``, 50,000) keeps its head
and tail. The marker is load-bearing: it names the harness as the one who
cut, says the source is complete, and says how to get the gap back. The
fork measured a model reading a spliced file, concluding it was damaged,
and spending a dozen turns "repairing" intact source. Today we cap only
at the source (48 KiB in ``read_file``); results from other tools, and a
history that crossed a model change, had no bound at all.

**Stale reads.** A ``read_file`` result superseded by a later read of the
same file (covering the same range) becomes
``[outdated - see the latest file content]``. Only once the history fills
``stale_read_share`` (0.4) of the window, and then every pending one at
once: each rewrite breaks the server's prefix cache from that point on,
and rewriting a few turns back on every read made it routine (the fork
measured 834 of 7,465 builds rewriting, 499k tokens re-prefilled). Below
the pressure a turn is a pure append. Nothing is rewritten unless the
reclaim is worth the break (2% of the window, 4 KB floor, 64 KB ceiling).

Both return the input list unchanged (the same object) when there is
nothing to do, so a history that never grows large is byte-identical.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

log = logging.getLogger("consultants.engine.history")

DEFAULT_TOOL_RESULT_MAX_CHARS = 32_000
DEFAULT_FILE_RESULT_MAX_CHARS = 50_000
FILE_TOOLS = ("read_file",)

OUTDATED_FILE_CONTENT = "[outdated - see the latest file content]"
STALE_READ_PRESSURE_SHARE = 0.4
APPROX_BYTES_PER_TOKEN = 4
REWRITE_CONTEXT_SHARE = 0.02
REWRITE_FLOOR_BYTES = 4_096
REWRITE_CEILING_BYTES = 65_536


def truncation_marker(n: int) -> str:
    return (f"\n\n...[truncated {n} chars: the harness removed them from the "
            f"middle of this tool result to keep the transcript small. The "
            f"file or command output itself is complete — the gap is only in "
            f"what you were shown here. Read the missing range again if you "
            f"need it.]...\n\n")


def _content(message: dict) -> Optional[str]:
    c = message.get("content")
    return c if isinstance(c, str) else None


def middle_truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    marker_room = len(truncation_marker(len(text)))
    keep = max(200, (limit - marker_room) // 2)
    head, tail = text[:keep], text[-keep:]
    return head + truncation_marker(len(text) - len(head) - len(tail)) + tail


def truncate_tool_results(messages: list[dict], *,
                          max_chars: int = DEFAULT_TOOL_RESULT_MAX_CHARS,
                          file_max_chars: int = DEFAULT_FILE_RESULT_MAX_CHARS
                          ) -> list[dict]:
    out: Optional[list[dict]] = None
    for i, m in enumerate(messages or []):
        if not isinstance(m, dict) or m.get("role") != "tool":
            continue
        text = _content(m)
        limit = file_max_chars if m.get("name") in FILE_TOOLS else max_chars
        if text is None or len(text) <= limit or "...[truncated " in text:
            continue
        if out is None:
            out = list(messages)
        out[i] = {**m, "content": middle_truncate(text, limit)}
        log.info("history: middle-truncated a %s result %d -> %d chars",
                 m.get("name") or "tool", len(text), len(out[i]["content"]))
    return messages if out is None else out


def _read_locator(arguments: Any) -> Optional[tuple[str, Optional[int],
                                                     Optional[int]]]:
    args = arguments
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (TypeError, ValueError):
            return None
    if not isinstance(args, dict) or not isinstance(args.get("path"), str):
        return None
    start, end = args.get("start_line"), args.get("end_line")
    return (args["path"], start if isinstance(start, int) else None,
            end if isinstance(end, int) else None)


def _covers(later: tuple, earlier: tuple) -> bool:
    """Whether a later read shows everything an earlier one did."""
    if later[0] != earlier[0]:
        return False
    ls, le = later[1], later[2]
    es, ee = earlier[1], earlier[2]
    if ls is None and le is None:
        return True
    if es is None and ee is None:
        return False
    return ((ls or 1) <= (es or 1)
            and (le is None or (ee is not None and le >= ee)))


def rewrite_threshold_bytes(context_length: Optional[int]) -> int:
    if not context_length:
        return REWRITE_CEILING_BYTES
    return int(min(REWRITE_CEILING_BYTES,
                   max(REWRITE_FLOOR_BYTES, context_length
                       * APPROX_BYTES_PER_TOKEN * REWRITE_CONTEXT_SHARE)))


def _history_bytes(messages: list[dict]) -> int:
    total = 0
    for m in messages or []:
        if isinstance(m, dict):
            total += len(json.dumps(m.get("content") or "", ensure_ascii=False))
            for tc in m.get("tool_calls") or []:
                total += len(str(((tc or {}).get("function") or {})
                                 .get("arguments") or ""))
    return total


def rewrite_stale_reads(messages: list[dict], *,
                        context_length: Optional[int],
                        share: float = STALE_READ_PRESSURE_SHARE
                        ) -> list[dict]:
    if not context_length or share <= 0:
        return messages
    if _history_bytes(messages) < context_length * APPROX_BYTES_PER_TOKEN * share:
        return messages
    reads: dict[str, tuple] = {}
    for m in messages or []:
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        for tc in m.get("tool_calls") or []:
            fn = (tc or {}).get("function") or {}
            if fn.get("name") in FILE_TOOLS and tc.get("id"):
                loc = _read_locator(fn.get("arguments"))
                if loc:
                    reads[tc["id"]] = loc
    results = [(i, m) for i, m in enumerate(messages)
               if isinstance(m, dict) and m.get("role") == "tool"
               and m.get("tool_call_id") in reads]
    stale: list[int] = []
    for pos, (i, m) in enumerate(results):
        if _content(m) in (None, OUTDATED_FILE_CONTENT):
            continue
        loc = reads[m["tool_call_id"]]
        if any(_covers(reads[later["tool_call_id"]], loc)
               and (_content(later) or "") not in ("", OUTDATED_FILE_CONTENT)
               for _, later in results[pos + 1:]):
            stale.append(i)
    reclaim = sum(len(_content(messages[i]) or "") for i in stale)
    if not stale or reclaim < rewrite_threshold_bytes(context_length):
        return messages
    out = list(messages)
    for i in stale:
        out[i] = {**out[i], "content": OUTDATED_FILE_CONTENT}
    log.info("history: rewrote %d superseded read(s), %d bytes reclaimed",
             len(stale), reclaim)
    return out


def tidy(messages: list[dict], *, context_length: Optional[int],
         settings: Any = None) -> list[dict]:
    """Both rewrites, with ``settings``' knobs
    (:class:`consultants.engine.compaction.Settings`)."""
    max_chars = getattr(settings, "tool_result_max_chars",
                        DEFAULT_TOOL_RESULT_MAX_CHARS)
    file_max = getattr(settings, "file_result_max_chars",
                       DEFAULT_FILE_RESULT_MAX_CHARS)
    share = getattr(settings, "stale_read_share", STALE_READ_PRESSURE_SHARE)
    out = truncate_tool_results(messages, max_chars=max_chars,
                                file_max_chars=file_max)
    return rewrite_stale_reads(out, context_length=context_length, share=share)


__all__ = ["truncate_tool_results", "rewrite_stale_reads", "tidy",
           "middle_truncate", "truncation_marker", "OUTDATED_FILE_CONTENT",
           "rewrite_threshold_bytes"]
