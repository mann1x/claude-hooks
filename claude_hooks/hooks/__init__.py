"""Hook event handlers for claude-hooks.

Each module exposes a ``handle(event, config, providers) -> dict | None``
function. The dispatcher imports them lazily by name.
"""
