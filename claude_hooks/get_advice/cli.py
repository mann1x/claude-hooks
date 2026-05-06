"""``claude-advisor`` — CLI driver for the /get-advice skill.

Subcommands (all output JSON-on-stdout for easy parsing by Claude):

    turn <sid> --message <text> [--first] [--cwd <path>]
    reset <sid> --carryover <text> [--new-sid <sid2>]
    get-model / set-model <name> [<ctx>]
    get-effort / set-effort <tier>
    get-tools / set-tools <csv>
    cleanup
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional

from claude_hooks.agent_loop import runner as agent_loop_runner
from claude_hooks.caliber_proxy import prompt as caliber_prompt
from claude_hooks.caliber_proxy import tools as caliber_tools
from claude_hooks.get_advice import (
    config as advisor_config,
    state as advisor_state,
)
from claude_hooks.get_advice.chat_client import ChatClient
from claude_hooks.get_advice.ctx_probe import base_url_default, probe_max_ctx


# First-message instructions injected ahead of Claude's framing text on
# the first turn of every session. Tells the advisor model that this is
# direct LLM-to-LLM and what to optimize for. Kept here (not in
# SKILL.md) because it's mechanical framing that should be identical
# across invocations; SKILL.md is for Claude's per-call judgement.
ADVISOR_PREAMBLE = (
    "You are an LLM advisor in a direct LLM-to-LLM conversation with "
    "another assistant (Claude Code). The other assistant is asking "
    "for a focused second opinion on a specific question — not "
    "general help, not chit-chat. Optimize for: concise replies, "
    "high information density per token, decisive recommendations "
    "where you have grounds, explicit uncertainty where you do not. "
    "Avoid filler, avoid restating the question, avoid sign-offs. "
    "When the question is technical (code, infra, training, design), "
    "use the available tools to verify claims against the actual "
    "files before answering — do not guess from names alone. "
    "When you do not have enough information, ask one specific "
    "question rather than several broad ones."
)


def _emit(obj: dict, code: int = 0) -> int:
    sys.stdout.write(json.dumps(obj, indent=2) + "\n")
    sys.stdout.flush()
    return code


def _err(msg: str, code: int = 2) -> int:
    return _emit({"ok": False, "error": msg}, code)


def _resolve_ctx_max(cfg: advisor_config.AdvisorConfig,
                     base_url: str) -> Optional[int]:
    if cfg.ctx_max_explicit and cfg.ctx_max:
        return cfg.ctx_max
    if cfg.ctx_max:
        return cfg.ctx_max
    return probe_max_ctx(cfg.model, base_url)


def _filter_tool_specs(allowed: list[str]) -> list[dict]:
    if not allowed:
        return []
    specs = caliber_tools.openai_tool_specs()
    return [s for s in specs
            if s.get("function", {}).get("name") in set(allowed)]


def _adapt_tool_executor():
    """The agent-loop runner expects ``(name, args_str, cwd) -> str``.
    ``caliber_tools.execute`` already has that signature, so we forward
    it directly."""
    return caliber_tools.execute


# ----------------------- subcommand handlers ----------------------- #

def cmd_get_model(args: argparse.Namespace) -> int:
    cfg = advisor_config.load_config()
    out = {
        "ok": True,
        "model": cfg.model,
        "ctx_max": cfg.ctx_max,
        "ctx_max_explicit": cfg.ctx_max_explicit,
    }
    return _emit(out)


def cmd_set_model(args: argparse.Namespace) -> int:
    try:
        cfg = advisor_config.set_model(args.name, args.ctx)
    except ValueError as e:
        return _err(str(e))
    return _emit({
        "ok": True,
        "model": cfg.model,
        "ctx_max": cfg.ctx_max,
        "ctx_max_explicit": cfg.ctx_max_explicit,
    })


def cmd_get_effort(args: argparse.Namespace) -> int:
    cfg = advisor_config.load_config()
    return _emit({
        "ok": True,
        "effort": cfg.effort,
        "budget_sessions": cfg.effort_budget,
        "available": list(advisor_config.EFFORT_BUDGETS.keys()),
    })


def cmd_set_effort(args: argparse.Namespace) -> int:
    try:
        cfg = advisor_config.set_effort(args.tier)
    except ValueError as e:
        return _err(str(e))
    return _emit({
        "ok": True,
        "effort": cfg.effort,
        "budget_sessions": cfg.effort_budget,
    })


def cmd_get_tools(args: argparse.Namespace) -> int:
    cfg = advisor_config.load_config()
    return _emit({
        "ok": True,
        "tools": list(cfg.tools),
        "known": list(advisor_config.KNOWN_TOOLS),
    })


def cmd_set_tools(args: argparse.Namespace) -> int:
    try:
        cfg = advisor_config.set_tools(args.spec)
    except ValueError as e:
        return _err(str(e))
    return _emit({"ok": True, "tools": list(cfg.tools)})


def cmd_cleanup(args: argparse.Namespace) -> int:
    removed = advisor_state.cleanup_old_sessions(
        max_age_seconds=int(args.max_age_seconds),
    )
    return _emit({"ok": True, "removed": removed})


def cmd_turn(args: argparse.Namespace) -> int:
    cfg = advisor_config.load_config()
    cwd = Path(args.cwd or os.getcwd()).resolve()
    base_url = base_url_default()

    sid = args.sid
    sess = advisor_state.load_session(sid)
    if sess is None:
        if not args.first:
            return _err(
                f"session {sid!r} not found; pass --first to start a new one",
            )
        ctx_max = _resolve_ctx_max(cfg, base_url)
        sess = advisor_state.SessionState(
            sid=sid,
            model=cfg.model,
            ctx_max=ctx_max,
            reset_threshold=cfg.reset_threshold,
        )
    elif args.first:
        return _err(
            f"session {sid!r} already exists; drop --first or use a new sid",
        )

    # Build the conversation. On the first turn, prepend grounding +
    # advisor preamble + caliber's tool addendum.
    if not sess.messages:
        tools_available = bool(cfg.tools)
        grounding = caliber_prompt.build_grounding_messages(
            str(cwd),
            extended_sources=False,
            tools_available=tools_available,
        )
        preamble = {"role": "system", "content": ADVISOR_PREAMBLE}
        sess.messages = list(grounding) + [preamble]

    sess.messages.append({"role": "user", "content": args.message})

    tool_specs = _filter_tool_specs(cfg.tools)
    tools_available = bool(tool_specs)
    cfg_loop = agent_loop_runner.LoopConfig(
        max_iterations=int(args.max_iter),
        force_answer_after=int(args.force_answer_after),
        tools_available=tools_available,
        # Let the advisor think. Cloud reasoning models (deepseek-v4,
        # qwen3.5:cloud) reject ``think=false`` outright (proxy returns
        # HTTP 500), and even where it's accepted, the whole point of
        # asking an advisor is to get its reasoning — disabling
        # thinking is the opposite of the skill's goal. Caliber's
        # historical ``think=false`` default exists because gemma4
        # over-thinks on structured-output tasks; that doesn't apply
        # here.
        think=True,
        # Don't force first tool: the advisor may answer cleanly from
        # context without needing tools. Caliber's force_first is for
        # gemma4 grounding-citation discipline; that's caliber's
        # contract, not ours.
        force_first_tool_call=False,
        force_first_retry_enabled=False,
        # Preserve burst dedup + cap; keeps small advisor models honest.
        max_tool_calls_per_turn=int(args.max_tool_calls_per_turn),
    )

    # Build the payload. Only forward ``num_ctx`` when the user
    # explicitly pinned a value via ``/get-advice--model NAME CTX``;
    # auto-probed values stay internal (used for the reset trigger,
    # not for sizing the upstream KV cache). Cloud-hosted models
    # (``:cloud`` suffix) reject explicit num_ctx with HTTP 500 when
    # it equals their full reported context, so leaving it off is
    # also the correct default for them.
    options: dict = {}
    if cfg.ctx_max_explicit and cfg.ctx_max:
        options["num_ctx"] = cfg.ctx_max
    payload = {
        "model": cfg.model,
        "messages": list(sess.messages),
        "options": options,
    }

    client = ChatClient(base_url)
    result = agent_loop_runner.run_loop(
        payload,
        str(cwd),
        config=cfg_loop,
        tool_specs=tool_specs,
        chat_fn=client.chat,
        tool_executor=_adapt_tool_executor(),
        preseed_builder=None,
    )

    choice = (result.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    reply = msg.get("content") or ""

    # Persist final assistant turn into messages history so the next
    # turn sees it. We deliberately drop intermediate tool messages
    # from sess.messages: replaying tool history wastes context on
    # small-window advisor models, and the runner already has the
    # final reply. This is a deliberate trade-off — the advisor will
    # not "remember" what tools it called previously, only its final
    # answer.
    if reply:
        sess.messages.append({"role": "assistant", "content": reply})

    last = client.last_usage
    sess.last_prompt_tokens = int(last.get("prompt_eval_count") or 0)
    sess.last_completion_tokens = int(last.get("eval_count") or 0)
    sess.cumulative_prompt_tokens += sess.last_prompt_tokens
    sess.cumulative_completion_tokens += sess.last_completion_tokens
    sess.turns += 1
    sess.save()

    return _emit({
        "ok": True,
        "sid": sid,
        "reply": reply,
        "model": cfg.model,
        "usage": {
            "prompt_eval_count": sess.last_prompt_tokens,
            "eval_count": sess.last_completion_tokens,
            "cumulative_prompt": sess.cumulative_prompt_tokens,
            "cumulative_completion": sess.cumulative_completion_tokens,
        },
        "ctx_max": sess.ctx_max,
        "ctx_used_pct": sess.ctx_used_pct(),
        "reset_recommended": sess.reset_recommended(),
        "turns": sess.turns,
        "tools_enabled": [s.get("function", {}).get("name")
                          for s in tool_specs],
    })


def cmd_reset(args: argparse.Namespace) -> int:
    cfg = advisor_config.load_config()
    new_sid = args.new_sid or f"{args.sid}-r{int(__import__('time').time())}"
    base_url = base_url_default()
    ctx_max = _resolve_ctx_max(cfg, base_url)
    sess = advisor_state.SessionState(
        sid=new_sid,
        model=cfg.model,
        ctx_max=ctx_max,
        reset_threshold=cfg.reset_threshold,
    )
    sess.save()
    # The carryover is stored as a one-shot user message that will be
    # delivered on the next ``turn`` call (with --first). Caller can
    # also just call ``turn`` directly with --first and the carryover
    # text — this command exists mostly to materialize the new sid.
    return _emit({
        "ok": True,
        "old_sid": args.sid,
        "new_sid": new_sid,
        "model": cfg.model,
        "ctx_max": ctx_max,
        "carryover_hint": args.carryover,
    })


# --------------------------- argparse ------------------------------ #

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="claude-advisor")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_gm = sub.add_parser("get-model")
    p_gm.set_defaults(func=cmd_get_model)

    p_sm = sub.add_parser("set-model")
    p_sm.add_argument("name")
    p_sm.add_argument("ctx", nargs="?", type=int, default=None)
    p_sm.set_defaults(func=cmd_set_model)

    p_ge = sub.add_parser("get-effort")
    p_ge.set_defaults(func=cmd_get_effort)

    p_se = sub.add_parser("set-effort")
    p_se.add_argument("tier")
    p_se.set_defaults(func=cmd_set_effort)

    p_gt = sub.add_parser("get-tools")
    p_gt.set_defaults(func=cmd_get_tools)

    p_st = sub.add_parser("set-tools")
    p_st.add_argument("spec", help="csv of tool names, or 'all', or 'none'")
    p_st.set_defaults(func=cmd_set_tools)

    p_cl = sub.add_parser("cleanup")
    p_cl.add_argument("--max-age-seconds", default=86400)
    p_cl.set_defaults(func=cmd_cleanup)

    p_t = sub.add_parser("turn")
    p_t.add_argument("sid")
    p_t.add_argument("--message", required=True)
    p_t.add_argument("--first", action="store_true")
    p_t.add_argument("--cwd", default=None)
    p_t.add_argument("--max-iter", default=8)
    p_t.add_argument("--force-answer-after", default=4)
    p_t.add_argument("--max-tool-calls-per-turn", default=6)
    p_t.set_defaults(func=cmd_turn)

    p_r = sub.add_parser("reset")
    p_r.add_argument("sid")
    p_r.add_argument("--carryover", required=True)
    p_r.add_argument("--new-sid", default=None)
    p_r.set_defaults(func=cmd_reset)

    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except SystemExit:
        raise
    except Exception as e:  # pragma: no cover — last-resort guard
        return _err(f"{type(e).__name__}: {e}", code=3)


if __name__ == "__main__":
    sys.exit(main())
