"""Transactional SMS for Dispatch (recipient notifications).

When a recipient receives a dispatch, the broker texts them so they know to
open Dispatch and review it — even if their daemon is offline.

If the TWILIO_* env vars are set, real texts go out via Twilio's REST API.
Otherwise the message is logged and the call is a no-op, so the dispatch
flow still works in development without any Twilio account.

Mirrors broker/email.py: one async _send() entry point, a provider helper,
and a dev-mode fallback. To wire in a different provider, replace
_send_via_twilio().
"""
from __future__ import annotations

import base64
import logging
import os
from dataclasses import dataclass
from typing import Optional

import httpx

logger = logging.getLogger("dispatch.broker.sms")


@dataclass
class SmsResult:
    delivered: bool          # True if a real text was sent
    sid: Optional[str]       # Twilio message SID when delivered, else None


def _twilio_config() -> Optional[dict]:
    """Read Twilio creds from the environment. Returns None if any are
    missing — the caller treats that as dev mode (log, don't send)."""
    cfg = {
        "account_sid": os.environ.get("TWILIO_ACCOUNT_SID"),
        "key_sid": os.environ.get("TWILIO_API_KEY_SID"),
        "key_secret": os.environ.get("TWILIO_API_KEY_SECRET"),
        "from_number": os.environ.get("TWILIO_FROM_NUMBER"),
    }
    if not all(cfg.values()):
        return None
    return cfg


def is_configured() -> bool:
    """Whether real SMS can be sent. Lets callers skip work / surface status."""
    return _twilio_config() is not None


async def send_sms(to_number: str, body: str) -> SmsResult:
    """Send one SMS. Never raises — logs and returns delivered=False on any
    failure so callers can fire-and-forget without guarding every call site."""
    cfg = _twilio_config()
    if cfg is None:
        logger.warning(
            "TWILIO_* not set; SMS not sent (dev mode).\n    To:   %s\n    Body: %s",
            to_number, body,
        )
        return SmsResult(delivered=False, sid=None)

    try:
        sid = await _send_via_twilio(to_number, body, cfg)
        logger.info("texted %s (sid=%s)", to_number, sid)
        return SmsResult(delivered=True, sid=sid)
    except Exception:
        logger.exception("Twilio send failed for %s", to_number)
        return SmsResult(delivered=False, sid=None)


async def _send_via_twilio(to_number: str, body: str, cfg: dict) -> Optional[str]:
    """POST to Twilio's Messages endpoint using API-key basic auth.

    Auth is the API key SID/secret pair (SK…/secret), scoped under the
    account SID in the URL — the same scheme the beeper relay uses.
    """
    url = (
        "https://api.twilio.com/2010-04-01/Accounts/"
        f"{cfg['account_sid']}/Messages.json"
    )
    auth = base64.b64encode(
        f"{cfg['key_sid']}:{cfg['key_secret']}".encode()
    ).decode()
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(
            url,
            headers={"Authorization": f"Basic {auth}"},
            data={"From": cfg["from_number"], "To": to_number, "Body": body},
        )
        if r.status_code >= 400:
            raise RuntimeError(f"Twilio API {r.status_code}: {r.text}")
        return r.json().get("sid")


def dispatch_notification_body(sender_id: str, task: str, queued: bool) -> str:
    """The SMS text for a freshly received dispatch.

    `queued` is True when the recipient's daemon was offline and the dispatch
    is waiting for it to reconnect, False when it was pushed live.
    """
    first_line = task.strip().splitlines()[0] if task.strip() else "(no task)"
    if len(first_line) > 100:
        first_line = first_line[:97] + "..."
    lead = "Dispatch queued from" if queued else "New dispatch from"
    return f"\U0001F4DF {lead} {sender_id}: {first_line}"
