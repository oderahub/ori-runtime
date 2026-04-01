# Copyright 2026 Ori Nexus Systems LTD
# SPDX-License-Identifier: Apache-2.0

import inspect
import logging
from collections import defaultdict
from typing import Awaitable, Callable

from ori.network.events import OriEvent

logger = logging.getLogger(__name__)

Handler = Callable[[OriEvent], Awaitable[None]]

_WILDCARD = "*"


class EventBus:
    """Asyncio pub/sub bus. Skills subscribe; the runtime publishes.

    Subscriptions are keyed on ``sensor_type`` (e.g. ``'current_clamp'``).
    The special key ``'*'`` receives every published event regardless of type.

    Delivery is direct — handlers are awaited inline inside :meth:`publish`,
    not queued.  Handler exceptions are caught, logged, and never allowed to
    interrupt delivery to other subscribers.

    Skill handlers that perform I/O (LLM calls, network requests, GPIO)
    must wrap that work in ``asyncio.create_task()`` to avoid blocking
    delivery to subsequent handlers.  The EventBus dispatches synchronously
    by design — handlers are responsible for yielding control.
    """

    def __init__(self) -> None:
        # sensor_type → list of handlers (preserves registration order)
        self._subscribers: dict[str, list[Handler]] = defaultdict(list)

    def subscribe(self, sensor_type: str, handler: Handler) -> None:
        """Register *handler* to receive events of *sensor_type*.

        Args:
            sensor_type: The ``OriEvent.reading.sensor_type`` value to match,
                or ``'*'`` to receive all events.
            handler: An async callable that accepts a single :class:`OriEvent`.
        """
        if not inspect.iscoroutinefunction(handler):
            logger.warning(
                "Handler %s registered for '%s' is not async. "
                "Wrap it with asyncio or it will fail silently.",
                handler.__name__,
                sensor_type,
            )
        self._subscribers[sensor_type].append(handler)

    def unsubscribe(self, sensor_type: str, handler: Handler) -> None:
        """Remove a previously registered *handler*.

        Silently does nothing if *handler* is not registered for *sensor_type*.
        """
        handlers = self._subscribers.get(sensor_type)
        if handlers and handler in handlers:
            handlers.remove(handler)

    def subscriber_count(self, sensor_type: str) -> int:
        """Return the number of handlers registered for *sensor_type*."""
        return len(self._subscribers.get(sensor_type, []))

    def clear(self) -> None:
        """Remove all subscriptions."""
        self._subscribers.clear()

    async def publish(self, event: OriEvent) -> None:
        """Deliver *event* to every matching subscriber and to wildcard handlers.

        Matching is by ``event.reading.sensor_type`` when a reading is present,
        otherwise by ``event.event_type``.  Wildcard (``'*'``) handlers always
        receive the event.

        Exceptions raised by individual handlers are caught and logged so that
        a misbehaving handler never prevents delivery to other subscribers.

        Logs a warning when no handlers are registered for the event.
        """
        sensor_type = (
            event.reading.sensor_type if event.reading is not None else event.event_type
        )

        specific = list(self._subscribers.get(sensor_type, []))
        wildcards = list(self._subscribers.get(_WILDCARD, []))
        targets = specific + [h for h in wildcards if h not in specific]

        if not targets:
            logger.warning(
                "EventBus: no subscribers for sensor_type=%r (event_id=%s)",
                sensor_type,
                event.event_id,
            )
            return

        for handler in targets:
            try:
                await handler(event)
            except Exception:
                logger.exception(
                    "EventBus: handler %r raised an exception for event_id=%s",
                    handler,
                    event.event_id,
                )
