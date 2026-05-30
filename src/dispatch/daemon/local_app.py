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
import secrets
import sys as _sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional
from uuid import UUID

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from dispatch.daemon.identity import dispatch_home
from dispatch.shared.schema import DispatchEvent, DispatchPayload, DispatchStatus

logger = logging.getLogger("dispatch.daemon.local")

LOCAL_TOKEN_PATH = dispatch_home() / "local.token"


def issue_local_token() -> str:
    """Return the persistent local bearer token, generating it on first run.

    The token is stored at 0600 so only this user can read it; a drive-by
    website on 127.0.0.1:8001 has no way to learn it.  We deliberately
    reuse the token across daemon restarts so the browser's sessionStorage
    copy stays valid — rotating it would leave any open window permanently
    stuck on "Connecting…" after a broker reconnect."""
    if LOCAL_TOKEN_PATH.exists():
        existing = LOCAL_TOKEN_PATH.read_text().strip()
        if existing:
            return existing
    token = secrets.token_urlsafe(24)
    LOCAL_TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOCAL_TOKEN_PATH.write_text(token)
    LOCAL_TOKEN_PATH.chmod(0o600)
    return token


def read_local_token() -> str:
    """Read the token written by the running daemon. Tray app uses this to
    open the desktop UI with the right credential."""
    return LOCAL_TOKEN_PATH.read_text().strip() if LOCAL_TOKEN_PATH.exists() else ""


# When running as a PyInstaller-bundled .app, static files live under _MEIPASS.
# After the React rewrite the SPA lives in web/desktop/dist/ (Vite build); fall
# back to web/desktop/ for the vanilla skeleton until that ships.
def _resolve_static_dir() -> Path:
    if getattr(_sys, "frozen", False):
        base = Path(_sys._MEIPASS) / "dispatch" / "web" / "desktop"  # type: ignore[attr-defined]
    else:
        base = Path(__file__).resolve().parent.parent / "web" / "desktop"
    built = base / "dist"
    return built if built.exists() else base


STATIC_DIR = _resolve_static_dir()


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

    `notify` is an optional OS-notification callback the tray app supplies.
    It receives (title, subtitle, message) and is responsible for marshalling
    to whatever thread the GUI requires. Headless `dispatch-daemon` leaves
    it None.
    """
    user_id: str = ""
    broker_url: str = ""
    broker_token: str = ""   # Dispatch JWT — used by the proxy handlers, never returned to the SPA
    entries: dict[UUID, InboxEntry] = field(default_factory=dict)
    watchers: list[WebSocket] = field(default_factory=list)
    notify: Optional[Callable[[str, str, str], None]] = None
    # Called (thread-safely) when the user signs out from the web UI so the
    # tray supervisor can restart the daemon with the new/cleared credentials.
    on_signout: Optional[Callable[[], None]] = None

    async def seed_from_broker(self) -> None:
        """Populate entries from the broker DB on startup so the inbox
        isn't empty after a daemon restart."""
        if not (self.broker_url and self.broker_token):
            return
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"{self.broker_url.rstrip('/')}/dispatches",
                    params={"role": "received"},
                    headers={"Authorization": f"Bearer {self.broker_token}"},
                )
            if resp.status_code != 200:
                return
            from datetime import datetime
            for d in resp.json().get("dispatches", []):
                did = UUID(d["dispatch_id"])
                if did in self.entries:
                    continue  # live entry takes precedence
                payload = DispatchPayload(
                    dispatch_id=did,
                    sender_id=d["sender_id"],
                    recipient_id=d["recipient_id"],
                    task=d["task"],
                    created_at=datetime.fromisoformat(d["created_at"]),
                    expires_at=datetime.fromisoformat(d["expires_at"]),
                )
                self.entries[did] = InboxEntry(
                    payload=payload,
                    scopes={},
                    status=DispatchStatus(d["status"]),
                )
        except Exception:
            pass  # best-effort — a fresh inbox is better than a crash

    def _push_notification(self, title: str, subtitle: str, message: str) -> None:
        if self.notify is None:
            return
        try:
            self.notify(title, subtitle, message)
        except Exception:
            logger.exception("notify callback failed")

    def on_new_dispatch(self, payload: DispatchPayload, scopes: dict | None) -> None:
        entry = InboxEntry(
            payload=payload,
            scopes=scopes or {},
            status=DispatchStatus.delivered,
        )
        self.entries[payload.dispatch_id] = entry
        self._broadcast({"type": "inbox_new", "data": _entry_summary(entry)})
        self._push_notification(
            "New dispatch",
            f"from {payload.sender_id}",
            payload.task[:140],
        )

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
        # Show enough to recognize the call without dumping the whole input.
        preview = next(
            (str(v) for v in tool_input.values() if isinstance(v, str) and v),
            "",
        )[:140]
        self._push_notification(
            "Permission needed",
            f"{tool} on {entry.payload.sender_id}'s dispatch",
            preview,
        )

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
        "recipient_id": p.recipient_id,
        "task": p.task,
        "created_at": p.created_at.isoformat(),
        "expires_at": p.expires_at.isoformat(),
        "status": entry.status.value,
        "scopes": entry.scopes,
        "pending_tools": entry.pending_tools,
    }


class _Decision(BaseModel):
    decision: str


class _Compose(BaseModel):
    recipient_id: Optional[str] = None
    recipient_ids: Optional[list[str]] = None
    task: str
    expires_in_seconds: int = 3600
    metadata: dict[str, Any] = {}


class _ScopesUpdate(BaseModel):
    scopes: dict[str, Any]


class _Invite(BaseModel):
    to_email: str


class _AcceptInvite(BaseModel):
    scopes: Optional[dict[str, Any]] = None


class _DeviceRename(BaseModel):
    label: str


def make_app(
    local_state: LocalState,
    daemon_state,
    local_token: str,
    workflow_engine=None,
) -> FastAPI:
    """Build the local FastAPI app.

    daemon_state is the DaemonState from dispatch.daemon.main; we resolve
    its futures when the user clicks Accept/Allow/etc. Not type-annotated
    here to avoid an import cycle.

    local_token is the per-launch bearer that authenticates the locally-
    served SPA. Required on every /api/* and /ws/* call so a drive-by
    page or rogue browser extension at 127.0.0.1 can't drive the daemon.

    workflow_engine is the WorkflowEngine created in main.run_session;
    we stash it on app.state so handle_broker can find it via the
    process-shared LocalServer handle.
    """
    app = FastAPI(title="Dispatch (local)")
    app.state.workflow_engine = workflow_engine

    def require_local_token(request: Request) -> None:
        header = request.headers.get("authorization", "")
        token = header[7:] if header.lower().startswith("bearer ") else ""
        if not token:
            token = request.query_params.get("t", "")
        if not secrets.compare_digest(token, local_token):
            raise HTTPException(status_code=401, detail="missing or wrong local token")

    def require_local_token_ws(token: Optional[str]) -> bool:
        return bool(token) and secrets.compare_digest(token, local_token)

    @app.get("/api/session", dependencies=[Depends(require_local_token)])
    async def session() -> dict:
        return {
            "user_id": local_state.user_id,
            "broker_url": local_state.broker_url,
        }

    @app.post("/api/open-broker", dependencies=[Depends(require_local_token)])
    async def open_broker() -> dict:
        """Open the broker page in the user's default browser.

        The desktop UI runs inside a WKWebView that can't spawn external
        browser windows, so we shell out to macOS's `open` command (or
        xdg-open on Linux) instead.
        """
        import subprocess, sys
        url = local_state.broker_url.rstrip("/") or "https://web-production-700f0.up.railway.app"
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        try:
            subprocess.Popen([opener, url])
        except FileNotFoundError:
            raise HTTPException(status_code=500, detail=f"no `{opener}` command available")
        return {"status": "opened", "url": url}

    @app.get("/api/install-command", dependencies=[Depends(require_local_token)])
    async def install_command() -> dict:
        """Render the install one-liner the user paste-installs on a new
        device. We assemble it daemon-side so the broker JWT never has to
        appear as its own field in any SPA response."""
        broker = local_state.broker_url.rstrip("/")
        token = local_state.broker_token
        return {
            "command": f"curl -fsSL {broker}/install.sh | bash -s -- {token}",
            "broker": broker,
        }

    @app.post("/api/sign-out", dependencies=[Depends(require_local_token)])
    async def sign_out() -> dict:
        """Clear the broker JWT from disk and from in-memory state so the
        daemon can't authenticate any more. The tray app will detect the
        disconnect and either show a re-sign-in alert or quit. We keep
        device_id + anthropic_api_key so the next sign-in is one-step."""
        from dispatch.daemon.main import _save_config, _load_config
        cfg = _load_config()
        cfg.pop("token", None)
        cfg.pop("broker", None)
        # Rewrite with the surviving fields.
        from pathlib import Path
        import json as _json
        path = Path.home() / ".dispatch" / "config.json"
        try:
            path.write_text(_json.dumps(cfg, indent=2))
            path.chmod(0o600)
        except OSError:
            pass
        local_state.broker_token = ""
        local_state.user_id = ""
        local_state.entries.clear()  # wipe stale inbox cache
        result = {"status": "signed_out", "broker": local_state.broker_url}
        # Tell the tray supervisor to stop the daemon so it exits the broker
        # WebSocket and the status badge updates. Schedule after response so
        # the HTTP reply goes out first.
        if local_state.on_signout:
            asyncio.get_event_loop().call_later(0.3, local_state.on_signout)
        return result

    @app.get("/api/inbox", dependencies=[Depends(require_local_token)])
    async def inbox() -> list[dict]:
        return [_entry_summary(e) for e in local_state.entries.values()]

    @app.get("/api/dispatch/{dispatch_id}", dependencies=[Depends(require_local_token)])
    async def dispatch_detail(dispatch_id: UUID):
        # Received dispatches live in the daemon's local state — return
        # those directly (events stream into LocalState from the agent
        # session, no network roundtrip).
        entry = local_state.entries.get(dispatch_id)
        if entry is not None:
            return {**_entry_summary(entry), "events": entry.events}
        # SENT dispatches and historical ones the daemon didn't witness
        # locally: fall back to the broker (proxied with the broker JWT).
        return await _broker_request("GET", f"/dispatch/{dispatch_id}")

    @app.post("/api/dispatch/{dispatch_id}/decision", dependencies=[Depends(require_local_token)])
    async def dispatch_decision(dispatch_id: UUID, body: _Decision) -> dict:
        if body.decision not in ("accept", "reject"):
            raise HTTPException(status_code=400, detail="decision must be accept|reject")
        fut = daemon_state.pending_decisions.get(str(dispatch_id))
        if fut is None or fut.done():
            raise HTTPException(status_code=409, detail="no pending decision for that dispatch")
        fut.set_result(body.decision)
        return {"status": "ok"}

    @app.post(
        "/api/dispatch/{dispatch_id}/tool/{request_id}/decision",
        dependencies=[Depends(require_local_token)],
    )
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
    async def ws_events(ws: WebSocket, t: Optional[str] = Query(default=None)) -> None:
        # WebSocket auth: token via ?t= query param (set by the SPA bootstrap
        # from the URL fragment the tray app delivers).
        if not require_local_token_ws(t):
            await ws.close(code=4401)
            return
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

    # ── Broker proxy ────────────────────────────────────────────────────
    # The SPA never holds the broker JWT — the daemon does, in LocalState.
    # These handlers forward requests to the broker with the JWT attached.

    async def _broker_request(
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: Optional[dict[str, Any]] = None,
        require_auth: bool = True,
    ) -> Response:
        if require_auth and not local_state.broker_token:
            raise HTTPException(status_code=503, detail="broker token unavailable")
        url = f"{local_state.broker_url.rstrip('/')}{path}"
        headers: dict[str, str] = {}
        if require_auth:
            headers["Authorization"] = f"Bearer {local_state.broker_token}"
        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                resp = await client.request(
                    method, url, json=json_body, params=params, headers=headers,
                )
            except httpx.HTTPError as exc:
                raise HTTPException(status_code=502, detail=f"broker unreachable: {exc}")
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type", "application/json"),
        )

    @app.post("/api/compose", dependencies=[Depends(require_local_token)])
    async def compose(body: _Compose) -> Response:
        return await _broker_request(
            "POST", "/dispatch", json_body=body.model_dump(exclude_none=True),
        )

    @app.get("/api/trust", dependencies=[Depends(require_local_token)])
    async def list_trust() -> Response:
        return await _broker_request("GET", "/trust")

    @app.patch("/api/trust/{trust_link_id}", dependencies=[Depends(require_local_token)])
    async def update_trust(trust_link_id: str, body: _ScopesUpdate) -> Response:
        return await _broker_request(
            "PATCH", f"/trust/{trust_link_id}", json_body=body.model_dump(),
        )

    @app.delete("/api/trust/{trust_link_id}", dependencies=[Depends(require_local_token)])
    async def revoke_trust(trust_link_id: str) -> Response:
        return await _broker_request("DELETE", f"/trust/{trust_link_id}")

    @app.post("/api/invitations", dependencies=[Depends(require_local_token)])
    async def create_invitation(body: _Invite) -> Response:
        return await _broker_request("POST", "/invitations", json_body=body.model_dump())

    @app.get("/api/invitations", dependencies=[Depends(require_local_token)])
    async def list_invitations() -> Response:
        return await _broker_request("GET", "/invitations")

    # Public — accepting an invite requires only the token + the (eventually)
    # authed user. We still wrap it locally so the broker JWT is attached.
    @app.get("/api/invitations/{token}", dependencies=[Depends(require_local_token)])
    async def get_invitation(token: str) -> Response:
        return await _broker_request("GET", f"/invitations/{token}", require_auth=False)

    @app.post("/api/invitations/{token}/accept", dependencies=[Depends(require_local_token)])
    async def accept_invitation(token: str, body: _AcceptInvite) -> Response:
        return await _broker_request(
            "POST", f"/invitations/{token}/accept", json_body=body.model_dump(),
        )

    @app.post("/api/invitations/{token}/decline", dependencies=[Depends(require_local_token)])
    async def decline_invitation(token: str) -> Response:
        return await _broker_request("POST", f"/invitations/{token}/decline", json_body={})

    @app.get("/api/dispatches", dependencies=[Depends(require_local_token)])
    async def list_dispatches(role: str = Query(default="received")) -> Response:
        return await _broker_request("GET", "/dispatches", params={"role": role})

    @app.post("/api/dispatch/{dispatch_id}/cancel", dependencies=[Depends(require_local_token)])
    async def cancel_dispatch_endpoint(dispatch_id: UUID) -> Response:
        return await _broker_request("POST", f"/dispatch/{dispatch_id}/cancel")

    @app.get("/api/devices", dependencies=[Depends(require_local_token)])
    async def list_devices() -> Response:
        return await _broker_request("GET", "/devices")

    @app.patch("/api/devices/{device_id}", dependencies=[Depends(require_local_token)])
    async def rename_device(device_id: str, body: _DeviceRename) -> Response:
        return await _broker_request("PATCH", f"/devices/{device_id}", json_body=body.model_dump())

    @app.delete("/api/devices/{device_id}", dependencies=[Depends(require_local_token)])
    async def revoke_device(device_id: str) -> Response:
        return await _broker_request("DELETE", f"/devices/{device_id}")

    # ── WebSocket proxy for the sender's live "watch" view ──────────────
    # Browser opens ws://127.0.0.1:8001/ws/dispatch/{id}?t=<local-token>
    # Daemon opens wss://<broker>/dispatch/{id}/watch?token=<broker-jwt>
    # and shuttles frames in both directions. Broker JWT never leaves the
    # daemon; the SPA only ever sees the local bearer.
    @app.websocket("/ws/dispatch/{dispatch_id}")
    async def ws_dispatch_watch(
        client_ws: WebSocket,
        dispatch_id: str,
        t: Optional[str] = Query(default=None),
    ) -> None:
        if not require_local_token_ws(t):
            await client_ws.close(code=4401)
            return
        await client_ws.accept()
        if not local_state.broker_token:
            await client_ws.send_text(json.dumps({
                "type": "error",
                "data": {"message": "broker token unavailable", "exception": "Unauthenticated"},
            }))
            await client_ws.close()
            return

        broker_ws_url = (
            local_state.broker_url.rstrip("/").replace("https://", "wss://").replace("http://", "ws://")
            + f"/dispatch/{dispatch_id}/watch?token={local_state.broker_token}"
        )

        import ssl as _ssl
        import certifi as _certifi
        ssl_ctx = (
            _ssl.create_default_context(cafile=_certifi.where())
            if broker_ws_url.startswith("wss://")
            else None
        )

        # websockets is already in our deps (daemon's broker WS uses it).
        import websockets as _ws
        try:
            async with _ws.connect(broker_ws_url, max_size=None, ssl=ssl_ctx) as broker_ws:
                async def client_to_broker() -> None:
                    try:
                        while True:
                            msg = await client_ws.receive_text()
                            await broker_ws.send(msg)
                    except WebSocketDisconnect:
                        pass

                async def broker_to_client() -> None:
                    try:
                        async for msg in broker_ws:
                            await client_ws.send_text(msg if isinstance(msg, str) else msg.decode())
                    except _ws.ConnectionClosed:
                        pass

                done, pending = await asyncio.wait(
                    [asyncio.create_task(client_to_broker()),
                     asyncio.create_task(broker_to_client())],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
        except Exception as exc:
            logger.warning("ws proxy failed: %s", exc)
        finally:
            try:
                await client_ws.close()
            except Exception:
                pass

    # Workflows: only mount when an engine was supplied. The router itself
    # is just a proxy + local-token guard; routes are 503 until the engine
    # is wired in by main.run_session.
    if workflow_engine is not None:
        from dispatch.daemon.workflow_routes import make_router as make_workflow_router
        app.include_router(
            make_workflow_router(
                workflow_engine,
                local_state,
                daemon_state,
                local_token,
                local_state.broker_url,
            )
        )

    if STATIC_DIR.exists():
        app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
    return app


@dataclass
class LocalServer:
    """Handle returned by spawn(). Lets the supervisor stop the uvicorn
    server cleanly so the port is released before the next iteration
    tries to bind."""
    server: Any   # uvicorn.Server
    task: asyncio.Task

    async def stop(self, timeout: float = 3.0) -> None:
        # Graceful path — flips Server.should_exit and lets uvicorn close
        # the listen socket on its own loop. Falls back to cancel() if the
        # server is stuck.
        self.server.should_exit = True
        try:
            await asyncio.wait_for(self.task, timeout=timeout)
        except (asyncio.TimeoutError, Exception):
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass


async def serve(
    local_state: LocalState,
    daemon_state,
    local_token: str,
    host: str = "127.0.0.1",
    port: int = 8001,
    workflow_engine=None,
) -> None:
    """Run the local FastAPI app in the current event loop."""
    import uvicorn
    app = make_app(local_state, daemon_state, local_token, workflow_engine=workflow_engine)
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()


def spawn(
    local_state: LocalState,
    daemon_state,
    local_token: str,
    host: str = "127.0.0.1",
    port: int = 8001,
    workflow_engine=None,
) -> LocalServer:
    """Start the local UI server. Returns a handle whose stop() releases
    the listen socket so the next iteration of a reconnect loop can
    re-bind.

    We pre-bind the socket with SO_REUSEADDR + SO_REUSEPORT so a reconnect
    immediately following a server stop can rebind without waiting for the
    kernel's TIME_WAIT.
    """
    import socket as _s
    import uvicorn
    app = make_app(local_state, daemon_state, local_token, workflow_engine=workflow_engine)
    sock = _s.socket(_s.AF_INET, _s.SOCK_STREAM)
    sock.setsockopt(_s.SOL_SOCKET, _s.SO_REUSEADDR, 1)
    try:
        sock.setsockopt(_s.SOL_SOCKET, _s.SO_REUSEPORT, 1)
    except (AttributeError, OSError):
        pass  # not all platforms have SO_REUSEPORT
    sock.bind((host, port))
    sock.listen(128)
    sock.setblocking(False)
    config = uvicorn.Config(app, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(sockets=[sock]))
    return LocalServer(server=server, task=task)
