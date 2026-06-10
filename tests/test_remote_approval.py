"""Runner-side proof for phone-as-approver (steps 1-2).

Drives the daemon's `_resolve_remote_approval` directly: a second device signs a
decision with its Ed25519 key, and the runner verifies that signature against
the enrolled public key and resolves the *same* pending-approval Future the local
127.0.0.1 endpoint would. No broker process needed — this isolates the security-
critical verify/resolve logic.
"""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from dispatch.daemon.main import DaemonState, _resolve_remote_approval
from dispatch.shared import crypto
from dispatch.shared.signing import canonical_approval_bytes

DISPATCH_ID = "11111111-1111-1111-1111-111111111111"
REQUEST_ID = "req-123"
APPROVER = "22222222-2222-2222-2222-222222222222"
TOOL = "Bash"

# One explicit loop for the whole module (Futures + run share it), avoiding the
# deprecated implicit get_event_loop().
LOOP = asyncio.new_event_loop()


def _local_state_with_pending(tool=TOOL):
    """A stand-in local_state exposing entries[uuid].pending_tools[req]."""
    entry = SimpleNamespace(pending_tools={REQUEST_ID: {"tool": tool, "input": {}}})
    return SimpleNamespace(
        entries={uuid.UUID(DISPATCH_ID): entry},
        broker_url="",   # so a key-miss refresh is a no-op, never a network call
        broker_token="",
    )


def _state_with_future(pubkey: bytes):
    state = DaemonState()
    fut = LOOP.create_future()
    state.pending_approvals[(DISPATCH_ID, REQUEST_ID)] = fut
    state.device_keys[APPROVER] = pubkey
    return state, fut


def _signed_msg(priv: bytes, *, decision="allow", tool=TOOL, issued_at=None,
                dispatch_id=DISPATCH_ID, request_id=REQUEST_ID, approver=APPROVER):
    issued_at = issued_at or datetime.now(timezone.utc).isoformat()
    canonical = canonical_approval_bytes(
        dispatch_id=dispatch_id, request_id=request_id, tool_name=tool,
        decision=decision, approver_device=approver, issued_at=issued_at,
    )
    return {
        "type": "approval_decision",
        "dispatch_id": dispatch_id,
        "request_id": request_id,
        "decision": decision,
        "approver_device": approver,
        "issued_at": issued_at,
        "signature": crypto.b64encode(crypto.sign(priv, canonical)),
    }


def _run(coro):
    return LOOP.run_until_complete(coro)


def test_valid_signature_resolves_future():
    priv, pub = crypto.generate_keypair()
    state, fut = _state_with_future(pub)
    ls = _local_state_with_pending()
    _run(_resolve_remote_approval(_signed_msg(priv), state, ls))
    assert fut.done() and fut.result() == "allow"


def test_always_decision_passes_through():
    priv, pub = crypto.generate_keypair()
    state, fut = _state_with_future(pub)
    ls = _local_state_with_pending()
    _run(_resolve_remote_approval(_signed_msg(priv, decision="always"), state, ls))
    assert fut.result() == "always"


def test_bad_signature_does_not_resolve():
    priv, pub = crypto.generate_keypair()
    state, fut = _state_with_future(pub)
    ls = _local_state_with_pending()
    msg = _signed_msg(priv)
    msg["signature"] = crypto.b64encode(b"\x00" * crypto.SIGNATURE_BYTES)
    _run(_resolve_remote_approval(msg, state, ls))
    assert not fut.done()


def test_wrong_key_does_not_resolve():
    priv, _ = crypto.generate_keypair()
    _, other_pub = crypto.generate_keypair()  # roster has a DIFFERENT key
    state, fut = _state_with_future(other_pub)
    ls = _local_state_with_pending()
    _run(_resolve_remote_approval(_signed_msg(priv), state, ls))
    assert not fut.done()


def test_tampered_tool_name_does_not_resolve():
    # Approver signed "Bash"; the live pending record says a different tool, so
    # the rebuilt canonical bytes differ and verification fails. This is the
    # replay-onto-another-call protection at the runner.
    priv, pub = crypto.generate_keypair()
    state, fut = _state_with_future(pub)
    ls = _local_state_with_pending(tool="mcp__notion__notion-move-pages")
    _run(_resolve_remote_approval(_signed_msg(priv, tool="Bash"), state, ls))
    assert not fut.done()


def test_stale_decision_rejected():
    priv, pub = crypto.generate_keypair()
    state, fut = _state_with_future(pub)
    ls = _local_state_with_pending()
    old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    _run(_resolve_remote_approval(_signed_msg(priv, issued_at=old), state, ls))
    assert not fut.done()


def test_unknown_device_rejected():
    priv, pub = crypto.generate_keypair()
    state, fut = _state_with_future(pub)
    state.device_keys.clear()           # approver not in roster
    ls = _local_state_with_pending()    # broker_url="" → refresh no-ops
    _run(_resolve_remote_approval(_signed_msg(priv), state, ls))
    assert not fut.done()


def test_no_pending_future_is_noop():
    # A device that ISN'T running this dispatch has no Future — must not crash.
    priv, pub = crypto.generate_keypair()
    state = DaemonState()
    state.device_keys[APPROVER] = pub
    ls = _local_state_with_pending()
    _run(_resolve_remote_approval(_signed_msg(priv), state, ls))  # no raise
