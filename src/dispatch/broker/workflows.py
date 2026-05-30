"""Workflows CRUD + dispatch-fan-out endpoints.

Mounted onto the broker FastAPI app under /workflows and /runs. The
n8n-style execution model: the sender designs a graph, picks N
recipients via POST /workflows/{id}/run, and the broker creates one
dispatch + one run row per recipient. Each dispatch carries a
WorkflowDispatchEnvelope as `metadata.workflow`; the recipient's
daemon detects it and runs the engine locally instead of the normal
single-prompt agent flow. The engine PATCHes /runs/{id} as it walks
the graph.
"""
from __future__ import annotations

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
# Runs — fan-out via dispatch
# ----------------------------------------------------------------------------


@router.get("/{workflow_id}/runs")
async def list_runs(
    workflow_id: UUID, user_id: str = Depends(_authed_user)
) -> dict:
    if await STORE.get_workflow(workflow_id, user_id) is None:
        raise HTTPException(status_code=404, detail="Unknown workflow")
    rows = await STORE.list_workflow_runs(workflow_id)
    return {"runs": [_run_summary(r) for r in rows]}


@router.post("/{workflow_id}/run")
async def start_run(
    workflow_id: UUID,
    req: WorkflowRunCreateRequest,
    user_id: str = Depends(_authed_user),
) -> dict:
    """Dispatch the workflow to each recipient. Returns one row per
    recipient with the run_id + dispatch_id (or a failure reason).

    The actual delivery uses the same trust → sign → deliver path as a
    normal dispatch; only the payload is different — `task` is a human
    header and `metadata.workflow` carries the envelope the recipient's
    daemon engine consumes.
    """
    workflow = await STORE.get_workflow(workflow_id, user_id)
    if workflow is None:
        raise HTTPException(status_code=404, detail="Unknown workflow")

    definition = workflow.get("definition") or {"nodes": [], "edges": []}
    workflow_name = workflow.get("name") or "workflow"

    # Sender daemon WS is needed for signing each dispatch — same
    # constraint POST /dispatch enforces.
    picked = STATE.pick_device(user_id)
    if picked is None:
        raise HTTPException(
            status_code=503,
            detail="Your daemon is offline — start dispatch-daemon to run workflows.",
        )
    sender_device_id, sender_ws = picked

    # _create_one_dispatch + _DispatchFailed live in broker/app.py to
    # keep them next to the /dispatch endpoint that uses them. Import
    # late to dodge the circular import (broker/app imports this module).
    from dispatch.broker.app import _create_one_dispatch, _DispatchFailed

    dispatched: list[dict] = []
    failures: list[dict] = []
    for recipient in _dedup_recipients(req.recipient_ids):
        run_id = uuid4()
        envelope = {
            "run_id": str(run_id),
            "workflow_id": str(workflow_id),
            "workflow_name": workflow_name,
            "definition": definition,
            "input": req.input,
        }
        task_line = f"Workflow: {workflow_name}"

        # Create the run row FIRST so a fast-accept recipient daemon
        # can PATCH it without racing the INSERT. triggered_by is the
        # executor (recipient); list/get gate by workflow ownership.
        await STORE.create_workflow_run(
            run_id, workflow_id, recipient, req.input,
        )

        try:
            result = await _create_one_dispatch(
                sender=user_id,
                recipient=recipient,
                task=task_line,
                expires_in_seconds=3600,
                metadata={"workflow": envelope},
                sender_device_id=sender_device_id,
                sender_ws=sender_ws,
            )
        except _DispatchFailed as f:
            # Roll back the run row we pre-inserted so it doesn't show
            # up as a perpetually-pending run in the owner's history.
            await STORE.update_workflow_run(
                run_id,
                status="failed",
                error=f"dispatch refused: {f.detail}",
                ended=True,
            )
            failures.append({
                "recipient_id": recipient,
                "status_code": f.status,
                "error": f.detail,
            })
            continue

        dispatched.append({
            "recipient_id": recipient,
            "run_id": str(run_id),
            "dispatch_id": result["dispatch_id"],
            "dispatch_status": result["status"],
        })

    if not dispatched and failures:
        # Total failure — surface the first reason like POST /dispatch does.
        only = failures[0]
        raise HTTPException(status_code=only["status_code"], detail=only["error"])

    return {"dispatched": dispatched, "failures": failures}


def _dedup_recipients(ids: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for r in ids:
        r = (r or "").strip().lower()
        if r and r not in seen:
            seen.add(r)
            out.append(r)
    return out


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
    row = await STORE.get_workflow_run_for_user(run_id, user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Unknown run")
    return _run_full(row)


@runs_router.patch("/{run_id}")
async def patch_run(
    run_id: UUID,
    req: _RunPatch,
    user_id: str = Depends(_authed_user),
) -> dict:
    # The recipient (executor) is the one writing checkpoints. The
    # workflow owner is also allowed — useful for cancel-from-owner.
    if await STORE.get_workflow_run_for_user(run_id, user_id) is None:
        raise HTTPException(status_code=404, detail="Unknown run")
    await STORE.update_workflow_run(
        run_id,
        status=req.status,
        node_states=req.node_states,
        error=req.error,
        ended=req.ended,
    )
    return {"status": "ok"}
