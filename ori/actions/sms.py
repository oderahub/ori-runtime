# Copyright 2026 Ori Nexus Systems LTD
# SPDX-License-Identifier: Apache-2.0

"""Africa's Talking SMS action — PRIMARY alert channel for Nigeria.

Works across all major Nigerian networks: MTN, Airtel, Glo, 9mobile.

SMS is the primary alert channel because:
- WhatsApp requires internet; SMS works on 2G/EDGE everywhere in Nigeria.
- Africa's Talking has direct carrier integrations, reducing delivery
  latency and failure rates compared to international aggregators.

Approval workflow note
----------------------
SMS does not support an inline response listener.  Africa's Talking
delivers incoming SMS via a webhook (HTTP POST to a configured URL on
your server).  For the PoC, :meth:`SMSAction.listen_for_response` is
stubbed and returns ``None``.  Full Tier C approval over SMS requires:

1. Expose an HTTPS endpoint that Africa's Talking can POST to.
2. Store incoming messages in the StateStore keyed by sender number.
3. Replace the stub with a polling loop that reads from StateStore.

For the PoC and most Tier A/B deployments, use WhatsApp for approval
workflows and SMS for fire-and-forget alerts.

Usage
-----
    action = SMSAction()                 # reads env vars at construction time
    ok = await action.send("Alert: overcurrent detected.", "+234XXXXXXXXXX")
"""

import asyncio
import logging
import os

logger = logging.getLogger(__name__)


class SMSAction:
    """Africa's Talking SMS sender — fire-and-forget alert delivery.

    Credentials are read from environment variables at construction time:

    - ``AT_API_KEY``    — Africa's Talking API key (required)
    - ``AT_USERNAME``   — Africa's Talking username (default: ``"sandbox"``)
    - ``AT_SENDER_ID``  — Alphanumeric sender ID shown to the recipient
                          (default: ``"ORI"``).  **Must be pre-registered with
                          Africa's Talking before production use** — unregistered
                          sender IDs are silently replaced by a short code.

    If ``AT_API_KEY`` is not set the action enters *degraded mode*: calls log a
    warning and return ``False`` without raising.
    """

    def __init__(self) -> None:
        self._api_key = os.environ.get("AT_API_KEY", "")
        self._username = os.environ.get("AT_USERNAME", "sandbox")
        self._sender_id = os.environ.get("AT_SENDER_ID", "ORI")
        self._ready = bool(self._api_key)
        if not self._ready:
            logger.warning("SMSAction: AT_API_KEY is not set — SMS delivery disabled.")
        if self._ready:
            try:
                import africastalking  # type: ignore[import-untyped]

                africastalking.initialize(self._username, self._api_key)
            except Exception:
                logger.exception("SMSAction: failed to initialise AT SDK")
                self._ready = False

    async def send(self, message: str, to_number: str) -> bool:
        """Send *message* to *to_number* via Africa's Talking.

        Args:
            message: Plain-text message body (max 160 chars per SMS segment).
            to_number: Recipient phone number in international format
                (e.g. ``"+234XXXXXXXXXX"``).

        Returns:
            ``True`` if Africa's Talking accepted the message, ``False``
            on any failure.  Never raises.
        """
        if not self._ready:
            logger.warning(
                "SMSAction.send: skipped (AT_API_KEY not configured). to=%r",
                to_number,
            )
            return False

        try:
            import africastalking  # type: ignore[import-untyped]

            # Africa's Talking SDK is synchronous — push to executor so the
            # event loop is not blocked during the HTTP round-trip.
            loop = asyncio.get_running_loop()

            def _send_sync() -> dict:
                sms = africastalking.SMS
                return sms.send(message, [to_number], self._sender_id)

            response = await loop.run_in_executor(None, _send_sync)

            # AT returns a dict with a "SMSMessageData" key containing
            # a "Recipients" list.  Each recipient has a "status" field.
            recipients = response.get("SMSMessageData", {}).get("Recipients") or []
            if recipients:
                status = recipients[0].get("status", "")
                if status == "Success":
                    logger.info(
                        "SMSAction.send: message delivered to %r (status=%r)",
                        to_number,
                        status,
                    )
                    return True
                logger.warning(
                    "SMSAction.send: delivery not confirmed for %r (status=%r)",
                    to_number,
                    status,
                )
                return False

            logger.warning(
                "SMSAction.send: empty recipients list in AT response for %r",
                to_number,
            )
            return False

        except Exception:
            logger.exception(
                "SMSAction.send: unexpected error sending to %r", to_number
            )
            return False

    async def listen_for_response(
        self,
        from_number: str,
        timeout_seconds: int,  # noqa: ARG002
    ) -> str | None:
        """Stub — incoming SMS via Africa's Talking requires a webhook.

        Africa's Talking delivers inbound SMS by POSTing to a configured
        HTTPS endpoint; there is no polling API.  Full Tier C approval
        workflow over SMS therefore requires:

        1. A publicly reachable HTTPS endpoint (e.g. on ori-gateway).
        2. The endpoint stores the incoming message body in StateStore,
           keyed by the sender's phone number.
        3. This method is replaced with a loop that polls StateStore
           until a message from *from_number* arrives or the timeout
           elapses.

        For the PoC, approval workflows should use
        :class:`~ori.actions.whatsapp.WhatsAppAction` as the response
        channel.  SMS remains the preferred *send* channel for Tier A
        alerts.

        Returns:
            Always ``None`` until the webhook integration is implemented.
        """
        logger.info(
            "SMSAction.listen_for_response: incoming SMS requires an "
            "Africa's Talking webhook endpoint — response listening is not "
            "yet implemented.  Configure an AT incoming SMS webhook and "
            "wire it to StateStore to enable SMS-based approval responses. "
            "(waiting for reply from %r, timeout=%ds — returning None)",
            from_number,
            timeout_seconds,
        )
        return None
