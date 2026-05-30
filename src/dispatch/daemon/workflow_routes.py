"""HTTP + WS routes for the daemon's workflows feature.

Mirrors the proxy pattern in local_app.py: the SPA hits /api/workflows*
on 127.0.0.1 with the local bearer, the daemon forwards each request to
the broker with the broker JWT attached. The browser never holds the
broker token.

The actual workflow ENGINE lives in dispatch.daemon.workflows. This
module only owns the wire surface — it hands the engine a parsed
{type:"workflow_run_start", ...} message when one arrives over the
broker WS.
"""
from __future__ import annotations

import logging
import secrets
from typing import Any, Optional
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

logger = logging.getLogger("dispatch.daemon.workflow_routes")


def make_router(
    engine,
    local_state,
    daemon_state,
    local_token: str,
    broker_url: str,
    broker_token_getter=None,
):
    """Build the FastAPI router exposing /api/workflows* + /api/runs/*.

    broker_token_getter: optional callable returning the current broker
    token at request time. If omitted, we fall back to local_state.broker_token
    on every request — that's the value local_app.py also reads, so the
    two stay in sync.
    """
    router = APIRouter()

    def require_local_token(request: Request) -> None:
        # Same shape as local_app.require_local_token — kept inline to
        # avoid an import cycle and to keep auth definition next to the
        # routes it guards.
        header = request.headers.get("authorization", "")
        token = header[7:] if header.lower().startswith("bearer ") else ""
        if not token:
            token = request.query_params.get("t", "")
        if not secrets.compare_digest(token, local_token):
            raise HTTPException(status_code=401, detail="missing or wrong local token")

    def _current_broker_token() -> str:
        if broker_token_getter is not None:
            return broker_token_getter() or ""
        # Daemon updates local_state.broker_token at sign-in and clears
        # it on sign-out, so reading through that handle gives us the
        # live value without a refresh hook.
        return getattr(local_state, "broker_token", "") or ""

    async def _broker_request(
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: Optional[dict[str, Any]] = None,
    ) -> Response:
        token = _current_broker_token()
        if not token:
            raise HTTPException(status_code=503, detail="broker token unavailable")
        url = f"{broker_url.rstrip('/')}{path}"
        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                resp = await client.request(
                    method, url,
                    json=json_body, params=params,
                    headers={"Authorization": f"Bearer {token}"},
                )
            except httpx.HTTPError as exc:
                raise HTTPException(status_code=502, detail=f"broker unreachable: {exc}")
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type", "application/json"),
        )

    # ── Workflow CRUD ───────────────────────────────────────────────────

    @router.post("/api/workflows", dependencies=[Depends(require_local_token)])
    async def create_workflow(request: Request) -> Response:
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid JSON body")
        return await _broker_request("POST", "/workflows", json_body=body)

    @router.get("/api/workflows", dependencies=[Depends(require_local_token)])
    async def list_workflows() -> Response:
        return await _broker_request("GET", "/workflows")

    @router.get(
        "/api/workflows/{workflow_id}", dependencies=[Depends(require_local_token)]
    )
    async def get_workflow(workflow_id: UUID) -> Response:
        return await _broker_request("GET", f"/workflows/{workflow_id}")

    @router.put(
        "/api/workflows/{workflow_id}", dependencies=[Depends(require_local_token)]
    )
    async def update_workflow(workflow_id: UUID, request: Request) -> Response:
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid JSON body")
        return await _broker_request("PUT", f"/workflows/{workflow_id}", json_body=body)

    @router.delete(
        "/api/workflows/{workflow_id}", dependencies=[Depends(require_local_token)]
    )
    async def delete_workflow(workflow_id: UUID) -> Response:
        return await _broker_request("DELETE", f"/workflows/{workflow_id}")

    # ── Runs ────────────────────────────────────────────────────────────

    @router.get(
        "/api/workflows/{workflow_id}/runs",
        dependencies=[Depends(require_local_token)],
    )
    async def list_runs(workflow_id: UUID) -> Response:
        return await _broker_request("GET", f"/workflows/{workflow_id}/runs")

    @router.post(
        "/api/workflows/{workflow_id}/run",
        dependencies=[Depends(require_local_token)],
    )
    async def trigger_run(workflow_id: UUID, request: Request) -> Response:
        # Broker creates the run row + WS-pushes workflow_run_start back
        # to this daemon. We don't have to call engine.start_run here —
        # that happens in handle_broker_workflow_message below.
        try:
            body = await request.json()
        except ValueError:
            body = {}
        return await _broker_request(
            "POST", f"/workflows/{workflow_id}/run", json_body=body,
        )

    @router.get("/api/runs/{run_id}", dependencies=[Depends(require_local_token)])
    async def get_run(run_id: UUID) -> Response:
        return await _broker_request("GET", f"/runs/{run_id}")

    return router


async def handle_broker_workflow_message(engine, msg: dict) -> bool:
    """Route a {type:"workflow_run_start",...} broker WS frame to the engine.

    Returns True if the message was a workflow message (consumed),
    False if the caller should keep dispatching. Lets handle_broker do:

        if await handle_broker_workflow_message(engine, msg):
            continue

    without growing a giant elif chain.
    """
    if msg.get("type") != "workflow_run_start":
        return False
    try:
        run_id = UUID(msg["run_id"])
        workflow_id = UUID(msg["workflow_id"])
        definition = msg.get("definition") or {}
        input_ = msg.get("input") or {}
        user_id = msg.get("user_id") or ""
    except (KeyError, ValueError):
        logger.exception("malformed workflow_run_start message: %s", msg)
        return True  # consumed (but bad) — don't fall through
    await engine.start_run(run_id, workflow_id, definition, input_, user_id)
    return True
