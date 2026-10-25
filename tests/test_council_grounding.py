"""ch-24: the council's anchor files are sized by its roles' windows.

A fixed 50,000-byte anchor budget made an 18,416-token head on a 32,768
window (csl-2026-10-25-0931-a7d3); compaction never folds the head, so
the lane thrashed. The cap is a share of the smallest window and only
ever lowers the default, so large windows send the same grounding.
"""
from __future__ import annotations

from types import SimpleNamespace

from claude_hooks.caliber_proxy import prompt
from consultants.engine import grounding


class Client:
    """A backend whose trained window is ``window`` for every model."""

    def __init__(self, window: int):
        self.window = window

    def context_length(self, model):
        return self.window


def _role(model, *, extra=(), budget=None):
    return SimpleNamespace(model=model, extra_models=list(extra),
                           budget=budget or {}, ctx_max=None,
                           ctx_max_explicit=False, model_overrides={})


def _cfg(**roles):
    return SimpleNamespace(budget={}, roles=roles)


def _project(tmp_path, claude_chars=50_185):
    (tmp_path / "CLAUDE.md").write_text("c" * claude_chars)
    (tmp_path / "README.md").write_text("r" * 4_000)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n')
    return str(tmp_path)


def _anchor_block(msgs):
    return next(m["content"] for m in msgs
                if m["content"].startswith("PROJECT ANCHOR FILES"))


class TestBudget:
    def test_the_share_of_a_small_window(self):
        assert grounding.anchor_bytes(32_768) == 9_830
        assert grounding.anchor_bytes(131_072) == 39_321

    def test_a_large_window_or_none_keeps_the_default(self):
        assert grounding.anchor_bytes(262_144) is None
        assert grounding.anchor_bytes(1_048_576) is None
        assert grounding.anchor_bytes(None) is None

    def test_the_smallest_window_of_every_role_and_model(self):
        cfg = _cfg(planner=_role("p"), researcher=_role("r", extra=["r2"]))
        clients = {"planner": Client(262_144), "researcher": Client(1_048_576)}
        assert grounding.smallest_window(cfg, clients,
                                         ["planner", "researcher"]) == 262_144

    def test_a_pinned_window_counts(self):
        # The ch-15 run: the backend says 1M, the researcher pins 32k.
        cfg = _cfg(planner=_role("p"),
                   researcher=_role("r", budget={"num_ctx": 32_768}))
        clients = {"planner": Client(1_048_576), "researcher": Client(1_048_576)}
        assert grounding.smallest_window(cfg, clients,
                                         ["planner", "researcher"]) == 32_768

    def test_a_disabled_role_does_not_count(self):
        cfg = _cfg(planner=_role("p"), coder=_role("c"))
        clients = {"planner": Client(262_144), "coder": Client(8_192)}
        assert grounding.smallest_window(cfg, clients, ["planner"]) == 262_144

    def test_an_undeclared_window_is_no_window(self, monkeypatch):
        # A backend whose caps declare no window resolves to the 32,768
        # fallback ("default"); that guess must not shrink the grounding.
        from consultants.engine import budget

        def fake(client, model, knobs=budget.DEFAULT_KNOBS):
            if model == "p":
                return budget.Window(budget.DEFAULT_WINDOW_TOKENS, "default")
            return budget.Window(262_144, "trained")
        monkeypatch.setattr(budget, "resolve_window", fake)
        cfg = _cfg(planner=_role("p"), researcher=_role("r"))
        clients = {"planner": Client(0), "researcher": Client(0)}
        assert grounding.smallest_window(cfg, clients, ["planner"]) is None
        assert grounding.smallest_window(cfg, clients,
                                         ["planner", "researcher"]) == 262_144

    def test_an_unanswered_probe_is_no_window(self):
        class Silent:
            def context_length(self, model):
                raise OSError("down")
        cfg = _cfg(planner=_role("p"))
        assert grounding.smallest_window(cfg, {"planner": Silent()},
                                         ["planner"]) is None

class TestGrounding:
    def test_a_large_window_is_byte_identical(self, tmp_path):
        cwd = _project(tmp_path)
        cfg = _cfg(planner=_role("p"), researcher=_role("r"))
        clients = {"planner": Client(262_144), "researcher": Client(1_048_576)}
        got = grounding.council_grounding(cwd, cfg, clients,
                                          ["planner", "researcher"])
        assert got == prompt.build_grounding_messages(cwd, tools_available=True)

    def test_a_small_window_caps_the_anchors_and_names_the_cut(self, tmp_path):
        cwd = _project(tmp_path)
        cfg = _cfg(researcher=_role("r", budget={"num_ctx": 32_768}))
        got = grounding.council_grounding(cwd, cfg,
                                          {"researcher": Client(1_048_576)},
                                          ["researcher"])
        block = _anchor_block(got)
        full = _anchor_block(prompt.build_grounding_messages(cwd))
        assert len(block) < len(full) // 3
        assert block.count("c") <= 9_830 + 200
        note = block[block.index("Left out to fit the context window"):]
        assert "CLAUDE.md (first" in note and "of 50,185 chars)" in note
        assert "README.md" in note and "read_file reaches them" in note

    def test_a_cut_at_the_default_says_nothing_new(self, tmp_path):
        # This repo's CLAUDE.md is past 50,000 chars: cut today, and the
        # block must stay as it was for every large-window council.
        cwd = _project(tmp_path)
        block = _anchor_block(prompt.build_grounding_messages(cwd))
        assert "[truncated to fit grounding budget]" in block
        assert "Left out to fit" not in block


class TestCaliberUnchanged:
    def test_read_anchor_files_is_as_before(self, tmp_path):
        cwd = _project(tmp_path)
        out = prompt.read_anchor_files(cwd, 10_000)
        assert list(out) == ["pyproject.toml", "CLAUDE.md"]
        assert out["CLAUDE.md"].endswith("[truncated to fit grounding budget]")

    def test_note_cut_is_off_by_default(self, tmp_path):
        cwd = _project(tmp_path)
        msgs = prompt.build_grounding_messages(cwd, max_anchor_bytes=10_000)
        assert "Left out to fit" not in _anchor_block(msgs)
