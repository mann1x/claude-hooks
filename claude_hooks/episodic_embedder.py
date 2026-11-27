"""episodic-memory embeds with the host's configured embedder.

The vendored episodic-memory ships its own model (bge-small, 384 dims) and
reads ``<superpowers>/embedder.json`` to use another one (see
``episodic-memory/src/embedder-config.ts``). This module writes that file
from the embedder the user configured for claude-hooks' own memory — the
enabled ``pgvector`` provider's, else ``sqlite_vec``'s — so episodic search
and memory recall share one model and one server.

The file names the vector space by the model, never by the server: a
one-time rebuild may run on another server of the same model (a GPU
Ollama) while queries go to the configured one. A file without our
``_managed_by`` marker is the user's own and is left alone.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Callable, Optional

MARKER = "claude-hooks"
LLAMAFILE_DEFAULT_URL = "http://127.0.0.1:38092/embedding"
OLLAMA_DEFAULT_URL = "http://localhost:11434/api/embeddings"
OLLAMA_DEFAULT_MODEL = "nomic-embed-text"
PROVIDERS = ("pgvector", "sqlite_vec")


def superpowers_dir() -> Path:
    """Same precedence as ``getSuperpowersDir()`` in episodic-memory's paths.ts."""
    for var in ("EPISODIC_MEMORY_CONFIG_DIR", "PERSONAL_SUPERPOWERS_DIR"):
        if os.environ.get(var):
            return Path(os.environ[var])
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "superpowers"
    return Path.home() / ".config" / "superpowers"


def config_path() -> Path:
    return superpowers_dir() / "embedder.json"


def _configured(cfg: dict) -> tuple[Optional[str], str, dict]:
    """(provider, embedder name, options) of the first enabled store."""
    providers = cfg.get("providers") or {}
    for name in PROVIDERS:
        p = providers.get(name) or {}
        if p.get("enabled"):
            return name, str(p.get("embedder") or ""), dict(p.get("embedder_options") or {})
    return None, "", {}


def _ollama_embed_url(url: str) -> str:
    """OllamaEmbedder posts to ``/api/embeddings``; episodic batches on ``/api/embed``."""
    base = re.sub(r"/api/embed(dings)?/?$", "", url.rstrip("/"))
    return base + "/api/embed"


def _spec(name: str, opts: dict) -> tuple[Optional[dict], str]:
    """One embedder as episodic-memory's JSON, or (None, why not)."""
    if name == "llamafile":
        model = opts.get("model")
        if not model:
            return None, ("the llamafile embedder has no embedder_options.model, "
                          "which names its vector space")
        spec = {"kind": "llamafile", "url": opts.get("url") or LLAMAFILE_DEFAULT_URL,
                "model": model}
    elif name == "ollama":
        spec = {"kind": "ollama", "url": _ollama_embed_url(opts.get("url") or OLLAMA_DEFAULT_URL),
                "model": opts.get("model") or OLLAMA_DEFAULT_MODEL}
    elif name in ("openai", "openai_compatible"):
        if not opts.get("url") or not opts.get("model"):
            return None, f"the {name} embedder needs url and model"
        spec = {"kind": "openai", "url": opts["url"], "model": opts["model"]}
        if opts.get("api_key"):
            spec["api_key"] = opts["api_key"]
    else:
        return None, f"embedder {name or 'none'!r} has no episodic-memory equivalent"
    timeout = opts.get("timeout")
    if isinstance(timeout, (int, float)) and timeout > 0:
        spec["timeout_ms"] = int(timeout * 1000)
    return spec, ""


def spec_for(cfg: dict) -> tuple[Optional[dict], str]:
    """The ``embedder.json`` the host's config asks for, or (None, why)."""
    provider, name, opts = _configured(cfg)
    if provider is None:
        return None, "no pgvector or sqlite_vec provider is enabled"
    if name == "composite":
        primary, why = _spec(str(opts.get("primary") or ""), dict(opts.get("primary_options") or {}))
        if primary is None:
            return None, f"providers.{provider}: composite primary: {why}"
        fallback, _ = _spec(str(opts.get("fallback") or ""), dict(opts.get("fallback_options") or {}))
        # A fallback of another model would write into another space.
        if fallback is not None and fallback["model"] == primary["model"]:
            primary["fallback"] = fallback
        spec = primary
    else:
        spec, why = _spec(name, opts)
        if spec is None:
            return None, f"providers.{provider}: {why}"
    spec["_managed_by"] = MARKER
    spec["_source"] = f"providers.{provider}"
    return spec, ""


def read_current() -> Optional[dict]:
    try:
        return json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def sync_file(cfg: dict, *, dry: bool = False,
              note: Callable[[str], None] = print) -> str:
    """Make ``embedder.json`` match the config.

    Returns ``written`` / ``unchanged`` / ``removed`` / ``user-owned`` /
    ``none``. A file the user wrote (no marker) is never touched.
    """
    path = config_path()
    want, why = spec_for(cfg)
    have = read_current()
    exists = path.exists()
    if exists and (not isinstance(have, dict) or have.get("_managed_by") != MARKER):
        note(f"{path} is not ours; left as it is")
        return "user-owned"
    if want is None:
        if exists:
            note(f"episodic-memory embedder: {why}; removing {path} (bundled model)")
            if not dry:
                path.unlink()
            return "removed"
        note(f"episodic-memory embedder: {why}; it keeps its bundled model")
        return "none"
    if have == want:
        note(f"episodic-memory embedder: {want['model']} via {want['kind']} (unchanged)")
        return "unchanged"
    note(f"episodic-memory embedder: {want['model']} via {want['kind']} at {want['url']} -> {path}")
    if not dry:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        # 0600: an openai-style entry may carry the provider's key.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(want, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, path)
    return "written"


def ensure_embedder(cfg: dict) -> None:
    """Before episodic-memory embeds: a daemon-managed llamafile may have
    been idle-reaped, and only the daemon can start it again. Best effort,
    like ``LlamafileEmbedder._ensure_running``."""
    _, name, opts = _configured(cfg)
    if name == "composite":
        name, opts = str(opts.get("primary") or ""), dict(opts.get("primary_options") or {})
    if name != "llamafile" or not opts.get("daemon_ensure"):
        return
    try:
        from claude_hooks import daemon_client
        daemon_client.embedding_ensure(timeout=30.0)
    except Exception:
        pass


# --------------------------------------------------------------------- #
# The index, and rebuilding it on another server of the same model
# --------------------------------------------------------------------- #
LEGACY_SPACE = "Xenova/bge-small-en-v1.5"
PARITY_MIN_COSINE = 0.995
PARITY_PROBES = (
    "Instruct: Given a search query, retrieve relevant passages from past "
    "conversations that answer the query\nQuery: rebuild a bcache superblock",
    "User: why did the deploy fail\n\nAssistant: verify_deploy found the "
    "episodic CLI linked to an out-of-tree checkout; relinking fixed it.",
)


def index_db_path() -> Path:
    env = os.environ.get("EPISODIC_MEMORY_DB_PATH")
    return Path(env) if env else superpowers_dir() / "conversation-index" / "db.sqlite"


def index_space(db_path: Optional[Path] = None) -> Optional[str]:
    """The vector space the index holds (``vector-space.ts``), or None
    when there is no index yet."""
    import sqlite3

    path = db_path or index_db_path()
    if not path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute("SELECT value FROM index_meta WHERE key = 'embedder'").fetchone()
        return row[0] if row and row[0] else LEGACY_SPACE
    except sqlite3.Error:
        return LEGACY_SPACE
    finally:
        conn.close()


def index_pending(db_path: Optional[Path] = None) -> Optional[int]:
    """Archived transcripts the index still owes exchanges to (``index_pending``,
    ``index-pending.ts``); 0 for an index from before the list, None without one."""
    import sqlite3

    path = db_path or index_db_path()
    if not path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return None
    try:
        return conn.execute("SELECT COUNT(*) FROM index_pending").fetchone()[0]
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


def space_of(spec: dict) -> str:
    """``spaceId()`` in embedder-config.ts, for a spec without a document prefix."""
    doc = spec.get("document_prefix") or ""
    return f"{spec['model']}|doc={doc}" if doc else spec["model"]


def _embed_once(spec: dict, texts: list[str], timeout: float = 120.0) -> list[list[float]]:
    import math
    import urllib.request

    kind = spec["kind"]
    if kind == "llamafile":
        body = {"content": texts}
    elif kind == "ollama":
        body = {"model": spec["model"], "input": texts, "truncate": True}
    else:
        body = {"model": spec["model"], "input": texts}
    headers = {"Content-Type": "application/json"}
    if spec.get("api_key"):
        headers["Authorization"] = f"Bearer {spec['api_key']}"
    req = urllib.request.Request(spec["url"], json.dumps(body).encode(), headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read())
    if kind == "llamafile":
        items = sorted(data if isinstance(data, list) else [data], key=lambda x: x.get("index", 0))
        vecs = [it["embedding"][0] if isinstance(it["embedding"][0], list) else it["embedding"]
                for it in items]
    elif kind == "ollama":
        vecs = data["embeddings"]
    else:
        vecs = [it["embedding"] for it in sorted(data["data"], key=lambda x: x.get("index", 0))]
    out = []
    for v in vecs:
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        out.append([x / n for x in v])
    return out


def parity(a: dict, b: dict) -> float:
    """Lowest cosine between two servers' vectors for the probe texts."""
    va, vb = _embed_once(a, list(PARITY_PROBES)), _embed_once(b, list(PARITY_PROBES))
    if any(len(x) != len(y) for x, y in zip(va, vb)):
        return 0.0
    return min(sum(p * q for p, q in zip(x, y)) for x, y in zip(va, vb))


def rebuild_spec(spec: dict, via_url: str, via_kind: str) -> dict:
    """The configured embedder, served by another server of the same model."""
    out = {k: v for k, v in spec.items() if not k.startswith("_") and k != "fallback"}
    out["kind"] = via_kind
    out["url"] = _ollama_embed_url(via_url) if via_kind == "ollama" else via_url
    if via_kind != "openai":
        out.pop("api_key", None)
    return out
