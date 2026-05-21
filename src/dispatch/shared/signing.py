"""Canonical dispatch payload for Ed25519 signing.

The sender's daemon signs these exact bytes; the recipient's daemon
rebuilds the identical bytes and verifies. Determinism is everything:
sorted keys, compact separators, UTF-8.
"""
from __future__ import annotations

import json
from typing import Optional


def canonical_dispatch_bytes(
    *,
    instruction: str,
    sender_device: str,
    recipient_user: str,
    target_device: Optional[str],
    nonce: str,
    created_at: str,
) -> bytes:
    """The deterministic byte string a dispatch signature covers (§7)."""
    obj = {
        "instruction": instruction,
        "sender_device": sender_device,
        "recipient_user": recipient_user,
        "target_device": target_device,
        "nonce": nonce,
        "created_at": created_at,
    }
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
