"""Email sending for magic links.

If RESEND_API_KEY is set, real emails are sent via Resend's HTTP API.
Otherwise the link is printed to the broker logs and returned in the
response (development convenience).

To wire in a different provider, replace _send_via_resend() and check
for that provider's API key in send_magic_link().
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

import httpx

logger = logging.getLogger("dispatch.broker.email")


@dataclass
class SendResult:
    delivered: bool        # True if a real email was sent
    dev_link: Optional[str]  # When delivered=False, the link to display to the user


async def send_magic_link(to_email: str, link: str) -> SendResult:
    api_key = os.environ.get("RESEND_API_KEY")
    if api_key:
        try:
            await _send_via_resend(to_email, link, api_key)
            logger.info("magic link emailed to %s", to_email)
            return SendResult(delivered=True, dev_link=None)
        except Exception:
            logger.exception("Resend send failed; falling back to dev mode")
            # Fall through to dev mode rather than failing the user.

    logger.warning(
        "RESEND_API_KEY not set or send failed; magic link printed below.\n"
        "    To:   %s\n"
        "    Link: %s",
        to_email, link,
    )
    return SendResult(delivered=False, dev_link=link)


async def _send_via_resend(to_email: str, link: str, api_key: str) -> None:
    from_addr = os.environ.get("RESEND_FROM", "onboarding@resend.dev")
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "from": from_addr,
                "to": to_email,
                "subject": "Your Dispatch sign-in link",
                "html": _html_body(link),
                "text": _text_body(link),
            },
        )
        if r.status_code >= 400:
            raise RuntimeError(f"Resend API {r.status_code}: {r.text}")


def _html_body(link: str) -> str:
    return (
        '<div style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;font-size:15px;color:#222;line-height:1.5">'
        '<p>Click this link to sign in to Dispatch:</p>'
        f'<p><a href="{link}" style="display:inline-block;background:#7aa2ff;color:#0b0e14;padding:10px 18px;border-radius:6px;text-decoration:none;font-weight:600">Sign in</a></p>'
        '<p style="color:#888;font-size:13px">Or paste this URL into your browser:</p>'
        f'<p style="color:#888;font-size:13px;word-break:break-all">{link}</p>'
        '<p style="color:#888;font-size:13px">This link expires in 15 minutes and can only be used once. If you didn\'t request it, ignore this email.</p>'
        '</div>'
    )


def _text_body(link: str) -> str:
    return (
        f"Click this link to sign in to Dispatch:\n\n{link}\n\n"
        "This link expires in 15 minutes and can only be used once. "
        "If you didn't request it, ignore this email."
    )
