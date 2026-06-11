"""Canonical dispatch payload for Ed25519 signing.

The sender's daemon signs these exact bytes; the recipient's daemon
rebuilds the identical bytes and verifies. Determinism is everything:
sorted keys, compact separators, UTF-8.
"""
from __future__ import annotations

import json
from typing import Any, Optional


def canonical_dispatch_bytes(
    *,
    instruction: str,
    sender_device: str,
    recipient_user: str,
    target_device: Optional[str],
    nonce: str,
    created_at: str,
    context: Optional[dict] = None,
    attachments: Optional[list[dict]] = None,
) -> bytes:
    """The deterministic byte string a dispatch signature covers (§7).

    `context` and `attachments` (the manifest: name/size/sha256 per file,
    NOT the bytes) are included in the signed object only when present —
    a plain dispatch's bytes are identical to the pre-rich-payload format,
    so old signatures stay verifiable and plain dispatches interop across
    versions. Derive both via canonical_context() / attachment_manifest()
    from the SAME metadata dict on every side, or the bytes won't match.
    """
    obj: dict[str, Any] = {
        "instruction": instruction,
        "sender_device": sender_device,
        "recipient_user": recipient_user,
        "target_device": target_device,
        "nonce": nonce,
        "created_at": created_at,
    }
    if context:
        obj["context"] = context
    if attachments:
        obj["attachments"] = attachments
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def canonical_context(metadata: Optional[dict]) -> Optional[dict]:
    """The signature-covered form of metadata['context']: known fields only,
    empties dropped, None when nothing remains. Both the broker (building the
    sign_request) and the recipient daemon (verifying) MUST derive it through
    here so the signed bytes agree."""
    raw = (metadata or {}).get("context")
    if not isinstance(raw, dict):
        return None
    out: dict[str, Any] = {}
    for key in ("project", "deliverable", "background"):
        v = raw.get(key)
        if isinstance(v, str) and v.strip():
            out[key] = v
    links = raw.get("links")
    if isinstance(links, list):
        kept = [l for l in links if isinstance(l, str) and l.strip()]
        if kept:
            out["links"] = kept
    return out or None


def attachment_manifest(metadata: Optional[dict]) -> Optional[list[dict]]:
    """The signature-covered manifest of metadata['attachments']: one
    {name, sha256, size} per file, sorted by name. The bytes themselves are
    NOT signed — the recipient daemon re-hashes each decoded blob against
    its manifest sha256, which transitively binds the content."""
    raw = (metadata or {}).get("attachments")
    if not isinstance(raw, list):
        return None
    entries = [
        {
            "name": a.get("name"),
            "sha256": a.get("sha256"),
            "size": a.get("size"),
        }
        for a in raw
        if isinstance(a, dict)
    ]
    if not entries:
        return None
    return sorted(entries, key=lambda e: str(e["name"]))


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
