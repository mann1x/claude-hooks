"""Tell the caller when this process is serving code older than the disk.

``python -m claude_hooks.lsp_mcp`` imports the package once and holds that
code for the life of the process. Upgrading underneath it — ``pip install
-e .``, a ``git pull``, a ``scripts/deploy.py`` run — does not reach a
server that is already running, and neither does ``restart_server``, which
restarts the *language servers* rather than this Python process. The
symptom is a fix that appears not to work, which is indistinguishable from
a fix that does not work.

Observed 2026-09-16: a ``did_open`` fix landed at 13:37; a peer session
whose MCP server had started at 12:56 could only verify it by driving
``Engine`` directly in a fresh interpreter. A client restart at 15:24
picked it up. The cost was never the staleness — it was not knowing.

Two signals, because either alone has a blind spot:

1. **Version.** ``claude_hooks.__version__`` resolves from the live
   ``pyproject.toml`` at import (see ``_resolve_version``), so calling the
   resolver again now re-reads disk. A differing pair is unambiguous.
2. **Source mtime.** A version bump only happens at a release cut. The
   incident above was ``f3c4bd3``, a 12-file fix that did not touch
   ``pyproject.toml`` — so a version-only check would have stayed silent
   through the exact case that motivated this. Any ``.py`` under the
   package whose mtime is newer than our import is the honest signal.

Deliberately an **announcement, not an automatic re-exec.** A re-exec
would drop in-flight language servers (including a warm clangd index) and
the client's ``initialize`` state, so two identical tool calls would
return differently for reasons the caller cannot see. That is the same
failure class as "no diagnostics" meaning both *clean* and *never parsed*,
and it cannot be fixed by adding another instance of it.

The notice goes in the **tool output**, not only the log: a log nobody
reads is how a wrong clangd survived for months here. It is emitted once
per session, on the first affected call — a banner on every ``get_hover``
becomes noise, and noise is how a real warning gets filtered out.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable, NamedTuple, Optional

import claude_hooks

log = logging.getLogger("claude_hooks.lsp_mcp.staleness")

#: Re-walking the package on every tool call would be wasteful, and the
#: answer cannot change usefully between two calls a second apart. Once
#: the notice has been emitted the walk stops entirely.
DEFAULT_MIN_RECHECK_SECONDS = 30.0

#: Escape hatch for ops and for tests that exercise the dispatch path
#: without wanting the banner in their expected output.
ENV_DISABLE = "LSP_MCP_STALENESS_CHECK"

_DOC_POINTER = (
    'docs/lsp-engine.md → "You shipped a fix and the behaviour did not '
    'change"'
)


class Report(NamedTuple):
    """What we found, in a form a caller can render or assert on."""

    #: ``"version"`` or ``"source"`` — which signal fired. Version is
    #: reported preferentially because it is the more legible of the two.
    reason: str
    imported_version: str
    installed_version: str
    #: Set only for ``reason == "source"``.
    changed_path: Optional[Path] = None
    changed_mtime: Optional[float] = None


def _default_installed_version() -> str:
    """Re-resolve the on-disk version.

    ``_resolve_version`` reads the live ``pyproject.toml`` on every call,
    which is precisely the property we need: ``claude_hooks.__version__``
    is frozen at our import, this is not.
    """
    try:
        return claude_hooks._resolve_version()  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover - defensive
        # A broken resolver must not take the tool call down with it.
        return claude_hooks.__version__


class StalenessDetector:
    """Compares the running process against the package on disk.

    Constructor arguments are all injectable so the tests can build a
    detector over a temporary tree rather than mutating global state.
    """

    def __init__(
        self,
        pkg_root: Path,
        imported_version: str,
        import_time: float,
        installed_version_fn: Callable[[], str] = _default_installed_version,
        min_recheck_seconds: float = DEFAULT_MIN_RECHECK_SECONDS,
    ) -> None:
        self.pkg_root = Path(pkg_root)
        self.imported_version = imported_version
        self.import_time = import_time
        self._installed_version_fn = installed_version_fn
        self._min_recheck = min_recheck_seconds

        self._lock = threading.Lock()
        self._announced = False
        self._last_check = 0.0
        self._cached: Optional[Report] = None

    # -- state ---------------------------------------------------------

    @property
    def announced(self) -> bool:
        """True once the notice has been handed to a caller."""
        return self._announced

    def _disabled(self) -> bool:
        return os.environ.get(ENV_DISABLE, "").strip() in {"0", "false", "no"}

    # -- detection -----------------------------------------------------

    def _newest_source(self) -> Optional[tuple[Path, float]]:
        """The most recently modified ``.py`` under the package, if any
        of them post-dates our import.

        ``.pyc`` is deliberately not consulted: a recompile does not mean
        the source changed, and the source is what we were asked about.
        """
        newest: Optional[tuple[Path, float]] = None
        try:
            for path in self.pkg_root.rglob("*.py"):
                if "__pycache__" in path.parts:
                    continue
                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    # Vanished mid-walk (a deploy in progress). Skip it
                    # rather than fail the tool call that triggered us.
                    continue
                if mtime <= self.import_time:
                    continue
                if newest is None or mtime > newest[1]:
                    newest = (path, mtime)
        except OSError:  # pragma: no cover - defensive
            return None
        return newest

    def check(self, now: Optional[float] = None) -> Optional[Report]:
        """Return a :class:`Report` if this process is stale, else None.

        Throttled, and inert once the notice has been announced.
        """
        if self._disabled():
            return None
        now = time.time() if now is None else now
        with self._lock:
            if self._announced:
                return None
            if self._cached is not None:
                return self._cached
            if now - self._last_check < self._min_recheck:
                return None
            self._last_check = now

            installed = self._installed_version_fn()
            if installed != self.imported_version:
                self._cached = Report(
                    reason="version",
                    imported_version=self.imported_version,
                    installed_version=installed,
                )
                return self._cached

            newest = self._newest_source()
            if newest is not None:
                self._cached = Report(
                    reason="source",
                    imported_version=self.imported_version,
                    installed_version=installed,
                    changed_path=newest[0],
                    changed_mtime=newest[1],
                )
                return self._cached
        return None

    # -- rendering -----------------------------------------------------

    def _render(self, report: Report) -> str:
        started = time.strftime("%Y-%m-%d %H:%M:%S",
                                time.localtime(self.import_time))
        lines = [
            "⚠  claude-hooks-lsp is serving code older than the tree on "
            "disk.",
            f"   imported: {report.imported_version} "
            f"(this process, started {started})",
        ]
        if report.reason == "version":
            lines.append(f"   on disk:  {report.installed_version}")
        else:
            changed = time.strftime("%Y-%m-%d %H:%M:%S",
                                    time.localtime(report.changed_mtime or 0))
            try:
                rel: str = str(
                    (report.changed_path or Path()).relative_to(
                        self.pkg_root.parent)
                )
            except ValueError:  # pragma: no cover - defensive
                rel = str(report.changed_path)
            lines.append(
                f"   on disk:  {report.installed_version} (same version, but "
                f"{rel} changed at {changed}, after this process imported it)"
            )
        lines += [
            "",
            "   Restart the MCP client — this Claude Code session — to pick "
            "it up.",
            "   restart_server restarts the language servers, not this "
            "process,",
            "   so it will not help here.",
            "",
            f"   Shown once per session. See {_DOC_POINTER}.",
        ]
        return "\n".join(lines)

    def banner(self, now: Optional[float] = None) -> Optional[str]:
        """The notice, exactly once per process. None afterwards."""
        report = self.check(now=now)
        if report is None:
            return None
        with self._lock:
            if self._announced:
                return None
            self._announced = True
        text = self._render(report)
        # Log it too. The tool output is what reaches the agent, but an
        # operator reading the daemon log deserves the same fact.
        log.warning("stale process: %s", text.replace("\n", " "))
        return text


#: Captured as early as possible: this module is imported during server
#: start-up, so it stands in for the process's own start time.
IMPORT_TIME = time.time()

DETECTOR = StalenessDetector(
    pkg_root=Path(claude_hooks.__file__).resolve().parent,
    imported_version=claude_hooks.__version__,
    import_time=IMPORT_TIME,
)


def banner() -> Optional[str]:
    """Module-level convenience over :data:`DETECTOR`."""
    return DETECTOR.banner()
