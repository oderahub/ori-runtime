# Copyright 2026 Ori Nexus Systems LTD
# SPDX-License-Identifier: Apache-2.0

import asyncio
import json
import sqlite3
from functools import partial
from typing import Optional

from ori.network.events import ActionResult, OriEvent, ReasoningResult, SensorReading

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
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger_name   TEXT    NOT NULL,
    tier_used      TEXT    NOT NULL,
    prompt         TEXT    NOT NULL DEFAULT '',
    response       TEXT    NOT NULL,
    confidence     REAL    NOT NULL,
    action_tier    TEXT    NOT NULL,
    device_id      TEXT    NOT NULL DEFAULT '',
    model          TEXT    NOT NULL DEFAULT '',
    tokens_used    INTEGER NOT NULL DEFAULT 0,
    latency_ms     INTEGER NOT NULL DEFAULT 0,
    proposed_action TEXT,
    timestamp      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS override_log (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger_name      TEXT    NOT NULL,
    action            TEXT    NOT NULL,
    reason            TEXT    NOT NULL DEFAULT '',
    operator_response TEXT,
    override_type     TEXT    NOT NULL,   -- 'rejection' | 'autonomous_tier_d'
    device_id         TEXT    NOT NULL DEFAULT '',
    timestamp         INTEGER NOT NULL
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

CREATE TABLE IF NOT EXISTS inbound_messages (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    channel             TEXT    NOT NULL,
    from_number         TEXT    NOT NULL,
    message             TEXT    NOT NULL,
    received_at         INTEGER NOT NULL,
    consumed_at         INTEGER
);

CREATE INDEX IF NOT EXISTS idx_inbound_lookup
    ON inbound_messages (channel, from_number, received_at, consumed_at);

CREATE TABLE IF NOT EXISTS sensor_history_5min (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sensor_id TEXT NOT NULL,
    sensor_type TEXT NOT NULL,
    bucket_ms INTEGER NOT NULL,
    avg_value REAL NOT NULL,
    unit TEXT NOT NULL,
    sample_count INTEGER NOT NULL,
    UNIQUE(sensor_id, bucket_ms)
);

CREATE TABLE IF NOT EXISTS sensor_history_hourly (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sensor_id TEXT NOT NULL,
    sensor_type TEXT NOT NULL,
    bucket_ms INTEGER NOT NULL,
    avg_value REAL NOT NULL,
    unit TEXT NOT NULL,
    sample_count INTEGER NOT NULL,
    UNIQUE(sensor_id, bucket_ms)
);

CREATE TABLE IF NOT EXISTS sensor_history_daily (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sensor_id TEXT NOT NULL,
    sensor_type TEXT NOT NULL,
    bucket_ms INTEGER NOT NULL,
    avg_value REAL NOT NULL,
    unit TEXT NOT NULL,
    sample_count INTEGER NOT NULL,
    UNIQUE(sensor_id, bucket_ms)
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
        # Add columns that may be missing from databases created before this
        # migration.  SQLite does not support ALTER TABLE ADD COLUMN IF NOT EXISTS
        # so we catch the OperationalError raised when the column already exists.
        _new_reasoning_cols = [
            ("device_id", "TEXT    NOT NULL DEFAULT ''"),
            ("model", "TEXT    NOT NULL DEFAULT ''"),
            ("tokens_used", "INTEGER NOT NULL DEFAULT 0"),
            ("latency_ms", "INTEGER NOT NULL DEFAULT 0"),
            ("proposed_action", "TEXT"),
        ]
        for col, typedef in _new_reasoning_cols:
            try:
                self._conn.execute(
                    f"ALTER TABLE reasoning_log ADD COLUMN {col} {typedef}"
                )
            except Exception:
                pass  # column already exists
        self._conn.commit()

    async def _run(self, fn, *args):
        """Run a synchronous callable in the executor, serialised by lock."""
        loop = asyncio.get_running_loop()
        async with self._lock:
            return await loop.run_in_executor(None, partial(fn, *args))

    # ─── sensor_history ───────────────────────────────────────────────────────

    async def compact_history(self) -> None:
        """Compact raw sensor history into time-bucketed averages.

        Call from runtime.py via asyncio.create_task() on a 5-minute
        schedule using asyncio periodic task pattern.
        """
        now_ms = _now_ms()
        cutoffs = {
            "raw": now_ms - (48 * 3600 * 1000),  # 48 hours
            "5min": now_ms - (30 * 86400 * 1000),  # 30 days
            "hourly": now_ms - (365 * 86400 * 1000),  # 1 year
        }
        await self._run(self._compact_sync, cutoffs, now_ms)

    def _compact_sync(self, cutoffs: dict, now_ms: int) -> None:
        assert self._conn is not None
        assert cutoffs["raw"] < now_ms - 3600000, (
            "Clock skew detected: refused to compact history"
        )

        # 1. Aggregate raw → 5-minute buckets older than 48h
        self._conn.execute(
            """
            INSERT OR IGNORE INTO sensor_history_5min
            (sensor_id, sensor_type, bucket_ms, avg_value, unit, sample_count)
            SELECT sensor_id, sensor_type,
                   (timestamp / 300000) * 300000 AS bucket_ms,
                   AVG(value), unit, COUNT(*)
            FROM sensor_history
            WHERE timestamp < ?
            GROUP BY sensor_id, (timestamp / 300000)
        """,
            (cutoffs["raw"],),
        )

        # 2. Delete raw rows older than 48h
        self._conn.execute(
            "DELETE FROM sensor_history WHERE timestamp < ?", (cutoffs["raw"],)
        )

        # 3. Aggregate 5-min → hourly buckets older than 30d
        self._conn.execute(
            """
            INSERT OR IGNORE INTO sensor_history_hourly
            (sensor_id, sensor_type, bucket_ms, avg_value, unit, sample_count)
            SELECT sensor_id, sensor_type,
                   (bucket_ms / 3600000) * 3600000,
                   AVG(avg_value), unit, SUM(sample_count)
            FROM sensor_history_5min
            WHERE bucket_ms < ?
            GROUP BY sensor_id, (bucket_ms / 3600000)
        """,
            (cutoffs["5min"],),
        )
        self._conn.execute(
            "DELETE FROM sensor_history_5min WHERE bucket_ms < ?", (cutoffs["5min"],)
        )

        # 4. Aggregate hourly → daily buckets older than 1 year
        self._conn.execute(
            """
            INSERT OR IGNORE INTO sensor_history_daily
            (sensor_id, sensor_type, bucket_ms, avg_value, unit, sample_count)
            SELECT sensor_id, sensor_type,
                   (bucket_ms / 86400000) * 86400000,
                   AVG(avg_value), unit, SUM(sample_count)
            FROM sensor_history_hourly
            WHERE bucket_ms < ?
            GROUP BY sensor_id, (bucket_ms / 86400000)
        """,
            (cutoffs["hourly"],),
        )
        self._conn.execute(
            "DELETE FROM sensor_history_hourly WHERE bucket_ms < ?",
            (cutoffs["hourly"],),
        )

        self._conn.commit()

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
        cutoff_ms = _now_ms() - hours * 3_600_000

        # Weighted average across all tiers to seamlessly span compaction boundaries
        row = self._conn.execute(
            """
            SELECT SUM(val * cnt) / SUM(cnt) AS avg_val
            FROM (
                SELECT value AS val, 1 AS cnt
                FROM sensor_history
                WHERE sensor_id = ? AND timestamp >= ?
                UNION ALL
                SELECT avg_value AS val, sample_count AS cnt
                FROM sensor_history_5min
                WHERE sensor_id = ? AND bucket_ms >= ?
                UNION ALL
                SELECT avg_value AS val, sample_count AS cnt
                FROM sensor_history_hourly
                WHERE sensor_id = ? AND bucket_ms >= ?
                UNION ALL
                SELECT avg_value AS val, sample_count AS cnt
                FROM sensor_history_daily
                WHERE sensor_id = ? AND bucket_ms >= ?
            )
            HAVING SUM(cnt) > 0
            """,
            (
                sensor_id,
                cutoff_ms,
                sensor_id,
                cutoff_ms,
                sensor_id,
                cutoff_ms,
                sensor_id,
                cutoff_ms,
            ),
        ).fetchone()
        return row["avg_val"] if row and row["avg_val"] is not None else None

    async def get_timeseries(
        self, sensor_id: str, start_ms: int, end_ms: int
    ) -> list[tuple[int, float]]:
        """Fetch chart data from the appropriate compaction tier."""
        return await self._run(self._get_timeseries_sync, sensor_id, start_ms, end_ms)

    def _get_timeseries_sync(
        self, sensor_id: str, start_ms: int, end_ms: int
    ) -> list[tuple[int, float]]:
        assert self._conn is not None
        duration_ms = end_ms - start_ms

        # Choose tier based on requested range
        if duration_ms <= 48 * 3600 * 1000:
            table, time_col, val_col = "sensor_history", "timestamp", "value"
        elif duration_ms <= 30 * 86400 * 1000:
            table, time_col, val_col = "sensor_history_5min", "bucket_ms", "avg_value"
        elif duration_ms <= 365 * 86400 * 1000:
            table, time_col, val_col = "sensor_history_hourly", "bucket_ms", "avg_value"
        else:
            table, time_col, val_col = "sensor_history_daily", "bucket_ms", "avg_value"

        rows = self._conn.execute(
            f"""
            SELECT {time_col} AS ts, {val_col} AS val
            FROM {table}
            WHERE sensor_id = ? AND {time_col} BETWEEN ? AND ?
            ORDER BY {time_col} ASC
            """,
            (sensor_id, start_ms, end_ms),
        ).fetchall()
        return [(row["ts"], row["val"]) for row in rows]

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

    # ─── inbound_messages ─────────────────────────────────────────────────────

    async def store_incoming_message(
        self,
        channel: str,
        from_number: str,
        message: str,
        received_at_ms: int | None = None,
    ) -> None:
        await self._run(
            self._store_incoming_message_sync,
            channel,
            from_number,
            message,
            received_at_ms if received_at_ms is not None else _now_ms(),
        )

    def _store_incoming_message_sync(
        self,
        channel: str,
        from_number: str,
        message: str,
        received_at_ms: int,
    ) -> None:
        assert self._conn is not None
        self._conn.execute(
            """
            INSERT INTO inbound_messages
                (channel, from_number, message, received_at, consumed_at)
            VALUES (?, ?, ?, ?, NULL)
            """,
            (channel, from_number, message, received_at_ms),
        )
        self._conn.commit()

    async def consume_incoming_message(
        self,
        channel: str,
        from_number: str,
        since_ms: int,
    ) -> Optional[str]:
        return await self._run(
            self._consume_incoming_message_sync, channel, from_number, since_ms
        )

    def _consume_incoming_message_sync(
        self,
        channel: str,
        from_number: str,
        since_ms: int,
    ) -> Optional[str]:
        assert self._conn is not None
        row = self._conn.execute(
            """
            SELECT id, message
            FROM inbound_messages
            WHERE channel = ?
              AND from_number = ?
              AND received_at >= ?
              AND consumed_at IS NULL
            ORDER BY received_at ASC, id ASC
            LIMIT 1
            """,
            (channel, from_number, since_ms),
        ).fetchone()
        if row is None:
            return None

        self._conn.execute(
            """
            UPDATE inbound_messages
            SET consumed_at = ?
            WHERE id = ? AND consumed_at IS NULL
            """,
            (_now_ms(), row["id"]),
        )
        self._conn.commit()
        return str(row["message"])

    # ─── reasoning_log ───────────────────────────────────────────────────────

    async def log_reasoning(
        self,
        result: ReasoningResult,
        trigger_name: str,
        device_id: str,
    ) -> None:
        """Persist a :class:`~ori.network.events.ReasoningResult` to reasoning_log."""
        await self._run(self._log_reasoning_sync, result, trigger_name, device_id)

    def _log_reasoning_sync(
        self,
        result: ReasoningResult,
        trigger_name: str,
        device_id: str,
    ) -> None:
        assert self._conn is not None
        self._conn.execute(
            """
            INSERT INTO reasoning_log
                (trigger_name, tier_used, prompt, response, confidence,
                 action_tier, device_id, model, tokens_used, latency_ms,
                 proposed_action, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trigger_name,
                result.tier,
                result.prompt,
                result.text,
                result.confidence,
                result.action_tier,
                device_id,
                result.model,
                result.tokens_used,
                result.latency_ms,
                result.proposed_action,
                _now_ms(),
            ),
        )
        self._conn.commit()

    # ─── override_log ─────────────────────────────────────────────────────────

    async def log_override(
        self,
        trigger_name: str,
        action: str,
        reason: str,
        operator_response: Optional[str],
        override_type: str,
        device_id: str,
    ) -> None:
        """Persist an operator rejection or autonomous Tier D override."""
        await self._run(
            self._log_override_sync,
            trigger_name,
            action,
            reason,
            operator_response,
            override_type,
            device_id,
        )

    def _log_override_sync(
        self,
        trigger_name: str,
        action: str,
        reason: str,
        operator_response: Optional[str],
        override_type: str,
        device_id: str,
    ) -> None:
        assert self._conn is not None
        self._conn.execute(
            """
            INSERT INTO override_log
                (trigger_name, action, reason, operator_response,
                 override_type, device_id, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trigger_name,
                action,
                reason,
                operator_response,
                override_type,
                device_id,
                _now_ms(),
            ),
        )
        self._conn.commit()

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
