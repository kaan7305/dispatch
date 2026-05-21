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

from dispatch.shared.schema import (
    DispatchEvent,
    DispatchPayload,
    DispatchStatus,
    utcnow,
)

logger = logging.getLogger("dispatch.broker.store")

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


@dataclass
class StoredDispatch:
    payload: DispatchPayload
    status: DispatchStatus
    trust_link_id: Optional[UUID] = None
    sender_device: Optional[UUID] = None
    nonce: Optional[str] = None
    signature: Optional[bytes] = None


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
        self,
        payload: DispatchPayload,
        status: DispatchStatus,
        trust_link_id: Optional[UUID] = None,
        sender_device: Optional[UUID] = None,
        nonce: Optional[str] = None,
        signature: Optional[bytes] = None,
    ) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO dispatches (
                    dispatch_id, sender_id, recipient_id, task,
                    metadata, created_at, expires_at, status, trust_link_id,
                    sender_device, nonce, signature
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                """,
                payload.dispatch_id,
                payload.sender_id,
                payload.recipient_id,
                payload.task,
                payload.metadata,
                payload.created_at,
                payload.expires_at,
                status.value,
                trust_link_id,
                sender_device,
                nonce,
                signature,
            )

    async def get_device_public_key(self, device_id: UUID) -> Optional[bytes]:
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT public_key FROM devices WHERE device_id = $1", device_id
            )

    async def list_inflight_dispatches(self, trust_link_id: UUID) -> list[dict]:
        """Dispatches on this trust edge not yet in a terminal state —
        the ones a revocation must cancel."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT dispatch_id, recipient_id FROM dispatches
                WHERE trust_link_id = $1
                  AND status IN ('pending', 'delivered', 'accepted', 'running')
                """,
                trust_link_id,
            )
            return [dict(r) for r in rows]

    async def count_recent_dispatches(self, trust_link_id: UUID, since) -> int:
        """How many dispatches have gone over this trust edge since `since`.
        Backs the per-edge daily rate limit."""
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                """
                SELECT COUNT(*) FROM dispatches
                WHERE trust_link_id = $1 AND created_at >= $2
                """,
                trust_link_id, since,
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
        return StoredDispatch(
            payload=payload,
            status=DispatchStatus(row["status"]),
            trust_link_id=row["trust_link_id"],
            sender_device=row["sender_device"],
            nonce=row["nonce"],
            signature=row["signature"],
        )

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

    # ---------------- devices ----------------

    async def enroll_device(
        self, user_id: str, label: str, public_key: bytes
    ) -> UUID:
        """Register a device. Idempotent on public_key: re-enrolling the same
        key for the same user returns the existing device_id."""
        async with self.pool.acquire() as conn:
            existing = await conn.fetchval(
                "SELECT device_id FROM devices WHERE user_id = $1 AND public_key = $2",
                user_id, public_key,
            )
            if existing is not None:
                return existing
            return await conn.fetchval(
                """
                INSERT INTO devices (user_id, label, public_key)
                VALUES ($1, $2, $3)
                RETURNING device_id
                """,
                user_id, label, public_key,
            )

    async def get_device_for_user(
        self, device_id: UUID, user_id: str
    ) -> Optional[dict]:
        """Fetch a device only if it belongs to user_id."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT device_id, user_id, label, status, last_seen, created_at
                FROM devices WHERE device_id = $1 AND user_id = $2
                """,
                device_id, user_id,
            )
            return dict(row) if row else None

    async def list_devices(self, user_id: str) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT device_id, label, status, last_seen, created_at
                FROM devices WHERE user_id = $1
                ORDER BY created_at
                """,
                user_id,
            )
            return [dict(r) for r in rows]

    async def revoke_device(self, user_id: str, device_id: UUID) -> bool:
        """Mark a device revoked. Returns False if it isn't this user's device."""
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE devices SET status = 'revoked'
                WHERE device_id = $1 AND user_id = $2 AND status != 'revoked'
                """,
                device_id, user_id,
            )
            return result.endswith(" 1")

    async def touch_device_last_seen(self, device_id: UUID) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE devices SET last_seen = NOW() WHERE device_id = $1",
                device_id,
            )

    # ---------------- invitations ----------------

    async def create_invitation(
        self, from_user: str, to_email: str, token: str, expires_at
    ) -> UUID:
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                """
                INSERT INTO invitations (from_user, to_email, token, expires_at)
                VALUES ($1, $2, $3, $4)
                RETURNING invitation_id
                """,
                from_user, to_email, token, expires_at,
            )

    async def get_invitation_by_token(self, token: str) -> Optional[dict]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT invitation_id, from_user, to_email, status,
                       expires_at, created_at
                FROM invitations WHERE token = $1
                """,
                token,
            )
            return dict(row) if row else None

    async def list_invitations(self, user_id: str) -> tuple[list[dict], list[dict]]:
        """Returns (sent, received) pending invitations for the user.
        user_id is the email, so received = invites addressed to it."""
        async with self.pool.acquire() as conn:
            sent = await conn.fetch(
                """
                SELECT invitation_id, from_user, to_email, token, status, created_at
                FROM invitations
                WHERE from_user = $1 AND status = 'pending'
                ORDER BY created_at DESC
                """,
                user_id,
            )
            received = await conn.fetch(
                """
                SELECT invitation_id, from_user, to_email, token, status, created_at
                FROM invitations
                WHERE LOWER(to_email) = LOWER($1) AND status = 'pending'
                ORDER BY created_at DESC
                """,
                user_id,
            )
            return ([dict(r) for r in sent], [dict(r) for r in received])

    async def accept_invitation(
        self, token: str, accepter: str, scopes: dict
    ) -> tuple[Optional[UUID], Optional[str]]:
        """Transactionally consume an invitation and create the trust edge.

        Returns (trust_link_id, None) on success, or (None, reason) where
        reason is one of: not_found, already_resolved, expired,
        wrong_recipient.
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                inv = await conn.fetchrow(
                    """
                    SELECT from_user, to_email, status, expires_at
                    FROM invitations WHERE token = $1 FOR UPDATE
                    """,
                    token,
                )
                if inv is None:
                    return None, "not_found"
                if inv["status"] != "pending":
                    return None, "already_resolved"
                if inv["expires_at"] <= utcnow():
                    return None, "expired"
                if accepter.strip().lower() != inv["to_email"].strip().lower():
                    return None, "wrong_recipient"

                await conn.execute(
                    "UPDATE invitations SET status = 'accepted' WHERE token = $1",
                    token,
                )
                await conn.execute(
                    "INSERT INTO users (user_id) VALUES ($1) ON CONFLICT DO NOTHING",
                    accepter,
                )
                trust_link_id = await conn.fetchval(
                    """
                    INSERT INTO trust_links (from_user, to_user, status, scopes)
                    VALUES ($1, $2, 'accepted', $3)
                    ON CONFLICT (from_user, to_user) DO UPDATE
                        SET status = 'accepted',
                            scopes = EXCLUDED.scopes,
                            updated_at = NOW()
                    RETURNING trust_link_id
                    """,
                    inv["from_user"], accepter, scopes,
                )
                return trust_link_id, None

    async def decline_invitation(self, token: str) -> bool:
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE invitations SET status = 'declined'
                WHERE token = $1 AND status = 'pending'
                """,
                token,
            )
            return result.endswith(" 1")

    # ---------------- trust links ----------------

    async def list_trust_links(self, user_id: str) -> list[dict]:
        """Accepted edges in either direction involving the user."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT trust_link_id, from_user, to_user, status, scopes,
                       created_at, updated_at
                FROM trust_links
                WHERE (from_user = $1 OR to_user = $1) AND status = 'accepted'
                ORDER BY updated_at DESC
                """,
                user_id,
            )
            return [dict(r) for r in rows]

    async def get_trust_link(self, trust_link_id: UUID) -> Optional[dict]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT trust_link_id, from_user, to_user, status, scopes
                FROM trust_links WHERE trust_link_id = $1
                """,
                trust_link_id,
            )
            return dict(row) if row else None

    async def get_trust_edge(
        self, from_user: str, to_user: str
    ) -> Optional[dict]:
        """The accepted edge that authorizes from_user → to_user, if any."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT trust_link_id, from_user, to_user, status, scopes
                FROM trust_links
                WHERE from_user = $1 AND to_user = $2 AND status = 'accepted'
                """,
                from_user, to_user,
            )
            return dict(row) if row else None

    async def update_trust_scopes(
        self, trust_link_id: UUID, editor: str, scopes: dict
    ) -> bool:
        """Only the to_user (the trustor) may edit an edge's scopes."""
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE trust_links
                SET scopes = $1, updated_at = NOW()
                WHERE trust_link_id = $2 AND to_user = $3 AND status = 'accepted'
                """,
                scopes, trust_link_id, editor,
            )
            return result.endswith(" 1")

    async def revoke_trust_link(self, trust_link_id: UUID, user_id: str) -> bool:
        """Either party may revoke the edge."""
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE trust_links
                SET status = 'revoked', updated_at = NOW()
                WHERE trust_link_id = $1
                  AND (from_user = $2 OR to_user = $2)
                  AND status = 'accepted'
                """,
                trust_link_id, user_id,
            )
            return result.endswith(" 1")


# Module-level singleton; init/close lifecycle is managed by the broker app.
STORE = Store()
