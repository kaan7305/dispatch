"""Dispatch broker.

The single FastAPI service that:
  - issues JWT tokens via /auth/login (self-issued OAuth-shaped bearer)
  - accepts new dispatches from senders (POST /dispatch)
  - holds a WebSocket per recipient daemon (/agent/connect)
  - lets senders watch a dispatch over WebSocket (/dispatch/{id}/watch)
  - serves the sender web UI at /

This module is the only place that knows about HTTP, WebSockets, and
the dispatch routing topology. The executor is reused unchanged by the
recipient daemon process.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import timedelta
from pathlib import Path
from typing import Optional
from uuid import UUID

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.staticfiles import StaticFiles

from dispatch.broker.state import STATE, DispatchRecord, FriendRequest
from dispatch.shared.identity import IdentityError, issue_token, verify_token
from dispatch.shared.schema import (
    DispatchCreateRequest,
    DispatchEvent,
    DispatchPayload,
    DispatchStatus,
    LoginRequest,
    utcnow,
)

logger = logging.getLogger("dispatch.broker")

import sys as _sys
if getattr(_sys, "frozen", False):
    STATIC_DIR = Path(_sys._MEIPASS) / "dispatch" / "web" / "sender"
else:
    STATIC_DIR = Path(__file__).resolve().parent.parent / "web" / "sender"

app = FastAPI(title="Dispatch broker")


@app.on_event("startup")
async def _startup() -> None:
    try:
        from dispatch.shared.identity import _secret

        _secret()
    except IdentityError as e:
        logger.warning("%s", e)


# ----------------------------------------------------------------------------
# Auth helpers
# ----------------------------------------------------------------------------


def authed_user(authorization: Optional[str] = Header(None)) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    try:
        return verify_token(token)
    except IdentityError as e:
        raise HTTPException(status_code=401, detail=str(e))


def _verify_ws(token: Optional[str]) -> Optional[str]:
    if not token:
        return None
    try:
        return verify_token(token)
    except IdentityError:
        return None


# ----------------------------------------------------------------------------
# HTTP endpoints
# ----------------------------------------------------------------------------


@app.post("/auth/login")
async def login(req: LoginRequest) -> dict:
    user_id = req.username.strip()
    STATE.users.add(user_id)
    return {"user_id": user_id, "token": issue_token(user_id)}


@app.get("/me")
async def me(user_id: str = Depends(authed_user)) -> dict:
    return {"user_id": user_id}


@app.get("/users")
async def list_users(_: str = Depends(authed_user)) -> dict:
    return {"users": sorted(STATE.users)}


@app.post("/dispatch")
async def create_dispatch(
    req: DispatchCreateRequest, sender: str = Depends(authed_user)
) -> dict:
    payload = DispatchPayload(
        sender_id=sender,
        recipient_id=req.recipient_id,
        task=req.task,
        expires_at=utcnow() + timedelta(seconds=req.expires_in_seconds),
        metadata=req.metadata,
    )
    record = DispatchRecord(payload=payload)
    STATE.dispatches[payload.dispatch_id] = record
    STATE.users.add(req.recipient_id)

    agent_ws = STATE.agents.get(req.recipient_id)
    if agent_ws is not None:
        try:
            await agent_ws.send_text(
                json.dumps(
                    {
                        "type": "new_dispatch",
                        "payload": payload.model_dump(mode="json"),
                    }
                )
            )
            record.status = DispatchStatus.delivered
        except Exception:
            logger.exception("failed to push dispatch to recipient daemon")
            STATE.pending_for_offline[req.recipient_id].append(payload.dispatch_id)
    else:
        STATE.pending_for_offline[req.recipient_id].append(payload.dispatch_id)

    return {
        "dispatch_id": str(payload.dispatch_id),
        "status": record.status.value,
    }


@app.get("/dispatch/{dispatch_id}")
async def get_dispatch(
    dispatch_id: UUID, user_id: str = Depends(authed_user)
) -> dict:
    record = STATE.dispatches.get(dispatch_id)
    if not record:
        raise HTTPException(status_code=404, detail="Unknown dispatch_id")
    if record.payload.sender_id != user_id and record.payload.recipient_id != user_id:
        raise HTTPException(status_code=403, detail="Not your dispatch")
    return {
        "dispatch_id": str(dispatch_id),
        "sender_id": record.payload.sender_id,
        "recipient_id": record.payload.recipient_id,
        "task": record.payload.task,
        "status": record.status.value,
        "events": record.events,
    }


# ----------------------------------------------------------------------------
# WebSocket: recipient daemon connects
# ----------------------------------------------------------------------------


@app.websocket("/agent/connect")
async def agent_connect(ws: WebSocket, token: Optional[str] = Query(None)) -> None:
    user_id = _verify_ws(token)
    if user_id is None:
        await ws.close(code=1008)
        return

    await ws.accept()
    # Replace any prior connection for this user with the new one.
    prior = STATE.agents.get(user_id)
    if prior is not None and prior is not ws:
        try:
            await prior.close()
        except Exception:
            pass
    STATE.agents[user_id] = ws
    STATE.users.add(user_id)
    logger.info("daemon connected: user_id=%s", user_id)

    # Deliver any queued dispatches.
    queued = STATE.pending_for_offline.pop(user_id, [])
    for did in queued:
        record = STATE.dispatches.get(did)
        if record is None:
            continue
        try:
            await ws.send_text(
                json.dumps(
                    {
                        "type": "new_dispatch",
                        "payload": record.payload.model_dump(mode="json"),
                    }
                )
            )
            record.status = DispatchStatus.delivered
            await _broadcast_status(record)
        except Exception:
            logger.exception("failed delivering queued dispatch")

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            await _handle_agent_message(user_id, msg)
    except WebSocketDisconnect:
        logger.info("daemon disconnected: user_id=%s", user_id)
    except Exception:
        logger.exception("agent_connect crash")
    finally:
        if STATE.agents.get(user_id) is ws:
            del STATE.agents[user_id]


async def _handle_agent_message(user_id: str, msg: dict) -> None:
    mtype = msg.get("type")
    if mtype == "dispatch_event":
        await _record_event(msg)
    elif mtype == "dispatch_status":
        await _record_status(msg)


async def _record_event(msg: dict) -> None:
    raw_id = msg.get("dispatch_id")
    event = msg.get("event")
    if not raw_id or not isinstance(event, dict):
        return
    try:
        dispatch_id = UUID(raw_id)
    except ValueError:
        return
    record = STATE.dispatches.get(dispatch_id)
    if record is None:
        return
    record.events.append(event)
    await _broadcast(record, event)


async def _record_status(msg: dict) -> None:
    raw_id = msg.get("dispatch_id")
    new_status = msg.get("status")
    if not raw_id or not new_status:
        return
    try:
        dispatch_id = UUID(raw_id)
    except ValueError:
        return
    record = STATE.dispatches.get(dispatch_id)
    if record is None:
        return
    try:
        record.status = DispatchStatus(new_status)
    except ValueError:
        return
    await _broadcast_status(record)


# ----------------------------------------------------------------------------
# WebSocket: sender watches a dispatch
# ----------------------------------------------------------------------------


@app.websocket("/dispatch/{dispatch_id}/watch")
async def watch_dispatch(
    ws: WebSocket,
    dispatch_id: UUID,
    token: Optional[str] = Query(None),
) -> None:
    user_id = _verify_ws(token)
    if user_id is None:
        await ws.close(code=1008)
        return

    record = STATE.dispatches.get(dispatch_id)
    if record is None:
        await ws.accept()
        await ws.send_text(
            json.dumps(
                {
                    "type": "error",
                    "data": {
                        "message": "Unknown dispatch_id",
                        "exception": "NotFound",
                    },
                }
            )
        )
        await ws.close(code=1003)
        return

    if record.payload.sender_id != user_id:
        await ws.close(code=1008)
        return

    await ws.accept()
    record.watchers.append(ws)

    # Replay current state.
    try:
        await ws.send_text(
            json.dumps(
                {"type": "dispatch_status", "data": {"status": record.status.value}}
            )
        )
        for event in record.events:
            await ws.send_text(json.dumps(event))
    except Exception:
        logger.exception("watch replay failed")

    try:
        while True:
            # We don't expect inbound messages from the sender; this is just keepalive.
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("watch_dispatch crash")
    finally:
        try:
            record.watchers.remove(ws)
        except ValueError:
            pass


# ----------------------------------------------------------------------------
# Broadcast helpers
# ----------------------------------------------------------------------------


async def _broadcast(record: DispatchRecord, event: DispatchEvent) -> None:
    if not record.watchers:
        return
    payload = json.dumps(event)
    dead: list[WebSocket] = []
    for w in record.watchers:
        try:
            await w.send_text(payload)
        except Exception:
            dead.append(w)
    for w in dead:
        try:
            record.watchers.remove(w)
        except ValueError:
            pass


async def _broadcast_status(record: DispatchRecord) -> None:
    await _broadcast(
        record,
        {"type": "dispatch_status", "data": {"status": record.status.value}},
    )


# ----------------------------------------------------------------------------
# Friends
# ----------------------------------------------------------------------------


@app.post("/friends/request")
async def send_friend_request(
    body: dict, sender: str = Depends(authed_user)
) -> dict:
    to_user = (body.get("to_user_id") or "").strip()
    if not to_user:
        raise HTTPException(status_code=422, detail="to_user_id required")
    if to_user == sender:
        raise HTTPException(status_code=422, detail="Cannot add yourself")
    if to_user in STATE.friends[sender]:
        return {"status": "already_friends"}

    # Check for duplicate pending request.
    for req in STATE.friend_requests.values():
        if req.from_user == sender and req.to_user == to_user:
            return {"status": "already_requested", "request_id": req.request_id}

    req = FriendRequest(from_user=sender, to_user=to_user)
    STATE.friend_requests[req.request_id] = req
    STATE.users.add(to_user)

    # Push to recipient daemon if online.
    agent_ws = STATE.agents.get(to_user)
    if agent_ws:
        try:
            await agent_ws.send_text(
                json.dumps({"type": "friend_request", "request_id": req.request_id, "from_user": sender})
            )
        except Exception:
            logger.exception("failed to push friend request to daemon")

    return {"status": "sent", "request_id": req.request_id}


@app.get("/friends/requests")
async def list_friend_requests(user_id: str = Depends(authed_user)) -> dict:
    incoming = [
        {"request_id": r.request_id, "from_user": r.from_user, "created_at": r.created_at.isoformat()}
        for r in STATE.friend_requests.values()
        if r.to_user == user_id
    ]
    return {"requests": incoming}


@app.post("/friends/accept/{request_id}")
async def accept_friend_request(
    request_id: str, user_id: str = Depends(authed_user)
) -> dict:
    req = STATE.friend_requests.get(request_id)
    if not req or req.to_user != user_id:
        raise HTTPException(status_code=404, detail="Request not found")
    STATE.friends[req.from_user].add(req.to_user)
    STATE.friends[req.to_user].add(req.from_user)
    del STATE.friend_requests[request_id]
    return {"status": "accepted", "friend": req.from_user}


@app.post("/friends/decline/{request_id}")
async def decline_friend_request(
    request_id: str, user_id: str = Depends(authed_user)
) -> dict:
    req = STATE.friend_requests.get(request_id)
    if not req or req.to_user != user_id:
        raise HTTPException(status_code=404, detail="Request not found")
    del STATE.friend_requests[request_id]
    return {"status": "declined"}


@app.get("/friends")
async def list_friends(user_id: str = Depends(authed_user)) -> dict:
    return {"friends": sorted(STATE.friends[user_id])}


# Static mount goes last so it doesn't shadow the routes above.
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
