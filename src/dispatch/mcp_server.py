"""dispatch-mcp — the in-session Dispatch helper (thin client of the daemon).

Claude Code launches this stdio MCP server per session (declared in the
plugin's plugin.json). It is **not** a second daemon: it holds no broker
connection, no device key, and runs no executor. Instead it is a thin client
of the local daemon's 127.0.0.1 API.

Lifecycle:
  1. Read ~/.dispatch/config.json. No broker token → **dormant**: the tools
     stay loaded but each one tells the user to run `dispatch login`. Nothing
     is spawned, no browser opens (Layer-0 courtesy: pre-login we do nothing).
  2. Otherwise **ensure a daemon is running** — if the local API isn't
     answering, spawn one detached (the tray on macOS, which hosts the daemon
     and gives the menu-bar indicator; bare `dispatch-daemon` elsewhere) and
     wait for it to bind. The daemon persists across sessions.
  3. Talk to that daemon over its local API for everything: inbox, status,
     send, accept/decline, per-tool approvals, trust, invitations.

Why this shape (vs. the old per-session daemon):
  - **One broker connection per machine.** The daemon owns it (guarded by the
    connection lock); sessions never compete for it, so the eviction/churn war
    is gone. See dispatch.daemon.connlock.
  - **Cross-session visibility for free.** Every session reads the same daemon
    inbox, so what one terminal accepts is visible in another.
  - **Security layers unchanged.** Layer 2 (signature + TOFU pin) and the
    executor run in the daemon. Layer 3 (human approval) is surfaced here via
    `ctx.elicit` and resolved against the daemon — the daemon, not the broker,
    holds the approval futures, so the broker still can't fabricate consent.

The dispatched task itself runs in the daemon's confined executor (a fresh
ClaudeSDKClient with `setting_sources=[]`); it never inherits this session's
skills/MCP. So which process *hosts* a dispatch makes no difference to how it
runs — only where the broker socket and approval futures live.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal, Optional
from uuid import UUID

import httpx
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.elicitation import AcceptedElicitation
from pydantic import BaseModel, Field, create_model

from dispatch.daemon.identity import dispatch_home
from dispatch.daemon.local_app import read_local_token
from dispatch.daemon.connlock import ConnectionLock
from dispatch.daemon.main import _load_config
from dispatch.shared.schema import reply_from_events

logger = logging.getLogger("dispatch.mcp")

# How long to wait for a freshly-spawned daemon's local API to come up.
DAEMON_BOOT_TIMEOUT_S = 30.0
DAEMON_POLL_S = 0.5
_TERMINAL = {"completed", "failed", "denied", "cancelled", "expired"}
_SUPERVISE_TIMEOUT_S = 600.0  # safety cap on a single accepted run
_LOGIN_HINT = "not signed in — run `dispatch login` in a terminal, then restart this session."


# ----------------------------------------------------------------------------
# Session link: a thin handle to the local daemon's API (set in the lifespan).
# ----------------------------------------------------------------------------


@dataclass
class _Link:
    base: str           # http://127.0.0.1:<port>
    local_token: str    # bearer for the daemon's local API
    user_id: str
    device_id: str
    broker: str


# Set during the MCP lifespan; read by the tools. One MCP process per session.
LINK: Optional[_Link] = None
LOGGED_OUT = False      # True when there's no broker token → dormant mode


def _resolve_conn() -> tuple[str, Optional[str]]:
    config = _load_config()
    broker = (
        os.environ.get("DISPATCH_BROKER") or config.get("broker") or "http://localhost:8000"
    ).rstrip("/")
    token = os.environ.get("DISPATCH_TOKEN") or config.get("token")
    return broker, token


def _local_port() -> int:
    """The port the daemon's local API listens on. Prefer the running owner's
    recorded port; fall back to the configured / default port."""
    owner = ConnectionLock(dispatch_home() / "connection.lock").read_owner()
    if isinstance(owner.get("local_port"), int):
        return owner["local_port"]
    config = _load_config()
    return int(os.environ.get("DISPATCH_LOCAL_PORT") or config.get("local_port") or 8001)


async def _ping(base: str, token: str) -> Optional[dict]:
    """Is a daemon answering the local API here? Returns its /api/session or None."""
    try:
        async with httpx.AsyncClient(timeout=2.0) as c:
            r = await c.get(f"{base}/api/session", headers={"Authorization": f"Bearer {token}"})
        return r.json() if r.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None


def _tray_available() -> bool:
    """Is the macOS [tray] extra (pyobjc/rumps) importable in this venv?"""
    import importlib.util
    return all(importlib.util.find_spec(m) is not None for m in ("objc", "rumps"))


def _spawn_daemon(*, prefer_tray: bool) -> None:
    """Launch a daemon detached so it outlives this session. On macOS we prefer
    the tray (it hosts the daemon AND gives the menu-bar indicator); elsewhere,
    or if the tray extra is missing, the bare daemon. Both read broker/token/
    port from ~/.dispatch/config.json, so no args are needed."""
    exe = None
    if prefer_tray and sys.platform == "darwin" and _tray_available():
        exe = shutil.which("dispatch-tray")
    if not exe:
        exe = shutil.which("dispatch-daemon")
    if not exe:
        raise RuntimeError("neither dispatch-tray nor dispatch-daemon found on PATH")
    try:
        log = open(dispatch_home() / "daemon-spawn.log", "ab")
    except OSError:
        log = subprocess.DEVNULL
    subprocess.Popen(
        [exe], stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True
    )
    logger.info("dispatch-mcp spawned a daemon via %s", exe)


async def _ensure_daemon() -> _Link:
    """Guarantee a daemon is serving the local API; spawn one if not. Returns a
    thin link to it. Raises if no token (caller handles dormant mode) or if the
    daemon never comes up."""
    broker, token = _resolve_conn()
    if not token:
        raise RuntimeError(_LOGIN_HINT)

    port = _local_port()
    base = f"http://127.0.0.1:{port}"
    local_token = read_local_token()
    session = await _ping(base, local_token) if local_token else None

    if session is None:
        # No daemon answering — spawn one and wait for it to bind.
        _spawn_daemon(prefer_tray=True)
        spawned_bare = False
        waited = 0.0
        while waited < DAEMON_BOOT_TIMEOUT_S:
            await asyncio.sleep(DAEMON_POLL_S)
            waited += DAEMON_POLL_S
            local_token = read_local_token()
            if local_token:
                base = f"http://127.0.0.1:{_local_port()}"
                session = await _ping(base, local_token)
                if session is not None:
                    break
            # Halfway in with nothing yet (e.g. tray couldn't launch on a
            # headless box): fall back to a bare daemon.
            if not spawned_bare and waited >= DAEMON_BOOT_TIMEOUT_S / 2:
                _spawn_daemon(prefer_tray=False)
                spawned_bare = True
        if session is None:
            raise RuntimeError(
                f"daemon did not come up within {DAEMON_BOOT_TIMEOUT_S:.0f}s "
                f"(see {dispatch_home() / 'daemon-spawn.log'})"
            )

    config = _load_config()
    return _Link(
        base=base,
        local_token=local_token,
        user_id=session.get("user_id", ""),
        device_id=str(config.get("device_id", "")),
        broker=broker,
    )


@asynccontextmanager
async def _lifespan(_server: FastMCP):
    global LINK, LOGGED_OUT
    _, token = _resolve_conn()
    if not token:
        LOGGED_OUT = True   # dormant: load tools, spawn nothing, prompt to log in
        logger.info("dispatch-mcp dormant: no broker token (run `dispatch login`)")
        yield None
        return
    try:
        LINK = await _ensure_daemon()
    except Exception as exc:  # noqa: BLE001 — stay loaded so tools can report the error
        logger.warning("dispatch-mcp could not reach/start a daemon: %s", exc)
    try:
        yield LINK
    finally:
        LINK = None


# ----------------------------------------------------------------------------
# Local API client (every tool routes through the daemon, never the broker).
# ----------------------------------------------------------------------------

mcp = FastMCP("dispatch", lifespan=_lifespan)


class _Approve(BaseModel):
    # Single-select → the client renders an arrow-key choose-one prompt.
    decision: Literal["Allow", "Deny", "Allow the rest of this dispatch"]


# Built-in tools the invite picker can grant (mirrors executor.ALL_TOOLS).
_ALL_TOOLS = ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]
_READONLY_TOOLS = ["Read", "Glob", "Grep"]


class _ToolGrant(BaseModel):
    # "Allow all" first, per the design. Single-select → arrow-key prompt.
    # Built-in file tools only; MCP servers are a separate question below so the
    # two are orthogonal (e.g. read-only files yet allowed to use one MCP).
    grant: Literal[
        "Allow all — every tool + all my MCP servers",
        "Read-only — Read, Glob, Grep",
        "Read + Write/Edit (no Bash)",
        "Custom — use the tools/paths I passed",
    ]


async def _pick_mcp_servers(ctx: Context, names: list[str]) -> list[str]:
    """Second invite question: which installed MCP servers this sender may use.

    Multi-select (one bool per server) → the client renders a checkable form.
    Server names aren't always valid field identifiers (hyphens, dots), so each
    maps to a positional field `s<i>` whose human label is the real name.
    Returns the chosen server names; [] if declined or elicitation unavailable."""
    if not names:
        return []
    fields = {
        f"s{i}": (bool, Field(default=False, title=name, description=f"Allow '{name}'"))
        for i, name in enumerate(names)
    }
    Model = create_model("McpServerPick", **fields)
    try:
        res = await ctx.elicit(
            message=(
                "Which of your MCP servers may this sender's tasks use? "
                "(leave all unchecked for no MCP access)"
            ),
            schema=Model,
        )
    except Exception:
        return []
    if not isinstance(res, AcceptedElicitation):
        return []
    return [name for i, name in enumerate(names) if getattr(res.data, f"s{i}", False)]


def _require_link() -> _Link:
    if LOGGED_OUT:
        raise _Dormant()
    if LINK is None:
        raise RuntimeError(
            "the local dispatch daemon isn't reachable yet — it may still be "
            "starting. Retry in a moment, or check ~/.dispatch/daemon-spawn.log."
        )
    return LINK


class _Dormant(Exception):
    """Raised when there's no broker token; tools convert it to a login hint."""


async def _local_call(method: str, path: str, **kw: Any) -> Any:
    link = _require_link()
    try:
        async with httpx.AsyncClient(timeout=30.0) as c:
            r = await c.request(
                method, f"{link.base}{path}",
                headers={"Authorization": f"Bearer {link.local_token}"}, **kw,
            )
    except httpx.HTTPError as exc:
        return {"error": "daemon_unreachable", "detail": str(exc)}
    if r.status_code >= 400:
        detail = r.text
        try:
            detail = r.json().get("detail", detail)
        except ValueError:
            pass
        return {"error": r.status_code, "detail": detail}
    return r.json() if r.content else {}


# ── Core handlers (plain functions; the grouped tools below route to these). ──


def _do_whoami() -> dict:
    link = _require_link()
    return {"user_id": link.user_id, "broker": link.broker, "device_id": link.device_id}


async def _do_inbox() -> Any:
    return await _local_call("GET", "/api/inbox")


async def _do_pending_approvals() -> Any:
    inbox = await _local_call("GET", "/api/inbox")
    if not isinstance(inbox, list):
        return inbox
    out: list[dict] = []
    for entry in inbox:
        for request_id, info in (entry.get("pending_tools") or {}).items():
            out.append({
                "dispatch_id": entry.get("dispatch_id"), "request_id": request_id,
                "sender_id": entry.get("sender_id"),
                "tool": info.get("tool"), "input": info.get("input"),
            })
    return out


async def _do_status(dispatch_id: str) -> dict:
    try:
        UUID(dispatch_id)
    except ValueError:
        return {"error": "bad_id", "detail": "dispatch_id must be a UUID"}
    result = await _local_call("GET", f"/api/dispatch/{dispatch_id}")
    # Local entries carry events but no derived reply; add it client-side. (The
    # broker-fallback path already includes `reply`.)
    if isinstance(result, dict) and "events" in result and "reply" not in result:
        result["reply"] = reply_from_events(result.get("events") or [])
    return result


async def _run_accept(dispatch_id: str, ctx: Context) -> dict:
    """Accept + supervise the daemon's confined run to completion, asking the
    human (via elicitation) for each tool call on a manual edge. The run
    executes in the DAEMON's executor; we relay its pending approvals here."""
    accept = await _local_call(
        "POST", f"/api/dispatch/{dispatch_id}/decision", json={"decision": "accept"},
    )
    if isinstance(accept, dict) and accept.get("error"):
        if accept.get("error") == 409:
            return {"status": "error",
                    "detail": "no pending decision for that dispatch — run "
                              "dispatch_read(what='inbox') (it may not be addressed "
                              "to you, or it's already decided/expired)."}
        return {"status": "error", "detail": accept.get("detail", "accept failed")}

    handled: set[str] = set()
    auto_allow = False  # set once the human picks "Allow the rest of this dispatch"
    waited = 0.0
    status: Optional[str] = None
    events = 0
    while waited < _SUPERVISE_TIMEOUT_S:
        detail = await _local_call("GET", f"/api/dispatch/{dispatch_id}")
        if isinstance(detail, dict) and not detail.get("error"):
            status = detail.get("status")
            events = len(detail.get("events") or [])
            sender = detail.get("sender_id", "?")
            for request_id, info in (detail.get("pending_tools") or {}).items():
                if request_id in handled:
                    continue
                handled.add(request_id)
                if auto_allow:
                    decision = "allow"
                else:
                    msg = (
                        f"Dispatch {dispatch_id[:8]}… from {sender} wants to run:\n"
                        f"  {info.get('tool')}: {info.get('input')}"
                    )
                    try:
                        res = await ctx.elicit(message=msg, schema=_Approve)
                        choice = res.data.decision if isinstance(res, AcceptedElicitation) else "Deny"
                        if choice == "Allow the rest of this dispatch":
                            auto_allow = True
                            decision = "allow"
                        elif choice == "Allow":
                            decision = "allow"
                        else:
                            decision = "deny"
                    except Exception:
                        decision = "deny"  # fail safe if elicitation is unavailable
                await _local_call(
                    "POST", f"/api/dispatch/{dispatch_id}/tool/{request_id}/decision",
                    json={"decision": decision},
                )
            if status in _TERMINAL:
                break
        await asyncio.sleep(0.25)
        waited += 0.25

    return {
        "status": status or "unknown",
        "dispatch_id": dispatch_id,
        "events": events,
        "note": "Ran in the daemon's sandboxed dp-agent (confined to the edge "
                "scope) and you approved each tool call above. Do NOT perform the "
                "task yourself or run any tools toward it — it is already done.",
    }


# ── The 4 grouped tools the agent sees. ──────────────────────────────────────

@mcp.tool()
async def dispatch_read(
    what: Literal["inbox", "status", "sent", "contacts", "invitations", "approvals", "whoami"],
    dispatch_id: str = "",
) -> Any:
    """Read dispatch state (no side effects).
      inbox       — dispatches addressed to you (+ scopes, pending approvals)
      status      — full detail + event trace (+ reply) for `dispatch_id`
      sent        — dispatches you've sent, with status
      contacts    — trust edges: who can dispatch to whom, scopes, online
      invitations — pending invitations you've sent / received (each has a token)
      approvals   — tool calls awaiting your allow/deny
      whoami      — your user id, broker, device id
    """
    try:
        if what == "whoami":
            return _do_whoami()
        if what == "inbox":
            return await _do_inbox()
        if what == "approvals":
            return await _do_pending_approvals()
        if what == "status":
            if not dispatch_id:
                return {"error": "dispatch_id required for what='status'"}
            return await _do_status(dispatch_id)
        if what == "sent":
            return await _local_call("GET", "/api/dispatches", params={"role": "sent"})
        if what == "contacts":
            return await _local_call("GET", "/api/trust")
        if what == "invitations":
            return await _local_call("GET", "/api/invitations")
        return {"error": f"unknown what: {what}"}
    except _Dormant:
        return {"error": "logged_out", "detail": _LOGIN_HINT}


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
    try:
        if action == "accept":
            return await _run_accept(dispatch_id, ctx)
        if action == "decline":
            r = await _local_call(
                "POST", f"/api/dispatch/{dispatch_id}/decision", json={"decision": "reject"},
            )
            return {"status": "ok", "dispatch_id": dispatch_id} if not (
                isinstance(r, dict) and r.get("error")) else {"status": "error", "detail": r.get("detail")}
        if action == "cancel":
            return await _local_call("POST", f"/api/dispatch/{dispatch_id}/cancel")
        if action in ("approve", "deny"):
            if not request_id:
                return {"status": "error", "detail": "request_id required for approve/deny"}
            r = await _local_call(
                "POST", f"/api/dispatch/{dispatch_id}/tool/{request_id}/decision",
                json={"decision": "allow" if action == "approve" else "deny"},
            )
            return {"status": "ok", "dispatch_id": dispatch_id, "request_id": request_id} if not (
                isinstance(r, dict) and r.get("error")) else {"status": "error", "detail": r.get("detail")}
        return {"status": "error", "detail": f"unknown action: {action}"}
    except _Dormant:
        return {"status": "error", "detail": _LOGIN_HINT}


@mcp.tool()
async def dispatch_send(
    recipient: str, task: str, expires_in_seconds: int = 3600, cwd: Optional[str] = None
) -> dict:
    """Send a dispatch to a trusted contact. The verbatim `task` runs on their
    machine across an accepted, scoped trust edge. Returns the dispatch_id
    (track with dispatch_read(what='status')).
    """
    try:
        metadata = {"cwd": cwd} if cwd else {}
        body = {
            "recipient_id": recipient, "task": task,
            "expires_in_seconds": expires_in_seconds, "metadata": metadata,
        }
        return await _local_call("POST", "/api/compose", json=body)
    except _Dormant:
        return {"error": "logged_out", "detail": _LOGIN_HINT}


@mcp.tool()
async def dispatch_invite(
    action: Literal["send", "list", "accept", "decline"],
    ctx: Context,
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
                dispatch to your machine. This PROMPTS the human to pick what the
                sender may use (Allow all / read-only / etc.) via an inline
                chooser — you do not need to pass `tools`. Passing `tools`
                (comma-separated ⊆ Read,Glob,Grep,Write,Edit,Bash) + `paths`
                pre-fills the "Custom" choice.
      decline — decline invite `token`; no edge is created.
    """
    try:
        if action == "send":
            if not to_email:
                return {"error": "to_email required for action='send'"}
            return await _local_call("POST", "/api/invitations", json={"to_email": to_email})
        if action == "list":
            return await _local_call("GET", "/api/invitations")
        if action == "accept":
            if not token:
                return {"error": "token required for action='accept'"}
            scope_tools = [t.strip() for t in tools.split(",") if t.strip()]
            scope_paths = [p.strip() for p in paths.split(",") if p.strip()]
            scope_mcp: list[str] = []
            # Q1 — built-in file tools. "Allow all" first; single-select → the
            # arrow-key prompt.
            try:
                res = await ctx.elicit(
                    message=(
                        f"Accept invite and let this sender dispatch to your machine.\n"
                        f"What built-in tools may their tasks use?"
                    ),
                    schema=_ToolGrant,
                )
                grant = res.data.grant if isinstance(res, AcceptedElicitation) else None
            except Exception:
                grant = None  # elicitation unavailable → fall back to passed/default scope
            if grant and grant.startswith("Allow all"):
                # Allow all is the only choice that auto-grants every MCP server,
                # so it skips the per-server question below.
                scope_tools, scope_mcp = list(_ALL_TOOLS), ["*"]
            else:
                if grant and grant.startswith("Read-only"):
                    scope_tools = list(_READONLY_TOOLS)
                elif grant and grant.startswith("Read + Write"):
                    scope_tools = ["Read", "Glob", "Grep", "Write", "Edit"]
                elif not scope_tools:
                    # "Custom" with nothing passed, or no elicitation → least priv.
                    scope_tools = list(_READONLY_TOOLS)
                # Q2 — which installed MCP servers (orthogonal to the tool tier).
                servers = await _local_call("GET", "/api/mcp/servers")
                names = [s["name"] for s in servers if isinstance(s, dict) and s.get("name")] \
                    if isinstance(servers, list) else []
                scope_mcp = await _pick_mcp_servers(ctx, names)
            scopes: dict[str, Any] = {
                "tools": scope_tools, "mcp": scope_mcp, "paths": scope_paths,
                "approval": approval, "max_dispatches_per_day": max_dispatches_per_day,
            }
            return await _local_call("POST", f"/api/invitations/{token}/accept", json={"scopes": scopes})
        if action == "decline":
            if not token:
                return {"error": "token required for action='decline'"}
            return await _local_call("POST", f"/api/invitations/{token}/decline")
        return {"error": f"unknown action: {action}"}
    except _Dormant:
        return {"error": "logged_out", "detail": _LOGIN_HINT}


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    mcp.run()


if __name__ == "__main__":
    main()
