# Copyright 2026 Ori Nexus Systems LTD
# SPDX-License-Identifier: Apache-2.0

from abc import ABC, abstractmethod

from ori.network.events import SensorReading


class AdapterConnectionError(Exception):
    """Raised when an adapter cannot establish or restore a connection."""


class AdapterTimeoutError(Exception):
    """Raised when a read or connect operation exceeds its deadline."""


class AdapterReadError(Exception):
    """Raised when a connection exists but a sensor read fails."""


class BaseAdapter(ABC):
    """Common interface for every hardware/protocol adapter in the HAL.

    Concrete adapters (GPIO, I2C, Serial, psutil, MQTT …) must subclass this and implement the three abstract methods.  The runtime interacts exclusively through this interface so that adapters are interchangeable.
    """

    _connected: bool = False

    # ── Abstract methods ──────────────────────────────────────────────────────

    @abstractmethod
    async def connect(self, config: dict) -> None:
        """Open the underlying hardware or protocol connection.

        Called once during runtime start-up.  Must set ``_connected = True`` on success.  Raise :exc:`AdapterConnectionError` if the resource cannot be reached, or :exc:`AdapterTimeoutError` if the attempt exceeds the configured deadline.

        Args:
            config: The sensor-level config dict from ``ori.yaml`` (keys such as ``address``, ``channel``, ``port`` vary by adapter type).
        """

    @abstractmethod
    async def read(self, sensor_id: str) -> SensorReading:
        """Sample the sensor and return a single normalised reading.

        Must be callable repeatedly at ``poll_interval_ms`` frequency.
        Raise :exc:`AdapterReadError` for transient read failures.
        Raise :exc:`AdapterTimeoutError` if the hardware does not respond in time.  Never returns ``None`` — callers rely on a valid :class:`~ori.network.events.SensorReading` on success.

        Args:
            sensor_id: The logical sensor id from ``ori.yaml``, embedded in the returned :class:`~ori.network.events.SensorReading`.
        """

    @abstractmethod
    async def close(self) -> None:
        """Release the underlying hardware or protocol connection.

        Called during graceful runtime shutdown.  Must set
        ``_connected = False``.  Should not raise even if the connection was already lost — log and return cleanly.
        """

    # ── Concrete methods (may be overridden) ──────────────────────────────────

    async def health_check(self) -> bool:
        """Return ``True`` if the adapter is operational.

        The default implementation returns ``True`` when :attr:`is_connected` is ``True``.  Adapters with richer diagnostics (e.g. register reads, ping commands) should override this.
        """
        return self._connected

    # ── Circuit-breaker stubs (Phase 2) ───────────────────────────────────────
    # Adapters call these hooks around every read() so that the Phase 2
    # circuit-breaker implementation can activate without modifying adapter code.
    # All stubs are no-ops / safe defaults in Phase 1.

    def _cb_init(self) -> None:
        """Initialise circuit-breaker state for this adapter instance.

        Called once at the end of a successful :meth:`connect`.  Phase 2 will
        set up failure counters and state-machine fields here.
        """

    def _cb_allow_read(self) -> bool:
        """Return ``True`` if the circuit breaker permits a read attempt.

        Phase 2 will return ``False`` when the breaker is open, causing
        :meth:`read` to raise :exc:`AdapterReadError` without touching hardware.
        Always returns ``True`` in Phase 1.
        """
        return True

    def _cb_record_success(self) -> None:
        """Record a successful read with the circuit breaker.

        Phase 2 will use this to transition the breaker from half-open to closed.
        No-op in Phase 1.
        """

    def _cb_record_failure(self) -> bool:
        """Record a failed read with the circuit breaker.

        Returns ``True`` when the breaker has just tripped (Phase 2), so callers
        can log a single "circuit opened" message.  Always returns ``False`` in
        Phase 1.
        """
        return False

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def is_connected(self) -> bool:
        """``True`` after a successful :meth:`connect`, ``False`` after :meth:`close`."""
        return self._connected

    @property
    def adapter_name(self) -> str:
        """Human-readable adapter identifier — defaults to the class name."""
        return type(self).__name__
