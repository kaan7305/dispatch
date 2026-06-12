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

    # ---------------- device-authorization grant ----------------

    async def create_device_auth(self, device_code: str, user_code: str, expires_at) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO device_auth (device_code, user_code, expires_at)
                VALUES ($1, $2, $3)
                """,
                device_code, user_code, expires_at,
            )

    async def get_device_auth(self, device_code: str) -> Optional[dict]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM device_auth WHERE device_code = $1", device_code
            )
            return dict(row) if row else None

    async def approve_device_auth(self, user_code: str, user_id: str) -> str:
        """Bind a pending user_code to the authenticated human. Returns
        'approved' on success, 'not_found' if no such pending code, or
        'expired' if it lapsed. Idempotent: re-approving an approved code is ok."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT status, expires_at FROM device_auth WHERE user_code = $1", user_code
            )
            if row is None:
                return "not_found"
            if row["expires_at"] <= utcnow():
                return "expired"
            if row["status"] == "consumed":
                return "not_found"
            await conn.execute(
                "UPDATE device_auth SET status = 'approved', user_id = $2 WHERE user_code = $1",
                user_code, user_id,
            )
            return "approved"

    async def consume_device_auth(self, device_code: str) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE device_auth SET status = 'consumed' WHERE device_code = $1",
                device_code,
            )

    async def mark_signed_out(self, user_id: str) -> None:
        """Bump the user's `signed_out_at` to now. Subsequent JWT checks
        with iat earlier than this timestamp will be rejected."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO users (user_id, signed_out_at)
                VALUES ($1, NOW())
                ON CONFLICT (user_id) DO UPDATE SET signed_out_at = NOW()
                """,
                user_id,
            )

    async def get_signed_out_at(self, user_id: str):
        """Return the user's last sign-out timestamp, or None if never."""
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT signed_out_at FROM users WHERE user_id = $1", user_id,
            )

    async def list_users(self) -> list[str]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT user_id FROM users ORDER BY user_id")
            return [r["user_id"] for r in rows]

    async def set_user_phone(self, user_id: str, phone: Optional[str]) -> None:
        """Set (or clear, with None) the user's SMS number. Upserts the user
        row so a phone can be saved before any other write touches it."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO users (user_id, phone) VALUES ($1, $2)
                ON CONFLICT (user_id) DO UPDATE SET phone = EXCLUDED.phone
                """,
                user_id, phone,
            )

    async def get_user_phone(self, user_id: str) -> Optional[str]:
        """Return the user's notification phone, or None if unset."""
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT phone FROM users WHERE user_id = $1", user_id,
            )

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
                  AND status IN ('awaiting_signature', 'pending', 'delivered', 'accepted', 'running')
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

    async def requeue_undelivered(self, recipient_id: str) -> list[UUID]:
        """Re-queue this recipient's delivered-but-unaccepted dispatches.

        Called when a recipient daemon disconnects: anything pushed to it
        live but not yet accepted (status 'delivered') would otherwise never
        be re-offered, since it was never in the offline queue. Reset it to
        'pending' and enqueue so the next reconnect re-pushes it.
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                rows = await conn.fetch(
                    """
                    UPDATE dispatches SET status = 'pending'
                    WHERE recipient_id = $1 AND status = 'delivered'
                    RETURNING dispatch_id
                    """,
                    recipient_id,
                )
                ids = [r["dispatch_id"] for r in rows]
                for did in ids:
                    await conn.execute(
                        """
                        INSERT INTO pending_for_offline (user_id, dispatch_id)
                        VALUES ($1, $2)
                        ON CONFLICT (user_id, dispatch_id) DO NOTHING
                        """,
                        recipient_id, did,
                    )
                return ids

    # ---------------- pending-signature queue (sender offline) ----------------

    async def enqueue_for_signature(self, sender_id: str, dispatch_id: UUID) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO pending_for_signature (sender_id, dispatch_id)
                VALUES ($1, $2)
                ON CONFLICT (sender_id, dispatch_id) DO NOTHING
                """,
                sender_id,
                dispatch_id,
            )

    async def pop_signature_queue(self, sender_id: str) -> list[UUID]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                DELETE FROM pending_for_signature
                WHERE sender_id = $1
                RETURNING dispatch_id
                """,
                sender_id,
            )
            return [r["dispatch_id"] for r in rows]

    async def attach_signature(
        self,
        dispatch_id: UUID,
        sender_device: UUID,
        signature: bytes,
        status: DispatchStatus,
    ) -> None:
        """Record the signature produced once the sender's daemon reconnected,
        binding the signing device, and promote the dispatch out of
        'awaiting_signature'. The nonce + created_at were fixed at creation
        and are what the signature covers."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE dispatches
                SET sender_device = $2, signature = $3, status = $4
                WHERE dispatch_id = $1
                """,
                dispatch_id, sender_device, signature, status.value,
            )

    # ---------------- expiry sweeper ----------------

    async def expire_overdue(self) -> list[dict]:
        """Mark not-yet-started dispatches past their expires_at as expired,
        and drop them from both delivery queues. Returns the affected rows
        ({dispatch_id, recipient_id}) so the caller can notify watchers."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                rows = await conn.fetch(
                    """
                    UPDATE dispatches SET status = 'expired'
                    WHERE expires_at < NOW()
                      AND status IN ('awaiting_signature', 'pending', 'delivered')
                    RETURNING dispatch_id, recipient_id
                    """
                )
                ids = [r["dispatch_id"] for r in rows]
                if ids:
                    await conn.execute(
                        "DELETE FROM pending_for_offline WHERE dispatch_id = ANY($1::uuid[])",
                        ids,
                    )
                    await conn.execute(
                        "DELETE FROM pending_for_signature WHERE dispatch_id = ANY($1::uuid[])",
                        ids,
                    )
                return [dict(r) for r in rows]

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

    async def list_device_keys(self, user_id: str) -> list[dict]:
        """Active devices' public keys for this user — the roster a runner daemon
        verifies remote tool-approval signatures against (phone-as-approver)."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT device_id, public_key, status
                FROM devices WHERE user_id = $1 AND status = 'active'
                ORDER BY created_at
                """,
                user_id,
            )
            return [dict(r) for r in rows]

    async def device_belongs_to(self, user_id: str, device_id: UUID) -> bool:
        """True iff this device is an active device of this user."""
        async with self.pool.acquire() as conn:
            return bool(
                await conn.fetchval(
                    """
                    SELECT 1 FROM devices
                    WHERE device_id = $1 AND user_id = $2 AND status = 'active'
                    """,
                    device_id, user_id,
                )
            )

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

    async def rename_device(self, user_id: str, device_id: UUID, label: str) -> bool:
        """Rename a device. Returns False if it doesn't belong to this user."""
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE devices SET label = $1
                WHERE device_id = $2 AND user_id = $3 AND status = 'active'
                """,
                label, device_id, user_id,
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


    # ---------------- account events (trust-layer audit log) -------------

    async def record_account_event(
        self, actor: str, peer: str, event_type: str, data: dict
    ) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO account_events (actor, peer, type, data)
                VALUES ($1, $2, $3, $4)
                """,
                actor, peer, event_type, data,
            )

    async def list_account_events(self, user_id: str, limit: int) -> list[dict]:
        """Events the user took part in, either side, newest first.
        Peer is matched case-insensitively: for invitations it's a raw
        email that may predate the user's registration."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, actor, peer, type, data, created_at
                FROM account_events
                WHERE actor = $1 OR LOWER(peer) = LOWER($1)
                ORDER BY created_at DESC, id DESC
                LIMIT $2
                """,
                user_id, limit,
            )
            return [dict(r) for r in rows]

    # ---------------- contexts (reusable system_prompt + files) ----------

    async def create_context(
        self, owner_id: str, name: str, description: str,
        system_prompt: str, files: list[dict],
    ) -> UUID:
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                """
                INSERT INTO contexts (owner_id, name, description, system_prompt, files)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING context_id
                """,
                owner_id, name, description, system_prompt, files,
            )

    async def update_context(
        self, context_id: UUID, owner_id: str, name: str,
        description: str, system_prompt: str, files: list[dict],
    ) -> bool:
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE contexts
                SET name = $1, description = $2, system_prompt = $3,
                    files = $4, updated_at = NOW()
                WHERE context_id = $5 AND owner_id = $6
                """,
                name, description, system_prompt, files, context_id, owner_id,
            )
            return result.endswith(" 1")

    async def get_context(
        self, context_id: UUID, owner_id: str,
    ) -> Optional[dict]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT context_id, owner_id, name, description, system_prompt,
                       files, created_at, updated_at
                FROM contexts WHERE context_id = $1 AND owner_id = $2
                """,
                context_id, owner_id,
            )
            return dict(row) if row else None

    async def list_contexts(self, owner_id: str) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT context_id, owner_id, name, description, system_prompt,
                       files, created_at, updated_at
                FROM contexts WHERE owner_id = $1
                ORDER BY updated_at DESC
                """,
                owner_id,
            )
            return [dict(r) for r in rows]

    async def delete_context(
        self, context_id: UUID, owner_id: str,
    ) -> bool:
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM contexts WHERE context_id = $1 AND owner_id = $2",
                context_id, owner_id,
            )
            return result.endswith(" 1")

    # ---------------- workflows ----------------

    async def create_workflow(
        self, owner_id: str, name: str, definition: dict
    ) -> UUID:
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                """
                INSERT INTO workflows (owner_id, name, definition)
                VALUES ($1, $2, $3)
                RETURNING workflow_id
                """,
                owner_id, name, definition,
            )

    async def update_workflow(
        self, workflow_id: UUID, owner_id: str, name: str, definition: dict
    ) -> bool:
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE workflows
                SET name = $1, definition = $2, updated_at = NOW()
                WHERE workflow_id = $3 AND owner_id = $4
                """,
                name, definition, workflow_id, owner_id,
            )
            return result.endswith(" 1")

    async def get_workflow(self, workflow_id: UUID, owner_id: str) -> Optional[dict]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT workflow_id, owner_id, name, definition, created_at, updated_at
                FROM workflows WHERE workflow_id = $1 AND owner_id = $2
                """,
                workflow_id, owner_id,
            )
            return dict(row) if row else None

    async def list_workflows(self, owner_id: str) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT workflow_id, owner_id, name, definition, created_at, updated_at
                FROM workflows WHERE owner_id = $1
                ORDER BY updated_at DESC
                """,
                owner_id,
            )
            return [dict(r) for r in rows]

    async def delete_workflow(self, workflow_id: UUID, owner_id: str) -> bool:
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM workflows WHERE workflow_id = $1 AND owner_id = $2",
                workflow_id, owner_id,
            )
            return result.endswith(" 1")

    # ---------------- workflow runs ----------------

    async def create_workflow_run(
        self, run_id: UUID, workflow_id: UUID, triggered_by: str, input_: dict,
    ) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO workflow_runs (run_id, workflow_id, triggered_by, status, input, node_states)
                VALUES ($1, $2, $3, 'running', $4, '{}'::jsonb)
                """,
                run_id, workflow_id, triggered_by, input_,
            )

    async def update_workflow_run(
        self, run_id: UUID, *, status: Optional[str] = None,
        node_states: Optional[dict] = None, error: Optional[str] = None,
        ended: bool = False,
    ) -> None:
        sets: list[str] = []
        args: list = []
        if status is not None:
            args.append(status); sets.append(f"status = ${len(args)}")
        if node_states is not None:
            args.append(node_states); sets.append(f"node_states = ${len(args)}")
        if error is not None:
            args.append(error); sets.append(f"error = ${len(args)}")
        if ended:
            sets.append("ended_at = NOW()")
        if not sets:
            return
        args.append(run_id)
        async with self.pool.acquire() as conn:
            await conn.execute(
                f"UPDATE workflow_runs SET {', '.join(sets)} WHERE run_id = ${len(args)}",
                *args,
            )

    async def get_workflow_run_for_user(
        self, run_id: UUID, user_id: str,
    ) -> Optional[dict]:
        """Return the run row if `user_id` is either the workflow owner
        OR the executor (triggered_by). Used by GET /runs/{id} and the
        PATCH endpoint so the recipient daemon (executor) can stream
        checkpoints back even though the workflow itself is owned by
        the sender."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT r.run_id, r.workflow_id, r.triggered_by, r.status,
                       r.input, r.node_states, r.error,
                       r.started_at, r.ended_at
                FROM workflow_runs r
                JOIN workflows w ON w.workflow_id = r.workflow_id
                WHERE r.run_id = $1
                  AND (r.triggered_by = $2 OR w.owner_id = $2)
                """,
                run_id, user_id,
            )
            return dict(row) if row else None

    async def list_workflow_runs(self, workflow_id: UUID) -> list[dict]:
        """All runs for a workflow. Caller is responsible for gating
        ownership via get_workflow() before calling — we don't filter by
        triggered_by because runs are now executed by recipients, not
        the workflow owner."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT run_id, workflow_id, triggered_by, status, started_at, ended_at
                FROM workflow_runs
                WHERE workflow_id = $1
                ORDER BY started_at DESC
                LIMIT 50
                """,
                workflow_id,
            )
            return [dict(r) for r in rows]


# Module-level singleton; init/close lifecycle is managed by the broker app.
STORE = Store()
