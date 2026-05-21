"""Ed25519 signing — the cryptographic identity of a device.

Each device holds a 32-byte private key (never leaves the machine) and
publishes its 32-byte public key to the broker. Dispatches are signed
with the private key and verified against the public key.
"""
from __future__ import annotations

import base64

from nacl.exceptions import BadSignatureError
from nacl.signing import SigningKey, VerifyKey

PRIVATE_KEY_BYTES = 32
PUBLIC_KEY_BYTES = 32
SIGNATURE_BYTES = 64


def generate_keypair() -> tuple[bytes, bytes]:
    """Return (private_key, public_key), 32 bytes each."""
    sk = SigningKey.generate()
    return bytes(sk), bytes(sk.verify_key)


def public_key_for(private_key: bytes) -> bytes:
    """Derive the public key from a private key."""
    return bytes(SigningKey(private_key).verify_key)


def sign(private_key: bytes, message: bytes) -> bytes:
    """Return a 64-byte detached Ed25519 signature over `message`."""
    return SigningKey(private_key).sign(message).signature


def verify(public_key: bytes, message: bytes, signature: bytes) -> bool:
    """True iff `signature` is a valid signature of `message` by `public_key`."""
    try:
        VerifyKey(public_key).verify(message, signature)
        return True
    except (BadSignatureError, ValueError):
        return False


def b64encode(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def b64decode(text: str) -> bytes:
    return base64.b64decode(text)
