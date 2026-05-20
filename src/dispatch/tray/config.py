"""Persistent config for the Dispatch tray app.

Stored at ~/.dispatch/config.json so it survives app restarts.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

CONFIG_PATH = Path.home() / ".dispatch" / "config.json"


@dataclass
class Config:
    broker_url: str = ""
    username: str = ""
    token: str = ""
    anthropic_api_key: str = ""
    ui_port: int = 8001
    workspace: str = str(Path.home() / "dispatch-workspace")

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
        CONFIG_PATH.write_text(json.dumps(asdict(self), indent=2))

    def is_complete(self) -> bool:
        return bool(self.username and self.token and self.anthropic_api_key)
