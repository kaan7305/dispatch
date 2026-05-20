"""Runtime-only state for the broker.

Things that can't (or shouldn't) survive a restart live here:
  - Live WebSocket connections to recipient daemons.
  - Live WebSocket connections from sender watchers (per dispatch).
  - Live WebSocket connections from recipient browsers (per user inbox).

Everything that should survive (dispatches, events, queued offline
dispatches, users, magic links) is in store.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from fastapi import WebSocket


@dataclass
class RuntimeState:
    # user_id → connected recipient daemon WS
    agents: dict[str, WebSocket] = field(default_factory=dict)
    # dispatch_id → list of sender WS watchers (one per browser tab)
    watchers: dict[UUID, list[WebSocket]] = field(default_factory=dict)
    # user_id → list of recipient inbox WSes (one per browser tab)
    recipient_watchers: dict[str, list[WebSocket]] = field(default_factory=dict)


STATE = RuntimeState()
