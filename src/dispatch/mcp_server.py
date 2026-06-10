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
from pydantic import BaseModel

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
    #   Allow                        — just this one call
    #   Deny                         — refuse this one call
    #   Always allow this tool       — persist onto the edge; never ask again
    #   Allow this tool this session — in-memory for the rest of this run
    #   Allow the rest of this dispatch — auto-allow every later call in THIS run
    decision: Literal[
        "Allow",
        "Deny",
        "Always allow this tool",
        "Allow this tool this session",
        "Allow the rest of this dispatch",
    ]


# Built-in tools the invite picker can grant (mirrors executor.ALL_TOOLS).
_ALL_TOOLS = ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]
_READONLY_TOOLS = ["Read", "Glob", "Grep"]
_RW_TOOLS = ["Read", "Glob", "Grep", "Write", "Edit"]

# Built-in tool tiers, shown as a single-select (arrow-key) prompt. "Allow all"
# is the only tier that also grants every MCP server (and so skips the per-server
# question). Order matters: the first entry is what the client highlights, which
# we exploit to "pre-select" the current tier when editing an existing edge.
_TIER_ALLOW_ALL = "Allow all — every tool + all my MCP servers"
_TIER_READONLY = "Read-only — Read, Glob, Grep"
_TIER_RW = "Read + Write/Edit (no Bash)"
_TIER_CUSTOM = "Custom — use the tools/paths I passed"
_TIERS = [_TIER_ALLOW_ALL, _TIER_READONLY, _TIER_RW, _TIER_CUSTOM]


class _ToolGrant(BaseModel):
    # Built-in file tools only; MCP servers are a separate question below so the
    # two are orthogonal (e.g. read-only files yet allowed to use one MCP).
    grant: Literal[
        "Allow all — every tool + all my MCP servers",
        "Read-only — Read, Glob, Grep",
        "Read + Write/Edit (no Bash)",
        "Custom — use the tools/paths I passed",
    ]


def _tier_of(scope_tools: list[str], scope_mcp: list[str]) -> str:
    """Which tier an existing edge's scopes correspond to — so editing can
    pre-select it. Anything that isn't a clean preset reads as Custom."""
    tools = set(scope_tools or [])
    if "*" in (scope_mcp or []) and tools == set(_ALL_TOOLS):
        return _TIER_ALLOW_ALL
    if tools == set(_READONLY_TOOLS):
        return _TIER_READONLY
    if tools == set(_RW_TOOLS):
        return _TIER_RW
    return _TIER_CUSTOM


# Per-server Allow/Don't, as STATIC enum models. Single-select Literals are the
# only elicitation shape Claude Code reliably renders (same as _Approve) — a
# dynamically-built create_model() schema silently fails to render, and a
# boolean checkbox conflates "accept the prompt" with "check the box" so an
# accepted-but-untoggled box reads as False. Two classes only differ in which
# option is listed first, i.e. which the client highlights as the default — we
# pick the one matching the current grant so editing is effectively pre-filled.
class _ServerGrantDenyFirst(BaseModel):
    allow: Literal["Don't allow", "Allow"]


class _ServerGrantAllowFirst(BaseModel):
    allow: Literal["Allow", "Don't allow"]


async def _elicit_tier(ctx: Context, message: str, default: str | None = None) -> str | None:
    """Single-select tier prompt using the STATIC _ToolGrant enum (a dynamic
    create_model Literal does not render in Claude Code). `default` can't reorder
    a static Literal, so when editing we surface the current tier in the message
    text instead. Returns the chosen tier string, or None if unavailable."""
    if default:
        message = f"{message}\n(Currently: {default})"
    try:
        res = await ctx.elicit(message=message, schema=_ToolGrant)
    except Exception:
        return None
    return res.data.grant if isinstance(res, AcceptedElicitation) else None


async def _pick_mcp_servers(
    ctx: Context, names: list[str], granted: list[str] | None = None
) -> list[str]:
    """The MCP-server question: which servers this sender may use. Asks ONE
    single-select Allow/Don't per server (static enum — renders reliably, and
    the choice IS the value so there's no accept-vs-toggle ambiguity).

    Candidates = installed servers UNION already-granted names, so a grant for a
    server you've since uninstalled is still re-confirmed (and preserved), never
    silently dropped. On decline/unavailable for any server we keep its current
    grant state. Returns the chosen server names."""
    granted_set = set(granted or [])
    candidates = sorted(set(names) | granted_set)
    chosen: list[str] = []
    for name in candidates:
        currently = name in granted_set
        installed = name in names
        msg = f"Allow {_server_label(name)} for this sender?"
        if not installed:
            msg += "\n(not currently installed on this machine)"
        schema = _ServerGrantAllowFirst if currently else _ServerGrantDenyFirst
        try:
            res = await ctx.elicit(message=msg, schema=schema)
        except Exception:
            if currently:
                chosen.append(name)  # can't ask → keep what they had
            continue
        if isinstance(res, AcceptedElicitation):
            if res.data.allow == "Allow":
                chosen.append(name)
        elif currently:
            chosen.append(name)  # declined the prompt → keep existing grant
    return chosen


def _server_label(server: str) -> str:
    return f"MCP server '{server}'"


async def _installed_server_names() -> list[str]:
    servers = await _local_call("GET", "/api/mcp/servers")
    if not isinstance(servers, list):
        return []
    return [s["name"] for s in servers if isinstance(s, dict) and s.get("name")]


async def _installed_server_names() -> list[str]:
    servers = await _local_call("GET", "/api/mcp/servers")
    if not isinstance(servers, list):
        return []
    return [s["name"] for s in servers if isinstance(s, dict) and s.get("name")]


async def _elicit_scopes(
    ctx: Context,
    message: str,
    *,
    current_tools: list[str] | None = None,
    current_mcp: list[str] | None = None,
    passed_tools: list[str] | None = None,
) -> tuple[list[str], list[str]] | None:
    """Run the two-question scope picker (built-in tool tier + MCP servers) and
    return (scope_tools, scope_mcp). Shared by invite-accept and edit.

    When `current_*` is supplied (editing) the tier is pre-selected and the
    servers pre-checked. `passed_tools` pre-fills the Custom tier on accept.

    Returns None when no scope could be determined — the human wasn't asked
    (the picker didn't render or was declined) AND there's nothing explicit to
    honor (no `current_tools`, no `passed_tools`). The caller MUST treat this as
    "abort, the trustor has to choose" rather than granting a default: a trust
    scope is never invented on the human's behalf."""
    current_tools = current_tools or []
    current_mcp = current_mcp or []
    default_tier = (
        _tier_of(current_tools, current_mcp) if (current_tools or current_mcp) else None
    )
    grant = await _elicit_tier(ctx, message, default=default_tier)
    if grant and grant.startswith("Allow all"):
        # The only tier that also grants every MCP server → skip Q2.
        return list(_ALL_TOOLS), ["*"]
    if grant and grant.startswith("Read-only"):
        scope_tools = list(_READONLY_TOOLS)
    elif grant and grant.startswith("Read + Write"):
        scope_tools = list(_RW_TOOLS)
    elif grant and grant.startswith("Custom"):
        scope_tools = list(passed_tools or current_tools or _READONLY_TOOLS)
    elif current_tools or passed_tools:
        # No interactive choice (picker unavailable or declined), but an explicit
        # scope already exists — the current grant when editing, or tools the
        # caller passed when accepting. Honor that; it's a real human choice.
        scope_tools = list(current_tools or passed_tools)
    else:
        # Picker unavailable AND nothing explicit to fall back to. Do NOT invent
        # a default grant — signal the caller to abort and make the human choose.
        return None
    granted = [m for m in current_mcp if m != "*"]
    scope_mcp = await _pick_mcp_servers(ctx, await _installed_server_names(), granted=granted)
    return scope_tools, scope_mcp


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


def _approval_needed(dispatch_id: str, sender: str, request_id: str, info: dict) -> dict:
    """Payload returned to the HOST agent when this session can't render an
    inline elicitation prompt. The pending tool call is handed to the main
    agent to relay to the human in chat — exactly like a Bash permission
    prompt — instead of cancelling the run or silently denying. The run stays
    paused on the daemon's approval future (which auto-denies after ~120s,
    see TOOL_APPROVAL_TIMEOUT_S in dispatch.daemon.main)."""
    return {
        "status": "approval_needed",
        "dispatch_id": dispatch_id,
        "request_id": request_id,
        "sender_id": sender,
        "tool": info.get("tool"),
        "input": info.get("input"),
        "answer_within_seconds": 120,
        "detail": (
            "This session can't render inline approval prompts, so this pending "
            "tool call is handed to you instead. ASK THE USER NOW — show the "
            "tool and its input verbatim and offer: Allow once / Always allow "
            "this tool / Allow for this session / Deny. Never decide on their "
            "behalf. Then relay their choice with dispatch_act(action='approve' "
            "or 'deny', dispatch_id=…, request_id=…, grant='once'|'always'|"
            "'session'). That call resumes watching the run and returns either "
            "the next approval_needed or the final result. The daemon "
            "auto-denies this call if no decision arrives within ~120s, so ask "
            "immediately."
        ),
    }


async def _supervise(
    dispatch_id: str, ctx: Context, already_handled: set[str] | None = None
) -> dict:
    """Watch a running dispatch to completion, gating each pending tool call on
    the human (Layer 3). When the session can render elicitation, the prompt is
    inline (arrow-key Allow/Deny). When it can't, the pending call is returned
    as an `approval_needed` payload so the host agent asks the user in chat and
    relays the decision via dispatch_act(approve/deny) — which re-enters this
    loop until the next gate or the final result. `already_handled` carries
    request ids whose decision was just posted, so the brief window before the
    daemon pops them from pending_tools can't surface them twice."""
    handled: set[str] = set(already_handled or ())
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
                if auto_allow:
                    decision = "allow"
                else:
                    msg = (
                        f"Dispatch {dispatch_id[:8]}… from {sender} wants to run:\n"
                        f"  {info.get('tool')}: {info.get('input')}"
                    )
                    try:
                        res = await ctx.elicit(message=msg, schema=_Approve)
                    except Exception:
                        # Elicitation is unavailable in this session (e.g. a
                        # non-interactive surface). Do NOT silently deny, and do
                        # NOT cancel the run — hand the approval to the host
                        # agent so the human is asked in chat. The run keeps
                        # waiting on the daemon's future meanwhile.
                        return _approval_needed(dispatch_id, sender, request_id, info)
                    choice = res.data.decision if isinstance(res, AcceptedElicitation) else "Deny"
                    if choice == "Allow the rest of this dispatch":
                        auto_allow = True
                        decision = "allow"
                    elif choice == "Always allow this tool":
                        # Persist onto the edge AND skip future prompts for it.
                        decision = "always"
                    elif choice == "Allow this tool this session":
                        decision = "session"
                    elif choice == "Allow":
                        decision = "allow"
                    else:
                        decision = "deny"
                handled.add(request_id)
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
                "scope); every gated tool call was decided by the human. Do NOT "
                "perform the task yourself or run any tools toward it — it is "
                "already done.",
    }


async def _run_accept(dispatch_id: str, ctx: Context) -> dict:
    """Accept + supervise the daemon's confined run to completion, asking the
    human for each tool call on a manual edge. The run executes in the DAEMON's
    executor; we relay its pending approvals here (inline elicitation, or an
    approval_needed hand-off to the host agent when elicitation can't render)."""
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
    return await _supervise(dispatch_id, ctx)


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
    grant: Literal["once", "always", "session"] = "once",
) -> dict:
    """Act on an inbound or in-flight dispatch.
      accept  — accept AND run it in the sandboxed dp-agent; BLOCKS until done,
                rendering an inline approval prompt for each tool call on a
                manual edge. This is the ONLY interactive way to accept — never
                tell the user to run the `dispatch accept` CLI instead (that is
                fire-and-forget with no prompt attached and silently times out).
                You MUST NOT perform the task yourself — accepting *is* running it.
                If this session can't render inline prompts, accept returns
                status='approval_needed' with one pending tool call: ask the
                user in chat (Allow/Deny, like a Bash permission prompt) and
                relay via approve/deny below — the run waits, but auto-denies
                that call after ~120s, so ask immediately.
      decline — reject an inbound dispatch; it never runs.
      cancel  — cancel an in-flight dispatch (either party).
      approve / deny — relay the human's allow/deny for one pending tool call
                (needs `request_id`, from an approval_needed result or
                dispatch_read(what='approvals')), then keep watching the run:
                returns the next approval_needed, or the final result.
                `grant` qualifies approve: 'once' (default), 'always' (persist
                onto the edge — never ask for this tool again), or 'session'
                (skip prompts for this tool for the rest of this run).
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
            decision = "deny"
            if action == "approve":
                decision = {"once": "allow", "always": "always", "session": "session"}[grant]
            r = await _local_call(
                "POST", f"/api/dispatch/{dispatch_id}/tool/{request_id}/decision",
                json={"decision": decision},
            )
            if isinstance(r, dict) and r.get("error"):
                return {"status": "error", "detail": r.get("detail")}
            # Keep watching the run so the chat-relay loop continues: the next
            # gated tool call comes back as approval_needed (or prompts inline),
            # and a finished run returns its final status.
            return await _supervise(dispatch_id, ctx, already_handled={request_id})
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
            passed_tools = [t.strip() for t in tools.split(",") if t.strip()]
            scope_paths = [p.strip() for p in paths.split(",") if p.strip()]
            # Two-question picker: built-in tool tier, then which MCP servers.
            picked = await _elicit_scopes(
                ctx,
                "Accept invite and let this sender dispatch to your machine.\n"
                "What built-in tools may their tasks use?",
                passed_tools=passed_tools,
            )
            if picked is None:
                # The scope picker couldn't be shown and no `tools` were passed.
                # Refuse to accept with a silent default — the human must choose.
                return {
                    "error": "scope_choice_required",
                    "detail": (
                        "Couldn't show the tool-scope picker and no `tools` were "
                        "given, so there is nothing to grant. Dispatch will not "
                        "apply a default scope on its own. Re-run accept with an "
                        "explicit `tools` — a subset of Read,Glob,Grep,Write,Edit,"
                        "Bash — to choose what this sender's agent may do on your "
                        "machine (Bash = full shell; grant deliberately)."
                    ),
                }
            scope_tools, scope_mcp = picked
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


def _edge_summary(edges: list[dict]) -> list[dict]:
    """Compact view of edges for an error payload the model can choose from."""
    return [
        {"trust_link_id": e.get("trust_link_id"), "peer": e.get("peer"),
         "scopes": e.get("scopes", {})}
        for e in edges
    ]


async def _resolve_editable_edge(
    trust_link_id: str, peer: str
) -> tuple[dict | None, dict | None]:
    """Find the trust edge to edit/revoke without forcing the caller to know the
    raw id. Only edges you can edit (you're the trustor — `can_edit_scopes`) are
    candidates. Resolution order: explicit id → peer email → the sole editable
    edge. Ambiguous/none → an error payload listing the choices so the model can
    retry with a concrete id (instead of dead-ending on a missing param).
    Returns (edge, None) on success or (None, error_dict)."""
    data = await _local_call("GET", "/api/trust")
    if isinstance(data, dict) and data.get("error"):
        return None, data
    edges = data.get("trust", []) if isinstance(data, dict) else []
    editable = [e for e in edges if e.get("can_edit_scopes")]
    if trust_link_id:
        edge = next((e for e in editable if e.get("trust_link_id") == trust_link_id), None)
        if edge is None:
            return None, {"error": "no editable trust edge with that id",
                          "editable_edges": _edge_summary(editable)}
        return edge, None
    if peer:
        matches = [e for e in editable if (e.get("peer") or "").lower() == peer.lower()]
        if len(matches) == 1:
            return matches[0], None
        if not matches:
            return None, {"error": f"no editable edge for peer '{peer}'",
                          "editable_edges": _edge_summary(editable)}
        return None, {"error": f"multiple edges match '{peer}' — pass trust_link_id",
                      "matches": _edge_summary(matches)}
    if len(editable) == 1:
        return editable[0], None
    if not editable:
        return None, {"error": "you have no editable trust edges — nobody can "
                               "dispatch to you yet"}
    return None, {"error": "multiple editable edges — pass trust_link_id or peer",
                  "editable_edges": _edge_summary(editable)}


@mcp.tool()
async def dispatch_trust(
    action: Literal["revoke", "edit"],
    ctx: Context,
    trust_link_id: str = "",
    peer: str = "",
) -> Any:
    """Revoke or edit an existing trust edge — one where someone can dispatch to
    YOU (you're the trustor who set their scopes).

    Identifying the edge: pass `trust_link_id` (from dispatch_read(what=
    'contacts')) OR `peer` (their email). If you pass neither and there's only
    one editable edge, it's used; if it's ambiguous, this returns the list of
    editable edges so you can retry with a concrete id — you never need to guess.

      revoke — delete the edge: that sender can no longer dispatch to you, and
               any in-flight dispatch on the edge is cancelled immediately.
      edit   — re-pick this sender's tool + MCP-server scopes via the same
               inline prompts as accepting an invite, PRE-FILLED with their
               current grants (the current tool tier is highlighted, the MCP
               servers they already have are pre-checked). A grant for a server
               you've since uninstalled is preserved, not silently dropped.
               Takes effect on their NEXT dispatch; anything in flight keeps the
               scope it started with (revoke to stop those too).
    """
    try:
        edge, err = await _resolve_editable_edge(trust_link_id, peer)
        if err is not None:
            return err
        tlid = edge["trust_link_id"]
        if action == "revoke":
            return await _local_call("DELETE", f"/api/trust/{tlid}")
        # edit — pre-fill the picker from the edge's current scopes.
        cur = edge.get("scopes") or {}
        picked = await _elicit_scopes(
            ctx,
            f"Edit what {edge.get('peer', 'this sender')} may use when dispatching "
            f"to you:",
            current_tools=cur.get("tools") or [],
            current_mcp=cur.get("mcp") or [],
        )
        if picked is None:
            # Picker unavailable and the edge had no tools to keep — don't invent
            # a scope; leave the edge unchanged and tell the human to choose.
            return {
                "error": "scope_choice_required",
                "detail": (
                    "Couldn't show the tool-scope picker, so the edge is "
                    "unchanged. Re-run from an interactive session to choose the "
                    "scope."
                ),
            }
        scope_tools, scope_mcp = picked
        # Only the tool/MCP permissions change; keep paths/approval/limits intact.
        new_scopes = dict(cur)
        new_scopes.update({"tools": scope_tools, "mcp": scope_mcp})
        return await _local_call(
            "PATCH", f"/api/trust/{tlid}", json={"scopes": new_scopes}
        )
    except _Dormant:
        return {"error": "logged_out", "detail": _LOGIN_HINT}


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    mcp.run()


if __name__ == "__main__":
    main()
