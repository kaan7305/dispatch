"""Workflows CRUD + run-orchestration endpoints.

Mounted onto the broker FastAPI app under /workflows and /runs. The
workflow_run_start frame the POST /workflows/{id}/run handler emits is
the one the daemon's engine listens for to begin local execution; the
engine streams progress back via PATCH /runs/{id} so the broker's row
is the durable source of truth.
"""
from __future__ import annotations

import json
import logging
from typing import Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from dispatch.broker.state import STATE
from dispatch.broker.store import STORE
from dispatch.shared.identity import IdentityError, verify_token_with_iat
from dispatch.shared.schema import (
    WorkflowCreateRequest,
    WorkflowRunCreateRequest,
)

logger = logging.getLogger("dispatch.broker.workflows")

router = APIRouter(prefix="/workflows", tags=["workflows"])
runs_router = APIRouter(prefix="/runs", tags=["workflows"])


# Local copy of the dependency to avoid a circular import on broker.app.
# Mirrors authed_user there: bearer token + server-side revocation check.
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
    definition = row.get("definition") or {}
    nodes = definition.get("nodes") or []
    return {
        "workflow_id": str(row["workflow_id"]),
        "name": row["name"],
        "node_count": len(nodes),
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
    }


def _full(row: dict) -> dict:
    return {
        "workflow_id": str(row["workflow_id"]),
        "owner_id": row["owner_id"],
        "name": row["name"],
        "definition": row.get("definition") or {"nodes": [], "edges": []},
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
    }


def _run_summary(row: dict) -> dict:
    return {
        "run_id": str(row["run_id"]),
        "workflow_id": str(row["workflow_id"]),
        "triggered_by": row["triggered_by"],
        "status": row["status"],
        "started_at": row["started_at"].isoformat(),
        "ended_at": row["ended_at"].isoformat() if row.get("ended_at") else None,
    }


def _run_full(row: dict) -> dict:
    return {
        "run_id": str(row["run_id"]),
        "workflow_id": str(row["workflow_id"]),
        "triggered_by": row["triggered_by"],
        "status": row["status"],
        "input": row.get("input") or {},
        "node_states": row.get("node_states") or {},
        "error": row.get("error"),
        "started_at": row["started_at"].isoformat(),
        "ended_at": row["ended_at"].isoformat() if row.get("ended_at") else None,
    }


# ----------------------------------------------------------------------------
# CRUD
# ----------------------------------------------------------------------------


@router.get("")
async def list_workflows(user_id: str = Depends(_authed_user)) -> dict:
    rows = await STORE.list_workflows(user_id)
    return {"workflows": [_summary(r) for r in rows]}


@router.post("")
async def create_workflow(
    req: WorkflowCreateRequest, user_id: str = Depends(_authed_user)
) -> dict:
    definition = req.definition.model_dump(mode="json", by_alias=True)
    workflow_id = await STORE.create_workflow(user_id, req.name, definition)
    return {"workflow_id": str(workflow_id)}


@router.get("/{workflow_id}")
async def get_workflow(
    workflow_id: UUID, user_id: str = Depends(_authed_user)
) -> dict:
    row = await STORE.get_workflow(workflow_id, user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Unknown workflow")
    return _full(row)


@router.put("/{workflow_id}")
async def update_workflow(
    workflow_id: UUID,
    req: WorkflowCreateRequest,
    user_id: str = Depends(_authed_user),
) -> dict:
    definition = req.definition.model_dump(mode="json", by_alias=True)
    ok = await STORE.update_workflow(workflow_id, user_id, req.name, definition)
    if not ok:
        raise HTTPException(status_code=404, detail="Unknown workflow")
    return {"status": "updated"}


@router.delete("/{workflow_id}")
async def delete_workflow(
    workflow_id: UUID, user_id: str = Depends(_authed_user)
) -> dict:
    ok = await STORE.delete_workflow(workflow_id, user_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Unknown workflow")
    return {"status": "deleted"}


# ----------------------------------------------------------------------------
# Runs
# ----------------------------------------------------------------------------


@router.get("/{workflow_id}/runs")
async def list_runs(
    workflow_id: UUID, user_id: str = Depends(_authed_user)
) -> dict:
    # 404 if the workflow isn't theirs — never leak rows from other owners.
    if await STORE.get_workflow(workflow_id, user_id) is None:
        raise HTTPException(status_code=404, detail="Unknown workflow")
    rows = await STORE.list_workflow_runs(workflow_id, user_id)
    return {"runs": [_run_summary(r) for r in rows]}


@router.post("/{workflow_id}/run")
async def start_run(
    workflow_id: UUID,
    req: WorkflowRunCreateRequest,
    user_id: str = Depends(_authed_user),
) -> dict:
    """Create the run row, then fan out workflow_run_start to every connected
    daemon of the user. The daemon's engine executes locally and streams
    progress back via PATCH /runs/{id}."""
    workflow = await STORE.get_workflow(workflow_id, user_id)
    if workflow is None:
        raise HTTPException(status_code=404, detail="Unknown workflow")

    definition = workflow.get("definition") or {"nodes": [], "edges": []}
    run_id = uuid4()
    await STORE.create_workflow_run(run_id, workflow_id, user_id, req.input)

    frame = json.dumps(
        {
            "type": "workflow_run_start",
            "run_id": str(run_id),
            "workflow_id": str(workflow_id),
            "definition": definition,
            "input": req.input,
        }
    )
    devices = STATE.agents.get(user_id, {})
    delivered = 0
    for ws in list(devices.values()):
        try:
            await ws.send_text(frame)
            delivered += 1
        except Exception:
            logger.exception("failed to push workflow_run_start")

    return {"run_id": str(run_id), "notified": delivered}


# ----------------------------------------------------------------------------
# Run snapshots (engine checkpoints + UI polling)
# ----------------------------------------------------------------------------


class _RunPatch(BaseModel):
    status: Optional[str] = None
    node_states: Optional[dict] = None
    error: Optional[str] = None
    ended: bool = False


@runs_router.get("/{run_id}")
async def get_run(run_id: UUID, user_id: str = Depends(_authed_user)) -> dict:
    row = await STORE.get_workflow_run(run_id, user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Unknown run")
    return _run_full(row)


@runs_router.patch("/{run_id}")
async def patch_run(
    run_id: UUID,
    req: _RunPatch,
    user_id: str = Depends(_authed_user),
) -> dict:
    # Ownership check so a daemon can't checkpoint other users' runs.
    if await STORE.get_workflow_run(run_id, user_id) is None:
        raise HTTPException(status_code=404, detail="Unknown run")
    await STORE.update_workflow_run(
        run_id,
        status=req.status,
        node_states=req.node_states,
        error=req.error,
        ended=req.ended,
    )
    return {"status": "ok"}
