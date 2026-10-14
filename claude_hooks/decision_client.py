"""Client for decision models: TypeSafe's Jev and ``/v1/systemone`` servers.

A decision model writes no text. It reads a **state** and answers typed
questions with probabilities:

``noul``    is this true? → ``noul`` = p(yes), no ``confidence`` field
``choice``  which option? → ``choice``, ``probabilities``, ``confidence``
``score``   which level of an ordered rubric? → ``score`` (probability-
            weighted, may fall between levels), ``probabilities``, ``confidence``

Two dialects serve the same API:

- **TypeSafe** (``https://api.typesafe.ai/v1``, model ``jev-latest``),
  Bearer key required.
- **Ollama / xOllama** ≥ 0.35 (``<root>/v1/systemone``), local GGUF decision
  models (``capabilities`` include ``decision``: tev1, nimble, …), no key.

Ported from Cerebriline (``sdk/packages/core/src/extensions/tools/jev.ts``,
fork commits ``c498e9cf4`` and ``f535f2035``); the Ollama deltas are recorded
in the fork's ``docs/knowledge-base/jev/ollama.md``:

- The server root, its ``/v1`` and the full ``/v1/systemone`` path all name one
  endpoint. A custom endpoint is asked ``GET /api/version`` once; a string
  ``version`` means Ollama. A probe that cannot connect is not cached.
- Ollama caps the body at 64 KiB and the prompt at the model's **loaded**
  window, which it never truncates (``400 prompt 0 has N tokens; expected
  1–M``). The state — never the questions — is cut to fit, retried, and the
  size that fitted is remembered per server and model.
- Ollama answers a choice with one letter, so more than 26 options fail
  before sending.
- Only 429 and 529 are retried, inside one deadline for the whole call.

The key is read from ``api_key``, else ``key_file``, else
``TYPESAFE_API_KEY``, and is sent only to the endpoint it was configured
for. It is never logged.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger("claude_hooks.decision_client")

DEFAULT_BASE_URL = "https://api.typesafe.ai/v1"
DEFAULT_MODEL = "jev-latest"
DEFAULT_TIMEOUT_S = 15.0
#: An answer at or above it counts as confident (fork ``JEV_DEFAULT_FLOOR``).
DEFAULT_FLOOR = 0.6
DEFAULT_HIGH_STAKES_FLOOR = 0.85
#: Per-part bound on text the harness puts into a state.
MAX_PART_CHARS = 12_000
OLLAMA_MAX_BODY_BYTES = 64 * 1024
OLLAMA_MAX_OPTIONS = 26
RETRY_DELAYS_S = (0.4, 1.2)
DIALECTS = ("typesafe", "ollama")

_OVERFLOW = re.compile(r"prompt \d+ has (\d+) tokens; expected 1\s*[–-]\s*(\d+)")

#: ``transport(method, url, body, headers, timeout) -> (status, body)``.
Transport = Callable[[str, str, Optional[bytes], dict, float], tuple[int, bytes]]


class DecisionError(RuntimeError):
    """A call that did not produce answers. ``status`` is the HTTP status
    when the server answered."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


@dataclass
class Endpoint:
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    api_key: Optional[str] = field(default=None, repr=False)
    key_file: Optional[str] = None
    timeout_s: float = DEFAULT_TIMEOUT_S
    #: ``typesafe`` / ``ollama``; ``None`` asks the server.
    dialect: Optional[str] = None
    floor: float = DEFAULT_FLOOR
    high_stakes_floor: float = DEFAULT_HIGH_STAKES_FLOOR

    def key(self) -> Optional[str]:
        if self.api_key:
            return self.api_key
        if self.key_file:
            try:
                text = Path(self.key_file).expanduser().read_text().strip()
            except OSError:
                log.warning("decision: key file %s is not readable",
                            self.key_file)
                return None
            return text or None
        if not routes(self.base_url).custom:
            return os.environ.get("TYPESAFE_API_KEY") or None
        return None


@dataclass(frozen=True)
class Routes:
    systemone: str
    models: str
    root: str
    custom: bool


@dataclass
class Response:
    model: str
    answers: dict[str, dict]
    usage: dict = field(default_factory=dict)
    duration_ms: int = 0
    dialect: str = "typesafe"


def routes(base_url: Optional[str]) -> Routes:
    """Where to call, from the root, the ``/v1`` base or the full path."""
    typed = re.sub(r"/systemone$", "", (base_url or "").strip().rstrip("/"),
                   flags=re.I).rstrip("/")
    custom = typed not in ("", DEFAULT_BASE_URL)
    api = typed if custom else DEFAULT_BASE_URL
    if not re.search(r"/v1$", api, re.I):
        api = f"{api}/v1"
    return Routes(systemone=f"{api}/systemone", models=f"{api}/models",
                  root=re.sub(r"/v1$", "", api, flags=re.I), custom=custom)


_lock = threading.Lock()
_dialects: dict[str, str] = {}
_state_ceilings: dict[str, int] = {}


def reset_caches() -> None:
    """Forget learned dialects and state sizes (for tests)."""
    with _lock:
        _dialects.clear()
        _state_ceilings.clear()


def _urllib_transport(method: str, url: str, body: Optional[bytes],
                      headers: dict, timeout: float) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def detect_dialect(endpoint: Endpoint, *,
                   transport: Optional[Transport] = None) -> str:
    if endpoint.dialect in DIALECTS:
        return endpoint.dialect
    r = routes(endpoint.base_url)
    if not r.custom:
        return "typesafe"
    with _lock:
        if r.root in _dialects:
            return _dialects[r.root]
    try:
        status, body = (transport or _urllib_transport)(
            "GET", f"{r.root}/api/version", None, {}, 3.0)
    except Exception:
        return "typesafe"   # unreachable: not cached, asked again next time
    dialect = "typesafe"
    if status == 200:
        try:
            if isinstance(json.loads(body or b"{}").get("version"), str):
                dialect = "ollama"
        except (ValueError, AttributeError):
            pass
    with _lock:
        _dialects[r.root] = dialect
    return dialect


def bound_text(text: Any, limit: int = MAX_PART_CHARS) -> str:
    """``text`` cut to ``limit`` chars, head and tail kept."""
    s = "" if text is None else str(text)
    if len(s) <= limit:
        return s
    keep = max(1, (limit - 40) // 2)
    return f"{s[:keep]}\n…[{len(s) - 2 * keep} chars cut]…\n{s[-keep:]}"


def _shrink(state: Any, factor: float) -> Any:
    if isinstance(state, str):
        return bound_text(state, max(200, int(len(state) * factor)))
    if isinstance(state, list):
        return [_shrink(v, factor) for v in state]
    if isinstance(state, dict):
        return {k: _shrink(v, factor) for k, v in state.items()}
    return state


def _size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def _body(model: str, state: Any, questions: dict, dialect: str,
          ceiling: Optional[int]) -> tuple[bytes, int]:
    if dialect == "ollama" and ceiling is not None:
        for _ in range(6):
            if _size(state) <= ceiling:
                break
            state = _shrink(state, ceiling / _size(state) * 0.95)
    payload = {"state": state, "model": model, "questions": questions}
    if dialect != "ollama":
        return json.dumps(payload, ensure_ascii=False).encode(), _size(state)
    for _ in range(6):
        size = _size(payload)
        if size <= OLLAMA_MAX_BODY_BYTES:
            break
        state = _shrink(state, OLLAMA_MAX_BODY_BYTES / size * 0.9)
        payload = {"state": state, "model": model, "questions": questions}
    if _size(payload) > OLLAMA_MAX_BODY_BYTES:
        raise DecisionError("the questions alone exceed Ollama's 64 KiB "
                            "request limit; ask fewer or shorter questions")
    return json.dumps(payload, ensure_ascii=False).encode(), _size(state)


def _check_options(questions: dict, dialect: str) -> None:
    if dialect != "ollama":
        return
    for name, q in questions.items():
        if q.get("type") == "choice" and len(q.get("criteria") or {}) > OLLAMA_MAX_OPTIONS:
            raise DecisionError(
                f"choice {name!r} has {len(q['criteria'])} options; Ollama "
                f"takes at most {OLLAMA_MAX_OPTIONS}")


def _error_text(detail: str) -> str:
    """The message inside an ``{"error": …}`` / ``{"detail": …}`` body —
    parsed, because the raw JSON may escape the en dash in Ollama's
    overflow message (``\u2013``)."""
    try:
        parsed = json.loads(detail)
        return str(parsed.get("error") or parsed.get("detail") or detail)
    except (ValueError, AttributeError):
        return detail


def _describe(status: int, detail: str, *, dialect: str, model: str) -> str:
    text = _error_text(detail)[:300]
    if status == 401:
        return "decision endpoint refused the key (401)"
    if status == 404 and dialect == "ollama":
        return f"model {model!r} is not on this server (404): pull it first"
    if status == 413:
        return "request too large for the decision endpoint (413)"
    if status == 422:
        return f"decision endpoint rejected the request (422): {text}"
    return f"decision endpoint answered {status}: {text}"


def evaluate(endpoint: Endpoint, state: Any, questions: dict[str, dict], *,
             transport: Optional[Transport] = None,
             sleep: Callable[[float], None] = time.sleep) -> Response:
    """Ask ``questions`` of ``state``. Raises :class:`DecisionError` on any
    failure, within ``endpoint.timeout_s`` for the whole call."""
    if not questions:
        raise DecisionError("no questions")
    send = transport or _urllib_transport
    deadline = time.monotonic() + float(endpoint.timeout_s)
    r = routes(endpoint.base_url)
    model = endpoint.model or DEFAULT_MODEL
    dialect = detect_dialect(endpoint, transport=transport)
    _check_options(questions, dialect)
    ceiling_key = f"{r.root} {model}"
    with _lock:
        ceiling = _state_ceilings.get(ceiling_key)
    body, state_bytes = _body(model, state, questions, dialect, ceiling)
    headers = {"Content-Type": "application/json"}
    key = endpoint.key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    elif not r.custom:
        raise DecisionError("TypeSafe's Jev needs an API key "
                            "(api_key, key_file or TYPESAFE_API_KEY)")
    overflows = 0
    attempt = 0
    t0 = time.monotonic()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DecisionError(f"no answer within {endpoint.timeout_s:g} s")
        try:
            status, raw = send("POST", r.systemone, body, headers, remaining)
        except Exception as e:
            raise DecisionError(f"decision endpoint unreachable: "
                                f"{type(e).__name__}: {e}") from None
        if status == 200:
            try:
                payload = json.loads(raw)
            except ValueError:
                raise DecisionError("decision endpoint answered non-JSON") from None
            answers = payload.get("answers") if isinstance(payload, dict) else None
            if not isinstance(answers, dict):
                raise DecisionError("decision endpoint answered without "
                                    "an `answers` map")
            return Response(model=str(payload.get("model") or model),
                            answers=answers, usage=payload.get("usage") or {},
                            duration_ms=int((time.monotonic() - t0) * 1000),
                            dialect=dialect)
        detail = (raw or b"")[:400].decode("utf-8", errors="replace")
        if status in (429, 529) and attempt < len(RETRY_DELAYS_S):
            delay = RETRY_DELAYS_S[attempt]
            attempt += 1
            if time.monotonic() + delay >= deadline:
                raise DecisionError(f"decision endpoint busy ({status}) "
                                    f"past the deadline", status)
            sleep(delay)
            continue
        m = _OVERFLOW.search(_error_text(detail)) if dialect == "ollama" and status == 400 else None
        if m and overflows < 3:
            have, limit = int(m.group(1)), int(m.group(2))
            new_ceiling = int(state_bytes * (limit / have) * 0.8)
            if have > limit and new_ceiling >= 400:
                overflows += 1
                with _lock:
                    _state_ceilings[ceiling_key] = new_ceiling
                body, state_bytes = _body(model, state, questions, dialect,
                                          new_ceiling)
                continue
        raise DecisionError(_describe(status, detail, dialect=dialect,
                                      model=model), status)


def list_models(endpoint: Endpoint, *,
                transport: Optional[Transport] = None) -> list[str]:
    """Decision models the endpoint serves: Ollama's ``/api/tags`` entries
    whose ``capabilities`` include ``decision``, else ``/v1/models``."""
    send = transport or _urllib_transport
    r = routes(endpoint.base_url)
    if detect_dialect(endpoint, transport=transport) == "ollama":
        status, raw = send("GET", f"{r.root}/api/tags", None, {}, 10.0)
        if status != 200:
            raise DecisionError(f"/api/tags answered {status}", status)
        return sorted({m.get("name") or m.get("model")
                       for m in (json.loads(raw).get("models") or [])
                       if "decision" in (m.get("capabilities") or [])} - {None})
    headers = {}
    key = endpoint.key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    status, raw = send("GET", r.models, None, headers, 10.0)
    if status != 200:
        raise DecisionError(_describe(status, raw.decode(errors="replace"),
                                      dialect="typesafe", model=""), status)
    data = json.loads(raw)
    rows = data.get("models") or data.get("data") or []
    return [str(m.get("name") or m.get("id")) for m in rows
            if m.get("name") or m.get("id")]


# --- questions and answers ---------------------------------------------- #

def noul(instructions: Any, criteria: Optional[dict] = None) -> dict:
    q = {"type": "noul", "instructions": instructions}
    if criteria:
        q["criteria"] = criteria
    return q


def choice(instructions: Any, options: dict[str, Any]) -> dict:
    return {"type": "choice", "instructions": instructions,
            "criteria": dict(options)}


def score(instructions: Any, levels: list[Any]) -> dict:
    """``levels`` low end first — an array; both dialects refuse a map."""
    return {"type": "score", "instructions": instructions,
            "criteria": list(levels)}


def confidence(answer: dict) -> float:
    """One scale for all three kinds: a choice's and score's
    ``confidence``; a noul's two-outcome ``|2p − 1|``."""
    if not isinstance(answer, dict):
        return 0.0
    if answer.get("type") == "noul" or ("noul" in answer
                                        and "confidence" not in answer):
        p = _clamp(answer.get("noul"))
        return abs(2 * p - 1)
    return _clamp(answer.get("confidence"))


def confident(answer: dict, floor: float = DEFAULT_FLOOR) -> bool:
    return confidence(answer) >= floor


def _clamp(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


__all__ = ["Endpoint", "Response", "Routes", "DecisionError", "routes",
           "detect_dialect", "evaluate", "list_models", "reset_caches",
           "noul", "choice", "score", "confidence", "confident",
           "bound_text", "DEFAULT_BASE_URL", "DEFAULT_MODEL", "DEFAULT_FLOOR",
           "DEFAULT_HIGH_STAKES_FLOOR", "OLLAMA_MAX_OPTIONS",
           "OLLAMA_MAX_BODY_BYTES"]
