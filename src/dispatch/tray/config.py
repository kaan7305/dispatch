"""Persistent config for the tray app, shared with the daemon CLI.

Stored at ~/.dispatch/config.json so a single install command + a launched
tray app see the same broker URL, daemon JWT, device id, and Anthropic key.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

CONFIG_PATH = Path.home() / ".dispatch" / "config.json"


@dataclass
class Config:
    broker: str = ""
    token: str = ""
    device_id: str = ""
    anthropic_api_key: str = ""
    local_port: int = 8001

    @classmethod
    def load(cls) -> "Config":
        # Explicit utf-8-sig: without an encoding, Windows decodes with the
        # ANSI codepage, so a config carrying a non-ASCII broker label — or one
        # a user opened and re-saved in Notepad, which adds a BOM — either
        # mojibakes or raises, and the tray starts up believing it is signed
        # out.
        if CONFIG_PATH.exists():
            try:
                data = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
                valid = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
                return cls(**valid)
            except Exception:
                pass
        return cls()

    def save(self) -> None:
        from dispatch.shared import fsperm

        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        fsperm.harden_dir(CONFIG_PATH.parent)
        existing: dict = {}
        if CONFIG_PATH.exists():
            try:
                existing = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
            except Exception:
                existing = {}
        existing.update({k: v for k, v in asdict(self).items() if v != ""})
        CONFIG_PATH.write_text(json.dumps(existing, indent=2), encoding="utf-8")
        # Holds the broker JWT and the Anthropic key. A chmod would be a no-op
        # on Windows, so this sets a real owner-only DACL there.
        fsperm.harden_file(CONFIG_PATH)

    def is_complete(self) -> bool:
        if self.broker and self.token:
            return True
        # Multi-home: the flat keys mirror only the PRIMARY broker; any other
        # signed-in broker entry still makes the daemon worth running.
        try:
            from dispatch.shared.config import broker_entries
            return any(e.get("token") for e in broker_entries())
        except Exception:
            return False
