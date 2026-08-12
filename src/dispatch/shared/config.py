"""Multi-broker client config (~/.dispatch/config.json).

One machine identity (the per-machine Ed25519 keypair) can be enrolled with
N brokers. Each broker gets its own entry in `brokers`:

    {"brokers": [{"url": ..., "token": ..., "label": ..., "device_id": ...}]}

Brokers never learn about each other — this list exists only client-side.

Back-compat contract with pre-multi-broker code paths (old daemon builds, the
tray sign-in flow, install scripts) that still read/write the flat `broker` /
`token` / `device_id` keys:

  * On load, a legacy `broker` key is folded into the list: if no entry has
    that URL one is synthesized (carrying the legacy token + device_id); if an
    entry already has it, the legacy `token` wins for that URL — legacy keys
    are only ever written by old code, so when they diverge they are the
    fresher value.
  * On save, the FIRST entry is mirrored back onto the legacy keys, so old
    code keeps working against the primary broker.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


def dispatch_home() -> Path:
    """Directory holding the client config. Override with DISPATCH_HOME.
    (Same resolution as daemon.identity.dispatch_home — kept import-light so
    the CLI never pulls in the keychain backend just to read config.)"""
    return Path(os.environ.get("DISPATCH_HOME", str(Path.home() / ".dispatch")))


def config_path() -> Path:
    return dispatch_home() / "config.json"


def load_config() -> dict:
    # utf-8-sig rather than the locale default: on Windows the implicit
    # encoding is the ANSI codepage, which mangles a non-ASCII broker label and
    # chokes on the BOM Notepad writes — either way the caller sees "no
    # brokers configured" instead of an error it could act on.
    try:
        return json.loads(config_path().read_text(encoding="utf-8-sig"))
    except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeDecodeError):
        return {}


def save_config(config: dict) -> None:
    from dispatch.shared import fsperm

    path = config_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fsperm.harden_dir(path.parent)
        path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        fsperm.harden_file(path)  # bearer tokens live here
    except OSError:
        pass


def normalize_url(url: str) -> str:
    return (url or "").strip().rstrip("/")


def _normalize_entry(raw: Any) -> Optional[dict]:
    if not isinstance(raw, dict):
        return None
    url = normalize_url(str(raw.get("url") or ""))
    if not url:
        return None
    return {
        "url": url,
        "token": raw.get("token") or "",
        "label": raw.get("label") or "",
        "device_id": raw.get("device_id") or None,
    }


def broker_entries(config: dict | None = None) -> list[dict]:
    """The configured brokers as a normalized list of
    {url, token, label, device_id}, primary first.

    Transparent migration: a config with only the legacy flat keys yields a
    one-entry list. The result is in-memory only — nothing is written until a
    caller persists via upsert_broker/set_broker_token."""
    config = load_config() if config is None else config
    entries: list[dict] = []
    for raw in config.get("brokers") or []:
        e = _normalize_entry(raw)
        if e is not None and e["url"] not in {x["url"] for x in entries}:
            entries.append(e)

    legacy_url = normalize_url(str(config.get("broker") or ""))
    if legacy_url:
        match = next((e for e in entries if e["url"] == legacy_url), None)
        if match is None:
            entries.insert(0, {
                "url": legacy_url,
                "token": config.get("token") or "",
                "label": "",
                "device_id": config.get("device_id") or None,
            })
        else:
            # Legacy keys are only written by pre-multi-broker code; when they
            # diverge from the entry they are the fresher value for that URL.
            if "token" in config:
                match["token"] = config.get("token") or ""
            if config.get("device_id") and not match.get("device_id"):
                match["device_id"] = config["device_id"]
    return entries


def _mirror_legacy(config: dict, entries: list[dict]) -> None:
    """Write `entries` and mirror the first entry onto the flat legacy keys so
    pre-multi-broker readers keep working against the primary broker."""
    config["brokers"] = entries
    if not entries:
        config.pop("broker", None)
        config.pop("token", None)
        return
    primary = entries[0]
    config["broker"] = primary["url"]
    if primary.get("token"):
        config["token"] = primary["token"]
    else:
        config.pop("token", None)
    if primary.get("device_id"):
        config["device_id"] = primary["device_id"]


def upsert_broker(
    url: str,
    *,
    token: Optional[str] = None,
    label: Optional[str] = None,
    device_id: Optional[str] = None,
    config: dict | None = None,
    persist: bool = True,
) -> dict:
    """Add or update (matched by URL) one broker entry; fields left None are
    preserved. Mirrors the first entry to the legacy keys and saves. Returns
    the updated config dict."""
    config = load_config() if config is None else config
    entries = broker_entries(config)
    target = normalize_url(url)
    entry = next((e for e in entries if e["url"] == target), None)
    if entry is None:
        entry = {"url": target, "token": "", "label": "", "device_id": None}
        entries.append(entry)
    if token is not None:
        entry["token"] = token
    if label is not None:
        entry["label"] = label
    if device_id is not None:
        entry["device_id"] = device_id
    _mirror_legacy(config, entries)
    if persist:
        save_config(config)
    return config


def clear_broker_token(url: str, *, config: dict | None = None) -> dict:
    """Drop one broker's token (sign-out / auth rejection) without touching
    its entry, then re-mirror + save."""
    config = load_config() if config is None else config
    entries = broker_entries(config)
    target = normalize_url(url)
    for e in entries:
        if e["url"] == target:
            e["token"] = ""
    _mirror_legacy(config, entries)
    save_config(config)
    return config


@dataclass
class BrokerLink:
    """One configured broker as the daemon runs it: identity of the connection
    (url/token/label/device_id) plus live connection state. Events for a
    dispatch are always sent back on the WS of the link it arrived on."""
    url: str
    token: str
    label: str = ""
    device_id: Optional[str] = None
    user_id: str = ""
    ws: Any = None            # the live broker WebSocket while connected
    connected: bool = False

    @property
    def base(self) -> str:
        return self.url.rstrip("/")

    def public(self) -> dict:
        """Connection-state summary safe to hand to UIs (no token)."""
        return {
            "url": self.url,
            "label": self.label,
            "connected": self.connected,
            "user_id": self.user_id,
            "device_id": self.device_id,
        }
