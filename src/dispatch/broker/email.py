"""Transactional email for Dispatch (magic links, invitations).

If RESEND_API_KEY is set, real emails go out via Resend's HTTP API.
Otherwise the link is logged and returned in the API response so the
flow still works in development.

To wire in a different provider, replace _send_via_resend().
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
    delivered: bool          # True if a real email was sent
    dev_link: Optional[str]  # When delivered=False, the link to show the user


async def _send(to_email: str, subject: str, html: str, text: str, link: str) -> SendResult:
    """Send via Resend if configured; otherwise dev mode (log + return link)."""
    api_key = os.environ.get("RESEND_API_KEY")
    if api_key:
        try:
            await _send_via_resend(to_email, subject, html, text, api_key)
            logger.info("emailed %r to %s", subject, to_email)
            return SendResult(delivered=True, dev_link=None)
        except Exception:
            logger.exception("Resend send failed; falling back to dev mode")

    logger.warning(
        "RESEND_API_KEY not set or send failed; link printed below.\n"
        "    To:   %s\n    Link: %s",
        to_email, link,
    )
    return SendResult(delivered=False, dev_link=link)


async def _send_via_resend(
    to_email: str, subject: str, html: str, text: str, api_key: str
) -> None:
    from_addr = os.environ.get("RESEND_FROM", "onboarding@resend.dev")
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"from": from_addr, "to": to_email,
                  "subject": subject, "html": html, "text": text},
        )
        if r.status_code >= 400:
            raise RuntimeError(f"Resend API {r.status_code}: {r.text}")


def _button_html(intro: str, link: str, label: str, footer: str) -> str:
    return (
        '<div style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,'
        'sans-serif;font-size:15px;color:#222;line-height:1.5">'
        f'<p>{intro}</p>'
        f'<p><a href="{link}" style="display:inline-block;background:#7aa2ff;'
        'color:#0b0e14;padding:10px 18px;border-radius:6px;text-decoration:none;'
        f'font-weight:600">{label}</a></p>'
        '<p style="color:#888;font-size:13px">Or paste this URL into your browser:</p>'
        f'<p style="color:#888;font-size:13px;word-break:break-all">{link}</p>'
        f'<p style="color:#888;font-size:13px">{footer}</p></div>'
    )


async def send_magic_link(to_email: str, link: str) -> SendResult:
    return await _send(
        to_email,
        "Your Dispatch sign-in link",
        _button_html("Click this link to sign in to Dispatch:", link, "Sign in",
                     "This link expires in 15 minutes and can only be used once. "
                     "If you didn't request it, ignore this email."),
        f"Sign in to Dispatch:\n\n{link}\n\n"
        "Expires in 15 minutes, single use. If you didn't request it, ignore this.",
        link,
    )


async def send_invitation(to_email: str, inviter: str, link: str) -> SendResult:
    return await _send(
        to_email,
        f"{inviter} invited you to Dispatch",
        _button_html(
            f"<strong>{inviter}</strong> wants to be able to send you Dispatches "
            "(agentic tasks that run on your machine, with your approval). "
            "Click below to review and accept or decline:",
            link, "Review invitation",
            "This invitation expires in 7 days. If you don't know the sender, "
            "ignore this email.",
        ),
        f"{inviter} invited you to Dispatch. Review it:\n\n{link}\n\n"
        "Expires in 7 days. If you don't know the sender, ignore this.",
        link,
    )
