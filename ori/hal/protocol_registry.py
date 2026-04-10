# Copyright 2026 Ori Nexus Systems LTD
# SPDX-License-Identifier: Apache-2.0

"""Shared protocol registry used by config validation and runtime adapter wiring."""

from ori.hal.base import BaseAdapter

SUPPORTED_SENSOR_PROTOCOLS: frozenset[str] = frozenset(
    {"psutil", "i2c", "serial", "growatt"}
)


class UnknownProtocolError(ValueError):
    """Raised when a sensor protocol is not registered in the runtime."""


def make_adapter(protocol: str) -> BaseAdapter:
    """Instantiate the HAL adapter for *protocol*."""
    if protocol == "psutil":
        from ori.hal.psutil_adapter import PsutilAdapter

        return PsutilAdapter()
    if protocol == "i2c":
        from ori.hal.i2c_adapter import I2CAdapter

        return I2CAdapter()
    if protocol == "growatt":
        from ori.hal.growatt_adapter import GrowattAdapter

        return GrowattAdapter()
    if protocol == "serial":
        from ori.hal.serial_adapter import SerialAdapter

        return SerialAdapter()

    raise UnknownProtocolError(
        f"Unknown sensor protocol '{protocol}'. "
        f"Supported: {sorted(SUPPORTED_SENSOR_PROTOCOLS)}. "
        "Check ori.yaml sensors configuration."
    )
