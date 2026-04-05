# Copyright 2026 Ori Nexus Systems LTD
# SPDX-License-Identifier: Apache-2.0

"""Physical relay control via Raspberry Pi GPIO.

Used for Tier B (soft physical) and Tier D (safety-critical) actions.

.. warning::
    **Safety — read before enabling in production.**

    Relay wiring connects Ori directly to mains voltage or industrial
    control circuits.  Incorrect wiring can cause electric shock, fire,
    equipment damage, or death.  Before setting ``relay.enabled: true``
    in ori.yaml:

    - Wiring must be inspected and approved by a qualified electrician.
    - Verify the relay's rated switching capacity (voltage, current) is
      not exceeded by the connected load.
    - Confirm ``active_high`` matches the relay module's trigger logic
      (most opto-isolated relay boards are ``active_low``).
    - Wire to the Normally Closed (NC) terminal, not Normally Open
      (NO).  NC wiring means power loss or an Ori crash defaults the
      load to the safe state (disconnected) without software
      intervention.
    - Test with the load de-energised before connecting live circuits.
    - Never operate a relay above its rated duty cycle.

    Ori accepts no liability for damage caused by incorrect relay wiring.

Platform guard
--------------
``gpiozero`` is only available on Raspberry Pi.  On non-Pi platforms
(developer laptops, CI) the import is caught and the action enters
*simulation mode*: all calls succeed and are logged at DEBUG level
without touching any hardware.  This allows the full action pipeline
to be exercised in tests without a Pi.

Usage
-----
    relay = RelayAction()
    await relay.connect(gpio_pin=26, active_high=True)

    # Pulse for 2 seconds (Tier B: switch power source)
    await relay.trigger(duration_seconds=2.0)

    # Latch open (Tier D: emergency cutoff, held until manual reset)
    await relay.trigger()   # duration_seconds=None → latch on
    await relay.release()   # explicit release
"""

import asyncio
import logging

logger = logging.getLogger(__name__)

# Valid BCM GPIO pin numbers on Raspberry Pi 4 (pins 0–1 are reserved for
# I2C ID EEPROM; 28–53 are not exposed on the 40-pin header).
_VALID_BCM_PINS: frozenset[int] = frozenset(range(2, 28))


class RelayAction:
    """Controls a single relay output pin via gpiozero.

    One :class:`RelayAction` instance manages one physical relay.
    Instantiate one per relay defined in ori.yaml.

    GPIO initialisation is deferred to :meth:`connect` (rather than
    ``__init__``) so the object can be constructed at runtime startup
    before the event loop is running and before a pin number is known.
    """

    def __init__(self) -> None:
        self._pin: int | None = None
        self._active_high: bool = True
        self._device = None          # gpiozero OutputDevice, or None in sim mode
        self._simulated: bool = False
        self._connected: bool = False
        self._sim_state: bool = False  # logical active state used in simulation

    # ── Connection ────────────────────────────────────────────────────────────

    async def connect(self, gpio_pin: int, active_high: bool = True) -> None:
        """Initialise *gpio_pin* as a relay output.

        The gpiozero constructor is fast (microseconds) and does not need
        to be pushed to an executor.

        Args:
            gpio_pin: BCM GPIO pin number (e.g. ``26`` per CLAUDE.md).
            active_high: ``True`` if the relay activates on a HIGH signal
                (default).  Set ``False`` for opto-isolated relay boards
                that trigger on LOW — verify the relay datasheet.

        Note:
            On non-Pi platforms gpiozero is unavailable and a warning is
            logged.  All subsequent calls run in simulation mode — no
            hardware is touched.
        """
        if gpio_pin not in _VALID_BCM_PINS:
            raise ValueError(
                f"RelayAction: gpio_pin={gpio_pin} is outside valid BCM "
                f"range (2-27 on Pi 4). Check ori.yaml relay config. "
                f"Misconfigured pins must fail at startup, not during "
                f"a safety action."
            )

        self._pin = gpio_pin
        self._active_high = active_high

        try:
            from gpiozero import OutputDevice  # type: ignore[import-untyped]

            # initial_value=False → relay starts de-energised
            self._device = OutputDevice(
                gpio_pin,
                active_high=active_high,
                initial_value=False,
            )
            self._simulated = False
            logger.info(
                "RelayAction: connected to GPIO pin %d (active_high=%s)",
                gpio_pin,
                active_high,
            )
        except ImportError:
            self._device = None
            self._simulated = True
            logger.warning(
                "RelayAction: gpiozero not available — running in simulation "
                "mode on GPIO pin %d.  No hardware will be actuated.",
                gpio_pin,
            )

        self._connected = True

    # ── Control ───────────────────────────────────────────────────────────────

    async def trigger(self, duration_seconds: float | None = None) -> bool:
        """Activate the relay.

        Args:
            duration_seconds: Activate for this many seconds then release
                automatically.  Pass ``None`` to latch the relay on
                indefinitely until :meth:`release` is called explicitly.

        Returns:
            ``True`` on success, ``False`` if not connected or on error.
        """
        if not self._connected:
            logger.error(
                "RelayAction.trigger: called before connect() — pin not initialised."
            )
            return False

        try:
            if self._simulated:
                self._sim_state = True
                logger.debug(
                    "RelayAction.trigger [SIM]: GPIO pin %d activated (duration=%s)",
                    self._pin,
                    f"{duration_seconds}s" if duration_seconds is not None else "latched",
                )
                if duration_seconds is not None:
                    await asyncio.sleep(duration_seconds)
                    self._sim_state = False
                    logger.debug(
                        "RelayAction.trigger [SIM]: GPIO pin %d released after %.2fs",
                        self._pin,
                        duration_seconds,
                    )
                return True

            # Real GPIO
            self._device.on()
            logger.info(
                "RelayAction.trigger: GPIO pin %d activated (duration=%s)",
                self._pin,
                f"{duration_seconds}s" if duration_seconds is not None else "latched",
            )
            if duration_seconds is not None:
                await asyncio.sleep(duration_seconds)
                self._device.off()
                logger.info(
                    "RelayAction.trigger: GPIO pin %d released after %.2fs",
                    self._pin,
                    duration_seconds,
                )
            return True

        except Exception:
            logger.exception(
                "RelayAction.trigger: error on GPIO pin %d", self._pin
            )
            return False

    async def release(self) -> bool:
        """Deactivate the relay (open the circuit).

        Returns:
            ``True`` on success, ``False`` if not connected or on error.
        """
        if not self._connected:
            logger.error(
                "RelayAction.release: called before connect() — pin not initialised."
            )
            return False

        try:
            if self._simulated:
                self._sim_state = False
                logger.debug(
                    "RelayAction.release [SIM]: GPIO pin %d deactivated", self._pin
                )
                return True

            self._device.off()
            logger.info(
                "RelayAction.release: GPIO pin %d deactivated", self._pin
            )
            return True

        except Exception:
            logger.exception(
                "RelayAction.release: error on GPIO pin %d", self._pin
            )
            return False

    # ── State ─────────────────────────────────────────────────────────────────

    @property
    def is_active(self) -> bool:
        """``True`` if the relay is currently energised.

        In simulation mode tracks the logical state set by :meth:`trigger`
        and :meth:`release`.  On real hardware reads the live pin value
        from gpiozero.
        """
        if not self._connected:
            return False
        if self._simulated:
            return self._sim_state
        return bool(self._device.value)
