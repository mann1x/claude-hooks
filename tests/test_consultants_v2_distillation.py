"""M14 — :mod:`consultants.engine.distillation` unit tests.

Covers the Caliber-style distillation that fires when the
consultants-daemon's :class:`StoreReaperThread` finds expiring
research entries. The critical invariant — *originals are only
deleted after a successful summary write* — lives in the reaper
(see :file:`test_consultants_v2_store_reaper.py`); these tests
exercise the LLM-side surface:

- ``project_id_from_cwd`` derivation (deterministic, resolves
  relative paths, collision-resistant).
- ``build_distillation_prompt`` shape (covers every finding,
  carries sid + cwd + question hints, truncates at the cost cap).
- ``Distiller.distill_session`` fallback chain (primary →
  fallbacks; raises :class:`DistillationFailed` only when *all*
  models fail; raises early when disabled / empty / no models).
- ``write_distilled_summary`` writes to the
  ``("project", project_id)`` namespace with the M14 metadata
  shape.

LangGraph is **not** imported here — the distillation module only
touches the consultants store through duck-typed ``store.put`` so
these tests run cleanly in both envs.
"""
from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from consultants.config import StoreDistillationConfig
from consultants.engine.distillation import (
    DISTILLATION_SYSTEM_PROMPT,
    Distiller,
    DistillationFailed,
    build_distillation_prompt,
    project_id_from_cwd,
    write_distilled_summary,
)


# ============================================================== #
# Fakes
# ============================================================== #


@dataclass
class _FakeRow:
    """Stand-in for :class:`claude_hooks.providers._content_hash.ExpiringRow`.

    The distillation module reads only ``content`` and ``metadata``
    off rows, so a lightweight dataclass is enough.
    """
    content: str
    metadata: dict = field(default_factory=dict)
    content_hash: bytes = b""
    expires_at: str = ""


class _RecordingChatClient:
    """Captures the ``chat`` payload + returns a configurable response.

    A list of behaviours feeds each call: ``"ok"`` returns the
    canned content, ``"empty"`` returns an empty-content shape, and
    a ``Callable`` is invoked to raise / return.
    """

    def __init__(self, behaviour: Any = "ok", content: str = "DISTILLED-SUMMARY") -> None:
        self.behaviour = behaviour
        self.content = content
        self.calls: list[dict] = []

    def chat(self, payload: dict) -> dict:
        self.calls.append(payload)
        if callable(self.behaviour):
            return self.behaviour(payload)
        if self.behaviour == "empty":
            return {"choices": [{"message": {"content": ""}}]}
        if self.behaviour == "ok":
            return {"choices": [{"message": {"content": self.content}}]}
        raise RuntimeError(f"unknown behaviour: {self.behaviour!r}")


class _FactorySpy:
    """Test double for :func:`make_agent_chat_client`. Records the
    (model, base_url) pairs it was called with and hands back the
    next client from a queue.
    """

    def __init__(self, clients: list[Any]) -> None:
        self._queue = list(clients)
        self.calls: list[tuple[str, str]] = []

    def __call__(self, model: str, base_url: str) -> Any:
        self.calls.append((model, base_url))
        if not self._queue:
            raise RuntimeError("FactorySpy exhausted; test set up wrong")
        return self._queue.pop(0)


class _RecordingStore:
    """Tiny store-shaped recorder for ``write_distilled_summary``."""

    def __init__(self) -> None:
        self.puts: list[tuple[tuple[str, ...], str, dict, Any]] = []

    def put(self, ns: tuple[str, ...], key: str, value: dict, **kwargs: Any) -> None:
        # Replicate the BaseStore signature without binding to it.
        self.puts.append((ns, key, value, kwargs))


def _dcfg(
    *,
    enabled: bool = True,
    model: str = "gemma4:31b-cloud",
    fallback_models: tuple[str, ...] = ("glm-5.1:cloud",),
    max_session_entries: int = 50,
) -> StoreDistillationConfig:
    """Build a StoreDistillationConfig with terse overrides."""
    return StoreDistillationConfig(
        enabled=enabled,
        model=model,
        fallback_models=fallback_models,
        sweep_interval_seconds=3600.0,
        min_entries_per_distillation=3,
        max_session_entries=max_session_entries,
    )


def _row(text: str, **meta: Any) -> _FakeRow:
    return _FakeRow(
        content=text,
        metadata=dict(meta),
        content_hash=hashlib.sha256(text.encode()).digest(),
        expires_at="2026-05-01T00:00:00+00:00",
    )


# ============================================================== #
# project_id_from_cwd
# ============================================================== #


class TestProjectIdFromCwd(unittest.TestCase):

    def test_project_id_from_cwd_is_deterministic(self) -> None:
        """Same cwd → same id, byte-for-byte."""
        a = project_id_from_cwd("/srv/dev-disk-by-label-opt/dev/claude-hooks")
        b = project_id_from_cwd("/srv/dev-disk-by-label-opt/dev/claude-hooks")
        self.assertEqual(a, b)
        self.assertEqual(len(a), 12)
        # All hex.
        int(a, 16)

    def test_project_id_from_cwd_collision_resistant(self) -> None:
        """Different cwds → different ids. With sha256 + 12 hex chars
        the collision space is 2^48; two hand-picked paths must not
        collide."""
        a = project_id_from_cwd("/proj/alpha")
        b = project_id_from_cwd("/proj/beta")
        self.assertNotEqual(a, b)

    def test_project_id_from_cwd_resolves_relative_paths(self) -> None:
        """``./.`` ↔ absolute cwd → same id. The reaper sees row
        metadata that may carry either shape (M8 doesn't normalize)."""
        with tempfile.TemporaryDirectory() as tmp:
            here = Path(tmp).resolve()
            saved = os.getcwd()
            try:
                os.chdir(here)
                a = project_id_from_cwd(".")
                b = project_id_from_cwd(str(here))
                self.assertEqual(a, b)
            finally:
                os.chdir(saved)

    def test_project_id_from_cwd_handles_none_and_empty(self) -> None:
        """None / empty → falls back to the sentinel hash. Doesn't
        crash on a row with missing metadata."""
        a = project_id_from_cwd(None)
        b = project_id_from_cwd("")
        # Both should produce the sentinel id.
        self.assertEqual(a, b)
        self.assertEqual(len(a), 12)


# ============================================================== #
# build_distillation_prompt
# ============================================================== #


class TestBuildDistillationPrompt(unittest.TestCase):

    def test_prompt_includes_system_rubric(self) -> None:
        msgs = build_distillation_prompt(
            "sid-1", [_row("hello"), _row("world"), _row("foo")],
        )
        self.assertGreaterEqual(len(msgs), 2)
        self.assertEqual(msgs[0]["role"], "system")
        self.assertEqual(msgs[0]["content"], DISTILLATION_SYSTEM_PROMPT)

    def test_prompt_includes_all_findings(self) -> None:
        """Every row's text shows up in the user message body."""
        rows = [_row("alpha finding"), _row("beta finding"), _row("gamma finding")]
        msgs = build_distillation_prompt("sid-x", rows)
        user = msgs[1]["content"]
        self.assertIn("alpha finding", user)
        self.assertIn("beta finding", user)
        self.assertIn("gamma finding", user)
        self.assertIn("Findings (N=3)", user)

    def test_prompt_includes_sid(self) -> None:
        msgs = build_distillation_prompt("csl-2026-05-18-abc", [_row("x")])
        self.assertIn("csl-2026-05-18-abc", msgs[1]["content"])

    def test_prompt_includes_cwd_and_question_hints(self) -> None:
        msgs = build_distillation_prompt(
            "sid-9", [_row("x")],
            cwd_hint="/proj/zeta",
            question_hint="How does the foo handle the bar?",
        )
        body = msgs[1]["content"]
        self.assertIn("/proj/zeta", body)
        self.assertIn("How does the foo handle the bar?", body)

    def test_prompt_truncates_at_max_session_entries(self) -> None:
        """Cost gate: 100 rows with cap=5 → only 5 in the prompt."""
        rows = [_row(f"finding-{i}") for i in range(100)]
        msgs = build_distillation_prompt("sid-trunc", rows, max_session_entries=5)
        body = msgs[1]["content"]
        self.assertIn("Findings (N=5)", body)
        self.assertIn("finding-0", body)
        self.assertIn("finding-4", body)
        self.assertNotIn("finding-50", body)

    def test_prompt_carries_lane_and_plan_item_headers(self) -> None:
        """Per-finding header surfaces lane_idx + plan_item so the LLM
        can attribute claims to lanes when synthesizing."""
        rows = [
            _row("a fact", lane_idx=0, plan_item="audit recall path"),
            _row("b fact", lane_idx=1, plan_item="audit record path"),
            _row("c fact", lane_idx=2, plan_item="audit dedup"),
        ]
        body = build_distillation_prompt("sid-h", rows)[1]["content"]
        self.assertIn("(lane 0)", body)
        self.assertIn("(lane 1)", body)
        self.assertIn("(lane 2)", body)
        self.assertIn("audit recall path", body)

    def test_prompt_includes_date_range_when_metadata_has_created_at(self) -> None:
        """Date range header anchors the LLM when newer findings
        contradict older ones."""
        rows = [
            _row("old", created_at="2026-05-01T00:00:00+00:00"),
            _row("mid", created_at="2026-05-10T00:00:00+00:00"),
            _row("new", created_at="2026-05-18T00:00:00+00:00"),
        ]
        body = build_distillation_prompt("sid-d", rows)[1]["content"]
        self.assertIn("2026-05-01T00:00:00+00:00", body)
        self.assertIn("2026-05-18T00:00:00+00:00", body)
        self.assertIn("Date range:", body)

    def test_prompt_empty_rows_returns_empty(self) -> None:
        """No rows → no prompt — caller (Distiller) treats this as
        DistillationFailed."""
        self.assertEqual(build_distillation_prompt("sid-z", []), [])


# ============================================================== #
# Distiller — fallback chain and error semantics
# ============================================================== #


class TestDistiller(unittest.TestCase):

    def _rows(self, n: int = 3) -> list[_FakeRow]:
        return [_row(f"finding-{i}", lane_idx=i) for i in range(n)]

    # ---- happy paths ---- #

    def test_distill_session_returns_summary_on_first_model_success(self) -> None:
        client = _RecordingChatClient(behaviour="ok", content="THE SUMMARY")
        spy = _FactorySpy([client])
        d = Distiller(_dcfg(), "http://ollama:11434", chat_client_factory=spy)
        out = d.distill_session("sid-1", self._rows())
        self.assertEqual(out, "THE SUMMARY")
        # Only the primary was instantiated.
        self.assertEqual([m for m, _ in spy.calls], ["gemma4:31b-cloud"])
        # Payload carries our messages + the model name.
        self.assertEqual(client.calls[0]["model"], "gemma4:31b-cloud")
        self.assertEqual(client.calls[0]["messages"][0]["role"], "system")

    def test_distill_session_fallback_chain_tries_each_model(self) -> None:
        """Primary raises → fallback runs → fallback returns content.

        Both factory calls AND both client invocations are recorded.
        """
        primary = _RecordingChatClient(
            behaviour=lambda _p: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        fb = _RecordingChatClient(behaviour="ok", content="FALLBACK SUMMARY")
        spy = _FactorySpy([primary, fb])
        d = Distiller(_dcfg(), "http://o:1", chat_client_factory=spy)
        out = d.distill_session("sid-2", self._rows())
        self.assertEqual(out, "FALLBACK SUMMARY")
        self.assertEqual(
            [m for m, _ in spy.calls],
            ["gemma4:31b-cloud", "glm-5.1:cloud"],
        )
        self.assertEqual(len(primary.calls), 1)
        self.assertEqual(len(fb.calls), 1)

    def test_distill_session_empty_content_triggers_fallback(self) -> None:
        """An OpenAI 200 response with empty content counts as failure
        — fall through to the next model."""
        primary = _RecordingChatClient(behaviour="empty")
        fb = _RecordingChatClient(behaviour="ok", content="REAL")
        spy = _FactorySpy([primary, fb])
        d = Distiller(_dcfg(), "http://o:1", chat_client_factory=spy)
        out = d.distill_session("sid-3", self._rows())
        self.assertEqual(out, "REAL")
        self.assertEqual(
            [m for m, _ in spy.calls],
            ["gemma4:31b-cloud", "glm-5.1:cloud"],
        )

    def test_distill_session_dedups_duplicate_models_in_chain(self) -> None:
        """If a user mis-configures ``fallback_models`` with the same
        entry as primary, we don't waste a second LLM call on it."""
        client = _RecordingChatClient(behaviour="ok", content="OK")
        spy = _FactorySpy([client])
        cfg = _dcfg(model="m1", fallback_models=("m1", "m1"))
        d = Distiller(cfg, "http://o:1", chat_client_factory=spy)
        out = d.distill_session("sid-d", self._rows())
        self.assertEqual(out, "OK")
        self.assertEqual([m for m, _ in spy.calls], ["m1"])

    # ---- error paths — the critical invariants ---- #

    def test_distillation_disabled_raises_failed_does_not_call_llm(self) -> None:
        """``enabled = False`` short-circuits to DistillationFailed
        before any factory / client call. This is what lets the
        reaper safely set ``cfg.store.distillation.enabled = False``
        as a per-tick kill switch."""
        spy = _FactorySpy([])  # exhausted: any call would explode.
        d = Distiller(_dcfg(enabled=False), "http://o:1", chat_client_factory=spy)
        with self.assertRaises(DistillationFailed):
            d.distill_session("sid-x", self._rows())
        self.assertEqual(spy.calls, [])

    def test_distillation_all_models_fail_raises(self) -> None:
        """Both primary and fallback raise → DistillationFailed
        propagates. *Critical invariant* — the reaper keys "keep
        originals" off this exception."""
        primary = _RecordingChatClient(
            behaviour=lambda _p: (_ for _ in ()).throw(RuntimeError("p")),
        )
        fb = _RecordingChatClient(
            behaviour=lambda _p: (_ for _ in ()).throw(RuntimeError("f")),
        )
        spy = _FactorySpy([primary, fb])
        d = Distiller(_dcfg(), "http://o:1", chat_client_factory=spy)
        with self.assertRaises(DistillationFailed):
            d.distill_session("sid-y", self._rows())
        # Confirm both were tried before the give-up.
        self.assertEqual(
            [m for m, _ in spy.calls],
            ["gemma4:31b-cloud", "glm-5.1:cloud"],
        )

    def test_distillation_empty_rows_raises(self) -> None:
        d = Distiller(_dcfg(), "http://o:1", chat_client_factory=_FactorySpy([]))
        with self.assertRaises(DistillationFailed):
            d.distill_session("sid-r", [])

    def test_distillation_no_models_configured_raises(self) -> None:
        """An admin who blanked out both primary + fallback gets an
        error rather than a silent "delete the originals"."""
        cfg = _dcfg(model="", fallback_models=())
        d = Distiller(cfg, "http://o:1", chat_client_factory=_FactorySpy([]))
        with self.assertRaises(DistillationFailed):
            d.distill_session("sid-q", self._rows())


# ============================================================== #
# write_distilled_summary — project-namespace write
# ============================================================== #


class TestWriteDistilledSummary(unittest.TestCase):

    def test_summary_written_to_project_namespace(self) -> None:
        """The output entry lands in ``("project", pid)`` exactly —
        not under the original sid."""
        store = _RecordingStore()
        key = write_distilled_summary(
            store,
            summary="A distilled finding.",
            sid="csl-2026-05-18-abc",
            project_id="abc123def456",
            original_count=7,
        )
        self.assertEqual(len(store.puts), 1)
        ns, k, value, kwargs = store.puts[0]
        self.assertEqual(ns, ("project", "abc123def456"))
        self.assertEqual(k, key)
        # The summary is the indexed text.
        self.assertEqual(value["text"], "A distilled finding.")
        # And the index hint is on "text" so it's recall-searchable.
        self.assertEqual(kwargs.get("index"), ["text"])

    def test_summary_metadata_carries_provenance(self) -> None:
        """``distilled_from_sid``, ``distilled_at``, ``original_count``
        are part of the value dict so a later recall hit can attribute
        back to the originating session."""
        store = _RecordingStore()
        write_distilled_summary(
            store,
            summary="Some summary text.",
            sid="csl-99",
            project_id="ppp",
            original_count=5,
            distilled_at="2026-05-18T12:00:00+00:00",
        )
        _, _, value, _ = store.puts[0]
        self.assertEqual(value["distilled_from_sid"], "csl-99")
        self.assertEqual(value["distilled_at"], "2026-05-18T12:00:00+00:00")
        self.assertEqual(value["original_count"], 5)

    def test_write_summary_refuses_empty_summary(self) -> None:
        """An empty / whitespace-only summary is treated as
        distillation failure even after a 200 from the LLM."""
        store = _RecordingStore()
        with self.assertRaises(DistillationFailed):
            write_distilled_summary(
                store, summary="   ", sid="s", project_id="p",
                original_count=1,
            )
        self.assertEqual(store.puts, [])

    def test_write_summary_refuses_no_store(self) -> None:
        """``store=None`` (e.g. provider load failed) → fail loud."""
        with self.assertRaises(DistillationFailed):
            write_distilled_summary(
                None, summary="x", sid="s", project_id="p",
                original_count=1,
            )

    def test_write_summary_key_is_deterministic_on_summary_text(self) -> None:
        """Two writes of the same (sid, summary) produce the same key
        — the providers' content_hash idempotency does the rest."""
        store = _RecordingStore()
        k1 = write_distilled_summary(
            store, summary="same body", sid="s1", project_id="p1",
            original_count=2,
        )
        k2 = write_distilled_summary(
            store, summary="same body", sid="s1", project_id="p1",
            original_count=2,
        )
        self.assertEqual(k1, k2)

    def test_write_summary_extra_meta_merged_into_value(self) -> None:
        """Reaper passes ``extra_meta`` to carry the run-side
        provenance (e.g. distill model used). It must land in the
        value dict so a recall hit can surface it."""
        store = _RecordingStore()
        write_distilled_summary(
            store, summary="body", sid="s", project_id="p",
            original_count=1,
            extra_meta={"distill_model": "gemma4:31b-cloud", "cwd": "/proj/x"},
        )
        _, _, value, _ = store.puts[0]
        self.assertEqual(value["distill_model"], "gemma4:31b-cloud")
        self.assertEqual(value["cwd"], "/proj/x")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
