"""Shared helpers for the M12 v1 behavior-parity regression suite.

Three utility groups, all dependency-free (stdlib only) so the
parity suite imports cleanly in any environment the rest of the
test suite runs in.

1. **Deterministic chat stubs** — ``StableChat``, ``RoundAwareChat``,
   ``OkSynth`` etc. Return canned content keyed by call index or
   role, so a parity assertion can pin the exact ``final_answer``
   string a default-config run produces.

2. **Event capture** — ``capture_events()`` context manager that
   patches ``consultants.engine.events.emit`` to record every
   ``CouncilEvent`` instance the engine emits during the block.
   Pairs with ``assert_no_v2_optin_events(captured)`` to verify
   default-config runs don't leak v2 internals.

3. **Mocked clock + stall stubs** — ``MockedClock``,
   ``HangAfterTokensStream``, ``SlowButProgressingStream`` for the
   cohort-3 2h-session replay. Targets the M3 ``StallMonitor``
   directly (its ``time_source=`` kwarg is the clean injection
   point) so the replay verifies the actual detector logic, not a
   shim around it.

These helpers are private to the parity suite (``_parity_helpers``
filename prefix matches the project convention for test-only
internals). They are not imported by production code.
"""
from __future__ import annotations

import contextlib
import threading
from typing import Any, Callable, Iterable, Optional


# ====================================================================== #
# 1. Deterministic chat stubs
# ====================================================================== #


def _ok_resp(content: str, *,
             prompt_tokens: int = 10,
             completion_tokens: int = 20) -> dict:
    """Ollama / OpenAI-shape response envelope. Used by every stub
    so the recorder + usage-rollup paths see consistent token data.

    Pinning ``prompt_tokens`` + ``completion_tokens`` here means the
    per-tier baseline-pinned totals in cohort 1 are stable across
    runs — change them only when intentionally re-baselining.
    """
    return {
        "choices": [{
            "message": {"role": "assistant", "content": content},
        }],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        },
    }


class StableChat:
    """Returns the same content on every call. Used for roles where
    the assertion only cares that the role fired, not what it said
    (planner returning "1. step" for the smoke question, e.g.).
    """

    def __init__(self, content: str,
                 *,
                 prompt_tokens: int = 10,
                 completion_tokens: int = 20):
        self.content = content
        self._pt = prompt_tokens
        self._ct = completion_tokens
        self.calls: list[dict] = []

    def chat(self, payload: dict, *, think: bool = True) -> dict:
        self.calls.append(payload)
        return _ok_resp(
            self.content,
            prompt_tokens=self._pt,
            completion_tokens=self._ct,
        )


class RoundAwareChat:
    """Returns content varying by call index. Used for the
    researcher in the per-tier scenarios so each round produces a
    distinct token sequence + the recorder can verify the round
    cap is honoured.

    ``contents`` is a list of strings; call ``i`` returns
    ``contents[min(i, len-1)]`` (so a call past the end keeps
    returning the last content rather than IndexError).
    """

    def __init__(self, contents: list[str],
                 *,
                 prompt_tokens: int = 10,
                 completion_tokens: int = 20):
        self.contents = list(contents)
        self._pt = prompt_tokens
        self._ct = completion_tokens
        self.calls: list[dict] = []

    def chat(self, payload: dict, *, think: bool = True) -> dict:
        idx = len(self.calls)
        self.calls.append(payload)
        chosen = self.contents[min(idx, len(self.contents) - 1)]
        return _ok_resp(
            chosen,
            prompt_tokens=self._pt,
            completion_tokens=self._ct,
        )


# Canonical stub content. Imported by parity tests so a single
# place changes when the engine's prompt-parsing rules change.
STUB_PLAN = "1. investigate the question"
STUB_RESEARCH = "Research findings: nothing surprising on smoke."
STUB_CRITIC_READY = "DECISION: ready\nThe research covers the question."
STUB_CRITIC_NEEDS_MORE = (
    "DECISION: needs_more_research\nGap in coverage."
)
STUB_FINAL_ANSWER = "FINAL ANSWER: smoke complete."


def make_default_stubs() -> dict[str, Any]:
    """Build a stable stub-client dict for every role. Each role's
    stub returns the canonical text above. Tests that need to
    perturb one role's behavior (e.g. a critic that dissents)
    replace just that entry."""
    return {
        "planner":       StableChat(STUB_PLAN),
        "researcher":    RoundAwareChat([
            STUB_RESEARCH + " (round 1)",
            STUB_RESEARCH + " (round 2)",
            STUB_RESEARCH + " (round 3)",
        ]),
        "critic":        StableChat(STUB_CRITIC_READY),
        "synthesizer":   StableChat(STUB_FINAL_ANSWER),
        # Roles that are off by default still get a stub so a test
        # can flip them on without re-plumbing the dict.
        "tool_executor": StableChat(STUB_FINAL_ANSWER),
        "coder":         StableChat(STUB_FINAL_ANSWER),
        # M3 adversary refuter: default stub clears the answer
        # (REFUTATION: none) so an enabled-but-unperturbed run is inert.
        "adversary":     StableChat("REFUTATION: none"),
    }


# ====================================================================== #
# 2. Event capture (cohorts 1 + 2)
# ====================================================================== #


@contextlib.contextmanager
def capture_events():
    """Patch ``consultants.engine.events.emit`` to record every
    ``CouncilEvent`` instance for the duration of the block.

    Usage::

        with capture_events() as captured:
            graph.invoke(initial, config=cfg)
        assert not has_v2_optin_events(captured)

    Returns a list reference that fills as events arrive. The
    original ``emit`` is restored on exit even if the body raises.
    """
    from consultants.engine import events as _events

    original = _events.emit
    captured: list[Any] = []

    def _capturing_emit(event):  # noqa: ANN001 — CouncilEvent
        captured.append(event)
        # Forward to the original so any downstream consumer
        # (recorder, SSE bridge) still sees the event. Defensive
        # so the patch doesn't accidentally swallow live emits.
        try:
            return original(event)
        except Exception:
            return False

    _events.emit = _capturing_emit
    try:
        yield captured
    finally:
        _events.emit = original


# These are the v2 milestone events that should NEVER fire on a
# default-config (no opt-ins) run. Membership here means: the v2
# feature that emits this event is OFF by default, so seeing it in
# captured-events on a default run indicates a regression.
V2_OPT_IN_EVENT_KINDS: tuple[str, ...] = (
    "coder_failover",     # #111 / M10
    "runtime_mutation",   # M5 (inject + control) / M7 (xauto)
    "interrupt",          # M5 (HITL)
    "resumed",            # M5 (HITL)
    "deadline_warning",   # M3 (soft deadlines)
    "awaiting_adversary", # M2 (adversary checkpoint — default OFF)
    # Note: "node_started" / "node_finished" / "tool_call" /
    # "partial_synthesis" / "confidence_update" are emitted on
    # every run, default or not, so they're NOT in the opt-in set.
)


def assert_no_v2_optin_events(captured: list,
                              *,
                              allowed_kinds: tuple[str, ...] = (),
                              ) -> None:
    """Assert no v2 opt-in events fired during the captured block.

    ``allowed_kinds`` is the per-test exception list — e.g. an
    xauto test allows ``runtime_mutation`` because escalation
    legitimately uses it. Default config runs pass ``()``.

    Raises ``AssertionError`` with a descriptive message naming
    every offending event so a CI failure points straight at the
    leak.
    """
    offenders = [
        ev for ev in captured
        if getattr(ev, "kind", None) in V2_OPT_IN_EVENT_KINDS
        and getattr(ev, "kind", None) not in allowed_kinds
    ]
    if offenders:
        names = ", ".join(getattr(e, "kind", "?") for e in offenders[:8])
        raise AssertionError(
            f"default-config run emitted v2 opt-in events: {names} "
            f"({len(offenders)} total). This means a v2 feature "
            "leaked into the default path — investigate which "
            "milestone's gate regressed."
        )


def events_by_kind(captured: list) -> dict[str, list]:
    """Group captured events by their ``kind`` field. Convenience
    for tests that want to assert on the SHAPE of emissions, not
    just absence."""
    out: dict[str, list] = {}
    for ev in captured:
        kind = getattr(ev, "kind", "?")
        out.setdefault(kind, []).append(ev)
    return out


# ====================================================================== #
# 3. Mocked clock + stall stubs (cohort 3)
# ====================================================================== #


class MockedClock:
    """Drop-in replacement for ``time.monotonic`` whose return
    advances explicitly.

    Pass ``clock.now`` as the ``time_source=`` kwarg to a
    ``StallMonitor`` (and ``clock.sleep`` as the ``sleep_fn=``).
    Tests then call ``clock.advance(seconds)`` to move the clock
    forward; reads through ``now()`` return the current value.

    Thread-safe — the stall monitor's watchdog runs in a separate
    thread and reads through ``now()`` from there.
    """

    def __init__(self, t0: float = 0.0):
        self._lock = threading.Lock()
        self._t = t0
        self.sleeps: list[float] = []

    def now(self) -> float:
        with self._lock:
            return self._t

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._t += seconds

    def sleep(self, seconds: float) -> None:
        """``sleep_fn``-compatible — records the requested duration
        AND advances the clock by it. Tests can read ``self.sleeps``
        to verify the stall monitor's backoff/check-interval cadence."""
        self.sleeps.append(seconds)
        self.advance(seconds)


class HangAfterTokensStream:
    """Stub for ``chat_streamed`` that emits N tokens via the
    controller's ``mark_token`` callback then blocks until cancelled
    or until the test fixture advances the clock past the stall
    threshold.

    Mirrors the 2026-05-15 audit-session pathology where gemini
    lanes emitted a few tokens then went silent for 30+ minutes
    on the cloud. The stall detector should:

    1. Recognise the gap (last_token_ts hasn't moved past the
       threshold).
    2. Cancel via the controller's cancellation signal.
    3. Retry once.
    4. Tombstone after the retry also hangs.

    ``hang_check_interval_s`` is the wall budget for each
    busy-wait iteration before re-checking ``cancel_check()`` —
    keep it small so the test's mocked-clock advance interleaves
    cleanly with the watchdog's check_interval.
    """

    def __init__(self, tokens: int = 3,
                 *,
                 clock: MockedClock,
                 hang_check_interval_s: float = 0.5):
        self.tokens = tokens
        self.clock = clock
        self.hang_check_interval_s = hang_check_interval_s
        # Per-call counters so a test can assert how many times
        # the stub was invoked across retries.
        self.calls = 0
        self.cancelled_after_n_tokens: list[int] = []

    def __call__(self, payload: dict,
                  on_token: Callable[[str], None],
                  cancel_check: Callable[[], bool]) -> dict:
        self.calls += 1
        # Emit the configured number of tokens, advancing the
        # mocked clock by 1s per token (well under stall_threshold
        # so they look like normal stream cadence).
        for i in range(self.tokens):
            if cancel_check():
                self.cancelled_after_n_tokens.append(i)
                raise _StallCancelled()
            on_token(f"tok{i}")
            self.clock.advance(1.0)

        # Hang: spin until cancelled OR a sentinel iteration cap
        # (defence against the test forgetting to advance the
        # clock — without this we'd block forever).
        for _ in range(10_000):
            if cancel_check():
                self.cancelled_after_n_tokens.append(self.tokens)
                raise _StallCancelled()
            self.clock.advance(self.hang_check_interval_s)
        # Sentinel exit — this shouldn't normally fire.
        raise AssertionError(
            "HangAfterTokensStream iteration cap hit — fixture "
            "likely forgot to advance the clock past the stall "
            "threshold"
        )


class SlowButProgressingStream:
    """Stub for ``chat_streamed`` that emits one token every
    ``token_interval_s`` mocked seconds for ``duration_s`` total.

    The stall detector should NOT kill this — gaps stay under the
    threshold throughout. Validates the M3 design goal of "don't
    punish legitimate deep thinking" (the user's 2026-05-16
    constraint).
    """

    def __init__(self, token_interval_s: float = 60.0,
                 duration_s: float = 1200.0,  # 20 min
                 *,
                 clock: MockedClock):
        self.token_interval_s = token_interval_s
        self.duration_s = duration_s
        self.clock = clock
        self.calls = 0
        self.tokens_emitted: int = 0

    def __call__(self, payload: dict,
                  on_token: Callable[[str], None],
                  cancel_check: Callable[[], bool]) -> dict:
        self.calls += 1
        deadline = self.clock.now() + self.duration_s
        i = 0
        while self.clock.now() < deadline:
            if cancel_check():
                raise _StallCancelled()
            on_token(f"tok{i}")
            i += 1
            self.tokens_emitted = i
            self.clock.advance(self.token_interval_s)
        # Normal completion — return an OkResp envelope.
        return _ok_resp(
            f"slow-progress result ({i} tokens)",
            completion_tokens=i,
        )


class _StallCancelled(Exception):
    """Internal exception used by the stall stubs to indicate the
    controller signalled cancellation. ``StallMonitor`` treats any
    exception from ``chat_streamed_fn`` as a failed attempt."""


# ====================================================================== #
# 4. Convenience for cohort 1 — build a minimal GraphDeps
# ====================================================================== #


def build_default_deps(*, enabled_roles: Iterable[str],
                       stubs: Optional[dict[str, Any]] = None,
                       cwd: str = "/tmp",
                       extra_models_by_role: Optional[
                           dict[str, list[str]]] = None,
                       synthesizer_fallback_models: Optional[
                           list[str]] = None,
                       ):
    """Build a ``GraphDeps`` with default v2 opt-ins all OFF.

    Tests in cohort 1 use this to spin up a council quickly. Cohort 2
    tests build their own deps to exercise specific feature gates.

    Lazy-imports ``consultants.engine.graph.GraphDeps`` so a test
    env without langgraph can still import this helper module.
    """
    from consultants.engine.graph import GraphDeps  # lazy

    stubs = stubs or make_default_stubs()
    enabled = tuple(enabled_roles)
    return GraphDeps(
        chat_clients={r: stubs[r] for r in enabled},
        models={r: "stub-model:test" for r in enabled},
        enabled_roles=enabled,
        cwd=cwd,
        tool_executor=lambda *a, **kw: "",
        tool_specs=[],
        grounding_msgs=[],
        disable_cache=True,
        extra_models_by_role=dict(extra_models_by_role or {}),
        synthesizer_fallback_models=list(
            synthesizer_fallback_models or []),
        # v2 opt-ins explicitly off:
        store=None,
        # coder_chat_clients_by_model / coder_routes_by_language /
        # coder_default_route all default to {} / None in the
        # dataclass — leave them as defaults to exercise the
        # back-compat path.
    )


__all__ = [
    "HangAfterTokensStream",
    "MockedClock",
    "RoundAwareChat",
    "STUB_CRITIC_NEEDS_MORE",
    "STUB_CRITIC_READY",
    "STUB_FINAL_ANSWER",
    "STUB_PLAN",
    "STUB_RESEARCH",
    "SlowButProgressingStream",
    "StableChat",
    "V2_OPT_IN_EVENT_KINDS",
    "_StallCancelled",
    "assert_no_v2_optin_events",
    "build_default_deps",
    "capture_events",
    "events_by_kind",
    "make_default_stubs",
]
