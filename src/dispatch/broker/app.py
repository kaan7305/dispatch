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

import asyncio
import json
import logging
import os
import secrets
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
from pydantic import BaseModel
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from dispatch.broker.clerk import ClerkAuthError, extract_email, verify_clerk_token
from dispatch.broker.email import send_invitation
from dispatch.broker.state import STATE
from dispatch.broker.store import STORE, StoredDispatch
from dispatch.shared import crypto
from dispatch.shared.identity import IdentityError, issue_token, verify_token
from dispatch.shared.schema import (
    AcceptInvitationRequest,
    ClerkExchangeRequest,
    DeviceEnrollRequest,
    DispatchCreateRequest,
    DispatchEvent,
    DispatchPayload,
    DispatchStatus,
    InvitationCreateRequest,
    LoginRequest,
    Scopes,
    TrustScopesUpdate,
    utcnow,
)

INVITATION_TTL_DAYS = 7

SIGN_TIMEOUT_S = 20.0

logger = logging.getLogger("dispatch.broker")

# request_id → Future resolved with the base64 signature returned by a
# sender's daemon in response to a sign_request.
_pending_signatures: dict[str, asyncio.Future] = {}

STATIC_DIR = Path(__file__).resolve().parent.parent / "web" / "app"


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


@app.post("/auth/signout")
async def auth_signout(user_id: str = Depends(authed_user)) -> dict:
    """Sign-out hook for the broker SPA.

    Notifies every one of this user's connected daemons over the WebSocket
    so their tray apps clear their cached JWT and stop reconnecting. The
    daemon's own JWT isn't revoked server-side (HS256 + no DB blocklist),
    so this is best-effort: any daemon NOT currently connected won't get
    the signal until it reconnects (and at that point its JWT still works
    until natural expiry).
    """
    devices = STATE.agents.get(user_id, {})
    delivered = 0
    msg = json.dumps({"type": "signed_out"})
    for device_id, ws in list(devices.items()):
        try:
            await ws.send_text(msg)
            delivered += 1
        except Exception:
            logger.exception("failed to notify daemon of sign-out")
    return {"status": "ok", "notified": delivered}


def _public_url() -> str:
    """The broker's externally-reachable base URL.

    Used to build magic-link emails and the /install.sh one-liner.
    Priority: explicit DISPATCH_PUBLIC_URL → Railway's auto-injected
    RAILWAY_PUBLIC_DOMAIN → localhost.
    """
    explicit = os.environ.get("DISPATCH_PUBLIC_URL")
    if explicit:
        return explicit.rstrip("/")
    railway = os.environ.get("RAILWAY_PUBLIC_DOMAIN")
    if railway:
        return f"https://{railway}"
    return "http://localhost:8000"


def _normalize_email(raw: str) -> Optional[str]:
    email = raw.strip().lower()
    if "@" not in email or " " in email or len(email) > 254:
        return None
    return email


@app.post("/auth/clerk")
async def auth_clerk(req: ClerkExchangeRequest) -> dict:
    """Exchange a Clerk session JWT for a Dispatch JWT.

    Browser flow: Clerk handles Google sign-in client-side, the SPA grabs
    the session token via Clerk.session.getToken(), POSTs it here. The
    broker verifies it against Clerk's JWKS, pulls the verified email out
    of the claims, upserts the user, and returns a long-lived Dispatch
    JWT — the same shape the daemon's install command bakes in."""
    try:
        claims = verify_clerk_token(req.clerk_token)
        email = extract_email(claims)
    except ClerkAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))

    normalized = _normalize_email(email)
    if not normalized:
        raise HTTPException(status_code=400, detail="Clerk returned an invalid email")
    await STORE.upsert_user(normalized)
    return {"user_id": normalized, "token": issue_token(normalized)}


@app.get("/config.js")
async def config_js() -> PlainTextResponse:
    """Tiny script the SPA loads before app.js so it knows which Clerk
    instance to talk to. Read from env so secrets/URLs stay out of the
    static assets."""
    publishable = os.environ.get("CLERK_PUBLISHABLE_KEY", "")
    frontend_api = (
        os.environ.get("CLERK_FRONTEND_API", "")
        .strip()
        .rstrip("/")
        .removeprefix("https://")
        .removeprefix("http://")
    )
    template = os.environ.get("CLERK_JWT_TEMPLATE", "dispatch")
    body = (
        "window.DISPATCH_CONFIG = "
        + json.dumps({
            "clerk_publishable_key": publishable,
            "clerk_frontend_api": frontend_api,
            "clerk_jwt_template": template,
        })
        + ";"
    )
    return PlainTextResponse(content=body, media_type="application/javascript")


def _daemon_install_spec() -> str:
    """What `pipx install` should be pointed at to get the daemon.

    Default assumes a public GitHub repo. Override DISPATCH_DAEMON_INSTALL
    with anything pip understands (a different repo, a wheel URL, etc.).
    """
    return os.environ.get(
        "DISPATCH_DAEMON_INSTALL",
        "git+https://github.com/your-org/dispatch.git",
    )


@app.get("/install.sh")
async def install_script() -> PlainTextResponse:
    """One-shot recipient installer.

    Usage on the recipient's machine:
        curl -fsSL <broker>/install.sh | bash -s -- <jwt>

    It installs pipx (if needed), installs the daemon, saves broker+token
    to ~/.dispatch/config.json, and starts the daemon. Subsequent runs are
    just `dispatch-daemon`.
    """
    broker = _public_url()
    spec = _daemon_install_spec()
    script = f"""#!/usr/bin/env bash
set -e

BROKER="{broker}"
INSTALL_SPEC="{spec}"
TOKEN="${{1:-$DISPATCH_TOKEN}}"

if [ -z "$TOKEN" ]; then
  echo "dispatch: no token supplied." >&2
  echo "  usage: curl -fsSL $BROKER/install.sh | bash -s -- <your-token>" >&2
  exit 1
fi

echo "dispatch: installing the recipient daemon..."

# 1. Ensure pipx is available.
if ! command -v pipx >/dev/null 2>&1; then
  if command -v brew >/dev/null 2>&1; then
    brew install pipx
  else
    python3 -m pip install --user pipx
  fi
fi
if command -v pipx >/dev/null 2>&1; then PIPX="pipx"; else PIPX="python3 -m pipx"; fi
$PIPX ensurepath >/dev/null 2>&1 || true

# 2. Install (or upgrade) the daemon.
$PIPX install --force "$INSTALL_SPEC"

# 3. Save broker + token so future runs are just `dispatch-daemon`.
mkdir -p "$HOME/.dispatch"
umask 077
cat > "$HOME/.dispatch/config.json" <<EOF
{{"broker": "$BROKER", "token": "$TOKEN"}}
EOF

# 4. Start it. If the installer's shell has an ANTHROPIC_API_KEY exported,
#    pass it through so the daemon persists it to ~/.dispatch/config.json.
DAEMON="$(command -v dispatch-daemon || echo "$HOME/.local/bin/dispatch-daemon")"
echo "dispatch: installed. starting daemon (next time, just run: dispatch-daemon)"
if [ -n "${{ANTHROPIC_API_KEY:-}}" ]; then
  exec "$DAEMON" --anthropic-key "$ANTHROPIC_API_KEY"
else
  exec "$DAEMON"
fi
"""
    return PlainTextResponse(content=script, media_type="text/x-shellscript")


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


async def _request_signature(
    sender_ws: WebSocket,
    *,
    instruction: str,
    sender_device: str,
    recipient_user: str,
    nonce: str,
    created_at: str,
) -> bytes:
    """Ask the sender's daemon to sign the canonical dispatch payload.

    The broker never holds a signing key — it only relays the fields and
    receives the signature. Returns the raw signature bytes; raises on
    timeout or daemon failure.
    """
    request_id = secrets.token_urlsafe(16)
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    _pending_signatures[request_id] = fut
    try:
        await sender_ws.send_text(
            json.dumps(
                {
                    "type": "sign_request",
                    "request_id": request_id,
                    "instruction": instruction,
                    "sender_device": sender_device,
                    "recipient_user": recipient_user,
                    "target_device": None,
                    "nonce": nonce,
                    "created_at": created_at,
                }
            )
        )
        signature_b64 = await asyncio.wait_for(fut, timeout=SIGN_TIMEOUT_S)
    finally:
        _pending_signatures.pop(request_id, None)
    return crypto.b64decode(signature_b64)


async def _build_new_dispatch(stored: StoredDispatch) -> dict:
    """The new_dispatch message pushed to a recipient daemon, carrying the
    signing block the daemon needs to verify the dispatch (Layer 2)."""
    msg: dict = {
        "type": "new_dispatch",
        "payload": stored.payload.model_dump(mode="json"),
    }
    if stored.sender_device and stored.signature and stored.nonce:
        pubkey = await STORE.get_device_public_key(stored.sender_device)
        msg["signing"] = {
            "sender_device": str(stored.sender_device),
            "target_device": None,
            "nonce": stored.nonce,
            "created_at": stored.payload.created_at.isoformat(),
            "signature": crypto.b64encode(stored.signature),
            "sender_public_key": crypto.b64encode(pubkey) if pubkey else None,
        }
    # The trust edge's scopes — the daemon constrains the agent to these.
    if stored.trust_link_id:
        link = await STORE.get_trust_link(stored.trust_link_id)
        if link:
            msg["scopes"] = link["scopes"] or {}
    return msg


@app.post("/dispatch")
async def create_dispatch(
    req: DispatchCreateRequest, sender: str = Depends(authed_user)
) -> dict:
    recipient = req.recipient_id.strip()

    # --- Layer 1: trust enforcement ---------------------------------------
    # A dispatch may only be created if the recipient has accepted an
    # invitation from the sender (an accepted trust edge sender → recipient).
    edge = await STORE.get_trust_edge(sender, recipient)
    if edge is None:
        raise HTTPException(
            status_code=403,
            detail=(
                f"No trust relationship: {recipient} has not accepted an "
                "invitation from you. Invite them from Contacts first."
            ),
        )
    edge_scopes = Scopes(**(edge["scopes"] or {}))

    # Edge expiry — an expired edge is treated as no trust.
    if edge_scopes.expires_at and edge_scopes.expires_at <= utcnow():
        raise HTTPException(
            status_code=403, detail="This trust relationship has expired."
        )

    # Per-edge daily rate limit.
    recent = await STORE.count_recent_dispatches(
        edge["trust_link_id"], utcnow() - timedelta(days=1)
    )
    if recent >= edge_scopes.max_dispatches_per_day:
        raise HTTPException(
            status_code=429,
            detail=(
                f"Daily dispatch limit ({edge_scopes.max_dispatches_per_day}) "
                f"reached for {recipient}."
            ),
        )
    # ----------------------------------------------------------------------

    payload = DispatchPayload(
        sender_id=sender,
        recipient_id=recipient,
        task=req.task,
        expires_at=utcnow() + timedelta(seconds=req.expires_in_seconds),
        metadata=req.metadata,
    )

    # --- Layer 2 (sender half): get the sender's daemon to sign -----------
    # Signing happens on the sender's device, never here. If no device of
    # the sender is online there is nothing that can sign, so the dispatch
    # cannot be created.
    picked = STATE.pick_device(sender)
    if picked is None:
        raise HTTPException(
            status_code=503,
            detail="Your daemon is offline — start dispatch-daemon to send.",
        )
    sender_device_id, sender_ws = picked
    nonce = secrets.token_urlsafe(16)
    try:
        signature = await _request_signature(
            sender_ws,
            instruction=payload.task,
            sender_device=sender_device_id,
            recipient_user=recipient,
            nonce=nonce,
            created_at=payload.created_at.isoformat(),
        )
    except Exception as exc:
        logger.warning("signature request failed: %s", exc)
        raise HTTPException(
            status_code=502,
            detail="Your daemon did not sign the dispatch — is it healthy?",
        )
    # ----------------------------------------------------------------------

    initial_status = DispatchStatus.pending
    await STORE.create_dispatch(
        payload,
        initial_status,
        trust_link_id=edge["trust_link_id"],
        sender_device=UUID(sender_device_id),
        nonce=nonce,
        signature=signature,
    )

    stored = await STORE.get_dispatch(payload.dispatch_id)
    new_dispatch_msg = json.dumps(await _build_new_dispatch(stored))

    agent_ws = STATE.pick_device_ws(recipient)
    if agent_ws is not None:
        try:
            await agent_ws.send_text(new_dispatch_msg)
            await STORE.update_status(payload.dispatch_id, DispatchStatus.delivered)
        except Exception:
            logger.exception("failed to push dispatch to recipient daemon")
            await STORE.enqueue_for_offline(recipient, payload.dispatch_id)
    else:
        await STORE.enqueue_for_offline(recipient, payload.dispatch_id)

    current = await STORE.get_dispatch(payload.dispatch_id)
    final_status = current.status if current else initial_status

    # Tell the recipient's inbox watchers immediately, so the UI lights up
    # before the daemon (re)connects.
    inbox_watchers = STATE.recipient_watchers.get(recipient, [])
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
# Devices
# ----------------------------------------------------------------------------


@app.post("/devices/enroll")
async def enroll_device(
    req: DeviceEnrollRequest, user_id: str = Depends(authed_user)
) -> dict:
    """A daemon registers a machine: its label + Ed25519 public key. The
    private key never leaves the device. Idempotent on the public key."""
    try:
        public_key = crypto.b64decode(req.public_key)
    except Exception:
        raise HTTPException(status_code=400, detail="public_key must be base64")
    if len(public_key) != crypto.PUBLIC_KEY_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"public_key must be {crypto.PUBLIC_KEY_BYTES} bytes",
        )
    await STORE.upsert_user(user_id)
    device_id = await STORE.enroll_device(user_id, req.label.strip(), public_key)
    return {"device_id": str(device_id)}


@app.get("/devices")
async def list_devices(user_id: str = Depends(authed_user)) -> dict:
    devices = await STORE.list_devices(user_id)
    online = STATE.agents.get(user_id, {})
    return {
        "devices": [
            {
                "device_id": str(d["device_id"]),
                "label": d["label"],
                "status": d["status"],
                "online": str(d["device_id"]) in online,
                "last_seen": d["last_seen"].isoformat() if d["last_seen"] else None,
                "created_at": d["created_at"].isoformat(),
            }
            for d in devices
        ]
    }


class _DeviceRename(BaseModel):
    label: str

@app.patch("/devices/{device_id}")
async def rename_device(
    device_id: UUID,
    req: _DeviceRename,
    user_id: str = Depends(authed_user),
) -> dict:
    label = req.label.strip()
    if not label:
        raise HTTPException(status_code=400, detail="label is required")
    if not await STORE.rename_device(user_id, device_id, label):
        raise HTTPException(status_code=404, detail="Unknown device")
    return {"status": "renamed"}


@app.delete("/devices/{device_id}")
async def revoke_device(
    device_id: UUID, user_id: str = Depends(authed_user)
) -> dict:
    revoked = await STORE.revoke_device(user_id, device_id)
    if not revoked:
        raise HTTPException(status_code=404, detail="Unknown device")
    # Drop its live connection if any — a revoked device must stop receiving.
    ws = STATE.agents.get(user_id, {}).pop(str(device_id), None)
    if ws is not None:
        try:
            await ws.close(code=1008)
        except Exception:
            pass
    return {"status": "revoked"}


# ----------------------------------------------------------------------------
# Invitations & trust
# ----------------------------------------------------------------------------


@app.post("/invitations")
async def create_invitation(
    req: InvitationCreateRequest, user_id: str = Depends(authed_user)
) -> dict:
    to_email = _normalize_email(req.to_email)
    if not to_email:
        raise HTTPException(status_code=400, detail="Invalid email")
    if to_email == user_id.lower():
        raise HTTPException(status_code=400, detail="You can't invite yourself")

    token = secrets.token_urlsafe(32)
    expires_at = utcnow() + timedelta(days=INVITATION_TTL_DAYS)
    await STORE.create_invitation(user_id, to_email, token, expires_at)

    link = f"{_public_url()}/invite/{token}"
    result = await send_invitation(to_email, user_id, link)
    body: dict = {"status": "sent", "delivered": result.delivered, "to_email": to_email}
    if not result.delivered and result.dev_link:
        body["dev_link"] = result.dev_link
    return body


@app.get("/invitations")
async def list_invitations(user_id: str = Depends(authed_user)) -> dict:
    sent, received = await STORE.list_invitations(user_id)

    def _fmt(rows: list[dict]) -> list[dict]:
        return [
            {
                "invitation_id": str(r["invitation_id"]),
                "from_user": r["from_user"],
                "to_email": r["to_email"],
                "token": r["token"],
                "status": r["status"],
                "created_at": r["created_at"].isoformat(),
            }
            for r in rows
        ]

    return {"sent": _fmt(sent), "received": _fmt(received)}


@app.get("/invitations/{token}")
async def get_invitation(token: str) -> dict:
    """Invite details for the acceptance UI. The token itself is the
    capability — whoever holds it was the one emailed it."""
    inv = await STORE.get_invitation_by_token(token)
    if inv is None:
        raise HTTPException(status_code=404, detail="Unknown invitation")
    return {
        "from_user": inv["from_user"],
        "to_email": inv["to_email"],
        "status": inv["status"],
        "expired": inv["expires_at"] <= utcnow(),
    }


@app.get("/invite/{token}")
async def invite_landing(token: str):
    """Email link target. Bounce into the SPA, which handles login plus
    the accept/decline UI."""
    return RedirectResponse(url=f"/?invite={token}", status_code=302)


@app.post("/invitations/{token}/accept")
async def accept_invitation(
    token: str,
    req: AcceptInvitationRequest,
    user_id: str = Depends(authed_user),
) -> dict:
    scopes = (req.scopes or Scopes()).model_dump(mode="json")
    trust_link_id, error = await STORE.accept_invitation(token, user_id, scopes)
    if error:
        status = {
            "not_found": 404,
            "already_resolved": 409,
            "expired": 410,
            "wrong_recipient": 403,
        }.get(error, 400)
        raise HTTPException(status_code=status, detail=error)
    return {"status": "accepted", "trust_link_id": str(trust_link_id)}


@app.post("/invitations/{token}/decline")
async def decline_invitation(
    token: str, _: str = Depends(authed_user)
) -> dict:
    if not await STORE.decline_invitation(token):
        raise HTTPException(status_code=404, detail="Unknown or already-resolved invitation")
    return {"status": "declined"}


@app.get("/trust")
async def list_trust(user_id: str = Depends(authed_user)) -> dict:
    """My contacts: accepted edges in both directions, with the peer's
    daemon presence and whether I'm allowed to edit the scopes."""
    links = await STORE.list_trust_links(user_id)
    out = []
    for tl in links:
        outgoing = tl["from_user"] == user_id
        peer = tl["to_user"] if outgoing else tl["from_user"]
        out.append(
            {
                "trust_link_id": str(tl["trust_link_id"]),
                "from_user": tl["from_user"],
                "to_user": tl["to_user"],
                "direction": "outgoing" if outgoing else "incoming",
                "peer": peer,
                "scopes": tl["scopes"],
                "peer_online": bool(STATE.agents.get(peer)),
                "can_edit_scopes": tl["to_user"] == user_id,
            }
        )
    return {"trust": out}


@app.patch("/trust/{trust_link_id}")
async def update_trust(
    trust_link_id: UUID,
    req: TrustScopesUpdate,
    user_id: str = Depends(authed_user),
) -> dict:
    ok = await STORE.update_trust_scopes(
        trust_link_id, user_id, req.scopes.model_dump(mode="json")
    )
    if not ok:
        raise HTTPException(
            status_code=403,
            detail="Only the trustor (the recipient) may edit this edge's scopes",
        )
    return {"status": "updated"}


async def _cancel_one(dispatch_id: UUID, recipient_id: str) -> None:
    """Mark a single dispatch cancelled, notify watchers, tell the
    recipient's daemon to stop the agent task."""
    await STORE.update_status(dispatch_id, DispatchStatus.cancelled)
    await _broadcast_status(dispatch_id, recipient_id, DispatchStatus.cancelled)
    agent_ws = STATE.pick_device_ws(recipient_id)
    if agent_ws is not None:
        try:
            await agent_ws.send_text(
                json.dumps({"type": "cancel_dispatch", "dispatch_id": str(dispatch_id)})
            )
        except Exception:
            logger.exception("failed to push cancel_dispatch")


@app.post("/dispatch/{dispatch_id}/cancel")
async def cancel_dispatch(
    dispatch_id: UUID, user_id: str = Depends(authed_user)
) -> dict:
    """Either party can cancel an in-flight dispatch. No-op once it's
    already in a terminal state."""
    stored = await STORE.get_dispatch(dispatch_id)
    if stored is None:
        raise HTTPException(status_code=404, detail="Unknown dispatch_id")
    if stored.payload.sender_id != user_id and stored.payload.recipient_id != user_id:
        raise HTTPException(status_code=403, detail="Not your dispatch")
    if stored.status in (
        DispatchStatus.completed,
        DispatchStatus.failed,
        DispatchStatus.denied,
        DispatchStatus.expired,
        DispatchStatus.cancelled,
    ):
        return {"status": "noop", "current_status": stored.status.value}
    await _cancel_one(dispatch_id, stored.payload.recipient_id)
    return {"status": "cancelled"}


async def _cancel_inflight(trust_link_id: UUID) -> int:
    """Cancel every in-flight dispatch on a revoked edge: mark it
    cancelled, tell watchers, and tell the recipient's daemon to stop."""
    inflight = await STORE.list_inflight_dispatches(trust_link_id)
    for d in inflight:
        dispatch_id = d["dispatch_id"]
        recipient_id = d["recipient_id"]
        await STORE.update_status(dispatch_id, DispatchStatus.cancelled)
        await _broadcast_status(dispatch_id, recipient_id, DispatchStatus.cancelled)
        agent_ws = STATE.pick_device_ws(recipient_id)
        if agent_ws is not None:
            try:
                await agent_ws.send_text(
                    json.dumps(
                        {"type": "cancel_dispatch", "dispatch_id": str(dispatch_id)}
                    )
                )
            except Exception:
                logger.exception("failed to push cancel_dispatch")
    return len(inflight)


@app.delete("/trust/{trust_link_id}")
async def revoke_trust(
    trust_link_id: UUID, user_id: str = Depends(authed_user)
) -> dict:
    if not await STORE.revoke_trust_link(trust_link_id, user_id):
        raise HTTPException(status_code=404, detail="Unknown trust link")
    # Revoking the edge also cancels anything in flight on it, and
    # POST /dispatch already refuses new dispatches (no accepted edge).
    cancelled = await _cancel_inflight(trust_link_id)
    return {"status": "revoked", "cancelled_dispatches": cancelled}


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

    # First frame must identify the device: {"type":"hello","device_id":...}.
    try:
        hello = json.loads(await ws.receive_text())
    except (WebSocketDisconnect, json.JSONDecodeError):
        await ws.close(code=1003)
        return
    raw_device_id = hello.get("device_id")
    if hello.get("type") != "hello" or not raw_device_id:
        await ws.send_text(
            json.dumps(
                {
                    "type": "error",
                    "data": {"message": "first frame must be a device hello",
                             "exception": "ProtocolError"},
                }
            )
        )
        await ws.close(code=1003)
        return
    try:
        device_uuid = UUID(raw_device_id)
    except ValueError:
        await ws.close(code=1003)
        return

    device = await STORE.get_device_for_user(device_uuid, user_id)
    if device is None or device["status"] != "active":
        await ws.send_text(
            json.dumps(
                {
                    "type": "error",
                    "data": {"message": "unknown or revoked device",
                             "exception": "Forbidden"},
                }
            )
        )
        await ws.close(code=1008)
        return

    device_id = str(device_uuid)
    user_devices = STATE.agents.setdefault(user_id, {})
    prior = user_devices.get(device_id)
    if prior is not None and prior is not ws:
        try:
            await prior.close()
        except Exception:
            pass
    user_devices[device_id] = ws
    await STORE.touch_device_last_seen(device_uuid)
    logger.info("daemon connected: user_id=%s device=%s", user_id, device_id[:8])

    # Deliver any queued dispatches to this freshly-connected device.
    queued = await STORE.pop_offline_queue(user_id)
    for did in queued:
        stored = await STORE.get_dispatch(did)
        if stored is None:
            continue
        try:
            await ws.send_text(json.dumps(await _build_new_dispatch(stored)))
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
        logger.info("daemon disconnected: user_id=%s device=%s", user_id, device_id[:8])
    except Exception:
        logger.exception("agent_connect crash")
    finally:
        user_devices = STATE.agents.get(user_id)
        if user_devices is not None and user_devices.get(device_id) is ws:
            del user_devices[device_id]
            if not user_devices:
                STATE.agents.pop(user_id, None)


async def _handle_agent_message(msg: dict) -> None:
    mtype = msg.get("type")
    if mtype == "dispatch_event":
        await _record_event(msg)
    elif mtype == "dispatch_status":
        await _record_status(msg)
    elif mtype == "signed":
        # A sender's daemon answering a sign_request.
        request_id = msg.get("request_id")
        signature = msg.get("signature")
        fut = _pending_signatures.get(request_id)
        if fut is not None and not fut.done() and signature:
            fut.set_result(signature)


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
# WebSocket: recipient's inbox + approval UI
# ----------------------------------------------------------------------------


@app.websocket("/inbox")
async def inbox(ws: WebSocket, token: Optional[str] = Query(None)) -> None:
    """One WebSocket per recipient browser tab.

    Server → client messages:
      {type: "inbox_new",         data: {dispatch_summary}}        -- a new dispatch landed
      {dispatch_id, type, data}                                   -- a per-dispatch event/status
    Client → server messages (forwarded verbatim to the daemon):
      {type: "dispatch_decision", dispatch_id, decision}          -- accept|reject
      {type: "tool_approval",      dispatch_id, request_id, decision}  -- allow|deny
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
    """Forward a approval decision from the recipient's browser to the daemon.

    Only forward if (a) the dispatch belongs to this user, and (b) the daemon
    is connected. If the daemon is offline the decision is silently dropped
    — the daemon will time out on its end and the user can re-decide once
    they restart it.
    """
    mtype = msg.get("type")
    if mtype not in ("dispatch_decision", "tool_approval"):
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

    daemon_ws = STATE.pick_device_ws(user_id)
    if daemon_ws is None:
        logger.warning("approval dropped: daemon offline for %s", user_id)
        return
    try:
        await daemon_ws.send_text(json.dumps(msg))
    except Exception:
        logger.exception("failed to forward approval to daemon")


# Static mount last so it doesn't shadow the routes above.
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
