"""deltr/bus.py — tiny in-process event log / pub-sub.

One ``EventBus`` per engine.  ``publish`` is synchronous (safe to call from any
coroutine or plain function on the event-loop thread), appends the event to a
bounded history deque and fans it out to every subscriber queue.  A slow
subscriber never blocks a publisher: when its queue is full the OLDEST queued
event is dropped so the consumer always sees the most recent activity.

Consumers: ``/ws/stream`` (one queue per socket) and ``State.snapshot()`` (reads
``history``).  Nothing here performs I/O and nothing prints to stdout.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Any, Optional

from deltr.models import AgentEvent

log = logging.getLogger("deltr.bus")

DEFAULT_QUEUE_MAXSIZE = 500


class EventBus:
    """Synchronous publish, per-subscriber ``asyncio.Queue`` fan-out, bounded history."""

    def __init__(self, history: int = 200, queue_maxsize: int = DEFAULT_QUEUE_MAXSIZE) -> None:
        self._history: deque[AgentEvent] = deque(maxlen=max(1, int(history)))
        self._queues: list[asyncio.Queue[AgentEvent]] = []
        self._queue_maxsize = max(1, int(queue_maxsize))
        self.published: int = 0
        self.dropped: int = 0

    # ------------------------------------------------------------------ publish
    def publish(
        self, topic: str, message: str, level: str = "info", data: Optional[dict[str, Any]] = None
    ) -> AgentEvent:
        """Create an ``AgentEvent``, append it to history and fan it out (drop-oldest on full queues)."""
        ev = AgentEvent(topic=topic, level=level, message=message, data=dict(data or {}))  # type: ignore[arg-type]
        self._history.append(ev)
        self.published += 1
        for q in list(self._queues):
            if q.full():
                try:
                    q.get_nowait()
                    self.dropped += 1
                except asyncio.QueueEmpty:  # pragma: no cover - racy but harmless
                    pass
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:  # pragma: no cover - cannot happen after the drop above
                self.dropped += 1
        return ev

    # ---------------------------------------------------------------- subscribe
    def subscribe(self) -> "asyncio.Queue[AgentEvent]":
        """Return a fresh bounded queue that receives every event published from now on."""
        q: asyncio.Queue[AgentEvent] = asyncio.Queue(maxsize=self._queue_maxsize)
        self._queues.append(q)
        return q

    def unsubscribe(self, q: "asyncio.Queue[AgentEvent]") -> None:
        """Detach a queue; unknown queues are ignored."""
        try:
            self._queues.remove(q)
        except ValueError:
            pass

    @property
    def subscribers(self) -> int:
        return len(self._queues)

    # ------------------------------------------------------------------ history
    def history(self, n: int = 100) -> list[AgentEvent]:
        """The last ``n`` events, oldest first."""
        if n <= 0:
            return []
        items = list(self._history)
        return items[-n:]

    def clear(self) -> None:
        self._history.clear()


__all__ = ["EventBus", "DEFAULT_QUEUE_MAXSIZE"]
