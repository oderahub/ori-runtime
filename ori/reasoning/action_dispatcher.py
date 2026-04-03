# Copyright 2026 Ori Nexus Systems LTD
# SPDX-License-Identifier: Apache-2.0

"""Action Dispatcher — the agent's executor.

Routes every :class:`~ori.network.events.ReasoningResult` to the appropriate
execution path based on its action tier:

- **Tier A** (Informational): execute immediately, no approval.
- **Tier B** (Soft Physical): execute immediately *unless* ``requires_approval``
  is ``True`` in the skill config, in which case run the approval workflow.
- **Tier C** (Hard Physical): approval workflow **always**.  No exception.
- **Tier D** (Safety-Critical): execute immediately, highest priority.

Every dispatch attempt produces an :class:`~ori.network.events.ActionResult`
and is logged to the ``action_log`` table — even on failure.  A failed action
must never crash the runtime.
"""

import asyncio
import datetime
import logging
import time
from typing import Any

from ori.network.events import ActionResult, ActionTier, ReasoningResult
from ori.reasoning.elevator import SkillContext

logger = logging.getLogger(__name__)

_YES_TOKENS = frozenset({"yes", "y", "approve", "go", "ok", "confirm"})
_NO_TOKENS = frozenset({"no", "n", "cancel", "stop", "reject", "deny"})

_DEFAULT_APPROVAL_TIMEOUT = 300  # seconds
_DEFAULT_SAFE_DEFAULT_ACTION = "log_to_dashboard"


def _parse_approval_response(response: str | None) -> bool:
    """Return ``True`` if *response* is an affirmative approval token.

    Args:
        response: Raw operator reply string, or ``None``.

    Returns:
        ``True`` if the response is YES/Y/approve/go/ok/confirm
        (case-insensitive).  ``False`` for NO tokens, ``None``, or
        anything unrecognised.
    """
    if response is None:
        return False
    token = response.strip().lower()
    return token in _YES_TOKENS


def _now_ms() -> int:
    return int(time.time() * 1000)


class ActionDispatcher:
    """Routes reasoning results to execution paths based on action tier.

    Action executors (WhatsApp, SMS, relay, etc.) are registered via
    :meth:`register_executor`.  If no executor is registered for an action
    name, the action is logged as not-executed and the dispatcher moves on.

    Args:
        state_store: :class:`~ori.state.store.StateStore` for logging.
            Falls back to ``context.state_store`` at dispatch time if not set.
        alert_sender: Object with ``send(to: str, message: str) -> Awaitable``
            used to deliver approval messages. ``None`` disables the approval
            workflow (actions fall back to safe_default immediately).
        config: Dispatcher-level config dict.  Recognised keys:
            ``operator_contact`` (str), ``secondary_contact`` (str),
            ``approval_timeout_seconds`` (int), ``primary_alert_channel`` (str).
    """

    def __init__(
        self,
        state_store: Any = None,
        alert_sender: Any = None,
        config: dict | None = None,
    ) -> None:
        self._state_store = state_store
        self._alert_sender = alert_sender
        self._config: dict = config or {}
        self._executors: dict[str, Any] = {}

    # ── Public API ────────────────────────────────────────────────────────────

    def register_executor(self, action_name: str, executor: Any) -> None:
        """Register a callable for *action_name*.

        The callable must be an async function with signature::

            async def execute(action: str, context: SkillContext) -> None

        Args:
            action_name: The action identifier (e.g. ``'alert_whatsapp'``).
            executor: Async callable invoked when the action fires.
        """
        self._executors[action_name] = executor

    async def dispatch(
        self,
        action: str,
        tier: str,
        context: SkillContext,
        result: ReasoningResult,
        safe_default_action: str = _DEFAULT_SAFE_DEFAULT_ACTION,
        approval_timeout_seconds: int = _DEFAULT_APPROVAL_TIMEOUT,
    ) -> ActionResult:
        """Route *action* to the correct execution path for *tier*.

        Tier routing:

        - **D**: :meth:`_execute_immediately` — no approval, immediate.
        - **A**: :meth:`_execute_immediately`.
        - **B**: :meth:`_execute_immediately`, unless
          ``context.skill.config.get('requires_approval')`` is truthy, in
          which case :meth:`_approval_workflow`.
        - **C**: :meth:`_approval_workflow` — always, no exception.

        All exceptions are caught.  An :class:`~ori.network.events.ActionResult`
        is always returned and logged.

        Args:
            action: Action name (e.g. ``'alert_whatsapp'``).
            tier: Action tier — ``'A'`` | ``'B'`` | ``'C'`` | ``'D'``.
            context: :class:`~ori.reasoning.elevator.SkillContext` carrying
                the skill, event, and state_store.
            result: The :class:`~ori.network.events.ReasoningResult` from
                the Intelligence Elevator.
            safe_default_action: Action to execute on Tier C approval timeout
                or NO response.  Overrides the dispatcher default.
            approval_timeout_seconds: Seconds to wait for operator approval
                before executing *safe_default_action*.

        Returns:
            :class:`~ori.network.events.ActionResult` describing what happened.
        """
        try:
            if tier == ActionTier.SAFETY_CRITICAL:
                action_result = await self._execute_immediately(action, tier, context)

            elif tier == ActionTier.INFORMATIONAL:
                action_result = await self._execute_immediately(action, tier, context)

            elif tier == ActionTier.SOFT_PHYSICAL:
                requires_approval = False
                if hasattr(context, "skill") and hasattr(context.skill, "config"):
                    requires_approval = bool(
                        context.skill.config.get("requires_approval", False)
                    )
                if requires_approval:
                    action_result = await self._approval_workflow(
                        action,
                        tier,
                        context,
                        result,
                        safe_default_action,
                        approval_timeout_seconds,
                    )
                else:
                    action_result = await self._execute_immediately(action, tier, context)

            elif tier == ActionTier.HARD_PHYSICAL:
                # Always approval workflow — no exception, no config override
                action_result = await self._approval_workflow(
                    action,
                    tier,
                    context,
                    result,
                    safe_default_action,
                    approval_timeout_seconds,
                )

            else:
                logger.warning(
                    "ActionDispatcher: unknown action tier %r for action=%r — "
                    "treating as Tier A",
                    tier,
                    action,
                )
                action_result = await self._execute_immediately(action, tier, context)

        except Exception:
            logger.exception(
                "ActionDispatcher: unhandled exception dispatching action=%r tier=%r",
                action,
                tier,
            )
            action_result = ActionResult(
                action_name=action,
                tier=tier,
                executed=False,
                approved=None,
                action_taken="",
                timestamp=_now_ms(),
            )

        await self._log_action(action_result, context)
        return action_result

    # ── Execution paths ───────────────────────────────────────────────────────

    async def _execute_immediately(
        self,
        action: str,
        tier: str,
        context: SkillContext,
    ) -> ActionResult:
        """Execute *action* without any approval step.

        If an executor is registered for *action*, it is called.  If no
        executor exists, the action is logged as executed (the intent is
        recorded even if no physical action fired).

        Args:
            action: Action name to execute.
            tier: Action tier (passed through to the result).
            context: Skill execution context.

        Returns:
            :class:`~ori.network.events.ActionResult` with ``executed=True``
            on success and ``executed=False`` if the executor raised.
        """
        executed = True
        try:
            executor = self._executors.get(action)
            if executor is not None:
                await executor(action, context)
            else:
                logger.debug(
                    "ActionDispatcher: no executor registered for action=%r — "
                    "logging intent only",
                    action,
                )
        except Exception:
            logger.exception(
                "ActionDispatcher: executor raised for action=%r", action
            )
            executed = False

        return ActionResult(
            action_name=action,
            tier=tier,
            executed=executed,
            approved=None,  # no approval step for A/B/D
            action_taken=action if executed else "",
            timestamp=_now_ms(),
        )

    async def _approval_workflow(
        self,
        action: str,
        tier: str,
        context: SkillContext,
        result: ReasoningResult,
        safe_default_action: str,
        approval_timeout_seconds: int,
    ) -> ActionResult:
        """Send an approval request and wait for YES/NO from the operator.

        Flow:

        1. Format and send the approval message via ``_alert_sender``.
        2. Await :meth:`_listen_for_response` with *approval_timeout_seconds*.
        3. Parse the response:
           - YES/approve → :meth:`_execute_immediately` with original *action*.
           - NO/cancel or ``None`` → :meth:`_execute_immediately` with
             *safe_default_action*.
        4. On timeout: execute *safe_default_action*; escalate to secondary
           contact if configured.

        Args:
            action: The proposed action awaiting approval.
            tier: Action tier.
            context: Skill execution context.
            result: Reasoning result for message formatting.
            safe_default_action: Fallback action on timeout/NO.
            approval_timeout_seconds: Approval wait window.

        Returns:
            :class:`~ori.network.events.ActionResult` with ``approved`` set to
            ``True`` / ``False`` based on the operator response.
        """
        device_id = context.event.device_id if context.event else "unknown"
        message = self._format_approval_message(
            device_id=device_id,
            timestamp_ms=context.event.timestamp if context.event else _now_ms(),
            result=result,
            action=action,
            timeout_seconds=approval_timeout_seconds,
        )

        # Send approval request
        operator_contact = self._config.get("operator_contact", "")
        if self._alert_sender is not None and operator_contact:
            try:
                await self._alert_sender.send(operator_contact, message)
            except Exception:
                logger.exception(
                    "ActionDispatcher: failed to send approval request for action=%r",
                    action,
                )

        # Wait for response
        operator_response: str | None = None
        timed_out = False
        try:
            operator_response = await asyncio.wait_for(
                self._listen_for_response(),
                timeout=float(approval_timeout_seconds),
            )
        except asyncio.TimeoutError:
            timed_out = True
            logger.warning(
                "ActionDispatcher: approval timeout for action=%r after %ds — "
                "executing safe_default=%r",
                action,
                approval_timeout_seconds,
                safe_default_action,
            )

        # Parse response
        approved = _parse_approval_response(operator_response)

        if approved:
            inner = await self._execute_immediately(action, tier, context)
            action_taken = inner.action_taken
            executed = inner.executed
        else:
            # NO, None, or timeout → safe default
            if timed_out:
                await self._escalate_to_secondary(action, context, result)
            inner = await self._execute_immediately(safe_default_action, tier, context)
            action_taken = inner.action_taken
            executed = inner.executed

        return ActionResult(
            action_name=action,
            tier=tier,
            executed=executed,
            approved=approved,
            action_taken=action_taken,
            timestamp=_now_ms(),
            operator_response=operator_response,
        )

    async def _listen_for_response(self) -> str | None:
        """Poll the alert inbox for an operator YES/NO reply.

        **Stub** — returns ``None`` (no response) in the PoC.
        Completed in Step 16 when :mod:`ori.actions.whatsapp` is built;
        at that point this method will be overridden or injected via
        ``alert_sender.listen()``.

        Returns:
            Operator's reply text, or ``None`` if no message was received
            before the caller's ``asyncio.wait_for`` timeout fires.
        """
        # PoC: park here and let wait_for timeout handle the no-response path.
        # Step 16 will replace this with a real inbox poller.
        await asyncio.sleep(3600)  # effectively never returns before timeout
        return None  # pragma: no cover

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _format_approval_message(
        self,
        device_id: str,
        timestamp_ms: int,
        result: ReasoningResult,
        action: str,
        timeout_seconds: int,
    ) -> str:
        """Format the WhatsApp/SMS approval request message.

        Args:
            device_id: The device that triggered the action.
            timestamp_ms: Unix milliseconds timestamp.
            result: The reasoning result with text and confidence.
            action: The proposed action name.
            timeout_seconds: Auto-cancel window.

        Returns:
            Formatted approval message string.
        """
        dt = datetime.datetime.fromtimestamp(
            timestamp_ms / 1000, tz=datetime.timezone.utc
        )
        formatted_time = dt.strftime("%Y-%m-%d %H:%M:%S UTC")

        return (
            f"ORI ALERT — Action Required\n"
            f"Device: {device_id}\n"
            f"Time: {formatted_time}\n"
            f"\n"
            f"OBSERVATION:\n"
            f"{result.text}\n"
            f"\n"
            f"PROPOSED ACTION:\n"
            f"{action}\n"
            f"\n"
            f"CONFIDENCE: {result.confidence:.0%}\n"
            f"\n"
            f"Reply YES to approve  |  Reply NO to cancel\n"
            f"Auto-cancel in {timeout_seconds} seconds if no response."
        )

    async def _escalate_to_secondary(
        self,
        action: str,
        context: SkillContext,
        result: ReasoningResult,
    ) -> None:
        """Notify the secondary contact when a Tier C approval times out.

        No-op if no secondary contact is configured or alert_sender is absent.

        Args:
            action: The action that timed out.
            context: Skill execution context.
            result: Reasoning result for message context.
        """
        secondary = self._config.get("secondary_contact", "")
        if not secondary or self._alert_sender is None:
            return
        device_id = context.event.device_id if context.event else "unknown"
        message = (
            f"ORI ESCALATION — Tier C approval timed out\n"
            f"Device: {device_id}\n"
            f"Action: {action}\n"
            f"Safe default was executed.\n"
            f"Observation: {result.text}"
        )
        try:
            await self._alert_sender.send(secondary, message)
        except Exception:
            logger.exception(
                "ActionDispatcher: failed to send escalation to secondary contact"
            )

    async def _log_action(
        self,
        action_result: ActionResult,
        context: SkillContext,
    ) -> None:
        """Persist *action_result* to the ``action_log`` table.

        Uses ``context.state_store`` first; falls back to the dispatcher's own
        ``_state_store``.  Silently skips logging if no store is available.

        Args:
            action_result: The result to persist.
            context: Skill execution context (carries state_store).
        """
        store = None
        if hasattr(context, "state_store") and context.state_store is not None:
            store = context.state_store
        elif self._state_store is not None:
            store = self._state_store

        if store is None:
            return

        trigger_name = context.event.sensor_id if context.event else ""
        try:
            await store.log_action(action_result, trigger_name)
        except Exception:
            logger.exception(
                "ActionDispatcher: failed to log action=%r to action_log",
                action_result.action_name,
            )
