# Copyright 2026 Ori Nexus Systems LTD
# SPDX-License-Identifier: Apache-2.0

from unittest.mock import patch

import pytest

from ori.hal.base import AdapterConnectionError, AdapterReadError, CircuitState
from ori.hal.growatt_adapter import _SENSOR_MAP, GrowattAdapter


def _config(
    sensor_type: str = "growatt_battery_soc",
    host: str = "192.168.1.50",
    serial: str = "ABC1234567",
    port: int = 8899,
    failure_threshold: int = 3,
) -> dict:
    return {
        "sensor_id": "inverter-01",
        "sensor_type": sensor_type,
        "host": host,
        "serial": serial,
        "port": port,
        "circuit_breaker": {
            "failure_threshold": failure_threshold,
            "recovery_timeout_s": 300,
            "success_threshold": 2,
        },
    }


def _split_u32(value: int) -> list[int]:
    value &= 0xFFFFFFFF
    return [(value >> 16) & 0xFFFF, value & 0xFFFF]


class _FakeSolarman:
    def __init__(self, *_args, **_kwargs):
        self._responses: dict[tuple[int, int], list[int]] = {
            (_SENSOR_MAP["growatt_battery_soc"][0], 1): [850],
            (_SENSOR_MAP["growatt_pv_power"][0], 2): _split_u32(12345),
            (_SENSOR_MAP["growatt_grid_power"][0], 2): _split_u32(-230),
            (_SENSOR_MAP["growatt_load_power"][0], 2): _split_u32(4321),
            (_SENSOR_MAP["growatt_battery_voltage"][0], 1): [520],
        }

    def read_holding_registers(self, register: int, count: int) -> list[int]:
        key = (register, count)
        if key not in self._responses:
            raise RuntimeError(f"no fake response for {key}")
        return self._responses[key]

    def close(self) -> None:
        return None


class TestGrowattAdapter:
    @pytest.mark.asyncio
    async def test_graceful_import_failure(self):
        adapter = GrowattAdapter()
        with patch("ori.hal.growatt_adapter._PYSOLARMAN_AVAILABLE", False):
            with pytest.raises(AdapterConnectionError, match="pysolarmanv5"):
                await adapter.connect(_config())

            assert adapter.is_connected is False
            with pytest.raises(AdapterConnectionError, match="pysolarmanv5"):
                await adapter.read("inverter-01")

    @pytest.mark.asyncio
    async def test_connect_stores_config(self):
        adapter = GrowattAdapter()
        with (
            patch("ori.hal.growatt_adapter._PYSOLARMAN_AVAILABLE", True),
            patch("ori.hal.growatt_adapter._PySolarmanV5", _FakeSolarman),
        ):
            await adapter.connect(
                _config(
                    sensor_type="growatt_battery_soc",
                    host="10.0.0.5",
                    serial="SERIAL-999",
                    port=9900,
                )
            )
            assert adapter.is_connected is True

        assert adapter._host == "10.0.0.5"
        assert adapter._serial == "SERIAL-999"
        assert adapter._port == 9900
        assert adapter._client is None  # lazy client initialization

    @pytest.mark.asyncio
    async def test_read_all_sensor_types(self):
        expected = {
            "growatt_battery_soc": (85.0, "percent"),
            "growatt_pv_power": (12345.0, "watt"),
            "growatt_grid_power": (-230.0, "watt"),
            "growatt_load_power": (4321.0, "watt"),
            "growatt_battery_voltage": (52.0, "volt"),
        }

        with (
            patch("ori.hal.growatt_adapter._PYSOLARMAN_AVAILABLE", True),
            patch("ori.hal.growatt_adapter._PySolarmanV5", _FakeSolarman),
        ):
            for sensor_type, (value, unit) in expected.items():
                adapter = GrowattAdapter()
                await adapter.connect(_config(sensor_type=sensor_type))
                reading = await adapter.read("inverter-01")
                assert reading.sensor_type == sensor_type
                assert reading.value == pytest.approx(value)
                assert reading.unit == unit
                assert reading.metadata["source"] == "growatt"
                await adapter.close()

    @pytest.mark.asyncio
    async def test_circuit_breaker_integration(self):
        adapter = GrowattAdapter()
        with (
            patch("ori.hal.growatt_adapter._PYSOLARMAN_AVAILABLE", True),
            patch("ori.hal.growatt_adapter._PySolarmanV5", _FakeSolarman),
        ):
            await adapter.connect(_config(failure_threshold=2))

            error = AdapterReadError("simulated read failure")
            with patch.object(adapter, "_read_sensor_value_sync", side_effect=error):
                with pytest.raises(AdapterReadError):
                    await adapter.read("inverter-01")
                assert adapter._breaker.state == CircuitState.CLOSED

                with pytest.raises(AdapterReadError):
                    await adapter.read("inverter-01")
                assert adapter._breaker.state == CircuitState.OPEN

                with pytest.raises(AdapterReadError, match="circuit breaker OPEN"):
                    await adapter.read("inverter-01")

    @pytest.mark.asyncio
    async def test_connection_refused_propagates(self):
        adapter = GrowattAdapter()
        with (
            patch("ori.hal.growatt_adapter._PYSOLARMAN_AVAILABLE", True),
            patch("ori.hal.growatt_adapter._PySolarmanV5", _FakeSolarman),
        ):
            await adapter.connect(_config())
            with patch.object(
                adapter,
                "_read_sensor_value_sync",
                side_effect=ConnectionRefusedError("refused"),
            ):
                with pytest.raises(AdapterReadError, match="connection refused"):
                    await adapter.read("inverter-01")
