"""Shared fixtures for the chat service tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_live_streams():
    """The live-stream registry is module-level global state (in-flight runs for
    resume). Reset it before AND after each test so a run started in one test can't
    leak into another."""
    from chat import live_streams

    live_streams.reset()
    yield
    live_streams.reset()
