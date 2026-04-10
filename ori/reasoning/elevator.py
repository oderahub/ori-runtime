# Copyright 2026 Ori Nexus Systems LTD
# SPDX-License-Identifier: Apache-2.0

"""Intelligence Elevator — Tier 1–4 reasoning selector.

The elevator picks the cheapest reasoning tier that can handle a given event
and returns a :class:`~ori.network.events.ReasoningResult`.

.. important::

   Callers MUST use ``asyncio.create_task(elevator.reason_and_dispatch(...))``
   not direct ``await``.  Direct ``await`` blocks EventBus delivery to all
   other subscribers for the full LLM inference duration (3–8 seconds).
   The ``create_task()`` call at the EventBus-handler boundary is the single
   point where control is yielded back to the event loop immediately.
"""

import datetime
import logging
import socket
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ori.network.events import OriEvent, ReasoningResult
from ori.reasoning.rule_engine import RuleEngine, RuleEngineSafetyError

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


# ── Minimal shared types ──────────────────────────────────────────────────────
# Skill and ActionDispatcher are implemented in later build steps.
# SkillContext is defined here so it is available at the EventBus boundary
# before those modules exist.


@dataclass
class SkillContext:
    """Lightweight context bundle threaded through the reasoning pipeline."""

    skill: Any  # Skill instance (loader.py step 14)
    event: OriEvent
    state_store: Any  # StateStore


def _now_ms() -> int:
    return int(time.time() * 1000)


def _hour_now() -> int:
    return datetime.datetime.now().hour


def _is_offline() -> bool:
    """Return ``True`` if no internet connectivity is detectable."""
    try:
        socket.setdefaulttimeout(1)
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect(("8.8.8.8", 53))
        return False
    except OSError:
        return True


def _complexity_score(
    current_value: float,
    avg_24h: float | None,
    history: list[float],
    hour: int,
) -> float:
    """Score 0.0–1.0 reflecting how much reasoning effort an event warrants.

    Three signals contribute equally (1/3 each):

    **Deviation** — how far the current reading is from the 24-hour average.
    Capped at 2× the average; above that it's always 1.0.

    **Volatility** — standard deviation of the last N readings, normalised by
    the mean.  High variance = higher complexity.

    **Unusual hour** — readings outside 06:00–22:00 are slightly elevated
    because nocturnal anomalies are less expected.
    """
    scores: list[float] = []

    # 1. Deviation from 24-hour average
    if avg_24h and avg_24h > 0:
        deviation_ratio = abs(current_value - avg_24h) / avg_24h
        scores.append(min(deviation_ratio / 2.0, 1.0))
    else:
        scores.append(0.0)

    # 2. Recent history volatility (coefficient of variation)
    if len(history) >= 2:
        mean = sum(history) / len(history)
        if mean > 0:
            variance = sum((x - mean) ** 2 for x in history) / len(history)
            cv = (variance**0.5) / mean
            scores.append(min(cv, 1.0))
        else:
            scores.append(0.0)
    else:
        scores.append(0.0)

    # 3. Unusual hour (outside 06:00–22:00)
    scores.append(0.0 if 6 <= hour <= 22 else 0.3)

    return sum(scores) / len(scores)


class IntelligenceElevator:
    """Selects the cheapest reasoning tier and returns a :class:`~ori.network.events.ReasoningResult`.

    Tier selection order (cheapest first):

    1. **Rule engine** — microseconds, always evaluated first.
       If a Tier D rule fires, returns immediately without LLM.
    2. **Local SLM** — 3–8 seconds, offline capable.
    3. **Gateway LLM** — 1–3 seconds, LAN required (not yet wired).
    4. **Cloud LLM** — 2–5 seconds, internet required (not yet wired).

    Fallback is always ``local_slm``.

    Args:
        local_llm: A :class:`~ori.reasoning.local_llm.LocalLLM` instance
            (optional — if ``None`` or unavailable, falls back to a stub result).
        rule_engine: A :class:`~ori.reasoning.rule_engine.RuleEngine` instance.
            Created internally if not provided.
    """

    def __init__(
        self,
        local_llm: Any = None,
        rule_engine: RuleEngine | None = None,
        config: Any = None,
    ) -> None:
        self._local_llm = local_llm
        self._rule_engine = rule_engine or RuleEngine()
        self._config = config  # ReasoningConfig from ori.yaml; None in test environments

    # ── Public API ────────────────────────────────────────────────────────────

    async def reason(
        self,
        event: OriEvent,
        skill: Any,
        state_store: Any,
    ) -> ReasoningResult:
        """Select the cheapest tier and return a reasoning result.

        .. warning::

           Must only be called via ``asyncio.create_task()``, never with direct
           ``await``, when invoked from an EventBus handler.

        Args:
            event: The sensor event to reason about.
            skill: The :class:`~ori.skills.loader.Skill` instance whose triggers
                are evaluated first by the rule engine.
            state_store: A :class:`~ori.state.store.StateStore` for history
                queries used in complexity scoring.

        Returns:
            :class:`~ori.network.events.ReasoningResult` from whichever tier
            handled the request.
        """
        tier = await self.select_tier(event, skill, state_store)

        rule_result, hook_ctx = await self._evaluate_rules_with_hooks(event, skill, state_store)

        if tier == "rule":
            return ReasoningResult(
                text=f"Rule matched: {rule_result.rule_name}",
                tier="rule",
                model="rule_engine",
                tokens_used=0,
                latency_ms=0,
                confidence=rule_result.confidence,
                action_tier=rule_result.action_tier,
                proposed_action=rule_result.action,
            )

        if tier in ("local_slm",) and self._local_llm is not None:
            prompt = self._build_prompt(event, skill)
            try:
                result = await self._local_llm.reason(prompt)
                result.prompt = prompt
                if rule_result.matched and result.action_tier != "D":
                    result.action_tier = rule_result.action_tier
                    result.proposed_action = result.proposed_action or rule_result.action
                return result
            except Exception:
                logger.exception(
                    "IntelligenceElevator: local_slm inference failed for "
                    "sensor_id=%s — returning stub result",
                    event.sensor_id if event else "unknown",
                )

        # Fallback stub — gateway/cloud tiers not yet wired
        stub = self._stub_result(tier, event)
        if rule_result.matched and stub.action_tier != "D":
            stub.action_tier = rule_result.action_tier
            stub.proposed_action = rule_result.action
        return stub

    async def _evaluate_rules_with_hooks(self, event: OriEvent, skill: Any, state_store: Any):
        """Build context with derived hook variables, and evaluate against RuleEngine."""
        rules = getattr(skill, "triggers", [])
        ctx: dict[str, Any] = {}
        if hasattr(skill, "config") and isinstance(skill.config, dict):
            ctx.update(skill.config)

        hook_ctx = None
        if hasattr(skill, "hooks") and hasattr(skill.hooks, "pre_trigger_eval"):
            from ori.skills.hooks_api import HookContext
            hook_ctx = HookContext.build(event, state_store, getattr(skill, "name", "unknown"))
            try:
                skill.hooks.pre_trigger_eval(hook_ctx)
                ctx.update(hook_ctx.derived)
            except Exception:
                logger.exception(
                    "IntelligenceElevator: pre_trigger_eval hook failed for %r",
                    getattr(skill, "name", "unknown")
                )

        rule_result = await self._rule_engine.evaluate(event, rules, context=ctx, state_store=state_store)
        return rule_result, hook_ctx

    async def select_tier(
        self,
        event: OriEvent,
        skill: Any,
        state_store: Any,
    ) -> str:
        """Choose ``'rule'`` | ``'local_slm'`` | ``'gateway'`` | ``'cloud'``.

        Selection logic:

        1. Run the rule engine.  If Tier D fires → ``'rule'`` immediately.
        2. Score complexity (0.0–1.0) from deviation, volatility, hour.
        3. ``complexity < 0.3`` OR offline → ``'local_slm'``
           ``complexity < 0.7`` AND LAN available → ``'gateway'`` (future)
           internet available → ``'cloud'`` (future)
           fallback → ``'local_slm'``
        """
        rule_result, _ = await self._evaluate_rules_with_hooks(event, skill, state_store)

        # Tier D is always handled by the rule engine — return immediately
        if rule_result.matched and rule_result.action_tier == "D":
            return "rule"

        # Any bypass_llm rule also skips LLM
        if rule_result.matched and rule_result.bypass_llm:
            return "rule"

        # Complexity scoring needs current value and history
        current_value = event.reading.value if event.reading else 0.0
        avg_24h: float | None = None
        history: list[float] = []

        if state_store is not None and event.reading is not None:
            try:
                avg_24h = await state_store.avg_last_hours(event.reading.sensor_id, 24)
                readings = await state_store.get_history(
                    event.reading.sensor_id, limit=10
                )
                history = [r.value for r in readings]
            except Exception:
                logger.debug(
                    "IntelligenceElevator: could not fetch history for %s",
                    event.sensor_id,
                )

        complexity = _complexity_score(current_value, avg_24h, history, _hour_now())

        offline = _is_offline()
        fallback = getattr(self._config, "offline_fallback", "rule") if self._config else "rule"
        threshold = getattr(self._config, "escalation_threshold", 0.70) if self._config else 0.70

        if offline:
            return fallback

        if complexity < 0.3:
            return "local_slm"

        if complexity < threshold:
            # Future: return "gateway" if LAN available
            return "local_slm"

        # Future: return "cloud" if complexity >= threshold and internet available
        return "local_slm"

    async def reason_and_dispatch(
        self,
        event: OriEvent,
        skill: Any,
        state_store: Any,
        dispatcher: Any,
    ) -> None:
        """Full reasoning + action pipeline.

        This is the coroutine that ``asyncio.create_task()`` wraps at the
        EventBus handler boundary.  All exceptions are caught — a reasoning
        failure must never crash the runtime or go unlogged.

        1. Run :meth:`reason` to get a :class:`~ori.network.events.ReasoningResult`.
        2. Look up the default actions for the event's sensor type from the skill.
        3. Dispatch each action through the :class:`~ori.reasoning.action_dispatcher.ActionDispatcher`.
        4. Log the reasoning result to the ``reasoning_log`` table.

        Args:
            event: The triggering event.
            skill: The matched :class:`~ori.skills.loader.Skill`.
            state_store: :class:`~ori.state.store.StateStore` for logging and history.
            dispatcher: :class:`~ori.reasoning.action_dispatcher.ActionDispatcher`.
        """
        try:
            result = await self.reason(event, skill, state_store)

            if hasattr(skill, "hooks") and hasattr(skill.hooks, "post_reasoning"):
                from ori.skills.hooks_api import HookContext

                # Rule matching trigger identification is useful for post_reasoning
                # even if tier was local_slm.
                rule_res, _ = await self._evaluate_rules_with_hooks(event, skill, state_store)

                pt_ctx = HookContext.build(event, state_store, getattr(skill, "name", "unknown"))
                pt_ctx.trigger_name = rule_res.rule_name if rule_res.matched else ""

                try:
                    skill.hooks.post_reasoning(result, pt_ctx)
                except Exception:
                    logger.exception(
                        "IntelligenceElevator: post_reasoning hook failed for %r",
                        getattr(skill, "name", "unknown")
                    )

            sensor_type = (
                event.reading.sensor_type if event.reading else event.event_type
            )
            actions: list[str] = []
            if hasattr(skill, "get_default_actions"):
                actions = skill.get_default_actions(sensor_type)

            context = SkillContext(skill=skill, event=event, state_store=state_store)
            for action in actions:
                await dispatcher.dispatch(
                    action=action,
                    tier=result.action_tier,
                    context=context,
                    result=result,
                )

            # Persist reasoning result
            if state_store is not None and hasattr(state_store, "log_reasoning"):
                await state_store.log_reasoning(
                    result=result,
                    trigger_name=event.sensor_id,
                    device_id=event.device_id,
                )

        except RuleEngineSafetyError as exc:
            logger.error("IntelligenceElevator: Safety check blocked reasoning: %s", exc)

            text = (
                f"Sensor safety check failed: {exc}. "
                f"Tier D protection for this sensor is suspended until "
                f"the sensor returns valid readings."
            )

            result = ReasoningResult(
                text=text,
                tier="rule",
                model="safety_fallback",
                tokens_used=0,
                latency_ms=0,
                confidence=1.0,
                action_tier="A",
            )

            synthetic_event = OriEvent(
                event_type="sensor.invalid_value",
                event_id=f"syn-{event.event_id}",
                device_id=event.device_id,
                sensor_id=event.sensor_id,
                timestamp=_now_ms(),
                reading=event.reading
            )

            actions = []
            if hasattr(skill, "get_default_actions"):
                actions = skill.get_default_actions("sensor.invalid_value")

            if not actions:
                actions = ["alert_whatsapp"]
                # Dispatch alert_sms if configured
                available_actions = []
                if hasattr(skill, "actions") and isinstance(skill.actions, dict):
                    available_acts = skill.actions.get("available", [])
                    for a in available_acts:
                        if isinstance(a, dict):
                            available_actions.append(a.get("name"))
                        elif isinstance(a, str):
                            available_actions.append(a)
                if "alert_sms" in available_actions:
                    actions.append("alert_sms")

            context = SkillContext(skill=skill, event=synthetic_event, state_store=state_store)
            for action in actions:
                await dispatcher.dispatch(
                    action=action,
                    tier=result.action_tier,
                    context=context,
                    result=result,
                )

        except Exception:
            skill_name = getattr(skill, "name", "<unknown>")
            logger.exception(
                "IntelligenceElevator: reasoning pipeline failed for "
                "skill=%s trigger=%s",
                skill_name,
                event.sensor_id,
            )

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _build_prompt(self, event: OriEvent, skill: Any) -> str:
        """Build a plain-text prompt from the event and skill metadata."""
        lines: list[str] = []
        if event.reading:
            r = event.reading
            lines.append(f"Sensor: {r.sensor_id} ({r.sensor_type})")
            lines.append(f"Current value: {r.value} {r.unit}")
            lines.append(f"Quality: {r.quality}")
        lines.append(f"Device: {event.device_id}")
        prompt_template: str | None = None
        if hasattr(skill, "prompts") and isinstance(skill.prompts, dict):
            prompt_template = skill.prompts.get(
                event.reading.sensor_type if event.reading else ""
            )
        if prompt_template:
            lines.append(prompt_template)
        else:
            lines.append("Is this reading anomalous? What is the most likely cause?")
            lines.append("Answer in plain English, 2-3 sentences, no jargon.")
        return "\n".join(lines)

    @staticmethod
    def _stub_result(tier: str, event: OriEvent) -> ReasoningResult:
        """Return a safe stub result when a tier is unavailable."""
        return ReasoningResult(
            text=f"[{tier} unavailable] No reasoning performed for {event.sensor_id}.",
            tier=tier,
            model="stub",
            tokens_used=0,
            latency_ms=0,
            confidence=0.0,
            action_tier="A",
        )
