# Copyright 2026 Ori Nexus Systems LTD
# SPDX-License-Identifier: Apache-2.0

import time
from unittest.mock import patch

import pytest

from ori.network.events import OriEvent, SensorReading
from ori.reasoning.rule_engine import (
    EvalContext,
    RuleEngine,
    RuleEngineSafetyError,
    RuleResult,
    _validate_sensor_value,
)

# ─── Helpers ──────────────────────────────────────────────────────────────────


def _ms() -> int:
    return int(time.time() * 1000)


def _reading(
    value: float = 5.0,
    sensor_id: str = "load-current",
    sensor_type: str = "current_clamp",
) -> SensorReading:
    return SensorReading(
        sensor_id=sensor_id,
        sensor_type=sensor_type,
        value=value,
        unit="ampere",
        timestamp=_ms(),
        quality=1.0,
    )


def _event(value: float = 5.0, sensor_type: str = "current_clamp") -> OriEvent:
    return OriEvent.from_reading(
        _reading(value=value, sensor_type=sensor_type), "dev-01"
    )


def _rule(
    name: str = "overcurrent",
    condition: str = "value > 10.0",
    action_tier: str = "A",
    bypass_llm: bool = False,
    action: str | None = "alert_whatsapp",
    escalate_to: str | None = None,
    cooldown_seconds: int = 0,
) -> dict:
    r: dict = {
        "name": name,
        "condition": condition,
        "action_tier": action_tier,
        "bypass_llm": bypass_llm,
        "cooldown_seconds": cooldown_seconds,
    }
    if action is not None:
        r["action"] = action
    if escalate_to is not None:
        r["escalate_to"] = escalate_to
    return r


# ─── RuleResult defaults ──────────────────────────────────────────────────────


class TestRuleResult:
    def test_defaults(self):
        result = RuleResult(matched=True, action_tier="A")
        assert result.rule_name is None
        assert result.escalate_to is None
        assert result.bypass_llm is False
        assert result.action is None
        assert result.confidence == 1.0

    def test_unmatched_result(self):
        result = RuleResult(matched=False, action_tier="A")
        assert result.matched is False


# ─── Basic evaluation ─────────────────────────────────────────────────────────


class TestEvaluate:
    def test_condition_true_returns_match(self):
        engine = RuleEngine()
        result = engine.evaluate(_event(value=15.0), [_rule(condition="value > 10.0")])
        assert result.matched is True
        assert result.rule_name == "overcurrent"

    def test_condition_false_returns_no_match(self):
        engine = RuleEngine()
        result = engine.evaluate(_event(value=5.0), [_rule(condition="value > 10.0")])
        assert result.matched is False

    def test_first_matching_rule_wins(self):
        engine = RuleEngine()
        rules = [
            _rule(name="low", condition="value > 3.0", action_tier="A"),
            _rule(name="high", condition="value > 8.0", action_tier="B"),
        ]
        result = engine.evaluate(_event(value=10.0), rules)
        assert result.rule_name == "low"

    def test_second_rule_matches_when_first_does_not(self):
        engine = RuleEngine()
        rules = [
            _rule(name="high", condition="value > 20.0", action_tier="C"),
            _rule(name="medium", condition="value > 8.0", action_tier="B"),
        ]
        result = engine.evaluate(_event(value=10.0), rules)
        assert result.rule_name == "medium"
        assert result.action_tier == "B"

    def test_no_rules_returns_no_match(self):
        engine = RuleEngine()
        assert engine.evaluate(_event(), []).matched is False

    def test_empty_condition_skipped(self):
        engine = RuleEngine()
        rules = [{"name": "empty", "condition": "", "action_tier": "A"}]
        assert engine.evaluate(_event(), rules).matched is False

    def test_result_carries_action_tier(self):
        engine = RuleEngine()
        result = engine.evaluate(
            _event(value=15.0),
            [_rule(condition="value > 10.0", action_tier="B")],
        )
        assert result.action_tier == "B"

    def test_result_carries_action(self):
        engine = RuleEngine()
        result = engine.evaluate(
            _event(value=15.0),
            [_rule(condition="value > 10.0", action="trip_breaker")],
        )
        assert result.action == "trip_breaker"

    def test_result_carries_escalate_to(self):
        engine = RuleEngine()
        result = engine.evaluate(
            _event(value=15.0),
            [_rule(condition="value > 10.0", escalate_to="local_slm")],
        )
        assert result.escalate_to == "local_slm"

    def test_confidence_is_1_for_rule_match(self):
        engine = RuleEngine()
        result = engine.evaluate(_event(value=15.0), [_rule(condition="value > 10.0")])
        assert result.confidence == 1.0

    def test_sensor_context_injected_from_reading(self):
        """sensor_id, sensor_type, unit, quality are available in conditions."""
        engine = RuleEngine()
        rules = [
            _rule(name="r", condition="sensor_type == 'current_clamp'", action_tier="A")
        ]
        assert engine.evaluate(_event(), rules).matched is True

    def test_extra_context_available_in_condition(self):
        engine = RuleEngine()
        rules = [_rule(condition="value > rated_capacity * 0.8", action_tier="A")]
        result = engine.evaluate(
            _event(value=9.0), rules, context={"rated_capacity": 10.0}
        )
        assert result.matched is True

    def test_broken_condition_is_skipped(self):
        """Conditions that raise at eval time are logged and skipped, not raised."""
        engine = RuleEngine()
        rules = [
            _rule(name="broken", condition="undefined_var > 5", action_tier="A"),
            _rule(name="ok", condition="value > 0", action_tier="A"),
        ]
        result = engine.evaluate(_event(value=1.0), rules)
        assert result.matched is True
        assert result.rule_name == "ok"


# ─── Action tiers ─────────────────────────────────────────────────────────────


class TestActionTiers:
    def test_tier_a_rule(self):
        engine = RuleEngine()
        result = engine.evaluate(
            _event(value=15.0), [_rule(condition="value > 10.0", action_tier="A")]
        )
        assert result.action_tier == "A"

    def test_tier_b_rule(self):
        engine = RuleEngine()
        result = engine.evaluate(
            _event(value=15.0), [_rule(condition="value > 10.0", action_tier="B")]
        )
        assert result.action_tier == "B"

    def test_tier_c_rule(self):
        engine = RuleEngine()
        result = engine.evaluate(
            _event(value=15.0), [_rule(condition="value > 10.0", action_tier="C")]
        )
        assert result.action_tier == "C"
        assert result.matched is True

    def test_tier_d_bypass_llm_returns_immediately(self):
        """Tier D with bypass_llm=True fires before other rules are evaluated."""
        engine = RuleEngine()
        rules = [
            _rule(
                name="dangerous_overcurrent",
                condition="value > 5.0",
                action_tier="D",
                bypass_llm=True,
            ),
            _rule(name="should_not_reach", condition="value > 5.0", action_tier="A"),
        ]
        result = engine.evaluate(_event(value=10.0), rules)
        assert result.matched is True
        assert result.action_tier == "D"
        assert result.bypass_llm is True
        assert result.rule_name == "dangerous_overcurrent"

    def test_tier_d_not_matched_continues_to_next_rule(self):
        """If the Tier D condition is false, evaluation continues normally."""
        engine = RuleEngine()
        rules = [
            _rule(
                name="extreme",
                condition="value > 50.0",
                action_tier="D",
                bypass_llm=True,
            ),
            _rule(name="medium", condition="value > 5.0", action_tier="B"),
        ]
        result = engine.evaluate(_event(value=10.0), rules)
        assert result.action_tier == "B"
        assert result.rule_name == "medium"

    def test_bypass_llm_propagated_in_result(self):
        engine = RuleEngine()
        result = engine.evaluate(
            _event(value=15.0),
            [_rule(condition="value > 10.0", bypass_llm=True, action_tier="D")],
        )
        assert result.bypass_llm is True


# ─── Safety checks ────────────────────────────────────────────────────────────


class TestSafetyChecks:
    @pytest.mark.parametrize(
        "condition",
        [
            "import os; os.system('rm -rf /')",
            "__import__('os')",
            "exec('print(1)')",
            "eval('1+1')",
            "open('/etc/passwd')",
            "os.path.exists('/')",
            "sys.exit()",
            "subprocess.run(['ls'])",
        ],
    )
    def test_unsafe_condition_raises(self, condition: str):
        engine = RuleEngine()
        with pytest.raises(RuleEngineSafetyError):
            engine.evaluate(_event(), [_rule(condition=condition)])

    def test_safe_condition_does_not_raise(self):
        engine = RuleEngine()
        engine.evaluate(_event(value=15.0), [_rule(condition="value > 10.0")])

    def test_safety_check_runs_before_any_evaluation(self):
        """Even if the first rule is safe, an unsafe later rule still raises."""
        engine = RuleEngine()
        rules = [
            _rule(name="safe", condition="value > 10.0", action_tier="A"),
            _rule(name="unsafe", condition="__import__('os')", action_tier="A"),
        ]
        with pytest.raises(RuleEngineSafetyError):
            engine.evaluate(_event(value=15.0), rules)

    def test_builtins_not_available_in_condition(self):
        """Built-in functions like len() are not available in conditions."""
        engine = RuleEngine()
        rules = [_rule(name="r", condition="len([1,2,3]) > 0", action_tier="A")]
        # len is not in __builtins__={} — this should be skipped (eval error), not matched
        result = engine.evaluate(_event(), rules)
        assert result.matched is False


# ─── Cooldown ─────────────────────────────────────────────────────────────────


class TestCooldown:
    def test_rule_fires_on_first_match(self):
        engine = RuleEngine()
        result = engine.evaluate(
            _event(value=15.0),
            [_rule(condition="value > 10.0", cooldown_seconds=60)],
        )
        assert result.matched is True

    def test_rule_suppressed_within_cooldown(self):
        engine = RuleEngine()
        rules = [_rule(condition="value > 10.0", cooldown_seconds=60)]
        engine.evaluate(_event(value=15.0), rules)
        result = engine.evaluate(_event(value=15.0), rules)
        assert result.matched is False

    def test_rule_fires_again_after_cooldown_expires(self):
        engine = RuleEngine()
        rules = [_rule(name="r", condition="value > 10.0", cooldown_seconds=5)]
        now = _ms()

        with patch("ori.reasoning.rule_engine._now_ms", return_value=now):
            engine.evaluate(_event(value=15.0), rules)

        # 6 seconds later — cooldown expired
        with patch("ori.reasoning.rule_engine._now_ms", return_value=now + 6_000):
            result = engine.evaluate(_event(value=15.0), rules)

        assert result.matched is True

    def test_zero_cooldown_always_fires(self):
        engine = RuleEngine()
        rules = [_rule(condition="value > 10.0", cooldown_seconds=0)]
        engine.evaluate(_event(value=15.0), rules)
        result = engine.evaluate(_event(value=15.0), rules)
        assert result.matched is True

    def test_cooldown_is_per_rule_name(self):
        """Two rules with different names have independent cooldowns."""
        engine = RuleEngine()
        rules = [
            _rule(
                name="r1",
                condition="value > 10.0",
                cooldown_seconds=60,
                action_tier="A",
            ),
            _rule(
                name="r2",
                condition="value > 10.0",
                cooldown_seconds=60,
                action_tier="B",
            ),
        ]
        engine.evaluate(_event(value=15.0), rules)
        # r1 is on cooldown → falls through to r2
        result = engine.evaluate(_event(value=15.0), rules)
        assert result.matched is True
        assert result.rule_name == "r2"


# ─── EvalContext ──────────────────────────────────────────────────────────────


class TestEvalContext:
    def test_values_available_in_namespace(self):
        ctx = EvalContext({"value": 5.0, "rated_capacity": 10.0})
        ns = ctx.as_dict()
        assert ns["value"] == 5.0
        assert ns["rated_capacity"] == 10.0

    def test_history_accessible_in_namespace(self):
        ctx = EvalContext({})
        ns = ctx.as_dict()
        assert ns["history"] is ctx

    def test_avg_24h_returns_zero_without_store(self):
        ctx = EvalContext({})
        assert ctx.avg_24h("any_sensor") == 0.0

    def test_last_n_returns_empty_without_store(self):
        ctx = EvalContext({})
        assert ctx.last_n("any_sensor", 5) == []

    def test_event_without_reading(self):
        """Events with no reading still evaluate correctly with provided context."""
        engine = RuleEngine()
        event = OriEvent(
            event_id="hb-001",
            event_type="device.heartbeat",
            device_id="dev-01",
            sensor_id="",
            timestamp=_ms(),
            reading=None,
        )
        rules = [_rule(name="r", condition="uptime_seconds > 3600", action_tier="A")]
        result = engine.evaluate(event, rules, context={"uptime_seconds": 7200})
        assert result.matched is True


# ─── _validate_sensor_value ───────────────────────────────────────────────────


class TestValidateSensorValue:
    def test_nan_raises(self):
        with pytest.raises(RuleEngineSafetyError, match="NaN"):
            _validate_sensor_value(float("nan"), "overcurrent")

    def test_positive_inf_raises(self):
        with pytest.raises(RuleEngineSafetyError, match="Inf"):
            _validate_sensor_value(float("inf"), "overcurrent")

    def test_negative_inf_raises(self):
        with pytest.raises(RuleEngineSafetyError, match="-Inf"):
            _validate_sensor_value(float("-inf"), "overcurrent")

    def test_none_raises(self):
        with pytest.raises(RuleEngineSafetyError, match="not numeric"):
            _validate_sensor_value(None, "overcurrent")

    def test_string_raises(self):
        with pytest.raises(RuleEngineSafetyError, match="not numeric"):
            _validate_sensor_value("8.2", "overcurrent")

    def test_valid_float_passes(self):
        # Must not raise for any finite float, including zero and negatives
        _validate_sensor_value(8.2, "overcurrent")
        _validate_sensor_value(0.0, "overcurrent")
        _validate_sensor_value(-1.5, "overcurrent")

    def test_valid_int_passes(self):
        # int is a subtype of numeric — must be accepted
        _validate_sensor_value(10, "overcurrent")


class TestEvaluateRejectsInvalidSensorValue:
    """evaluate() must raise RuleEngineSafetyError before touching rule expressions."""

    def _event_with_value(self, value) -> OriEvent:
        reading = SensorReading(
            sensor_id="load-current",
            sensor_type="current_clamp",
            value=value,
            unit="ampere",
            timestamp=_ms(),
            quality=1.0,
        )
        return OriEvent.from_reading(reading, "dev-01")

    def test_nan_value_raises_before_eval(self):
        engine = RuleEngine()
        rules = [_rule(name="r", condition="value > 5.0")]
        with pytest.raises(RuleEngineSafetyError, match="NaN"):
            engine.evaluate(self._event_with_value(float("nan")), rules)

    def test_inf_value_raises_before_eval(self):
        engine = RuleEngine()
        rules = [_rule(name="r", condition="value > 5.0")]
        with pytest.raises(RuleEngineSafetyError, match="Inf"):
            engine.evaluate(self._event_with_value(float("inf")), rules)
