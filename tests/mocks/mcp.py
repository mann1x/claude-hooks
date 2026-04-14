"""
Convenience re-export so tests can do::

    from tests.mocks.mcp import FakeMcpProvider

Most handler tests take providers via the ``fake_provider`` fixture in
``conftest.py``; this module exists so imports from ``tests.mocks.mcp``
work for tests that prefer direct instantiation.
"""

from tests.conftest import FakeProvider as FakeMcpProvider  # noqa: F401

__all__ = ["FakeMcpProvider"]
