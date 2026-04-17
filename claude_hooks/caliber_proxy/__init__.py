"""Caliber grounding proxy: OpenAI-compatible endpoint that wraps Ollama
with agent-style tool use so local models (e.g. gemma4-98e) can ground
their output in the real project files — mirroring how Claude Code's CLI
grounds itself via internal tool calls.

See ``server.py`` for the entry point.
"""

__version__ = "0.1.0"
