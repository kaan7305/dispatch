"""Runtime-only state for the broker.

Things that can't (or shouldn't) survive a restart live here:
  - Live WebSocket connections to recipient daemons, keyed by device.
  - Live WebSocket connections from sender watchers (per dispatch).
  - Live WebSocket connections from recipient browsers (per user inbox).

Everything that should survive (users, dispatches, events, devices,
trust links, invitations, magic links) is in store.py / Postgres.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from uuid import UUID

from fastapi import WebSocket


@dataclass
class RuntimeState:
    # user_id → { device_id → live daemon WebSocket }.
    # A user may have several devices online at once.
    agents: dict[str, dict[str, WebSocket]] = field(
        default_factory=lambda: defaultdict(dict)
    )
    # dispatch_id → list of sender WS watchers (one per browser tab)
    watchers: dict[UUID, list[WebSocket]] = field(default_factory=dict)
    # user_id → list of recipient inbox WSes (one per browser tab)
    recipient_watchers: dict[str, list[WebSocket]] = field(default_factory=dict)

    def pick_device(self, user_id: str) -> tuple[str, WebSocket] | None:
        """(device_id, WS) of the user's most-recently-connected device.

        Used for routing when no specific target_device is named (the §18
        decision-4 default). Dict insertion order tracks recency.
        """
        devices = self.agents.get(user_id)
        if not devices:
            return None
        device_id = next(reversed(list(devices)), None)
        if device_id is None:
            return None
        return device_id, devices[device_id]

    def pick_device_ws(self, user_id: str) -> WebSocket | None:
        picked = self.pick_device(user_id)
        return picked[1] if picked else None


STATE = RuntimeState()
