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


def canonical_approval_bytes(
    *,
    dispatch_id: str,
    request_id: str,
    tool_name: str,
    decision: str,
    approver_device: str,
    issued_at: str,
) -> bytes:
    """The deterministic byte string a *remote* tool-approval signature covers.

    Phone-as-approver: a second device of the recipient signs these exact bytes
    to authorize one tool call running on the recipient's runner daemon. The
    broker only relays the signature; the runner rebuilds these bytes and
    verifies against the approver device's enrolled public key — so a compromised
    broker can drop or delay a decision but never forge one.

    Every field is bound into the signature on purpose:
      - ``request_id`` + ``tool_name`` pin the signature to ONE specific tool
        call, so a captured signature can't be replayed onto a different call.
      - ``approver_device`` names which device's key must verify it.
      - ``issued_at`` (ISO-8601 UTC) bounds the replay window.
    """
    obj = {
        "dispatch_id": dispatch_id,
        "request_id": request_id,
        "tool_name": tool_name,
        "decision": decision,
        "approver_device": approver_device,
        "issued_at": issued_at,
    }
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
