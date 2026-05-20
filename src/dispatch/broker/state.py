"""In-memory broker state.

The abstractions are deliberately narrow so a future swap to a real
store (Postgres for dispatches, Redis for live connections) lands here
and nowhere else. Restart loses everything for now.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi import WebSocket

from dispatch.shared.schema import DispatchEvent, DispatchPayload, DispatchStatus


@dataclass
class DispatchRecord:
    payload: DispatchPayload
    status: DispatchStatus = DispatchStatus.pending
    events: list[DispatchEvent] = field(default_factory=list)
    watchers: list[WebSocket] = field(default_factory=list)


@dataclass
class FriendRequest:
    request_id: str = field(default_factory=lambda: str(uuid4()))
    from_user: str = ""
    to_user: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class BrokerState:
    # user_id → currently-connected recipient daemon WS
    agents: dict[str, WebSocket] = field(default_factory=dict)
    # user_id → list of dispatch_ids queued while daemon was offline
    pending_for_offline: dict[str, list[UUID]] = field(
        default_factory=lambda: defaultdict(list)
    )
    # dispatch_id → record
    dispatches: dict[UUID, DispatchRecord] = field(default_factory=dict)
    # known user_ids (auto-registered on first login)
    users: set[str] = field(default_factory=set)

    # request_id → FriendRequest (pending only)
    friend_requests: dict[str, FriendRequest] = field(default_factory=dict)
    # user_id → set of friend user_ids (bidirectional on accept)
    friends: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))


STATE = BrokerState()
