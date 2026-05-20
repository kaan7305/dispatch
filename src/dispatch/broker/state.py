"""Runtime-only state for the broker.

Things that can't (or shouldn't) survive a restart live here:
  - Live WebSocket connections to recipient daemons.
  - Live WebSocket connections from sender watchers (per dispatch).
  - Live WebSocket connections from recipient browsers (per user inbox).

Everything that should survive (dispatches, events, queued offline
dispatches, users, magic links) is in store.py.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi import WebSocket


@dataclass
class FriendRequest:
    request_id: str = field(default_factory=lambda: str(uuid4()))
    from_user: str = ""
    to_user: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class RuntimeState:
    # user_id → connected recipient daemon WS
    agents: dict[str, WebSocket] = field(default_factory=dict)
    # dispatch_id → list of sender WS watchers (one per browser tab)
    watchers: dict[UUID, list[WebSocket]] = field(default_factory=dict)
    # user_id → list of recipient inbox WSes (one per browser tab)
    recipient_watchers: dict[str, list[WebSocket]] = field(default_factory=dict)

    # request_id → FriendRequest (pending only)
    friend_requests: dict[str, FriendRequest] = field(default_factory=dict)
    # user_id → set of friend user_ids (bidirectional on accept)
    friends: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))


STATE = RuntimeState()
