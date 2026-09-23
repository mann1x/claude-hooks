"""Ollama Cloud prices, dated, and the $ cost of a recorded run.

The account is billed in dollars against a monthly budget, per token,
per model, so a model comparison that stops at token counts leaves out
the number the choice is actually made on. This module is the one place
the prices live; every bench and report prices through it.

The table is a snapshot. Prices move, so it carries the date it was
read and the page it was read from, and a report states which snapshot
it used. To refresh: re-read the page, add a new table with its date,
and keep the old one — a cost recorded against an old snapshot must
stay reproducible.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

PRICING_SOURCE = "https://ollama.com/pricing"
PRICING_DATE = "2026-09-23"


@dataclass(frozen=True)
class Price:
    """US$ per million tokens."""
    input: float
    output: float
    cached_input: Optional[float] = None
    off_peak_input: Optional[float] = None
    off_peak_output: Optional[float] = None


#: Read from PRICING_SOURCE on PRICING_DATE. Off-peak rates are listed
#: for the deepseek models only.
PRICES: dict[str, Price] = {
    "deepseek-v4.1-flash": Price(0.30, 1.20, 0.006, 0.15, 0.60),
    "deepseek-v4-flash": Price(0.44, 1.32, 0.014, 0.22, 0.66),
    "deepseek-v4-pro": Price(1.32, 3.96, 0.044, 0.66, 1.98),
    "gemma4": Price(0.14, 0.40, 0.05),
    "glm-5.3": Price(1.40, 4.40, 0.26),
    "glm-5.3-flash": Price(0.15, 0.50, 0.03),
    "glm-5.2": Price(1.40, 4.40, 0.26),
    "glm-5.1": Price(1.00, 3.20, 0.20),
    "gpt-oss:120b": Price(0.15, 0.60, 0.014),
    "gpt-oss:20b": Price(0.07, 0.30, 0.035),
    "kimi-k3": Price(3.00, 15.00, 0.30),
    "kimi-k2.7-code": Price(0.95, 4.00, 0.19),
    "kimi-k2.6": Price(0.95, 4.00, 0.16),
    "minimax-m3": Price(0.60, 2.40, 0.12),
    "minimax-m2.7": Price(0.30, 1.20, 0.06),
    "mistral-large-3": Price(0.50, 1.50),
    "nemotron-3-nano": Price(0.06, 0.24),
    "nemotron-3-super": Price(0.015, 0.60, 0.015),
    "nemotron-3-ultra": Price(0.10, 3.00, 0.10),
    "qwen3.5:397b": Price(0.60, 3.60),
}


def model_key(tag: str) -> Optional[str]:
    """Map an Ollama tag to its row in :data:`PRICES`, or None.

    ``gemma4:31b-cloud`` → ``gemma4``, ``gpt-oss:120b-cloud`` →
    ``gpt-oss:120b``, ``kimi-k2.6:cloud`` → ``kimi-k2.6``. A tag with no
    row (``gemini-3-flash-preview:cloud``, ``qwen3.5:cloud``) is None,
    and is reported as unpriced — never guessed from a neighbour.
    """
    t = (tag or "").strip().lower()
    if t.endswith(":cloud"):
        t = t[: -len(":cloud")]
    elif t.endswith("-cloud"):
        t = t[: -len("-cloud")]
    if t in PRICES:
        return t
    base = t.split(":", 1)[0]
    return base if base in PRICES else None


def is_off_peak(at: datetime) -> bool:
    """Off-peak is outside 12:00–18:00 UTC on weekdays, and all weekend."""
    at = at.astimezone(timezone.utc) if at.tzinfo else at.replace(
        tzinfo=timezone.utc)
    return at.weekday() >= 5 or not (12 <= at.hour < 18)


def rates(tag: str, at: Optional[datetime] = None
          ) -> Optional[tuple[float, float]]:
    """(input, output) $/M for ``tag`` at time ``at``; peak when unknown."""
    key = model_key(tag)
    if key is None:
        return None
    p = PRICES[key]
    if at is not None and is_off_peak(at) and p.off_peak_input is not None:
        return p.off_peak_input, p.off_peak_output
    return p.input, p.output


def cost_usd(tag: str, prompt: int, completion: int,
             at: Optional[datetime] = None) -> Optional[float]:
    """Dollars for one model's tokens, or None when the model is unpriced.

    Every prompt token is priced at the uncached rate: the traces do not
    record cache hits, so this is an upper bound whenever the provider
    served part of a prompt from cache.
    """
    r = rates(tag, at)
    if r is None:
        return None
    return (prompt * r[0] + completion * r[1]) / 1_000_000


def usage_cost(usage: dict, at: Optional[datetime] = None) -> dict:
    """Price a ``trial.usage`` record.

    Returns ``{"by_role": {role: $}, "total": $, "unpriced": [roles]}``.
    A role whose model has no price is listed, not dropped and not
    counted as free: the total is then a floor and says so.
    """
    by_role: dict[str, float] = {}
    unpriced: list[str] = []
    for role, u in (usage or {}).items():
        c = cost_usd(u.get("model", ""), int(u.get("prompt") or 0),
                     int(u.get("completion") or 0), at)
        if c is None:
            unpriced.append(role)
        else:
            by_role[role] = c
    return {"by_role": by_role, "total": sum(by_role.values()),
            "unpriced": unpriced}
