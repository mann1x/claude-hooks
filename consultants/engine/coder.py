"""``coder`` role — sandboxed code generation lane.

M10 of the v2 council overhaul. The coder is the second specialist
role (after M6's ``tool_executor``) that the planner can route to
when the user's question is fundamentally a code-writing task
rather than a "explain / audit / decide" task. Same shape as
``researcher_node`` / ``tool_executor_node`` so the graph wiring
follows the established pattern; the differences are:

1. **Sandboxed output.** The coder gets a single ``write_file``
   tool that writes ONLY inside
   ``<cwd>/.claude-hooks/consultants/<sid>/coder-out/``. No
   ``read_file``, ``grep``, etc — research is the researcher's job;
   by the time the coder fires, the researcher's findings are
   already in state and rendered into the coder's prompt.
2. **Per-session caps.** The sandbox guard rejects writes that
   would exceed per-file (default 50 KB) or per-session (default
   1 MB) byte budgets, and a per-session file count (default 16).
   A runaway model that decides to write the whole stdlib gets a
   tombstone instead of disk pressure.
3. **Planner-gated.** Only fires when the planner declares
   ``requires_code_generation: true`` in its preamble AND
   ``cfg.roles.coder.enabled``. Both conditions matter — the gate
   exists so a question like "Explain dataclass internals" doesn't
   trigger speculative coder output, and the role flag exists so
   users can opt out entirely while we collect M11b benchmark data
   on which model performs best.

The node emits ``CoderArtifact`` entries on the additive
``coder_artifacts`` channel; the synthesizer's prompt builder renders
those entries alongside the research findings so the final answer
can cite the on-disk paths the coder produced.

Pure-Python (except for an optional lazy ``run_loop`` import); the
sandbox guard is plain ``pathlib`` + ``os.makedirs`` so the module
loads cleanly in the main env for tests.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

from consultants.engine.state_v2 import CoderArtifact, CoderTaskItem

log = logging.getLogger("consultants.engine.coder")


# ============================================================== #
# Prompt fragments
# ============================================================== #

CODER_SYSTEM = (
    "ROLE: coder. You are the code-writing specialist on a council "
    "of LLM agents. The planner emitted a task that requires you to "
    "produce code; the researcher already gathered relevant evidence "
    "from the project. Your job is to write the code inside the "
    "sandbox using the ``write_file`` tool — nothing else.\n\n"
    "RULES\n"
    "1. Use ``write_file`` only. The tool writes inside a per-session "
    "sandbox; you cannot read files, run shell commands, or escape "
    "it. The researcher's findings (rendered in your prompt) are "
    "your view of the existing codebase.\n"
    "2. Paths are RELATIVE to the sandbox root. Use forward slashes. "
    "Never start a path with ``/`` or ``..``; both are rejected.\n"
    "3. Be terse. Write the smallest code that satisfies the task. "
    "Comment only where intent is non-obvious. No filler docstrings "
    "explaining what the code obviously does.\n"
    "4. Size budget — each file capped at the per-session limit (see "
    "your prompt for the bytes). Exceeding the cap returns an "
    "error from ``write_file`` rather than silently truncating.\n"
    "5. After writing, produce a SHORT final message (5-15 lines) "
    "naming the file(s) you wrote and a one-sentence summary of "
    "each. Do NOT paste the code back into the message — the "
    "synthesizer reads the files via the artifact listing."
)


# Inserted into the planner's system prompt when ``cfg.roles.coder.enabled``.
# Placed after the existing planner job description; the planner
# emits its plan as usual, then appends a fenced ``json`` block when
# it decides the question is code-shaped. ``parse_coder_preamble``
# scans the model output for this fence.
PLANNER_CODER_GATE_BLOCK = (
    "\n\nCODER GATE — only when the user's question REQUIRES writing "
    "new code or files (not just analyzing existing code). When it "
    "does, append ONE fenced ```json block at the end of your reply, "
    "shaped:\n\n"
    "```json\n"
    "{\"requires_code_generation\": true, \"coder_tasks\": ["
    "{\"task\": \"<one-sentence what to write>\", "
    "\"path\": \"<relative sandbox path>\", "
    "\"why\": \"<one-line reason>\"}"
    "]}\n"
    "```\n\n"
    "Rules: 1-4 tasks max; each task is self-contained (the coder "
    "lane will not see the other tasks); ``path`` is advisory — the "
    "coder may rename. When code generation is NOT needed, OMIT the "
    "block entirely OR emit "
    "``{\"requires_code_generation\": false}``."
)


def _fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n // 1024} KB"
    return f"{n // (1024 * 1024)} MB"


def build_coder_messages(task: CoderTaskItem,
                         grounding_msgs: list[dict],
                         *,
                         question: str = "",
                         plan: str = "",
                         research: Optional[list[str]] = None,
                         max_file_bytes: int = 50 * 1024,
                         max_total_bytes: int = 1024 * 1024,
                         max_files: int = 16) -> list[dict]:
    """Build the conversation seed for one coder lane.

    Layout: grounding first (anchor files + structure map — same as
    the researcher), then the CODER_SYSTEM message, then a user
    message that names the task, surfaces the researcher's findings
    (so the coder knows what existing structure to fit into), and
    declares the sandbox caps.
    """
    msgs: list[dict] = list(grounding_msgs or [])
    msgs.append({"role": "system", "content": CODER_SYSTEM})
    parts: list[str] = []
    if question:
        parts.append(f"USER QUESTION (context only):\n{question.strip()}")
    if plan:
        parts.append(f"\nPLAN:\n{plan.strip()}")
    if research:
        parts.append("\nRESEARCHER FINDINGS:")
        for i, r in enumerate(research, start=1):
            text = (r or "").strip()
            if not text:
                continue
            parts.append(f"\n--- finding {i} ---\n{text}")
    parts.append(f"\nTASK:\n{task.task.strip()}")
    if task.path:
        parts.append(f"\nSUGGESTED PATH (sandbox-relative, advisory): "
                     f"{task.path.strip()}")
    if task.why:
        parts.append(f"\nWHY (planner's reason):\n{task.why.strip()}")
    parts.append(
        f"\nSANDBOX CAPS\n"
        f"- per-file max: {_fmt_size(max_file_bytes)}\n"
        f"- total bytes this lane: {_fmt_size(max_total_bytes)}\n"
        f"- max files this lane: {max_files}\n"
        f"Exceeding any cap returns an error from write_file."
    )
    msgs.append({"role": "user", "content": "\n".join(parts)})
    return msgs


# ============================================================== #
# Sandboxed write_file tool factory
# ============================================================== #

# OpenAI-shape tool spec the coder's agent_loop sees. We deliberately
# expose ONLY this tool to the coder; the researcher's full
# read_file / grep / glob stack is gated out so the coder can't
# regress to "research before writing" inside its own lane.
CODER_WRITE_FILE_TOOL_SPEC: dict = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": (
            "Write a file inside the coder sandbox. The path is "
            "relative to the per-session sandbox root; it must not "
            "start with '/' or contain '..'. Per-file and per-session "
            "byte caps apply — exceeding either returns an error and "
            "the file is NOT written. Returns a short status string "
            "with the absolute path + byte count on success."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Relative path inside the sandbox. Use "
                        "forward slashes, no leading '/'."
                    ),
                },
                "content": {
                    "type": "string",
                    "description": "Full file contents.",
                },
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
    },
}


def _normalise_sandbox_path(rel: str) -> str:
    """Reject traversal / absolute paths; return a clean POSIX-ish
    relative path. Raises ``ValueError`` on rejection.

    The check is conservative: we walk the parts and bail on any
    ``..`` or empty-or-root segment, then re-join with ``os.sep``.
    Tested separately so the rejection criteria are documented.
    """
    if not rel or not isinstance(rel, str):
        raise ValueError("path is empty")
    s = rel.strip().replace("\\", "/")
    if not s:
        raise ValueError("path is empty after strip")
    if s.startswith("/"):
        raise ValueError(f"absolute paths rejected: {rel!r}")
    parts = [p for p in s.split("/") if p not in ("", ".")]
    if not parts:
        raise ValueError(f"path has no usable segments: {rel!r}")
    for p in parts:
        if p == "..":
            raise ValueError(
                f"path traversal rejected: {rel!r}"
            )
        # Defensive — disallow null bytes / pure whitespace.
        if "\x00" in p or p.strip() != p or not p.strip():
            raise ValueError(
                f"path segment rejected: {p!r} in {rel!r}"
            )
    return "/".join(parts)


def sandbox_root_for(cwd: str, sid: str) -> Path:
    """Return the absolute sandbox root for ``sid``. The directory
    is NOT created here; the writer creates it lazily on first
    successful write so a coder lane that never writes leaves no
    on-disk trace.
    """
    return Path(cwd) / ".claude-hooks" / "consultants" / str(sid) / "coder-out"


class CoderSandbox:
    """Per-lane sandbox + audit buffer. One instance per
    ``coder_node`` invocation; the closure-style ``write_file``
    executor holds a reference and updates the byte/file counters as
    the agent loop calls in.

    The audit list ``writes`` is the source of truth for the
    ``CoderArtifact`` the node returns. Each entry is a dict
    ``{"path": str, "bytes": int, "sha256": str}``; we expose plain
    dicts (not a dataclass) so they round-trip through JSON for SSE
    + transcript.db with no special encoder.
    """

    def __init__(self, *, root: Path,
                 max_file_bytes: int,
                 max_total_bytes: int,
                 max_files: int):
        self.root = root
        self.max_file_bytes = int(max_file_bytes)
        self.max_total_bytes = int(max_total_bytes)
        self.max_files = int(max_files)
        self.writes: list[dict] = []
        self.bytes_written: int = 0
        self.rejections: list[str] = []

    def write(self, rel_path: str, content: str) -> str:
        """Atomically write ``content`` at ``rel_path`` inside the
        sandbox after enforcing the caps. Returns a status string for
        the LLM's tool result on success; raises ``ValueError`` on
        any guard failure. The caller wraps the raise into the OpenAI
        tool-call error shape.
        """
        if not isinstance(content, str):
            raise ValueError("content must be a string")
        body = content
        body_bytes = body.encode("utf-8")
        n = len(body_bytes)
        # Per-file cap
        if n > self.max_file_bytes:
            msg = (
                f"file size {n} B exceeds per-file cap "
                f"{self.max_file_bytes} B"
            )
            self.rejections.append(msg)
            raise ValueError(msg)
        # Per-file-count cap — checked BEFORE per-total so the limit
        # message is unambiguous when both are saturated.
        if len(self.writes) >= self.max_files:
            msg = (
                f"file count {len(self.writes)} reached max_files cap "
                f"{self.max_files}"
            )
            self.rejections.append(msg)
            raise ValueError(msg)
        # Per-session total cap
        if self.bytes_written + n > self.max_total_bytes:
            msg = (
                f"total bytes would reach "
                f"{self.bytes_written + n} B, exceeding cap "
                f"{self.max_total_bytes} B"
            )
            self.rejections.append(msg)
            raise ValueError(msg)
        # Path guard
        clean = _normalise_sandbox_path(rel_path)
        target = self.root / clean
        # Resolve and double-check we're still under root after
        # symlinks / weird cases. We don't call resolve(strict=True)
        # because the file doesn't exist yet; instead build the
        # absolute path and verify it starts with root's absolute.
        target_abs = target.resolve(strict=False)
        root_abs = self.root.resolve(strict=False)
        try:
            target_abs.relative_to(root_abs)
        except ValueError:
            msg = f"resolved path escaped sandbox: {target_abs!s}"
            self.rejections.append(msg)
            raise ValueError(msg) from None
        # Create parents + write atomically (temp file + rename) so a
        # partial write never lands on disk.
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        try:
            with open(tmp, "wb") as f:
                f.write(body_bytes)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, target)
        except OSError as e:
            try:
                tmp.unlink(missing_ok=True)  # py 3.8+
            except OSError:  # pragma: no cover
                pass
            raise ValueError(f"filesystem error writing {clean}: {e}") from e
        sha = hashlib.sha256(body_bytes).hexdigest()
        entry = {"path": clean, "bytes": n, "sha256": sha}
        self.writes.append(entry)
        self.bytes_written += n
        return (
            f"wrote {clean} ({n} B, sha256={sha[:12]}). "
            f"used {self.bytes_written}/{self.max_total_bytes} B, "
            f"{len(self.writes)}/{self.max_files} files."
        )


def make_coder_sandbox(*, cwd: str, sid: str,
                       max_file_bytes: int = 50 * 1024,
                       max_total_bytes: int = 1024 * 1024,
                       max_files: int = 16) -> CoderSandbox:
    """Factory — wraps the sandbox-root resolver + cap defaults so
    callers don't recompute the path arithmetic at every entry."""
    return CoderSandbox(
        root=sandbox_root_for(cwd, sid),
        max_file_bytes=max_file_bytes,
        max_total_bytes=max_total_bytes,
        max_files=max_files,
    )


def make_sandbox_tool_executor(sandbox: CoderSandbox):
    """Return an executor callable that the agent_loop runner uses
    to dispatch tool calls. Honors the same shape as
    ``caliber_proxy.tools.make_executor`` — a ``(name, args, **kw)``
    callable that returns the tool's string output.

    Only the ``write_file`` tool is accepted; any other tool name
    returns an error string so the model self-corrects on the next
    iteration.
    """
    def _exec(name: str, args: str, **kw) -> str:
        if name != "write_file":
            return (
                f"error: tool {name!r} not available to the coder "
                f"role; use write_file only."
            )
        try:
            payload = json.loads(args) if isinstance(args, str) else dict(args)
        except (TypeError, ValueError) as e:
            return f"error: invalid JSON args: {e}"
        path = payload.get("path")
        content = payload.get("content")
        if not isinstance(path, str) or not isinstance(content, str):
            return (
                "error: write_file requires {path: str, content: str}"
            )
        try:
            return sandbox.write(path, content)
        except ValueError as e:
            return f"error: {e}"
    return _exec


# ============================================================== #
# Event helpers — defensive no-ops outside a runnable context
# ============================================================== #

def _emit_started(role: str, *, round: int, lane_idx: Optional[int],
                  model: Optional[str]) -> None:
    try:
        from consultants.engine.events import NodeStarted, emit
        emit(NodeStarted(role=role, round=round, lane_idx=lane_idx,
                          model=model))
    except Exception:  # pragma: no cover
        log.exception("emit NodeStarted raised; ignored")


def _emit_finished(role: str, *, round: int, lane_idx: Optional[int],
                   duration_ms: int, ok: bool,
                   error: Optional[str] = None) -> None:
    try:
        from consultants.engine.events import NodeFinished, emit
        emit(NodeFinished(role=role, round=round, lane_idx=lane_idx,
                           duration_ms=duration_ms,
                           ok=ok, error=error))
    except Exception:  # pragma: no cover
        log.exception("emit NodeFinished raised; ignored")


def _emit_tool_call(role: str, *, round: int, lane_idx: Optional[int],
                    tool: str, args_preview: str, output_preview: str,
                    duration_ms: int,
                    error: Optional[str] = None) -> None:
    try:
        from consultants.engine.events import ToolCall, emit
        emit(ToolCall(role=role, round=round, lane_idx=lane_idx,
                       tool=tool, args_preview=args_preview[:200],
                       output_preview=output_preview[:200],
                       duration_ms=duration_ms, error=error))
    except Exception:  # pragma: no cover
        log.exception("emit ToolCall raised; ignored")


# ============================================================== #
# Node
# ============================================================== #

def coder_node(state: dict,
               *,
               chat_client,
               grounding_msgs: list[dict],
               model: str,
               cwd: str,
               sid: str,
               think: Any = "high",
               loop_runner=None,
               recorder=None,
               max_file_bytes: int = 50 * 1024,
               max_total_bytes: int = 1024 * 1024,
               max_files: int = 16) -> dict:
    """Execute one ``CoderTaskItem`` from the per-lane state slice.

    Returns a state delta merging into ``coder_artifacts``. Never
    raises — failures land as a tombstone ``CoderArtifact`` with the
    ``error`` field populated.

    Parameters mirror ``tool_executor_node`` so the runner can wire
    the same deps (chat_client per role, grounding_msgs, recorder,
    cwd) without divergent plumbing. ``sid`` is the session id used
    to root the sandbox under ``<cwd>/.claude-hooks/consultants/<sid>
    /coder-out/`` — required (the runner injects from
    ``state.sid``).

    ``loop_runner`` defaults to
    ``claude_hooks.agent_loop.runner.run_loop`` lazy-imported so the
    module loads in envs where claude_hooks isn't on the path.
    """
    item: Optional[CoderTaskItem] = state.get("coder_task_item")
    lane_idx = state.get("lane_idx")
    parent_round = (
        int(item.parent_round) if item is not None else 1
    )
    t0 = time.monotonic()

    # Defensive: a Send without a task is a graph-wiring bug.
    if item is None or not isinstance(item, CoderTaskItem) \
            or not (item.task or "").strip():
        _emit_started("coder", round=parent_round,
                       lane_idx=lane_idx, model=model)
        _emit_finished(
            "coder", round=parent_round, lane_idx=lane_idx,
            duration_ms=int((time.monotonic() - t0) * 1000),
            ok=False, error="missing coder_task_item",
        )
        return {
            "coder_artifacts": [CoderArtifact(
                task="(missing)",
                summary="",
                error="coder lane invoked without a "
                       "coder_task_item on per-lane state",
                lane_idx=lane_idx,
                parent_round=parent_round,
                duration_ms=int((time.monotonic() - t0) * 1000),
            )],
        }

    _emit_started("coder", round=parent_round,
                   lane_idx=lane_idx, model=model)
    if recorder is not None:
        try:
            recorder.record_node(
                role="coder", kind="node_enter",
                round=parent_round, lane_idx=lane_idx,
            )
        except Exception:  # pragma: no cover
            log.exception("recorder.record_node raised; ignored")

    # Build per-lane sandbox + tool executor. The sandbox is short-
    # lived (one node invocation) but the audit log lives long enough
    # to seed the CoderArtifact return.
    sandbox = make_coder_sandbox(
        cwd=cwd, sid=sid,
        max_file_bytes=max_file_bytes,
        max_total_bytes=max_total_bytes,
        max_files=max_files,
    )
    tool_executor = make_sandbox_tool_executor(sandbox)

    # Lazy run_loop import — same pattern as tool_executor_node.
    if loop_runner is None:
        try:
            from claude_hooks.agent_loop.runner import run_loop  # lazy
            loop_runner = run_loop
        except Exception:  # pragma: no cover
            log.exception("could not import run_loop; aborting lane")
            dt_ms = int((time.monotonic() - t0) * 1000)
            _emit_finished(
                "coder", round=parent_round, lane_idx=lane_idx,
                duration_ms=dt_ms, ok=False,
                error="run_loop import failed",
            )
            return {
                "coder_artifacts": [CoderArtifact(
                    task=item.task, summary="",
                    error="agent_loop.runner.run_loop not available",
                    lane_idx=lane_idx, parent_round=parent_round,
                    duration_ms=dt_ms,
                )],
            }

    try:
        from claude_hooks.agent_loop.runner import LoopConfig
    except Exception:  # pragma: no cover
        LoopConfig = None  # type: ignore[assignment]

    msgs = build_coder_messages(
        item, grounding_msgs,
        question=state.get("question") or "",
        plan=state.get("plan") or "",
        research=list(state.get("research") or []),
        max_file_bytes=max_file_bytes,
        max_total_bytes=max_total_bytes,
        max_files=max_files,
    )
    payload = {"model": model, "messages": msgs, "stream": False}

    cfg = None
    if LoopConfig is not None:
        cfg = LoopConfig(
            # Coder is more iterative than tool_executor (write,
            # potentially revise after reading an error), but not as
            # exploratory as researcher. Tuned conservatively pending
            # M11b data.
            max_iterations=8,
            force_answer_after=6,
            tools_available=True,
            think=think,
            force_first_tool_call=False,
        )

    # Recorder callbacks — coder role tag so post-mortem audits can
    # SQL by role and find every coder lane regardless of session.
    on_iter_cb = None
    on_tool_cb = None
    tools_called: list[str] = []
    if recorder is not None:
        def _on_iter(_idx: int, req: dict, resp: dict, dt_ms: int) -> None:
            pt, ct = _usage_from(resp)
            recorder.record_llm(
                role="coder", round=parent_round,
                lane_idx=lane_idx, model=model,
                request=req, response=resp,
                prompt_tokens=pt, completion_tokens=ct,
                duration_ms=dt_ms,
            )

        def _on_tool(name: str, args: str, output: str,
                     dt_ms: int, err: Optional[str]) -> None:
            recorder.record_tool(
                role="coder", round=parent_round,
                lane_idx=lane_idx, tool=name, args=args,
                output=output, duration_ms=dt_ms, error=err,
            )
            tools_called.append(name)
            _emit_tool_call(
                "coder", round=parent_round,
                lane_idx=lane_idx, tool=name,
                args_preview=args, output_preview=output,
                duration_ms=dt_ms, error=err,
            )
        on_iter_cb = _on_iter
        on_tool_cb = _on_tool
    else:
        def _on_tool_norec(name: str, args: str, output: str,
                           dt_ms: int, err: Optional[str]) -> None:
            tools_called.append(name)
            _emit_tool_call(
                "coder", round=parent_round,
                lane_idx=lane_idx, tool=name,
                args_preview=args, output_preview=output,
                duration_ms=dt_ms, error=err,
            )
        on_tool_cb = _on_tool_norec

    def _chat_fn(_payload: dict) -> dict:
        return chat_client.chat(_payload)

    try:
        result = loop_runner(
            payload,
            cwd,
            config=cfg,
            tool_specs=[CODER_WRITE_FILE_TOOL_SPEC],
            chat_fn=_chat_fn,
            tool_executor=tool_executor,
            on_iter=on_iter_cb,
            on_tool=on_tool_cb,
        )
    except Exception as e:
        log.exception("coder lane %s failed: %s", lane_idx, e)
        dt_ms = int((time.monotonic() - t0) * 1000)
        _emit_finished(
            "coder", round=parent_round, lane_idx=lane_idx,
            duration_ms=dt_ms, ok=False,
            error=f"{type(e).__name__}: {e}",
        )
        return {
            "coder_artifacts": [CoderArtifact(
                task=item.task,
                summary="",
                files=list(sandbox.writes),
                error=f"{type(e).__name__}: {e}",
                lane_idx=lane_idx, parent_round=parent_round,
                duration_ms=dt_ms,
            )],
        }

    final_text = _extract_final_content(result)
    dt_ms = int((time.monotonic() - t0) * 1000)
    if recorder is not None:
        try:
            recorder.record_node(
                role="coder", kind="node_exit",
                round=parent_round, lane_idx=lane_idx,
                duration_ms=dt_ms,
            )
        except Exception:  # pragma: no cover
            log.exception("recorder.record_node raised; ignored")
    # If the lane wrote zero files AND rejections occurred, surface
    # the rejection reason as an error so the synthesizer flags the
    # gap. Empty-files-with-no-rejections is treated as "model
    # decided no code was needed" — surfaced as the summary text
    # with no error.
    err: Optional[str] = None
    if not sandbox.writes and sandbox.rejections:
        err = "; ".join(sandbox.rejections[-3:])
    _emit_finished("coder", round=parent_round, lane_idx=lane_idx,
                    duration_ms=dt_ms, ok=err is None, error=err)
    return {
        "coder_artifacts": [CoderArtifact(
            task=item.task,
            summary=final_text,
            files=list(sandbox.writes),
            lane_idx=lane_idx, parent_round=parent_round,
            duration_ms=dt_ms,
            error=err,
        )],
    }


# ============================================================== #
# Internal helpers shared with tool_executor (kept local to avoid
# coupling — same shape but the coder picks the choice list
# differently in the future if we surface partial writes).
# ============================================================== #

def _usage_from(resp: dict) -> tuple[int, int]:
    if not isinstance(resp, dict):
        return (0, 0)
    usage = resp.get("usage") or {}
    pt = (
        usage.get("prompt_tokens")
        or resp.get("prompt_eval_count")
        or 0
    )
    ct = (
        usage.get("completion_tokens")
        or resp.get("eval_count")
        or 0
    )
    try:
        return (int(pt), int(ct))
    except (TypeError, ValueError):
        return (0, 0)


def _extract_final_content(result: Any) -> str:
    if isinstance(result, dict):
        final = result.get("final") or result
        if isinstance(final, dict):
            choices = final.get("choices") or []
            if choices and isinstance(choices, list):
                msg = choices[0].get("message") if isinstance(
                    choices[0], dict) else None
                if isinstance(msg, dict):
                    content = msg.get("content") or ""
                    if isinstance(content, str):
                        return content
        content = final.get("content") if isinstance(final, dict) else None
        if isinstance(content, str):
            return content
    if isinstance(result, str):
        return result
    return str(result) if result is not None else ""


# ============================================================== #
# Planner-output parsers (coder gate + coder_tasks)
# ============================================================== #

_CODER_FENCE_RE = re.compile(
    r"```(?:json)?\s*(\{.*?\})\s*```",
    re.DOTALL | re.IGNORECASE,
)


def _candidate_bodies(text: str) -> list[str]:
    out: list[str] = []
    for m in _CODER_FENCE_RE.finditer(text):
        out.append(m.group(1))
    if not out:
        out.append(text.strip())
    return out


def parse_coder_preamble(text: str) -> Optional[bool]:
    """Extract the ``requires_code_generation`` flag from the
    planner's response. Returns:

    - ``True`` if the planner asserted code generation is needed
    - ``False`` if the planner explicitly opted out
    - ``None`` if the planner emitted no decidable signal
      (caller treats this as "no, default behavior")

    The function is tolerant: fenced JSON wins, bare JSON
    object at the end of the response is also accepted, malformed
    payloads return ``None`` so a confused planner doesn't crash
    the graph.
    """
    if not text or not isinstance(text, str):
        return None
    for raw in _candidate_bodies(text):
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        if "requires_code_generation" not in data:
            continue
        v = data.get("requires_code_generation")
        if isinstance(v, bool):
            return v
        # Tolerate "true"/"false" strings.
        if isinstance(v, str) and v.strip().lower() in ("true", "false"):
            return v.strip().lower() == "true"
    return None


def parse_coder_tasks(text: str, *,
                      parent_round: int = 1) -> list[CoderTaskItem]:
    """Extract ``CoderTaskItem`` entries from the planner's
    coder-gate JSON block. Same tolerance pattern as
    :func:`tool_executor.parse_tool_plan`: items missing ``task`` are
    skipped silently; malformed bodies return an empty list. The
    return is bounded — planner is instructed to emit 1-4 tasks; the
    parser doesn't enforce a hard cap because the graph dispatcher
    handles fanout sizing.
    """
    if not text or not isinstance(text, str):
        return []
    for raw in _candidate_bodies(text):
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        items: list = []
        if isinstance(data, dict) and "coder_tasks" in data:
            maybe = data["coder_tasks"]
            if isinstance(maybe, list):
                items = maybe
        elif isinstance(data, list):
            # Bare list shape — convenience.
            items = data
        else:
            continue
        out: list[CoderTaskItem] = []
        for idx, raw_item in enumerate(items):
            if not isinstance(raw_item, dict):
                continue
            task = (raw_item.get("task") or "").strip()
            if not task:
                continue
            path = (raw_item.get("path") or "").strip()
            why = (raw_item.get("why") or "").strip()
            out.append(CoderTaskItem(
                task=task, path=path, why=why,
                lane_idx=idx,
                parent_round=int(parent_round),
            ))
        if out:
            return out
    return []


# Block appended to the synthesizer's user message when
# coder_artifacts is non-empty. The synthesizer reads the artifact
# paths + summaries (not the file contents — those are too long to
# inline) and references them in the final answer.
def build_coder_artifacts_block(artifacts: list[CoderArtifact]) -> str:
    """Render the ``coder_artifacts`` channel for the synthesizer's
    prompt. Each entry surfaces:

    - the task (so the synthesizer knows what the lane was asked to do)
    - the summary (the coder's terminal message — usually a short
      "wrote X.py and Y.py because Z" paragraph)
    - the file listing with byte counts (so the synthesizer can
      reference paths and the user can verify on disk)
    - the error (tombstone), when set, so the synthesizer surfaces
      the gap rather than silently composing past it

    Empty list -> empty string (caller blindly concatenates).
    """
    if not artifacts:
        return ""
    lines = ["CODE GENERATED BY THE COUNCIL (from coder lanes):"]
    for i, a in enumerate(artifacts, start=1):
        if a.error:
            lines.append(
                f"\n{i}. (task: {a.task!r} FAILED: {a.error})"
            )
            continue
        files_summary = (
            ", ".join(f"{f['path']} ({f['bytes']} B)" for f in a.files)
            if a.files else "(no files written)"
        )
        summary = (a.summary or "").strip()
        if len(summary) > 600:
            summary = summary[:600] + "... [truncated]"
        lines.append(
            f"\n{i}. TASK: {a.task}\n"
            f"   FILES: {files_summary}\n"
            f"   SUMMARY: {summary}"
        )
    return "\n".join(lines)


__all__ = [
    "CODER_SYSTEM",
    "CODER_WRITE_FILE_TOOL_SPEC",
    "CoderSandbox",
    "PLANNER_CODER_GATE_BLOCK",
    "build_coder_artifacts_block",
    "build_coder_messages",
    "coder_node",
    "make_coder_sandbox",
    "make_sandbox_tool_executor",
    "parse_coder_preamble",
    "parse_coder_tasks",
    "sandbox_root_for",
]
