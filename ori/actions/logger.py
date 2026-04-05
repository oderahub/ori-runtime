# Copyright 2026 Ori Nexus Systems LTD
# SPDX-License-Identifier: Apache-2.0

"""Structured stdout logger action.

Writes human-readable structured lines to the Python logging system.
SQLite persistence is handled by :class:`~ori.state.store.StateStore`
and is called by the elevator and dispatcher — not by this class.

This class has no StateStore dependency and no SQLite imports.
It is intentionally thin: one INFO line per reasoning result,
one WARNING line per override event.
"""

import logging

from ori.network.events import ReasoningResult

logger = logging.getLogger(__name__)


class LoggerAction:
    """Writes structured log lines for reasoning results and override events.

    No constructor arguments — reads nothing from config and holds no state.
    All methods are synchronous and never raise.
    """

    def log_reasoning(
        self,
        result: ReasoningResult,
        trigger_name: str,
        device_id: str,
    ) -> None:
        """Write one INFO line summarising a reasoning result.

        Format::

            [reasoning] device=<id> trigger=<name> tier=<tier> \
model=<model> confidence=<n>% latency=<n>ms

        Args:
            result: The :class:`~ori.network.events.ReasoningResult` to log.
            trigger_name: The sensor ID or trigger name that caused reasoning.
            device_id: The device this result belongs to.
        """
        try:
            logger.info(
                "[reasoning] device=%s trigger=%s tier=%s model=%s "
                "confidence=%.0f%% latency=%dms",
                device_id,
                trigger_name,
                result.tier,
                result.model,
                result.confidence * 100,
                result.latency_ms,
            )
        except Exception:  # pragma: no cover
            pass  # logging must never crash the runtime

    def log_override(
        self,
        action: str,
        override_type: str,
        device_id: str,
    ) -> None:
        """Write one WARNING line for an operator rejection or Tier D override.

        Format::

            [override] device=<id> action=<action> type=<override_type>

        Args:
            action: The action name that was overridden or bypassed.
            override_type: ``'rejection'`` (operator said NO / timeout) or
                ``'autonomous_tier_d'`` (safety action fired without approval).
            device_id: The device on which the override occurred.
        """
        try:
            logger.warning(
                "[override] device=%s action=%s type=%s",
                device_id,
                action,
                override_type,
            )
        except Exception:  # pragma: no cover
            pass  # logging must never crash the runtime
