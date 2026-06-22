"""Shared fixtures for the conformance test suite.

Re-exports fixtures from tests.router.conftest so that conformance tests
can use them without duplicating the implementations (D-10).
"""
from __future__ import annotations

# Re-export fixtures from the router conftest so pytest discovers them
# in tests/conformance/. The implementations live in tests/router/conftest.py.
from tests.router.conftest import FakeEngine, fake_engine, make_event  # noqa: F401
