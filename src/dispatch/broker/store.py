"""Persistent storage for the broker.

Runs in two modes:
  - Postgres (asyncpg) when DATABASE_URL is set — used in production/Railway.
  - In-memory               when DATABASE_URL is absent — used by the local tray app.

Routes call STORE.xxx(); nothing outside this file knows which backend is active.
"""
from __future__ import annotations

import json
import logging
import os
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from uuid import UUID

from dispatch.shared.schema import DispatchEvent, DispatchPayload, DispatchStatus

logger = logging.getLogger("dispatch.broker.store")

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


@dataclass
class StoredDispatch:
    payload: DispatchPayload
    status: DispatchStatus


# ---------------------------------------------------------------------------
# In-memory backend
# ---------------------------------------------------------------------------

@dataclass
class _MemoryBackend:
    users: set[str] = field(default_factory=set)
    dispatches: dict[UUID, StoredDispatch] = field(default_factory=dict)
    events: dict[UUID, list[DispatchEvent]] = field(default_factory=lambda: defaultdict(list))
    offline: dict[str, list[UUID]] = field(default_factory=lambda: defaultdict(list))
    magic_links: dict[str, dict] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class Store:
    def __init__(self, dsn: str | None = None) -> None:
        self.dsn = dsn or os.environ.get("DATABASE_URL")
        self.pool = None
        self._mem: _MemoryBackend | None = None

    @property
    def _local(self) -> bool:
        return self._mem is not None

    async def init(self) -> None:
        if not self.dsn:
            self._mem = _MemoryBackend()
            logger.info("Store running in-memory (no DATABASE_URL)")
            return

        import asyncpg
        self.pool = await asyncpg.create_pool(
            dsn=self.dsn,
            min_size=1,
            max_size=10,
            init=self._init_conn,
        )
        async with self.pool.acquire() as conn:
            await conn.execute(SCHEMA_PATH.read_text())
        logger.info("Store ready (Postgres)")

    async def _init_conn(self, conn) -> None:
        await conn.set_type_codec(
            "jsonb",
            encoder=json.dumps,
            decoder=json.loads,
            schema="pg_catalog",
        )

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.close()
            self.pool = None

    # ---------------- users ----------------

    async def upsert_user(self, user_id: str) -> None:
        if self._local:
            self._mem.users.add(user_id)
            return
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO users (user_id) VALUES ($1) ON CONFLICT (user_id) DO NOTHING",
                user_id,
            )

    async def list_users(self) -> list[str]:
        if self._local:
            return sorted(self._mem.users)
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT user_id FROM users ORDER BY user_id")
            return [r["user_id"] for r in rows]

    # ---------------- dispatches ----------------

    async def create_dispatch(self, payload: DispatchPayload, status: DispatchStatus) -> None:
        if self._local:
            self._mem.dispatches[payload.dispatch_id] = StoredDispatch(payload=payload, status=status)
            return
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO dispatches (
                    dispatch_id, sender_id, recipient_id, task,
                    metadata, created_at, expires_at, status
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                """,
                payload.dispatch_id, payload.sender_id, payload.recipient_id,
                payload.task, payload.metadata, payload.created_at,
                payload.expires_at, status.value,
            )

    async def get_dispatch(self, dispatch_id: UUID) -> Optional[StoredDispatch]:
        if self._local:
            return self._mem.dispatches.get(dispatch_id)
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM dispatches WHERE dispatch_id = $1", dispatch_id
            )
            return self._row_to_dispatch(row) if row else None

    async def update_status(self, dispatch_id: UUID, status: DispatchStatus) -> None:
        if self._local:
            rec = self._mem.dispatches.get(dispatch_id)
            if rec:
                rec.status = status
            return
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE dispatches SET status = $1 WHERE dispatch_id = $2",
                status.value, dispatch_id,
            )

    async def list_dispatches_for_user(self, user_id: str, role: str) -> list[StoredDispatch]:
        if role not in ("sent", "received"):
            raise ValueError(f"role must be 'sent' or 'received', got {role!r}")
        if self._local:
            attr = "sender_id" if role == "sent" else "recipient_id"
            return [
                d for d in self._mem.dispatches.values()
                if getattr(d.payload, attr) == user_id
            ]
        col = "sender_id" if role == "sent" else "recipient_id"
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT * FROM dispatches WHERE {col} = $1 ORDER BY created_at DESC LIMIT 200",
                user_id,
            )
            return [self._row_to_dispatch(r) for r in rows]

    def _row_to_dispatch(self, row) -> StoredDispatch:
        payload = DispatchPayload(
            dispatch_id=row["dispatch_id"],
            sender_id=row["sender_id"],
            recipient_id=row["recipient_id"],
            task=row["task"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            metadata=row["metadata"] or {},
        )
        return StoredDispatch(payload=payload, status=DispatchStatus(row["status"]))

    # ---------------- events ----------------

    async def append_event(self, dispatch_id: UUID, event: DispatchEvent) -> int:
        if self._local:
            lst = self._mem.events[dispatch_id]
            lst.append(event)
            return len(lst)
        async with self.pool.acquire() as conn:
            seq = await conn.fetchval(
                """
                INSERT INTO dispatch_events (dispatch_id, seq, type, data)
                VALUES (
                    $1,
                    COALESCE(
                        (SELECT MAX(seq) FROM dispatch_events WHERE dispatch_id = $1), 0
                    ) + 1,
                    $2, $3
                )
                RETURNING seq
                """,
                dispatch_id, event["type"], event["data"],
            )
            return seq

    async def get_events(self, dispatch_id: UUID) -> list[DispatchEvent]:
        if self._local:
            return list(self._mem.events.get(dispatch_id, []))
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT type, data FROM dispatch_events WHERE dispatch_id = $1 ORDER BY seq",
                dispatch_id,
            )
            return [{"type": r["type"], "data": r["data"]} for r in rows]

    # ---------------- offline queue ----------------

    async def enqueue_for_offline(self, user_id: str, dispatch_id: UUID) -> None:
        if self._local:
            if dispatch_id not in self._mem.offline[user_id]:
                self._mem.offline[user_id].append(dispatch_id)
            return
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO pending_for_offline (user_id, dispatch_id)
                VALUES ($1, $2) ON CONFLICT (user_id, dispatch_id) DO NOTHING
                """,
                user_id, dispatch_id,
            )

    async def pop_offline_queue(self, user_id: str) -> list[UUID]:
        if self._local:
            ids = list(self._mem.offline.pop(user_id, []))
            return ids
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "DELETE FROM pending_for_offline WHERE user_id = $1 RETURNING dispatch_id",
                user_id,
            )
            return [r["dispatch_id"] for r in rows]

    # ---------------- magic links ----------------

    async def create_magic_link(self, token: str, email: str, expires_at) -> None:
        if self._local:
            self._mem.magic_links[token] = {"email": email, "expires_at": expires_at, "used_at": None}
            return
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO magic_links (token, email, expires_at) VALUES ($1, $2, $3)",
                token, email, expires_at,
            )

    async def consume_magic_link(self, token: str) -> Optional[str]:
        if self._local:
            link = self._mem.magic_links.get(token)
            if not link or link["used_at"] is not None:
                return None
            now = datetime.now(timezone.utc)
            exp = link["expires_at"]
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if now > exp:
                return None
            link["used_at"] = now
            return link["email"]
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE magic_links SET used_at = NOW()
                WHERE token = $1 AND used_at IS NULL AND expires_at > NOW()
                RETURNING email
                """,
                token,
            )
            return row["email"] if row else None


# Module-level singleton; init/close lifecycle is managed by the broker app.
STORE = Store()
