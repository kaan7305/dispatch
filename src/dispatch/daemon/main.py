"""Recipient daemon.

Usage:
  python -m dispatch.daemon \\
    --broker http://localhost:8000 \\
    --token <jwt> \\
    --ui-port 8001

What it does:
  - Hosts a local web UI on --ui-port (consent + execution view).
  - Connects to the broker over WebSocket at /agent/connect.
  - For each new dispatch:
      1. Prompts the local UI for accept/reject.
      2. If accepted, runs the agent via run_dispatch() with a
         can_use_tool callback that asks the local UI for Allow/Deny
         on Write/Edit/Bash.
      3. Streams every event back to the broker so the sender sees it.

The Claude Agent SDK runs on this machine using THIS user's
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
import webbrowser
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

logger = logging.getLogger("dispatch.daemon")


def _broker_ws_url(broker: str, path: str, token: str) -> str:
    p = urlparse(broker)
    scheme = "wss" if p.scheme == "https" else "ws"
    return urlunparse((scheme, p.netloc, path, "", f"token={token}", ""))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="dispatch.daemon")
    parser.add_argument(
        "--broker",
        default=os.environ.get("DISPATCH_BROKER", "http://localhost:8000"),
        help="Broker base URL (http or https). Default: $DISPATCH_BROKER or http://localhost:8000",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("DISPATCH_TOKEN"),
        help="JWT bearer token. Default: $DISPATCH_TOKEN",
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
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="Don't try to open the local UI in a browser automatically.",
    )
    return parser.parse_args(argv)


async def serve_local_ui(state: LocalState, port: int) -> uvicorn.Server:
    config = uvicorn.Config(
        make_app(state),
        host="127.0.0.1",
        port=port,
        log_level="warning",
        lifespan="on",
    )
    server = uvicorn.Server(config)
    asyncio.create_task(server.serve())
    # Wait for it to actually be ready.
    for _ in range(40):
        await asyncio.sleep(0.05)
        if server.started:
            return server
    return server


async def run_session(args: argparse.Namespace) -> int:
    if not args.token:
        print("error: --token is required (or set DISPATCH_TOKEN)", file=sys.stderr)
        return 2
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "warning: ANTHROPIC_API_KEY is not set; the agent will fail to run.",
            file=sys.stderr,
        )

    workspace = Path(args.workspace).expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=True)

    state = LocalState()
    server = await serve_local_ui(state, args.ui_port)
    ui_url = f"http://127.0.0.1:{args.ui_port}"
    print(f"[daemon] local consent UI: {ui_url}")
    if not args.no_open:
        try:
            webbrowser.open(ui_url)
        except Exception:
            pass

    ws_url = _broker_ws_url(args.broker, "/agent/connect", args.token)
    print(f"[daemon] connecting to broker: {args.broker}")
    try:
        async with websockets.connect(ws_url, max_size=None) as ws:
            print("[daemon] connected. Waiting for dispatches…")
            await handle_broker(ws, state, workspace)
    except websockets.InvalidStatus as e:
        print(f"[daemon] handshake failed: {e}", file=sys.stderr)
        return 3
    except OSError as e:
        print(f"[daemon] could not reach broker: {e}", file=sys.stderr)
        return 4
    finally:
        server.should_exit = True
    return 0


async def handle_broker(
    ws: "websockets.WebSocketClientProtocol",
    state: LocalState,
    workspace: Path,
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
        if msg.get("type") == "new_dispatch":
            try:
                payload = DispatchPayload(**msg["payload"])
            except Exception:
                logger.exception("rejecting malformed dispatch")
                continue
            asyncio.create_task(
                process_dispatch(payload, state, workspace, send_status, send_event)
            )


async def process_dispatch(
    payload: DispatchPayload,
    state: LocalState,
    workspace: Path,
    send_status,
    send_event,
) -> None:
    print(f"[daemon] new dispatch from {payload.sender_id}: {payload.task!r}")
    decision_fut = state.add_dispatch(payload)
    await send_status(payload.dispatch_id, DispatchStatus.delivered)

    try:
        top_decision = await asyncio.wait_for(decision_fut, timeout=300)
    except asyncio.TimeoutError:
        state.mark_status(payload.dispatch_id, DispatchStatus.expired)
        await send_status(payload.dispatch_id, DispatchStatus.expired)
        return

    if top_decision != "accept":
        state.mark_status(payload.dispatch_id, DispatchStatus.denied)
        await send_status(payload.dispatch_id, DispatchStatus.denied)
        return

    state.mark_status(payload.dispatch_id, DispatchStatus.accepted)
    await send_status(payload.dispatch_id, DispatchStatus.accepted)

    async def can_use_tool(
        tool_name: str,
        tool_input: dict[str, Any],
        ctx: ToolPermissionContext,
    ):
        if tool_name not in DESTRUCTIVE_TOOLS:
            return PermissionResultAllow(updated_input=tool_input)

        # Forward the prompt to the sender so they can see it.
        await send_event(
            payload.dispatch_id,
            {
                "type": "permission_request",
                "data": {"tool": tool_name, "input": tool_input},
            },
        )
        _req_id, decision = await state.request_tool_consent(
            payload.dispatch_id, tool_name, tool_input
        )
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

    state.mark_status(payload.dispatch_id, DispatchStatus.running)
    await send_status(payload.dispatch_id, DispatchStatus.running)

    final = DispatchStatus.completed
    try:
        async for event in run_dispatch(
            payload, cwd=str(workspace), can_use_tool=can_use_tool
        ):
            state.record_event(payload.dispatch_id, event)
            await send_event(payload.dispatch_id, event)
            if event["type"] == "error":
                final = DispatchStatus.failed
    except Exception as exc:
        logger.exception("executor crashed")
        final = DispatchStatus.failed
        err_event: DispatchEvent = {
            "type": "error",
            "data": {"message": str(exc), "exception": type(exc).__name__},
        }
        state.record_event(payload.dispatch_id, err_event)
        await send_event(payload.dispatch_id, err_event)

    state.mark_status(payload.dispatch_id, final)
    await send_status(payload.dispatch_id, final)


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(name)s %(levelname)s %(message)s")
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
