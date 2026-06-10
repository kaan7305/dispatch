"""Tests for the remote-approval signing path (phone-as-approver).

These cover the pure crypto contract steps 1-2 rest on: the bytes are
deterministic, every field is bound into the signature, and a real Ed25519
sign/verify round-trip with the device keypair succeeds — while any tamper or
wrong key fails.
"""
from dispatch.shared import crypto
from dispatch.shared.signing import canonical_approval_bytes

BASE = dict(
    dispatch_id="d1",
    request_id="r1",
    tool_name="Bash",
    decision="allow",
    approver_device="phone-1",
    issued_at="2026-06-05T00:00:00+00:00",
)


def test_deterministic_and_sorted():
    # Same inputs → identical bytes, regardless of kwarg order.
    a = canonical_approval_bytes(**BASE)
    b = canonical_approval_bytes(
        issued_at=BASE["issued_at"],
        approver_device=BASE["approver_device"],
        decision=BASE["decision"],
        tool_name=BASE["tool_name"],
        request_id=BASE["request_id"],
        dispatch_id=BASE["dispatch_id"],
    )
    assert a == b
    # Compact + key-sorted, like the dispatch payload.
    assert a == (
        b'{"approver_device":"phone-1","decision":"allow","dispatch_id":"d1",'
        b'"issued_at":"2026-06-05T00:00:00+00:00","request_id":"r1","tool_name":"Bash"}'
    )


def test_every_field_changes_the_bytes():
    base = canonical_approval_bytes(**BASE)
    for field in BASE:
        changed = {**BASE, field: BASE[field] + "X"}
        assert canonical_approval_bytes(**changed) != base, field


def test_sign_verify_roundtrip():
    priv, pub = crypto.generate_keypair()
    msg = canonical_approval_bytes(**BASE)
    sig = crypto.sign(priv, msg)
    assert crypto.verify(pub, msg, sig)


def test_signature_bound_to_tool_call():
    # A signature over "allow Bash" must NOT verify "allow a different tool" —
    # this is the replay-onto-another-call protection.
    priv, pub = crypto.generate_keypair()
    sig = crypto.sign(priv, canonical_approval_bytes(**BASE))
    forged = canonical_approval_bytes(**{**BASE, "tool_name": "mcp__notion__notion-move-pages"})
    assert not crypto.verify(pub, forged, sig)
    flipped = canonical_approval_bytes(**{**BASE, "decision": "deny"})
    assert not crypto.verify(pub, flipped, sig)


def test_wrong_key_fails():
    priv, _ = crypto.generate_keypair()
    _, other_pub = crypto.generate_keypair()
    msg = canonical_approval_bytes(**BASE)
    sig = crypto.sign(priv, msg)
    assert not crypto.verify(other_pub, msg, sig)
