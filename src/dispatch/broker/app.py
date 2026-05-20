"""Dispatch broker.

The single FastAPI service that:
  - issues JWT tokens via /auth/login (broker-issued bearer; OAuth-shaped)
  - accepts new dispatches from senders (POST /dispatch)
  - holds a WebSocket per recipient daemon (/agent/connect)
  - lets senders watch a dispatch over WebSocket (/dispatch/{id}/watch)
  - lets clients list their dispatch history (GET /dispatches)
  - serves the sender web UI at /

This module is the only place that knows about HTTP, WebSockets, and
the dispatch routing topology. The executor and the store are reused
unchanged by other components.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import sys as _sys
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Literal, Optional
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
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from dispatch.broker.email import send_magic_link
from dispatch.broker.state import STATE, FriendRequest
from dispatch.broker.store import STORE, StoredDispatch
from dispatch.shared.identity import IdentityError, issue_token, verify_token
from dispatch.shared.schema import (
    DispatchCreateRequest,
    DispatchEvent,
    DispatchPayload,
    DispatchStatus,
    LoginRequest,
    MagicLinkRequest,
    utcnow,
)

MAGIC_LINK_TTL_MINUTES = 15

logger = logging.getLogger("dispatch.broker")

if getattr(_sys, "frozen", False):
    STATIC_DIR = Path(_sys._MEIPASS) / "dispatch" / "web" / "sender"
else:
    STATIC_DIR = Path(__file__).resolve().parent.parent / "web" / "sender"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fail fast if JWT secret missing — every login depends on it.
    try:
        from dispatch.shared.identity import _secret

        _secret()
    except IdentityError as e:
        logger.warning("%s", e)
    await STORE.init()
    try:
        yield
    finally:
        await STORE.close()


app = FastAPI(title="Dispatch broker", lifespan=lifespan)


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
    """Dev-mode / CLI login. The web UI uses the magic-link flow below."""
    user_id = req.username.strip()
    await STORE.upsert_user(user_id)
    return {"user_id": user_id, "token": issue_token(user_id)}


def _public_url() -> str:
    return os.environ.get("DISPATCH_PUBLIC_URL", "http://localhost:8000").rstrip("/")


def _normalize_email(raw: str) -> Optional[str]:
    email = raw.strip().lower()
    if "@" not in email or " " in email or len(email) > 254:
        return None
    return email


@app.post("/auth/request")
async def auth_request(req: MagicLinkRequest) -> dict:
    """Send a one-time magic-link email. If RESEND_API_KEY isn't set, the
    link comes back in the response body (development convenience)."""
    email = _normalize_email(req.email)
    if not email:
        raise HTTPException(status_code=400, detail="Invalid email")

    token = secrets.token_urlsafe(32)
    expires_at = utcnow() + timedelta(minutes=MAGIC_LINK_TTL_MINUTES)
    await STORE.create_magic_link(token, email, expires_at)

    link = f"{_public_url()}/auth/magic?token={token}"
    result = await send_magic_link(email, link)

    body: dict = {"status": "sent", "delivered": result.delivered, "email": email}
    if not result.delivered and result.dev_link:
        body["dev_link"] = result.dev_link
    return body


@app.get("/auth/magic")
async def auth_magic(token: str):
    """User clicks the magic link → exchange token for a JWT, then redirect
    to the SPA with the JWT in the query string. The SPA picks it up,
    stores it in localStorage, and strips the query string."""
    email = await STORE.consume_magic_link(token)
    if not email:
        return RedirectResponse(url="/?auth_error=invalid_or_expired", status_code=302)
    await STORE.upsert_user(email)
    jwt_token = issue_token(email)
    # We pass the JWT back via the redirect query string. The frontend
    # extracts and removes it from the URL on load.
    return RedirectResponse(
        url=f"/?login_token={jwt_token}&user_id={email}", status_code=302
    )


@app.get("/health")
async def health() -> dict:
    """Liveness + DB readiness check. Railway hits this on every deploy."""
    db_ok = True
    try:
        async with STORE.pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
    except Exception:
        db_ok = False
    return {"status": "ok" if db_ok else "degraded", "database": "up" if db_ok else "down"}


@app.get("/me")
async def me(user_id: str = Depends(authed_user)) -> dict:
    return {"user_id": user_id}


@app.get("/users")
async def list_users(_: str = Depends(authed_user)) -> dict:
    return {"users": await STORE.list_users()}


def _payload_summary(payload: DispatchPayload, status: DispatchStatus) -> dict:
    return {
        "dispatch_id": str(payload.dispatch_id),
        "sender_id": payload.sender_id,
        "recipient_id": payload.recipient_id,
        "task": payload.task,
        "status": status.value,
        "created_at": payload.created_at.isoformat(),
        "expires_at": payload.expires_at.isoformat(),
    }


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
    initial_status = DispatchStatus.pending
    await STORE.upsert_user(req.recipient_id)
    await STORE.create_dispatch(payload, initial_status)

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
            await STORE.update_status(payload.dispatch_id, DispatchStatus.delivered)
        except Exception:
            logger.exception("failed to push dispatch to recipient daemon")
            await STORE.enqueue_for_offline(req.recipient_id, payload.dispatch_id)
    else:
        await STORE.enqueue_for_offline(req.recipient_id, payload.dispatch_id)

    current = await STORE.get_dispatch(payload.dispatch_id)
    final_status = current.status if current else initial_status

    # Tell the recipient's inbox watchers immediately, so the UI lights up
    # before the daemon (re)connects.
    inbox_watchers = STATE.recipient_watchers.get(req.recipient_id, [])
    if inbox_watchers:
        await _fan_out(
            inbox_watchers,
            json.dumps({"type": "inbox_new", "data": _payload_summary(payload, final_status)}),
        )

    return {
        "dispatch_id": str(payload.dispatch_id),
        "status": final_status.value,
    }


@app.get("/dispatch/{dispatch_id}")
async def get_dispatch(
    dispatch_id: UUID, user_id: str = Depends(authed_user)
) -> dict:
    stored = await STORE.get_dispatch(dispatch_id)
    if not stored:
        raise HTTPException(status_code=404, detail="Unknown dispatch_id")
    if stored.payload.sender_id != user_id and stored.payload.recipient_id != user_id:
        raise HTTPException(status_code=403, detail="Not your dispatch")
    events = await STORE.get_events(dispatch_id)
    return {
        "dispatch_id": str(dispatch_id),
        "sender_id": stored.payload.sender_id,
        "recipient_id": stored.payload.recipient_id,
        "task": stored.payload.task,
        "status": stored.status.value,
        "created_at": stored.payload.created_at.isoformat(),
        "expires_at": stored.payload.expires_at.isoformat(),
        "events": events,
    }


@app.get("/dispatches")
async def list_dispatches(
    role: Literal["sent", "received"] = Query(...),
    user_id: str = Depends(authed_user),
) -> dict:
    stored = await STORE.list_dispatches_for_user(user_id, role)
    return {
        "role": role,
        "dispatches": [_dispatch_summary(s) for s in stored],
    }


def _dispatch_summary(s: StoredDispatch) -> dict:
    p = s.payload
    return {
        "dispatch_id": str(p.dispatch_id),
        "sender_id": p.sender_id,
        "recipient_id": p.recipient_id,
        "task": p.task,
        "status": s.status.value,
        "created_at": p.created_at.isoformat(),
        "expires_at": p.expires_at.isoformat(),
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
    prior = STATE.agents.get(user_id)
    if prior is not None and prior is not ws:
        try:
            await prior.close()
        except Exception:
            pass
    STATE.agents[user_id] = ws
    await STORE.upsert_user(user_id)
    logger.info("daemon connected: user_id=%s", user_id)

    # Deliver any queued dispatches.
    queued = await STORE.pop_offline_queue(user_id)
    for did in queued:
        stored = await STORE.get_dispatch(did)
        if stored is None:
            continue
        try:
            await ws.send_text(
                json.dumps(
                    {
                        "type": "new_dispatch",
                        "payload": stored.payload.model_dump(mode="json"),
                    }
                )
            )
            await STORE.update_status(did, DispatchStatus.delivered)
            await _broadcast_status(did, stored.payload.recipient_id, DispatchStatus.delivered)
        except Exception:
            logger.exception("failed delivering queued dispatch")

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            await _handle_agent_message(msg)
    except WebSocketDisconnect:
        logger.info("daemon disconnected: user_id=%s", user_id)
    except Exception:
        logger.exception("agent_connect crash")
    finally:
        if STATE.agents.get(user_id) is ws:
            del STATE.agents[user_id]


async def _handle_agent_message(msg: dict) -> None:
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
    stored = await STORE.get_dispatch(dispatch_id)
    if stored is None:
        return
    await STORE.append_event(dispatch_id, event)  # type: ignore[arg-type]
    await _broadcast_event(dispatch_id, stored.payload.recipient_id, event)  # type: ignore[arg-type]


async def _record_status(msg: dict) -> None:
    raw_id = msg.get("dispatch_id")
    new_status = msg.get("status")
    if not raw_id or not new_status:
        return
    try:
        dispatch_id = UUID(raw_id)
        status = DispatchStatus(new_status)
    except ValueError:
        return
    stored = await STORE.get_dispatch(dispatch_id)
    if stored is None:
        return
    await STORE.update_status(dispatch_id, status)
    await _broadcast_status(dispatch_id, stored.payload.recipient_id, status)


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

    stored = await STORE.get_dispatch(dispatch_id)
    if stored is None:
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

    if stored.payload.sender_id != user_id and stored.payload.recipient_id != user_id:
        await ws.close(code=1008)
        return

    await ws.accept()
    STATE.watchers.setdefault(dispatch_id, []).append(ws)

    # Replay current state from the store.
    try:
        await ws.send_text(
            json.dumps(
                {"type": "dispatch_status", "data": {"status": stored.status.value}}
            )
        )
        for event in await STORE.get_events(dispatch_id):
            await ws.send_text(json.dumps(event))
    except Exception:
        logger.exception("watch replay failed")

    try:
        while True:
            await ws.receive_text()  # keepalive; ignored
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("watch_dispatch crash")
    finally:
        watchers = STATE.watchers.get(dispatch_id, [])
        try:
            watchers.remove(ws)
        except ValueError:
            pass
        if not watchers:
            STATE.watchers.pop(dispatch_id, None)


# ----------------------------------------------------------------------------
# WebSocket: recipient's inbox + consent UI
# ----------------------------------------------------------------------------


@app.websocket("/inbox")
async def inbox(ws: WebSocket, token: Optional[str] = Query(None)) -> None:
    """One WebSocket per recipient browser tab.

    Server → client messages:
      {type: "inbox_new",         data: {dispatch_summary}}        -- a new dispatch landed
      {dispatch_id, type, data}                                   -- a per-dispatch event/status
    Client → server messages (forwarded verbatim to the daemon):
      {type: "dispatch_decision", dispatch_id, decision}          -- accept|reject
      {type: "tool_consent",      dispatch_id, request_id, decision}  -- allow|deny
    """
    user_id = _verify_ws(token)
    if user_id is None:
        await ws.close(code=1008)
        return

    await ws.accept()
    STATE.recipient_watchers.setdefault(user_id, []).append(ws)
    await STORE.upsert_user(user_id)

    # Snapshot: every dispatch already addressed to this user, with current
    # status + replay of past events.
    try:
        dispatches = await STORE.list_dispatches_for_user(user_id, "received")
        for stored in dispatches:
            await ws.send_text(
                json.dumps(
                    {"type": "inbox_new", "data": _payload_summary(stored.payload, stored.status)}
                )
            )
            for event in await STORE.get_events(stored.payload.dispatch_id):
                await ws.send_text(
                    json.dumps({"dispatch_id": str(stored.payload.dispatch_id), **event})
                )
    except Exception:
        logger.exception("inbox snapshot failed")

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            await _handle_inbox_message(user_id, msg)
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("inbox WS crash")
    finally:
        watchers = STATE.recipient_watchers.get(user_id, [])
        try:
            watchers.remove(ws)
        except ValueError:
            pass
        if not watchers:
            STATE.recipient_watchers.pop(user_id, None)


async def _handle_inbox_message(user_id: str, msg: dict) -> None:
    """Forward a consent decision from the recipient's browser to the daemon.

    Only forward if (a) the dispatch belongs to this user, and (b) the daemon
    is connected. If the daemon is offline the decision is silently dropped
    — the daemon will time out on its end and the user can re-decide once
    they restart it.
    """
    mtype = msg.get("type")
    if mtype not in ("dispatch_decision", "tool_consent"):
        return
    raw_id = msg.get("dispatch_id")
    if not raw_id:
        return
    try:
        dispatch_id = UUID(raw_id)
    except ValueError:
        return
    stored = await STORE.get_dispatch(dispatch_id)
    if stored is None or stored.payload.recipient_id != user_id:
        return  # not their dispatch

    daemon_ws = STATE.agents.get(user_id)
    if daemon_ws is None:
        logger.warning("consent dropped: daemon offline for %s", user_id)
        return
    try:
        await daemon_ws.send_text(json.dumps(msg))
    except Exception:
        logger.exception("failed to forward consent to daemon")


# ----------------------------------------------------------------------------
# Broadcast helpers
# ----------------------------------------------------------------------------


async def _fan_out(sockets: list[WebSocket], payload: str) -> None:
    """Best-effort send to every socket, pruning dead ones in place."""
    dead: list[WebSocket] = []
    for w in sockets:
        try:
            await w.send_text(payload)
        except Exception:
            dead.append(w)
    for w in dead:
        try:
            sockets.remove(w)
        except ValueError:
            pass


async def _broadcast_event(
    dispatch_id: UUID, recipient_id: str, event: DispatchEvent
) -> None:
    payload = json.dumps({"dispatch_id": str(dispatch_id), **event})
    raw_event = json.dumps(event)
    # Per-dispatch watchers (sender's "watch this dispatch" tab) get the raw event.
    watchers = STATE.watchers.get(dispatch_id, [])
    if watchers:
        await _fan_out(watchers, raw_event)
    # Inbox watchers (recipient's "everything for me" tab) get the dispatch_id-tagged event.
    inbox = STATE.recipient_watchers.get(recipient_id, [])
    if inbox:
        await _fan_out(inbox, payload)


async def _broadcast_status(
    dispatch_id: UUID, recipient_id: str, status: DispatchStatus
) -> None:
    await _broadcast_event(
        dispatch_id,
        recipient_id,
        {"type": "dispatch_status", "data": {"status": status.value}},
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
    await STORE.upsert_user(to_user)

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
