"""dispatch-mcp — the in-session Dispatch helper.

Instead of a separate always-on daemon, this is a stdio MCP server that
Claude Code launches per session (declared in the plugin's plugin.json). For
the session's lifetime it:

  - holds this machine's Ed25519 device key (never leaves the machine),
  - keeps the broker WebSocket open,
  - signs outgoing dispatches and verifies + runs incoming ones,
  - exposes tools the Claude session drives (send / inbox / accept / decline /
    approve / status / contacts / cancel, plus invite / invitations /
    accept-invitation / decline-invitation for establishing trust edges).

It REUSES the daemon internals verbatim — identity/keys, signing, the
durable replay guard, signature verification, the executor, and LocalState —
so the security layers are unchanged:

  - Layer 2 (signature + TOFU pin) runs locally in this process, against keys
    the broker can't substitute.
  - Layer 3 (human approval) stays local: incoming dispatches and per-tool
    permission requests are surfaced via `dispatch_inbox` /
    `dispatch_pending_approvals` and resolved by `dispatch_accept` /
    `dispatch_approve` — the same future-resolution the daemon's 127.0.0.1
    web UI used, just exposed as MCP tools instead of HTTP endpoints.

Only the *surface* (MCP tools, not a local web server) and the *lifecycle*
(per-session, not always-on) differ from `dispatch-daemon`. The trade-off:
this can only receive/run dispatches while a Claude session is open;
dispatches sent while you're away wait in the broker's offline queue and
land when you next open Claude. For always-on reachability or scheduled
runs, use `dispatch-daemon` (same code, run as a service).

A future enhancement can drive the per-tool approval as a live MCP
elicitation inside the `dispatch_accept` tool (which has a request Context);
today approvals are tool-resolved, which works regardless of elicitation
support and keeps the human in control.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Optional
from uuid import UUID

import certifi
import httpx
import websockets
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.elicitation import AcceptedElicitation
from pydantic import BaseModel

from dispatch.daemon.identity import dispatch_home, ensure_enrolled, get_private_key
from dispatch.daemon.local_app import LocalState, _entry_summary
from dispatch.shared.schema import reply_from_events
from dispatch.daemon.main import (
    DEFAULT_WORKSPACE,
    FRESHNESS_WINDOW_S,
    DaemonState,
    SignedOutByBroker,
    _broker_ws_url,
    _load_config,
    _ssl_context_for,
    handle_broker,
    verify_token_user,
)
from dispatch.daemon.nonces import NonceStore
from dispatch.daemon.connlock import ConnectionLock, STANDBY_POLL_S

logger = logging.getLogger("dispatch.mcp")

ENROLL_TIMEOUT_S = 15.0


# ----------------------------------------------------------------------------
# Session link: the per-session "daemon" state, set up in the MCP lifespan.
# ----------------------------------------------------------------------------


@dataclass
class _Link:
    broker: str
    token: str
    user_id: str
    device_id: str
    workspace: Path
    private_key: bytes
    daemon_state: DaemonState
    local_state: LocalState
    nonce_store: NonceStore
    conn_lock: ConnectionLock
    ws_task: asyncio.Task
    stop: asyncio.Event


# Set during the MCP lifespan; read by the tools. One MCP process per session.
LINK: Optional[_Link] = None


def _resolve_conn() -> tuple[str, Optional[str]]:
    config = _load_config()
    broker = (os.environ.get("DISPATCH_BROKER") or config.get("broker") or "http://localhost:8000").rstrip("/")
    token = os.environ.get("DISPATCH_TOKEN") or config.get("token")
    return broker, token


async def _ws_loop(link_box: dict[str, _Link], stop: asyncio.Event) -> None:
    """Hold the broker WebSocket open for the session, reconnecting with
    backoff. `handle_broker` does the real work — signing outgoing dispatches
    (sign_request) and verifying + running incoming ones (new_dispatch),
    surfacing both into LocalState and parking approval futures the tools
    resolve."""
    link = link_box["link"]
    lock = link.conn_lock
    ws_url = _broker_ws_url(link.broker, link.token)
    ssl_ctx = _ssl_context_for(ws_url)
    backoff = 1.0
    while not stop.is_set():
        # Single connection-owner: only one process per machine holds the broker
        # WS. If another (e.g. the daemon or another session) owns it, stand by
        # and poll — we take over only if it exits (the lock auto-releases on
        # death). Once we own it, we keep ownership across broker reconnects.
        if not lock.held:
            if not lock.acquire():
                logger.info("another process owns the broker connection; standing by")
                try:
                    await asyncio.wait_for(stop.wait(), timeout=STANDBY_POLL_S)
                    return  # stop was set while standing by
                except asyncio.TimeoutError:
                    continue
            lock.write_owner(role="session")
            logger.info("dispatch-mcp acquired broker-connection ownership")
        try:
            async with websockets.connect(ws_url, max_size=None, ssl=ssl_ctx) as ws:
                await ws.send(json.dumps({"type": "hello", "device_id": link.device_id}))
                logger.info("dispatch-mcp connected to broker %s", link.broker)
                backoff = 1.0
                await handle_broker(
                    ws,
                    link.daemon_state,
                    link.workspace,
                    link.private_key,
                    local_state=link.local_state,
                    my_user=link.user_id,
                    my_device=link.device_id,
                )
        except SignedOutByBroker:
            logger.info("broker signaled sign-out; stopping link")
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — keep the session alive across blips
            if stop.is_set():
                return
            logger.warning("broker link dropped (%s); reconnecting in %.0fs", exc, backoff)
            try:
                await asyncio.wait_for(stop.wait(), timeout=backoff)
                return  # stop was set during the wait
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 30.0)


async def _start_link() -> _Link:
    broker, token = _resolve_conn()
    if not token:
        raise RuntimeError(
            "no broker token. Sign in to the broker and run the installer "
            "(writes ~/.dispatch/config.json), or set $DISPATCH_TOKEN."
        )

    config = _load_config()
    if config.get("anthropic_api_key") and not os.environ.get("ANTHROPIC_API_KEY"):
        os.environ["ANTHROPIC_API_KEY"] = config["anthropic_api_key"]

    device_id = await asyncio.wait_for(
        ensure_enrolled(broker, token, config.get("device_id")), timeout=ENROLL_TIMEOUT_S
    )
    private_key = get_private_key()
    if private_key is None:
        raise RuntimeError("no device private key after enrollment")

    user_id = verify_token_user(token)
    workspace = Path(os.environ.get("DISPATCH_WORKSPACE", str(DEFAULT_WORKSPACE))).expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=True)

    nonce_store = NonceStore(dispatch_home() / "nonces.db", FRESHNESS_WINDOW_S)
    try:
        from datetime import datetime, timezone

        nonce_store.prune(datetime.now(timezone.utc).timestamp())
    except Exception:
        logger.exception("nonce prune failed (continuing)")

    daemon_state = DaemonState()
    daemon_state.nonce_store = nonce_store
    local_state = LocalState(user_id=user_id, broker_url=broker, broker_token=token)
    await local_state.seed_from_broker()

    conn_lock = ConnectionLock(dispatch_home() / "connection.lock")

    stop = asyncio.Event()
    link_box: dict[str, _Link] = {}
    ws_task = asyncio.create_task(_ws_loop(link_box, stop))
    link = _Link(
        broker=broker, token=token, user_id=user_id, device_id=device_id,
        workspace=workspace, private_key=private_key, daemon_state=daemon_state,
        local_state=local_state, nonce_store=nonce_store, conn_lock=conn_lock,
        ws_task=ws_task, stop=stop,
    )
    link_box["link"] = link
    return link


async def _stop_link(link: _Link) -> None:
    link.stop.set()
    link.ws_task.cancel()
    try:
        await link.ws_task
    except (asyncio.CancelledError, Exception):
        pass
    link.nonce_store.close()
    link.conn_lock.release()  # hand off connection ownership to any standby


@asynccontextmanager
async def _lifespan(_server: FastMCP):
    global LINK
    LINK = await _start_link()
    try:
        yield LINK
    finally:
        if LINK is not None:
            await _stop_link(LINK)
        LINK = None


# ----------------------------------------------------------------------------
# Broker HTTP (control-plane ops the in-session signer doesn't handle locally)
# ----------------------------------------------------------------------------


def _require_link() -> _Link:
    if LINK is None:
        raise RuntimeError("dispatch link not ready yet — the broker connection is starting.")
    return LINK


async def _broker_call(method: str, path: str, **kw: Any) -> Any:
    link = _require_link()
    try:
        async with httpx.AsyncClient(timeout=30.0, verify=certifi.where()) as c:
            r = await c.request(
                method, f"{link.broker}{path}",
                headers={"Authorization": f"Bearer {link.token}"}, **kw,
            )
    except httpx.HTTPError as exc:
        return {"error": "broker_unreachable", "detail": str(exc)}
    if r.status_code >= 400:
        detail = r.text
        try:
            detail = r.json().get("detail", detail)
        except ValueError:
            pass
        return {"error": r.status_code, "detail": detail}
    return r.json() if r.content else {}


# ----------------------------------------------------------------------------
# MCP server + tools
# ----------------------------------------------------------------------------

mcp = FastMCP("dispatch", lifespan=_lifespan)


class _ApproveToolCall(BaseModel):
    allow: bool


_TERMINAL = {"completed", "failed", "denied", "cancelled", "expired"}
_SUPERVISE_TIMEOUT_S = 600.0  # safety cap on a single accepted run


# ── Core handlers (plain functions; the grouped tools below route to these). ──
# Collapsing ~15 tools into 4 grouped tools keeps the agent from doing a
# ToolSearch round-trip before every distinct dispatch call.

def _do_whoami() -> dict:
    link = _require_link()
    return {"user_id": link.user_id, "broker": link.broker, "device_id": link.device_id}


def _do_inbox() -> list[dict]:
    link = _require_link()
    return [_entry_summary(e) for e in link.local_state.entries.values()]


def _do_pending_approvals() -> list[dict]:
    link = _require_link()
    out: list[dict] = []
    for did, entry in link.local_state.entries.items():
        for request_id, info in entry.pending_tools.items():
            out.append({
                "dispatch_id": str(did), "request_id": request_id,
                "sender_id": entry.payload.sender_id,
                "tool": info.get("tool"), "input": info.get("input"),
            })
    return out


async def _do_status(dispatch_id: str) -> dict:
    link = _require_link()
    try:
        did = UUID(dispatch_id)
    except ValueError:
        return {"error": "bad_id", "detail": "dispatch_id must be a UUID"}
    entry = link.local_state.entries.get(did)
    if entry is not None:
        return {**_entry_summary(entry), "reply": reply_from_events(entry.events),
                "events": entry.events}
    return await _broker_call("GET", f"/dispatch/{dispatch_id}")


def _resolve_decision(dispatch_id: str, decision: str) -> dict:
    link = _require_link()
    fut = link.daemon_state.pending_decisions.get(dispatch_id)
    if fut is None or fut.done():
        return {"status": "error",
                "detail": "no pending decision for that dispatch — run dispatch_read(what='inbox') "
                          "(it may not be addressed to you, or it's already decided/expired)."}
    fut.set_result(decision)
    return {"status": "ok", "dispatch_id": dispatch_id, "decision": decision}


def _resolve_tool(dispatch_id: str, request_id: str, decision: str) -> dict:
    link = _require_link()
    fut = link.daemon_state.pending_approvals.get((dispatch_id, request_id))
    if fut is None or fut.done():
        return {"status": "error", "detail": "no pending approval for that tool call"}
    fut.set_result(decision)
    return {"status": "ok", "dispatch_id": dispatch_id, "request_id": request_id, "decision": decision}


async def _run_accept(dispatch_id: str, ctx: Context) -> dict:
    """Accept + supervise the sandboxed run to completion, asking the human
    (via elicitation) for each tool call on a manual edge."""
    link = _require_link()
    ds = link.daemon_state
    fut = ds.pending_decisions.get(dispatch_id)
    if fut is None or fut.done():
        return {"status": "error",
                "detail": "no pending decision for that dispatch — run dispatch_read(what='inbox') "
                          "(it may not be addressed to you, or it's already decided/expired)."}
    fut.set_result("accept")  # release the confined run

    try:
        did = UUID(dispatch_id)
    except ValueError:
        did = None

    handled: set[str] = set()
    waited = 0.0
    while waited < _SUPERVISE_TIMEOUT_S:
        entry = link.local_state.entries.get(did) if did else None
        for (d, request_id), afut in list(ds.pending_approvals.items()):
            if d != dispatch_id or request_id in handled or afut.done():
                continue
            handled.add(request_id)
            info = (entry.pending_tools.get(request_id) if entry else {}) or {}
            msg = (
                f"Dispatch {dispatch_id[:8]}… from "
                f"{entry.payload.sender_id if entry else '?'} wants to run:\n"
                f"  {info.get('tool')}: {info.get('input')}\n\nAllow this tool call?"
            )
            try:
                res = await ctx.elicit(message=msg, schema=_ApproveToolCall)
                decision = "allow" if (isinstance(res, AcceptedElicitation) and res.data.allow) else "deny"
            except Exception:
                decision = "deny"  # fail safe if elicitation is unavailable
            if not afut.done():
                afut.set_result(decision)

        status = entry.status.value if entry else None
        if status in _TERMINAL:
            break
        if dispatch_id not in ds.running and status in _TERMINAL | {None} and waited > 1.0:
            break
        await asyncio.sleep(0.25)
        waited += 0.25

    entry = link.local_state.entries.get(did) if did else None
    return {
        "status": entry.status.value if entry else "unknown",
        "dispatch_id": dispatch_id,
        "events": len(entry.events) if entry else 0,
        "note": "Ran in the sandboxed dp-agent (confined to the edge scope) and "
                "you approved each tool call above. Do NOT perform the task "
                "yourself or run any tools toward it — it is already done.",
    }


# ── The 4 grouped tools the agent sees. ──────────────────────────────────────

@mcp.tool()
async def dispatch_read(
    what: Literal["inbox", "status", "sent", "contacts", "invitations", "approvals", "whoami"],
    dispatch_id: str = "",
) -> Any:
    """Read dispatch state (no side effects).
      inbox       — dispatches addressed to you this session (+ scopes, pending approvals)
      status      — full detail + event trace for `dispatch_id`
      sent        — dispatches you've sent, with status
      contacts    — trust edges: who can dispatch to whom, scopes, online
      invitations — pending invitations you've sent / received (each has a token)
      approvals   — tool calls awaiting your allow/deny
      whoami      — your user id, broker, device id
    """
    if what == "whoami":
        return _do_whoami()
    if what == "inbox":
        return _do_inbox()
    if what == "approvals":
        return _do_pending_approvals()
    if what == "status":
        if not dispatch_id:
            return {"error": "dispatch_id required for what='status'"}
        return await _do_status(dispatch_id)
    if what == "sent":
        return await _broker_call("GET", "/dispatches", params={"role": "sent"})
    if what == "contacts":
        return await _broker_call("GET", "/trust")
    if what == "invitations":
        return await _broker_call("GET", "/invitations")
    return {"error": f"unknown what: {what}"}


@mcp.tool()
async def dispatch_act(
    action: Literal["accept", "decline", "approve", "deny", "cancel"],
    dispatch_id: str,
    ctx: Context,
    request_id: str = "",
) -> dict:
    """Act on an inbound or in-flight dispatch.
      accept  — accept AND run it in the sandboxed dp-agent; BLOCKS until done,
                prompting you inline for each tool call on a manual edge. You
                MUST NOT perform the task yourself — accepting *is* running it.
      decline — reject an inbound dispatch; it never runs.
      cancel  — cancel an in-flight dispatch (either party).
      approve / deny — allow/deny one pending tool call (needs `request_id`,
                from dispatch_read(what='approvals')). Fallback; normally
                `accept` handles approvals inline.
    """
    if action == "accept":
        return await _run_accept(dispatch_id, ctx)
    if action == "decline":
        return _resolve_decision(dispatch_id, "reject")
    if action == "cancel":
        return await _broker_call("POST", f"/dispatch/{dispatch_id}/cancel")
    if action in ("approve", "deny"):
        if not request_id:
            return {"status": "error", "detail": "request_id required for approve/deny"}
        return _resolve_tool(dispatch_id, request_id, "allow" if action == "approve" else "deny")
    return {"status": "error", "detail": f"unknown action: {action}"}


@mcp.tool()
async def dispatch_send(
    recipient: str, task: str, expires_in_seconds: int = 3600, cwd: Optional[str] = None
) -> dict:
    """Send a dispatch to a trusted contact. The verbatim `task` runs on their
    machine across an accepted, scoped trust edge. Signing happens in THIS
    session (your device key), so this session must be connected to the broker.
    Returns the dispatch_id (track with dispatch_read(what='status')).
    """
    metadata = {"cwd": cwd} if cwd else {}
    body = {
        "recipient_id": recipient, "task": task,
        "expires_in_seconds": expires_in_seconds, "metadata": metadata,
    }
    return await _broker_call("POST", "/dispatch", json=body)


@mcp.tool()
async def dispatch_invite(
    action: Literal["send", "list", "accept", "decline"],
    to_email: str = "",
    token: str = "",
    tools: str = "",
    paths: str = "",
    approval: Literal["manual", "auto"] = "manual",
    max_dispatches_per_day: int = 50,
) -> Any:
    """Manage invitations (how trust edges are created).
      send    — invite `to_email` to let YOU dispatch to them (they accept + set
                your scopes). Grants you nothing on its own.
      list    — list invitations you've sent / received (each has a token).
      accept  — accept invite `token`, creating an edge that lets the INVITER
                dispatch to your machine, confined to scopes YOU set here:
                `tools` (comma-separated ⊆ Read,Glob,Grep,Write,Edit,Bash;
                default read-only), `paths` (comma-separated dir allowlist),
                `approval` (manual|auto). Granting Bash grants full shell —
                confirm with the human.
      decline — decline invite `token`; no edge is created.
    """
    if action == "send":
        if not to_email:
            return {"error": "to_email required for action='send'"}
        return await _broker_call("POST", "/invitations", json={"to_email": to_email})
    if action == "list":
        return await _broker_call("GET", "/invitations")
    if action == "accept":
        if not token:
            return {"error": "token required for action='accept'"}
        scopes: dict[str, Any] = {"approval": approval, "max_dispatches_per_day": max_dispatches_per_day}
        if tools:
            scopes["tools"] = [t.strip() for t in tools.split(",") if t.strip()]
        if paths:
            scopes["paths"] = [p.strip() for p in paths.split(",") if p.strip()]
        return await _broker_call("POST", f"/invitations/{token}/accept", json={"scopes": scopes})
    if action == "decline":
        if not token:
            return {"error": "token required for action='decline'"}
        return await _broker_call("POST", f"/invitations/{token}/decline")
    return {"error": f"unknown action: {action}"}


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    mcp.run()


if __name__ == "__main__":
    main()
