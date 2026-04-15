"""
Extract metadata from request / response bodies without modifying them.

Called from the proxy handler to build the JSONL log record. Must be
resilient to:

- JSON parse failures (return partial info)
- SSE (``text/event-stream``) responses — we peek the first ``message_start``
  event for the ``model`` + ``usage.input_tokens`` (the final usage arrives
  in ``message_delta`` but that's not in the first chunk; P1 will tail
  the stream for full usage)
- Binary / non-JSON bodies (skip)
- The synthetic-rate-limit marker: ``"model": "<synthetic>"`` with
  zero usage counts — proves the CLI blocked the call locally without
  hitting the API (per ArkNill's B3 analysis of issue #42796)
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional


def extract_request_info(
    body: bytes,
    headers: dict[str, str],
) -> dict[str, Any]:
    """Return key fields from a request JSON body, including a
    ``stream`` flag that P3's stub needs to decide between SSE and
    non-streaming replies.

    S2 additions — pulled from the live request shape observed on
    Claude Code 2.1.x:

    - ``cc_version`` / ``cc_entrypoint`` — from the first ``system``
      block (``x-anthropic-billing-header: cc_version=...;
      cc_entrypoint=...``). Correlates metric regressions with
      specific CC releases.
    - ``effort`` — from ``output_config.effort`` (medium / high / max).
      The bcherny thinking-tier dial; shifts since Opus 4.6 launch
      on Feb 9.
    - ``thinking_type`` — ``thinking.type`` (adaptive / disabled).
    - ``account_uuid`` — parsed out of ``metadata.user_id`` JSON.
    - ``num_tools`` / ``num_messages`` — rough complexity signals.
    - ``agent_type`` / ``agent_name`` — inferred:
        * ``warmup`` when the first user message is "Warmup"
        * ``main``   when ``system[1]`` starts with "You are Claude Code"
        * ``subagent`` otherwise, with ``agent_name`` parsed from the
          first sentence of ``system[1]``
    - ``request_class`` — same classification but used as the
      canonical rollup key.
    - ``beta_features`` — list of tokens from the ``anthropic-beta``
      request header.
    """
    out: dict[str, Any] = {
        "model_requested": None,
        "is_warmup": False,
        "session_id": None,
        "stream": False,
        "account_uuid": None,
        "cc_version": None,
        "cc_entrypoint": None,
        "effort": None,
        "thinking_type": None,
        "max_tokens": None,
        "num_tools": None,
        "num_messages": None,
        "agent_type": None,
        "agent_name": None,
        "request_class": None,
        "beta_features": None,
    }

    # Beta features come from the request header and are independent
    # of the body — capture them even on malformed bodies.
    out["beta_features"] = _extract_beta_features(headers)

    if not body:
        return out
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return out
    if not isinstance(data, dict):
        return out

    out["model_requested"] = data.get("model")
    out["stream"] = bool(data.get("stream"))
    out["max_tokens"] = data.get("max_tokens")

    # Thinking + effort dials (new since Opus 4.6).
    thinking = data.get("thinking")
    if isinstance(thinking, dict):
        out["thinking_type"] = thinking.get("type")
    oc = data.get("output_config")
    if isinstance(oc, dict):
        out["effort"] = oc.get("effort")

    # Counts — cheap, useful as complexity / depth signals.
    tools = data.get("tools")
    if isinstance(tools, list):
        out["num_tools"] = len(tools)
    msgs = data.get("messages")
    if isinstance(msgs, list):
        out["num_messages"] = len(msgs)

    # Warmup detection via first user message == "Warmup".
    first_user_text = _first_user_text(msgs)
    is_warmup = first_user_text.strip() == "Warmup"
    out["is_warmup"] = is_warmup

    # Agent classification from system block layout.
    agent_type, agent_name = _classify_agent(data.get("system"), is_warmup)
    out["agent_type"] = agent_type
    out["agent_name"] = agent_name
    out["request_class"] = agent_type

    # CC version / entrypoint from the first system block's billing tag.
    cc_v, cc_e = _extract_cc_billing(data.get("system"))
    out["cc_version"] = cc_v
    out["cc_entrypoint"] = cc_e

    # Session / account identifiers.
    md = data.get("metadata")
    if isinstance(md, dict):
        raw = md.get("user_id") or md.get("session_id")
        out["session_id"] = raw
        out["account_uuid"] = _extract_account_uuid(raw)

    return out


# ------------------------------------------------------------------ #
# S2 helpers
# ------------------------------------------------------------------ #
_MAIN_AGENT_PREFIX = "You are Claude Code"


def _first_user_text(messages: Any) -> str:
    """First ``text`` block of the first ``user`` message, or empty."""
    if not isinstance(messages, list) or not messages:
        return ""
    first = messages[0]
    if not isinstance(first, dict) or first.get("role") != "user":
        return ""
    content = first.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                return block.get("text", "") or ""
    return ""


def _classify_agent(
    system: Any, is_warmup: bool,
) -> tuple[Optional[str], Optional[str]]:
    """Return ``(agent_type, agent_name)`` from the ``system`` block
    shape.

    Claude Code sends ``system`` as a list of text blocks:
        [0] billing tag     (x-anthropic-billing-header: cc_version=...)
        [1] primary persona ("You are Claude Code..." for main;
                              "You are a code reviewer..." etc. for subagent)
        [2] full instructions

    The second block's opening sentence is the cleanest signal.
    ``agent_name`` is the first few words of that block when it's a
    subagent (used to group the agent_rollup).
    """
    if is_warmup:
        return "warmup", "warmup"
    persona = _find_persona_text(system)
    if not persona:
        return "unknown", None
    if persona.startswith(_MAIN_AGENT_PREFIX):
        return "main", "main"
    # Subagent — try to extract a readable handle from the first clause.
    name = _extract_agent_name(persona)
    return "subagent", name


def _find_persona_text(system: Any) -> str:
    """Return the first non-billing ``text`` block from ``system``."""
    if isinstance(system, str):
        return system
    if not isinstance(system, list):
        return ""
    for block in system:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "text":
            continue
        text = block.get("text", "") or ""
        if not text or text.lower().startswith("x-anthropic-billing-header"):
            continue
        return text
    return ""


def _extract_agent_name(persona: str) -> Optional[str]:
    """Extract a short agent-name handle from the opening of a subagent
    system prompt.

    Examples:
        "You are a code reviewer specialized in..." → "code reviewer"
        "You are the General-Purpose research agent..." → "General-Purpose"

    Bounded to ≤ 60 chars, single line. Returns None if the prompt
    doesn't start with "You are".
    """
    first_line = persona.split("\n", 1)[0].strip()
    if not first_line.lower().startswith("you are"):
        return None
    tail = first_line[len("you are"):].strip().lstrip("aan ").strip()
    # Cut at first sentence-ending punctuation so we don't capture a paragraph.
    for sep in (".", "!", "?", ":", ";", " that ", " who ", " specialized ",
                " responsible "):
        idx = tail.find(sep)
        if idx >= 0:
            tail = tail[:idx]
    tail = tail.strip()[:60]
    return tail or None


_CC_VERSION_RE = re.compile(r"cc_version=([^;\s]+)")
_CC_ENTRYPOINT_RE = re.compile(r"cc_entrypoint=([^;\s]+)")


def _extract_cc_billing(system: Any) -> tuple[Optional[str], Optional[str]]:
    """Parse ``cc_version`` + ``cc_entrypoint`` from the billing
    system block (``x-anthropic-billing-header: cc_version=...;
    cc_entrypoint=...``).
    """
    if isinstance(system, str):
        blocks_text = [system]
    elif isinstance(system, list):
        blocks_text = [
            b.get("text", "") for b in system
            if isinstance(b, dict) and b.get("type") == "text"
        ]
    else:
        return None, None
    for text in blocks_text:
        if not isinstance(text, str):
            continue
        if "cc_version" not in text:
            continue
        m_v = _CC_VERSION_RE.search(text)
        m_e = _CC_ENTRYPOINT_RE.search(text)
        return (m_v.group(1) if m_v else None,
                m_e.group(1) if m_e else None)
    return None, None


def _extract_account_uuid(user_id: Any) -> Optional[str]:
    """Parse ``account_uuid`` out of ``metadata.user_id``.

    Two observed formats:
      * JSON: ``{"device_id":"...","account_uuid":"...","session_id":"..."}``
      * Legacy: ``user_<dev>_account_<uuid>_session_<sess>``
    """
    if not isinstance(user_id, str):
        return None
    # JSON form
    if user_id.startswith("{"):
        try:
            j = json.loads(user_id)
        except json.JSONDecodeError:
            j = None
        if isinstance(j, dict):
            v = j.get("account_uuid")
            if isinstance(v, str):
                return v
    # Legacy form
    m = re.search(r"account_([0-9a-fA-F-]{32,})", user_id)
    if m:
        return m.group(1)
    return None


def _extract_beta_features(headers: dict[str, str]) -> Optional[list[str]]:
    """Parse the ``anthropic-beta`` request header into a list of
    feature tokens. Header is case-insensitive; values are
    comma-separated.
    """
    if not headers:
        return None
    value = None
    for k, v in headers.items():
        if k.lower() == "anthropic-beta":
            value = v
            break
    if not value:
        return None
    toks = [t.strip() for t in value.split(",") if t.strip()]
    return toks or None


def extract_response_info(
    headers: dict[str, str],
    first_chunk: Optional[bytes],
) -> dict[str, Any]:
    """Return status / model / usage / rate-limit info from response headers +
    first body chunk. ``first_chunk`` may be the full body (JSON) or the
    opening SSE event bytes.
    """
    out: dict[str, Any] = {
        "model_delivered": None,
        "usage": None,
        "rate_limit": _extract_rate_limit(headers),
        "synthetic": False,
    }
    if not first_chunk:
        return out
    # Try JSON first.
    parsed = None
    try:
        parsed = json.loads(first_chunk)
    except (json.JSONDecodeError, UnicodeDecodeError):
        parsed = None
    if isinstance(parsed, dict):
        out["model_delivered"] = parsed.get("model")
        u = parsed.get("usage")
        if isinstance(u, dict):
            out["usage"] = u
        if parsed.get("model") == "<synthetic>":
            out["synthetic"] = True
        return out
    # Else try SSE: scan the chunk for the first ``data: {...}`` line of type
    # ``message_start``. That event carries the model + initial usage.
    try:
        text = first_chunk.decode("utf-8", errors="replace")
    except Exception:
        return out
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload:
            continue
        try:
            evt = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if not isinstance(evt, dict):
            continue
        if evt.get("type") == "message_start":
            msg = evt.get("message") or {}
            out["model_delivered"] = msg.get("model")
            u = msg.get("usage")
            if isinstance(u, dict):
                out["usage"] = u
            if msg.get("model") == "<synthetic>":
                out["synthetic"] = True
            break
    return out


# ------------------------------------------------------------------ #
# Rate-limit headers
# ------------------------------------------------------------------ #
# Anthropic returns a cluster of ``anthropic-ratelimit-unified-*``
# headers for subscription users (per ArkNill's analysis). Capture the
# numeric ones verbatim; leave the raw dict under ``rate_limit`` for
# P1 to consume.

_RL_HEADER_PREFIXES = (
    "anthropic-ratelimit-",
    "x-ratelimit-",   # older variant seen on some endpoints
    "retry-after",
)


def _extract_rate_limit(headers: dict[str, str]) -> Optional[dict[str, str]]:
    if not headers:
        return None
    out: dict[str, str] = {}
    for k, v in headers.items():
        lk = k.lower()
        for prefix in _RL_HEADER_PREFIXES:
            if lk.startswith(prefix):
                out[lk] = v
                break
    return out or None
