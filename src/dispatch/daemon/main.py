"""Recipient daemon — pure WebSocket client.

Usage:
  dispatch-daemon                       # uses saved config from ~/.dispatch/config.json
  dispatch-daemon --broker URL --token JWT   # explicit; also saves config for next time
  python -m dispatch.daemon ...         # equivalent if installed in a venv

What it does:
  - Connects to the broker via WebSocket as the authenticated user.
  - For each `new_dispatch` from the broker:
      1. Sends `dispatch_status: delivered`.
      2. Waits for `dispatch_decision: accept` or `reject` from the broker
         (which the recipient triggered in the unified web UI).
      3. If accepted, runs run_dispatch() — destructive tool calls cause a
         `permission_request` event to flow back through the broker to the
         UI, where the user clicks Allow / Deny. The decision returns via
         a `tool_approval` message from the broker.
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

import websockets
from claude_agent_sdk import (
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

from dispatch.executor import run_dispatch
from dispatch.shared.schema import DispatchEvent, DispatchPayload, DispatchStatus

DESTRUCTIVE_TOOLS = frozenset({"Write", "Edit", "Bash"})
DEFAULT_WORKSPACE = Path.cwd() / "workspace"
DISPATCH_DECISION_TIMEOUT_S = 300.0     # how long the recipient has to accept/reject
TOOL_APPROVAL_TIMEOUT_S = 120.0          # how long they have to allow/deny one tool call
CONFIG_PATH = Path.home() / ".dispatch" / "config.json"

logger = logging.getLogger("dispatch.daemon")


def _load_config() -> dict:
    """Reads ~/.dispatch/config.json. Returns {} if absent or malformed."""
    try:
        return json.loads(CONFIG_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_config(broker: str, token: str) -> None:
    """Persists broker + token so the next run can be a bare `dispatch-daemon`."""
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps({"broker": broker, "token": token}, indent=2))
        CONFIG_PATH.chmod(0o600)  # bearer token — keep it private
    except OSError:
        logger.warning("could not save config to %s", CONFIG_PATH)


@dataclass
class DaemonState:
    # dispatch_id (str) → Future resolved with "accept" | "reject"
    pending_decisions: dict[str, asyncio.Future] = field(default_factory=dict)
    # (dispatch_id, request_id) → Future resolved with "allow" | "deny"
    pending_approvals: dict[tuple[str, str], asyncio.Future] = field(default_factory=dict)


def _broker_ws_url(broker: str, token: str) -> str:
    p = urlparse(broker)
    scheme = "wss" if p.scheme == "https" else "ws"
    return urlunparse((scheme, p.netloc, "/agent/connect", "", f"token={token}", ""))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    # Resolution order for broker/token: CLI flag > env var > saved config file.
    config = _load_config()
    parser = argparse.ArgumentParser(prog="dispatch-daemon")
    parser.add_argument(
        "--broker",
        default=os.environ.get("DISPATCH_BROKER") or config.get("broker") or "http://localhost:8000",
        help="Broker base URL. Default: $DISPATCH_BROKER, then ~/.dispatch/config.json, then localhost.",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("DISPATCH_TOKEN") or config.get("token"),
        help="JWT bearer token. Default: $DISPATCH_TOKEN, then ~/.dispatch/config.json.",
    )
    parser.add_argument(
        "--workspace",
        default=os.environ.get("DISPATCH_WORKSPACE", str(DEFAULT_WORKSPACE)),
        help="Working directory the agent operates in. Default: ./workspace",
    )
    return parser.parse_args(argv)


async def run_session(args: argparse.Namespace) -> int:
    if not args.token:
        print(
            "error: no token. Sign in on the broker's web page, then either:\n"
            "  - run the one-line installer it shows you, or\n"
            "  - run: dispatch-daemon --broker <url> --token <jwt>",
            file=sys.stderr,
        )
        return 2

    # Remember broker + token so future runs can be a bare `dispatch-daemon`.
    _save_config(args.broker, args.token)

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
                "dispatches and respond to approval prompts."
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

        elif mtype == "dispatch_decision":
            did = msg.get("dispatch_id")
            decision = msg.get("decision")
            fut = state.pending_decisions.get(did)
            if fut and not fut.done() and decision in ("accept", "reject"):
                fut.set_result(decision)

        elif mtype == "tool_approval":
            did = msg.get("dispatch_id")
            req_id = msg.get("request_id")
            decision = msg.get("decision")
            fut = state.pending_approvals.get((did, req_id))
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

    # Step 2 — per-tool approval.
    async def can_use_tool(
        tool_name: str,
        tool_input: dict[str, Any],
        ctx: ToolPermissionContext,
    ):
        if tool_name not in DESTRUCTIVE_TOOLS:
            return PermissionResultAllow(updated_input=tool_input)

        request_id = str(uuid.uuid4())
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        state.pending_approvals[(dispatch_id, request_id)] = fut
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
            decision = await asyncio.wait_for(fut, timeout=TOOL_APPROVAL_TIMEOUT_S)
        except asyncio.TimeoutError:
            decision = "deny"
        finally:
            state.pending_approvals.pop((dispatch_id, request_id), None)

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
