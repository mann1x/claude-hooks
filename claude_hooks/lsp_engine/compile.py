"""Opt-in compile-aware diagnostics layer.

Phase 3 of ``docs/PLAN-lsp-engine.md``. Disabled by default
(``lsp_engine.compile_aware.enabled = false``); when enabled, the
daemon spawns a per-language compile command (``cargo check``,
``tsc --noEmit``, etc) on a debounced schedule and merges its
diagnostics into the same ``diagnostics`` op response the LSPs feed.

Why this matters: LSPs answer "is the *file* well-formed?" — they
don't run the build system. Cargo's borrow-checker errors,
TypeScript's project-wide type-narrowing, mypy's full type analysis
only land via the actual compile pass. Pairing the two gives the
hook a complete answer: LSP for fast per-file feedback, compile for
the truth a build would surface.

Design constraints:

- **Off the hot path.** The compile pass runs in a worker thread,
  not on the IPC handler's call stack. Sessions querying
  ``diagnostics`` see whatever compile output is currently cached;
  they never block on a compile run.
- **Debounced.** Rapid ``did_change`` events for the same language
  coalesce into one run after ``debounce_seconds`` (default 1.5).
  Tests pin lower values for determinism.
- **Soft-fail.** If the compile binary isn't on PATH, or returns
  garbage, or times out, the runner logs and skips — LSP
  diagnostics still flow through normally.

Two built-in output parsers:

- ``cargo-json`` — parses cargo's ``--message-format=json`` line
  stream. Auto-selected when the command line contains
  ``--message-format=json``.
- ``text`` — generic line-based parser for ``file:line:col:
  severity: message`` (tsc, gcc, clang, mypy with default flags,
  most rustc-likes). The default fallback.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from claude_hooks.lsp_engine.lsp import Diagnostic, path_to_uri

log = logging.getLogger("claude_hooks.lsp_engine.compile")


DEFAULT_DEBOUNCE_S = 1.5
DEFAULT_RUN_TIMEOUT_S = 60.0


# ─── parsers ─────────────────────────────────────────────────────────


_TEXT_DIAG_RE = re.compile(
    # ``<path>:<line>:<col>: <severity>: <message>``
    # Severity captured loosely; we map common labels to LSP's 1-4 scale.
    r"^(?P<path>[^:\n]+):(?P<line>\d+):(?P<col>\d+):\s*"
    r"(?P<severity>error|warning|note|info|hint)?\s*:?\s*"
    r"(?P<msg>.+)$",
    re.IGNORECASE,
)

_SEVERITY_BY_LABEL = {
    "error": 1,
    "warning": 2,
    "info": 3,
    "note": 3,
    "hint": 4,
}


def parse_text_output(
    stdout: str,
    project_root: str | os.PathLike,
    *,
    source: str = "compile",
) -> list[Diagnostic]:
    """Parse generic text-style compiler output.

    Each matching line yields one ``Diagnostic``. Unmatched lines
    are dropped (they're typically progress noise or summaries).
    Relative paths are resolved against ``project_root`` so the
    daemon's URI lookups match.
    """
    root = Path(project_root)
    out: list[Diagnostic] = []
    for line in (stdout or "").splitlines():
        m = _TEXT_DIAG_RE.match(line.strip())
        if not m:
            continue
        path_str = m.group("path").strip()
        path = Path(path_str)
        if not path.is_absolute():
            path = (root / path).resolve()
        sev_label = (m.group("severity") or "error").lower()
        severity = _SEVERITY_BY_LABEL.get(sev_label, 1)
        try:
            line_num = max(0, int(m.group("line")) - 1)
            col_num = max(0, int(m.group("col")) - 1)
        except ValueError:
            continue
        out.append(
            Diagnostic(
                uri=path_to_uri(path),
                severity=severity,
                line=line_num,
                character=col_num,
                message=m.group("msg").strip(),
                source=source,
            )
        )
    return out


def parse_cargo_json_output(
    stdout: str,
    project_root: str | os.PathLike,
    *,
    source: str = "cargo",
) -> list[Diagnostic]:
    """Parse cargo's ``--message-format=json`` stream.

    Cargo emits one JSON object per line. The relevant ones are
    ``reason: "compiler-message"`` envelopes whose ``message.spans``
    array contains the source locations. We surface one Diagnostic
    per *primary* span (``is_primary=true``); secondary spans are
    related-info we'd surface in Phase 5+ if the LSP-engine grows a
    Hover op.
    """
    root = Path(project_root)
    out: list[Diagnostic] = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("reason") != "compiler-message":
            continue
        msg = obj.get("message") or {}
        level = (msg.get("level") or "error").lower()
        severity = _SEVERITY_BY_LABEL.get(level, 1)
        message_text = msg.get("message") or ""
        code_obj = msg.get("code") or {}
        code = code_obj.get("code") if isinstance(code_obj, dict) else None
        for span in msg.get("spans") or []:
            if not span.get("is_primary"):
                continue
            file_name = span.get("file_name") or ""
            try:
                line_start = max(0, int(span.get("line_start", 1)) - 1)
                col_start = max(0, int(span.get("column_start", 1)) - 1)
            except (TypeError, ValueError):
                continue
            path = Path(file_name)
            if not path.is_absolute():
                path = (root / path).resolve()
            out.append(
                Diagnostic(
                    uri=path_to_uri(path),
                    severity=severity,
                    line=line_start,
                    character=col_start,
                    message=message_text,
                    code=code,
                    source=source,
                )
            )
    return out


def _autodetect_parser(command: tuple[str, ...]) -> str:
    """Pick a parser by inspecting the command line."""
    for arg in command:
        if "--message-format=json" in arg or arg == "--json":
            return "cargo-json"
    return "text"


# ─── runners ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CompileSpec:
    """One language ↔ one compile command."""

    language: str               # extension, e.g. "rs", "ts"
    command: tuple[str, ...]
    cwd: Optional[str] = None   # default: project root
    parser: Optional[str] = None  # "cargo-json" / "text" / None=auto
    debounce_seconds: float = DEFAULT_DEBOUNCE_S
    run_timeout_s: float = DEFAULT_RUN_TIMEOUT_S
    source_label: Optional[str] = None  # appears in Diagnostic.source

    def resolve_parser(self) -> str:
        return self.parser or _autodetect_parser(self.command)

    def resolve_source_label(self) -> str:
        if self.source_label:
            return self.source_label
        # First arg of the command makes a fine default
        # ("cargo" / "tsc" / "mypy"), strip path prefix if any.
        return Path(self.command[0]).name if self.command else "compile"


@dataclass
class CompileRunner:
    """Owns the debounced run loop for one language.

    Internal state is the per-path diagnostics map produced by the
    most recent run. Callers read it via ``get_diagnostics(path)``;
    we replace the whole map on each run, matching LSP semantics
    (the latest publish is the truth).
    """

    spec: CompileSpec
    project_root: Path

    _diagnostics: dict[str, list[Diagnostic]] = field(default_factory=dict)
    _diag_lock: threading.Lock = field(default_factory=threading.Lock)
    _trigger_event: threading.Event = field(default_factory=threading.Event)
    _stop_event: threading.Event = field(default_factory=threading.Event)
    _thread: Optional[threading.Thread] = None
    _last_run_at: float = 0.0
    _last_returncode: Optional[int] = None
    _last_stderr: str = ""

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("CompileRunner already started")
        self._thread = threading.Thread(
            target=self._loop,
            name=f"lsp-engine-compile-{self.spec.language}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._trigger_event.set()  # wake the loop so it can exit
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def trigger(self) -> None:
        """Schedule a debounced run. Multiple calls within the
        debounce window collapse into one run after the window
        elapses with no further triggers."""
        self._trigger_event.set()

    def get_diagnostics(self, abs_path: str) -> list[Diagnostic]:
        key = path_to_uri(abs_path)
        with self._diag_lock:
            return list(self._diagnostics.get(key, []))

    def all_diagnostics(self) -> dict[str, list[Diagnostic]]:
        with self._diag_lock:
            return {k: list(v) for k, v in self._diagnostics.items()}

    @property
    def last_returncode(self) -> Optional[int]:
        return self._last_returncode

    # ─── internals ───────────────────────────────────────────────────

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            # Wait for an initial trigger.
            if not self._trigger_event.wait(timeout=1.0):
                continue
            if self._stop_event.is_set():
                return
            # Debounce: clear the trigger, sleep the debounce window;
            # if more triggers arrive, restart the window.
            self._trigger_event.clear()
            deadline = self._now() + self.spec.debounce_seconds
            while self._now() < deadline:
                remaining = deadline - self._now()
                if self._trigger_event.wait(timeout=remaining):
                    self._trigger_event.clear()
                    deadline = self._now() + self.spec.debounce_seconds
                    if self._stop_event.is_set():
                        return
            # Execute.
            try:
                self._run_once()
            except Exception:  # pragma: no cover — defensive
                log.exception(
                    "compile runner %s crashed", self.spec.language,
                )

    @staticmethod
    def _now() -> float:
        import time
        return time.monotonic()

    def _run_once(self) -> None:
        cwd = self.spec.cwd or str(self.project_root)
        log.info(
            "compile: running %s for .%s",
            " ".join(self.spec.command), self.spec.language,
        )
        try:
            proc = subprocess.run(
                list(self.spec.command),
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=self.spec.run_timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired:
            log.warning(
                "compile: %s timed out after %.0fs",
                self.spec.command[0], self.spec.run_timeout_s,
            )
            return
        except OSError as e:
            log.warning(
                "compile: failed to invoke %s: %s",
                self.spec.command[0], e,
            )
            return

        self._last_returncode = proc.returncode
        self._last_stderr = (proc.stderr or "")[-2000:]

        diags = self._parse_output(proc.stdout or "", proc.stderr or "")
        # Key by the URI itself (not a path string) to avoid
        # path-shape ambiguity on Windows: ``_uri_to_path_str`` returns
        # ``/C:/foo/bar`` on Windows but callers query with native
        # ``C:\foo\bar``, and the two miss each other in the dict
        # despite naming the same file. URIs are canonical — pyright
        # and friends already use them as the source of truth.
        new_map: dict[str, list[Diagnostic]] = {}
        for d in diags:
            new_map.setdefault(d.uri, []).append(d)
        with self._diag_lock:
            self._diagnostics = new_map
        log.info(
            "compile: %s emitted %d diagnostics across %d files",
            self.spec.language, len(diags), len(new_map),
        )

    def _parse_output(self, stdout: str, stderr: str) -> list[Diagnostic]:
        parser = self.spec.resolve_parser()
        source = self.spec.resolve_source_label()
        if parser == "cargo-json":
            return parse_cargo_json_output(
                stdout, self.project_root, source=source,
            )
        # Generic text parser: try stdout first, then fall back to
        # stderr (gcc / clang / tsc print diagnostics there). Some
        # tools split: warnings on stdout, errors on stderr — combine.
        return (
            parse_text_output(stdout, self.project_root, source=source)
            + parse_text_output(stderr, self.project_root, source=source)
        )


def _uri_to_path_str(uri: str) -> str:
    if not uri.startswith("file://"):
        raise ValueError(f"not a file URI: {uri}")
    from urllib.parse import unquote, urlparse
    parsed = urlparse(uri)
    return unquote(parsed.path)


# ─── orchestrator ────────────────────────────────────────────────────


class CompileOrchestrator:
    """Routes ``did_open`` / ``did_change`` notifications to the
    matching ``CompileRunner`` by file extension. One runner per
    language.

    Constructed by the daemon when ``compile_aware.enabled`` is true.
    Soft no-op when the spec list is empty (user enabled the flag
    but configured no commands — we don't want to fail the daemon
    over a config oversight).
    """

    def __init__(
        self,
        project_root: str | os.PathLike,
        specs: list[CompileSpec],
    ) -> None:
        self._project_root = Path(project_root).resolve()
        self._runners: dict[str, CompileRunner] = {}
        for spec in specs:
            ext = spec.language.lower().lstrip(".")
            self._runners[ext] = CompileRunner(
                spec=spec, project_root=self._project_root,
            )

    @classmethod
    def from_engine_config(
        cls,
        project_root: str | os.PathLike,
        commands: dict[str, tuple[str, ...]],
        *,
        debounce_seconds: float = DEFAULT_DEBOUNCE_S,
    ) -> "CompileOrchestrator":
        specs = [
            CompileSpec(
                language=ext,
                command=tuple(cmd),
                debounce_seconds=debounce_seconds,
            )
            for ext, cmd in commands.items()
        ]
        return cls(project_root, specs)

    def start(self) -> None:
        for runner in self._runners.values():
            runner.start()

    def stop(self) -> None:
        for runner in self._runners.values():
            runner.stop()

    def notify_change(self, path: str | os.PathLike) -> None:
        ext = Path(path).suffix.lower().lstrip(".")
        runner = self._runners.get(ext)
        if runner is not None:
            runner.trigger()

    def get_diagnostics(self, path: str | os.PathLike) -> list[Diagnostic]:
        abs_path = str(Path(path).resolve())
        out: list[Diagnostic] = []
        for runner in self._runners.values():
            out.extend(runner.get_diagnostics(abs_path))
        return out

    def force_run_all(self) -> None:
        """Test seam: trigger every runner regardless of language.
        Useful for tests that don't want to wait for ``notify_change``
        debouncing on a specific extension."""
        for runner in self._runners.values():
            runner.trigger()

    def runners(self) -> dict[str, CompileRunner]:
        return dict(self._runners)
