"""Transactional SMS / WhatsApp for Dispatch (recipient notifications).

When a recipient receives a dispatch, the broker messages them so they know to
open Dispatch and review it — even if their daemon is offline.

If the TWILIO_* env vars are set, real messages go out via Twilio's REST API.
Otherwise the message is logged and the call is a no-op, so the dispatch
flow still works in development without any Twilio account.

Channel is selected with TWILIO_CHANNEL ("sms" default, or "whatsapp"). For
WhatsApp the From/To numbers are sent with a "whatsapp:" prefix; everything
else is identical. Auth accepts either an API-key pair
(TWILIO_API_KEY_SID/SECRET) or the account's Auth Token (TWILIO_AUTH_TOKEN),
whichever is configured.

Mirrors broker/email.py: one async _send() entry point, a provider helper,
and a dev-mode fallback. To wire in a different provider, replace
_send_via_twilio().
"""
from __future__ import annotations

import base64
import json
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
    """Read Twilio creds from the environment. Returns None if any required
    value is missing — the caller treats that as dev mode (log, don't send).

    Auth prefers an API-key pair (TWILIO_API_KEY_SID/SECRET); if that isn't
    set it falls back to the account Auth Token (TWILIO_AUTH_TOKEN). The
    channel ("sms" or "whatsapp") comes from TWILIO_CHANNEL, default "sms".
    TWILIO_CONTENT_SID, when set, names an approved WhatsApp template to send
    instead of a freeform body (the only way past WhatsApp's 24h window).
    """
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
    from_number = os.environ.get("TWILIO_FROM_NUMBER")
    channel = (os.environ.get("TWILIO_CHANNEL") or "sms").strip().lower()
    content_sid = os.environ.get("TWILIO_CONTENT_SID") or None

    key_sid = os.environ.get("TWILIO_API_KEY_SID")
    key_secret = os.environ.get("TWILIO_API_KEY_SECRET")
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
    if key_sid and key_secret:
        username, password = key_sid, key_secret
    elif account_sid and auth_token:
        username, password = account_sid, auth_token
    else:
        return None

    if not all([account_sid, from_number, username, password]):
        return None
    return {
        "account_sid": account_sid,
        "username": username,
        "password": password,
        "from_number": from_number,
        "channel": channel,
        "content_sid": content_sid,
    }


def is_configured() -> bool:
    """Whether real SMS can be sent. Lets callers skip work / surface status."""
    return _twilio_config() is not None


async def send_sms(
    to_number: str,
    body: str,
    content_variables: Optional[dict] = None,
) -> SmsResult:
    """Send one message. Never raises — logs and returns delivered=False on any
    failure so callers can fire-and-forget without guarding every call site.

    `content_variables` fills an approved WhatsApp template's {{1}}, {{2}}…
    placeholders; it's used only when TWILIO_CONTENT_SID is configured on the
    WhatsApp channel. Otherwise `body` is sent as freeform text.
    """
    cfg = _twilio_config()
    if cfg is None:
        logger.warning(
            "TWILIO_* not set; message not sent (dev mode).\n    To:   %s\n    Body: %s",
            to_number, body,
        )
        return SmsResult(delivered=False, sid=None)

    try:
        sid = await _send_via_twilio(to_number, body, cfg, content_variables)
        logger.info("messaged %s (sid=%s)", to_number, sid)
        return SmsResult(delivered=True, sid=sid)
    except Exception:
        logger.exception("Twilio send failed for %s", to_number)
        return SmsResult(delivered=False, sid=None)


async def _send_via_twilio(
    to_number: str,
    body: str,
    cfg: dict,
    content_variables: Optional[dict] = None,
) -> Optional[str]:
    """POST to Twilio's Messages endpoint using basic auth.

    Auth is the API-key pair (SK…/secret) or the account Auth Token, scoped
    under the account SID in the URL. For the WhatsApp channel both From and
    To carry a "whatsapp:" prefix; for SMS they're sent as-is.

    When a Content template SID is configured (WhatsApp only), the message is
    sent as that approved template with ContentVariables, which is exempt from
    WhatsApp's 24-hour freeform window. Otherwise a freeform Body is sent.
    """
    url = (
        "https://api.twilio.com/2010-04-01/Accounts/"
        f"{cfg['account_sid']}/Messages.json"
    )
    auth = base64.b64encode(
        f"{cfg['username']}:{cfg['password']}".encode()
    ).decode()
    prefix = "whatsapp:" if cfg["channel"] == "whatsapp" else ""
    data = {
        "From": f"{prefix}{cfg['from_number']}",
        "To": f"{prefix}{to_number}",
    }
    if cfg["channel"] == "whatsapp" and cfg.get("content_sid"):
        data["ContentSid"] = cfg["content_sid"]
        if content_variables:
            data["ContentVariables"] = json.dumps(content_variables)
    else:
        data["Body"] = body
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(
            url,
            headers={"Authorization": f"Basic {auth}"},
            data=data,
        )
        if r.status_code >= 400:
            raise RuntimeError(f"Twilio API {r.status_code}: {r.text}")
        return r.json().get("sid")


def _task_first_line(task: str) -> str:
    """The task's first line, trimmed to 100 chars — shared by the freeform
    body and the template variables so both stay consistent."""
    first_line = task.strip().splitlines()[0] if task.strip() else "(no task)"
    if len(first_line) > 100:
        first_line = first_line[:97] + "..."
    return first_line


def dispatch_notification_body(sender_id: str, task: str, queued: bool) -> str:
    """The freeform message text for a freshly received dispatch.

    `queued` is True when the recipient's daemon was offline and the dispatch
    is waiting for it to reconnect, False when it was pushed live.
    """
    lead = "Dispatch queued from" if queued else "New dispatch from"
    return f"\U0001F4DF {lead} {sender_id}: {_task_first_line(task)}"


def dispatch_notification_variables(sender_id: str, task: str) -> dict:
    """ContentVariables for an approved WhatsApp template: {{1}} is the sender,
    {{2}} is the task's first line. Pair with a template whose body reads e.g.
    "\U0001F4DF New dispatch from {{1}}: {{2}}". (Templates can't vary their
    wording, so the queued/live distinction isn't reflected in template mode.)
    """
    return {"1": sender_id, "2": _task_first_line(task)}
