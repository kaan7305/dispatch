"""Local FastAPI app the daemon serves on 127.0.0.1.

Purpose: the recipient's browser approves dispatches against this server,
not against the broker, so the broker can never fabricate or auto-approve
a "user clicked Accept" message.

State split:
  * DaemonState  — futures the running dispatch coroutine awaits
                  (resolved here when the user clicks).
  * LocalState   — the inbox the SPA renders (snapshots + live WS feed).

The daemon process passes both into make_app() and pushes lifecycle
events into LocalState via the on_* hooks below.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys as _sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from uuid import UUID

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from dispatch.shared.schema import DispatchEvent, DispatchPayload, DispatchStatus

logger = logging.getLogger("dispatch.daemon.local")


# When running as a PyInstaller-bundled .app, static files live under _MEIPASS.
if getattr(_sys, "frozen", False):
    STATIC_DIR = Path(_sys._MEIPASS) / "dispatch" / "web" / "recipient"  # type: ignore[attr-defined]
else:
    STATIC_DIR = Path(__file__).resolve().parent.parent / "web" / "recipient"


@dataclass
class InboxEntry:
    payload: DispatchPayload
    scopes: dict
    status: DispatchStatus
    events: list[DispatchEvent] = field(default_factory=list)
    pending_tools: dict[str, dict] = field(default_factory=dict)  # request_id → {tool, input}


@dataclass
class LocalState:
    """Inbox state shown to the recipient. Mutated by the daemon process,
    read by the local FastAPI handlers.

    Hook methods (on_*) are called from the daemon's event loop. They MUST
    be invoked from the same event loop the FastAPI app runs on, so the
    queued notifications can be scheduled directly.
    """
    user_id: str = ""
    broker_url: str = ""
    entries: dict[UUID, InboxEntry] = field(default_factory=dict)
    watchers: list[WebSocket] = field(default_factory=list)

    def on_new_dispatch(self, payload: DispatchPayload, scopes: dict | None) -> None:
        entry = InboxEntry(
            payload=payload,
            scopes=scopes or {},
            status=DispatchStatus.delivered,
        )
        self.entries[payload.dispatch_id] = entry
        self._broadcast({"type": "inbox_new", "data": _entry_summary(entry)})

    def on_status(self, dispatch_id: UUID, status: DispatchStatus) -> None:
        entry = self.entries.get(dispatch_id)
        if entry is None:
            return
        entry.status = status
        self._broadcast({
            "type": "dispatch_status",
            "dispatch_id": str(dispatch_id),
            "data": {"status": status.value},
        })

    def on_event(self, dispatch_id: UUID, event: DispatchEvent) -> None:
        entry = self.entries.get(dispatch_id)
        if entry is None:
            return
        entry.events.append(event)
        self._broadcast({
            "type": "dispatch_event",
            "dispatch_id": str(dispatch_id),
            "data": event,
        })

    def on_pending_tool(
        self, dispatch_id: UUID, request_id: str, tool: str, tool_input: dict
    ) -> None:
        entry = self.entries.get(dispatch_id)
        if entry is None:
            return
        entry.pending_tools[request_id] = {"tool": tool, "input": tool_input}

    def on_tool_resolved(self, dispatch_id: UUID, request_id: str) -> None:
        entry = self.entries.get(dispatch_id)
        if entry is not None:
            entry.pending_tools.pop(request_id, None)

    def _broadcast(self, message: dict) -> None:
        if not self.watchers:
            return
        encoded = json.dumps(message, default=str)
        for ws in list(self.watchers):
            try:
                asyncio.create_task(ws.send_text(encoded))
            except Exception:
                try:
                    self.watchers.remove(ws)
                except ValueError:
                    pass


def _entry_summary(entry: InboxEntry) -> dict:
    p = entry.payload
    return {
        "dispatch_id": str(p.dispatch_id),
        "sender_id": p.sender_id,
        "task": p.task,
        "created_at": p.created_at.isoformat(),
        "expires_at": p.expires_at.isoformat(),
        "status": entry.status.value,
        "scopes": entry.scopes,
        "pending_tools": entry.pending_tools,
    }


class _Decision(BaseModel):
    decision: str


def make_app(local_state: LocalState, daemon_state) -> FastAPI:
    """Build the local FastAPI app.

    daemon_state is the DaemonState from dispatch.daemon.main; we resolve
    its futures when the user clicks Accept/Allow/etc. Not type-annotated
    here to avoid an import cycle.
    """
    app = FastAPI(title="Dispatch (local)")

    @app.get("/api/session")
    async def session() -> dict:
        return {
            "user_id": local_state.user_id,
            "broker_url": local_state.broker_url,
        }

    @app.get("/api/inbox")
    async def inbox() -> list[dict]:
        return [_entry_summary(e) for e in local_state.entries.values()]

    @app.get("/api/dispatch/{dispatch_id}")
    async def dispatch_detail(dispatch_id: UUID) -> dict:
        entry = local_state.entries.get(dispatch_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="unknown dispatch")
        return {**_entry_summary(entry), "events": entry.events}

    @app.post("/api/dispatch/{dispatch_id}/decision")
    async def dispatch_decision(dispatch_id: UUID, body: _Decision) -> dict:
        if body.decision not in ("accept", "reject"):
            raise HTTPException(status_code=400, detail="decision must be accept|reject")
        fut = daemon_state.pending_decisions.get(str(dispatch_id))
        if fut is None or fut.done():
            raise HTTPException(status_code=409, detail="no pending decision for that dispatch")
        fut.set_result(body.decision)
        return {"status": "ok"}

    @app.post("/api/dispatch/{dispatch_id}/tool/{request_id}/decision")
    async def tool_decision(
        dispatch_id: UUID, request_id: str, body: _Decision
    ) -> dict:
        if body.decision not in ("allow", "deny"):
            raise HTTPException(status_code=400, detail="decision must be allow|deny")
        fut = daemon_state.pending_approvals.get((str(dispatch_id), request_id))
        if fut is None or fut.done():
            raise HTTPException(status_code=409, detail="no pending approval for that tool call")
        fut.set_result(body.decision)
        return {"status": "ok"}

    @app.websocket("/ws/events")
    async def ws_events(ws: WebSocket) -> None:
        await ws.accept()
        local_state.watchers.append(ws)
        try:
            # Snapshot on connect so the SPA starts populated.
            await ws.send_text(json.dumps({
                "type": "snapshot",
                "data": [_entry_summary(e) for e in local_state.entries.values()],
            }))
            while True:
                # Keep the socket alive; ignore inbound messages.
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            try:
                local_state.watchers.remove(ws)
            except ValueError:
                pass

    if STATIC_DIR.exists():
        app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
    return app


async def serve(
    local_state: LocalState, daemon_state, host: str = "127.0.0.1", port: int = 8001
) -> None:
    """Run the local FastAPI app in the current event loop. Returns the
    asyncio Task so the caller can cancel it on shutdown."""
    import uvicorn
    app = make_app(local_state, daemon_state)
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()


def spawn(
    local_state: LocalState, daemon_state, host: str = "127.0.0.1", port: int = 8001
) -> asyncio.Task:
    """Fire-and-forget version: schedules serve() on the running loop."""
    return asyncio.create_task(serve(local_state, daemon_state, host=host, port=port))
