"""Tests for SMS dispatch notifications (broker.sms + phone validation).

Pure-logic only — no Postgres or Twilio account required. The send path is
exercised against an unconfigured environment so it stays a logged no-op.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dispatch.broker.sms import (  # noqa: E402
    dispatch_notification_body,
    is_configured,
    send_sms,
)
from dispatch.shared.schema import PhoneUpdateRequest  # noqa: E402

TWILIO_VARS = (
    "TWILIO_ACCOUNT_SID",
    "TWILIO_API_KEY_SID",
    "TWILIO_API_KEY_SECRET",
    "TWILIO_FROM_NUMBER",
)


def _clear_twilio(monkeypatch):
    for var in TWILIO_VARS:
        monkeypatch.delenv(var, raising=False)


# ---------------- message body ----------------

def test_body_live_vs_queued():
    live = dispatch_notification_body("alice@example.com", "Fix the build", queued=False)
    queued = dispatch_notification_body("alice@example.com", "Fix the build", queued=True)
    assert "New dispatch from alice@example.com" in live
    assert "Fix the build" in live
    assert "queued" in queued.lower()


def test_body_uses_first_line_only():
    body = dispatch_notification_body("bob@x.com", "Deploy now\nstep two\nstep three", queued=False)
    assert "Deploy now" in body
    assert "step two" not in body


def test_body_truncates_long_task():
    body = dispatch_notification_body("bob@x.com", "x" * 300, queued=False)
    assert body.endswith("...")
    # 100-char cap on the task portion (97 + ellipsis).
    assert "x" * 98 not in body


def test_body_handles_empty_task():
    body = dispatch_notification_body("bob@x.com", "   ", queued=False)
    assert "(no task)" in body


# ---------------- phone validation ----------------

@pytest.mark.parametrize("raw,expected", [
    ("+14155550123", "+14155550123"),
    ("+1 415 555 0123", "+14155550123"),
    ("+1-415-555-0123", "+14155550123"),
    ("", None),
    ("   ", None),
    (None, None),
])
def test_phone_accepts_and_normalizes(raw, expected):
    assert PhoneUpdateRequest(phone=raw).phone == expected


@pytest.mark.parametrize("raw", [
    "4155550123",      # no country-code +
    "+1",              # too short
    "+1415555012345678",  # too long
    "+1415555O123",    # letter, not digit
])
def test_phone_rejects_non_e164(raw):
    with pytest.raises(ValueError):
        PhoneUpdateRequest(phone=raw)


# ---------------- send path (dev-mode no-op) ----------------

def test_unconfigured_is_no_op(monkeypatch):
    _clear_twilio(monkeypatch)
    assert is_configured() is False
    result = asyncio.run(send_sms("+14155550123", "hi"))
    assert result.delivered is False
    assert result.sid is None
