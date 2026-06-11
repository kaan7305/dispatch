"""Tests for the rich dispatch payload (context + attachments).

The contract under test:
  - A plain dispatch's canonical bytes are IDENTICAL to the pre-rich format
    (old signatures verify, plain dispatches interop across versions).
  - Context and the attachment manifest are bound into the signed bytes, so
    tampering either breaks the signature.
  - Attachment *bytes* bind transitively: the daemon re-hashes each decoded
    blob against its (signed) manifest sha256 and rejects a mismatch.
"""
import base64
import hashlib

from dispatch.shared.signing import (
    attachment_manifest,
    canonical_context,
    canonical_dispatch_bytes,
)
from dispatch.daemon.main import _verify_attachment_blobs

BASE = dict(
    instruction="do the thing",
    sender_device="dev-1",
    recipient_user="edward",
    target_device=None,
    nonce="n1",
    created_at="2026-06-10T00:00:00+00:00",
)


def _attachment(name: str, data: bytes) -> dict:
    return {
        "name": name,
        "content_b64": base64.b64encode(data).decode(),
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
    }


def test_plain_dispatch_bytes_unchanged():
    legacy_shape = (
        b'{"created_at":"2026-06-10T00:00:00+00:00","instruction":"do the thing",'
        b'"nonce":"n1","recipient_user":"edward","sender_device":"dev-1",'
        b'"target_device":null}'
    )
    assert canonical_dispatch_bytes(**BASE) == legacy_shape
    # Explicit None/empty rich fields are byte-identical to omitting them.
    assert canonical_dispatch_bytes(**BASE, context=None, attachments=None) == legacy_shape
    assert canonical_dispatch_bytes(**BASE, context={}, attachments=[]) == legacy_shape


def test_rich_fields_change_the_bytes():
    plain = canonical_dispatch_bytes(**BASE)
    with_ctx = canonical_dispatch_bytes(**BASE, context={"project": "yuni"})
    manifest = [{"name": "a.txt", "sha256": "e" * 64, "size": 1}]
    with_atts = canonical_dispatch_bytes(**BASE, attachments=manifest)
    assert len({plain, with_ctx, with_atts}) == 3
    # Any manifest field flips the bytes (name / hash / size all bound).
    for k, v in (("name", "b.txt"), ("sha256", "f" * 64), ("size", 2)):
        tampered = canonical_dispatch_bytes(**BASE, attachments=[{**manifest[0], k: v}])
        assert tampered != with_atts


def test_canonical_context_drops_empties_and_unknowns():
    md = {
        "context": {
            "project": "Yuni",
            "deliverable": "  ",
            "background": "",
            "links": ["", "https://a"],
            "rogue": "ignored",
        }
    }
    assert canonical_context(md) == {"project": "Yuni", "links": ["https://a"]}
    assert canonical_context({}) is None
    assert canonical_context({"context": {"project": " "}}) is None


def test_attachment_manifest_sorted_and_bytes_excluded():
    md = {"attachments": [_attachment("b.txt", b"xyz"), _attachment("a.txt", b"x")]}
    manifest = attachment_manifest(md)
    assert [e["name"] for e in manifest] == ["a.txt", "b.txt"]
    assert all("content_b64" not in e for e in manifest)
    assert attachment_manifest({}) is None


def test_blob_verification_accepts_good_rejects_tampered():
    good = {"attachments": [_attachment("a.txt", b"hello")]}
    ok, reason = _verify_attachment_blobs(good)
    assert ok, reason

    swapped = {"attachments": [{**good["attachments"][0],
                                "content_b64": base64.b64encode(b"evil!").decode()}]}
    ok, reason = _verify_attachment_blobs(swapped)
    assert not ok and "sha256 mismatch" in reason

    wrong_size = {"attachments": [{**good["attachments"][0], "size": 99}]}
    ok, reason = _verify_attachment_blobs(wrong_size)
    assert not ok and "size mismatch" in reason

    ok, _ = _verify_attachment_blobs({})
    assert ok
