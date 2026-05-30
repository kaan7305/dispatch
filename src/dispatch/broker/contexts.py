"""Context-packs CRUD endpoints.

A context pack is a reusable bundle of (system_prompt, files[]) the
sender can attach to a workflow without re-pasting the same content
into a Context node every time. The workflow Context node has a
"Load from library" picker that prefills its inline params from one
of these packs — packs are snapshotted into the workflow at edit
time, so a later edit to a pack does NOT silently change shipped
workflows. Keeps workflows self-contained.
"""
from __future__ import annotations

import logging
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException

from dispatch.broker.store import STORE
from dispatch.shared.identity import IdentityError, verify_token_with_iat
from dispatch.shared.schema import ContextCreateRequest

logger = logging.getLogger("dispatch.broker.contexts")

router = APIRouter(prefix="/contexts", tags=["contexts"])


async def _authed_user(authorization: Optional[str] = Header(None)) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    try:
        user_id, iat = verify_token_with_iat(token)
    except IdentityError as e:
        raise HTTPException(status_code=401, detail=str(e))
    signed_out_at = await STORE.get_signed_out_at(user_id)
    if signed_out_at is not None and int(signed_out_at.timestamp()) > iat:
        raise HTTPException(status_code=401, detail="Token revoked — sign in again")
    return user_id


def _summary(row: dict) -> dict:
    files = row.get("files") or []
    return {
        "context_id": str(row["context_id"]),
        "name": row["name"],
        "description": row.get("description") or "",
        "file_count": len(files) if isinstance(files, list) else 0,
        "has_system_prompt": bool((row.get("system_prompt") or "").strip()),
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
    }


def _full(row: dict) -> dict:
    return {
        "context_id": str(row["context_id"]),
        "owner_id": row["owner_id"],
        "name": row["name"],
        "description": row.get("description") or "",
        "system_prompt": row.get("system_prompt") or "",
        "files": row.get("files") or [],
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
    }


@router.get("")
async def list_contexts(user_id: str = Depends(_authed_user)) -> dict:
    rows = await STORE.list_contexts(user_id)
    return {"contexts": [_summary(r) for r in rows]}


@router.post("")
async def create_context(
    req: ContextCreateRequest, user_id: str = Depends(_authed_user),
) -> dict:
    files = [f.model_dump(mode="json") for f in req.files]
    context_id = await STORE.create_context(
        user_id, req.name, req.description, req.system_prompt, files,
    )
    return {"context_id": str(context_id)}


@router.get("/{context_id}")
async def get_context(
    context_id: UUID, user_id: str = Depends(_authed_user),
) -> dict:
    row = await STORE.get_context(context_id, user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Unknown context")
    return _full(row)


@router.put("/{context_id}")
async def update_context(
    context_id: UUID,
    req: ContextCreateRequest,
    user_id: str = Depends(_authed_user),
) -> dict:
    files = [f.model_dump(mode="json") for f in req.files]
    ok = await STORE.update_context(
        context_id, user_id, req.name, req.description, req.system_prompt, files,
    )
    if not ok:
        raise HTTPException(status_code=404, detail="Unknown context")
    return {"status": "updated"}


@router.delete("/{context_id}")
async def delete_context(
    context_id: UUID, user_id: str = Depends(_authed_user),
) -> dict:
    ok = await STORE.delete_context(context_id, user_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Unknown context")
    return {"status": "deleted"}
