import time
from dataclasses import dataclass, field
from typing import Any, Optional

from ori.network.events import OriEvent


class HookHistoryAdapter:
    """Synchronous adapter to wrap StateStore for skill hooks."""
    def __init__(self, store: Any):
        self._store = store

    def avg_hours(self, sensor_id: str, hours: int) -> Optional[float]:
        if not self._store:
            return None
        return self._store._avg_last_hours_sync(sensor_id, hours)

    def avg_last_n(self, sensor_id: str, n: int) -> Optional[float]:
        if not self._store:
            return None
        return self._store._avg_last_n_sync(sensor_id, n)

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

    def fetch_history(self, sensor_id: str, limit: int = 1) -> list[dict[str, Any]]:
        if not self._store:
            return []
        history = self._store._get_history_sync(sensor_id, limit)
        return [
            {
                "sensor_id": r.sensor_id,
                "sensor_type": r.sensor_type,
                "value": r.value,
                "unit": r.unit,
                "timestamp": r.timestamp,
                "quality": r.quality,
                "metadata": r.metadata,
            }
            for r in history
        ]


class HookStateAdapter:
    """Provides key-value persistence specifically isolated to the active skill."""
    def __init__(self, store: Any, skill_name: str):
        self._store = store
        self._skill_name = skill_name

    def get(self, key: str) -> Optional[str]:
        if not self._store or not self._skill_name:
            return None
        return self._store._get_skill_state_sync(self._skill_name, key)

    def set(self, key: str, value: str) -> None:
        if not self._store or not self._skill_name:
            return
        self._store._set_skill_state_sync(self._skill_name, key, value)


@dataclass
class HookContext:
    """Context block provided to synchronous skill hooks."""
    trigger_name: str
    readings: dict[str, Any]
    history: HookHistoryAdapter
    state: HookStateAdapter
    timestamp: int
    derived: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def build(cls, event: OriEvent, store: Any, skill_name: str) -> "HookContext":
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
            state=HookStateAdapter(store, skill_name),
            timestamp=event.timestamp if event else int(time.time() * 1000)
        )
