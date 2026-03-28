"""
tests/conftest.py
=================
Pytest configuration for the CERBERUS test suite.
Sets asyncio_mode=auto so all async tests work without explicit marks.
"""
import pytest


def pytest_configure(config):
    """Register asyncio_mode=auto globally."""
    config.addinivalue_line(
        "markers",
        "asyncio: mark test as async (handled automatically)",
    )
