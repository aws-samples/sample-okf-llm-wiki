"""In-process live-stream registry — decouples an agent RUN from the HTTP request.

Sparky-style stream resume: a chat turn runs as a detached ``asyncio`` task that
buffers every typed chunk in a registry keyed by the (per-user namespaced) thread
id. The HTTP ``/invocations`` response merely SUBSCRIBES to that buffer — so:

- **Disconnect ≠ stop.** If the browser navigates away, the subscribing HTTP
  generator is torn down but the runner task keeps going, still buffering. A later
  ``resume`` request re-subscribes: it replays the buffered chunks (what the client
  missed) then streams the rest live.
- **Only an explicit stop cancels.** ``cancel()`` cancels the runner task, which
  triggers the checkpoint reconcile (see chat.cancellation) — this is the sole
  path that ends a run early, replacing the old "disconnect cancels" behaviour.

Concurrency is lock-free by design: append-to-list and ``Queue.put_nowait`` are
atomic between ``await`` points in asyncio, and :func:`subscribe` snapshots the
backlog and registers its queue with NO ``await`` between the two — so a chunk is
never both replayed AND delivered live (no dup), and never missed (no gap).

CAVEAT (AgentCore): the buffer lives in the microVM's memory. Resume relies on
session affinity routing the reconnect to the same warm VM; a cold-start/recycle
between disconnect and reconnect loses the LIVE stream (the conversation itself is
safe in the DynamoDB checkpointer — only the in-flight replay is best-effort).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Awaitable, Callable

# Sentinel pushed to each subscriber queue when the run finishes, so a live
# subscriber's ``await queue.get()`` loop knows to stop (distinct from any chunk).
_SENTINEL = object()


@dataclass
class LiveStream:
    """One in-flight (or just-finished) agent run and its subscribers."""

    key: str
    chunks: list[dict[str, Any]] = field(default_factory=list)  # buffered, for replay
    subscribers: set[asyncio.Queue] = field(default_factory=set)
    user_message: str = ""
    done: bool = False
    task: asyncio.Task | None = None
    # Called (sync) if the runner is cancelled; returns extra chunks to publish
    # (the checkpoint-repair "cancelled" tool chunks + a cancelled end marker).
    on_cancel: Callable[[], list[dict[str, Any]]] | None = None


# The registry, keyed by internal (sub-namespaced) thread id. One entry per thread;
# a new run for a key replaces the previous (done) entry.
_active: dict[str, LiveStream] = {}


def reset() -> None:
    """Drop all tracked streams (test isolation)."""
    _active.clear()


def get(key: str) -> LiveStream | None:
    return _active.get(key)


def is_active(key: str) -> bool:
    """True iff a run for ``key`` exists and hasn't finished (resume-able / live)."""
    s = _active.get(key)
    return bool(s and not s.done)


def _publish(stream: LiveStream, chunk: dict[str, Any]) -> None:
    """Buffer a chunk and fan it out to every current subscriber. Sync + atomic."""
    stream.chunks.append(chunk)
    for q in stream.subscribers:
        q.put_nowait(chunk)


def _finish(stream: LiveStream) -> None:
    """Mark the run done and signal every subscriber to end (sentinel)."""
    stream.done = True
    for q in stream.subscribers:
        q.put_nowait(_SENTINEL)


async def _runner(stream: LiveStream, source: AsyncGenerator[dict[str, Any], None]) -> None:
    """Drive the chunk source, publishing each chunk; handle stop + always finish.

    The source (``_produce_run_chunks``) yields raw chunk dicts including its own
    terminal ``{"end": …}`` on success and an ``error`` chunk on failure — so only
    CANCELLATION (explicit stop → ``task.cancel()``) is handled here: run the
    injected ``on_cancel`` (checkpoint repair + cancelled end) then let the
    CancelledError propagate. ``_finish`` always runs so no subscriber hangs.
    """
    try:
        async for chunk in source:
            _publish(stream, chunk)
    except asyncio.CancelledError:
        if stream.on_cancel is not None:
            try:
                for chunk in stream.on_cancel():
                    _publish(stream, chunk)
            except Exception:  # noqa: BLE001 - repair is best-effort; never mask stop
                pass
        raise
    finally:
        _finish(stream)


def start(
    key: str,
    source: AsyncGenerator[dict[str, Any], None],
    *,
    user_message: str = "",
    on_cancel: Callable[[], list[dict[str, Any]]] | None = None,
) -> LiveStream:
    """Register a new run for ``key`` and spawn its detached runner task.

    Replaces any prior (finished) stream for the key. Requires a running event loop
    (called from the async request handler).
    """
    prev = _active.get(key)
    if prev is not None and prev.task is not None and not prev.task.done():
        # An active run already exists for this key — don't double-run. Return it so
        # the caller subscribes to the existing run instead (reconnect / double-send).
        return prev

    stream = LiveStream(key=key, user_message=user_message, on_cancel=on_cancel)
    _active[key] = stream
    stream.task = asyncio.create_task(_runner(stream, source))
    return stream


async def subscribe(key: str) -> AsyncGenerator[dict[str, Any], None]:
    """Yield a run's chunks: the buffered backlog first, then live until it ends.

    Registering the subscriber queue and snapshotting the backlog happen with NO
    ``await`` between them, so the handoff from replay to live is exactly seamless
    (no gap, no dup). Yields nothing if the key isn't registered.
    """
    stream = _active.get(key)
    if stream is None:
        return

    q: asyncio.Queue = asyncio.Queue()
    # --- atomic region (no await): snapshot backlog + capture done + register ---
    backlog = list(stream.chunks)
    already_done = stream.done
    stream.subscribers.add(q)
    # ---------------------------------------------------------------------------
    try:
        for chunk in backlog:
            yield chunk
        if already_done:
            return  # finished before we registered — backlog is the whole story
        while True:
            item = await q.get()
            if item is _SENTINEL:
                break
            yield item
    finally:
        stream.subscribers.discard(q)


async def cancel(key: str) -> bool:
    """Explicit stop: cancel the run's task (triggers on_cancel + checkpoint repair).

    Returns True if a live run was cancelled, False if there was nothing running.
    Awaits the task's teardown so the reconcile is scheduled before we return.
    """
    stream = _active.get(key)
    if stream is None or stream.task is None or stream.task.done():
        return False
    stream.task.cancel()
    try:
        await stream.task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001 - teardown, expected
        pass
    return True
