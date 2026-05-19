"""Local FastAPI app hosted by the recipient daemon.

The recipient opens this in their browser to see incoming dispatches,
press Accept / Reject on the whole task, and Allow / Deny on individual
destructive tool calls. Talks to the daemon process via in-memory
futures — no network between the daemon and its local UI.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from uuid import UUID

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from dispatch.shared.schema import DispatchEvent, DispatchPayload, DispatchStatus

logger = logging.getLogger("dispatch.daemon.local")

STATIC_DIR = Path(__file__).resolve().parent.parent / "web" / "recipient"


@dataclass
class DispatchEntry:
    payload: DispatchPayload
    status: DispatchStatus = DispatchStatus.delivered
    events: list[DispatchEvent] = field(default_factory=list)
    decision: Optional[asyncio.Future] = None
    pending_tool_consents: dict[str, asyncio.Future] = field(default_factory=dict)
    watchers: list[WebSocket] = field(default_factory=list)


@dataclass
class LocalState:
    user_id: str = ""
    dispatches: dict[UUID, DispatchEntry] = field(default_factory=dict)
    inbox_watchers: list[WebSocket] = field(default_factory=list)

    # ----- mutation API used by daemon process -----

    def add_dispatch(self, payload: DispatchPayload) -> asyncio.Future:
        loop = asyncio.get_running_loop()
        decision = loop.create_future()
        entry = DispatchEntry(payload=payload, decision=decision)
        self.dispatches[payload.dispatch_id] = entry
        asyncio.create_task(self._notify_inbox(self._summary(entry)))
        return decision

    def mark_status(self, dispatch_id: UUID, status: DispatchStatus) -> None:
        entry = self.dispatches.get(dispatch_id)
        if entry is None:
            return
        entry.status = status
        asyncio.create_task(self._notify_inbox(self._summary(entry)))
        asyncio.create_task(
            self._notify_watchers(
                dispatch_id,
                {"type": "dispatch_status", "data": {"status": status.value}},
            )
        )

    def record_event(self, dispatch_id: UUID, event: DispatchEvent) -> None:
        entry = self.dispatches.get(dispatch_id)
        if entry is None:
            return
        entry.events.append(event)
        asyncio.create_task(self._notify_watchers(dispatch_id, event))

    async def request_tool_consent(
        self, dispatch_id: UUID, tool_name: str, tool_input: dict
    ) -> tuple[str, str]:
        """Returns (request_id, decision)."""
        entry = self.dispatches.get(dispatch_id)
        if entry is None:
            return ("", "deny")
        loop = asyncio.get_running_loop()
        request_id = str(uuid.uuid4())
        fut: asyncio.Future = loop.create_future()
        entry.pending_tool_consents[request_id] = fut
        event: DispatchEvent = {
            "type": "permission_request",
            "data": {"id": request_id, "tool": tool_name, "input": tool_input},
        }
        entry.events.append(event)
        await self._notify_watchers(dispatch_id, event)
        try:
            decision = await asyncio.wait_for(fut, timeout=120.0)
        except asyncio.TimeoutError:
            decision = "deny"
        finally:
            entry.pending_tool_consents.pop(request_id, None)
        return request_id, decision

    # ----- resolvers called by the local web UI -----

    def resolve_tool_consent(
        self, dispatch_id: UUID, request_id: str, decision: str
    ) -> None:
        entry = self.dispatches.get(dispatch_id)
        if entry is None:
            return
        fut = entry.pending_tool_consents.get(request_id)
        if fut and not fut.done() and decision in ("allow", "deny"):
            fut.set_result(decision)

    def resolve_dispatch_decision(self, dispatch_id: UUID, decision: str) -> None:
        entry = self.dispatches.get(dispatch_id)
        if entry is None or entry.decision is None:
            return
        if not entry.decision.done() and decision in ("accept", "reject"):
            entry.decision.set_result(decision)

    # ----- helpers -----

    def _summary(self, entry: DispatchEntry) -> dict:
        p = entry.payload
        return {
            "type": "inbox_update",
            "data": {
                "dispatch_id": str(p.dispatch_id),
                "sender_id": p.sender_id,
                "task": p.task,
                "created_at": p.created_at.isoformat(),
                "expires_at": p.expires_at.isoformat(),
                "status": entry.status.value,
            },
        }

    async def _notify_inbox(self, message: dict) -> None:
        if not self.inbox_watchers:
            return
        encoded = json.dumps(message)
        dead = []
        for ws in self.inbox_watchers:
            try:
                await ws.send_text(encoded)
            except Exception:
                dead.append(ws)
        for ws in dead:
            try:
                self.inbox_watchers.remove(ws)
            except ValueError:
                pass

    async def _notify_watchers(self, dispatch_id: UUID, event: dict) -> None:
        entry = self.dispatches.get(dispatch_id)
        if entry is None or not entry.watchers:
            return
        encoded = json.dumps(event)
        dead = []
        for ws in entry.watchers:
            try:
                await ws.send_text(encoded)
            except Exception:
                dead.append(ws)
        for ws in dead:
            try:
                entry.watchers.remove(ws)
            except ValueError:
                pass


def make_app(state: LocalState) -> FastAPI:
    app = FastAPI(title="Dispatch recipient (local UI)")

    @app.get("/api/me")
    async def me() -> dict:
        return {"user_id": state.user_id}

    @app.get("/api/inbox")
    async def inbox() -> list[dict]:
        return [state._summary(e)["data"] for e in state.dispatches.values()]

    @app.get("/api/dispatch/{dispatch_id}")
    async def dispatch_detail(dispatch_id: UUID) -> dict:
        entry = state.dispatches.get(dispatch_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="not found")
        return {
            **state._summary(entry)["data"],
            "events": entry.events,
        }

    @app.websocket("/ws/inbox")
    async def ws_inbox(ws: WebSocket) -> None:
        await ws.accept()
        state.inbox_watchers.append(ws)
        # Snapshot.
        for entry in state.dispatches.values():
            await ws.send_text(json.dumps(state._summary(entry)))
        try:
            while True:
                raw = await ws.receive_text()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if msg.get("type") == "dispatch_decision":
                    try:
                        did = UUID(msg["dispatch_id"])
                    except (KeyError, ValueError):
                        continue
                    state.resolve_dispatch_decision(did, msg.get("decision", ""))
        except WebSocketDisconnect:
            pass
        finally:
            try:
                state.inbox_watchers.remove(ws)
            except ValueError:
                pass

    @app.websocket("/ws/dispatch/{dispatch_id}")
    async def ws_dispatch(ws: WebSocket, dispatch_id: UUID) -> None:
        await ws.accept()
        entry = state.dispatches.get(dispatch_id)
        if entry is None:
            await ws.send_text(
                json.dumps(
                    {
                        "type": "error",
                        "data": {"message": "unknown dispatch", "exception": "NotFound"},
                    }
                )
            )
            await ws.close(code=1003)
            return
        entry.watchers.append(ws)
        # Replay status + events.
        try:
            await ws.send_text(
                json.dumps(
                    {"type": "dispatch_status", "data": {"status": entry.status.value}}
                )
            )
            for event in entry.events:
                await ws.send_text(json.dumps(event))
        except Exception:
            logger.exception("replay failed")
        try:
            while True:
                raw = await ws.receive_text()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if msg.get("type") == "permission_response":
                    state.resolve_tool_consent(
                        dispatch_id, msg.get("id", ""), msg.get("decision", "")
                    )
        except WebSocketDisconnect:
            pass
        finally:
            try:
                entry.watchers.remove(ws)
            except ValueError:
                pass

    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
    return app
