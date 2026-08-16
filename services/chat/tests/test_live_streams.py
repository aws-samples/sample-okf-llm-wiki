"""The live-stream registry — the core of stream resume.

Proves the load-bearing properties: a run keeps going after a subscriber detaches
(disconnect ≠ stop); a late subscriber replays the backlog then streams live with
no gap/dup; explicit cancel stops the run and fires on_cancel; and a finished run's
subscriber just gets the full backlog.
"""

from __future__ import annotations

import asyncio

import pytest

from chat import live_streams


@pytest.fixture(autouse=True)
def _reset():
    live_streams.reset()
    yield
    live_streams.reset()


async def _gated_source(items, gate: asyncio.Event):
    """Yield each item, awaiting `gate` before the LAST one so a test can observe
    the mid-stream (not-yet-done) state deterministically."""
    for i, it in enumerate(items):
        if i == len(items) - 1:
            await gate.wait()
        yield it


def _collect(agen):
    async def run():
        return [c async for c in agen]

    return run


def test_run_continues_after_subscriber_detaches():
    async def main():
        gate = asyncio.Event()
        live_streams.start(
            "k1", _gated_source([{"a": 1}, {"a": 2}, {"end": True}], gate)
        )
        # Subscribe, take ONE chunk, then abandon (simulate disconnect).
        sub = live_streams.subscribe("k1")
        first = await sub.__anext__()
        await sub.aclose()
        assert first == {"a": 1}
        # The run is still active (not cancelled by the detach).
        assert live_streams.is_active("k1")
        # Let it finish.
        gate.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not live_streams.is_active("k1")

    asyncio.run(main())


def test_late_subscriber_replays_backlog_then_streams_live():
    async def main():
        gate = asyncio.Event()
        live_streams.start(
            "k2", _gated_source([{"t": "a"}, {"t": "b"}, {"end": True}], gate)
        )
        # Let the first two chunks buffer (gate still closed → last chunk blocked).
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        # A fresh subscriber (reconnect) should get the backlog THEN the live tail.
        got = []
        sub = live_streams.subscribe("k2")

        async def drain():
            async for c in sub:
                got.append(c)

        task = asyncio.create_task(drain())
        await asyncio.sleep(0)  # replay backlog
        gate.set()  # release the final chunk
        await task
        # Backlog (a, b) then the live end — each exactly once, in order.
        assert got == [{"t": "a"}, {"t": "b"}, {"end": True}]

    asyncio.run(main())


def test_finished_run_subscriber_gets_full_backlog():
    async def main():
        gate = asyncio.Event()
        gate.set()  # no blocking — runs to completion immediately
        live_streams.start("k3", _gated_source([{"x": 1}, {"end": True}], gate))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not live_streams.is_active("k3")
        # Subscribing to a DONE run still replays the whole buffer.
        got = await _collect(live_streams.subscribe("k3"))()
        assert got == [{"x": 1}, {"end": True}]

    asyncio.run(main())


def test_cancel_stops_run_and_fires_on_cancel():
    async def main():
        gate = asyncio.Event()  # never set → the run blocks forever without cancel
        fired = []

        def on_cancel():
            fired.append(True)
            return [{"type": "tool", "error": True}, {"end": True, "cancelled": True}]

        live_streams.start(
            "k4",
            _gated_source([{"t": "hi"}, {"end": True}], gate),
            on_cancel=on_cancel,
        )
        await asyncio.sleep(0)  # let the first chunk buffer
        assert live_streams.is_active("k4")

        stopped = await live_streams.cancel("k4")
        assert stopped is True
        assert fired == [True]  # on_cancel ran
        assert not live_streams.is_active("k4")

        # The cancelled chunks were published to the buffer (a subscriber sees them).
        got = await _collect(live_streams.subscribe("k4"))()
        assert {"end": True, "cancelled": True} in got
        assert any(c.get("type") == "tool" and c.get("error") for c in got)

    asyncio.run(main())


def test_cancel_returns_false_when_nothing_active():
    async def main():
        assert await live_streams.cancel("nope") is False

    asyncio.run(main())


def test_start_twice_does_not_double_run():
    async def main():
        gate = asyncio.Event()
        s1 = live_streams.start("k5", _gated_source([{"n": 1}, {"end": True}], gate))
        # A second start for the SAME active key returns the SAME stream (no 2nd task).
        s2 = live_streams.start("k5", _gated_source([{"n": 99}, {"end": True}], gate))
        assert s1 is s2
        gate.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(main())
