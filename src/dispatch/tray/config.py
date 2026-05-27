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
        if CONFIG_PATH.exists():
            try:
                data = json.loads(CONFIG_PATH.read_text())
                valid = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
                return cls(**valid)
            except Exception:
                pass
        return cls()

    def save(self) -> None:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        existing: dict = {}
        if CONFIG_PATH.exists():
            try:
                existing = json.loads(CONFIG_PATH.read_text())
            except Exception:
                existing = {}
        existing.update({k: v for k, v in asdict(self).items() if v != ""})
        CONFIG_PATH.write_text(json.dumps(existing, indent=2))
        CONFIG_PATH.chmod(0o600)

    def is_complete(self) -> bool:
        return bool(self.broker and self.token)
