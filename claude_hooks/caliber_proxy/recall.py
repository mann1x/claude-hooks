"""Optional cross-provider recall + store-back layer for the caliber
grounding proxy.

When enabled, every ``POST /v1/chat/completions`` is augmented two ways:

1. **Server-side prepend.** Before forwarding to Ollama, the proxy
   gathers the latest user message, runs ``recall(query, k)`` against
   every enabled memory provider (Qdrant, Memory KG, pgvector,
   sqlite_vec — whatever's configured in ``config/claude-hooks.json``),
   merges the hits, and prepends a ``## Recalled memory`` system
   message. Mirrors the deterministic ``UserPromptSubmit`` recall
   claude-hooks runs inside Claude Code.

2. **Store-back.** After the agent loop completes, the assistant's
   final content is sent through ``store(content, metadata)`` on every
   enabled provider. Idempotency is the backend's responsibility — the
   pgvector + qdrant providers both dedup on hash; memory_kg appends
   observations to the project entity.

3. **Tool.** A ``recall_memory(query, k)`` tool is exposed alongside
   ``read_file`` / ``grep`` / ``glob`` / ``list_files`` so the model
   can ask for deeper queries explicitly mid-loop.

Everything is best-effort: any error path logs a warning and degrades
silently (returns empty for recall, no-op for store). The proxy never
fails just because recall did.

Config lives in ``config/claude-hooks.json`` under the existing
``providers`` block — same backends Claude Code's hooks use, so users
who already have qdrant or pgvector set up don't configure it twice.
A few env knobs override behaviour without touching the JSON:

  CALIBER_GROUNDING_RECALL_ENABLED  - on by default when providers exist
  CALIBER_GROUNDING_RECALL_K        - per-provider top-k (default 5)
  CALIBER_GROUNDING_RECALL_STORE    - store-back on/off (default on)
  CALIBER_GROUNDING_RECALL_PROVIDERS - comma-list to restrict which
                                       providers participate (e.g.
                                       ``pgvector,qdrant``); empty = all
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("claude_hooks.caliber_proxy.recall")

DEFAULT_K = 5
# Cap on the query text we hand to the embedder. Default 16000 chars
# (~4000 tokens) sits comfortably inside qwen3-embedding-0.6b's 16k
# context window so we don't lose recall fidelity by truncating
# caliber's longer prompts. Override via env if your daemon is
# warmed at a different num_ctx.
DEFAULT_QUERY_MAX_CHARS = 16000
DEFAULT_STORE_MIN_CHARS = 200
DEFAULT_STORE_MAX_CHARS = 4000


# -- Config ----------------------------------------------------------- #
@dataclass
class RecallConfig:
    enabled: bool
    k: int
    store_back: bool
    providers_filter: Optional[set[str]]  # None = all enabled
    query_max_chars: int
    store_min_chars: int
    store_max_chars: int


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def load_config() -> RecallConfig:
    pf_raw = os.environ.get("CALIBER_GROUNDING_RECALL_PROVIDERS", "").strip()
    providers_filter = (
        {p.strip() for p in pf_raw.split(",") if p.strip()}
        if pf_raw else None
    )
    return RecallConfig(
        enabled=_env_flag("CALIBER_GROUNDING_RECALL_ENABLED", True),
        k=_env_int("CALIBER_GROUNDING_RECALL_K", DEFAULT_K),
        store_back=_env_flag("CALIBER_GROUNDING_RECALL_STORE", True),
        providers_filter=providers_filter,
        query_max_chars=_env_int(
            "CALIBER_GROUNDING_RECALL_QUERY_MAX_CHARS", DEFAULT_QUERY_MAX_CHARS,
        ),
        store_min_chars=_env_int(
            "CALIBER_GROUNDING_RECALL_STORE_MIN_CHARS", DEFAULT_STORE_MIN_CHARS,
        ),
        store_max_chars=_env_int(
            "CALIBER_GROUNDING_RECALL_STORE_MAX_CHARS", DEFAULT_STORE_MAX_CHARS,
        ),
    )


# -- Provider cache --------------------------------------------------- #
# Build once per process. Providers are lightweight wrappers around an
# embedder + connection / MCP client; rebuilding on every request would
# pay the ~30-50ms init cost on the hot path.
_LOCK = threading.Lock()
_PROVIDERS: Optional[list] = None  # list[Provider]
_LAST_FILTER: Optional[set[str]] = None


def _get_providers(cfg: RecallConfig) -> list:
    """Build (and cache) the list of provider instances to fan out over."""
    global _PROVIDERS, _LAST_FILTER
    with _LOCK:
        if _PROVIDERS is not None and _LAST_FILTER == cfg.providers_filter:
            return _PROVIDERS
        try:
            from claude_hooks.config import load_config as load_app_config
            from claude_hooks.dispatcher import build_providers
        except Exception as e:
            log.warning("recall: cannot import provider stack: %s", e)
            _PROVIDERS = []
            _LAST_FILTER = cfg.providers_filter
            return _PROVIDERS
        try:
            app_cfg = load_app_config()
            # Pin embedding models in VRAM for the lifetime of this proxy
            # — caliber's run hits us with hundreds of recall+store
            # embedding calls, and Ollama's default 5-min TTL combined
            # with VRAM pressure from a chat model that's already loaded
            # otherwise causes constant evict/reload thrashing. Override
            # only here, not for the regular claude-hooks recall path,
            # because hooks fire infrequently and don't need the pin.
            providers_cfg = app_cfg.setdefault("providers", {})
            for pname in ("pgvector", "sqlite_vec"):
                pcfg = providers_cfg.get(pname)
                if not pcfg or not pcfg.get("enabled"):
                    continue
                eopts = dict(pcfg.get("embedder_options") or {})
                # Ollama parses keep_alive as either an integer (seconds)
                # or a Go duration string ("5m", "1h"). The string "-1"
                # fails parsing — Ollama wants the literal integer -1
                # to mean "keep loaded forever".
                eopts.setdefault("keep_alive", -1)
                # Force the embedder to CPU/RAM in the caliber-proxy
                # context. The chat model running alongside (gemma4-98e
                # at 19.9B Q6_K) routinely claims ~22 GB of VRAM on a
                # 24 GB 3090, leaving no room for qwen3-embedding to
                # coexist on GPU; Ollama would otherwise evict one of
                # them. CPU embedding for a 0.6B model is fast enough
                # for recall (~50-200ms) and keeps the embedder
                # always-resident with no eviction. Override via
                # CALIBER_GROUNDING_RECALL_EMBED_NUM_GPU when the
                # daemon has VRAM to spare.
                num_gpu_env = os.environ.get(
                    "CALIBER_GROUNDING_RECALL_EMBED_NUM_GPU")
                try:
                    eopts["num_gpu"] = (
                        int(num_gpu_env) if num_gpu_env is not None else 0
                    )
                except ValueError:
                    eopts["num_gpu"] = 0
                # Pin num_ctx to a value the daemon was warmed at —
                # Ollama allocates KV cache statically at model-load, so
                # this also bounds VRAM. 16k is the tested sweet spot for
                # qwen3-embedding-0.6b: ~3.5 GiB at q8_0 KV, large enough
                # to embed real caliber queries without truncation
                # artefacts. We override unconditionally — the user's
                # claude-hooks.json may set 32k for the regular hook
                # recall path, which would force qwen3-embedding to
                # reload at 32k mid-run and evict whatever was sharing
                # GPU. Override with CALIBER_GROUNDING_RECALL_EMBED_NUM_CTX
                # if the proxy needs a different ceiling.
                num_ctx_env = os.environ.get(
                    "CALIBER_GROUNDING_RECALL_EMBED_NUM_CTX")
                try:
                    eopts["num_ctx"] = (
                        int(num_ctx_env) if num_ctx_env else 16384
                    )
                except ValueError:
                    eopts["num_ctx"] = 16384
                # Embedder request timeout. The OllamaEmbedder default
                # is 10s and main claude-hooks config sets 30s. Cold-
                # loading qwen3-embedding:0.6b on the first call after
                # ollama auto-evicted it — observed at 1m45s on a
                # 24 GB GPU when gemma was holding the model slot —
                # blows through anything under ~120s, producing
                # recurring "ollama unreachable: timed out" warnings
                # even though the daemon is healthy and would respond
                # if we waited. Default to 180s so the cold-start fits
                # without a retry; the proxy also pre-warms at startup
                # (see ``preheat_embedder``) so steady-state calls
                # rarely need more than a few seconds. Override with
                # CALIBER_GROUNDING_RECALL_EMBED_TIMEOUT for tuning.
                timeout_env = os.environ.get(
                    "CALIBER_GROUNDING_RECALL_EMBED_TIMEOUT")
                try:
                    eopts["timeout"] = (
                        float(timeout_env) if timeout_env else 180.0
                    )
                except ValueError:
                    eopts["timeout"] = 180.0
                pcfg = dict(pcfg)
                pcfg["embedder_options"] = eopts
                providers_cfg[pname] = pcfg
            built = build_providers(app_cfg)
        except Exception as e:
            log.warning("recall: build_providers failed: %s", e)
            _PROVIDERS = []
            _LAST_FILTER = cfg.providers_filter
            return _PROVIDERS
        if cfg.providers_filter is not None:
            built = [p for p in built if p.name in cfg.providers_filter]
        names = [p.name for p in built]
        log.info("recall: %d provider(s) active: %s", len(built),
                 ", ".join(names) if names else "(none)")
        _PROVIDERS = built
        _LAST_FILTER = cfg.providers_filter
        return _PROVIDERS


def reset_state_for_tests() -> None:
    """Drop cached provider list. Tests use this between cases."""
    global _PROVIDERS, _LAST_FILTER
    with _LOCK:
        _PROVIDERS = None
        _LAST_FILTER = None


def preheat_embedder(cfg: Optional[RecallConfig] = None) -> dict[str, str]:
    """Fire one tiny ``embed("warmup")`` per enabled provider so the
    embedding model is loaded into Ollama's slot before caliber's
    real prompts arrive.

    Cold-loading ``qwen3-embedding:0.6b`` after ollama evicted it can
    take 90-110s; the recall path's 180s timeout absorbs that, but the
    delay still shows up as a 60-180s lag on the *first* caliber
    request, which derails the user's mental model of how the proxy is
    behaving. Pre-warming at startup pushes that cost to a place where
    it's expected — the moment the proxy becomes ready to serve.

    Best-effort. Logs the per-provider outcome and never raises so a
    flaky embedding endpoint doesn't block the proxy from starting.
    Returns ``{provider_name: status}`` for tests / logging.

    Set ``CALIBER_GROUNDING_PREHEAT=0`` to skip.
    """
    if not _env_flag("CALIBER_GROUNDING_PREHEAT", default=True):
        return {}
    cfg = cfg or load_config()
    if not cfg.enabled:
        return {}
    providers = _get_providers(cfg)
    if not providers:
        return {}
    results: dict[str, str] = {}
    import time as _time
    for p in providers:
        # Prefer the public `recall` API — it triggers the provider's
        # internal `_ensure_ready` (which lazily constructs the
        # embedder + opens the DB connection) and then runs an embed
        # for the warmup query, matching exactly what the first real
        # recall would do. Falls back to a direct embedder call when
        # the provider exposes one (covers test fakes that don't
        # implement the full lazy-init dance).
        t0 = _time.monotonic()
        try:
            embedder = (
                getattr(p, "_embedder", None) or getattr(p, "embedder", None)
            )
            if hasattr(p, "recall"):
                p.recall("warmup", k=1)
            elif embedder is not None and hasattr(embedder, "embed"):
                embedder.embed("warmup")
            else:
                results[p.name] = "skipped (no embedder)"
                continue
            dt = _time.monotonic() - t0
            results[p.name] = f"ok in {dt:.1f}s"
            log.info("recall: pre-warmed %s in %.1fs", p.name, dt)
        except Exception as e:
            dt = _time.monotonic() - t0
            results[p.name] = f"failed after {dt:.1f}s: {e}"
            log.warning(
                "recall: pre-warm %s failed after %.1fs: %s",
                p.name, dt, e,
            )
    return results


# -- Recall ----------------------------------------------------------- #
def recall_hits(query: str, cfg: Optional[RecallConfig] = None,
                k: Optional[int] = None) -> list[dict]:
    """Fan out ``query`` to every enabled provider, collect hits, dedup
    on text. Returns ``[{text, metadata, source_provider}, ...]``.
    Empty list on any failure or when disabled.
    """
    cfg = cfg or load_config()
    if not cfg.enabled or not query.strip():
        return []
    k = k if k is not None and k > 0 else cfg.k
    providers = _get_providers(cfg)
    if not providers:
        return []
    truncated = query[: cfg.query_max_chars]
    seen_texts: set[str] = set()
    out: list[dict] = []
    for p in providers:
        try:
            hits = p.recall(truncated, k=k)
        except Exception as e:
            log.warning("recall: provider %s recall failed: %s", p.name, e)
            continue
        for m in hits or []:
            text = getattr(m, "text", None) or ""
            if not text:
                continue
            key = " ".join(text.split())[:200]
            if key in seen_texts:
                continue
            seen_texts.add(key)
            metadata = getattr(m, "metadata", None) or {}
            source = (
                getattr(m, "source_provider", "")
                or p.display_name
                or p.name
            )
            out.append({
                "text": text,
                "metadata": metadata,
                "source_provider": source,
            })
    return out


def format_hits(hits: list[dict], header: str = "## Recalled memory") -> str:
    """Render hits as a markdown block ready to inject as a system
    message. Trims content to ~600 chars per hit so a chatty store
    doesn't blow the model's context.

    Hits are grouped by provider (matches the format claude-hooks uses
    in ``UserPromptSubmit`` so the model sees a familiar shape).
    """
    if not hits:
        return ""
    by_provider: dict[str, list[str]] = {}
    for h in hits:
        text = (h.get("text") or "").strip()
        if not text:
            continue
        if len(text) > 600:
            text = text[:600].rstrip() + " […]"
        single = " ".join(text.split())
        src = h.get("source_provider") or "memory"
        by_provider.setdefault(src, []).append(single)
    if not by_provider:
        return ""
    lines = [header, ""]
    for src, items in by_provider.items():
        lines.append(f"### {src} ({len(items)})")
        for item in items:
            lines.append(f"- {item}")
        lines.append("")
    return "\n".join(lines).strip()


def build_prepend_messages(latest_user_text: str,
                            cfg: Optional[RecallConfig] = None) -> list[dict]:
    """Turn the latest user-text into a list of system messages to
    prepend before the model's grounding messages. Empty list when
    recall is disabled, the query is empty, or no hits come back.
    """
    cfg = cfg or load_config()
    if not cfg.enabled or not latest_user_text.strip():
        return []
    hits = recall_hits(latest_user_text, cfg=cfg)
    block = format_hits(hits)
    if not block:
        return []
    return [{"role": "system", "content": block}]


def latest_user_text(messages: list[dict]) -> str:
    """Pick the most recent ``user`` message's text — that's the query."""
    for m in reversed(messages or []):
        if not isinstance(m, dict):
            continue
        if m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            # OpenAI vision-style content can be a list of parts.
            parts = [p.get("text", "") for p in c if isinstance(p, dict)]
            return "\n".join(parts)
    return ""


# -- Store-back ------------------------------------------------------- #
def store(content: str, metadata: Optional[dict] = None,
          cfg: Optional[RecallConfig] = None) -> int:
    """Fan out a store call across every enabled provider. Returns the
    count of providers that didn't raise (False per-provider on raise).
    Caller doesn't need to check this; it's just for tests + diagnostics.
    """
    cfg = cfg or load_config()
    if not cfg.enabled or not cfg.store_back:
        return 0
    text = (content or "").strip()
    if len(text) < cfg.store_min_chars:
        return 0
    if len(text) > cfg.store_max_chars:
        text = text[: cfg.store_max_chars]
    providers = _get_providers(cfg)
    ok = 0
    for p in providers:
        try:
            p.store(text, metadata or {})
            ok += 1
        except Exception as e:
            log.warning("recall.store: provider %s failed: %s", p.name, e)
    return ok


def maybe_store_assistant_turn(payload: dict, result: dict,
                                cfg: Optional[RecallConfig] = None) -> None:
    """After the agent loop, save the assistant's final content if it
    looks substantive. Tagged with ``source=caliber-grounding-proxy``
    in metadata so future recalls can filter by provenance.
    """
    cfg = cfg or load_config()
    if not cfg.enabled or not cfg.store_back:
        return
    try:
        choice = (result.get("choices") or [{}])[0]
        content = (choice.get("message") or {}).get("content") or ""
        if not isinstance(content, str):
            return
        store(
            content,
            metadata={
                "source": "caliber-grounding-proxy",
                "model": payload.get("model"),
            },
            cfg=cfg,
        )
    except Exception as e:
        log.warning("recall.maybe_store: %s", e)
