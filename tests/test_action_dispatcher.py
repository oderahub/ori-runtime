# Copyright 2026 Ori Nexus Systems LTD
# SPDX-License-Identifier: Apache-2.0

import asyncio
import time
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, patch

from ori.network.events import (
    ActionResult,
    ActionTier,
    OriEvent,
    ReasoningResult,
    SensorReading,
)
from ori.reasoning.action_dispatcher import ActionDispatcher, _parse_approval_response
from ori.reasoning.elevator import SkillContext

# ─── Helpers ──────────────────────────────────────────────────────────────────


def _ms() -> int:
    return int(time.time() * 1000)


def _reading(value: float = 5.0) -> SensorReading:
    return SensorReading(
        sensor_id="load-current",
        sensor_type="current_clamp",
        value=value,
        unit="ampere",
        timestamp=_ms(),
        quality=1.0,
    )


def _event() -> OriEvent:
    return OriEvent.from_reading(_reading(), "dev-01")


@dataclass
class FakeSkill:
    name: str = "test-skill"
    config: dict = field(default_factory=dict)


def _context(skill_config: dict | None = None) -> SkillContext:
    skill = FakeSkill(config=skill_config or {})
    return SkillContext(skill=skill, event=_event(), state_store=None)


def _result(
    text: str = "Load is 40% above baseline.",
    confidence: float = 0.85,
    action_tier: str = "A",
) -> ReasoningResult:
    return ReasoningResult(
        text=text,
        tier="local_slm",
        model="qwen.gguf",
        tokens_used=20,
        latency_ms=500,
        confidence=confidence,
        action_tier=action_tier,
    )


def _mock_store() -> AsyncMock:
    store = AsyncMock()
    store.log_action = AsyncMock(return_value=None)
    return store


# ─── _parse_approval_response (module-level) ──────────────────────────────────


class TestParseApprovalResponse:
    def test_yes_returns_true(self):
        assert _parse_approval_response("YES") is True

    def test_y_returns_true(self):
        assert _parse_approval_response("Y") is True

    def test_approve_returns_true(self):
        assert _parse_approval_response("approve") is True

    def test_go_returns_true(self):
        assert _parse_approval_response("go") is True

    def test_no_returns_false(self):
        assert _parse_approval_response("NO") is False

    def test_cancel_returns_false(self):
        assert _parse_approval_response("cancel") is False

    def test_none_returns_false(self):
        assert _parse_approval_response(None) is False

    def test_gibberish_returns_false(self):
        assert _parse_approval_response("maybe") is False

    def test_case_insensitive(self):
        assert _parse_approval_response("Yes") is True
        assert _parse_approval_response("YES") is True
        assert _parse_approval_response("yes") is True

    def test_strips_whitespace(self):
        assert _parse_approval_response("  yes  ") is True


# ─── Tier A — executes immediately ────────────────────────────────────────────


class TestTierA:
    async def test_executes_immediately(self):
        d = ActionDispatcher()
        result = await d.dispatch(
            "alert_whatsapp", ActionTier.INFORMATIONAL, _context(), _result()
        )
        assert result.executed is True

    async def test_approved_is_none(self):
        d = ActionDispatcher()
        result = await d.dispatch(
            "alert_whatsapp", ActionTier.INFORMATIONAL, _context(), _result()
        )
        assert result.approved is None

    async def test_action_name_in_result(self):
        d = ActionDispatcher()
        result = await d.dispatch(
            "alert_whatsapp", ActionTier.INFORMATIONAL, _context(), _result()
        )
        assert result.action_name == "alert_whatsapp"

    async def test_tier_in_result(self):
        d = ActionDispatcher()
        result = await d.dispatch(
            "alert_sms", ActionTier.INFORMATIONAL, _context(), _result()
        )
        assert result.tier == ActionTier.INFORMATIONAL

    async def test_registered_executor_called(self):
        mock_exec = AsyncMock()
        d = ActionDispatcher()
        d.register_executor("alert_whatsapp", mock_exec)
        ctx = _context()
        await d.dispatch("alert_whatsapp", ActionTier.INFORMATIONAL, ctx, _result())
        mock_exec.assert_awaited_once_with("alert_whatsapp", ctx)

    async def test_no_executor_still_returns_executed_true(self):
        """No executor registered → logs intent only, but executed=True."""
        d = ActionDispatcher()
        result = await d.dispatch(
            "unknown_action", ActionTier.INFORMATIONAL, _context(), _result()
        )
        assert result.executed is True

    async def test_logged_to_state_store(self):
        store = _mock_store()
        ctx = SkillContext(skill=FakeSkill(), event=_event(), state_store=store)
        d = ActionDispatcher()
        await d.dispatch("alert_whatsapp", ActionTier.INFORMATIONAL, ctx, _result())
        store.log_action.assert_awaited_once()

    async def test_returns_action_result_instance(self):
        d = ActionDispatcher()
        result = await d.dispatch(
            "log_to_dashboard", ActionTier.INFORMATIONAL, _context(), _result()
        )
        assert isinstance(result, ActionResult)


# ─── Tier D — executes immediately, safety-critical ──────────────────────────


class TestTierD:
    async def test_executes_immediately(self):
        d = ActionDispatcher()
        result = await d.dispatch(
            "emergency_cutoff", ActionTier.SAFETY_CRITICAL, _context(), _result()
        )
        assert result.executed is True

    async def test_approved_is_none(self):
        d = ActionDispatcher()
        result = await d.dispatch(
            "emergency_cutoff", ActionTier.SAFETY_CRITICAL, _context(), _result()
        )
        assert result.approved is None

    async def test_tier_d_in_result(self):
        d = ActionDispatcher()
        result = await d.dispatch(
            "emergency_cutoff", ActionTier.SAFETY_CRITICAL, _context(), _result()
        )
        assert result.tier == ActionTier.SAFETY_CRITICAL

    async def test_registered_executor_called(self):
        mock_exec = AsyncMock()
        d = ActionDispatcher()
        d.register_executor("emergency_cutoff", mock_exec)
        ctx = _context()
        await d.dispatch("emergency_cutoff", ActionTier.SAFETY_CRITICAL, ctx, _result())
        mock_exec.assert_awaited_once()

    async def test_bypasses_approval_workflow(self):
        d = ActionDispatcher()
        with patch.object(d, "_approval_workflow", new=AsyncMock()) as mock_wf:
            await d.dispatch(
                "emergency_cutoff", ActionTier.SAFETY_CRITICAL, _context(), _result()
            )
        mock_wf.assert_not_awaited()


# ─── Tier B — autonomous by default ──────────────────────────────────────────


class TestTierBAutonomous:
    async def test_executes_without_approval_by_default(self):
        d = ActionDispatcher()
        ctx = _context(skill_config={})
        result = await d.dispatch(
            "switch_power_source", ActionTier.SOFT_PHYSICAL, ctx, _result()
        )
        assert result.executed is True
        assert result.approved is None

    async def test_requires_approval_false_executes_immediately(self):
        d = ActionDispatcher()
        ctx = _context(skill_config={"requires_approval": False})
        result = await d.dispatch(
            "switch_power_source", ActionTier.SOFT_PHYSICAL, ctx, _result()
        )
        assert result.approved is None

    async def test_executor_called_for_tier_b(self):
        mock_exec = AsyncMock()
        d = ActionDispatcher()
        d.register_executor("switch_power_source", mock_exec)
        ctx = _context()
        await d.dispatch(
            "switch_power_source", ActionTier.SOFT_PHYSICAL, ctx, _result()
        )
        mock_exec.assert_awaited_once()


# ─── Tier B with requires_approval=True → approval workflow ──────────────────


class TestTierBWithApproval:
    async def test_calls_approval_workflow_when_configured(self):
        d = ActionDispatcher()
        ctx = _context(skill_config={"requires_approval": True})

        with patch.object(
            d,
            "_approval_workflow",
            new=AsyncMock(
                return_value=ActionResult(
                    action_name="switch_power_source",
                    tier=ActionTier.SOFT_PHYSICAL,
                    executed=True,
                    approved=True,
                    action_taken="switch_power_source",
                    timestamp=_ms(),
                )
            ),
        ) as mock_wf:
            result = await d.dispatch(
                "switch_power_source",
                ActionTier.SOFT_PHYSICAL,
                ctx,
                _result(),
            )

        mock_wf.assert_awaited_once()
        assert result.approved is True

    async def test_does_not_call_approval_without_config(self):
        d = ActionDispatcher()
        ctx = _context(skill_config={})

        with patch.object(d, "_approval_workflow", new=AsyncMock()) as mock_wf:
            await d.dispatch(
                "switch_power_source", ActionTier.SOFT_PHYSICAL, ctx, _result()
            )

        mock_wf.assert_not_awaited()


# ─── Tier C — approval workflow ALWAYS ───────────────────────────────────────


class TestTierC:
    async def test_always_calls_approval_workflow(self):
        d = ActionDispatcher()
        ctx = _context()

        with patch.object(
            d,
            "_approval_workflow",
            new=AsyncMock(
                return_value=ActionResult(
                    action_name="trip_main_breaker",
                    tier=ActionTier.HARD_PHYSICAL,
                    executed=False,
                    approved=False,
                    action_taken="log_to_dashboard",
                    timestamp=_ms(),
                )
            ),
        ) as mock_wf:
            await d.dispatch(
                "trip_main_breaker", ActionTier.HARD_PHYSICAL, ctx, _result()
            )

        mock_wf.assert_awaited_once()

    async def test_tier_c_with_yes_response_executes_action(self):
        d = ActionDispatcher()
        mock_sender = AsyncMock()
        d._alert_sender = mock_sender
        d._config = {"operator_contact": "+234800000000"}
        ctx = _context()

        with patch.object(d, "_listen_for_response", new=AsyncMock(return_value="YES")):
            result = await d.dispatch(
                "trip_main_breaker",
                ActionTier.HARD_PHYSICAL,
                ctx,
                _result(),
                approval_timeout_seconds=10,
            )

        assert result.approved is True
        assert result.executed is True
        assert result.action_taken == "trip_main_breaker"

    async def test_tier_c_with_no_response_executes_safe_default(self):
        d = ActionDispatcher()
        ctx = _context()

        with patch.object(d, "_listen_for_response", new=AsyncMock(return_value="NO")):
            result = await d.dispatch(
                "trip_main_breaker",
                ActionTier.HARD_PHYSICAL,
                ctx,
                _result(),
                safe_default_action="log_to_dashboard",
                approval_timeout_seconds=10,
            )

        assert result.approved is False
        assert result.operator_response == "NO"
        assert result.action_taken == "log_to_dashboard"

    async def test_tier_c_timeout_executes_safe_default(self):
        d = ActionDispatcher()
        ctx = _context()

        async def slow_listen():
            await asyncio.sleep(999)
            return None  # pragma: no cover

        with patch.object(d, "_listen_for_response", new=slow_listen):
            result = await d.dispatch(
                "trip_main_breaker",
                ActionTier.HARD_PHYSICAL,
                ctx,
                _result(),
                safe_default_action="log_to_dashboard",
                approval_timeout_seconds=0,  # expire immediately
            )

        assert result.approved is False
        assert result.action_taken == "log_to_dashboard"

    async def test_tier_c_timeout_escalates_to_secondary(self):
        mock_sender = AsyncMock()
        d = ActionDispatcher(
            alert_sender=mock_sender,
            config={
                "operator_contact": "+234800000000",
                "secondary_contact": "+234800000001",
            },
        )
        ctx = _context()

        async def slow_listen():
            await asyncio.sleep(999)
            return None  # pragma: no cover

        with patch.object(d, "_listen_for_response", new=slow_listen):
            await d.dispatch(
                "trip_main_breaker",
                ActionTier.HARD_PHYSICAL,
                ctx,
                _result(),
                approval_timeout_seconds=0,
            )

        # send() called: once for operator (approval request), once for secondary (escalation)
        assert mock_sender.send.await_count >= 1
        # Check secondary was notified
        calls = [str(c) for c in mock_sender.send.await_args_list]
        assert any("+234800000001" in c for c in calls)

    async def test_tier_c_approved_flag_true_on_yes(self):
        d = ActionDispatcher()
        ctx = _context()

        with patch.object(d, "_listen_for_response", new=AsyncMock(return_value="yes")):
            result = await d.dispatch(
                "trip_main_breaker",
                ActionTier.HARD_PHYSICAL,
                ctx,
                _result(),
                approval_timeout_seconds=10,
            )

        assert result.approved is True

    async def test_tier_c_operator_response_preserved_in_result(self):
        d = ActionDispatcher()
        ctx = _context()

        with patch.object(d, "_listen_for_response", new=AsyncMock(return_value="YES")):
            result = await d.dispatch(
                "trip_main_breaker",
                ActionTier.HARD_PHYSICAL,
                ctx,
                _result(),
                approval_timeout_seconds=10,
            )

        assert result.operator_response == "YES"

    async def test_tier_c_logged_to_store(self):
        store = _mock_store()
        ctx = SkillContext(skill=FakeSkill(), event=_event(), state_store=store)
        d = ActionDispatcher()

        with patch.object(d, "_listen_for_response", new=AsyncMock(return_value="NO")):
            await d.dispatch(
                "trip_main_breaker",
                ActionTier.HARD_PHYSICAL,
                ctx,
                _result(),
                approval_timeout_seconds=10,
            )

        store.log_action.assert_awaited_once()


# ─── Failed action — exception handling ───────────────────────────────────────


class TestFailedAction:
    async def test_executor_raises_returns_executed_false(self):
        async def boom(action, context):
            raise RuntimeError("GPIO failure")

        d = ActionDispatcher()
        d.register_executor("trip_breaker", boom)
        result = await d.dispatch(
            "trip_breaker", ActionTier.INFORMATIONAL, _context(), _result()
        )
        assert result.executed is False

    async def test_executor_raises_still_returns_action_result(self):
        async def boom(action, context):
            raise RuntimeError("network down")

        d = ActionDispatcher()
        d.register_executor("alert_sms", boom)
        result = await d.dispatch(
            "alert_sms", ActionTier.INFORMATIONAL, _context(), _result()
        )
        assert isinstance(result, ActionResult)

    async def test_unhandled_exception_returns_action_result(self):
        d = ActionDispatcher()

        with patch.object(
            d, "_execute_immediately", side_effect=RuntimeError("very unexpected")
        ):
            result = await d.dispatch(
                "alert_whatsapp", ActionTier.INFORMATIONAL, _context(), _result()
            )

        assert isinstance(result, ActionResult)
        assert result.executed is False

    async def test_failed_log_does_not_raise(self):
        store = AsyncMock()
        store.log_action.side_effect = RuntimeError("db locked")
        ctx = SkillContext(skill=FakeSkill(), event=_event(), state_store=store)
        d = ActionDispatcher()
        # Must not raise even when logging fails
        result = await d.dispatch(
            "alert_whatsapp", ActionTier.INFORMATIONAL, ctx, _result()
        )
        assert isinstance(result, ActionResult)


# ─── Logging ──────────────────────────────────────────────────────────────────


class TestLogging:
    async def test_dispatcher_store_used_when_context_has_none(self):
        store = _mock_store()
        ctx = SkillContext(skill=FakeSkill(), event=_event(), state_store=None)
        d = ActionDispatcher(state_store=store)
        await d.dispatch("alert_whatsapp", ActionTier.INFORMATIONAL, ctx, _result())
        store.log_action.assert_awaited_once()

    async def test_context_store_takes_priority(self):
        ctx_store = _mock_store()
        dispatcher_store = _mock_store()
        ctx = SkillContext(skill=FakeSkill(), event=_event(), state_store=ctx_store)
        d = ActionDispatcher(state_store=dispatcher_store)
        await d.dispatch("alert_whatsapp", ActionTier.INFORMATIONAL, ctx, _result())
        ctx_store.log_action.assert_awaited_once()
        dispatcher_store.log_action.assert_not_awaited()

    async def test_no_store_does_not_raise(self):
        ctx = SkillContext(skill=FakeSkill(), event=_event(), state_store=None)
        d = ActionDispatcher(state_store=None)
        result = await d.dispatch(
            "alert_whatsapp", ActionTier.INFORMATIONAL, ctx, _result()
        )
        assert isinstance(result, ActionResult)

    async def test_log_called_on_execution_failure_too(self):
        store = _mock_store()
        ctx = SkillContext(skill=FakeSkill(), event=_event(), state_store=store)
        d = ActionDispatcher()

        async def boom(action, context):
            raise RuntimeError("fail")

        d.register_executor("alert_whatsapp", boom)
        await d.dispatch("alert_whatsapp", ActionTier.INFORMATIONAL, ctx, _result())
        store.log_action.assert_awaited_once()


# ─── Approval message format ─────────────────────────────────────────────────


class TestApprovalMessageFormat:
    def test_contains_device_id(self):
        d = ActionDispatcher()
        msg = d._format_approval_message(
            device_id="dev-lagos-01",
            timestamp_ms=1_700_000_000_000,
            result=_result(text="AC unit drawing 40% above baseline."),
            action="trip_main_breaker",
            timeout_seconds=300,
        )
        assert "dev-lagos-01" in msg

    def test_contains_observation_text(self):
        d = ActionDispatcher()
        msg = d._format_approval_message(
            device_id="dev-01",
            timestamp_ms=_ms(),
            result=_result(text="Dangerous overcurrent detected."),
            action="emergency_cutoff",
            timeout_seconds=60,
        )
        assert "Dangerous overcurrent detected." in msg

    def test_contains_action_name(self):
        d = ActionDispatcher()
        msg = d._format_approval_message(
            device_id="dev-01",
            timestamp_ms=_ms(),
            result=_result(),
            action="trip_main_breaker",
            timeout_seconds=300,
        )
        assert "trip_main_breaker" in msg

    def test_contains_confidence_percentage(self):
        d = ActionDispatcher()
        msg = d._format_approval_message(
            device_id="dev-01",
            timestamp_ms=_ms(),
            result=_result(confidence=0.85),
            action="a",
            timeout_seconds=300,
        )
        assert "85%" in msg

    def test_contains_timeout_value(self):
        d = ActionDispatcher()
        msg = d._format_approval_message(
            device_id="dev-01",
            timestamp_ms=_ms(),
            result=_result(),
            action="a",
            timeout_seconds=120,
        )
        assert "120" in msg

    def test_contains_yes_no_instructions(self):
        d = ActionDispatcher()
        msg = d._format_approval_message(
            device_id="dev-01",
            timestamp_ms=_ms(),
            result=_result(),
            action="a",
            timeout_seconds=300,
        )
        assert "YES" in msg
        assert "NO" in msg


# ─── Unknown tier fallback ────────────────────────────────────────────────────


class TestUnknownTier:
    async def test_unknown_tier_falls_back_to_execute_immediately(self):
        d = ActionDispatcher()
        result = await d.dispatch("some_action", "X", _context(), _result())
        assert result.executed is True
        assert result.approved is None
