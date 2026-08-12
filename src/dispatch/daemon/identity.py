"""Device identity for the daemon.

Each machine that runs the daemon has one Ed25519 keypair. The private
key is stored in the OS keychain by default, or — when
DISPATCH_KEY_BACKEND=file — in a 0600 file under the dispatch home
directory (useful for headless servers, CI, and tests). The public key
is registered with the broker via POST /devices/enroll.
"""
from __future__ import annotations

import json
import os
import socket
from pathlib import Path

import httpx
import keyring

from dispatch.shared import crypto, fsperm

KEYRING_SERVICE = "dispatch-daemon"
KEYRING_ACCOUNT = "device-private-key"


def dispatch_home() -> Path:
    """Directory holding the daemon's config (and, for the file key
    backend, the private key). Override with DISPATCH_HOME."""
    return Path(os.environ.get("DISPATCH_HOME", str(Path.home() / ".dispatch")))


def _use_file_backend() -> bool:
    return os.environ.get("DISPATCH_KEY_BACKEND", "").lower() == "file"


def _key_file() -> Path:
    return dispatch_home() / "device_key"


def get_private_key() -> bytes | None:
    if _use_file_backend():
        try:
            return crypto.b64decode(_key_file().read_text(encoding="utf-8").strip())
        except (FileNotFoundError, OSError, ValueError, UnicodeDecodeError):
            return None
    stored = keyring.get_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
    return crypto.b64decode(stored) if stored else None


def set_private_key(private_key: bytes) -> None:
    encoded = crypto.b64encode(private_key)
    if _use_file_backend():
        # This is the device's Ed25519 signing key: the one secret on the
        # machine whose disclosure lets someone impersonate this device to the
        # broker. write_private_text hardens the directory *before* creating
        # the file, so on Windows the key is never briefly readable under the
        # inherited profile ACL while waiting for a follow-up chmod that would
        # not have restricted anything anyway.
        fsperm.write_private_text(_key_file(), encoded)
        return
    keyring.set_password(KEYRING_SERVICE, KEYRING_ACCOUNT, encoded)


def load_or_create_keypair() -> tuple[bytes, bytes]:
    """Return (private_key, public_key); create + persist on first run."""
    priv = get_private_key()
    if priv is None:
        priv, pub = crypto.generate_keypair()
        set_private_key(priv)
        return priv, pub
    return priv, crypto.public_key_for(priv)


def _pins_file() -> Path:
    return dispatch_home() / "pins.json"


def load_pins() -> dict:
    """device_id → base64 public key, pinned on first sight (TOFU)."""
    try:
        return json.loads(_pins_file().read_text(encoding="utf-8-sig"))
    except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeDecodeError):
        return {}


def save_pins(pins: dict) -> None:
    # Not a secret, but integrity matters: these pins are what stop a
    # compromised broker substituting a sender's key, so they get the same
    # owner-only protection as the key itself.
    try:
        fsperm.write_private_text(_pins_file(), json.dumps(pins, indent=2))
    except OSError:
        pass


async def ensure_enrolled(
    broker: str, token: str, existing_device_id: str | None
) -> str:
    """Guarantee this machine has a keypair and a broker-issued device_id.

    Returns the device_id. Enrolls with the broker only if we don't
    already have one saved. Enrollment is idempotent broker-side (keyed
    on the public key), so a lost device_id just re-resolves.
    """
    _priv, public_key = load_or_create_keypair()
    if existing_device_id:
        return existing_device_id
    label = socket.gethostname() or "unknown-device"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{broker.rstrip('/')}/devices/enroll",
            json={"label": label, "public_key": crypto.b64encode(public_key)},
            headers={"Authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()
        return resp.json()["device_id"]
