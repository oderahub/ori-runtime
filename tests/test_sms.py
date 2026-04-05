# Copyright 2026 Ori Nexus Systems LTD
# SPDX-License-Identifier: Apache-2.0

"""Tests for ori/actions/sms.py.

The Africa's Talking SDK is never imported in these tests — it is stubbed
at the module level via monkeypatch so no live credentials are required.
"""

import sys
import types

import pytest

from ori.actions.sms import SMSAction

# ── AT SDK stub ───────────────────────────────────────────────────────────────


def _make_at_stub(status: str = "Success") -> types.ModuleType:
    """Build a minimal africastalking module stub that returns *status*."""
    stub = types.ModuleType("africastalking")

    class _SMS:
        @staticmethod
        def send(message, recipients, sender_id):
            return {
                "SMSMessageData": {
                    "Recipients": [{"status": status, "number": recipients[0]}]
                }
            }

    stub.SMS = _SMS
    stub.initialize = lambda username, api_key: None
    return stub


def _make_at_stub_empty_recipients() -> types.ModuleType:
    stub = types.ModuleType("africastalking")
    stub.SMS = type(
        "_SMS",
        (),
        {"send": staticmethod(lambda msg, recip, sid: {"SMSMessageData": {"Recipients": []}})},
    )
    stub.initialize = lambda username, api_key: None
    return stub


def _make_at_stub_raises() -> types.ModuleType:
    stub = types.ModuleType("africastalking")

    class _SMS:
        @staticmethod
        def send(message, recipients, sender_id):
            raise RuntimeError("AT network error")

    stub.SMS = _SMS
    stub.initialize = lambda username, api_key: None
    return stub


# ── Degraded mode (no credentials) ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_returns_false_without_api_key(monkeypatch):
    monkeypatch.delenv("AT_API_KEY", raising=False)
    action = SMSAction()
    ok = await action.send("Alert", "+2340000000000")
    assert ok is False


@pytest.mark.asyncio
async def test_listen_returns_none_without_api_key(monkeypatch):
    monkeypatch.delenv("AT_API_KEY", raising=False)
    action = SMSAction()
    result = await action.listen_for_response("+2340000000000", timeout_seconds=10)
    assert result is None


# ── Successful send ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_returns_true_on_success(monkeypatch):
    monkeypatch.setenv("AT_API_KEY", "test-key")
    monkeypatch.setitem(sys.modules, "africastalking", _make_at_stub("Success"))
    action = SMSAction()
    ok = await action.send("Alert: overcurrent.", "+2341234567890")
    assert ok is True


@pytest.mark.asyncio
async def test_send_passes_message_and_number_to_sdk(monkeypatch):
    """initialize() is called exactly once at construction, not on every send."""
    monkeypatch.setenv("AT_API_KEY", "test-key")
    send_calls: list[tuple] = []
    init_calls: list[tuple] = []

    stub = types.ModuleType("africastalking")

    class _SMS:
        @staticmethod
        def send(message, recipients, sender_id):
            send_calls.append((message, recipients, sender_id))
            return {"SMSMessageData": {"Recipients": [{"status": "Success"}]}}

    stub.SMS = _SMS
    stub.initialize = lambda u, k: init_calls.append((u, k))
    monkeypatch.setitem(sys.modules, "africastalking", stub)

    action = SMSAction()
    # initialize() must have fired exactly once at construction time
    assert len(init_calls) == 1, "initialize() must be called once in __init__"

    await action.send("hello from ori", "+2341111111111")
    await action.send("second alert", "+2341111111111")

    # Still exactly one init call after two sends
    assert len(init_calls) == 1, "initialize() must not be called again on send()"

    assert len(send_calls) == 2
    message, recipients, sender_id = send_calls[0]
    assert message == "hello from ori"
    assert "+2341111111111" in recipients
    assert sender_id == "ORI"


@pytest.mark.asyncio
async def test_send_uses_at_sender_id_env_var(monkeypatch):
    monkeypatch.setenv("AT_API_KEY", "key")
    monkeypatch.setenv("AT_SENDER_ID", "MYAPP")
    calls: list[str] = []

    stub = types.ModuleType("africastalking")

    class _SMS:
        @staticmethod
        def send(message, recipients, sender_id):
            calls.append(sender_id)
            return {"SMSMessageData": {"Recipients": [{"status": "Success"}]}}

    stub.SMS = _SMS
    stub.initialize = lambda u, k: None
    monkeypatch.setitem(sys.modules, "africastalking", stub)

    action = SMSAction()
    await action.send("test", "+234000")
    assert calls[0] == "MYAPP"


# ── Failed send paths ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_returns_false_on_non_success_status(monkeypatch):
    monkeypatch.setenv("AT_API_KEY", "test-key")
    monkeypatch.setitem(sys.modules, "africastalking", _make_at_stub("InvalidPhoneNumber"))
    action = SMSAction()
    ok = await action.send("Alert", "+000")
    assert ok is False


@pytest.mark.asyncio
async def test_send_returns_false_on_empty_recipients(monkeypatch):
    monkeypatch.setenv("AT_API_KEY", "test-key")
    monkeypatch.setitem(sys.modules, "africastalking", _make_at_stub_empty_recipients())
    action = SMSAction()
    ok = await action.send("Alert", "+000")
    assert ok is False


@pytest.mark.asyncio
async def test_send_returns_false_on_sdk_exception(monkeypatch):
    monkeypatch.setenv("AT_API_KEY", "test-key")
    monkeypatch.setitem(sys.modules, "africastalking", _make_at_stub_raises())
    action = SMSAction()
    ok = await action.send("Alert", "+234000")
    assert ok is False


@pytest.mark.asyncio
async def test_send_never_raises(monkeypatch):
    """send() must never propagate any exception — always returns bool."""
    monkeypatch.setenv("AT_API_KEY", "test-key")
    monkeypatch.setitem(sys.modules, "africastalking", _make_at_stub_raises())
    action = SMSAction()
    result = await action.send("Alert", "+234000")
    assert isinstance(result, bool)


# ── listen_for_response stub ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_listen_for_response_returns_none(monkeypatch):
    monkeypatch.setenv("AT_API_KEY", "key")
    monkeypatch.setitem(sys.modules, "africastalking", _make_at_stub())
    action = SMSAction()
    result = await action.listen_for_response("+2340000000000", timeout_seconds=30)
    assert result is None


@pytest.mark.asyncio
async def test_listen_for_response_does_not_block(monkeypatch):
    """Stub must return immediately — no sleeping or polling."""
    import time
    monkeypatch.setenv("AT_API_KEY", "key")
    monkeypatch.setitem(sys.modules, "africastalking", _make_at_stub())
    action = SMSAction()
    start = time.monotonic()
    await action.listen_for_response("+234000", timeout_seconds=300)
    elapsed = time.monotonic() - start
    assert elapsed < 0.5, f"listen_for_response blocked for {elapsed:.2f}s"
