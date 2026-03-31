# Copyright 2026 Ori Nexus Systems LTD
# SPDX-License-Identifier: Apache-2.0

import asyncio
import json
import sqlite3
from functools import partial
from typing import Optional

from ori.network.events import ActionResult, OriEvent, SensorReading

_DDL = """
CREATE TABLE IF NOT EXISTS sensor_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    sensor_id   TEXT    NOT NULL,
    sensor_type TEXT    NOT NULL,
    value       REAL    NOT NULL,
    unit        TEXT    NOT NULL,
    timestamp   INTEGER NOT NULL,
    quality     REAL    NOT NULL,
    metadata    TEXT    NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_sensor_history_sensor_id_ts
    ON sensor_history (sensor_id, timestamp DESC);

CREATE TABLE IF NOT EXISTS reasoning_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger_name TEXT    NOT NULL,
    tier_used    TEXT    NOT NULL,
    prompt       TEXT    NOT NULL,
    response     TEXT    NOT NULL,
    confidence   REAL    NOT NULL,
    action_tier  TEXT    NOT NULL,
    timestamp    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS action_log (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    action_name       TEXT    NOT NULL,
    tier              TEXT    NOT NULL,
    executed          INTEGER NOT NULL,   -- 0 or 1
    approved          INTEGER,            -- NULL for Tiers A/B/D, 0/1 for C
    action_taken      TEXT    NOT NULL,
    operator_response TEXT,
    trigger_name      TEXT    NOT NULL,
    timestamp         INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS causal_memory (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern_key TEXT    NOT NULL UNIQUE,
    resolution  TEXT    NOT NULL,
    confidence  REAL    NOT NULL,
    created_at  INTEGER NOT NULL,
    last_seen   INTEGER NOT NULL,
    hit_count   INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS skill_state (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_name  TEXT    NOT NULL,
    key         TEXT    NOT NULL,
    value       TEXT    NOT NULL,
    updated_at  INTEGER NOT NULL,
    UNIQUE (skill_name, key)
);
"""


class StateStore:
    """Async-safe SQLite state store.

    All blocking SQLite calls are dispatched to a thread-pool executor so the
    asyncio event loop is never blocked.
    """

    def __init__(self, db_path: str = "ori_state.db") -> None:
        self._db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = asyncio.Lock()

    # ─── Lifecycle ────────────────────────────────────────────────────────────

    async def open(self) -> None:
        """Open the database connection and apply DDL migrations."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._open_sync)

    def _open_sync(self) -> None:
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        self._migrate_sync()

    async def close(self) -> None:
        if self._conn is not None:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._conn.close)
            self._conn = None

    def _migrate_sync(self) -> None:
        assert self._conn is not None
        self._conn.executescript(_DDL)
        self._conn.commit()

    async def _run(self, fn, *args):
        """Run a synchronous callable in the executor, serialised by lock."""
        loop = asyncio.get_running_loop()
        async with self._lock:
            return await loop.run_in_executor(None, partial(fn, *args))

    # ─── sensor_history ───────────────────────────────────────────────────────

    async def append_history(self, event: OriEvent) -> None:
        """Persist a sensor reading from an OriEvent."""
        if event.reading is None:
            return
        r = event.reading
        await self._run(self._insert_reading_sync, r)

    def _insert_reading_sync(self, r: SensorReading) -> None:
        assert self._conn is not None
        self._conn.execute(
            """
            INSERT INTO sensor_history
                (sensor_id, sensor_type, value, unit, timestamp, quality, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                r.sensor_id,
                r.sensor_type,
                r.value,
                r.unit,
                r.timestamp,
                r.quality,
                json.dumps(r.metadata),
            ),
        )
        self._conn.commit()

    async def get_history(
        self, sensor_id: str, limit: int = 100
    ) -> list[SensorReading]:
        return await self._run(self._get_history_sync, sensor_id, limit)

    def _get_history_sync(self, sensor_id: str, limit: int) -> list[SensorReading]:
        assert self._conn is not None
        rows = self._conn.execute(
            """
            SELECT sensor_id, sensor_type, value, unit, timestamp, quality, metadata
            FROM sensor_history
            WHERE sensor_id = ?
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (sensor_id, limit),
        ).fetchall()
        return [
            SensorReading(
                sensor_id=row["sensor_id"],
                sensor_type=row["sensor_type"],
                value=row["value"],
                unit=row["unit"],
                timestamp=row["timestamp"],
                quality=row["quality"],
                metadata=json.loads(row["metadata"]),
            )
            for row in rows
        ]

    async def avg_last_n(self, sensor_id: str, n: int) -> Optional[float]:
        """Average of the n most-recent readings for a sensor."""
        return await self._run(self._avg_last_n_sync, sensor_id, n)

    def _avg_last_n_sync(self, sensor_id: str, n: int) -> Optional[float]:
        assert self._conn is not None
        row = self._conn.execute(
            """
            SELECT AVG(value) AS avg_val
            FROM (
                SELECT value
                FROM sensor_history
                WHERE sensor_id = ?
                ORDER BY timestamp DESC
                LIMIT ?
            )
            """,
            (sensor_id, n),
        ).fetchone()
        return row["avg_val"] if row else None

    async def avg_last_hours(self, sensor_id: str, hours: int) -> Optional[float]:
        """Average of all readings within the last *hours* hours."""
        return await self._run(self._avg_last_hours_sync, sensor_id, hours)

    def _avg_last_hours_sync(self, sensor_id: str, hours: int) -> Optional[float]:
        assert self._conn is not None
        # timestamp column is unix milliseconds
        cutoff_ms = _now_ms() - hours * 3_600_000
        row = self._conn.execute(
            """
            SELECT AVG(value) AS avg_val
            FROM sensor_history
            WHERE sensor_id = ?
              AND timestamp >= ?
            """,
            (sensor_id, cutoff_ms),
        ).fetchone()
        return row["avg_val"] if row else None

    # ─── action_log ───────────────────────────────────────────────────────────

    async def log_action(self, result: ActionResult, trigger_name: str) -> None:
        await self._run(self._log_action_sync, result, trigger_name)

    def _log_action_sync(self, result: ActionResult, trigger_name: str) -> None:
        assert self._conn is not None
        approved_int: Optional[int] = None
        if result.approved is not None:
            approved_int = 1 if result.approved else 0
        self._conn.execute(
            """
            INSERT INTO action_log
                (action_name, tier, executed, approved, action_taken,
                 operator_response, trigger_name, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                result.action_name,
                result.tier,
                1 if result.executed else 0,
                approved_int,
                result.action_taken,
                result.operator_response,
                trigger_name,
                result.timestamp,
            ),
        )
        self._conn.commit()

    async def get_action_log(self, limit: int = 50) -> list[dict]:
        return await self._run(self._get_action_log_sync, limit)

    def _get_action_log_sync(self, limit: int) -> list[dict]:
        assert self._conn is not None
        rows = self._conn.execute(
            """
            SELECT action_name, tier, executed, approved, action_taken,
                   operator_response, trigger_name, timestamp
            FROM action_log
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        result = []
        for row in rows:
            approved_val: Optional[bool] = None
            if row["approved"] is not None:
                approved_val = bool(row["approved"])
            result.append(
                {
                    "action_name": row["action_name"],
                    "tier": row["tier"],
                    "executed": bool(row["executed"]),
                    "approved": approved_val,
                    "action_taken": row["action_taken"],
                    "operator_response": row["operator_response"],
                    "trigger_name": row["trigger_name"],
                    "timestamp": row["timestamp"],
                }
            )
        return result

    # ─── causal_memory ────────────────────────────────────────────────────────

    async def lookup_causal_memory(self, pattern_key: str) -> Optional[str]:
        return await self._run(self._lookup_causal_sync, pattern_key)

    def _lookup_causal_sync(self, pattern_key: str) -> Optional[str]:
        assert self._conn is not None
        row = self._conn.execute(
            """
            SELECT resolution FROM causal_memory WHERE pattern_key = ?
            """,
            (pattern_key,),
        ).fetchone()
        if row is None:
            return None
        # Increment hit_count and update last_seen in the same transaction
        self._conn.execute(
            """
            UPDATE causal_memory
            SET hit_count = hit_count + 1, last_seen = ?
            WHERE pattern_key = ?
            """,
            (_now_ms(), pattern_key),
        )
        self._conn.commit()
        return row["resolution"]

    async def store_causal_memory(
        self, pattern_key: str, resolution: str, confidence: float
    ) -> None:
        await self._run(self._store_causal_sync, pattern_key, resolution, confidence)

    def _store_causal_sync(
        self, pattern_key: str, resolution: str, confidence: float
    ) -> None:
        assert self._conn is not None
        now = _now_ms()
        self._conn.execute(
            """
            INSERT INTO causal_memory
                (pattern_key, resolution, confidence, created_at, last_seen, hit_count)
            VALUES (?, ?, ?, ?, ?, 1)
            ON CONFLICT(pattern_key) DO UPDATE SET
                resolution = excluded.resolution,
                confidence = excluded.confidence,
                last_seen  = excluded.last_seen,
                hit_count  = hit_count + 1
            """,
            (pattern_key, resolution, confidence, now, now),
        )
        self._conn.commit()

    # ─── skill_state ──────────────────────────────────────────────────────────

    async def get_skill_state(self, skill_name: str, key: str) -> Optional[str]:
        return await self._run(self._get_skill_state_sync, skill_name, key)

    def _get_skill_state_sync(self, skill_name: str, key: str) -> Optional[str]:
        assert self._conn is not None
        row = self._conn.execute(
            """
            SELECT value FROM skill_state
            WHERE skill_name = ? AND key = ?
            """,
            (skill_name, key),
        ).fetchone()
        return row["value"] if row else None

    async def set_skill_state(self, skill_name: str, key: str, value: str) -> None:
        await self._run(self._set_skill_state_sync, skill_name, key, value)

    def _set_skill_state_sync(self, skill_name: str, key: str, value: str) -> None:
        assert self._conn is not None
        self._conn.execute(
            """
            INSERT INTO skill_state (skill_name, key, value, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(skill_name, key) DO UPDATE SET
                value      = excluded.value,
                updated_at = excluded.updated_at
            """,
            (skill_name, key, value, _now_ms()),
        )
        self._conn.commit()


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _now_ms() -> int:
    """Current time as unix milliseconds (UTC)."""
    import time

    return int(time.time() * 1000)
