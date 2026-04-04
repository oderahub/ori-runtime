# Copyright 2026 Ori Nexus Systems LTD
# SPDX-License-Identifier: Apache-2.0

import logging
import math
import time
from dataclasses import dataclass
from typing import Any

from ori.network.events import OriEvent

logger = logging.getLogger(__name__)

# Patterns that must never appear in a rule condition expression.
_UNSAFE_PATTERNS = ("import", "__", "exec", "eval", "open", "os", "sys", "subprocess")


class RuleEngineSafetyError(Exception):
    """Raised when a rule condition contains a forbidden pattern."""


@dataclass
class RuleResult:
    """Outcome of a single :meth:`RuleEngine.evaluate` call."""

    matched: bool
    action_tier: str  # 'A' | 'B' | 'C' | 'D'
    rule_name: str | None = None
    escalate_to: str | None = None  # 'rule' | 'local_slm' | 'gateway' | 'cloud'
    bypass_llm: bool = False
    action: str | None = None
    confidence: float = 1.0


@dataclass
class _CooldownRecord:
    last_fired_ms: int


class EvalContext:
    """Thin wrapper that exposes sensor history helpers inside rule expressions.

    Passes through a simple ``context`` dict as the primary evaluation
    namespace.  The ``history`` attribute is available as ``history`` inside
    expressions.
    """

    def __init__(self, values: dict[str, Any], state_store: Any = None) -> None:
        self._values = values
        self._store = state_store

    # ------------------------------------------------------------------
    # History helpers exposed as ``history.avg_24h(...)`` etc.
    # ------------------------------------------------------------------

    def avg_24h(self, sensor_id: str) -> float:
        """Return 24-hour rolling average for *sensor_id* (0.0 if unavailable)."""
        if self._store is None:
            return 0.0
        import asyncio

        try:
            loop = asyncio.get_event_loop()
            readings = loop.run_until_complete(
                self._store.get_sensor_history(sensor_id, limit=86_400)
            )
            if not readings:
                return 0.0
            return sum(r["value"] for r in readings) / len(readings)
        except Exception:
            logger.warning(
                "EvalContext.avg_24h: could not fetch history for %r", sensor_id
            )
            return 0.0

    def last_n(self, sensor_id: str, n: int) -> list[float]:
        """Return the last *n* values for *sensor_id* (empty list if unavailable)."""
        if self._store is None:
            return []
        import asyncio

        try:
            loop = asyncio.get_event_loop()
            readings = loop.run_until_complete(
                self._store.get_sensor_history(sensor_id, limit=n)
            )
            return [r["value"] for r in readings]
        except Exception:
            logger.warning(
                "EvalContext.last_n: could not fetch history for %r", sensor_id
            )
            return []

    def as_dict(self) -> dict[str, Any]:
        """Return the namespace dict used for ``eval``."""
        return {**self._values, "history": self}


def _validate_sensor_value(value: Any, rule_name: str) -> None:
    """Raise :exc:`RuleEngineSafetyError` if *value* is not a finite number.

    Rejects NaN, ±Inf, and any non-numeric type.  Called before the eval
    context is constructed so that malformed readings never reach rule
    expressions.
    """
    if not isinstance(value, (int, float)):
        raise RuleEngineSafetyError(
            f"Rule {rule_name!r}: sensor value {value!r} is not numeric "
            f"(got {type(value).__name__}). Refusing to evaluate rules against "
            "a non-numeric reading."
        )
    if math.isnan(value):
        raise RuleEngineSafetyError(
            f"Rule {rule_name!r}: sensor value is NaN. "
            "NaN in a rule expression produces undefined comparisons."
        )
    if math.isinf(value):
        raise RuleEngineSafetyError(
            f"Rule {rule_name!r}: sensor value is {'Inf' if value > 0 else '-Inf'}. "
            "Infinite values cannot be safely used in rule expressions."
        )


def _check_safety(condition: str) -> None:
    """Raise :exc:`RuleEngineSafetyError` if *condition* contains forbidden patterns."""
    for pattern in _UNSAFE_PATTERNS:
        if pattern in condition:
            raise RuleEngineSafetyError(
                f"Rule condition contains forbidden pattern {pattern!r}: {condition!r}"
            )


def _now_ms() -> int:
    return int(time.time() * 1000)


class RuleEngine:
    """Deterministic, LLM-free rule evaluator — Tier 1 of the Intelligence Elevator.

    Rules are plain dicts (as loaded from skill YAML).  Each rule must carry:

    - ``name`` (str)
    - ``condition`` (str) — a Python expression evaluated against sensor values
    - ``action_tier`` (str) — ``'A'``, ``'B'``, ``'C'``, or ``'D'``

    Optional rule keys:

    - ``bypass_llm`` (bool, default ``False``)
    - ``escalate_to`` (str) — which Intelligence Elevator tier to use if the
      rule matches but does *not* bypass the LLM
    - ``action`` (str) — action name to pass to the dispatcher
    - ``cooldown_seconds`` (int, default ``0``) — minimum seconds between
      successive fires of this rule

    **Tier D handling:** Any rule with ``bypass_llm=True`` and
    ``action_tier='D'`` is treated as safety-critical.  The engine returns
    immediately upon encountering the first such rule that matches — it does
    not continue evaluating remaining rules.
    """

    def __init__(self) -> None:
        # rule_name → last-fired timestamp
        self._cooldowns: dict[str, _CooldownRecord] = {}

    def evaluate(
        self,
        event: OriEvent,
        rules: list[dict[str, Any]],
        context: dict[str, Any] | None = None,
        state_store: Any = None,
    ) -> RuleResult:
        """Evaluate *rules* against *event* and return the first match.

        Args:
            event: The incoming sensor event.
            rules: Ordered list of rule dicts (from skill YAML ``triggers``).
            context: Extra key→value pairs available inside condition expressions
                (e.g. ``{'rated_capacity': 10.0}``).  ``value``, ``sensor_id``,
                and ``sensor_type`` are always injected from the event.
            state_store: Optional :class:`~ori.state.store.StateStore` instance
                passed to :class:`EvalContext` for history helpers.

        Returns:
            A :class:`RuleResult`.  ``matched=False`` means no rule fired.

        Raises:
            :exc:`RuleEngineSafetyError`: if any rule condition contains a
                forbidden pattern (checked before evaluation).
        """
        base_ctx: dict[str, Any] = dict(context or {})
        if event.reading is not None:
            first_rule_name = rules[0].get("name", "<unnamed>") if rules else "<unknown>"
            _validate_sensor_value(event.reading.value, first_rule_name)
            base_ctx.setdefault("value", event.reading.value)
            base_ctx.setdefault("sensor_id", event.reading.sensor_id)
            base_ctx.setdefault("sensor_type", event.reading.sensor_type)
            base_ctx.setdefault("unit", event.reading.unit)
            base_ctx.setdefault("quality", event.reading.quality)

        eval_ctx = EvalContext(base_ctx, state_store)
        namespace = eval_ctx.as_dict()

        # Safety-check all conditions up front before evaluating anything.
        for rule in rules:
            condition = rule.get("condition", "")
            if condition:
                _check_safety(condition)

        for rule in rules:
            name: str = rule.get("name", "<unnamed>")
            condition: str = rule.get("condition", "")
            bypass_llm: bool = bool(rule.get("bypass_llm", False))
            action_tier: str = str(rule.get("action_tier", "A"))
            escalate_to: str | None = rule.get("escalate_to")
            action: str | None = rule.get("action")
            cooldown_s: int = int(rule.get("cooldown_seconds", 0))

            if not condition:
                continue

            try:
                matched = bool(eval(condition, {"__builtins__": {}}, namespace))  # noqa: S307
            except Exception:
                logger.exception(
                    "RuleEngine: error evaluating condition %r for rule %r",
                    condition,
                    name,
                )
                continue

            if not matched:
                continue

            # Check cooldown
            if cooldown_s > 0:
                rec = self._cooldowns.get(name)
                if (
                    rec is not None
                    and (_now_ms() - rec.last_fired_ms) < cooldown_s * 1000
                ):
                    logger.debug(
                        "RuleEngine: rule %r suppressed by cooldown (%ds)",
                        name,
                        cooldown_s,
                    )
                    continue

            # Record fire time
            self._cooldowns[name] = _CooldownRecord(last_fired_ms=_now_ms())

            logger.info(
                "RuleEngine: rule %r matched (tier=%s, bypass_llm=%s)",
                name,
                action_tier,
                bypass_llm,
            )

            return RuleResult(
                matched=True,
                rule_name=name,
                escalate_to=escalate_to,
                bypass_llm=bypass_llm,
                action=action,
                action_tier=action_tier,
                confidence=1.0,
            )

        return RuleResult(matched=False, action_tier="A")
