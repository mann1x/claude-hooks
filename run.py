#!/usr/bin/env python3
"""
Cross-platform entry point for claude-hooks.

Invoked from settings.json hook commands as:

    python3 /path/to/claude-hooks/run.py <EventName>
    python  C:\\path\\to\\claude-hooks\\run.py <EventName>

Reads the event JSON from stdin, dispatches to the matching handler, and
writes any hook output to stdout.
"""

from __future__ import annotations

import os
import sys


def main() -> int:
    # Make the repo's claude_hooks/ package importable without needing pip
    # install or PYTHONPATH gymnastics.
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)

    if len(sys.argv) < 2:
        sys.stderr.write("usage: run.py <EventName>\n")
        return 0  # never block Claude — exit 0 even on misuse

    event_name = sys.argv[1]

    from claude_hooks.dispatcher import dispatch, read_event_from_stdin

    event = read_event_from_stdin()
    return dispatch(event_name, event)


if __name__ == "__main__":
    raise SystemExit(main())
