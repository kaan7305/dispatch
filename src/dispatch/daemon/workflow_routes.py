"""HTTP routes for the daemon's workflows feature.

Mirrors the proxy pattern in local_app.py: the SPA hits /api/workflows*
on 127.0.0.1 with the local bearer, the daemon forwards each request to
the broker with the broker JWT attached. The browser never holds the
broker token.

The workflow engine lives in dispatch.daemon.workflows and is invoked
from process_dispatch on the RECIPIENT side when a dispatch arrives
with metadata.workflow set — there is no broker→daemon WS frame for
workflow execution anymore.
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
        # Broker creates one dispatch + one run row per recipient. The
        # SPA gets back the list; the recipient daemons receive the
        # dispatches and run the engine locally on Accept.
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

    @router.post("/api/runs/{run_id}/cancel", dependencies=[Depends(require_local_token)])
    async def cancel_run(run_id: UUID) -> dict:
        # Cancellation is local on the recipient side where the engine
        # task lives. We also PATCH the broker so the row reflects it
        # for the workflow owner even if the recipient daemon drops.
        was_running = engine.cancel(run_id)
        try:
            await _broker_request(
                "PATCH", f"/runs/{run_id}",
                json_body={"status": "cancelled", "ended": True},
            )
        except Exception:
            pass
        return {"status": "cancelled" if was_running else "noop"}

    # ── Context packs (reusable system_prompt + files bundles) ──────────

    @router.get("/api/contexts", dependencies=[Depends(require_local_token)])
    async def list_contexts() -> Response:
        return await _broker_request("GET", "/contexts")

    @router.post("/api/contexts", dependencies=[Depends(require_local_token)])
    async def create_context(request: Request) -> Response:
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid JSON body")
        return await _broker_request("POST", "/contexts", json_body=body)

    @router.get(
        "/api/contexts/{context_id}",
        dependencies=[Depends(require_local_token)],
    )
    async def get_context(context_id: UUID) -> Response:
        return await _broker_request("GET", f"/contexts/{context_id}")

    @router.put(
        "/api/contexts/{context_id}",
        dependencies=[Depends(require_local_token)],
    )
    async def update_context(context_id: UUID, request: Request) -> Response:
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid JSON body")
        return await _broker_request(
            "PUT", f"/contexts/{context_id}", json_body=body,
        )

    @router.delete(
        "/api/contexts/{context_id}",
        dependencies=[Depends(require_local_token)],
    )
    async def delete_context(context_id: UUID) -> Response:
        return await _broker_request("DELETE", f"/contexts/{context_id}")

    return router
