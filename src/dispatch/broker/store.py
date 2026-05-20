"""Persistent storage for the broker (Postgres via asyncpg).

This is the only module that talks SQL. Routes call `STORE.xxx()`; if we
later swap Postgres for something else (Neon branch, sharded Postgres,
test stub), nothing outside this file needs to change.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from uuid import UUID

import asyncpg

from dispatch.shared.schema import DispatchEvent, DispatchPayload, DispatchStatus

logger = logging.getLogger("dispatch.broker.store")

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


@dataclass
class StoredDispatch:
    payload: DispatchPayload
    status: DispatchStatus


class Store:
    def __init__(self, dsn: str | None = None) -> None:
        self.dsn = dsn or os.environ.get("DATABASE_URL")
        self.pool: Optional[asyncpg.Pool] = None

    async def init(self) -> None:
        if not self.dsn:
            raise RuntimeError(
                "DATABASE_URL is not set. Add it to .env "
                "(e.g., postgresql://user:pass@host:port/dbname). On Railway, "
                "the Postgres add-on injects this automatically."
            )
        self.pool = await asyncpg.create_pool(
            dsn=self.dsn,
            min_size=1,
            max_size=10,
            init=self._init_conn,
        )
        async with self.pool.acquire() as conn:
            await conn.execute(SCHEMA_PATH.read_text())
        logger.info("Store ready")

    async def _init_conn(self, conn: asyncpg.Connection) -> None:
        # JSONB <-> Python dict.
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
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO users (user_id) VALUES ($1)
                ON CONFLICT (user_id) DO NOTHING
                """,
                user_id,
            )

    async def list_users(self) -> list[str]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT user_id FROM users ORDER BY user_id")
            return [r["user_id"] for r in rows]

    # ---------------- dispatches ----------------

    async def create_dispatch(
        self, payload: DispatchPayload, status: DispatchStatus
    ) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO dispatches (
                    dispatch_id, sender_id, recipient_id, task,
                    metadata, created_at, expires_at, status
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                """,
                payload.dispatch_id,
                payload.sender_id,
                payload.recipient_id,
                payload.task,
                payload.metadata,
                payload.created_at,
                payload.expires_at,
                status.value,
            )

    async def get_dispatch(self, dispatch_id: UUID) -> Optional[StoredDispatch]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM dispatches WHERE dispatch_id = $1", dispatch_id
            )
            return self._row_to_dispatch(row) if row else None

    async def update_status(self, dispatch_id: UUID, status: DispatchStatus) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE dispatches SET status = $1 WHERE dispatch_id = $2",
                status.value,
                dispatch_id,
            )

    async def list_dispatches_for_user(
        self, user_id: str, role: str
    ) -> list[StoredDispatch]:
        if role not in ("sent", "received"):
            raise ValueError(f"role must be 'sent' or 'received', got {role!r}")
        col = "sender_id" if role == "sent" else "recipient_id"
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT * FROM dispatches
                WHERE {col} = $1
                ORDER BY created_at DESC
                LIMIT 200
                """,
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

    async def append_event(
        self, dispatch_id: UUID, event: DispatchEvent
    ) -> int:
        async with self.pool.acquire() as conn:
            seq = await conn.fetchval(
                """
                INSERT INTO dispatch_events (dispatch_id, seq, type, data)
                VALUES (
                    $1,
                    COALESCE(
                        (SELECT MAX(seq) FROM dispatch_events WHERE dispatch_id = $1),
                        0
                    ) + 1,
                    $2,
                    $3
                )
                RETURNING seq
                """,
                dispatch_id,
                event["type"],
                event["data"],
            )
            return seq

    async def get_events(self, dispatch_id: UUID) -> list[DispatchEvent]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT type, data FROM dispatch_events
                WHERE dispatch_id = $1
                ORDER BY seq
                """,
                dispatch_id,
            )
            return [{"type": r["type"], "data": r["data"]} for r in rows]

    # ---------------- offline queue ----------------

    async def enqueue_for_offline(self, user_id: str, dispatch_id: UUID) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO pending_for_offline (user_id, dispatch_id)
                VALUES ($1, $2)
                ON CONFLICT (user_id, dispatch_id) DO NOTHING
                """,
                user_id,
                dispatch_id,
            )

    async def pop_offline_queue(self, user_id: str) -> list[UUID]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                DELETE FROM pending_for_offline
                WHERE user_id = $1
                RETURNING dispatch_id
                """,
                user_id,
            )
            return [r["dispatch_id"] for r in rows]

    # ---------------- magic links ----------------

    async def create_magic_link(
        self, token: str, email: str, expires_at
    ) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO magic_links (token, email, expires_at)
                VALUES ($1, $2, $3)
                """,
                token, email, expires_at,
            )

    async def consume_magic_link(self, token: str) -> Optional[str]:
        """Returns the email if the token is valid (unused, unexpired) and
        marks it used atomically. Returns None otherwise."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE magic_links
                SET used_at = NOW()
                WHERE token = $1
                  AND used_at IS NULL
                  AND expires_at > NOW()
                RETURNING email
                """,
                token,
            )
            return row["email"] if row else None


# Module-level singleton; init/close lifecycle is managed by the broker app.
STORE = Store()
