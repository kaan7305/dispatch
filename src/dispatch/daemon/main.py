"""Recipient daemon.

Usage:
  dispatch-daemon --broker https://your-broker --token <jwt>
  # or
  python -m dispatch.daemon --broker ... --token ...

What it does:
  - Connects to the broker via WebSocket as the authenticated user.
  - For each `new_dispatch` from the broker:
      1. Sends `dispatch_status: delivered`.
      2. Waits for `dispatch_decision: accept` or `reject` from the broker
         (which the recipient triggered in the unified web UI).
      3. If accepted, runs run_dispatch() — destructive tool calls cause a
         `permission_request` event to flow back through the broker to the
         UI, where the user clicks Allow / Deny. The decision returns via
         a `tool_consent` message from the broker.
      4. Streams every executor event back to the broker.

The Claude Agent SDK runs in this process using THIS user's
ANTHROPIC_API_KEY. The broker never touches the API key.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

import uvicorn
import websockets
from claude_agent_sdk import (
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

from dispatch.daemon.local_app import LocalState, make_app
from dispatch.executor import run_dispatch
from dispatch.shared.schema import DispatchEvent, DispatchPayload, DispatchStatus

DESTRUCTIVE_TOOLS = frozenset({"Write", "Edit", "Bash"})
DEFAULT_WORKSPACE = Path.cwd() / "workspace"
DISPATCH_DECISION_TIMEOUT_S = 300.0     # how long the recipient has to accept/reject
TOOL_CONSENT_TIMEOUT_S = 120.0          # how long they have to allow/deny one tool call

logger = logging.getLogger("dispatch.daemon")


@dataclass
class DaemonState:
    # dispatch_id (str) → Future resolved with "accept" | "reject"
    pending_decisions: dict[str, asyncio.Future] = field(default_factory=dict)
    # (dispatch_id, request_id) → Future resolved with "allow" | "deny"
    pending_consents: dict[tuple[str, str], asyncio.Future] = field(default_factory=dict)


def _broker_ws_url(broker: str, token: str) -> str:
    p = urlparse(broker)
    scheme = "wss" if p.scheme == "https" else "ws"
    return urlunparse((scheme, p.netloc, "/agent/connect", "", f"token={token}", ""))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="dispatch-daemon")
    parser.add_argument(
        "--broker",
        default=os.environ.get("DISPATCH_BROKER", "http://localhost:8000"),
        help="Broker base URL (http or https). Default: $DISPATCH_BROKER or http://localhost:8000",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("DISPATCH_TOKEN"),
        help="JWT bearer token from the broker login page. Default: $DISPATCH_TOKEN",
    )
    parser.add_argument(
        "--ui-port",
        type=int,
        default=int(os.environ.get("DISPATCH_UI_PORT", "8001")),
        help="Port for the local consent UI. Default: 8001",
    )
    parser.add_argument(
        "--workspace",
        default=os.environ.get("DISPATCH_WORKSPACE", str(DEFAULT_WORKSPACE)),
        help="Working directory the agent operates in. Default: ./workspace",
    )
    return parser.parse_args(argv)


async def serve_local_ui(state: LocalState, port: int) -> uvicorn.Server:
    config = uvicorn.Config(
        make_app(state),
        host="127.0.0.1",
        port=port,
        log_level="warning",
        lifespan="off",
    )
    server = uvicorn.Server(config)
    serve_task = asyncio.create_task(server.serve())
    # Wait up to 15 s for uvicorn to bind (longer inside PyInstaller bundles).
    for _ in range(300):
        await asyncio.sleep(0.05)
        if server.started:
            return server
        if serve_task.done():
            break
    if not server.started:
        exc = serve_task.exception() if serve_task.done() and not serve_task.cancelled() else None
        Path("/tmp/dispatch_daemon.log").write_text(
            f"Local UI (port {port}) did not start. Exception: {exc}"
        )
    return server


async def run_session(args: argparse.Namespace) -> int:
    if not args.token:
        print("error: --token is required (or set DISPATCH_TOKEN)", file=sys.stderr)
        return 2
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "warning: ANTHROPIC_API_KEY is not set; the agent will fail to run unless "
            "the Claude Code CLI has its own session.",
            file=sys.stderr,
        )

    workspace = Path(args.workspace).expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    print(f"[daemon] workspace: {workspace}")

    state = DaemonState()
    ws_url = _broker_ws_url(args.broker, args.token)
    print(f"[daemon] connecting to broker: {args.broker}")
    try:
        async with websockets.connect(ws_url, max_size=None) as ws:
            print(
                f"[daemon] connected. Open {args.broker} in a browser to see incoming "
                "dispatches and respond to consent prompts."
            )
            await handle_broker(ws, state, workspace)
    except websockets.InvalidStatus as e:
        print(f"[daemon] handshake failed: {e}", file=sys.stderr)
        return 3
    except OSError as e:
        print(f"[daemon] could not reach broker: {e}", file=sys.stderr)
        return 4
    return 0


async def handle_broker(
    ws: "websockets.WebSocketClientProtocol",
    state: DaemonState,
    workspace: Path,
    on_friend_request=None,
) -> None:
    async def send_status(dispatch_id, status: DispatchStatus) -> None:
        await ws.send(
            json.dumps(
                {
                    "type": "dispatch_status",
                    "dispatch_id": str(dispatch_id),
                    "status": status.value,
                }
            )
        )

    async def send_event(dispatch_id, event: DispatchEvent) -> None:
        await ws.send(
            json.dumps(
                {
                    "type": "dispatch_event",
                    "dispatch_id": str(dispatch_id),
                    "event": event,
                }
            )
        )

    async for raw in ws:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue
        mtype = msg.get("type")

        if mtype == "new_dispatch":
            try:
                payload = DispatchPayload(**msg["payload"])
            except Exception:
                logger.exception("rejecting malformed dispatch")
                continue
            asyncio.create_task(
                process_dispatch(payload, state, workspace, send_status, send_event)
            )
        elif mtype == "friend_request" and on_friend_request:
            on_friend_request(msg.get("from_user", "unknown"))

        elif mtype == "dispatch_decision":
            did = msg.get("dispatch_id")
            decision = msg.get("decision")
            fut = state.pending_decisions.get(did)
            if fut and not fut.done() and decision in ("accept", "reject"):
                fut.set_result(decision)

        elif mtype == "tool_consent":
            did = msg.get("dispatch_id")
            req_id = msg.get("request_id")
            decision = msg.get("decision")
            fut = state.pending_consents.get((did, req_id))
            if fut and not fut.done() and decision in ("allow", "deny"):
                fut.set_result(decision)


async def process_dispatch(
    payload: DispatchPayload,
    state: DaemonState,
    workspace: Path,
    send_status,
    send_event,
) -> None:
    dispatch_id = str(payload.dispatch_id)
    print(f"[daemon] new dispatch {dispatch_id[:8]}… from {payload.sender_id}: {payload.task!r}")

    # Step 1 — top-level Accept / Reject.
    decision_fut: asyncio.Future = asyncio.get_running_loop().create_future()
    state.pending_decisions[dispatch_id] = decision_fut
    await send_status(payload.dispatch_id, DispatchStatus.delivered)

    try:
        top_decision = await asyncio.wait_for(decision_fut, timeout=DISPATCH_DECISION_TIMEOUT_S)
    except asyncio.TimeoutError:
        await send_status(payload.dispatch_id, DispatchStatus.expired)
        return
    finally:
        state.pending_decisions.pop(dispatch_id, None)

    if top_decision != "accept":
        await send_status(payload.dispatch_id, DispatchStatus.denied)
        return

    await send_status(payload.dispatch_id, DispatchStatus.accepted)

    # Step 2 — per-tool consent.
    async def can_use_tool(
        tool_name: str,
        tool_input: dict[str, Any],
        ctx: ToolPermissionContext,
    ):
        if tool_name not in DESTRUCTIVE_TOOLS:
            return PermissionResultAllow(updated_input=tool_input)

        request_id = str(uuid.uuid4())
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        state.pending_consents[(dispatch_id, request_id)] = fut
        await send_event(
            payload.dispatch_id,
            {
                "type": "permission_request",
                "data": {
                    "id": request_id,
                    "tool": tool_name,
                    "input": tool_input,
                },
            },
        )
        try:
            decision = await asyncio.wait_for(fut, timeout=TOOL_CONSENT_TIMEOUT_S)
        except asyncio.TimeoutError:
            decision = "deny"
        finally:
            state.pending_consents.pop((dispatch_id, request_id), None)

        await send_event(
            payload.dispatch_id,
            {
                "type": "permission_response",
                "data": {"tool": tool_name, "decision": decision},
            },
        )
        if decision == "allow":
            return PermissionResultAllow(updated_input=tool_input)
        return PermissionResultDeny(message="Recipient denied", interrupt=False)

    # Step 3 — actually run the agent.
    await send_status(payload.dispatch_id, DispatchStatus.running)
    final = DispatchStatus.completed
    try:
        async for event in run_dispatch(
            payload, cwd=str(workspace), can_use_tool=can_use_tool
        ):
            await send_event(payload.dispatch_id, event)
            if event["type"] == "error":
                final = DispatchStatus.failed
    except Exception as exc:
        logger.exception("executor crashed")
        final = DispatchStatus.failed
        await send_event(
            payload.dispatch_id,
            {
                "type": "error",
                "data": {"message": str(exc), "exception": type(exc).__name__},
            },
        )

    await send_status(payload.dispatch_id, final)


def main() -> int:
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    args = parse_args()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _shutdown(*_):
        for task in asyncio.all_tasks(loop):
            task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown)
        except NotImplementedError:
            pass

    try:
        return loop.run_until_complete(run_session(args))
    except asyncio.CancelledError:
        return 0
    finally:
        loop.close()


if __name__ == "__main__":
    sys.exit(main())
