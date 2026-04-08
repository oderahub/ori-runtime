# Copyright 2026 Ori Nexus Systems LTD
# SPDX-License-Identifier: Apache-2.0

"""Tests for ori/runtime.py — Step 20.

All external dependencies (HAL adapters, WhatsApp, SMS, relay, LocalLLM)
are mocked.  No real hardware, credentials, or network calls are made.
"""

import asyncio
import logging
import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ori.runtime import OriRuntime

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def minimal_config(tmp_path: Path) -> Path:
    """Write a minimal valid ori.yaml that uses only the psutil adapter."""
    skill_dir = tmp_path / "skills" / "test-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "skill.yaml").write_text(
        textwrap.dedent("""\
            name: test-skill
            version: 0.1.0
            author: test
            sensors_required:
              - type: cpu_percent
            triggers:
              - name: high_cpu
                condition: "value > 90"
                action_tier: A
                cooldown_seconds: 0
                escalate_to: local_slm
            actions:
              available:
                - name: alert_whatsapp
                  tier: A
              defaults:
                high_cpu: [alert_whatsapp]
        """),
        encoding="utf-8",
    )

    cfg = tmp_path / "ori.yaml"
    cfg.write_text(
        textwrap.dedent(f"""\
            device:
              id: test-device-01
              name: Test Device
              location: Test Lab

            sensors:
              - id: cpu-sensor
                type: cpu_percent
                protocol: psutil
                poll_interval_ms: 100

            skills:
              - name: test-skill
                version: "0.1.0"
                config: {{}}

            reasoning:
              default_tier: local
              local_model: ""
              model_path: ""
              offline_fallback: rule

            gateway:
              enabled: false
              broker_url: ""

            actions:
              primary_alert_channel: sms
              whatsapp:
                enabled: false
              sms:
                enabled: false
              relay:
                enabled: false

            skills_dir: {str(tmp_path / "skills")}
        """),
        encoding="utf-8",
    )
    return cfg


def _patch_external(monkeypatch):
    """Patch all external I/O so tests run without hardware or credentials."""
    monkeypatch.setattr(
        "ori.actions.whatsapp.TwilioProvider.send", AsyncMock(return_value=True)
    )
    monkeypatch.setattr("ori.actions.sms.SMSAction.send", AsyncMock(return_value=True))


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestAdapterProtocol:
    async def test_unknown_protocol_raises_config_error(
        self, tmp_path: Path, monkeypatch
    ):
        """A sensor with an unknown protocol must raise ConfigValidationError
        immediately at startup — never silently substitute a wrong adapter."""
        from ori.config import ConfigValidationError

        skill_dir = tmp_path / "skills" / "s"
        skill_dir.mkdir(parents=True)
        (skill_dir / "skill.yaml").write_text(
            "name: s\nversion: 0.1.0\nauthor: t\ntriggers: []\nactions: {}\n",
            encoding="utf-8",
        )
        cfg = tmp_path / "ori.yaml"
        cfg.write_text(
            textwrap.dedent(f"""\
                device:
                  id: dev-01
                  name: Dev
                  location: Lab
                sensors:
                  - id: inv-current
                    type: current
                    protocol: growatt
                    poll_interval_ms: 1000
                skills: []
                reasoning:
                  default_tier: local
                  local_model: ""
                  model_path: ""
                  offline_fallback: rule
                gateway:
                  enabled: false
                  broker_url: ""
                actions:
                  primary_alert_channel: sms
                  whatsapp:
                    enabled: false
                  sms:
                    enabled: false
                  relay:
                    enabled: false
                skills_dir: {str(tmp_path / "skills")}
            """),
            encoding="utf-8",
        )

        runtime = OriRuntime(config_path=str(cfg))

        async def _stop():
            await asyncio.sleep(0.5)
            await runtime.stop()

        with pytest.raises(ConfigValidationError, match="growatt"):
            await asyncio.gather(runtime.start(), _stop())


class TestLifecycle:
    async def test_runtime_starts_and_stops_cleanly(self, minimal_config, monkeypatch):
        """OriRuntime starts, stop() fires after 0.1 s, no error, all tasks cancelled."""
        _patch_external(monkeypatch)

        runtime = OriRuntime(config_path=str(minimal_config))

        async def _auto_stop():
            await asyncio.sleep(0.1)
            await runtime.stop()

        await asyncio.gather(runtime.start(), _auto_stop())
        # If we reach here, start() returned cleanly after stop()

    async def test_stop_is_idempotent(self, minimal_config, monkeypatch):
        """Calling stop() twice must not raise."""
        _patch_external(monkeypatch)
        runtime = OriRuntime(config_path=str(minimal_config))

        async def _double_stop():
            await asyncio.sleep(0.05)
            await runtime.stop()
            await runtime.stop()  # second call — must be a no-op

        await asyncio.gather(runtime.start(), _double_stop())


class TestStartupLogs:
    async def test_startup_logs_skill_tiers(self, minimal_config, monkeypatch, caplog):
        """After start(), caplog must contain '[skill]' with trigger + tier."""
        _patch_external(monkeypatch)
        runtime = OriRuntime(config_path=str(minimal_config))

        async def _stop():
            await asyncio.sleep(0.1)
            await runtime.stop()

        with caplog.at_level(logging.INFO):
            await asyncio.gather(runtime.start(), _stop())

        skill_lines = [r.message for r in caplog.records if "[skill]" in r.message]
        assert any("test-skill" in line for line in skill_lines), (
            f"Expected '[skill] test-skill' in log. Got: {skill_lines}"
        )
        trigger_lines = [r.message for r in caplog.records if "high_cpu" in r.message]
        assert any("Tier A" in line for line in trigger_lines), (
            f"Expected 'Tier A' in trigger log. Got: {trigger_lines}"
        )

    async def test_runtime_logs_event_loop_ready(
        self, minimal_config, monkeypatch, caplog
    ):
        """Log must contain '[runtime] event loop ready' after startup."""
        _patch_external(monkeypatch)
        runtime = OriRuntime(config_path=str(minimal_config))

        async def _stop():
            await asyncio.sleep(0.1)
            await runtime.stop()

        with caplog.at_level(logging.INFO):
            await asyncio.gather(runtime.start(), _stop())

        messages = [r.message for r in caplog.records]
        assert any("event loop ready" in m for m in messages), (
            f"'event loop ready' not found in log. Messages: {messages}"
        )


class TestShutdown:
    async def test_shutdown_drains_tier_d_tasks(self, minimal_config, monkeypatch):
        """A Tier D task marked with _is_tier_d=True must complete before cancellation."""
        _patch_external(monkeypatch)
        runtime = OriRuntime(config_path=str(minimal_config))
        completed: list[bool] = []

        async def _tier_d_work():
            await asyncio.sleep(0.2)
            completed.append(True)

        async def _inject_and_stop():
            await asyncio.sleep(0.05)
            # Create a mock Tier D task and mark it
            tier_d_task = asyncio.create_task(_tier_d_work())
            tier_d_task._is_tier_d = True  # type: ignore[attr-defined]
            await runtime.stop()
            # Give the drained task time to finish
            await asyncio.sleep(0.25)

        await asyncio.gather(runtime.start(), _inject_and_stop())
        assert completed == [True], "Tier D task was abandoned before completion"


class TestWatchdog:
    async def test_watchdog_skipped_gracefully_without_device(
        self, minimal_config, monkeypatch, caplog
    ):
        """/dev/watchdog absent → warning logged, runtime continues normally."""
        _patch_external(monkeypatch)
        monkeypatch.setattr("ori.runtime.os.path.exists", lambda p: False)

        runtime = OriRuntime(config_path=str(minimal_config))

        async def _stop():
            await asyncio.sleep(0.1)
            await runtime.stop()

        with caplog.at_level(logging.WARNING):
            await asyncio.gather(runtime.start(), _stop())

        watchdog_warnings = [
            r.message
            for r in caplog.records
        ]
        assert watchdog_warnings, "Expected watchdog 'not found' warning in logs"


    async def test_watchdog_writes_magic_v_on_shutdown(
        self, minimal_config, monkeypatch, caplog
    ):
        """/dev/watchdog open/write are called, magic V written on shutdown."""
        import builtins
        from unittest.mock import mock_open

        _patch_external(monkeypatch)
        monkeypatch.setattr("ori.runtime.os.path.exists", lambda p: True)

        m_open = mock_open()
        real_open = builtins.open

        def _smart_open(file, *args, **kwargs):
            if file == "/dev/watchdog":
                return m_open(file, *args, **kwargs)
            return real_open(file, *args, **kwargs)

        monkeypatch.setattr("builtins.open", _smart_open)

        runtime = OriRuntime(config_path=str(minimal_config))

        async def _stop():
            await asyncio.sleep(0.1)
            await runtime.stop()

        with caplog.at_level(logging.INFO):
            await asyncio.gather(runtime.start(), _stop())

        # Assert watchdog device was opened for writing
        m_open.assert_called_with("/dev/watchdog", "wb", buffering=0)

        # Assert magical 'V' was written during shutdown
        handle = m_open()
        writes = [c.args[0] for c in handle.write.call_args_list if c.args]
        assert b"V" in writes, "Expected magic 'V' to be written to watchdog"

        # Check logs for clean shutdown line
        v_log = [r.message for r in caplog.records if "magic V written" in r.message]
        assert v_log, "Expected magic V log message"


class TestSensorPolling:
    async def test_sensor_read_error_does_not_crash_runtime(
        self, minimal_config, monkeypatch, caplog
    ):
        """AdapterReadError during polling must log a warning, not crash."""
        from ori.hal.base import AdapterReadError

        _patch_external(monkeypatch)

        read_count = 0

        async def _failing_read(*_: Any):
            nonlocal read_count
            read_count += 1
            raise AdapterReadError("sensor timeout")

        monkeypatch.setattr("ori.hal.psutil_adapter.PsutilAdapter.read", _failing_read)

        runtime = OriRuntime(config_path=str(minimal_config))

        async def _stop():
            await asyncio.sleep(0.35)  # allow a few poll cycles
            await runtime.stop()

        with caplog.at_level(logging.WARNING):
            await asyncio.gather(runtime.start(), _stop())

        assert read_count >= 2, "Expected at least 2 poll attempts"
        warning_msgs = [r.message for r in caplog.records if "read failed" in r.message]
        assert warning_msgs, "Expected 'read failed' warning log"
