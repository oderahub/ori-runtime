# Copyright 2026 Ori Nexus Systems LTD
# SPDX-License-Identifier: Apache-2.0

import asyncio
import json
import logging
import time
from typing import Any, Iterable

from ori.hal.base import (
    AdapterConnectionError,
    AdapterReadError,
    BaseAdapter,
    HardwareCircuitBreaker,
)

logger = logging.getLogger(__name__)

try:
    import aiomqtt as _aiomqtt  # type: ignore[import-untyped]

    _AIOMQTT_AVAILABLE = True
except ImportError:
    _aiomqtt = None
    _AIOMQTT_AVAILABLE = False


class MqttCachedAdapter(BaseAdapter):
    """Reusable base for MQTT adapters that subscribe and cache latest values."""

    def __init__(self) -> None:
        self._connected = False
        self._broker_host: str = ""
        self._port: int = 1883

        self._breaker: HardwareCircuitBreaker | None = None
        self._client: Any = None
        self._listener_task: asyncio.Task[None] | None = None

        # topic -> (value, timestamp_ms, raw_payload)
        self._cache: dict[str, tuple[float, int, Any]] = {}

    @property
    def is_connected(self) -> bool:
        return self._connected and _AIOMQTT_AVAILABLE

    def _ensure_aiomqtt_available(self) -> None:
        if not _AIOMQTT_AVAILABLE or _aiomqtt is None:
            raise AdapterConnectionError(
                f"{self.adapter_name}: 'aiomqtt' is not installed. Run: pip install aiomqtt"
            )

    async def _connect_mqtt(
        self,
        *,
        config: dict,
        topics: Iterable[str],
        default_port: int = 1883,
        broker_host_key: str = "broker_host",
        port_key: str = "port",
        listener_name: str | None = None,
    ) -> None:
        self._ensure_aiomqtt_available()

        self._broker_host = str(config.get(broker_host_key, "")).strip()
        self._port = int(config.get(port_key, default_port))
        self._breaker = HardwareCircuitBreaker(self.adapter_name, config)

        if not self._broker_host:
            raise AdapterConnectionError(
                f"{self.adapter_name}: '{broker_host_key}' is required in sensor config."
            )
        if self._port <= 0:
            raise AdapterConnectionError(f"{self.adapter_name}: '{port_key}' must be > 0.")

        try:
            client = _aiomqtt.Client(hostname=self._broker_host, port=self._port)
            self._client = await client.__aenter__()
            for topic in topics:
                await self._client.subscribe(topic)
            self._connected = True
            self._listener_task = asyncio.create_task(
                self._listen_loop(),
                name=listener_name or f"mqtt-listener:{self._broker_host}:{self._port}",
            )
        except Exception as exc:
            await self._close_mqtt_quietly()
            raise AdapterConnectionError(
                f"{self.adapter_name}: failed to connect/subscribe to "
                f"{self._broker_host}:{self._port}: {exc}"
            ) from exc

    async def _close_mqtt(self) -> None:
        self._connected = False

        task = self._listener_task
        self._listener_task = None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        await self._close_mqtt_quietly()

    async def _close_mqtt_quietly(self) -> None:
        client = self._client
        self._client = None
        if client is None:
            return
        try:
            await client.__aexit__(None, None, None)
        except Exception:
            logger.warning("%s: exception while closing MQTT client", self.adapter_name)

    async def _listen_loop(self) -> None:
        if self._client is None:
            return

        try:
            async for message in self._client.messages:
                topic = str(message.topic)
                try:
                    await self._handle_message(topic, message.payload)
                except AdapterReadError as exc:
                    logger.warning(
                        "%s: skipping invalid payload on topic=%s: %s",
                        self.adapter_name,
                        topic,
                        exc,
                    )
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception(
                "%s: listener loop crashed for broker %s:%s",
                self.adapter_name,
                self._broker_host,
                self._port,
            )

    async def _handle_message(self, topic: str, payload: Any) -> None:
        """Override in concrete adapters to parse and cache message values."""
        raise NotImplementedError

    def _cache_value(self, topic: str, value: float, raw_payload: Any) -> None:
        self._cache[topic] = (float(value), int(time.time() * 1000), raw_payload)

    @staticmethod
    def parse_numeric_payload(payload: Any) -> tuple[float, Any]:
        """Parse common MQTT payload formats into a float.

        Supports:
        - plain numeric strings/bytes, e.g. b"42.5"
        - JSON object payload with a `value` field, e.g. {"value": 42.5}
        """
        raw_payload: Any = payload
        if isinstance(payload, (bytes, bytearray)):
            text = bytes(payload).decode("utf-8", errors="replace").strip()
            raw_payload = text
        else:
            text = str(payload).strip()
            raw_payload = text

        if not text:
            raise AdapterReadError("MQTT payload is empty")

        parsed: Any = text
        if text.startswith("{") or text.startswith("["):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as exc:
                raise AdapterReadError(f"Invalid JSON payload: {exc}") from exc

        if isinstance(parsed, dict):
            if "value" not in parsed:
                raise AdapterReadError("JSON payload missing required 'value' field")
            value_candidate = parsed["value"]
            raw_payload = parsed
        else:
            value_candidate = parsed

        try:
            return float(value_candidate), raw_payload
        except (TypeError, ValueError) as exc:
            raise AdapterReadError(
                f"MQTT payload value is not numeric: {value_candidate!r}"
            ) from exc
