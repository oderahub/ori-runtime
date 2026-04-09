import time
from dataclasses import dataclass, field
from typing import Any, Optional

from ori.network.events import OriEvent


class HookHistoryAdapter:
    """Synchronous adapter to wrap StateStore for skill hooks."""
    def __init__(self, store: Any):
        self._store = store

    def avg_1h(self, sensor_id: str) -> Optional[float]:
        if not self._store:
            return None
        return self._store._avg_last_hours_sync(sensor_id, 1)

    def last_value(self, sensor_id: str) -> Optional[float]:
        if not self._store:
            return None
        history = self._store._get_history_sync(sensor_id, 1)
        if history:
            return history[0].value
        return None

    def last_timestamp(self, sensor_id: str) -> Optional[int]:
        if not self._store:
            return None
        history = self._store._get_history_sync(sensor_id, 1)
        if history:
            return history[0].timestamp
        return None


@dataclass
class HookContext:
    """Context block provided to synchronous skill hooks."""
    trigger_name: str
    readings: dict[str, Any]
    history: HookHistoryAdapter
    timestamp: int
    derived: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def build(cls, event: OriEvent, store: Any) -> "HookContext":
        readings = {}
        if event and event.reading:
            readings[event.reading.sensor_id] = event.reading.value
            # Also spread meta into readings for easy trigger addressing
            if isinstance(event.reading.metadata, dict):
                readings.update(event.reading.metadata)

        return cls(
            trigger_name="",
            readings=readings,
            history=HookHistoryAdapter(store),
            timestamp=event.timestamp if event else int(time.time() * 1000)
        )
